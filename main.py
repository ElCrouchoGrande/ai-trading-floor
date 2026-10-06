"""
Main entry point for Railway.
FastAPI app + APScheduler orchestrating all jobs.
"""
import os
import logging
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from db.models import get_engine
from db.session import get_session_factory, get_session, initialise_ledgers
from analyst.agent import AnalystAgent, TriggerDetector
from traders.all_traders import (
    MomentumTrader, InsiderTracker, ShortSeller,
    WSBApe, TheBoomer, IndexHugger
)
from engine.pnl import PnLEngine
from engine.debate import DebateEngine
from reporting.weekly_report import WeeklyReporter
from reporting.dashboard import DASHBOARD_HTML, get_cached_state, get_reports_archive, get_trades_archive

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "changeme")
# A blank or default token is treated as "no token configured": rather than
# silently accepting "changeme", the admin endpoints are disabled outright so a
# publicly-reachable deploy can't be abused (e.g. /inject burning Claude spend).
ADMIN_TOKEN_INSECURE = ADMIN_TOKEN in ("", "changeme")
# Hard cap on tickers analysed per scheduled trigger run, so a WSB spike day
# can't fire the full pipeline on 20 tickers at once.
MAX_TICKERS_PER_RUN = int(os.environ.get("MAX_TICKERS_PER_RUN", "5"))

engine = get_engine(DATABASE_URL)
SessionFactory, _ = get_session_factory(DATABASE_URL)

scheduler = AsyncIOScheduler(timezone="UTC")

TRADERS = [MomentumTrader, InsiderTracker, ShortSeller, WSBApe, TheBoomer, IndexHugger]


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def run_analyst_triggers():
    """Check triggers and run analyst where needed.

    Earnings tickers come first (time-sensitive); WSB fills the remainder of
    the per-run cap. The analyst's own cooldown then drops any ticker that
    already has a fresh report, so a stock that stays hot for a day isn't
    re-pipelined every cron tick.
    """
    with get_session(SessionFactory) as session:
        detector = TriggerDetector(session)
        analyst = AnalystAgent(session)
        debate_engine = DebateEngine(session)

        queue: list[tuple[str, str]] = []
        seen: set[str] = set()
        for ticker in detector.check_earnings_triggers():
            if ticker not in seen:
                queue.append((ticker, "earnings"))
                seen.add(ticker)
        for ticker, reason in detector.check_wsb_triggers():
            if ticker not in seen:
                queue.append((ticker, reason))
                seen.add(ticker)

        if len(queue) > MAX_TICKERS_PER_RUN:
            logger.info(
                f"Trigger cap: {len(queue)} candidates, processing first "
                f"{MAX_TICKERS_PER_RUN} (earnings first)"
            )
            queue = queue[:MAX_TICKERS_PER_RUN]

        for ticker, trigger_type in queue:
            report = analyst.run_for_ticker(ticker, trigger_type=trigger_type)
            if report:
                _run_all_traders(session, report, debate_engine)


def run_weekly_digest():
    """Monday morning: analyst covers all tickers with significant movement."""
    with get_session(SessionFactory) as session:
        analyst = AnalystAgent(session)
        debate_engine = DebateEngine(session)
        reports = analyst.run_weekly_digest()
        for report in reports:
            _run_all_traders(session, report, debate_engine)


def run_daily_pnl():
    """Daily P&L mark-to-market and exit checks."""
    with get_session(SessionFactory) as session:
        engine = PnLEngine(session)
        results = engine.run_daily_job()
        logger.info(f"Daily P&L job: {results}")


