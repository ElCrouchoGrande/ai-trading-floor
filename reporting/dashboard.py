"""
Live dashboard — public, read-only view of the trading desk.

Serves a single mobile-friendly HTML page (`DASHBOARD_HTML`) that polls
`/api/state` (`get_cached_state`) every 20s. The state is rebuilt from Postgres
at most once per `ttl` seconds because `get_portfolio_summary()` hits yfinance
for every open position — the cache is what stops polling clients from
hammering yfinance.

Trader avatars are generated client-side by DiceBear (CDN); if the image fails
to load the trader's emoji is shown instead, so a blocked CDN degrades cleanly.
"""
import json
import math
import time
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session
from sqlalchemy import select

from db.models import Report, Trade, Position, Debate
from db.session import get_session
from engine.pnl import PnLEngine
from engine.debate import humanize_transcript

logger = logging.getLogger(__name__)

# Fixed left-to-right order for the trader columns / cards.
TRADER_ORDER = ["momentum", "insider", "short", "regard", "boomer", "hugger"]

# Single source of truth for names, emoji fallbacks, sprite seeds and the
# distilled character bios (drawn from the personas in traders/all_traders.py).
TRADERS_META = {
    "momentum": {
        "name": "The Momentum Trader",
        "emoji": "📈",
        "seed": "momentum",
        "tagline": "The trend is your friend.",
        "bio": "Buys what's going up and sells what isn't. Doesn't care about "
               "valuation — expensive is fine as long as the chart keeps "
               "climbing. Decisive to a fault; sitting on the fence isn't her game.",
        "horizon": "2–6 weeks",
        "risk": "Medium–high · 10–30% of pot",
        "quirk": "Bails the moment price breaks below the 20-day moving average.",
    },
    "insider": {
        "name": "The Insider Tracker",
        "emoji": "🕵️",
        "seed": "insider",
        "tagline": "Follow the smart money.",
        "bio": "Trades off Form 4 filings — executives and insiders buying with "
               "their own cash. A cluster of buys is a strong tell; a lone "
               "purchase is a maybe. Mostly he just watches and waits.",
        "horizon": "60–90 days",
        "risk": "Low–medium · patient & selective",
        "quirk": "His most common answer, by far, is HOLD.",
    },
    "short": {
        "name": "The Short Seller",
        "emoji": "🐻",
        "seed": "short",
        "tagline": "Be forensic. Be contrarian. Be right eventually.",
        "bio": "A skeptic who profits when stocks fall. Hunts for unjustifiable "
               "multiples, fundamentals dressed up in PR, and meme plays "
               "disconnected from reality. Always names her stop and her target.",
        "horizon": "Weeks to months",
        "risk": "High · up to 40% of pot, losses uncapped",
        "quirk": "Keeps trading even on negative credits — “I've seen worse.”",
    },
    "regard": {
        "name": "The WSB Ape",
        "emoji": "🚀",
        "seed": "regard",
        "tagline": "To the moon. 💎🙌",
        "bio": "Trades on vibes, memes and whatever r/wallstreetbets is hyped "
               "about today. Anything a boomer would like gets passed on. "
               "Position size is a dice roll between 20% and 100% of the pot.",
        "horizon": "Until the moon or zero",
        "risk": "Maximum · full YOLO permitted",
        "quirk": "Quietly gets bailed out and reset whenever he blows up — and "
                 "he'll never admit it happened.",
    },
    "boomer": {
        "name": "The Boomer",
        "emoji": "👴",
        "seed": "boomer",
        "tagline": "This too shall pass.",
        "bio": "Investing since the 1980s — lived through Black Monday, "
               "dot-com, 2008 and COVID, and panics at none of it. Buys durable "
               "moats with real balance sheets and reinvests the dividends.",
        "horizon": "Months to years",
        "risk": "Low · quality and patience",
        "quirk": "Only touches his approved blue-chip list; the rest is “not in my wheelhouse.”",
    },
    "hugger": {
        "name": "The Index Hugger",
        "emoji": "🫂",
        "seed": "hugger",
        "tagline": "The market is efficient (probably).",
        "bio": "Believes you can't beat the index, so mostly doesn't try. Only "
               "deviates from cash on a HIGH-confidence call, and even then "
               "sizes tiny. Measures himself against the S&P 500, not in the abstract.",
        "horizon": "Until the signal fades",
        "risk": "Very low · max 15% per deviation",
        "quirk": "The control group — and he probably wins by doing nothing.",
    },
}


