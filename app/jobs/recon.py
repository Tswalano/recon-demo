"""The scheduled recon (Ops Console: Workers & jobs → scheduled job, command `python -m app.jobs.recon`).
Reconciles the day's payments and exits; its runs show as "schedule" on the web page."""
from ..db import init
from ..recon import add_sample, run

if __name__ == "__main__":
    init()
    add_sample(40)
    run("schedule")
