from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexaploy_agent.config import Settings
from nexaploy_agent.context import AgentContext
from nexaploy_agent.jobs.worker import Worker
from nexaploy_agent.runtime.docker import Call, FakeRunner, Result

TOKEN = "test-token-0123456789"
DB_PW = "db-root-pw-SECRET"
IMAGE = "nexaploy/bench:erp-v15-main-20261103T2130"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    root = tmp_path / "nexaploy"
    s = Settings.model_validate({
        "agent": {"server_id": "srv-test", "token": TOKEN, "root": str(root)},
        "backup": {"remote": "nexaploy-backups", "prefix": "srv-test", "retention_days": 14},
    })
    (root / "config").mkdir(parents=True)
    (root / "config" / "db.env").write_text(f"DB_ROOT_PASSWORD={DB_PW}\n")
    return s


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def ctx(settings: Settings, runner: FakeRunner) -> AgentContext:
    return AgentContext(settings, runner=runner)


@pytest.fixture
def worker(ctx: AgentContext) -> Worker:
    return Worker(ctx, idle_seconds=0.01)


def make_bench(ctx: AgentContext, bench: str = "erp-v15", image: str = IMAGE) -> Path:
    b = ctx.benches
    b.ensure_dirs(bench)
    b.write_env(bench, b.default_env(bench, image))
    b.render(bench)
    return b.path(bench)


def make_site(ctx: AgentContext, bench: str, site: str, domains: list | None = None,
              extra: dict | None = None) -> Path:
    d = ctx.benches.sites_dir(bench) / site
    d.mkdir(parents=True, exist_ok=True)
    cfg = {"db_name": "_abc", "db_password": "x"}
    if domains is not None:
        cfg["domains"] = domains
    cfg.update(extra or {})
    (d / "site_config.json").write_text(json.dumps(cfg))
    return d


def bench_running(runner: FakeRunner) -> None:
    """Script `compose ps --status running --services` to report backend up."""
    runner.on(["ps", "--status", "running", "--services"], stdout="backend\nfrontend\nscheduler\n")


def apps_listing(runner: FakeRunner, *apps: str) -> None:
    out = "".join(f"{a} 15.0.0 version-15\n" for a in apps)
    runner.on("list-apps", stdout=out)


def side_effect(fn):
    """Wrap fn(call) as a FakeRunner result callable returning success."""
    def _inner(call: Call) -> Result:
        r = fn(call)
        return r if isinstance(r, Result) else Result(0, "", "")
    return _inner


def run_job(ctx: AgentContext, worker: Worker, kind: str, params: dict) -> dict:
    job = ctx.enqueue(kind, params)
    assert worker.run_once()
    return ctx.store.get(job["id"])
