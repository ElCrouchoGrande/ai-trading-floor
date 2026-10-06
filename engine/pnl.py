"""
P&L engine — daily job that marks positions to market, checks exit conditions,
and handles the regard's secret reset mechanic.
Runs at 21:00 UTC (after US market close).
"""
import logging
import math
from datetime import datetime, timezone, timedelta
from typing import Optional

import yfinance as yf
from sqlalchemy.orm import Session
from sqlalchemy import select

from db.models import Position, Trade, LedgerSnapshot, Ledger
from db.session import get_ledger, adjust_credits, STARTING_CREDITS, REGARD_RESET_THRESHOLD

logger = logging.getLogger(__name__)

# Exit rules per trader (days held, loss threshold, gain threshold)
EXIT_RULES = {
    "momentum": {
        "max_hold_days": 42,       # 6 weeks
        "stop_loss_pct": -0.15,    # close if down 15%
        "take_profit_pct": 0.30,   # close if up 30%
        "ma_cross_exit": True,     # close if price crosses below 20DMA
    },
    "insider": {
        "max_hold_days": 90,
        "stop_loss_pct": -0.20,
        "take_profit_pct": 0.40,
        "ma_cross_exit": False,
    },
    "short": {
        "max_hold_days": 60,
        "stop_loss_pct": -0.20,    # cover if short goes 20% against (stock rises 20%)
        "take_profit_pct": 0.40,   # cover if stock falls 40%
        "ma_cross_exit": False,
    },
    "regard": {
        "max_hold_days": 999,      # regards hold forever or go to zero
        "stop_loss_pct": -0.99,    # effectively no stop loss
        "take_profit_pct": 10.0,   # only exits on 10x (never happens)
        "ma_cross_exit": False,
    },
    "boomer": {
        "max_hold_days": 365,
        "stop_loss_pct": -0.25,    # boomers can stomach a lot
        "take_profit_pct": 1.0,    # rarely takes profit
        "ma_cross_exit": False,
    },
    "hugger": {
        "max_hold_days": 90,
        "stop_loss_pct": -0.12,    # tight stop — deviations should be high conviction
        "take_profit_pct": 0.25,
        "ma_cross_exit": False,
    },
}


