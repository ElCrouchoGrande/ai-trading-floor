"""
Weekly report — builds the HTML digest, served via the admin /weekly/preview endpoint.
Uses Haiku for formatting, Sonnet handles the substantive work upstream.
"""
import os
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy.orm import Session
from sqlalchemy import select

from db.models import Report, Trade, Debate, Ledger, LedgerSnapshot
from engine.pnl import PnLEngine
from engine.debate import humanize_transcript

logger = logging.getLogger(__name__)

TRADER_DISPLAY = {
    "momentum": "📈 Momentum Trader",
    "insider":  "🕵️ Insider Tracker",
    "short":    "🐻 Short Seller",
    "regard":   "🚀 WSB Ape",
    "boomer":   "👴 The Boomer",
    "hugger":   "🫂 Index Hugger",
}

MEDAL = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣"]


class WeeklyReporter:

    def __init__(self, session: Session):
        self.session = session
        self.pnl_engine = PnLEngine(session)

    def _build_html(self, week_start: datetime, week_end: datetime) -> str:
        portfolio = self.pnl_engine.get_portfolio_summary()
        leaderboard = sorted(portfolio.items(), key=lambda x: x[1]["total_value"], reverse=True)

        reports = self._get_week_reports(week_start)
        trades = self._get_week_trades(week_start)
        debates = self._get_week_debates(week_start)
        regard_data = portfolio.get("regard", {})

        date_str = week_end.strftime("%d %B %Y")

        html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          max-width: 680px; margin: 0 auto; padding: 24px; color: #1a1a1a; }}
  h1 {{ font-size: 24px; border-bottom: 3px solid #1a1a1a; padding-bottom: 12px; }}
  h2 {{ font-size: 18px; margin-top: 32px; color: #2c2c2c; }}
  h3 {{ font-size: 15px; color: #444; margin-top: 20px; }}
  table {{ width: 100%; border-collapse: collapse; margin: 16px 0; }}
  th {{ background: #f0f0f0; padding: 10px 12px; text-align: left; font-size: 13px; }}
  td {{ padding: 10px 12px; border-bottom: 1px solid #eee; font-size: 14px; }}
  .positive {{ color: #16a34a; font-weight: 600; }}
  .negative {{ color: #dc2626; font-weight: 600; }}
  .neutral  {{ color: #6b7280; }}
  .card {{ background: #f9f9f9; border: 1px solid #e5e5e5; border-radius: 8px;
            padding: 16px; margin: 12px 0; }}
  .debate {{ background: #fff8f0; border: 1px solid #fed7aa; border-radius: 8px;
             padding: 16px; margin: 12px 0; font-size: 14px; line-height: 1.6; }}
  .regard-box {{ background: #fef3c7; border: 1px solid #fcd34d; border-radius: 8px;
                 padding: 16px; margin: 12px 0; }}
  .tag {{ display: inline-block; padding: 2px 8px; border-radius: 4px;
          font-size: 12px; font-weight: 600; }}
  .buy {{ background: #dcfce7; color: #16a34a; }}
  .sell {{ background: #fee2e2; color: #dc2626; }}
  .hold {{ background: #f3f4f6; color: #6b7280; }}
  footer {{ margin-top: 40px; padding-top: 16px; border-top: 1px solid #eee;
            font-size: 12px; color: #9ca3af; }}
</style>
</head>
<body>

<h1>📈 Trading Desk Weekly</h1>
<p style="color:#6b7280; font-size:14px;">Week ending {date_str}</p>

<!-- LEADERBOARD -->
<h2>🏆 Leaderboard</h2>
<table>
  <tr>
    <th>Rank</th><th>Trader</th><th>Pot (credits)</th>
    <th>Unrealised P&L</th><th>Total Value</th><th>Return</th><th>Positions</th>
  </tr>
"""
        for i, (trader_id, data) in enumerate(leaderboard):
            ret = data["return_pct"]
            ret_class = "positive" if ret >= 0 else "negative"
            unreal = data["unrealised_pnl"]
            unreal_class = "positive" if unreal >= 0 else "negative"
            html += f"""  <tr>
    <td>{MEDAL[i]}</td>
    <td><strong>{TRADER_DISPLAY.get(trader_id, trader_id)}</strong></td>
    <td>{data['credits']:.2f}</td>
    <td class="{unreal_class}">{unreal:+.2f}</td>
    <td><strong>{data['total_value']:.2f}</strong></td>
    <td class="{ret_class}">{ret:+.1f}%</td>
    <td>{data['open_positions']}</td>
  </tr>
"""
        html += "</table>\n"

        # ANALYST REPORTS
        html += "<h2>📋 Analyst Reports This Week</h2>\n"
        if reports:
            for r in reports:
                rating_class = r.analyst_rating.lower() if r.analyst_rating else "hold"
                html += f"""<div class="card">
  <strong>{r.ticker}</strong> — {r.company_name or ''} &nbsp;
  <span class="tag {rating_class}">{r.analyst_rating}</span>
  <span class="neutral" style="font-size:13px;"> · Confidence: {r.confidence} · Target: ${r.price_target}</span>
  <p style="margin:8px 0 0 0; font-size:14px;">{r.summary or ''}</p>
  <p style="font-size:12px; color:#9ca3af; margin:6px 0 0 0;">
    Trigger: {r.trigger_type} · {r.report_date.strftime('%a %d %b')}
  </p>
</div>
"""
        else:
            html += "<p class='neutral'>No analyst reports published this week.</p>\n"

        # TRADE ACTIVITY
        html += "<h2>⚡ Trade Activity</h2>\n"
        if trades:
            # Group by trader
            by_trader = {}
            for t in trades:
                by_trader.setdefault(t.trader_id, []).append(t)

            for trader_id, trader_trades in by_trader.items():
                html += f"<h3>{TRADER_DISPLAY.get(trader_id, trader_id)}</h3>\n<ul>\n"
                for t in trader_trades:
                    pnl_str = f" · P&L: <span class='{'positive' if (t.pnl or 0) >= 0 else 'negative'}'>{t.pnl:+.2f}</span>" if t.pnl is not None else ""
                    html += (
                        f"<li><strong>{t.action}</strong> {t.ticker} "
                        f"@ ${t.price_at_trade}{pnl_str}<br>"
                        f"<span style='font-size:13px;color:#555;'>{(t.reasoning or '')[:150]}{'...' if len(t.reasoning or '') > 150 else ''}</span></li>\n"
                    )
                html += "</ul>\n"
        else:
            html += "<p class='neutral'>No trades this week.</p>\n"

        # DEBATE OF THE WEEK
        if debates:
            debate = debates[0]  # Most recent
            transcript = humanize_transcript(debate.debate_transcript) or ''
            resolution = humanize_transcript(debate.resolution) or 'See transcript.'
            html += f"""<h2>🥊 Debate of the Week</h2>
<div class="debate">
  <strong>{debate.ticker}</strong> —
  {' vs '.join(TRADER_DISPLAY.get(t, t) for t in (debate.conflicting_traders or []))}
  <pre style="white-space:pre-wrap;font-family:inherit;font-size:13px;margin:12px 0;">
{transcript[:1500]}{'...' if len(transcript) > 1500 else ''}
  </pre>
  <strong>Resolution:</strong> {resolution}
</div>
"""

        # REGARD WATCH
        html += f"""<h2>🚀 Regard Watch</h2>
<div class="regard-box">
  <strong>Current pot:</strong> {regard_data.get('credits', 1000):.2f} credits &nbsp;
  <strong>Total value:</strong> {regard_data.get('total_value', 1000):.2f} &nbsp;
  <strong>Return:</strong> {regard_data.get('return_pct', 0):+.1f}%<br>
  <strong>Lifetime resets:</strong> {regard_data.get('lifetime_resets', 0)} 💀<br>
  <strong>Open positions:</strong> {regard_data.get('open_positions', 0)}
</div>
"""

        # EARLY SIGNALS
        html += self._build_early_signals()

        html += """<footer>
  Generated by the Trading Desk · Multi-agent simulation · Credits are not real money
</footer>
</body>
</html>"""

        return html

    def _get_week_reports(self, since: datetime) -> list:
        return self.session.execute(
            select(Report).where(Report.report_date >= since)
            .order_by(Report.report_date.desc())
        ).scalars().all()

    def _get_week_trades(self, since: datetime) -> list:
        return self.session.execute(
            select(Trade).where(
                Trade.traded_at >= since,
                Trade.action != "HOLD",
            ).order_by(Trade.traded_at.desc())
        ).scalars().all()

    def _get_week_debates(self, since: datetime) -> list:
        return self.session.execute(
            select(Debate).where(Debate.debated_at >= since)
            .order_by(Debate.debated_at.desc())
        ).scalars().all()

    def _build_early_signals(self) -> str:
        """Upcoming earnings, WSB trends, insider filings."""
        try:
            from analyst.data_fetcher import get_earnings_calendar, CORE_TICKERS
            upcoming = get_earnings_calendar(list(CORE_TICKERS.keys()), days_ahead=7)
        except Exception:
            upcoming = []

        html = "<h2>🔭 Early Signals (Next 7 Days)</h2>\n"
        if upcoming:
            html += "<ul>\n"
            for e in upcoming:
                html += f"<li><strong>{e['ticker']}</strong> — earnings in {e['days_until']} day(s)</li>\n"
            html += "</ul>\n"
        else:
            html += "<p class='neutral'>No earnings in the next 7 days for watched tickers.</p>\n"

        return html
