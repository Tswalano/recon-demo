"""The scheduled recon through Redis (Ops Console: Workers & jobs → scheduled job, command `python -m app.jobs.redis_recon`).

Finds the statement dates with files imported since their last recon (as app.jobs.recon does), but instead of
reconciling them itself it puts each on the Redis queue for the Redis worker, then exits: the schedule → queue →
worker pattern. A date already waiting in the queue isn't added twice. Exits 1 (a failed run in the Ops Console)
when Redis isn't set or can't be reached.
"""
import sys

from .. import redisq
from ..db import init
from ..recon import dates_to_reconcile, metric

if __name__ == "__main__":
    init()
    r = redisq.client()
    if r is None:
        print("error: REDIS_URL isn't set: set the app's Redis on the Ops Console's Workers & jobs tab", flush=True)
        metric(label="Redis recon: REDIS_URL not set")
        sys.exit(1)
    try:
        waiting = redisq.queued_dates(r)
        days = [d for d in dates_to_reconcile() if d.isoformat() not in waiting]
        for d in days:
            redisq.enqueue(r, d, "scheduled (redis_recon)")
        c = redisq.counts(r)
    except Exception as e:  # noqa: BLE001: Redis down or refusing: a failed run, with the reason in its output
        print(f"redis recon failed: {type(e).__name__}: {e}", flush=True)
        metric(label="Redis recon: can't reach Redis")
        sys.exit(1)
    print(f"redis recon: queued {len(days)} date(s) for the redis worker" + (f": {', '.join(map(str, days))}" if days else ""), flush=True)
    metric(label=f"Redis recon: queued {len(days)} date(s)", queued_now=len(days), **c)
    sys.exit(0)
