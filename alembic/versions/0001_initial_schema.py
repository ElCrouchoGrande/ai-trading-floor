"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "reports",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("ticker", sa.String(length=20), nullable=False),
        sa.Column("company_name", sa.String(length=200), nullable=True),
        sa.Column(
            "report_date",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("trigger_type", sa.String(length=50), nullable=True),
        sa.Column("bull_case", sa.Text(), nullable=True),
        sa.Column("bear_case", sa.Text(), nullable=True),
        sa.Column("key_risks", sa.Text(), nullable=True),
        sa.Column("price_at_report", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("analyst_rating", sa.String(length=10), nullable=True),
        sa.Column("price_target", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("price_target_horizon", sa.String(length=50), nullable=True),
        sa.Column("confidence", sa.String(length=10), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("raw_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index(op.f("ix_reports_ticker"), "reports", ["ticker"], unique=False)

    op.create_table(
        "ledgers",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("trader_id", sa.String(length=50), nullable=False),
        sa.Column("credits", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("lifetime_resets", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.UniqueConstraint("trader_id", name="uq_ledgers_trader_id"),
    )
    op.create_index(
        op.f("ix_ledgers_trader_id"), "ledgers", ["trader_id"], unique=False
    )

    op.create_table(
        "trades",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("trader_id", sa.String(length=50), nullable=False),
        sa.Column("ticker", sa.String(length=20), nullable=False),
        sa.Column("action", sa.String(length=10), nullable=False),
        sa.Column("credits_risked", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("price_at_trade", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("report_id", sa.Integer(), nullable=True),
        sa.Column(
            "traded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_price", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("pnl", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("reset_occurred", sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"], name="fk_trades_report_id"),
    )
    op.create_index(
        op.f("ix_trades_trader_id"), "trades", ["trader_id"], unique=False
    )

    op.create_table(
        "positions",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("trader_id", sa.String(length=50), nullable=False),
        sa.Column("ticker", sa.String(length=20), nullable=False),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("credits_risked", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("entry_price", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column(
            "entry_date",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column("trade_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["trade_id"], ["trades.id"], name="fk_positions_trade_id"),
    )
    op.create_index(
        op.f("ix_positions_trader_id"), "positions", ["trader_id"], unique=False
    )

    op.create_table(
        "debates",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("ticker", sa.String(length=20), nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=True),
        sa.Column(
            "conflicting_traders",
            postgresql.ARRAY(sa.String()),
            nullable=True,
        ),
        sa.Column("debate_transcript", sa.Text(), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column(
            "debated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"], name="fk_debates_report_id"),
    )

    op.create_table(
        "ledger_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("trader_id", sa.String(length=50), nullable=False),
        sa.Column(
            "snapshot_date",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column("credits", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("unrealised_pnl", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("total_value", sa.Numeric(precision=10, scale=2), nullable=True),
    )
    op.create_index(
        op.f("ix_ledger_snapshots_trader_id"),
        "ledger_snapshots",
        ["trader_id"],
        unique=False,
    )

    op.create_table(
        "watched_tickers",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("ticker", sa.String(length=20), nullable=False),
        sa.Column("company_name", sa.String(length=200), nullable=True),
        sa.Column("exchange", sa.String(length=20), nullable=True),
        sa.Column("added_reason", sa.String(length=50), nullable=True),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("ticker", name="uq_watched_tickers_ticker"),
    )


def downgrade() -> None:
    op.drop_table("watched_tickers")
    op.drop_index(op.f("ix_ledger_snapshots_trader_id"), table_name="ledger_snapshots")
    op.drop_table("ledger_snapshots")
    op.drop_table("debates")
    op.drop_index(op.f("ix_positions_trader_id"), table_name="positions")
    op.drop_table("positions")
    op.drop_index(op.f("ix_trades_trader_id"), table_name="trades")
    op.drop_table("trades")
    op.drop_index(op.f("ix_ledgers_trader_id"), table_name="ledgers")
    op.drop_table("ledgers")
    op.drop_index(op.f("ix_reports_ticker"), table_name="reports")
    op.drop_table("reports")
