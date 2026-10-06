"""
Debate system — detects conflicting positions and runs two-round debates.
"""
import os
import json
import logging
import re
from datetime import datetime, timezone, timedelta

import anthropic
from sqlalchemy.orm import Session
from sqlalchemy import select

from db.models import Trade, Debate, Report

logger = logging.getLogger(__name__)

DEBATE_WINDOW_HOURS = 24  # Trades within this window can trigger a debate

# Display names for transcripts (trader_id stays the DB key everywhere else).
TRADER_NAMES = {
    "momentum": "The Momentum Trader",
    "insider": "The Insider Tracker",
    "short": "The Short Seller",
    "ape": "The WSB Ape",
    "boomer": "The Boomer",
    "hugger": "The Index Hugger",
}


def _trader_name(trader_id: str) -> str:
    return TRADER_NAMES.get(trader_id, trader_id)


# Display-time cleanup for Debate rows created before this module mapped ids to
# names — those transcripts baked raw trader ids into the round labels.
_ROUND_LABEL_RE = re.compile(r"(ROUND [12] - )(" + "|".join(TRADER_NAMES) + r")\b")
_NO_REASONING_RE = re.compile(
    r"(\[No reasoning recorded for )(" + "|".join(TRADER_NAMES) + r")(\])"
)


def humanize_transcript(text: str | None) -> str | None:
    """Swap raw trader_ids for display names in stored debate text.

    Structured "name slots" (round headers, the no-reasoning marker) are safe to
    rewrite for every trader. Free prose is left alone: `short`/`momentum`
    collide with ordinary finance words.
    """
    if not text:
        return text
    text = _ROUND_LABEL_RE.sub(lambda m: m.group(1) + TRADER_NAMES[m.group(2)], text)
    text = _NO_REASONING_RE.sub(
        lambda m: m.group(1) + TRADER_NAMES[m.group(2)] + m.group(3), text
    )
    return text

MEDIATOR_PROMPT = """You are moderating a structured debate between two traders who have taken \
opposing positions on the same stock after reading the same analyst report.

Your role:
- Present each trader's argument clearly and fairly
- After both rounds, summarise the key points of disagreement
- Note whether either trader revised their position size (not direction) after the debate
- Be concise — this is a briefing document, not a novel

Neither trader is required to change their mind. The debate is for the record."""


class DebateEngine:

    def __init__(self, session: Session):
        self.session = session
        self.client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    def check_for_conflicts(self, report: Report) -> list[tuple[str, str]]:
        """
        After all traders have reacted to a report, check for conflicting positions.
        Returns list of (trader_a, trader_b) pairs in conflict.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=DEBATE_WINDOW_HOURS)

        recent_trades = self.session.execute(
            select(Trade).where(
                Trade.report_id == report.id,
                Trade.traded_at >= cutoff,
                Trade.action.in_(["BUY", "SHORT"]),
            )
        ).scalars().all()

        # Group by direction on this ticker
        longs = [t for t in recent_trades if t.action == "BUY"]
        shorts = [t for t in recent_trades if t.action == "SHORT"]

        conflicts = []
        for long_trade in longs:
            for short_trade in shorts:
                conflicts.append((long_trade.trader_id, short_trade.trader_id))

        return conflicts

    def run_debate(
        self,
        trader_a: str,
        trader_b: str,
        report: Report,
    ) -> Debate:
        """
        Run a two-round debate between two conflicting traders.
        Stores transcript in DB.
        """
        logger.info(f"Debate: {trader_a} vs {trader_b} on {report.ticker}")

        # Get their trade reasoning
        reasoning_a = self._get_trade_reasoning(trader_a, report)
        reasoning_b = self._get_trade_reasoning(trader_b, report)

        # Build debate transcript
        transcript = self._conduct_debate(
            trader_a, trader_b, report, reasoning_a, reasoning_b
        )

        # Store in DB
        debate = Debate(
            ticker=report.ticker,
            report_id=report.id,
            conflicting_traders=[trader_a, trader_b],
            debate_transcript=transcript,
            resolution=self._extract_resolution(transcript),
            debated_at=datetime.now(timezone.utc),
        )
        self.session.add(debate)
        self.session.commit()

        return debate

    def _conduct_debate(
        self,
        trader_a: str,
        trader_b: str,
        report: Report,
        reasoning_a: str,
        reasoning_b: str,
    ) -> str:
        """Two-round debate via Claude."""

        name_a = _trader_name(trader_a)
        name_b = _trader_name(trader_b)

        context = f"""## Debate Context
Ticker: {report.ticker}
Analyst rating: {report.analyst_rating} (Confidence: {report.confidence})
Price at report: ${report.price_at_report}
Price target: ${report.price_target}

{name_a} (LONG) initial reasoning:
{reasoning_a}

{name_b} (SHORT) initial reasoning:
{reasoning_b}"""

        # Round 1: Both state positions (already have from reasoning)
        # Round 2: Each responds to the other's strongest argument

        round2_prompt = f"""{context}

## Round 2 — Response

{name_a}, respond to {name_b}'s strongest argument against being long.
{name_b}, respond to {name_a}'s strongest argument for being long.

Each response: 100 words max, in character, stay true to your trading philosophy.
Format:
ROUND 2 - {name_a}:
[response]

ROUND 2 - {name_b}:
[response]

RESOLUTION:
[2-3 sentence summary of the core disagreement and whether either trader revised their sizing]"""

        try:
            response = self.client.messages.create(
                model="claude-sonnet-5",
                max_tokens=800,
                system=MEDIATOR_PROMPT,
                messages=[{"role": "user", "content": round2_prompt}],
            )
            round2_text = response.content[0].text.strip()
        except Exception as e:
            logger.error(f"Debate API error: {e}")
            round2_text = f"[Debate round 2 failed: {e}]"

        transcript = f"""ROUND 1 - {name_a} (LONG):
{reasoning_a}

ROUND 1 - {name_b} (SHORT):
{reasoning_b}

{round2_text}"""

        return transcript

    def _get_trade_reasoning(self, trader_id: str, report: Report) -> str:
        """Fetch the trader's reasoning from their most recent trade on this report."""
        trade = self.session.execute(
            select(Trade).where(
                Trade.trader_id == trader_id,
                Trade.report_id == report.id,
            ).order_by(Trade.traded_at.desc())
        ).scalar_one_or_none()

        if trade and trade.reasoning:
            return trade.reasoning
        return f"[No reasoning recorded for {trader_id}]"

    def _extract_resolution(self, transcript: str) -> str:
        """Extract the RESOLUTION section from the debate transcript."""
        if "RESOLUTION:" in transcript:
            return transcript.split("RESOLUTION:")[-1].strip()
        return "See full transcript."
