"""The reconciliation itself: compare the ledger with the bank statement and record what doesn't match."""
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


def run(trigger: str) -> ReconRun:
    started = time.monotonic()
    with Session() as s:
        r = ReconRun(trigger=trigger, host=socket.gethostname())
        s.add(r)
        s.flush()
        ledger, bank = {}, {}
        for t in s.query(Transaction).all():
            (ledger if t.source == "ledger" else bank)[t.reference] = t.amount
        for ref in sorted(set(ledger) | set(bank)):
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
        time.sleep(min(3.0, 0.5 + len(ledger) / 400))  # so a run takes visibly long enough to watch
        r.finished_at = now()
        s.commit()
        print(f"recon #{r.id} ({trigger}): {r.matched} matched, {r.mismatched} amounts differ, {r.missing} missing, {time.monotonic() - started:.1f}s", flush=True)
        return r
