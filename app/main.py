"""Recon demo: a web page over the recon runs, the worker's heartbeat and its queue."""
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from .db import Heartbeat, ReconItem, ReconRun, Session, Task, Transaction, init, now
from .recon import add_sample, run

app = FastAPI(title="Recon demo")
init()
PAGE = Path(__file__).parent / "static" / "index.html"
iso = lambda d: d.isoformat() if d else None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def page():
    return FileResponse(PAGE)


@app.get("/api/status")
def status():
    with Session() as s:
        h = s.get(Heartbeat, "worker")
        beat = h.beat_at if h else None
        if beat is not None and beat.tzinfo is None:  # SQLite returns naive times (stored as UTC)
            beat = beat.replace(tzinfo=now().tzinfo)
        alive = bool(beat and now() - beat < timedelta(seconds=20))
        runs = s.query(ReconRun).order_by(ReconRun.id.desc()).limit(25).all()
        last_sched = s.query(ReconRun).filter(ReconRun.trigger == "schedule").order_by(ReconRun.id.desc()).first()
        return {
            "worker": {"alive": alive, "lastBeat": iso(beat), "processed": h.processed if h else 0, "host": h.host if h else None, "pid": h.pid if h else None},
            "queue": {"queued": s.query(Task).filter(Task.status == "queued").count(), "running": s.query(Task).filter(Task.status == "running").count(),
                      "recent": [{"id": t.id, "status": t.status, "requestedAt": iso(t.requested_at), "finishedAt": iso(t.finished_at), "error": t.error}
                                 for t in s.query(Task).order_by(Task.id.desc()).limit(8)]},
            "lastScheduled": iso(last_sched.started_at) if last_sched else None,
            "transactions": s.query(Transaction).count(),
            "runs": [{"id": r.id, "trigger": r.trigger, "startedAt": iso(r.started_at), "finishedAt": iso(r.finished_at), "matched": r.matched,
                      "mismatched": r.mismatched, "missing": r.missing, "host": r.host} for r in runs],
        }


@app.get("/api/runs/{run_id}")
def run_items(run_id: int):
    with Session() as s:
        return [{"reference": i.reference, "problem": i.problem, "ledger": str(i.ledger_amount) if i.ledger_amount is not None else None,
                 "bank": str(i.bank_amount) if i.bank_amount is not None else None}
                for i in s.query(ReconItem).filter(ReconItem.run_id == run_id).order_by(ReconItem.id).limit(200)]


@app.post("/api/queue")
def queue():
    """A recon for the worker to pick up (shows whether the worker is running)."""
    with Session() as s:
        t = Task()
        s.add(t)
        s.commit()
        return {"queued": t.id}


@app.post("/api/sample")
def sample():
    return {"added": add_sample(40)}


@app.post("/api/run-now")
def run_now():
    """A recon right here in the web process (for comparison with the worker and the schedule)."""
    r = run("button")
    return {"run": r.id}
