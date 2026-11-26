"""The scheduled recon (Ops Console: Workers & jobs → scheduled job, command `python -m app.jobs.recon`).
Reconciles the day's payments and exits; its runs show as "schedule" on the web page."""
from ..db import init
from ..recon import add_sample, metric, run

if __name__ == "__main__":
    init()
    add_sample(40)
    r = run("schedule", label="Scheduled recon")
    metric(label=f"Scheduled recon #{r.id} done", matched=r.matched, amounts_differ=r.mismatched, missing=r.missing)
