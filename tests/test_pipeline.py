"""The whole path a file takes: inbox → import (and rejects) → recon, plus duplicates, bad files, a stop part-way and
recovery after a crash. Run: pip install pytest httpx && pytest"""
import os
import shutil
import sys
import time
from datetime import date
from pathlib import Path

import pytest

TMP = Path(os.environ.setdefault("RECON_TEST_DIR", f"/tmp/recon-test-{os.getpid()}"))
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)
os.environ.update(DATABASE_URL=f"sqlite:///{TMP}/test.db", FILES_DIR=str(TMP / "files"), ROW_DELAY_MS="0")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import files, worker  # noqa: E402
from app.db import ImportFile, Payment, ReconRun, Session, Task, init  # noqa: E402
from app.recon import dates_to_reconcile, run  # noqa: E402
from app.samples import write_demo_set  # noqa: E402

SAMPLES = TMP / "samples"
init()
files.ensure_dirs()
write_demo_set(SAMPLES)


def drop(name: str, dest: str | None = None) -> Path:
    """A file arriving in the inbox, finished arriving a while ago (so the worker takes it straight away)."""
    p = files.INBOX / (dest or name)
    shutil.copy(SAMPLES / name, p)
    old = time.time() - 10
    os.utime(p, (old, old))
    return p


def import_next() -> ImportFile:
    inbox = worker.Inbox()
    inbox.ready()                      # the first look only notes the size
    assert worker.handle_file(inbox)   # the second takes it
    with Session() as s:
        return s.query(ImportFile).order_by(ImportFile.id.desc()).first()


def test_clean_pair_is_imported_and_reconciled():
    drop("ledger_2026-11-26.csv")
    ledger = import_next()
    assert ledger.status == "done" and ledger.rows_rejected == 0 and ledger.rows_ok == ledger.rows_total > 0
    assert (files.FILES_DIR / ledger.stored_as).exists() and not any(files.INBOX.iterdir())
    with Session() as s:
        assert not s.query(Task).filter(Task.status == "queued").count()   # only one side so far
    drop("bank_2026-11-26.csv")
    bank = import_next()
    assert bank.status == "done"
    with Session() as s:
        assert s.query(Task).filter(Task.status == "queued", Task.statement_date == date(2026, 11, 26)).count() == 1
    assert worker.handle_task()
    with Session() as s:
        r = s.query(ReconRun).order_by(ReconRun.id.desc()).first()
    assert r.status == "done" and r.trigger == "worker" and r.matched > 300 and r.mismatched > 0 and r.missing > 0 and r.note is None
    assert r.matched + r.mismatched + r.missing == r.total


def test_bad_lines_are_rejected_with_reasons():
    drop("ledger_2026-11-27.csv")
    import_next()
    drop("bank_2026-11-27_with-errors.csv")
    f = import_next()
    assert f.status == "done" and f.rows_rejected == 5
    with Session() as s:
        from app.db import ImportReject
        reasons = [r.reason for r in s.query(ImportReject).filter(ImportReject.file_id == f.id).order_by(ImportReject.line)]
    assert any("not a number" in x for x in reasons)
    assert any("this is the statement for 2026-11-27" in x for x in reasons)
    assert any("repeats line" in x for x in reasons)
    assert any(x == "no reference" for x in reasons)
    assert any("columns" in x for x in reasons)


def test_same_file_again_is_a_duplicate():
    drop("bank_2026-11-26.csv")
    f = import_next()
    assert f.status == "duplicate" and "already imported" in f.error and f.stored_as.startswith("failed/")


def test_same_references_in_another_file_are_rejected():
    drop("ledger_2026-11-26.csv", "ledger_2026-11-26_resent.csv")
    f = import_next()   # same content, new name
    assert f.status == "duplicate"
    p = files.INBOX / "ledger_2026-11-26_edited.csv"
    p.write_text((SAMPLES / "ledger_2026-11-26.csv").read_text() + "NEW-1,2026-11-26,10.00,x\n")
    os.utime(p, (time.time() - 10,) * 2)
    f = import_next()
    assert f.status == "done" and f.rows_ok == 1 and f.rows_rejected == f.rows_total - 1


