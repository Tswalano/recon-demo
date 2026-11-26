"""The Redis worker (Ops Console: Workers & jobs → worker, command `python -m app.redis_worker`; set the app's Redis
first, on the Workers & jobs tab: Valkey on the server or an external Redis such as Upstash).

Always running. It waits on the Redis queue (app.redisq) and reconciles each statement date it's given, like the
database worker does for its own queue. Runs show as "redis" on the page. On start it puts back what a stopped or
killed worker was on; stopped part-way (a deploy, Stop), it puts that task back first in line. If Redis can't be
reached it says so and tries again, rather than exiting.
"""
import json
import signal
import time
from datetime import date

from . import redisq
from .db import init
from .files import Stopped
from .recon import interrupt_stale, metric, run

stopping = False
def stop(*_):
    global stopping
    stopping = True
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
should_stop = lambda: stopping


def handle_one(r, timeout: int = redisq.BLOCK) -> bool:
    """Wait for one task and reconcile it; False when none came within the timeout."""
    raw = redisq.take(r, timeout)
    if raw is None:
        return False
    try:
        task = json.loads(raw)
        day = date.fromisoformat(task["date"])
    except (ValueError, KeyError, TypeError) as e:
        print(f"redis task failed: not a recon task ({e}): {raw[:200]}", flush=True)
        redisq.finish(r, raw, {"id": None, "date": None, "status": "failed", "error": "not a recon task", "at": time.time()})
        return True
    started = time.time()
    try:
        rr = run("redis", day, label=f"Redis task {task.get('id')} ({day})", extra=redisq.counts(r), should_stop=should_stop)
        result = {"id": task.get("id"), "date": day.isoformat(), "status": "done", "run": rr.id, "matched": rr.matched,
                  "mismatched": rr.mismatched, "missing": rr.missing, "seconds": round(time.time() - started, 1), "at": time.time()}
    except Stopped:
        redisq.put_back(r, raw)
        print(f"stopped during redis task {task.get('id')} ({day}): it went back to the queue", flush=True)
        return True
    except Exception as e:  # noqa: BLE001: record it and keep going
        print(f"redis task {task.get('id')} ({day}) failed: {type(e).__name__}: {e}", flush=True)
        result = {"id": task.get("id"), "date": day.isoformat(), "status": "failed", "error": f"{type(e).__name__}: {e}"[:300], "at": time.time()}
    redisq.finish(r, raw, result)
    metric(label=f"Redis task {task.get('id')} {result['status']}", **redisq.counts(r))
    return True


def main():
    init()
    interrupt_stale("redis")
    r = redisq.client()
    while r is None and not stopping:   # stays up (not restarted over and over), and says what's missing once a minute
        print("error: REDIS_URL isn't set: set the app's Redis on the Ops Console's Workers & jobs tab", flush=True)
        metric(label="Redis worker: REDIS_URL not set")
        for _ in range(120):
            if stopping:
                return
            time.sleep(0.5)
        r = redisq.client()
    print(f"redis worker started, waiting on {redisq.QUEUE} at {redisq.where()}", flush=True)
    import redis
    recovered, idle_reported, beaten, backoff = False, 0.0, 0.0, 5
    while not stopping:
        try:
            if not recovered:
                if n := redisq.recover(r):
                    print(f"recovered {n} redis task(s): back in the queue", flush=True)
                recovered = True
            if time.monotonic() - beaten > 45:   # about once a minute: Upstash counts every command
                redisq.beat(r); beaten = time.monotonic()
            if handle_one(r):
                idle_reported = time.monotonic()
            elif time.monotonic() - idle_reported > 300:   # idle: what it has done so far, every 5 minutes
                metric(label="Redis worker: waiting for tasks", **redisq.counts(r)); idle_reported = time.monotonic()
            backoff = 5
        except (redis.ConnectionError, redis.TimeoutError) as e:
            print(f"redis connection error: {e} (trying again in {backoff}s)", flush=True)
            metric(label="Redis worker: can't reach Redis")
            for _ in range(backoff * 2):
                if stopping:
                    break
                time.sleep(0.5)
            backoff = min(backoff * 2, 60)
    print("redis worker stopping", flush=True)


if __name__ == "__main__":
    main()