def _run_all_traders(session: Session, report, debate_engine: DebateEngine):
    """Run all traders on a report, then check for debates."""
    for TraderClass in TRADERS:
        try:
            trader = TraderClass(session)
            trader.react_to_report(report)
        except Exception as e:
            logger.error(f"{TraderClass.trader_id} error on {report.ticker}: {e}")

    # Check for conflicts and run debates
    conflicts = debate_engine.check_for_conflicts(report)
    for trader_a, trader_b in conflicts:
        try:
            debate_engine.run_debate(trader_a, trader_b, report)
        except Exception as e:
            logger.error(f"Debate error {trader_a} vs {trader_b}: {e}")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Schema is managed by Alembic (`alembic upgrade head` runs before uvicorn
    # via railway.toml startCommand). Here we only seed ledger rows.
    with get_session(SessionFactory) as session:
        initialise_ledgers(session)
    logger.info("Ledgers seeded")

    if ADMIN_TOKEN_INSECURE:
        logger.warning(
            "ADMIN_TOKEN is unset or still 'changeme' — admin endpoints "
            "(/inject, /portfolio, /report/latest, /weekly/*) are DISABLED. "
            "Set a strong ADMIN_TOKEN to enable them."
        )

    # Schedule jobs
    # Trigger check: every hour 07:00-22:00 UTC weekdays
    scheduler.add_job(
        run_analyst_triggers,
        # Every 3 hours during the trading window (07, 10, 13, 16, 19, 22 UTC)
        # rather than hourly — the underlying signals (WSB rank, earnings dates)
        # don't change fast enough to justify 16 runs/day.
        CronTrigger(day_of_week="mon-fri", hour="7-22/3", minute=0),
        id="analyst_triggers",
        replace_existing=True,
    )

    # Weekly digest: Monday 08:00 UTC
    scheduler.add_job(
        run_weekly_digest,
        CronTrigger(day_of_week="mon", hour=8, minute=0),
        id="weekly_digest",
        replace_existing=True,
    )

    # Daily P&L: 21:00 UTC (after US close)
    scheduler.add_job(
        run_daily_pnl,
        CronTrigger(day_of_week="mon-fri", hour=21, minute=0),
        id="daily_pnl",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("Scheduler started")

    yield

    scheduler.shutdown()


app = FastAPI(title="Trading Desk", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Public dashboard (read-only, no token)
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=DASHBOARD_HTML)


@app.get("/api/state")
async def api_state():
    return get_cached_state(SessionFactory)


@app.get("/api/reports")
async def api_reports():
    """Fuller DD history for the dashboard archive view."""
    return get_reports_archive(SessionFactory)


@app.get("/api/trades")
async def api_trades():
    """Fuller trade history for the dashboard activity view."""
    return get_trades_archive(SessionFactory)


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

def verify_token(x_admin_token: str = Header(...)):
    if ADMIN_TOKEN_INSECURE:
        raise HTTPException(
            status_code=503,
            detail="Admin endpoints are disabled: set a strong ADMIN_TOKEN.",
        )
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid admin token")


class InjectRequest(BaseModel):
    ticker: str
    trigger_type: str = "manual"
    news_context: str = ""


@app.post("/inject", dependencies=[Depends(verify_token)])
async def inject_ticker(req: InjectRequest):
    """Manually push a ticker into the analyst queue."""
    with get_session(SessionFactory) as session:
        analyst = AnalystAgent(session)
        debate_engine = DebateEngine(session)
        report = analyst.run_for_ticker(
            req.ticker,
            trigger_type=req.trigger_type,
            news_context=req.news_context or None,
            force=True,  # manual injects always run, bypassing the cooldown
        )
        if not report:
            raise HTTPException(status_code=422, detail=f"Analyst returned no report for {req.ticker}")
        _run_all_traders(session, report, debate_engine)
        # Capture before the session commits/closes — expire_on_commit would
        # otherwise make report.id raise DetachedInstanceError out here.
        report_id = report.id
    return {"status": "ok", "report_id": report_id, "ticker": req.ticker}


@app.get("/portfolio")
async def portfolio(x_admin_token: str = Header(...)):
    verify_token(x_admin_token)
    with get_session(SessionFactory) as session:
        engine = PnLEngine(session)
        return engine.get_portfolio_summary()


@app.get("/report/latest/{ticker}")
async def latest_report(ticker: str, x_admin_token: str = Header(...)):
    verify_token(x_admin_token)
    from db.models import Report as ReportModel
    from sqlalchemy import select
    with get_session(SessionFactory) as session:
        report = session.execute(
            select(ReportModel)
            .where(ReportModel.ticker == ticker.upper())
            .order_by(ReportModel.report_date.desc())
        ).scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="No report found")
        return {
            "ticker": report.ticker,
            "date": report.report_date.isoformat(),
            "rating": report.analyst_rating,
            "confidence": report.confidence,
            "price_target": float(report.price_target or 0),
            "summary": report.summary,
            "bull_case": report.bull_case,
            "bear_case": report.bear_case,
        }


@app.get("/weekly/preview", response_class=HTMLResponse)
async def weekly_preview(x_admin_token: str = Header(...)):
    """Preview the weekly report HTML without sending it."""
    verify_token(x_admin_token)
    with get_session(SessionFactory) as session:
        reporter = WeeklyReporter(session)
        now = datetime.now(timezone.utc)
        html = reporter._build_html(
            week_start=now.replace(hour=0, minute=0, second=0),
            week_end=now,
        )
    return HTMLResponse(content=html)


@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}
