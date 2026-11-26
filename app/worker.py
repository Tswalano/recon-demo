"""The background worker (Ops Console: Workers & jobs → worker, command `python -m app.worker`).
Beats every 5 seconds and runs the recons people queue from the web page."""
import os
import signal
import socket
import time

from .db import Heartbeat, Session, Task, init, now
from .recon import add_sample, metric, run

stopping = False
def stop(*_):
    global stopping
    stopping = True
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


def beat(processed_delta: int = 0):
    with Session() as s:
        h = s.get(Heartbeat, "worker") or Heartbeat(name="worker", processed=0)
        h.beat_at, h.host, h.pid = now(), socket.gethostname(), os.getpid()
        h.processed = (h.processed or 0) + processed_delta
        s.merge(h)
        s.commit()


def next_task():
    with Session() as s:
        t = s.query(Task).filter(Task.status == "queued").order_by(Task.id).with_for_update(skip_locked=True).first() \
            if not s.bind.url.drivername.startswith("sqlite") else s.query(Task).filter(Task.status == "queued").order_by(Task.id).first()
        if not t:
            return None
        t.status, t.started_at = "running", now()
        s.commit()
        return t.id


def counts():
    with Session() as s:
        return {"tasks_done": s.query(Task).filter(Task.status == "done").count(), "tasks_failed": s.query(Task).filter(Task.status == "failed").count(),
                "queued": s.query(Task).filter(Task.status == "queued").count()}


def main():
    init()
    print(f"worker started (pid {os.getpid()})", flush=True)
    idle_reported = 0.0
    while not stopping:
        beat()
        task_id = next_task()
        if not task_id and time.monotonic() - idle_reported > 60:   # idle: what it has done so far, once a minute
            metric(label="Waiting for tasks", **counts()); idle_reported = time.monotonic()
        if task_id:
            try:
                add_sample(30)   # a fresh batch of payments, so each queued recon has something new to check
                run("worker", label=f"Recon task #{task_id}", extra=counts())
                status, err = "done", None
            except Exception as e:  # noqa: BLE001: record it and keep going
                status, err = "failed", str(e)[:500]
            with Session() as s:
                t = s.get(Task, task_id)
                t.status, t.finished_at, t.error = status, now(), err
                s.commit()
            beat(1)
            metric(label=f"Recon task #{task_id} {status}", **counts()); idle_reported = time.monotonic()
            continue
        for _ in range(10):  # 5 seconds, in small steps so a stop is quick
            if stopping:
                break
            time.sleep(0.5)
    print("worker stopping", flush=True)



if __name__ == "__main__":
    main()
