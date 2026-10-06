"""
Analyst agent — detects triggers and produces structured DD reports.
Uses Claude Sonnet 4 for report generation.
"""
import os
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import anthropic
from sqlalchemy import select
from sqlalchemy.orm import Session

from analyst.data_fetcher import (
    get_price_data, get_fundamentals, get_earnings_calendar,
    get_insider_filings, CORE_TICKERS
)
from wsb.scraper import get_wsb_sentiment, get_wsb_ticker_detail
from db.models import Report, WatchedTicker

logger = logging.getLogger(__name__)

# Skip re-running the full pipeline on a ticker that already has a fresh
# report — keeps a hot WSB stock from being re-analysed every hour.
ANALYST_COOLDOWN_HOURS = int(os.environ.get("ANALYST_COOLDOWN_HOURS", "12"))

ANALYST_SYSTEM_PROMPT = """You are a rigorous equity analyst. You produce structured due diligence \
reports on publicly traded companies. You have access to real price data, fundamentals, \
news coverage, and market sentiment signals.

Your job is to synthesise available data into a clear investment thesis. \
You are not a cheerleader. You identify risks as readily as opportunities. \
You rate stocks BUY, SELL, or HOLD with a 90-day price target.

You never fabricate data. If a data source is unavailable, note the gap \
explicitly rather than inferring. Your confidence rating (HIGH/MEDIUM/LOW) \
reflects data quality as much as thesis strength. LOW confidence is appropriate \
when key data is missing — do not upgrade confidence to compensate.

Respond ONLY with a JSON object. No preamble, no markdown fences. Schema:
{
  "ticker": "string",
  "company_name": "string",
  "analyst_rating": "BUY|SELL|HOLD",
  "price_target": number,
  "price_target_horizon": "90 days",
  "confidence": "HIGH|MEDIUM|LOW",
  "bull_case": "string (2-3 sentences)",
  "bear_case": "string (2-3 sentences)",
  "key_risks": ["string", "string", "string"],
  "summary": "string (200 words max, plain English, suitable for non-specialists)"
}"""


