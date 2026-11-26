"""The database: DATABASE_URL from the Ops Console (SQLite, MySQL/MariaDB or PostgreSQL), else a local SQLite file."""
import os
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, Numeric, String, Text, create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

URL = os.environ.get("DATABASE_URL") or "sqlite:///./recon.db"
if URL.startswith("mysql://"):  # a plain mysql:// URL: use the PyMySQL driver
    URL = "mysql+pymysql://" + URL[len("mysql://"):]
if URL.startswith("postgres://"):
    URL = "postgresql://" + URL[len("postgres://"):]

engine = create_engine(URL, pool_pre_ping=True, **({"connect_args": {"check_same_thread": False, "timeout": 15}} if URL.startswith("sqlite") else {}))
if URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _wal(conn, _):  # the web app, the worker and the job share the file
        conn.execute("PRAGMA journal_mode=WAL")

Session = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()
now = lambda: datetime.now(timezone.utc)


class Transaction(Base):
    """A payment as two systems see it: our ledger and the bank's statement."""
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True)
    reference = Column(String(40), index=True, nullable=False)
    source = Column(String(10), nullable=False)  # ledger | bank
    amount = Column(Numeric(12, 2), nullable=False)
    created_at = Column(DateTime(timezone=True), default=now)


class Task(Base):
    """A recon someone queued for the worker (a database-backed queue: no Redis needed)."""
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    status = Column(String(10), default="queued", index=True)  # queued | running | done | failed
    requested_at = Column(DateTime(timezone=True), default=now)
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))
    error = Column(Text)


class ReconRun(Base):
    __tablename__ = "recon_runs"
    id = Column(Integer, primary_key=True)
    trigger = Column(String(20), nullable=False)  # schedule | worker | button
    started_at = Column(DateTime(timezone=True), default=now)
    finished_at = Column(DateTime(timezone=True))
    matched = Column(Integer, default=0)
    mismatched = Column(Integer, default=0)
    missing = Column(Integer, default=0)
    host = Column(String(100))


class ReconItem(Base):
    """One problem found by a run: an amount that differs, or a payment only one side has."""
    __tablename__ = "recon_items"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, index=True, nullable=False)
    reference = Column(String(40), nullable=False)
    problem = Column(String(20), nullable=False)  # amount_differs | missing_in_bank | missing_in_ledger
    ledger_amount = Column(Numeric(12, 2))
    bank_amount = Column(Numeric(12, 2))


class Heartbeat(Base):
    """The worker's pulse: one row it updates every few seconds."""
    __tablename__ = "heartbeats"
    name = Column(String(40), primary_key=True)
    beat_at = Column(DateTime(timezone=True))
    processed = Column(Integer, default=0)
    host = Column(String(100))
    pid = Column(Integer)


def init():
    Base.metadata.create_all(engine)
