"""A recon queue in Redis (REDIS_URL): Valkey on the server, or an external Redis such as Upstash. It sits next to the
database queue (app.worker), so the same recon can be tried both ways.

    queue       recons waiting (a list: the page and the scheduled job push, the Redis worker takes)
    processing  the one the worker is on: a task moves here as it's taken, in one command, so a worker stopped or
                killed part-way leaves it here, and the next start puts it back in the queue (nothing is lost)
    done/failed how many finished each way; recent: the last 20 results; worker:beat: set while the worker is up

Upstash counts every command, so the worker waits on the queue (one blocking command, up to REDIS_BLOCK_SECONDS)
instead of checking it every few seconds, and the page's figures are cached for a few seconds.
"""
import json
import os
import time
import uuid
from datetime import date

PREFIX = os.environ.get("REDIS_PREFIX", "recon-demo")
QUEUE, PROCESSING, RECENT = f"{PREFIX}:queue", f"{PREFIX}:processing", f"{PREFIX}:recent"
DONE, FAILED, BEAT = f"{PREFIX}:count:done", f"{PREFIX}:count:failed", f"{PREFIX}:worker:beat"
BLOCK = max(1, min(int(os.environ.get("REDIS_BLOCK_SECONDS", "20")), 60))


def client():
    """A Redis client for REDIS_URL, or None when it isn't set (redis:// or rediss:// for TLS, as Upstash gives)."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        return None
    import redis
    # the socket waits longer than the worker's blocking wait, or every wait would end in a timeout error
    return redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=10, socket_timeout=BLOCK + 15,
                                health_check_interval=30, retry_on_timeout=True)


def where() -> str:
    """Which Redis, without its password: e.g. 'rediss://your-db.upstash.io:6379'."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        return ""
    scheme, _, rest = url.partition("://")
    return f"{scheme}://{rest.rpartition('@')[2]}"


def enqueue(r, day: date, reason: str) -> str:
    task_id = uuid.uuid4().hex[:8]
    r.lpush(QUEUE, json.dumps({"id": task_id, "date": day.isoformat(), "reason": reason, "queuedAt": time.time()}))
    return task_id


def queued_dates(r) -> set[str]:
    return {json.loads(x).get("date") for x in r.lrange(QUEUE, 0, -1) + r.lrange(PROCESSING, 0, -1)}


def take(r, timeout: int = BLOCK) -> str | None:
    """The next task, moved to processing in the same command (waits up to timeout seconds for one)."""
    import redis
    try:
        return r.blmove(QUEUE, PROCESSING, timeout, "RIGHT", "LEFT")
    except redis.ResponseError:   # a Redis older than 6.2: the older command does the same
        return r.brpoplpush(QUEUE, PROCESSING, timeout)


def finish(r, raw: str, result: dict):
    """Done with a task: off processing, counted, and kept in the recent results."""
    p = r.pipeline()
    p.lrem(PROCESSING, 1, raw)
    p.incr(DONE if result.get("status") == "done" else FAILED)
    p.lpush(RECENT, json.dumps(result))
    p.ltrim(RECENT, 0, 19)
    p.execute()


def put_back(r, raw: str):
    """A task the worker didn't finish (it was stopped): first in line again."""
    p = r.pipeline()
    p.lrem(PROCESSING, 1, raw)
    p.rpush(QUEUE, raw)
    p.execute()


def recover(r) -> int:
    """On start: tasks a stopped or killed worker left in processing go back in the queue (one Redis worker assumed)."""
    n = 0
    while r.lmove(PROCESSING, QUEUE, "LEFT", "RIGHT") is not None:
        n += 1
    return n


def beat(r):
    r.set(BEAT, str(int(time.time())), ex=BLOCK + 90)   # set about once a minute (see the worker)


def counts(r) -> dict:
    p = r.pipeline()
    p.llen(QUEUE); p.llen(PROCESSING); p.get(DONE); p.get(FAILED)
    q, running, done, failed = p.execute()
    return {"redis_queued": q, "redis_running": running, "redis_done": int(done or 0), "redis_failed": int(failed or 0)}


_cache: tuple[float, dict] | None = None
def status() -> dict:
    """What the page shows (cached for 5 seconds: the page refreshes every 3, and Upstash counts commands)."""
    global _cache
    if _cache and time.monotonic() - _cache[0] < 5:
        return _cache[1]
    r = client()
    if r is None:
        out = {"configured": False}
    else:
        try:
            p = r.pipeline()
            p.exists(BEAT); p.lrange(RECENT, 0, 4)
            alive, recent = p.execute()
            out = {"configured": True, "where": where(), "workerAlive": bool(alive), **counts(r), "recent": [json.loads(x) for x in recent]}
        except Exception as e:  # noqa: BLE001: the page says it couldn't reach Redis, and why
            out = {"configured": True, "where": where(), "error": f"{type(e).__name__}: {e}"[:300]}
    _cache = (time.monotonic(), out)
    return out