@pytest.mark.parametrize("name, error", [("bank_2026-11-28_corrupt.csv", "UTF-8"), ("bank_2026-11-28_wrong-columns.csv", "header"),
                                         ("statement.csv", "name must be")])
def test_files_that_cannot_be_imported_fail(name, error):
    drop(name)
    f = import_next()
    assert f.status == "failed" and error in f.error and f.stored_as.startswith("failed/")


def test_a_file_still_arriving_is_not_taken():
    p = files.INBOX / "bank_2026-11-29.csv"
    p.write_text("reference,date,amount\n")
    inbox = worker.Inbox()
    assert inbox.ready() is None and inbox.ready() is None    # changed less than 2 seconds ago
    (files.INBOX / ".bank_2026-11-29.csv.part").write_text("x")   # an upload in progress: ignored
    assert not inbox.changed()
    (files.INBOX / "ledger_2026-11-29.csv").write_text("x")         # something new: stop waiting
    assert inbox.changed()
    (files.INBOX / "ledger_2026-11-29.csv").unlink()
    p.unlink(); (files.INBOX / ".bank_2026-11-29.csv.part").unlink()


def test_stopped_part_way_the_file_goes_back_and_nothing_is_saved():
    drop("ledger_2026-11-25_big.csv")
    with Session() as s:
        before = s.query(Payment).count()
    calls = {"n": 0}
    def stop_soon():
        calls["n"] += 1
        return calls["n"] > 100
    worker.should_stop = stop_soon
    try:
        inbox = worker.Inbox(); inbox.ready()
        assert worker.handle_file(inbox)
    finally:
        worker.should_stop = lambda: False
    assert (files.INBOX / "ledger_2026-11-25_big.csv").exists() and not any(files.PROCESSING.iterdir())
    with Session() as s:
        assert s.query(Payment).count() == before
        assert s.query(ImportFile).order_by(ImportFile.id.desc()).first().status == "interrupted"
    f = import_next()   # and it imports fine next time
    assert f.status == "done" and f.rows_ok == 4878


def test_recovery_after_a_crash_mid_import():
    p = drop("ledger_2026-11-27.csv", "ledger_2026-11-30.csv")
    p.write_text(p.read_text().replace("2026-11-27", "2026-11-30"))
    claimed = files.claim(p)
    with Session() as s:   # as if the worker died here
        s.add(ImportFile(name="ledger_2026-11-30.csv", status="processing")); s.commit()
    assert files.recover() == ["ledger_2026-11-30.csv"] and not claimed.exists()
    with Session() as s:
        assert not s.query(ImportFile).filter(ImportFile.status == "processing").count()


def test_scheduled_job_reconciles_what_is_new_and_names_the_missing_file():
    days = dates_to_reconcile()
    assert date(2026, 11, 25) in days and date(2026, 11, 26) in days   # 26th: a new ledger file came after its recon
    r = run("schedule", date(2026, 11, 25))
    assert r.note == "No bank file received for 2026-11-25" and r.missing == r.total == 4878
    assert date(2026, 11, 25) not in dates_to_reconcile()


def test_web_upload_and_pages():
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    res = c.post("/api/upload", files=[("files", ("bank_2026-12-01.csv", b"reference,date,amount\nA1,2026-12-01,5.00\n", "text/csv"))])
    assert res.status_code == 200 and res.json()["saved"] == ["bank_2026-12-01.csv"]
    assert (files.INBOX / "bank_2026-12-01.csv").exists()
    assert c.post("/api/upload", files=[("files", ("../evil.sh", b"x", "text/plain"))]).status_code == 400
    st = c.get("/api/status").json()
    assert st["inbox"] and st["files"] and st["runs"] and st["runs"][0]["startedAt"].endswith("+00:00")
    assert c.get(f"/api/files/{st['files'][0]['id']}").status_code == 200
    d = c.get(f"/api/runs/{st['runs'][0]['id']}").json()
    assert d["files"] and "problems" in d
    assert c.post("/api/run-now").json()["run"]