def _f(value):
    """Decimal/None -> float/None so the cached dict is JSON- and cache-safe.

    NaN/Inf become None: JSON doesn't have a representation for them, so a stray
    NaN (e.g. a yfinance miss on a delisted ticker) would otherwise 500 the
    response inside starlette's encoder.
    """
    if value is None:
        return None
    f = float(value)
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _jsonsafe(obj):
    """Recursively replace NaN/Inf floats with None so json.dumps can't choke."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _jsonsafe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonsafe(v) for v in obj]
    return obj


def _decode_risks(raw) -> list:
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
        return decoded if isinstance(decoded, list) else [str(decoded)]
    except (ValueError, TypeError):
        return [raw]


def _report_dict(r) -> dict:
    return {
        "ticker": r.ticker,
        "company_name": r.company_name,
        "analyst_rating": r.analyst_rating,
        "confidence": r.confidence,
        "price_target": _f(r.price_target),
        "price_at_report": _f(r.price_at_report),
        "trigger_type": r.trigger_type,
        "report_date": r.report_date.isoformat() if r.report_date else None,
        "summary": r.summary,
        "bull_case": r.bull_case,
        "bear_case": r.bear_case,
        "key_risks": _decode_risks(r.key_risks),
    }


def _trade_dict(t) -> dict:
    return {
        "trader_id": t.trader_id,
        "action": t.action,
        "ticker": t.ticker,
        "credits_risked": _f(t.credits_risked),
        "price_at_trade": _f(t.price_at_trade),
        "reasoning": t.reasoning,
        "pnl": _f(t.pnl),
        "traded_at": t.traded_at.isoformat() if t.traded_at else None,
    }


def build_dashboard_state(session: Session) -> dict:
    """Assemble the full dashboard payload as plain JSON-able types.

    Each section is built in isolation so a single broken section (a bad
    debate transcript, a yfinance hiccup, etc.) can't blank the whole page.
    Failures are logged and surfaced in the response's `_errors` list so the
    cause is visible by just opening /api/state in a browser.
    """
    errors: list[str] = []

    leaderboard: list[dict] = []
    try:
        pnl = PnLEngine(session)
        summary = pnl.get_portfolio_summary()
        ordered = sorted(
            summary.items(),
            key=lambda kv: kv[1]["total_value"],
            reverse=True,
        )
        leaderboard = [
            {
                "rank": i + 1,
                "trader_id": tid,
                "credits": data["credits"],
                "committed": data["committed"],
                "unrealised_pnl": data["unrealised_pnl"],
                "total_value": data["total_value"],
                "return_pct": data["return_pct"],
                "open_positions": data["open_positions"],
                "lifetime_resets": data["lifetime_resets"],
            }
            for i, (tid, data) in enumerate(ordered)
        ]
    except Exception as e:
        logger.exception("dashboard: leaderboard build failed")
        errors.append(f"leaderboard: {type(e).__name__}: {e}")

    reports_out: list[dict] = []
    try:
        reports = session.execute(
            select(Report).order_by(Report.report_date.desc()).limit(5)
        ).scalars().all()
        reports_out = [_report_dict(r) for r in reports]
    except Exception as e:
        logger.exception("dashboard: reports build failed")
        errors.append(f"reports: {type(e).__name__}: {e}")

    trades_out: list[dict] = []
    try:
        trades = session.execute(
            select(Trade)
            .where(Trade.action != "HOLD")
            .order_by(Trade.traded_at.desc())
            .limit(10)
        ).scalars().all()
        trades_out = [_trade_dict(t) for t in trades]
    except Exception as e:
        logger.exception("dashboard: trades build failed")
        errors.append(f"trades: {type(e).__name__}: {e}")

    positions_out: list[dict] = []
    try:
        positions = session.execute(select(Position)).scalars().all()
        positions_out = [
            {
                "trader_id": p.trader_id,
                "ticker": p.ticker,
                "direction": p.direction,
                "credits_risked": _f(p.credits_risked),
                "entry_price": _f(p.entry_price),
                "entry_date": p.entry_date.isoformat() if p.entry_date else None,
            }
            for p in positions
        ]
    except Exception as e:
        logger.exception("dashboard: positions build failed")
        errors.append(f"positions: {type(e).__name__}: {e}")

    debates_out: list[dict] = []
    try:
        debates = session.execute(
            select(Debate).order_by(Debate.debated_at.desc()).limit(5)
        ).scalars().all()
        debates_out = [
            {
                "ticker": d.ticker,
                "conflicting_traders": list(d.conflicting_traders or []),
                "resolution": humanize_transcript(d.resolution),
                "transcript": humanize_transcript(d.debate_transcript),
                "debated_at": d.debated_at.isoformat() if d.debated_at else None,
            }
            for d in debates
        ]
    except Exception as e:
        logger.exception("dashboard: debates build failed")
        errors.append(f"debates: {type(e).__name__}: {e}")

    return {
        "leaderboard": leaderboard,
        "reports": reports_out,
        "trades": trades_out,
        "positions": positions_out,
        "debates": debates_out,
        "_errors": errors,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


_CACHE = {"at": 0.0, "data": None}


def get_cached_state(session_factory, ttl: int = 60) -> dict:
    """Return the dashboard state, rebuilding from the DB at most once per ttl.

    Wrapped in a top-level try/except so failures outside build_dashboard_state's
    per-section guards (e.g. a dead DB connection during get_session) still
    produce a valid JSON response with the exception surfaced in `_errors` —
    that way the cause is visible by just opening /api/state.
    """
    now = time.time()
    if _CACHE["data"] is not None and (now - _CACHE["at"]) < ttl:
        return _CACHE["data"]

    try:
        with get_session(session_factory) as session:
            data = build_dashboard_state(session)
    except Exception as e:
        logger.exception("dashboard: top-level build failed")
        return {
            "leaderboard": [],
            "reports": [],
            "trades": [],
            "positions": [],
            "debates": [],
            "_errors": [f"top-level: {type(e).__name__}: {e}"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    data = _jsonsafe(data)
    _CACHE["at"] = now
    _CACHE["data"] = data
    return data


def build_reports_archive(session: Session, limit: int = 100) -> dict:
    """Full(er) DD history for the archive view. No yfinance, so no cache needed."""
    reports = session.execute(
        select(Report).order_by(Report.report_date.desc()).limit(limit)
    ).scalars().all()
    return {
        "reports": [_report_dict(r) for r in reports],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


_REPORTS_CACHE = {"at": 0.0, "data": None}


def get_reports_archive(session_factory, limit: int = 100, ttl: int = 60) -> dict:
    now = time.time()
    if _REPORTS_CACHE["data"] is not None and (now - _REPORTS_CACHE["at"]) < ttl:
        return _REPORTS_CACHE["data"]
    with get_session(session_factory) as session:
        data = build_reports_archive(session, limit)
    data = _jsonsafe(data)
    _REPORTS_CACHE["at"] = now
    _REPORTS_CACHE["data"] = data
    return data


def build_trades_archive(session: Session, limit: int = 200) -> dict:
    """Full(er) trade history for the activity view. No yfinance, so no cache."""
    trades = session.execute(
        select(Trade)
        .where(Trade.action != "HOLD")
        .order_by(Trade.traded_at.desc())
        .limit(limit)
    ).scalars().all()
    return {
        "trades": [_trade_dict(t) for t in trades],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


_TRADES_CACHE = {"at": 0.0, "data": None}


def get_trades_archive(session_factory, limit: int = 200, ttl: int = 60) -> dict:
    now = time.time()
    if _TRADES_CACHE["data"] is not None and (now - _TRADES_CACHE["at"]) < ttl:
        return _TRADES_CACHE["data"]
    with get_session(session_factory) as session:
        data = build_trades_archive(session, limit)
    data = _jsonsafe(data)
    _TRADES_CACHE["at"] = now
    _TRADES_CACHE["data"] = data
    return data


_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trade Off — Live</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Press+Start+2P&family=VT323&display=swap" rel="stylesheet">
<style>
  :root { --bg:#211d33; --panel:#332b4f; --panel2:#3f3660; --line:#6b5b95;
          --text:#efeefc; --muted:#a89fce; --green:#7ed957; --red:#ff5c7a;
          --accent:#b39ddb; --ink:#1a1330;
          --font-head:'Press Start 2P', 'Courier New', monospace;
          --font-body:'VT323', 'Courier New', monospace; }
  * { box-sizing: border-box; }
  body { font-family: var(--font-body); font-size: 18px;
         background: var(--bg); color: var(--text); margin: 0;
         padding: 16px 14px 64px; max-width: 760px; margin: 0 auto;
         -webkit-text-size-adjust: 100%; }
  h1 { font-family: var(--font-head); font-size: 18px; line-height: 1.35;
       margin: 4px 0 6px; color: var(--accent); text-align: center; }
  h2 { font-family: var(--font-head); font-size: 12px; line-height: 1.4;
       margin: 0 0 12px; color: var(--text); }
  .sub { color: var(--muted); font-size: 17px; margin: 0 0 14px; text-align: center; }
  .sprite-row { display: flex; gap: 10px; justify-content: center; flex-wrap: wrap;
                margin: 0 0 18px; }
  .sprite-row .s { width: 56px; text-align: center; cursor: pointer; }
  .sprite-row .s .nm { display: block; font-size: 12px; color: var(--muted);
                       line-height: 1.15; margin-top: 4px; word-break: break-word; }
  .tabs { display: flex; gap: 8px; margin: 14px 0 18px; }
  .tab { flex: 1; text-align: center; padding: 10px 6px; border-radius: 0;
         background: var(--panel); color: var(--muted); font-family: var(--font-head);
         font-size: 10px; line-height: 1.4; cursor: pointer; border: 2px solid var(--line);
         user-select: none; }
  .tab.active { background: var(--accent); color: var(--ink); border-color: var(--accent); }
  .panel { background: var(--panel); border: 2px solid var(--line);
           border-radius: 0; padding: 14px; margin: 0 0 16px;
           box-shadow: 4px 4px 0 rgba(0,0,0,.35); }
  .panel > h2 { display: flex; align-items: center; gap: 8px; }
  .avatar { border-radius: 0; background: var(--panel2); vertical-align: middle;
            object-fit: cover; image-rendering: pixelated; }
  .av-emoji { display: inline-flex; align-items: center; justify-content: center;
              background: var(--panel2); border-radius: 0; vertical-align: middle;
              line-height: 1; }
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; font-size: 13px; text-transform: uppercase;
       letter-spacing: .04em; color: var(--muted); padding: 6px 6px; }
  td { padding: 8px 6px; border-top: 1px solid var(--line); font-size: 16px;
       vertical-align: middle; }
  .trader-cell { display: flex; align-items: center; gap: 8px; }
  .trader-cell .nm { font-weight: 600; }
  .positive { color: var(--green); font-weight: 600; }
  .negative { color: var(--red); font-weight: 600; }
  .muted { color: var(--muted); }
  .nowrap { white-space: nowrap; }
  .right { text-align: right; }
  .tag { display: inline-block; padding: 3px 6px; border-radius: 0;
         font-family: var(--font-head); font-size: 9px; letter-spacing: .02em; }
  .buy, .long { background: rgba(34,197,94,.16); color: var(--green); }
  .sell, .short, .cover { background: rgba(239,68,68,.16); color: var(--red); }
  .hold { background: rgba(139,147,161,.16); color: var(--muted); }
  .reset { color: var(--accent); font-size: 11px; margin-left: 6px; }
  .card { background: var(--panel2); border: 2px solid var(--line);
          border-radius: 0; padding: 12px; margin: 0 0 10px; }
  .card-head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
  .card-head .tk { font-weight: 700; font-size: 15px; }
  .card-head .co { color: var(--muted); font-size: 12px; }
  .meta { color: var(--muted); font-size: 13px; margin-top: 6px; }
  .body { font-size: 16px; line-height: 1.45; margin: 8px 0 0; }
  details { margin-top: 8px; }
  details summary { cursor: pointer; color: var(--accent); font-size: 12px;
                    font-weight: 600; list-style: none; }
  details summary::-webkit-details-marker { display: none; }
  details summary::before { content: "▸ "; }
  details[open] summary::before { content: "▾ "; }
  /* Collapsible cards: DD reports + trader activity rows (title-only until opened) */
  details.collapse { background: var(--panel2); border: 2px solid var(--line);
                     border-radius: 0; padding: 10px 12px; margin: 0 0 8px; }
  details.collapse > summary { cursor: pointer; list-style: none; display: flex;
                     align-items: center; gap: 7px; flex-wrap: wrap; color: var(--text);
                     font-size: 15px; }
  details.collapse > summary::-webkit-details-marker { display: none; }
  details.collapse > summary::before { content: "▸"; color: var(--accent); font-weight: 700; }
  details.collapse[open] > summary::before { content: "▾"; }
  details.collapse.is-hold { opacity: .6; }
  details.collapse > summary .tk { font-weight: 700; font-size: 14px; }
  details.collapse > summary .nm { font-weight: 600; }
  details.collapse > summary .co { color: var(--muted); font-size: 12px; }
  details.collapse > summary .push { margin-left: auto; }
  .collapse-body { margin-top: 10px; }
  .collapse-body .why { font-size: 15px; color: var(--muted); line-height: 1.4; }
  .case { font-size: 15px; line-height: 1.45; margin: 8px 0; }
  .case b { color: var(--text); }
  .case.bull b { color: var(--green); }
  .case.bear b { color: var(--red); }
  .case ul { margin: 4px 0 0; padding-left: 18px; }
  .trade { display: flex; align-items: flex-start; gap: 10px; padding: 10px 0;
           border-top: 1px solid var(--line); }
  .trade.is-hold { opacity: .55; }
  .trade .t-main { flex: 1; min-width: 0; }
  .trade .t-line { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .trade .why { font-size: 12px; color: var(--muted); line-height: 1.45; margin-top: 4px; }
  .matrix-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .matrix th, .matrix td { text-align: center; white-space: nowrap; padding: 7px 5px; }
  .matrix th.tk-col, .matrix td.tk-col { text-align: left; font-weight: 700; position: sticky; left: 0; background: var(--panel); }
  .matrix .cell-long { color: var(--green); font-weight: 600; font-size: 12px; }
  .matrix .cell-short { color: var(--red); font-weight: 600; font-size: 12px; }
  .debate .d-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
                    font-size: 15px; }
  .debate .vs { color: var(--muted); }
  .transcript { white-space: pre-wrap; font-size: 15px; line-height: 1.5;
                background: var(--bg); border: 2px solid var(--line);
                border-radius: 0; padding: 10px; margin-top: 8px; }
  .res { font-size: 15px; margin-top: 8px; }
  .bio-card { display: flex; gap: 14px; align-items: flex-start; }
  .bio-card .bio-main { flex: 1; min-width: 0; }
  .bio-card .nm { font-family: var(--font-head); font-size: 12px; line-height: 1.4; }
  .bio-card .tagline { color: var(--accent); font-size: 16px; font-style: italic;
                       margin: 4px 0 8px; }
  .bio-card .desc { font-size: 16px; line-height: 1.45; }
  .bio-attrs { margin-top: 10px; font-size: 15px; }
  .bio-attrs div { margin: 3px 0; }
  .bio-attrs .k { color: var(--muted); display: inline-block; min-width: 64px; }
  .empty { color: var(--muted); font-size: 15px; padding: 6px 0; }
  .hidden { display: none; }
  .updated { text-align: center; color: var(--muted); font-size: 11px; margin-top: 8px; }
  .disclaimer { text-align: center; color: var(--muted); font-size: 13px;
                line-height: 1.5; margin: 4px auto 18px; max-width: 560px; }
  .more { display: inline-block; margin-top: 10px; color: var(--accent);
          font-size: 16px; cursor: pointer; }
</style>
</head>
<body>
  <h1>TRADE OFF</h1>
  <p class="sub">live agents, live markets, live crash outs</p>
  <div class="sprite-row" id="sprite-row"></div>

  <div class="tabs">
    <div class="tab active" data-view="live" onclick="showView('live')">Live</div>
    <div class="tab" data-view="archive" onclick="showView('archive')">Archive</div>
    <div class="tab" data-view="activity" onclick="showView('activity')">Activity</div>
    <div class="tab" data-view="traders" onclick="showView('traders')">Traders</div>
  </div>

  <div id="view-live">
    <div class="panel" id="reports-panel">
      <h2>📋 Latest DD</h2>
      <div id="reports"><p class="empty">Loading…</p></div>
      <a class="more" onclick="showView('archive')">View all DDs →</a>
    </div>
    <div class="panel">
      <h2>🏆 League Table</h2>
      <div id="league"><p class="empty">Loading…</p></div>
    </div>
    <div class="panel">
      <h2>⚡ Recent Trades</h2>
      <div id="trades"><p class="empty">Loading…</p></div>
      <a class="more" onclick="showView('activity')">View all activity →</a>
    </div>
    <div class="panel">
      <h2>🥊 Debates</h2>
      <div id="debates"><p class="empty">Loading…</p></div>
    </div>
    <div class="panel">
      <h2>🧭 Open Positions</h2>
      <div id="positions"><p class="empty">Loading…</p></div>
    </div>
    <p class="updated" id="updated"></p>
    <p class="disclaimer">Simulation only. Fictional AI agents trade imaginary
      credits against real market prices — no real money, no real positions.
      Nothing here is investment advice or a recommendation to buy or sell
      anything.</p>
  </div>

  <div id="view-archive" class="hidden">
    <div class="panel">
      <h2>🗄️ DD Archive</h2>
      <div id="archive"><p class="empty">Loading…</p></div>
    </div>
  </div>

  <div id="view-activity" class="hidden">
    <div class="panel">
      <h2>⚡ Trade History</h2>
      <div id="activity"><p class="empty">Loading…</p></div>
    </div>
  </div>

  <div id="view-traders" class="hidden">
    <div id="bios"></div>
  </div>

<script>
const META = __TRADERS_META__;
const ORDER = __TRADER_ORDER__;

function esc(s) {
  if (s === null || s === undefined) return "";
  return String(s).replace(/[&<>"']/g, c => (
    {"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}[c]
  ));
}

function avatar(traderId, size) {
  const m = META[traderId] || {};
  const seed = encodeURIComponent(m.seed || traderId);
  const url = "https://api.dicebear.com/9.x/pixel-art/svg?seed=" + seed;
  const emoji = m.emoji || "❓";
  const px = size + "px";
  // <img> with an emoji fallback if the CDN image fails to load.
  return '<img class="avatar" width="' + size + '" height="' + size +
    '" alt="" src="' + url + '" ' +
    'onerror="this.outerHTML=\\'<span class=&quot;av-emoji&quot; style=&quot;width:' + px +
    ';height:' + px + ';font-size:' + Math.round(size*0.62) + 'px&quot;>' + emoji + '</span>\\'">';
}

function name(traderId) {
  return (META[traderId] && META[traderId].name) || traderId;
}

function showView(v) {
  document.getElementById("view-live").classList.toggle("hidden", v !== "live");
  document.getElementById("view-archive").classList.toggle("hidden", v !== "archive");
  document.getElementById("view-activity").classList.toggle("hidden", v !== "activity");
  document.getElementById("view-traders").classList.toggle("hidden", v !== "traders");
  document.querySelectorAll(".tab").forEach(t =>
    t.classList.toggle("active", t.dataset.view === v));
  if (v === "archive") loadArchive();
  if (v === "activity") loadActivity();
}

async function loadArchive() {
  const el = document.getElementById("archive");
  el.innerHTML = '<p class="empty">Loading…</p>';
  try {
    const res = await fetch("/api/reports", {cache: "no-store"});
    const s = await res.json();
    el.innerHTML = renderReports(s.reports || []);
  } catch (e) {
    el.innerHTML = '<p class="empty">Could not load archive.</p>';
  }
}

async function loadActivity() {
  const el = document.getElementById("activity");
  el.innerHTML = '<p class="empty">Loading…</p>';
  try {
    const res = await fetch("/api/trades", {cache: "no-store"});
    const s = await res.json();
    el.innerHTML = renderTrades(s.trades || []);
  } catch (e) {
    el.innerHTML = '<p class="empty">Could not load activity.</p>';
  }
}

function renderSpriteRow() {
  let h = "";
  for (const tid of ORDER) {
    h += '<div class="s" onclick="showView(\\'traders\\')" title="' + esc(name(tid)) + '">' +
      avatar(tid, 52) + '<span class="nm">' + esc(name(tid)) + '</span></div>';
  }
  document.getElementById("sprite-row").innerHTML = h;
}

function num(n, dp) { return (n === null || n === undefined) ? "—" : Number(n).toFixed(dp === undefined ? 2 : dp); }
function signed(n, dp) {
  if (n === null || n === undefined) return "—";
  const v = Number(n).toFixed(dp === undefined ? 2 : dp);
  return (n >= 0 ? "+" : "") + v;
}
function cls(n) { return Number(n) >= 0 ? "positive" : "negative"; }
function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, {month:"short", day:"numeric"}) + " " +
         d.toLocaleTimeString(undefined, {hour:"2-digit", minute:"2-digit"});
}

function renderLeague(rows) {
  if (!rows.length) { return '<p class="empty">No traders yet.</p>'; }
  let h = '<div class="matrix-wrap"><table><tr><th>#</th><th>Trader</th>' +
          '<th class="right">Cash</th><th class="right">Invested</th>' +
          '<th class="right">Unreal.</th><th class="right">Value</th>' +
          '<th class="right">Return</th><th class="right">Pos</th></tr>';
  for (const r of rows) {
    const resets = (r.trader_id === "regard" && r.lifetime_resets > 0)
      ? '<span class="reset" title="Lifetime blow-ups">💀×' + r.lifetime_resets + '</span>' : '';
    h += '<tr><td class="muted">' + r.rank + '</td>' +
      '<td><span class="trader-cell">' + avatar(r.trader_id, 28) +
      '<span class="nm">' + esc(name(r.trader_id)) + resets + '</span></span></td>' +
      '<td class="right nowrap">' + num(r.credits) + '</td>' +
      '<td class="right nowrap">' + num(r.committed) + '</td>' +
      '<td class="right nowrap ' + cls(r.unrealised_pnl) + '">' + signed(r.unrealised_pnl) + '</td>' +
      '<td class="right nowrap"><strong>' + num(r.total_value) + '</strong></td>' +
      '<td class="right nowrap ' + cls(r.return_pct) + '">' + signed(r.return_pct, 1) + '%</td>' +
      '<td class="right">' + r.open_positions + '</td></tr>';
  }
  return h + '</table></div>';
}

function renderReports(reports) {
  if (!reports.length) { return '<p class="empty">No reports yet.</p>'; }
  let h = "";
  for (const r of reports) {
    const rating = (r.analyst_rating || "HOLD").toLowerCase();
    const risks = (r.key_risks || []).map(x => '<li>' + esc(x) + '</li>').join("");
    h += '<details class="collapse"><summary>' +
      '<span class="tk">' + esc(r.ticker) + '</span>' +
      '<span class="tag ' + rating + '">' + esc(r.analyst_rating || "—") + '</span>' +
      (r.price_target ? '<span class="co">🎯 $' + num(r.price_target) + '</span>' : '') +
      (r.confidence ? '<span class="co">· ' + esc(r.confidence) + ' conf.</span>' : '') +
      '<span class="co push">' + when(r.report_date) + '</span>' +
      '</summary><div class="collapse-body">';
    if (r.company_name) h += '<div class="meta">' + esc(r.company_name) + '</div>';
    if (r.summary) h += '<p class="body">' + esc(r.summary) + '</p>';
    if (r.bull_case) h += '<p class="case bull"><b>Bull:</b> ' + esc(r.bull_case) + '</p>';
    if (r.bear_case) h += '<p class="case bear"><b>Bear:</b> ' + esc(r.bear_case) + '</p>';
    if (risks) h += '<div class="case"><b>Key risks:</b><ul>' + risks + '</ul></div>';
    h += '<div class="meta">' + esc(r.trigger_type || "") + ' · ' + when(r.report_date) + '</div>';
    h += '</div></details>';
  }
  return h;
}

function renderTrades(trades) {
  if (!trades.length) { return '<p class="empty">No activity yet.</p>'; }
  let h = "";
  for (const t of trades) {
    const act = (t.action || "").toLowerCase();
    const isHold = act === "hold";
    const pnl = (t.pnl !== null && t.pnl !== undefined)
      ? '<span class="' + cls(t.pnl) + '">' + signed(t.pnl) + '</span>' : '';
    h += '<details class="collapse' + (isHold ? ' is-hold' : '') + '"><summary>' +
      avatar(t.trader_id, 24) +
      '<span class="nm">' + esc(name(t.trader_id)) + '</span>' +
      '<span class="tag ' + act + '">' + esc(t.action) + '</span>' +
      '<span class="nm">' + esc(t.ticker) + '</span>' +
      (t.price_at_trade ? '<span class="co">@ $' + num(t.price_at_trade) + '</span>' : '') +
      (t.credits_risked ? '<span class="co">· ' + num(t.credits_risked) + ' cr</span>' : '') +
      pnl +
      '<span class="co push">' + when(t.traded_at) + '</span>' +
      '</summary><div class="collapse-body">' +
      (t.reasoning ? '<div class="why">' + esc(t.reasoning) + '</div>'
                   : '<div class="why">No reasoning recorded.</div>') +
      '</div></details>';
  }
  return h;
}

function renderPositions(positions) {
  if (!positions.length) { return '<p class="empty">No open positions.</p>'; }
  const tickers = [...new Set(positions.map(p => p.ticker))].sort();
  const map = {};
  for (const p of positions) { map[p.ticker + "|" + p.trader_id] = p; }
  let h = '<div class="matrix-wrap"><table class="matrix"><tr><th class="tk-col">Ticker</th>';
  for (const tid of ORDER) {
    h += '<th title="' + esc(name(tid)) + '">' + avatar(tid, 26) + '</th>';
  }
  h += '</tr>';
  for (const tk of tickers) {
    h += '<tr><td class="tk-col">' + esc(tk) + '</td>';
    for (const tid of ORDER) {
      const p = map[tk + "|" + tid];
      if (!p) { h += '<td class="muted">·</td>'; continue; }
      const c = p.direction === "LONG" ? "cell-long" : "cell-short";
      h += '<td class="' + c + '">' + esc(p.direction) + '<br>' + num(p.credits_risked, 0) + '</td>';
    }
    h += '</tr>';
  }
  return h + '</table></div>';
}

function renderDebates(debates) {
  if (!debates.length) { return '<p class="empty">No debates yet.</p>'; }
  let h = "";
  for (const d of debates) {
    const parts = (d.conflicting_traders || []).map(tid =>
      '<span class="trader-cell">' + avatar(tid, 24) + '<span>' + esc(name(tid)) + '</span></span>');
    h += '<div class="card debate"><div class="d-head">' +
      '<span class="tk">' + esc(d.ticker) + '</span>' +
      (parts.length ? parts[0] + '<span class="vs">vs</span>' + (parts[1] || "") : "") +
      '</div>';
    if (d.resolution) h += '<div class="res"><b>Resolution:</b> ' + esc(d.resolution) + '</div>';
    if (d.transcript) {
      h += '<details><summary>Read the full debate</summary>' +
        '<div class="transcript">' + esc(d.transcript) + '</div></details>';
    }
    h += '<div class="meta">' + when(d.debated_at) + '</div></div>';
  }
  return h;
}

function renderBios() {
  let h = "";
  for (const tid of ORDER) {
    const m = META[tid];
    if (!m) continue;
    h += '<div class="panel"><div class="bio-card">' + avatar(tid, 72) +
      '<div class="bio-main">' +
      '<div class="nm">' + esc(m.name) + ' ' + (m.emoji || "") + '</div>' +
      '<div class="tagline">' + esc(m.tagline) + '</div>' +
      '<div class="desc">' + esc(m.bio) + '</div>' +
      '<div class="bio-attrs">' +
      '<div><span class="k">Horizon</span> ' + esc(m.horizon) + '</div>' +
      '<div><span class="k">Risk</span> ' + esc(m.risk) + '</div>' +
      '<div><span class="k">Quirk</span> ' + esc(m.quirk) + '</div>' +
      '</div></div></div></div>';
  }
  document.getElementById("bios").innerHTML = h;
}

async function refresh() {
  try {
    const res = await fetch("/api/state", {cache: "no-store"});
    const s = await res.json();
    document.getElementById("league").innerHTML = renderLeague(s.leaderboard || []);
    document.getElementById("reports").innerHTML = renderReports(s.reports || []);
    document.getElementById("trades").innerHTML = renderTrades(s.trades || []);
    document.getElementById("positions").innerHTML = renderPositions(s.positions || []);
    document.getElementById("debates").innerHTML = renderDebates(s.debates || []);
    document.getElementById("updated").textContent =
      "Updated " + when(s.generated_at);
  } catch (e) {
    document.getElementById("updated").textContent = "Connection lost — retrying…";
  }
}

renderBios();
renderSpriteRow();
refresh();
setInterval(refresh, 20000);
</script>
</body>
</html>
"""

DASHBOARD_HTML = (
    _PAGE_TEMPLATE
    .replace("__TRADERS_META__", json.dumps(TRADERS_META))
    .replace("__TRADER_ORDER__", json.dumps(TRADER_ORDER))
)
