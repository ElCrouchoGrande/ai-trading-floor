from contextlib import contextmanager
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy import select
from db.models import Ledger, get_engine
import os

TRADER_IDS = ["momentum", "insider", "short", "ape", "boomer", "hugger"]
STARTING_CREDITS = 1000.00
APE_RESET_THRESHOLD = 100.00


def get_session_factory(database_url: str = None):
    url = database_url or os.environ["DATABASE_URL"]
    engine = get_engine(url)
    return sessionmaker(bind=engine), engine


@contextmanager
def get_session(session_factory) -> Session:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def initialise_ledgers(session: Session):
    """Create ledger rows for all traders if they don't exist."""
    for trader_id in TRADER_IDS:
        existing = session.execute(
            select(Ledger).where(Ledger.trader_id == trader_id)
        ).scalar_one_or_none()

        if not existing:
            session.add(Ledger(
                trader_id=trader_id,
                credits=STARTING_CREDITS,
                lifetime_resets=0
            ))
    session.commit()


def get_ledger(session: Session, trader_id: str) -> Ledger:
    return session.execute(
        select(Ledger).where(Ledger.trader_id == trader_id)
    ).scalar_one()


def adjust_credits(session: Session, trader_id: str, delta: float):
    ledger = get_ledger(session, trader_id)
    ledger.credits = float(ledger.credits) + delta
    session.commit()
    return float(ledger.credits)
