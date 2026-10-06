"""
Base trader class. All six traders inherit from this.
Handles Claude API calls, position management, and trade logging.
"""
import os
import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

import anthropic
from sqlalchemy.orm import Session
from sqlalchemy import select

from db.models import Trade, Position, Ledger, Report
from db.session import get_ledger, adjust_credits

logger = logging.getLogger(__name__)

MAX_POSITION_PCT = 0.30   # 30% of pot max per position (short seller: 0.40)
REGARD_MAX_PCT = 1.00     # regard can yolo entire pot


class BaseTrader(ABC):

    trader_id: str
    display_name: str
    max_position_pct: float = MAX_POSITION_PCT

    def __init__(self, session: Session):
        self.session = session
        self.client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    @property
    @abstractmethod
    def system_prompt(self) -> str:
        """Each trader defines their own ethos."""
        ...

    def get_current_pot(self) -> float:
        ledger = get_ledger(self.session, self.trader_id)
        return float(ledger.credits)

    def get_open_positions(self) -> list[dict]:
        positions = self.session.execute(
            select(Position).where(Position.trader_id == self.trader_id)
        ).scalars().all()
        return [
            {
                "ticker": p.ticker,
                "direction": p.direction,
                "credits_risked": float(p.credits_risked),
                "entry_price": float(p.entry_price),
                "entry_date": p.entry_date.isoformat(),
            }
            for p in positions
        ]

    def react_to_report(self, report: Report) -> Optional[dict]:
        """
        Main entry point. Given an analyst report, produce a trade decision.
        Also re-evaluates any open positions in the same ticker.
        """
        pot = self.get_current_pot()
        open_positions = self.get_open_positions()

        # Check if we already hold this ticker
        existing = next(
            (p for p in open_positions if p["ticker"] == report.ticker), None
        )

        prompt = self._build_decision_prompt(report, pot, open_positions, existing)

        try:
            response = self.client.messages.create(
                model="claude-sonnet-5",
                max_tokens=800,
                system=self.system_prompt,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()
            decision = json.loads(raw)
        except Exception as e:
            logger.error(f"{self.trader_id} decision error for {report.ticker}: {e}")
            return None

        # Validate and execute
        return self._execute_decision(decision, report, pot)

    def _execute_decision(self, decision: dict, report: Report, pot: float) -> Optional[dict]:
        action = decision.get("action", "HOLD").upper()
        ticker = report.ticker

        if action == "HOLD":
            self._log_trade(ticker, "HOLD", 0, report, decision.get("reasoning", ""))
            return decision

        if action == "CLOSE":
            return self._close_position(ticker, report, decision.get("reasoning", ""))

        # Size validation
        credits_risked = float(decision.get("credits_risked", 0))
        max_allowed = pot * self.max_position_pct
        credits_risked = min(credits_risked, max_allowed)

        if credits_risked <= 0:
            return None

        if action in ("BUY", "SHORT"):
            return self._open_position(ticker, action, credits_risked, report, decision)

        return None

    def _open_position(
        self, ticker: str, action: str, credits_risked: float,
        report: Report, decision: dict
    ) -> dict:
        direction = "LONG" if action == "BUY" else "SHORT"
        price = float(report.price_at_report)
        reasoning = decision.get("reasoning", "")

        # Deduct credits
        adjust_credits(self.session, self.trader_id, -credits_risked)

        # Log trade
        trade = self._log_trade(ticker, action, credits_risked, report, reasoning)

        # Open position
        # Close any existing position in this ticker first
        self._remove_position(ticker)
        position = Position(
            trader_id=self.trader_id,
            ticker=ticker,
            direction=direction,
            credits_risked=credits_risked,
            entry_price=price,
            trade_id=trade.id,
        )
        self.session.add(position)
        self.session.commit()

        logger.info(f"{self.trader_id}: {action} {ticker} @ {price} ({credits_risked} credits)")
        return {**decision, "executed": True, "credits_risked": credits_risked}

    def _close_position(self, ticker: str, report: Report, reasoning: str) -> Optional[dict]:
        position = self.session.execute(
            select(Position).where(
                Position.trader_id == self.trader_id,
                Position.ticker == ticker,
            )
        ).scalar_one_or_none()

        if not position:
            return None

        close_price = float(report.price_at_report)
        entry_price = float(position.entry_price)
        credits_risked = float(position.credits_risked)

        if position.direction == "LONG":
            pnl = (close_price - entry_price) / entry_price * credits_risked
        else:  # SHORT
            pnl = (entry_price - close_price) / entry_price * credits_risked

        # Return credits + PnL
        adjust_credits(self.session, self.trader_id, credits_risked + pnl)

        # Update trade record
        trade = self.session.execute(
            select(Trade).where(Trade.id == position.trade_id)
        ).scalar_one_or_none()
        if trade:
            trade.closed_at = datetime.now(timezone.utc)
            trade.close_price = close_price
            trade.pnl = pnl

        self._remove_position(ticker)
        self.session.commit()

        logger.info(
            f"{self.trader_id}: CLOSE {ticker} @ {close_price} "
            f"PnL: {pnl:+.2f} credits"
        )
        return {"action": "CLOSE", "ticker": ticker, "pnl": pnl, "reasoning": reasoning}

    def _remove_position(self, ticker: str):
        position = self.session.execute(
            select(Position).where(
                Position.trader_id == self.trader_id,
                Position.ticker == ticker,
            )
        ).scalar_one_or_none()
        if position:
            self.session.delete(position)

    def _log_trade(
        self, ticker: str, action: str, credits_risked: float,
        report: Report, reasoning: str
    ) -> Trade:
        trade = Trade(
            trader_id=self.trader_id,
            ticker=ticker,
            action=action,
            credits_risked=credits_risked,
            price_at_trade=report.price_at_report,
            reasoning=reasoning,
            report_id=report.id,
        )
        self.session.add(trade)
        self.session.commit()
        return trade

    def _build_decision_prompt(
        self,
        report: Report,
        pot: float,
        open_positions: list[dict],
        existing_position: Optional[dict],
    ) -> str:
        key_risks = []
        try:
            key_risks = json.loads(report.key_risks or "[]")
        except Exception:
            pass

        parts = [
            f"## Analyst Report: {report.ticker}",
            f"Rating: {report.analyst_rating} | Confidence: {report.confidence}",
            f"Price at report: ${report.price_at_report} | Target: ${report.price_target}",
            "",
            f"Bull case: {report.bull_case}",
            f"Bear case: {report.bear_case}",
            f"Key risks: {', '.join(key_risks)}",
            f"Summary: {report.summary}",
            "",
            f"## Your current state",
            f"Available credits: {pot:.2f}",
            f"Max position size: {pot * self.max_position_pct:.2f} credits",
            f"Open positions: {json.dumps(open_positions, indent=2) if open_positions else 'None'}",
        ]

        if existing_position:
            parts += [
                "",
                f"## Note: You already hold {report.ticker}",
                f"Position: {json.dumps(existing_position, indent=2)}",
                "You may HOLD (keep), CLOSE (exit), or adjust. You cannot add to an existing position.",
            ]

        parts += [
            "",
            "## Decision required",
            "Respond ONLY with JSON. No preamble. Schema:",
            '{',
            '  "action": "BUY|SELL|SHORT|COVER|CLOSE|HOLD",',
            '  "credits_risked": number,  // 0 if HOLD or CLOSE',
            '  "reasoning": "string (2-3 sentences in your voice)",',
            '  "hold_duration_estimate": "string e.g. 2-4 weeks"',
            '}',
        ]

        return "\n".join(parts)
