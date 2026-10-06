from sqlalchemy import (
    create_engine, Column, Integer, String, Numeric, Text,
    Boolean, DateTime, ForeignKey, ARRAY
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func

Base = declarative_base()


class Report(Base):
    __tablename__ = "reports"

    id = Column(Integer, primary_key=True)
    ticker = Column(String(20), nullable=False, index=True)
    company_name = Column(String(200))
    report_date = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    trigger_type = Column(String(50))  # 'earnings', 'news_spike', 'wsb_spike', 'weekly', 'manual'
    bull_case = Column(Text)
    bear_case = Column(Text)
    key_risks = Column(Text)
    price_at_report = Column(Numeric(10, 2))
    analyst_rating = Column(String(10))   # 'BUY', 'SELL', 'HOLD'
    price_target = Column(Numeric(10, 2))
    price_target_horizon = Column(String(50), default="90 days")
    confidence = Column(String(10))       # 'HIGH', 'MEDIUM', 'LOW'
    summary = Column(Text)               # 200-word plain English summary
    raw_data = Column(JSONB)

    trades = relationship("Trade", back_populates="report")
    debates = relationship("Debate", back_populates="report")


class Ledger(Base):
    __tablename__ = "ledgers"

    id = Column(Integer, primary_key=True)
    trader_id = Column(String(50), nullable=False, unique=True, index=True)
    credits = Column(Numeric(10, 2), nullable=False, default=1000.00)
    lifetime_resets = Column(Integer, default=0)  # Ape only
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True)
    trader_id = Column(String(50), nullable=False, index=True)
    ticker = Column(String(20), nullable=False)
    action = Column(String(10), nullable=False)  # 'BUY', 'SELL', 'SHORT', 'COVER', 'HOLD', 'CLOSE'
    credits_risked = Column(Numeric(10, 2))
    price_at_trade = Column(Numeric(10, 2))
    reasoning = Column(Text)
    report_id = Column(Integer, ForeignKey("reports.id"))
    traded_at = Column(DateTime(timezone=True), server_default=func.now())
    closed_at = Column(DateTime(timezone=True))
    close_price = Column(Numeric(10, 2))
    pnl = Column(Numeric(10, 2))          # null until closed
    reset_occurred = Column(Boolean, default=False)  # Ape only

    report = relationship("Report", back_populates="trades")


class Position(Base):
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True)
    trader_id = Column(String(50), nullable=False, index=True)
    ticker = Column(String(20), nullable=False)
    direction = Column(String(10), nullable=False)  # 'LONG' or 'SHORT'
    credits_risked = Column(Numeric(10, 2))
    entry_price = Column(Numeric(10, 2))
    entry_date = Column(DateTime(timezone=True), server_default=func.now())
    trade_id = Column(Integer, ForeignKey("trades.id"))


class Debate(Base):
    __tablename__ = "debates"

    id = Column(Integer, primary_key=True)
    ticker = Column(String(20), nullable=False)
    report_id = Column(Integer, ForeignKey("reports.id"))
    conflicting_traders = Column(ARRAY(String))
    debate_transcript = Column(Text)
    resolution = Column(Text)
    debated_at = Column(DateTime(timezone=True), server_default=func.now())

    report = relationship("Report", back_populates="debates")


class LedgerSnapshot(Base):
    """Daily snapshot for charting P&L over time."""
    __tablename__ = "ledger_snapshots"

    id = Column(Integer, primary_key=True)
    trader_id = Column(String(50), nullable=False, index=True)
    snapshot_date = Column(DateTime(timezone=True), server_default=func.now())
    credits = Column(Numeric(10, 2))
    unrealised_pnl = Column(Numeric(10, 2))
    total_value = Column(Numeric(10, 2))  # credits + unrealised_pnl


class WatchedTicker(Base):
    """Dynamic ticker watchlist."""
    __tablename__ = "watched_tickers"

    id = Column(Integer, primary_key=True)
    ticker = Column(String(20), nullable=False, unique=True)
    company_name = Column(String(200))
    exchange = Column(String(20))         # 'US', 'EU', 'AS' etc.
    added_reason = Column(String(50))     # 'core', 'wsb_spike', 'news_spike', 'manual'
    added_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True))  # null = permanent core ticker


def create_tables(engine):
    Base.metadata.create_all(engine)


def get_engine(database_url: str):
    return create_engine(database_url, pool_pre_ping=True)
