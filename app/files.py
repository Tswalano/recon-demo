"""Files in, payments out: the inbox the bank (SFTP) and the page (Upload) drop statement files into, and the import.

FILES_DIR (default ./files) holds four folders:
    inbox/       files arrive here (SFTP or the Upload button); the worker picks them up
    processing/  a file the worker is importing right now (moved here first, so it is only taken once)
    archive/     imported files, named <file id>_<original name>
    failed/      files that could not be imported, and duplicates

A file's name says what it is: bank_YYYY-MM-DD[anything].csv or ledger_YYYY-MM-DD[anything].csv.
Its columns: reference,date,amount[,description] with a header line; every date must be the file's date.

Set FILES_DIR to a folder outside the app's code (e.g. /srv/recon-files) so a deploy never replaces it.
"""
import csv
import hashlib
import io
import os
import re
import shutil
import socket
import time
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

from .db import ImportFile, ImportReject, Payment, Session, now

FILES_DIR = Path(os.environ.get("FILES_DIR") or "./files").resolve()
INBOX, PROCESSING, ARCHIVE, FAILED = (FILES_DIR / d for d in ("inbox", "processing", "archive", "failed"))
MAX_BYTES = int(float(os.environ.get("MAX_FILE_MB") or 20) * 1024 * 1024)
ROW_DELAY = float(os.environ.get("ROW_DELAY_MS") or 3) / 1000   # a little time per line, so progress can be watched
NAME = re.compile(r"^(bank|ledger)_(\d{4}-\d{2}-\d{2})[^/]*\.csv$", re.IGNORECASE)
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,150}$")


class Stopped(Exception):
    """The process was asked to stop part-way: nothing was written, the file goes back to the inbox."""


def ensure_dirs():
    for d in (INBOX, PROCESSING, ARCHIVE, FAILED):
        d.mkdir(parents=True, exist_ok=True)


def _ignored(name: str) -> bool:
    """Files still being written: hidden ones and *.part / *.tmp / *.filepart (what SFTP clients use while uploading)."""
    return name.startswith(".") or name.lower().endswith((".part", ".tmp", ".filepart"))


def waiting() -> list[dict]:
    """What's in the inbox now (for the page)."""
    try:
        entries = [e for e in os.scandir(INBOX) if e.is_file() and not _ignored(e.name)]
    except FileNotFoundError:
        return []
    return [{"name": e.name, "size": e.stat().st_size} for e in sorted(entries, key=lambda e: e.stat().st_mtime)]


class Inbox:
    """Finds files that have finished arriving: an SFTP upload is written bit by bit, so a file is only taken once its
    size and modification time are the same on two looks and it hasn't changed for 2 seconds."""

    def __init__(self):
        self.seen: dict[str, tuple[int, float]] = {}

    def ready(self) -> Path | None:
        try:
            entries = sorted((e for e in os.scandir(INBOX) if e.is_file() and not _ignored(e.name)), key=lambda e: e.stat().st_mtime)
        except FileNotFoundError:
            return None
        current, found = {}, None
        for e in entries:
            st = e.stat()
            current[e.name] = (st.st_size, st.st_mtime)
            if found is None and self.seen.get(e.name) == current[e.name] and time.time() - st.st_mtime >= 2:
                found = Path(e.path)
        self.seen = current
        return found

    def changed(self) -> bool:
        """Something new or still growing in the inbox, or a file now ready to take: the worker stops waiting early."""
        try:
            names = {e.name: (e.stat().st_size, e.stat().st_mtime) for e in os.scandir(INBOX) if e.is_file() and not _ignored(e.name)}
        except FileNotFoundError:
            return False
        return bool(names) and (names != self.seen or any(time.time() - m >= 2 for _, m in names.values()))


def _free_name(folder: Path, name: str) -> Path:
    """folder/name, or folder/name-1, -2… if that's taken (never overwrite a file)."""
    p, stem, suffix, i = folder / name, Path(name).stem, Path(name).suffix, 1
    while p.exists():
        p, i = folder / f"{stem}-{i}{suffix}", i + 1
    return p


