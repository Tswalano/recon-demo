"""The reconciliation itself: for one statement date, compare the ledger's payments with the bank's and record what
doesn't match. The comparison writes nothing until it's done, then saves the result in one short transaction."""
import json
import socket
import time
from datetime import date
from typing import Callable

from sqlalchemy import func

from .db import ImportFile, Payment, ReconItem, ReconRun, Session, now, utc
from .files import ROW_DELAY, Stopped


def metric(**values):
    """One `ops-metric {json}` line: the Ops Console shows the latest on the Workers & jobs tab (processed/total as a bar)."""
    print("ops-metric " + json.dumps(values, default=str), flush=True)


def latest_date() -> date | None:
    """The newest statement date any file was imported for (what the page's buttons reconcile)."""
    with Session() as s:
        return s.query(func.max(Payment.statement_date)).scalar()


def dates_to_reconcile() -> list[date]:
    """Statement dates with files imported since their last finished recon (or never reconciled)."""
    with Session() as s:
        imported = s.query(ImportFile.statement_date, func.max(ImportFile.finished_at)).filter(
            ImportFile.status == "done").group_by(ImportFile.statement_date).all()
        reconciled = dict(s.query(ReconRun.statement_date, func.max(ReconRun.started_at)).filter(
            ReconRun.status == "done").group_by(ReconRun.statement_date).all())
    return sorted(d for d, last_file in imported if d not in reconciled or utc(last_file) > utc(reconciled[d]))


def sides(day: date) -> set[str]:
    with Session() as s:
        return {src for (src,) in s.query(ImportFile.source).filter(ImportFile.statement_date == day, ImportFile.status == "done").distinct()}


def interrupt_stale(trigger: str):
    """Runs of this kind left 'running' by a process that was stopped or crashed (on start, one process of each kind)."""
    with Session() as s:
        for r in s.query(ReconRun).filter(ReconRun.trigger == trigger, ReconRun.status == "running", ReconRun.host == socket.gethostname()):
            r.status, r.finished_at, r.error = "interrupted", now(), "The process stopped during the run."
        s.commit()


def run(trigger: str, day: date, label: str | None = None, extra: dict | None = None, *,
        tick: Callable[[], None] = lambda: None, should_stop: Callable[[], bool] = lambda: False) -> ReconRun:
    """Reconcile one statement date, reporting progress as it goes (label: what the console shows, e.g. "Recon task #7")."""
    started = time.monotonic()
    with Session() as s:
        r = ReconRun(trigger=trigger, statement_date=day, status="running", host=socket.gethostname())
        s.add(r)
        s.commit()
        ledger, bank = {}, {}
        for ref, src, amount in s.query(Payment.reference, Payment.source, Payment.amount).filter(Payment.statement_date == day):
            (ledger if src == "ledger" else bank)[ref] = amount
    try:
        refs = sorted(set(ledger) | set(bank))
        label = label or f"Recon #{r.id} of {day} ({trigger})"
        matched, items, last = 0, [], 0.0
        for i, ref in enumerate(refs, 1):
            if should_stop():
                raise Stopped()
            if i == 1 or i == len(refs) or time.monotonic() - last >= 1:
                metric(label=label, processed=i, total=len(refs), **(extra or {}))
                last = time.monotonic()
            tick()
            if ROW_DELAY:
                time.sleep(ROW_DELAY)
            a, b = ledger.get(ref), bank.get(ref)
            if a is not None and b is not None and a == b:
                matched += 1
                continue
            problem = "missing_in_bank" if b is None else "missing_in_ledger" if a is None else "amount_differs"
            items.append(ReconItem(run_id=r.id, reference=ref, problem=problem, ledger_amount=a, bank_amount=b))
        missing_side = [n for n, side in (("ledger", ledger), ("bank", bank)) if not side]
        with Session() as s:
            s.add_all(items)
            row = s.get(ReconRun, r.id)
            row.status, row.finished_at, row.total, row.matched = "done", now(), len(refs), matched
            row.mismatched = sum(1 for x in items if x.problem == "amount_differs")
            row.missing = len(items) - row.mismatched
            row.note = f"No {' or '.join(missing_side)} file received for {day}" if missing_side else None
            s.commit()
    except BaseException as e:
        with Session() as s:
            row = s.get(ReconRun, r.id)
            row.status = "interrupted" if isinstance(e, (Stopped, KeyboardInterrupt)) else "failed"
            row.finished_at, row.error = now(), (str(e) or type(e).__name__)[:500]
            s.commit()
        raise
    print(f"recon #{row.id} of {day} ({trigger}): {row.matched} matched, {row.mismatched} amounts differ, {row.missing} missing"
          f"{' - ' + row.note if row.note else ''}, {time.monotonic() - started:.1f}s", flush=True)
    return row
