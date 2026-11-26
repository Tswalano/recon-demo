"""The background worker (Ops Console: Workers & jobs → worker, command `python -m app.worker`).

Always running. Every few seconds it beats, then:
  1. runs the next queued recon (queued by step 2, or by the page's button);
  2. else imports the next file that has arrived in the inbox (by SFTP or the page's Upload button) and, when both
     the ledger and the bank file for a day are in, queues a recon of that day.
On start it recovers what a stopped or crashed worker left half done. Stopped part-way (a deploy, Stop), it puts the
file back or the recon back in the queue: nothing is saved half done.
"""
import os
import signal
import socket
import time

from .db import Heartbeat, Session, Task, init, now
from .files import FILES_DIR, NAME, Inbox, Stopped, claim, ensure_dirs, import_file, move_to_failed, recover, release, waiting
from .recon import interrupt_stale, metric, run, sides

stopping = False
def stop(*_):
    global stopping
    stopping = True
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
should_stop = lambda: stopping


_last_beat = 0.0
def beat(processed_delta: int = 0, *, every: float = 0):
    """Update the heartbeat row (every: at most this often, for calls from inside a long import or recon)."""
    global _last_beat
    if every and time.monotonic() - _last_beat < every:
        return
    _last_beat = time.monotonic()
    with Session() as s:
        h = s.get(Heartbeat, "worker") or Heartbeat(name="worker", processed=0)
        h.beat_at, h.host, h.pid = now(), socket.gethostname(), os.getpid()
        h.processed = (h.processed or 0) + processed_delta
        s.merge(h)
        s.commit()
tick = lambda: beat(every=3)


def next_task():
    with Session() as s:
        q = s.query(Task).filter(Task.status == "queued").order_by(Task.id)
        t = (q if s.bind.url.drivername.startswith("sqlite") else q.with_for_update(skip_locked=True)).first()
        if not t:
            return None
        t.status, t.started_at = "running", now()
        s.commit()
        return t


def counts():
    with Session() as s:
        return {"tasks_done": s.query(Task).filter(Task.status == "done").count(), "tasks_failed": s.query(Task).filter(Task.status == "failed").count(),
                "queued": s.query(Task).filter(Task.status == "queued").count()}


def queue_recon_if_complete(day, file_id):
    """Both sides for the day are in: queue its recon (unless one is already queued, or another file for the same day
    is still waiting in the inbox: its import queues the recon, so the day isn't reconciled half-updated)."""
    if day is None or sides(day) != {"bank", "ledger"}:
        return
    if any((m := NAME.match(f["name"])) and m.group(2) == day.isoformat() for f in waiting()):
        return
    with Session() as s:
        if s.query(Task).filter(Task.status == "queued", Task.statement_date == day).first():
            return
        s.add(Task(statement_date=day, reason=f"file #{file_id} completed {day}"))
        s.commit()


def handle_file(inbox: Inbox) -> bool:
    path = inbox.ready()
    if not path or not (claimed := claim(path)):
        return False
    try:
        f = import_file(claimed, report=metric, tick=tick, should_stop=should_stop)
    except Stopped:
        release(claimed)
        print(f"stopped while importing {path.name}: it went back to the inbox", flush=True)
        return True
    except Exception as e:  # noqa: BLE001: a file the code couldn't handle: set it aside and keep going
        move_to_failed(claimed, f"The import crashed: {type(e).__name__}: {e}")
        print(f"import of {path.name} crashed: {type(e).__name__}: {e}", flush=True)
        return True
    print(f"file #{f.id} {f.name}: {f.status}, {f.rows_ok} imported, {f.rows_rejected} rejected"
          f"{' - ' + f.error if f.error else ''}", flush=True)
    metric(label=f"{f.name}: {f.status}", imported=f.rows_ok, rejected=f.rows_rejected, **counts())
    if f.status == "done":
        queue_recon_if_complete(f.statement_date, f.id)
    beat(1)
    return True


def handle_task() -> bool:
    t = next_task()
    if not t:
        return False
    status, err, run_id = "done", None, None
    try:
        if t.statement_date is None:
            raise ValueError("No statement date to reconcile.")
        r = run("worker", t.statement_date, label=f"Recon task #{t.id} ({t.statement_date})", extra=counts(), tick=tick, should_stop=should_stop)
        run_id = r.id
    except Stopped:
        with Session() as s:   # back in the queue, for the worker's next start
            row = s.get(Task, t.id)
            row.status, row.started_at = "queued", None
            s.commit()
        return True
    except Exception as e:  # noqa: BLE001: record it and keep going
        status, err = "failed", str(e)[:500]
    with Session() as s:
        row = s.get(Task, t.id)
        row.status, row.finished_at, row.error, row.run_id = status, now(), err, run_id
        s.commit()
    beat(1)
    metric(label=f"Recon task #{t.id} {status}", **counts())
    return True


def main():
    init()
    ensure_dirs()
    print(f"worker started (pid {os.getpid()}), watching {FILES_DIR / 'inbox'}", flush=True)
    for name in recover():
        print(f"recovered {name}: back in the inbox to import again", flush=True)
    with Session() as s:   # recons a stopped worker was running go back in the queue (one worker assumed)
        for t in s.query(Task).filter(Task.status == "running"):
            t.status, t.started_at = "queued", None
            print(f"recovered recon task #{t.id}: back in the queue", flush=True)
        s.commit()
    interrupt_stale("worker")
    inbox, idle_reported = Inbox(), 0.0
    while not stopping:
        beat()
        if handle_task() or handle_file(inbox):   # a queued recon first: a day is reconciled as soon as its files are in
            idle_reported = time.monotonic()
            continue
        if time.monotonic() - idle_reported > 60:   # idle: what it has done so far, once a minute
            metric(label="Waiting for files", **counts()); idle_reported = time.monotonic()
        for _ in range(10):  # 5 seconds, in small steps so a stop is quick and a new file is seen soon
            if stopping or inbox.changed():
                break
            time.sleep(0.5)
    print("worker stopping", flush=True)


if __name__ == "__main__":
    main()
