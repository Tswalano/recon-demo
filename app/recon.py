"""The reconciliation itself: compare the ledger with the bank statement and record what doesn't match."""
import json
import random
import socket
import string
import time
from decimal import Decimal

from .db import ReconItem, ReconRun, Session, Transaction, now


def add_sample(n: int = 40) -> int:
    """A day of payments: both sides agree on most; a few amounts differ, a few are missing on one side."""
    with Session() as s:
        for _ in range(n):
            ref = "PAY-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
            amount = Decimal(random.randint(1000, 250000)) / 100
            roll = random.random()
            if roll > 0.06:
                s.add(Transaction(reference=ref, source="ledger", amount=amount))
            if roll < 0.94 or roll > 0.97:
                bank = amount if random.random() > 0.08 else amount + Decimal(random.choice([-50, -10, 5, 20])) 
                s.add(Transaction(reference=ref, source="bank", amount=bank))
        s.commit()
    return n


def metric(**values):
    """One `ops-metric {json}` line: the Ops Console shows the latest on the Workers & jobs tab (processed/total as a bar)."""
    print("ops-metric " + json.dumps(values), flush=True)


def run(trigger: str, label: str | None = None, extra: dict | None = None) -> ReconRun:
    """Reconcile every payment, reporting progress as it goes (label: what the console shows, e.g. "Recon task #7")."""
    started = time.monotonic()
    with Session() as s:
        r = ReconRun(trigger=trigger, host=socket.gethostname())
        s.add(r)
        s.flush()
        ledger, bank = {}, {}
        for t in s.query(Transaction).all():
            (ledger if t.source == "ledger" else bank)[t.reference] = t.amount
        refs = sorted(set(ledger) | set(bank))
        label = label or f"Recon run #{r.id} ({trigger})"
        for i, ref in enumerate(refs, 1):
            if i == 1 or i % 10 == 0 or i == len(refs):
                metric(label=label, processed=i, total=len(refs), **(extra or {}))
            time.sleep(0.04)   # a little time per payment, so the progress can be watched
            a, b = ledger.get(ref), bank.get(ref)
            if a is not None and b is not None and a == b:
                r.matched += 1
                continue
            problem = "missing_in_bank" if b is None else "missing_in_ledger" if a is None else "amount_differs"
            if problem == "amount_differs":
                r.mismatched += 1
            else:
                r.missing += 1
            s.add(ReconItem(run_id=r.id, reference=ref, problem=problem, ledger_amount=a, bank_amount=b))
        r.finished_at = now()
        s.commit()
        print(f"recon #{r.id} ({trigger}): {r.matched} matched, {r.mismatched} amounts differ, {r.missing} missing, {time.monotonic() - started:.1f}s", flush=True)
        return r
