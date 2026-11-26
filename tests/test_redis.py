"""The Redis queue: page → Redis → Redis worker, the scheduled job queueing on Redis, a stop part-way and recovery.
A fake Redis in memory stands in for Valkey or Upstash. Run: pip install pytest httpx fakeredis && pytest"""
import json
import os
import runpy
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import fakeredis
import pytest

TMP = Path(os.environ.setdefault("RECON_TEST_DIR", f"/tmp/recon-test-{os.getpid()}"))
TMP.mkdir(parents=True, exist_ok=True)
for k, v in (("DATABASE_URL", f"sqlite:///{TMP}/test.db"), ("FILES_DIR", str(TMP / "files")), ("ROW_DELAY_MS", "0")):
    os.environ.setdefault(k, v)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import redis_worker, redisq  # noqa: E402
from app.db import ImportFile, Payment, ReconRun, Session, init, now  # noqa: E402

init()
DAY = date(2031, 3, 14)   # a date of its own, apart from the other tests' files


@pytest.fixture()
def r(monkeypatch):
    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redisq, "client", lambda: fake)
    monkeypatch.setattr(redisq, "_cache", None)
    monkeypatch.setenv("REDIS_URL", "rediss://default:secret@example.upstash.io:6379")
    return fake


def payments_for(day: date):
    """Both sides for a day: 3 match, 1 amount differs, 1 only in the ledger (imported files, as the worker leaves them)."""
    with Session() as s:
        if s.query(Payment).filter(Payment.statement_date == day).count():
            return
        for src in ("ledger", "bank"):
            f = ImportFile(name=f"{src}_{day}.csv", source=src, statement_date=day, status="done", finished_at=now())
            s.add(f); s.flush()
            rows = {"R1": "10.00", "R2": "20.00", "R3": "30.00", "R4": "40.00" if src == "ledger" else "41.00"}
            if src == "ledger":
                rows["R5"] = "50.00"
            s.add_all(Payment(file_id=f.id, source=src, statement_date=day, reference=f"{day}-{k}", amount=Decimal(v)) for k, v in rows.items())
        s.commit()


def test_a_queued_recon_is_done_by_the_redis_worker(r):
    payments_for(DAY)
    task_id = redisq.enqueue(r, DAY, "test")
    assert redisq.counts(r)["redis_queued"] == 1
    assert redis_worker.handle_one(r, timeout=1)
    c = redisq.counts(r)
    assert (c["redis_queued"], c["redis_running"], c["redis_done"], c["redis_failed"]) == (0, 0, 1, 0)
    last = json.loads(r.lindex(redisq.RECENT, 0))
    assert (last["id"], last["status"], last["matched"], last["mismatched"], last["missing"]) == (task_id, "done", 3, 1, 1)
    with Session() as s:
        run = s.get(ReconRun, last["run"])
        assert (run.trigger, run.status, run.statement_date) == ("redis", "done", DAY)
    assert not redis_worker.handle_one(r, timeout=1)   # nothing else waiting


def test_a_bad_task_is_counted_as_failed_and_the_worker_goes_on(r):
    r.lpush(redisq.QUEUE, "not json")
    assert redis_worker.handle_one(r, timeout=1)
    assert redisq.counts(r)["redis_failed"] == 1 and redisq.counts(r)["redis_running"] == 0


def test_stopped_part_way_the_task_goes_back_first_in_line(r, monkeypatch):
    payments_for(DAY)
    redisq.enqueue(r, DAY, "first"); redisq.enqueue(r, DAY, "second")
    monkeypatch.setattr(redis_worker, "should_stop", lambda: True)
    assert redis_worker.handle_one(r, timeout=1)
    assert redisq.counts(r)["redis_queued"] == 2 and redisq.counts(r)["redis_running"] == 0
    assert json.loads(redisq.take(r, 1))["reason"] == "first"   # still first in line


def test_a_killed_worker_leaves_its_task_in_processing_and_the_next_start_puts_it_back(r):
    redisq.enqueue(r, DAY, "test")
    assert redisq.take(r, 1)   # taken, then the process dies
    assert redisq.counts(r)["redis_running"] == 1
    assert redisq.recover(r) == 1
    assert (redisq.counts(r)["redis_queued"], redisq.counts(r)["redis_running"]) == (1, 0)


def test_scheduled_job_queues_new_dates_once(r, capsys):
    payments_for(DAY)
    with Session() as s:   # DAY counts as new: no finished recon after its files
        s.query(ReconRun).filter(ReconRun.statement_date == DAY).delete(); s.commit()
    for _ in range(2):   # the second run finds the date already waiting: not added again
        with pytest.raises(SystemExit) as e:
            runpy.run_module("app.jobs.redis_recon", run_name="__main__")
        assert e.value.code == 0
    assert DAY.isoformat() in redisq.queued_dates(r)
    assert [json.loads(x)["date"] for x in r.lrange(redisq.QUEUE, 0, -1)].count(DAY.isoformat()) == 1
    assert "queued 0 date(s)" in capsys.readouterr().out


def test_scheduled_job_fails_without_redis(monkeypatch):
    monkeypatch.setattr(redisq, "client", lambda: None)
    with pytest.raises(SystemExit) as e:
        runpy.run_module("app.jobs.redis_recon", run_name="__main__")
    assert e.value.code == 1


def test_page_status_and_queue_button(r):
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    payments_for(DAY)
    st = c.get("/api/status").json()["redis"]
    assert st["configured"] and st["where"] == "rediss://example.upstash.io:6379"   # never the password
    q = c.post(f"/api/redis/queue?date={DAY}")
    assert q.status_code == 200 and q.json()["date"] == DAY.isoformat()
    assert redisq.counts(r)["redis_queued"] >= 1


def test_queue_button_without_redis_says_what_to_do(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(redisq, "client", lambda: None)
    q = TestClient(app).post(f"/api/redis/queue?date={DAY}")
    assert q.status_code == 409 and "REDIS_URL" in q.json()["detail"]