def save_upload(name: str, chunks) -> str:
    """The page's Upload button: write the file under a hidden name, then rename it into the inbox in one step, so
    the worker never sees half a file. Returns the name it got in the inbox."""
    name = os.path.basename(name or "").strip()
    if not SAFE_NAME.match(name) or not name.lower().endswith(".csv"):
        raise ValueError("Use a .csv file whose name has only letters, digits, '.', '-' and '_'.")
    ensure_dirs()
    tmp, size = INBOX / f".upload-{uuid.uuid4().hex}.part", 0
    try:
        with open(tmp, "wb") as out:
            for chunk in chunks:
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError(f"That file is over {MAX_BYTES // (1024 * 1024)} MB.")
                out.write(chunk)
        final = _free_name(INBOX, name)
        os.replace(tmp, final)
        return final.name
    finally:
        tmp.unlink(missing_ok=True)


def claim(path: Path) -> Path | None:
    """Move a file from the inbox to processing/ (an atomic rename: if two workers try, only one gets it)."""
    target = PROCESSING / f"{uuid.uuid4().hex[:8]}__{path.name}"
    try:
        os.rename(path, target)
    except FileNotFoundError:
        return None
    return target


def _original(processing_path: Path) -> str:
    return processing_path.name.split("__", 1)[-1]


def recover() -> list[str]:
    """On the worker's start: files a stopped or crashed worker was importing go back to the inbox to be imported
    again (nothing of theirs was saved), and their records are marked interrupted. This assumes one worker."""
    ensure_dirs()
    moved = []
    for p in sorted(PROCESSING.iterdir()):
        if p.is_file():
            dest = _free_name(INBOX, _original(p))
            os.rename(p, dest)
            moved.append(dest.name)
    with Session() as s:
        for f in s.query(ImportFile).filter(ImportFile.status == "processing"):
            f.status, f.finished_at, f.error = "interrupted", now(), "The worker stopped while importing it; it went back to the inbox."
        s.commit()
    return moved


def _amount(text: str) -> Decimal:
    try:
        a = Decimal(text.strip().replace(" ", ""))
    except InvalidOperation:
        raise ValueError(f"amount '{text.strip()[:30]}' is not a number")
    if not a.is_finite():
        raise ValueError(f"amount '{text.strip()[:30]}' is not a number")
    if a.as_tuple().exponent < -2:
        raise ValueError(f"amount {text.strip()} has more than 2 decimals")
    if abs(a) >= Decimal("10000000000"):
        raise ValueError(f"amount {text.strip()} is too large")
    return a


