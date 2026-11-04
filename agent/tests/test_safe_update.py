"""Safe update support: site.clone onto another bench (target_bench) and site.smoke_check."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from nexaploy_agent.api import create_app
from nexaploy_agent.jobs.base import Conflict, NotFound
from nexaploy_agent.jobs.registry import REGISTRY
from nexaploy_agent.runtime import smoke as rs
from nexaploy_agent.runtime.docker import Call, Result
from nexaploy_agent.runtime.lifecycle import STAGING_CONFIG_KEY

from .conftest import DB_PW, TOKEN, bench_running, make_bench, make_site, run_job, side_effect

SRC = "erp.example.com"
TST = "uptest-erp.example.com"
KEY = "enc-key-SECRET-456"


# ---- site.clone with target_bench ------------------------------------------------------------
def _setup(ctx, runner):
    make_bench(ctx, "erp-v15")
    make_bench(ctx, "erp-v15-new", image="nexaploy/bench:erp-v15-main-20261201T1000")
    src = make_site(ctx, "erp-v15", SRC, extra={"encryption_key": KEY})

    def backup(call):
        d = src / "private" / "backups"
        d.mkdir(parents=True, exist_ok=True)
        (d / "20261201_100000-erp_example_com-database.sql.gz").write_bytes(b"sql")
        (d / "20261201_100000-erp_example_com-files.tar").write_bytes(b"pub")

    runner.on(["backup", "--with-files"], result=side_effect(backup))

    def new_site(call):
        project = call.args[call.args.index("-p") + 1]
        make_site(ctx, project, call.args[call.args.index("new-site") + 1], extra={"db_name": "_tst1"})

    runner.on("new-site", result=side_effect(new_site))
    return src


def test_clone_onto_another_bench(ctx, worker, runner):
    _setup(ctx, runner)
    job = run_job(ctx, worker, "site.clone",
                  {"bench": "erp-v15", "site": SRC, "target_site": TST, "target_bench": "erp-v15-new"})
    assert job["status"] == "success", job
    assert job["result"]["target_bench"] == "erp-v15-new"
    # backup on the source bench, everything else on the target bench
    assert runner.find("backup --with-files")[0].args[:4] == ["docker", "compose", "-p", "erp-v15"]
    for needle in (f"new-site {TST}", f"--site {TST} restore", f"--site {TST} set-config encryption_key",
                   f"--site {TST} migrate"):
        calls = runner.find(needle)
        assert calls and calls[0].args[:4] == ["docker", "compose", "-p", "erp-v15-new"], needle
    assert ctx.sites.find_bench(TST) == "erp-v15-new"
    assert ctx.sites.site_config("erp-v15-new", TST)[STAGING_CONFIG_KEY] == SRC
    assert ctx.sites.find_bench(SRC) == "erp-v15"
    assert "erp-v15-new-frontend" in ctx.proxy.route_path(TST).read_text()
    blob = json.dumps(job)
    assert KEY not in blob and DB_PW not in blob


def test_clone_target_bench_prechecks(ctx, worker, runner):
    _setup(ctx, runner)
    with pytest.raises(NotFound):
        ctx.enqueue("site.clone", {"bench": "erp-v15", "site": SRC, "target_site": TST, "target_bench": "nope-bench"},
                    precheck=True)
    # the copy exists on the *source* bench -> conflict when asked for another bench
    make_site(ctx, "erp-v15", TST, extra={STAGING_CONFIG_KEY: SRC})
    with pytest.raises(Conflict):
        ctx.enqueue("site.clone", {"bench": "erp-v15", "site": SRC, "target_site": TST, "target_bench": "erp-v15-new"},
                    precheck=True)
    with pytest.raises(ValidationError):
        ctx.enqueue("site.clone", {"bench": "erp-v15", "site": SRC, "target_site": TST, "target_bench": "Bad Name"})


# ---- site.smoke_check -----------------------------------------------------------------------
def test_smoke_pure_helpers():
    assert rs.valid_path("/api/method/ping") == "/api/method/ping"
    for bad in ("api", "/a b", "/../etc", "", "/" + "x" * 300):
        with pytest.raises(ValueError):
            rs.valid_path(bad)
    args = rs.http_args(SRC, ["/login"], 15)
    assert args[0] == rs.CONTAINER_PYTHON and args[1] == "-c" and args[3:] == [SRC, "15", "/login"]
    assert rs.parse_output("x\n" + rs.RESULT_MARKER + '[{"a": 1}]\n') == [{"a": 1}]
    assert rs.parse_output("garbage") is None and rs.parse_output(rs.RESULT_MARKER + "{bad") is None
    checks = rs.evaluate_http([
        {"path": "/api/method/ping", "status": 200, "ms": 12, "snippet": '{"message":"pong"}'},
        {"path": "/login", "status": 502, "ms": 3, "snippet": "Bad   gateway"},
        {"path": "/x", "status": None, "error": "URLError: refused"},
        {"path": "/api/method/ping", "status": 200, "snippet": "{}"},
    ])
    assert [c["ok"] for c in checks] == [True, False, False, False]
    assert "HTTP 502" in checks[1]["detail"] and "Bad gateway" in checks[1]["detail"]
    assert "refused" in checks[2]["detail"] and "pong" in checks[3]["detail"]
    db = rs.evaluate_db({"ok": True, "users": 1, "installed_apps": ["frappe"], "duration": 0.2}, ["frappe", "erpnext"])
    assert db[0]["ok"] and not db[1]["ok"] and "erpnext" in db[1]["detail"]
    assert rs.evaluate_db(None)[0]["ok"] is False
    assert rs.evaluate_db({"ok": False, "error": "OperationalError: x"})[0]["detail"] == "OperationalError: x"
    assert rs.summary([{"name": "a", "ok": True}]) == "1 check(s) passed"
    assert rs.summary([{"name": "a", "ok": False, "detail": "boom"}]) == "a: boom"


def test_probes_compile_and_db_probe_runs_with_stub(tmp_path):
    compile(rs.HTTP_PROBE, "<http>", "exec")
    compile(rs.DB_PROBE, "<db>", "exec")
    (tmp_path / "frappe.py").write_text(
        "class _DB:\n    def rollback(self): pass\n"
        "db = _DB()\n"
        "def init(site, sites_path): pass\n"
        "def connect(): pass\n"
        "def destroy(): pass\n"
        "def get_all(dt, fields=None, limit=None): return [{'name': 'Administrator'}]\n"
        "def get_installed_apps(): return ['frappe', 'erpnext']\n"
    )
    proc = subprocess.run([sys.executable, "-c", rs.DB_PROBE, SRC], cwd=tmp_path, capture_output=True, text=True,
                          env={"PYTHONPATH": str(tmp_path)})
    data = rs.parse_output(proc.stdout)
    assert data["ok"] is True and data["users"] == 1 and data["installed_apps"] == ["frappe", "erpnext"]


def _probe(http_status=200, db_ok=True):
    def result(call: Call) -> Result:
        script = call.args[call.args.index("-c") + 1]
        if script == rs.HTTP_PROBE:
            paths = call.args[call.args.index("-c") + 4:]
            out = [{"path": p, "status": http_status, "ms": 5, "error": None,
                    "snippet": '{"message":"pong"}' if "ping" in p else "<html>"} for p in paths]
        else:
            out = {"ok": db_ok, "users": 1, "installed_apps": ["frappe", "erpnext"], "duration": 0.1,
                   "error": None if db_ok else "OperationalError: (1146, table missing)"}
        return Result(0, rs.RESULT_MARKER + json.dumps(out) + "\n", "")
    return result


def test_smoke_check_job_passes(ctx, worker, runner):
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", TST)
    bench_running(runner)
    runner.on(["exec", "-T", "-w"], result=_probe())
    job = run_job(ctx, worker, "site.smoke_check", {"site": TST, "apps": ["frappe", "erpnext"]})
    assert job["status"] == "success", job
    res = job["result"]
    assert res["ok"] is True and [c["name"] for c in res["checks"]] == [
        "http /api/method/ping", "http /login", "database", "installed apps"]
    call = runner.find("exec -T -w")[0]
    assert call.args[:4] == ["docker", "compose", "-p", "erp-v15"] and "backend" in call.args
    assert call.args[-2:] == ["/api/method/ping", "/login"]


def test_smoke_check_job_fails_with_reasons(ctx, worker, runner):
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", TST)
    bench_running(runner)
    runner.on(["exec", "-T", "-w"], result=_probe(http_status=500, db_ok=False))
    job = run_job(ctx, worker, "site.smoke_check", {"site": TST})
    assert job["status"] == "failure"
    assert "http /login: HTTP 500" in job["error"] and "table missing" in job["error"]
    assert job["result"]["ok"] is False
    assert [s["status"] for s in job["steps"]] == ["success", "success", "failure"]


def test_smoke_check_needs_running_bench_and_valid_params(ctx, worker, runner):
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", TST)
    with pytest.raises(ValidationError):
        ctx.enqueue("site.smoke_check", {"site": TST, "paths": ["no-slash"]})
    with pytest.raises(ValidationError):
        ctx.enqueue("site.smoke_check", {"site": TST, "timeout": 9999})
    job = run_job(ctx, worker, "site.smoke_check", {"site": TST})
    assert job["status"] == "failure" and "not running" in job["error"]


def test_smoke_check_api(ctx):
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", TST)
    assert "site.smoke_check" in REGISTRY
    client = TestClient(create_app(ctx))
    headers = {"Authorization": f"Bearer {TOKEN}"}
    r = client.post(f"/v1/sites/{TST}/smoke-check", headers=headers, json={"paths": ["/login"]})
    assert r.status_code == 202 and r.json()["kind"] == "site.smoke_check"
    assert client.post("/v1/sites/missing.example.com/smoke-check", headers=headers).status_code == 404
