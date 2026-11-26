"""The database: DATABASE_URL from the Ops Console (SQLite, MySQL/MariaDB or PostgreSQL), else a local SQLite file.

The tables are created on start. They have new names from the version that generated random payments
(transactions, tasks, recon_runs, recon_items), so an existing database needs no migration: the old tables are
simply no longer used and can be dropped.
"""
import os
from datetime import datetime, timezone

from sqlalchemy import Column, Date, DateTime, Integer, Numeric, String, Text, UniqueConstraint, create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

URL = os.environ.get("DATABASE_URL") or "sqlite:///./recon.db"
if URL.startswith("mysql://"):  # a plain mysql:// URL: use the PyMySQL driver
    URL = "mysql+pymysql://" + URL[len("mysql://"):]
if URL.startswith("postgres://"):
    URL = "postgresql://" + URL[len("postgres://"):]
SQLITE = URL.startswith("sqlite")

engine = create_engine(URL, pool_pre_ping=True, **({"connect_args": {"check_same_thread": False, "timeout": 15}} if SQLITE else {}))
if SQLITE:
    @event.listens_for(engine, "connect")
    def _wal(conn, _):  # the web app, the worker and the job share the file
        conn.execute("PRAGMA journal_mode=WAL")

Session = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()
now = lambda: datetime.now(timezone.utc)


def utc(d: datetime | None) -> datetime | None:
    """SQLite gives times back without a time zone (they are stored as UTC): make every time comparable."""
    return d.replace(tzinfo=timezone.utc) if d is not None and d.tzinfo is None else d


class ImportFile(Base):
    """A file that arrived in the inbox (by SFTP or the page's Upload button) and what importing it did."""
    __tablename__ = "import_files"
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    source = Column(String(10))               # bank | ledger (from the name), None when the name says neither
    statement_date = Column(Date, index=True)
    size = Column(Integer)
    sha256 = Column(String(64), index=True)
    status = Column(String(12), default="processing", index=True)  # processing | done | failed | duplicate | interrupted
    rows_total = Column(Integer, default=0)
    rows_ok = Column(Integer, default=0)
    rows_rejected = Column(Integer, default=0)
    error = Column(Text)
    stored_as = Column(String(300))           # where the file is now, relative to FILES_DIR (archive/… or failed/…)
    received_at = Column(DateTime(timezone=True))   # when it landed in the inbox (its modification time)
    started_at = Column(DateTime(timezone=True), default=now)
    finished_at = Column(DateTime(timezone=True))
    host = Column(String(100))


class ImportReject(Base):
    """A line of a file that could not be imported, and why."""
    __tablename__ = "import_rejects"
    id = Column(Integer, primary_key=True)
    file_id = Column(Integer, index=True, nullable=False)
    line = Column(Integer, nullable=False)
    raw = Column(String(500))
    reason = Column(String(200), nullable=False)


class Payment(Base):
    """A payment as one side sees it: our ledger or the bank's statement. One reference per side, ever."""
    __tablename__ = "payments"
    __table_args__ = (UniqueConstraint("source", "reference", name="uq_payment_source_reference"),)
    id = Column(Integer, primary_key=True)
    file_id = Column(Integer, index=True, nullable=False)
    source = Column(String(10), nullable=False)  # ledger | bank
    statement_date = Column(Date, index=True, nullable=False)
    reference = Column(String(40), index=True, nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)
    description = Column(String(200))
    line = Column(Integer)


class Task(Base):
    """A recon queued for the worker (a database-backed queue: no Redis needed)."""
    __tablename__ = "recon_queue"
    id = Column(Integer, primary_key=True)
    status = Column(String(10), default="queued", index=True)  # queued | running | done | failed
    statement_date = Column(Date)
    reason = Column(String(120))                # e.g. "both files for 26 Nov are in" or "queued from the page"
    requested_at = Column(DateTime(timezone=True), default=now)
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))
    run_id = Column(Integer)
    error = Column(Text)


class ReconRun(Base):
    """One reconciliation of one statement date."""
    __tablename__ = "recons"
    id = Column(Integer, primary_key=True)
    trigger = Column(String(20), nullable=False)  # schedule | worker | button
    statement_date = Column(Date, index=True, nullable=False)
    status = Column(String(12), default="running")  # running | done | failed | interrupted
    started_at = Column(DateTime(timezone=True), default=now)
    finished_at = Column(DateTime(timezone=True))
    total = Column(Integer, default=0)
    matched = Column(Integer, default=0)
    mismatched = Column(Integer, default=0)
    missing = Column(Integer, default=0)
    note = Column(String(200))                  # e.g. "No bank file received for 2026-11-25"
    error = Column(Text)
    host = Column(String(100))


class ReconItem(Base):
    """One problem found by a run: an amount that differs, or a payment only one side has."""
    __tablename__ = "recon_problems"
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
