"""Recon demo: a web page over the files that arrived, what they held, the recon runs, the worker and its queue."""
import base64
import os
import secrets
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import func

from . import redisq
from .db import Heartbeat, ImportFile, ImportReject, Payment, ReconItem, ReconRun, Session, Task, init, now, utc
from .files import FILES_DIR, ensure_dirs, save_upload, waiting
from .recon import interrupt_stale, latest_date, run
from .samples import drop_pair

app = FastAPI(title="Recon demo")
init()
ensure_dirs()
interrupt_stale("button")   # a button run the last web process never finished
PAGE = Path(__file__).parent / "static" / "index.html"
PASSWORD = os.environ.get("APP_PASSWORD")   # set it to ask for a password (any user name) on everything but /health
iso = lambda d: utc(d).isoformat() if hasattr(d, "tzinfo") else (d.isoformat() if d else None)
money = lambda a: str(a) if a is not None else None


@app.middleware("http")
async def password(request: Request, call_next):
    if PASSWORD and request.url.path != "/health":
        kind, _, value = request.headers.get("authorization", "").partition(" ")
        try:
            given = base64.b64decode(value).decode().partition(":")[2] if kind.lower() == "basic" else ""
        except Exception:  # noqa: BLE001: a malformed header is just a wrong password
            given = ""
        if not secrets.compare_digest(given.encode(), PASSWORD.encode()):
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Recon demo"'})
    return await call_next(request)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def page():
    return FileResponse(PAGE)


def file_json(f: ImportFile) -> dict:
    return {"id": f.id, "name": f.name, "source": f.source, "statementDate": iso(f.statement_date), "status": f.status,
            "size": f.size, "rowsTotal": f.rows_total, "rowsOk": f.rows_ok, "rowsRejected": f.rows_rejected, "error": f.error,
            "storedAs": f.stored_as, "receivedAt": iso(f.received_at), "startedAt": iso(f.started_at), "finishedAt": iso(f.finished_at)}


def run_json(r: ReconRun) -> dict:
    return {"id": r.id, "trigger": r.trigger, "statementDate": iso(r.statement_date), "status": r.status, "startedAt": iso(r.started_at),
            "finishedAt": iso(r.finished_at), "total": r.total, "matched": r.matched, "mismatched": r.mismatched, "missing": r.missing,
            "note": r.note, "error": r.error, "host": r.host}


@app.get("/api/status")
def status():
    with Session() as s:
        h = s.get(Heartbeat, "worker")
        beat = utc(h.beat_at) if h else None
        alive = bool(beat and now() - beat < timedelta(seconds=20))
        last_sched = s.query(ReconRun).filter(ReconRun.trigger == "schedule").order_by(ReconRun.id.desc()).first()
        return {
            "worker": {"alive": alive, "lastBeat": iso(beat), "processed": h.processed if h else 0, "host": h.host if h else None, "pid": h.pid if h else None},
            "queue": {"queued": s.query(Task).filter(Task.status == "queued").count(), "running": s.query(Task).filter(Task.status == "running").count()},
            "lastScheduled": iso(last_sched.started_at) if last_sched else None,
            "payments": s.query(Payment).count(),
            "folder": str(FILES_DIR),
            "inbox": waiting(),
            "processing": [file_json(f) for f in s.query(ImportFile).filter(ImportFile.status == "processing")],
            "files": [file_json(f) for f in s.query(ImportFile).order_by(ImportFile.id.desc()).limit(30)],
            "runs": [run_json(r) for r in s.query(ReconRun).order_by(ReconRun.id.desc()).limit(25)],
            "redis": redisq.status(),
        }


@app.get("/api/files/{file_id}")
def file_detail(file_id: int, limit: int = 500):
    """A file and its records: the lines it couldn't import (with why) and the payments it brought in."""
    with Session() as s:
        f = s.get(ImportFile, file_id)
        if not f:
            raise HTTPException(404, "No such file.")
        rejects = s.query(ImportReject).filter(ImportReject.file_id == file_id).order_by(ImportReject.line).limit(limit).all()
        payments = s.query(Payment).filter(Payment.file_id == file_id).order_by(Payment.line).limit(limit).all()
        return {"file": file_json(f),
                "rejects": [{"line": r.line, "raw": r.raw, "reason": r.reason} for r in rejects],
                "payments": [{"line": p.line, "reference": p.reference, "amount": money(p.amount), "description": p.description} for p in payments]}


@app.get("/api/runs/{run_id}")
def run_detail(run_id: int, limit: int = 500):
    """A recon run, the files it reconciled (that day's) and the problems it found."""
    with Session() as s:
        r = s.get(ReconRun, run_id)
        if not r:
            raise HTTPException(404, "No such run.")
        q = s.query(ReconItem).filter(ReconItem.run_id == run_id)
        files = s.query(ImportFile).filter(ImportFile.statement_date == r.statement_date, ImportFile.status == "done",
                                           ImportFile.started_at <= (r.finished_at or now())).order_by(ImportFile.id)
        return {"run": run_json(r), "problemCount": q.count(), "files": [file_json(f) for f in files],
                "problems": [{"reference": i.reference, "problem": i.problem, "ledger": money(i.ledger_amount), "bank": money(i.bank_amount)}
                             for i in q.order_by(ReconItem.id).limit(limit)]}


@app.post("/api/upload")
def upload(files: list[UploadFile] = File(...)):
    """The Upload button: the files go into the inbox, exactly as if they had arrived by SFTP; the worker imports them."""
    saved, errors = [], []
    for f in files:
        try:
            saved.append(save_upload(f.filename, iter(lambda: f.file.read(1 << 20), b"")))
        except ValueError as e:
            errors.append(f"{f.filename}: {e}")
    if errors and not saved:
        raise HTTPException(400, " ".join(errors))
    return {"saved": saved, "errors": errors}


@app.post("/api/samples")
def samples():
    """A fresh ledger + bank file for today, into the inbox."""
    return {"saved": drop_pair()}


def _day(value: str | None) -> date:
    if value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise HTTPException(400, "date must be YYYY-MM-DD")
    day = latest_date()
    if day is None:
        raise HTTPException(400, "No files have been imported yet: upload or drop some first.")
    return day


@app.post("/api/queue")
def queue(date: str | None = None):
    """A recon of a day (default: the newest) for the worker to pick up (shows whether the worker is running)."""
    day = _day(date)
    with Session() as s:
        t = Task(statement_date=day, reason="queued from the page")
        s.add(t)
        s.commit()
        return {"queued": t.id, "date": iso(day)}


@app.post("/api/redis/queue")
def redis_queue(date: str | None = None):
    """A recon of a day (default: the newest) on the Redis queue, for the Redis worker (app.redis_worker)."""
    day = _day(date)
    r = redisq.client()
    if r is None:
        raise HTTPException(409, "REDIS_URL isn't set: set the app's Redis on the Ops Console's Workers & jobs tab first.")
    try:
        task_id = redisq.enqueue(r, day, "queued from the page")
    except Exception as e:  # noqa: BLE001: Redis unreachable or refusing
        raise HTTPException(502, f"Couldn't reach Redis: {type(e).__name__}: {e}"[:300])
    return {"queued": task_id, "date": iso(day)}


@app.post("/api/run-now")
def run_now(date: str | None = None):
    """A recon right here in the web process (for comparison with the worker and the schedule)."""
    r = run("button", _day(date))
    return {"run": r.id, "date": iso(r.statement_date)}