class PnLEngine:

    def __init__(self, session: Session):
        self.session = session

    def run_daily_job(self) -> dict:
        """
        Main daily job:
        1. Fetch closing prices for all open positions
        2. Check exit conditions
        3. Close positions that should exit
        4. Check regard reset
        5. Take daily snapshots
        """
        logger.info("P&L daily job starting")
        results = {
            "positions_checked": 0,
            "positions_closed": [],
            "regard_reset": False,
            "snapshots_taken": 0,
        }

        # 1. Get all open positions
        positions = self.session.execute(select(Position)).scalars().all()
        results["positions_checked"] = len(positions)

        if not positions:
            logger.info("No open positions to check")
        else:
            # 2. Batch fetch prices
            tickers = list({p.ticker for p in positions})
            prices = self._fetch_closing_prices(tickers)

            # 3. Check exit conditions
            for position in positions:
                current_price = prices.get(position.ticker)
                if current_price is None:
                    logger.warning(f"No price for {position.ticker}, skipping exit check")
                    continue

                should_exit, reason = self._check_exit_conditions(position, current_price)
                if should_exit:
                    pnl = self._close_position(position, current_price, reason)
                    results["positions_closed"].append({
                        "trader_id": position.trader_id,
                        "ticker": position.ticker,
                        "reason": reason,
                        "pnl": pnl,
                    })

        # 4. Regard reset check (net-worth based)
        reset = self._check_regard_reset()
        if reset:
            results["regard_reset"] = True

        # 5. Daily snapshots
        snapshots = self._take_snapshots()
        results["snapshots_taken"] = snapshots

        logger.info(f"P&L job complete: {results}")
        return results

    def get_portfolio_summary(self) -> dict:
        """
        Current state of all trader pots + unrealised P&L.
        Used by weekly report.
        """
        summary = {}
        positions = self.session.execute(select(Position)).scalars().all()
        tickers = list({p.ticker for p in positions})
        prices = self._fetch_closing_prices(tickers) if tickers else {}

        ledgers = self.session.execute(select(Ledger)).scalars().all()

        for ledger in ledgers:
            trader_positions = [p for p in positions if p.trader_id == ledger.trader_id]
            committed = 0.0   # principal staked in open positions (already debited from cash)
            unrealised = 0.0  # mark-to-market P&L on that principal
            for pos in trader_positions:
                committed += float(pos.credits_risked)
                price = prices.get(pos.ticker)
                if price:
                    if pos.direction == "LONG":
                        unrealised += (price - float(pos.entry_price)) / float(pos.entry_price) * float(pos.credits_risked)
                    else:
                        unrealised += (float(pos.entry_price) - price) / float(pos.entry_price) * float(pos.credits_risked)

            cash = float(ledger.credits)
            total_value = cash + committed + unrealised
            summary[ledger.trader_id] = {
                "credits": cash,                       # free, uninvested cash
                "committed": round(committed, 2),      # principal locked in open positions
                "unrealised_pnl": round(unrealised, 2),
                "total_value": round(total_value, 2),  # net worth: cash + positions at market
                "return_pct": round((total_value - 1000) / 1000 * 100, 2),
                "open_positions": len(trader_positions),
                "lifetime_resets": ledger.lifetime_resets,  # regard only
            }

        return summary

    def _check_exit_conditions(self, position: Position, current_price: float) -> tuple[bool, str]:
        rules = EXIT_RULES.get(position.trader_id, EXIT_RULES["momentum"])
        entry = float(position.entry_price)
        days_held = (datetime.now(timezone.utc) - position.entry_date).days

        if position.direction == "LONG":
            change_pct = (current_price - entry) / entry
        else:  # SHORT
            change_pct = (entry - current_price) / entry

        if change_pct <= rules["stop_loss_pct"]:
            return True, f"Stop loss triggered ({change_pct:.1%})"

        if change_pct >= rules["take_profit_pct"]:
            return True, f"Take profit triggered ({change_pct:.1%})"

        if days_held >= rules["max_hold_days"]:
            return True, f"Max hold period reached ({days_held} days)"

        # MA cross exit for momentum trader
        if rules.get("ma_cross_exit") and position.direction == "LONG":
            ma20 = self._get_ma20(position.ticker)
            if ma20 and current_price < ma20:
                return True, f"Price crossed below 20DMA (price: {current_price:.2f}, MA20: {ma20:.2f})"

        return False, ""

    def _close_position(self, position: Position, close_price: float, reason: str) -> float:
        entry = float(position.entry_price)
        credits_risked = float(position.credits_risked)

        if position.direction == "LONG":
            pnl = (close_price - entry) / entry * credits_risked
        else:
            pnl = (entry - close_price) / entry * credits_risked

        # Return credits + PnL to ledger
        adjust_credits(self.session, position.trader_id, credits_risked + pnl)

        # Update trade record
        trade = self.session.execute(
            select(Trade).where(Trade.id == position.trade_id)
        ).scalar_one_or_none()
        if trade:
            trade.closed_at = datetime.now(timezone.utc)
            trade.close_price = close_price
            trade.pnl = pnl

        self.session.delete(position)
        self.session.commit()

        logger.info(
            f"Closed {position.trader_id} {position.direction} {position.ticker} "
            f"@ {close_price:.2f} | PnL: {pnl:+.2f} | Reason: {reason}"
        )
        return pnl

    def _fetch_closing_prices(self, tickers: list[str]) -> dict[str, float]:
        """Batch fetch today's closing prices via yfinance.

        Delisted tickers (e.g. acquired/renamed names sitting in old positions)
        come back as NaN — those are dropped so they can't leak into JSON, where
        NaN isn't valid and would 500 the dashboard.
        """
        prices = {}
        try:
            data = yf.download(tickers, period="1d", auto_adjust=True, progress=False)
            if "Close" in data.columns:
                for ticker in tickers:
                    try:
                        price = float(data["Close"][ticker].iloc[-1])
                        if math.isnan(price) or math.isinf(price):
                            continue
                        prices[ticker] = price
                    except Exception:
                        pass
            elif len(tickers) == 1:
                # Single ticker returns differently
                try:
                    price = float(data["Close"].iloc[-1])
                    if not (math.isnan(price) or math.isinf(price)):
                        prices[tickers[0]] = price
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Price fetch error: {e}")
        return prices

    def _get_ma20(self, ticker: str) -> Optional[float]:
        """Fetch 20-day moving average for momentum exit check."""
        try:
            hist = yf.Ticker(ticker).history(period="1mo")
            if len(hist) >= 20:
                return float(hist["Close"].tail(20).mean())
        except Exception:
            pass
        return None

    def _take_snapshots(self) -> int:
        """Record daily ledger snapshots for charting."""
        summary = self.get_portfolio_summary()
        count = 0
        for trader_id, data in summary.items():
            snap = LedgerSnapshot(
                trader_id=trader_id,
                snapshot_date=datetime.now(timezone.utc),
                credits=data["credits"],
                unrealised_pnl=data["unrealised_pnl"],
                total_value=data["total_value"],
            )
            self.session.add(snap)
            count += 1
        self.session.commit()
        return count

    def _check_regard_reset(self) -> bool:
        """
        Silently restore the Ape to a clean 1000-credit net worth if it has
        busted. Triggers on true net worth (cash + open positions marked to
        market), NOT cash alone — cash goes low whenever the Ape is merely
        fully deployed, so a cash-based check would hand it a fresh 1000 on top
        of live positions (2000 net worth). On a genuine bust we record the
        wipeout on each open trade, clear the positions, then refill to 1000.
        The Ape is never told this happened.
        """
        positions = self.session.execute(
            select(Position).where(Position.trader_id == "regard")
        ).scalars().all()
        prices = self._fetch_closing_prices(
            list({p.ticker for p in positions})
        ) if positions else {}

        def _pnl(pos) -> Optional[float]:
            price = prices.get(pos.ticker)
            if price is None:
                return None
            entry, cr = float(pos.entry_price), float(pos.credits_risked)
            move = (price - entry) / entry if pos.direction == "LONG" else (entry - price) / entry
            return move * cr

        ledger = get_ledger(self.session, "regard")
        net_worth = float(ledger.credits)
        for pos in positions:
            net_worth += float(pos.credits_risked) + (_pnl(pos) or 0.0)

        if net_worth >= REGARD_RESET_THRESHOLD:
            return False

        now = datetime.now(timezone.utc)
        for pos in positions:
            trade = self.session.execute(
                select(Trade).where(Trade.id == pos.trade_id)
            ).scalar_one_or_none()
            if trade:
                realised = _pnl(pos)
                trade.pnl = round(realised, 2) if realised is not None else -float(pos.credits_risked)
                if prices.get(pos.ticker) is not None:
                    trade.close_price = prices[pos.ticker]
                trade.closed_at = now
                trade.reset_occurred = True
            self.session.delete(pos)

        ledger.credits = STARTING_CREDITS
        ledger.lifetime_resets += 1
        self.session.commit()
        logger.info("Ape reset (silent): net worth %.2f below threshold", net_worth)
        return True