def import_file(path: Path, *, report: Callable[..., None] = lambda **_: None, tick: Callable[[], None] = lambda: None,
                should_stop: Callable[[], bool] = lambda: False) -> ImportFile:
    """Import one claimed file (in processing/). The slow part, reading and checking every line, writes nothing; the
    payments, the rejected lines and the result are then saved together in one short transaction. So a file is
    imported completely or not at all, and other processes are not kept waiting on the database meanwhile."""
    name = _original(path)
    raw = path.read_bytes()
    st = path.stat()
    m = NAME.match(name)
    with Session() as s:
        f = ImportFile(name=name, size=len(raw), sha256=hashlib.sha256(raw).hexdigest(), status="processing",
                       source=m.group(1).lower() if m else None, host=socket.gethostname(),
                       received_at=datetime.fromtimestamp(st.st_mtime, timezone.utc))
        s.add(f)
        s.commit()

    def finish(status: str, error: str | None = None, payments=(), rejects=(), total: int = 0) -> ImportFile:
        folder = ARCHIVE if status == "done" else FAILED
        dest = _free_name(folder, f"{f.id}_{name}")
        with Session() as s:
            s.add_all(payments)
            s.add_all(rejects)
            row = s.get(ImportFile, f.id)
            row.status, row.error, row.finished_at = status, error, now()
            row.rows_total, row.rows_ok, row.rows_rejected = total, len(payments), len(rejects)
            row.stored_as = str(dest.relative_to(FILES_DIR))
            s.commit()
        os.rename(path, dest)
        return row

    if not m:
        return finish("failed", "The name must be bank_YYYY-MM-DD….csv or ledger_YYYY-MM-DD….csv: it says which side and day it is.")
    source = m.group(1).lower()
    try:
        day = date.fromisoformat(m.group(2))
    except ValueError:
        return finish("failed", f"{m.group(2)} in the name is not a date.")
    with Session() as s:
        s.get(ImportFile, f.id).statement_date = day
        s.commit()
        f.statement_date = day
        same = s.query(ImportFile).filter(ImportFile.sha256 == f.sha256, ImportFile.status == "done").order_by(ImportFile.id).first()
        known = dict(s.query(Payment.reference, Payment.file_id).filter(Payment.source == source))
    if same:
        return finish("duplicate", f"Same content as file #{same.id} ({same.name}), already imported: nothing was imported again.")
    if len(raw) > MAX_BYTES:
        return finish("failed", f"The file is over {MAX_BYTES // (1024 * 1024)} MB.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return finish("failed", "This is not a UTF-8 text (CSV) file.")
    lines = list(csv.reader(io.StringIO(text)))
    rows = [(i, r) for i, r in enumerate(lines, 1) if any(c.strip() for c in r)]
    if not rows:
        return finish("failed", "The file is empty.")
    head_line, header = rows[0]
    cols = {c.strip().lower(): n for n, c in enumerate(header)}
    need = [c for c in ("reference", "date", "amount") if c not in cols]
    if need:
        return finish("failed", f"Line {head_line} should be the header 'reference,date,amount,description'; it lacks: {', '.join(need)}.")
    rows = rows[1:]
    total, payments, rejects, in_file = len(rows), [], [], {}
    label, last = f"Importing {name}", 0.0
    report(label=label, processed=0, total=total, rejected=0)
    for n, (line, r) in enumerate(rows, 1):
        if should_stop():
            raise Stopped()
        if time.monotonic() - last >= 1 or n == total:
            report(label=label, processed=n, total=total, rejected=len(rejects))
            last = time.monotonic()
        tick()
        if ROW_DELAY:
            time.sleep(ROW_DELAY)
        cell = lambda c: r[cols[c]].strip() if cols.get(c) is not None and cols[c] < len(r) else ""
        try:
            if len(r) < len(header):
                raise ValueError(f"{len(r)} columns, the header has {len(header)}")
            ref = cell("reference")
            if not ref:
                raise ValueError("no reference")
            if len(ref) > 40:
                raise ValueError("reference is longer than 40 characters")
            try:
                d = date.fromisoformat(cell("date"))
            except ValueError:
                raise ValueError(f"date '{cell('date')[:20]}' is not YYYY-MM-DD")
            if d != day:
                raise ValueError(f"dated {d}, but this is the statement for {day}")
            amount = _amount(cell("amount"))
            if ref in in_file:
                raise ValueError(f"reference repeats line {in_file[ref]}")
            if ref in known:
                raise ValueError(f"already imported from file #{known[ref]}")
        except ValueError as e:
            rejects.append(ImportReject(file_id=f.id, line=line, raw=",".join(r)[:500], reason=str(e)[:200]))
            continue
        in_file[ref] = line
        payments.append(Payment(file_id=f.id, source=source, statement_date=day, reference=ref, amount=amount,
                                description=cell("description")[:200] or None, line=line))
    if not payments:
        return finish("failed", f"None of the {total} lines could be imported.", rejects=rejects, total=total)
    return finish("done", payments=payments, rejects=rejects, total=total)


def release(path: Path):
    """A stop part-way through: put the file back in the inbox to be imported again."""
    with Session() as s:
        for f in s.query(ImportFile).filter(ImportFile.status == "processing", ImportFile.name == _original(path)):
            f.status, f.finished_at, f.error = "interrupted", now(), "The worker was stopped while importing it; it went back to the inbox."
        s.commit()
    os.rename(path, _free_name(INBOX, _original(path)))


def move_to_failed(path: Path, error: str):
    """An import that crashed: keep the file aside rather than retrying it for ever."""
    with Session() as s:
        for f in s.query(ImportFile).filter(ImportFile.status == "processing", ImportFile.name == _original(path)):
            f.status, f.finished_at, f.error = "failed", now(), error[:500]
        s.commit()
    if path.exists():
        shutil.move(str(path), _free_name(FAILED, _original(path)))