class AnalystAgent:

    def __init__(self, session: Session):
        self.session = session
        self.client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    def run_for_ticker(
        self,
        ticker: str,
        trigger_type: str,
        news_context: Optional[str] = None,
        force: bool = False,
    ) -> Optional[Report]:
        """
        Full analyst pipeline for one ticker.
        Fetches data, calls Claude, writes report to DB.

        Unless `force=True`, skips if a report on this ticker exists within the
        last ANALYST_COOLDOWN_HOURS (default 12h). Manual /inject passes
        force=True; the hourly trigger does not.
        """
        if not force and ANALYST_COOLDOWN_HOURS > 0:
            recent = self.session.execute(
                select(Report)
                .where(Report.ticker == ticker.upper())
                .order_by(Report.report_date.desc())
                .limit(1)
            ).scalar_one_or_none()
            if recent and recent.report_date:
                last = recent.report_date
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - last < timedelta(hours=ANALYST_COOLDOWN_HOURS):
                    logger.info(
                        f"Skipping {ticker}: report within last "
                        f"{ANALYST_COOLDOWN_HOURS}h ({trigger_type})"
                    )
                    return None

        logger.info(f"Analyst running for {ticker} (trigger: {trigger_type})")

        # 1. Gather data
        price_data = get_price_data(ticker)
        fundamentals = get_fundamentals(ticker)
        insider_filings = get_insider_filings(ticker, days_back=30)
        wsb_context = get_wsb_ticker_detail(ticker)

        if "error" in price_data:
            logger.warning(f"No price data for {ticker}, skipping")
            return None

        current_price = price_data["current_price"]

        # 2. Build prompt
        prompt = self._build_prompt(
            ticker=ticker,
            price_data=price_data,
            fundamentals=fundamentals,
            insider_filings=insider_filings,
            wsb_context=wsb_context,
            news_context=news_context,
            trigger_type=trigger_type,
        )

        # 3. Call Claude Sonnet 4
        try:
            response = self.client.messages.create(
                model="claude-sonnet-5",
                max_tokens=1500,
                system=ANALYST_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            raw_text = response.content[0].text.strip()
            report_data = json.loads(raw_text)
        except json.JSONDecodeError as e:
            logger.error(f"Analyst JSON parse error for {ticker}: {e}\nRaw: {raw_text}")
            return None
        except Exception as e:
            logger.error(f"Claude API error for {ticker}: {e}")
            return None

        # 4. Write to DB
        report = Report(
            ticker=ticker,
            company_name=report_data.get("company_name", fundamentals.get("company_name")),
            report_date=datetime.now(timezone.utc),
            trigger_type=trigger_type,
            bull_case=report_data.get("bull_case"),
            bear_case=report_data.get("bear_case"),
            key_risks=json.dumps(report_data.get("key_risks", [])),
            price_at_report=current_price,
            analyst_rating=report_data.get("analyst_rating"),
            price_target=report_data.get("price_target"),
            price_target_horizon=report_data.get("price_target_horizon", "90 days"),
            confidence=report_data.get("confidence"),
            summary=report_data.get("summary"),
            raw_data={
                "price_data": price_data,
                "fundamentals": {k: v for k, v in fundamentals.items()
                                 if k not in ("error",)},
                "insider_filings_count": len(insider_filings),
                "wsb_mentions": wsb_context.get("mentions", 0) if wsb_context else 0,
                "wsb_rank": wsb_context.get("rank") if wsb_context else None,
                "wsb_rising": wsb_context.get("rising", False) if wsb_context else False,
                "news_context_length": len(news_context) if news_context else 0,
            },
        )
        self.session.add(report)
        self.session.commit()
        logger.info(
            f"Report written for {ticker}: {report_data['analyst_rating']} "
            f"@ {report_data.get('price_target')} ({report_data['confidence']})"
        )
        return report

    def run_weekly_digest(self) -> list[Report]:
        """
        Weekly pass: run analyst on all watched tickers with >5% price movement.
        """
        reports = []
        tickers = list(CORE_TICKERS.keys())

        for ticker in tickers:
            price_data = get_price_data(ticker)
            if "error" in price_data:
                continue
            week_change = price_data.get("week_change_pct")
            if week_change is not None and abs(week_change) >= 5.0:
                report = self.run_for_ticker(ticker, trigger_type="weekly")
                if report:
                    reports.append(report)

        return reports

    def _build_prompt(
        self,
        ticker: str,
        price_data: dict,
        fundamentals: dict,
        insider_filings: list,
        wsb_context: Optional[dict],
        news_context: Optional[str],
        trigger_type: str,
    ) -> str:
        sections = [
            f"## Analyst Report Request: {ticker}",
            f"Trigger: {trigger_type}",
            f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
            "",
            "### Price Data",
            json.dumps(price_data, indent=2),
            "",
            "### Fundamentals",
            json.dumps(fundamentals, indent=2),
        ]

        if insider_filings:
            sections += [
                "",
                f"### Insider Filings (last 30 days) — {len(insider_filings)} purchase(s)",
                json.dumps(insider_filings[:5], indent=2),
            ]
        else:
            sections += ["", "### Insider Filings", "No insider purchases in last 30 days."]

        if wsb_context and wsb_context.get("wsb_active"):
            rank_str = f"rank #{wsb_context['rank']}" if wsb_context.get("rank") else "unranked"
            change = wsb_context.get("rank_change", 0)
            change_str = f"↑{change} places in 24h" if change > 0 else (f"↓{abs(change)} places" if change < 0 else "stable")
            sections += [
                "",
                "### WSB Activity (ApeWisdom)",
                f"Mentions (24h): {wsb_context['mentions']} | Upvotes: {wsb_context['upvotes']}",
                f"Ranking: {rank_str} ({change_str})",
                f"Sentiment: {wsb_context['sentiment']} | Rising fast: {wsb_context['rising']}",
            ]

        if news_context:
            sections += ["", "### News Context", news_context[:2000]]

        sections += [
            "",
            "Produce your structured DD report now.",
        ]

        return "\n".join(sections)


# ---------------------------------------------------------------------------
# Trigger detection
# ---------------------------------------------------------------------------

class TriggerDetector:

    def __init__(self, session: Session):
        self.session = session

    def check_earnings_triggers(self) -> list[str]:
        """Returns tickers with earnings in the next 2 days."""
        tickers = list(CORE_TICKERS.keys())
        upcoming = get_earnings_calendar(tickers, days_ahead=2)
        return [e["ticker"] for e in upcoming]

    def check_wsb_triggers(self) -> list[tuple[str, str]]:
        """
        Returns (ticker, trigger_reason) pairs for WSB-triggered tickers.
        Two signal types:
          - 'wsb_spike'  : raw mention volume above threshold
          - 'wsb_rising' : rank improved sharply in 24h (momentum signal)
        """
        sentiment = get_wsb_sentiment()
        triggered = {}

        for ticker in sentiment.get("spike_tickers", []):
            triggered[ticker] = "wsb_spike"

        # Rising tickers get their own trigger type — often more interesting
        # than raw volume (catches things before they fully blow up)
        for ticker in sentiment.get("rising_tickers", []):
            if ticker not in triggered:
                triggered[ticker] = "wsb_rising"

        return list(triggered.items())

    def check_price_movement_triggers(self, threshold_pct: float = 5.0) -> list[str]:
        """Returns tickers that moved >threshold% in the past week."""
        triggered = []
        for ticker in CORE_TICKERS:
            data = get_price_data(ticker, period="1mo")
            if "error" in data:
                continue
            if abs(data.get("week_change_pct") or 0) >= threshold_pct:
                triggered.append(ticker)
        return triggered
