"""rename the legacy WSB Ape trader id to 'ape'

The Ape's trader_id used to be a different, slang-derived string. This migration
rewrites it to 'ape' everywhere it is stored:

  * ledgers / trades / positions / ledger_snapshots .trader_id
  * debates.conflicting_traders (array)
  * debates.debate_transcript / debates.resolution -- old rows baked the raw id
    into the text; it is swapped for the display name, exactly as the old
    display-time cleanup in engine.debate.humanize_transcript did.

It is idempotent (a second run changes nothing) and refuses to run if both the
old and new ledgers already exist, rather than silently merging two pots.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


OLD_ID = "regard"
NEW_ID = "ape"
DISPLAY_NAME = "The WSB Ape"
OLD_ID_WORD_RE = r"\yregard\y"  # Postgres word-boundary regex

TRADER_ID_TABLES = ("ledgers", "trades", "positions", "ledger_snapshots")


def _rename_ids(old: str, new: str) -> None:
    bind = op.get_bind()

    has_old = bind.execute(
        sa.text("SELECT 1 FROM ledgers WHERE trader_id = :id"), {"id": old}
    ).first()
    has_new = bind.execute(
        sa.text("SELECT 1 FROM ledgers WHERE trader_id = :id"), {"id": new}
    ).first()
    if has_old and has_new:
        raise RuntimeError(
            f"Both '{old}' and '{new}' ledgers exist; refusing to merge two pots. "
            "Resolve manually before re-running the migration."
        )

    for table in TRADER_ID_TABLES:
        bind.execute(
            sa.text(f"UPDATE {table} SET trader_id = :new WHERE trader_id = :old"),
            {"old": old, "new": new},
        )

    bind.execute(
        sa.text(
            "UPDATE debates "
            "SET conflicting_traders = array_replace("
            "conflicting_traders, CAST(:old AS varchar), CAST(:new AS varchar)) "
            "WHERE CAST(:old AS varchar) = ANY(conflicting_traders)"
        ),
        {"old": old, "new": new},
    )


def upgrade() -> None:
    _rename_ids(OLD_ID, NEW_ID)

    bind = op.get_bind()
    for column in ("debate_transcript", "resolution"):
        bind.execute(
            sa.text(
                f"UPDATE debates SET {column} = "
                f"regexp_replace({column}, :pattern, :name, 'gi') "
                f"WHERE {column} ~* :pattern"
            ),
            {"pattern": OLD_ID_WORD_RE, "name": DISPLAY_NAME},
        )


def downgrade() -> None:
    # Trader ids are restored. Debate text is not: the rewritten transcripts now
    # carry the display name, which is what the app showed anyway.
    _rename_ids(NEW_ID, OLD_ID)
