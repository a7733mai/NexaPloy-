import threading

import pytest

from nexaploy_agent.jobs import registry
from nexaploy_agent.jobs.base import BaseParams, Job
from nexaploy_agent.jobs.store import JobNotFound, JobStore, MAX_OUTPUT
from nexaploy_agent.jobs.worker import Worker
from nexaploy_agent.redact import MASK


# ---- store ---------------------------------------------------------------------------------
def test_store_basics(tmp_path):
    s = JobStore(tmp_path / "data" / "jobs.db")
    assert s._conn().execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    j = s.create("bench.start", {"bench": "abc"}, ["start"], origin="control_plane", remote_id="AJ-1")
    assert j["status"] == "queued" and j["origin"] == "control_plane" and j["remote_id"] == "AJ-1"
    assert [st["status"] for st in j["steps"]] == ["pending"]
    assert s.find_remote("AJ-1")["id"] == j["id"]
    with pytest.raises(Exception):  # unique (origin, remote_id)
        s.create("bench.start", {"bench": "abc"}, ["start"], origin="control_plane", remote_id="AJ-1")
    with pytest.raises(JobNotFound):
        s.get("nope")


def test_claim_oldest_first_and_once(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    ids = [s.create("k", {"i": i}, ["a"])["id"] for i in range(3)]
    first = s.claim_next()
    assert first["id"] == ids[0] and first["status"] == "running" and first["started_at"]
    assert s.claim_next()["id"] == ids[1]


def test_concurrent_claims_never_duplicate(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    for i in range(40):
        s.create("k", {"i": i}, ["a"])
    claimed, lock = [], threading.Lock()

    def grab():
        while (job := s.claim_next()) is not None:
            with lock:
                claimed.append(job["id"])

    threads = [threading.Thread(target=grab) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(claimed) == 40 == len(set(claimed))


def test_output_append_and_tail(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    j = s.create("k", {}, ["a"])
    s.append_output(j["id"], 0, "hello ")
    s.append_output(j["id"], 0, "world")
    assert s.steps(j["id"])[0]["output"] == "hello world"
    s.append_output(j["id"], 0, "x" * (MAX_OUTPUT + 10))
    out = s.steps(j["id"])[0]["output"]
    assert len(out) == MAX_OUTPUT and out.endswith("x")


def test_cancel_only_queued(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    a = s.create("k", {}, ["a", "b"])
    assert s.cancel(a["id"])
    got = s.get(a["id"])
    assert got["status"] == "cancelled" and {st["status"] for st in got["steps"]} == {"skipped"}
    b = s.create("k", {}, ["a"])
    s.claim_next()
    assert not s.cancel(b["id"])


# ---- worker with synthetic jobs ---------------------------------------------------------------
CALLS: list[str] = []


class DemoParams(BaseParams):
    fail_at: str | None = None
    admin_password: str | None = None


class DemoJob(Job):
    kind = "test.demo"
    steps = ("one", "two", "three")
    Params = DemoParams

    def _step(self, name):
        CALLS.append(name)
        self.log(f"running {name} with {self.p.admin_password}")
        self.result.setdefault("done", []).append(name)
        if self.p.fail_at == name:
            self.fail(f"boom in {name} ({self.p.admin_password})")

    def step_one(self):
        self._step("one")

    def step_two(self):
        self._step("two")

    def step_three(self):
        self._step("three")


class CrashJob(DemoJob):
    kind = "test.crash"
    steps = ("one",)

    def step_one(self):
        raise ZeroDivisionError("unexpected")


@pytest.fixture(autouse=True)
def demo_registry(monkeypatch):
    CALLS.clear()
    monkeypatch.setitem(registry.REGISTRY, DemoJob.kind, DemoJob)
    monkeypatch.setitem(registry.REGISTRY, CrashJob.kind, CrashJob)


def test_worker_success_and_on_change(ctx):
    changes = []
    w = Worker(ctx, on_change=changes.append)
    job = ctx.enqueue("test.demo", {"admin_password": "hunter22"})
    assert w.run_once() and not w.run_once()
    got = ctx.store.get(job["id"])
    assert got["status"] == "success" and got["ended_at"]
    assert [s["status"] for s in got["steps"]] == ["success"] * 3
    assert CALLS == ["one", "two", "three"]
    assert got["result"]["done"] == ["one", "two", "three"]
    assert "hunter22" not in got["steps"][0]["output"] and MASK in got["steps"][0]["output"]
    assert got["params"]["admin_password"] == MASK  # scrubbed once final
    assert len(changes) >= 1 + 3 * 2 + 1 and set(changes) == {job["id"]}


def test_worker_failure_skips_remaining(ctx, worker):
    job = ctx.enqueue("test.demo", {"fail_at": "two", "admin_password": "hunter22"})
    worker.run_once()
    got = ctx.store.get(job["id"])
    assert got["status"] == "failure"
    assert [s["status"] for s in got["steps"]] == ["success", "failure", "skipped"]
    assert "boom in two" in got["error"] and "hunter22" not in got["error"]
    assert "ERROR: boom in two" in got["steps"][1]["output"]
    assert CALLS == ["one", "two"]


def test_worker_unexpected_exception_has_traceback(ctx, worker):
    job = ctx.enqueue("test.crash", {})
    worker.run_once()
    got = ctx.store.get(job["id"])
    assert got["status"] == "failure" and got["error"].startswith("ZeroDivisionError")
    assert "Traceback" in got["steps"][0]["output"]


def test_worker_resumes_after_crash(ctx, worker):
    job = ctx.enqueue("test.demo", {})
    # Simulate a crash mid-way: step one done, step two running.
    ctx.store.claim_next()
    ctx.store.start_step(job["id"], 0)
    ctx.store.end_step(job["id"], 0, "success")
    ctx.store.start_step(job["id"], 1)
    ctx.store.set_result(job["id"], {"done": ["one"]})
    assert worker.recover() == [job["id"]]
    assert ctx.store.get(job["id"])["status"] == "queued"
    assert worker.run_once()
    got = ctx.store.get(job["id"])
    assert got["status"] == "success"
    assert CALLS == ["two", "three"]  # step one not re-run
    assert got["result"]["done"] == ["one", "two", "three"]
    assert "agent restarted" in got["steps"][1]["output"]


def test_worker_rejects_unknown_kind(ctx, worker):
    rec = ctx.store.create("test.gone", {}, ["x"])
    worker.run_once()
    got = ctx.store.get(rec["id"])
    assert got["status"] == "failure" and "unknown job kind" in got["error"]


def test_run_forever_stops(ctx):
    w = Worker(ctx, idle_seconds=0.01)
    job = ctx.enqueue("test.demo", {})
    stop = threading.Event()
    t = threading.Thread(target=w.run_forever, args=(stop,))
    t.start()
    for _ in range(200):
        if ctx.store.get(job["id"], with_steps=False)["status"] == "success":
            break
        threading.Event().wait(0.01)
    stop.set()
    ctx.wake.set()
    t.join(2)
    assert not t.is_alive()
    assert ctx.store.get(job["id"])["status"] == "success"


def test_retry_requeues_failed_job_and_keeps_successful_steps(tmp_path):
    from nexaploy_agent.jobs.store import JobStore

    store = JobStore(tmp_path / "jobs.db")
    job = store.create("bench.create", {"name": "erp"}, ["render", "configure", "start"])
    jid = job["id"] if isinstance(job, dict) else job
    store.claim_next()
    store.start_step(jid, 0)
    store.end_step(jid, 0, "success")
    store.start_step(jid, 1)
    store.end_step(jid, 1, "failure")
    store.skip_remaining(jid, 1)
    store.finish(jid, "failure", "boom")

    assert store.retry(jid) is True
    got = store.get(jid)
    assert got["status"] == "queued" and got["error"] is None
    assert [s["status"] for s in got["steps"]] == ["success", "pending", "pending"]
    assert store.retry(jid) is False  # only failed jobs
