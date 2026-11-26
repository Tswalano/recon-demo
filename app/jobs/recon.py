"""The scheduled recon (Ops Console: Workers & jobs → scheduled job, command `python -m app.jobs.recon`).

Reconciles every statement date that has files imported since its last recon, then exits. A date with only one
side's file is reconciled too: the run says which file never arrived (everything shows as missing on that side).
Its runs show as "schedule" on the web page.
"""
import sys

from ..db import init
from ..recon import dates_to_reconcile, interrupt_stale, metric, run

if __name__ == "__main__":
    init()
    interrupt_stale("schedule")
    days = dates_to_reconcile()
    if not days:
        print("scheduled recon: nothing new to reconcile", flush=True)
        metric(label="Scheduled recon: nothing new", dates=0)
        sys.exit(0)
    notes, failed = [], 0
    for n, day in enumerate(days, 1):
        try:
            r = run("schedule", day, label=f"Scheduled recon {n}/{len(days)}: {day}")
            if r.note:
                notes.append(r.note)
        except Exception as e:  # noqa: BLE001: one bad date shouldn't stop the others
            failed += 1
            print(f"scheduled recon of {day} failed: {e}", flush=True)
    metric(label=f"Scheduled recon done: {len(days)} date(s)", dates=len(days), failed=failed,
           missing_files="; ".join(notes) or "none")
    sys.exit(1 if failed else 0)   # a failure shows as a failed run in the Ops Console
