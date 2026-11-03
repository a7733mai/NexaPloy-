"""site.usage (monitor log offsets, day rollover, MYSQL_PWD), site.get_config /
site.update_config, backup file list and the heartbeat usage block."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError as PydanticValidationError

from nexaploy_agent.control_plane import ControlPlaneClient, ControlPlaneService
from nexaploy_agent.redact import MASK
from nexaploy_agent.runtime.usage import (
    HeartbeatUsage, accumulate, compact_usage, db_size_command, parse_db_sizes, parse_du,
    parse_monitor_line, prune_days, read_new_lines,
)

from .conftest import DB_PW, make_bench, make_site, run_job, side_effect

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def mline(site, ts, duration, ttype="request"):
    return json.dumps({"site": site, "timestamp": ts, "transaction_type": ttype, "duration": duration,
                       "uuid": "u"}, separators=(",", ":")) + "\n"


def monitor_log(ctx, bench="erp-v15") -> Path:
    p = ctx.benches.path(bench) / "logs" / "monitor.json.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def db_sizes_out(**schemas):
    return "".join(f"{k}\t{v}\n" for k, v in schemas.items())


# --------------------------------------------------------------------------- pure parsing
def test_parse_monitor_line():
    assert parse_monitor_line(mline("a.example.com", "2026-11-03 10:00:00.123456+00:00", 2500000)) == (
        "a.example.com", "2026-11-03", "request", 2500000)
    # naive timestamps are UTC (older Frappe)
    assert parse_monitor_line(mline("a.example.com", "2026-11-03 23:59:59.9", 5, "job"))[1] == "2026-11-03"
    # non-UTC offsets are converted to the UTC day
    assert parse_monitor_line(mline("a.example.com", "2026-11-04T01:00:00+03:00", 5))[1] == "2026-11-03"
    for bad in ("not json", "[]", json.dumps({"site": "a", "transaction_type": "other", "duration": 1,
                                              "timestamp": "2026-11-03 00:00:00"}),
                mline("a.example.com", "garbage", 1), mline("a.example.com", "2026-11-03 00:00:00", -1),
                mline("a.example.com", "2026-11-03 00:00:00", True)):
        assert parse_monitor_line(bad) is None


def test_accumulate_day_rollover_and_prune():
    days: dict = {}
    n = accumulate(days, [
        mline("a.example.com", "2026-11-02 23:59:59.000000", 1_000_000),
        mline("a.example.com", "2026-11-03 00:00:01.000000", 2_000_000),
        mline("a.example.com", "2026-11-03 08:00:00.000000", 500_000, "job"),
        mline("b.example.com", "2026-11-03 08:00:00.000000", 7),
        "garbage\n",
    ])
    assert n == 4
    assert days["a.example.com"]["2026-11-02"] == {"cpu_us": 1_000_000, "requests": 1, "jobs": 0}
    assert days["a.example.com"]["2026-11-03"] == {"cpu_us": 2_500_000, "requests": 1, "jobs": 1}
    days["a.example.com"]["2026-10-20"] = {"cpu_us": 1, "requests": 1, "jobs": 0}
    prune_days(days, NOW.date(), keep=7)
    assert "2026-10-20" not in days["a.example.com"] and "2026-11-02" in days["a.example.com"]


def test_read_new_lines_offsets_partial_and_rotation(tmp_path):
    p = tmp_path / "monitor.json.log"
    p.write_text("one\ntwo\nthr")  # partial last line
    lines, off, ino = read_new_lines(p, 0, None)
    assert lines == ["one", "two"] and off == len("one\ntwo\n")
    assert read_new_lines(p, off, ino)[0] == []  # partial line is not consumed
    with p.open("a") as fh:
        fh.write("ee\nfour\n")
    lines, off2, ino = read_new_lines(p, off, ino)
    assert lines == ["three", "four"] and off2 == p.stat().st_size
    # truncation -> start over
    p.write_text("x\n")
    assert read_new_lines(p, off2, ino)[0] == ["x"]
    # rotation (new inode) -> start over even if the new file is larger
    p2 = tmp_path / "new.log"
    p2.write_text("a\n" * 50)
    os.replace(p2, p)
    lines, _, ino2 = read_new_lines(p, 2, ino)
    assert len(lines) == 50 and ino2 != ino
    assert read_new_lines(tmp_path / "missing.log", 10, 1) == ([], 0, None)


def test_db_and_du_parsing():
    assert parse_db_sizes("_abc\t1048576\n_def\t0\nbad line\n_x\tNaN?\n") == {"_abc": 1048576, "_def": 0}
    assert parse_du("100\t/a\n200\t/b\n") == 300
    argv = db_size_command()
    assert argv[:5] == ["docker", "exec", "-e", "MYSQL_PWD", "nexaploy-db"]
    assert not any("MYSQL_PWD=" in a or "-p" == a[:2] for a in argv)


# --------------------------------------------------------------------------- collector / job
def _setup_usage(ctx, runner):
    make_bench(ctx, "erp-v15")
    site_dir = make_site(ctx, "erp-v15", "a.example.com", extra={"db_name": "_abc", "monitor": 1})
    make_site(ctx, "erp-v15", "b.example.com", extra={"db_name": "_def"})
    for sub in ("public/files", "private/files"):
        (site_dir / sub).mkdir(parents=True)
    runner.on(["mariadb", "-uroot"], stdout=db_sizes_out(_abc=5 * 1024 * 1024, _def=1024 * 1024))
    runner.on(["du", "-sb"], stdout=f"{2 * 1024 * 1024}\t/x/public/files\n{1024 * 1024}\t/x/private/files\n")
    return site_dir


def test_collect_incremental_and_mysql_pwd(ctx, runner):
    _setup_usage(ctx, runner)
    log = monitor_log(ctx)
    log.write_text(mline("a.example.com", "2026-11-03 10:00:00.000000+00:00", 1_500_000)
                   + mline("b.example.com", "2026-11-03 10:00:00.000000+00:00", 250_000, "job"))
    r1 = ctx.usage.collect(now=NOW)
    a = r1["sites"]["a.example.com"]
    assert a["days"]["2026-11-03"] == {"cpu_seconds": 1.5, "requests": 1, "jobs": 0}
    assert a["database_mb"] == 5.0 and a["storage_mb"] == 3.0 and a["monitor_enabled"] is True
    assert r1["sites"]["b.example.com"]["days"]["2026-11-03"]["jobs"] == 1
    assert r1["sites"]["b.example.com"]["monitor_enabled"] is False

    # the root password reaches docker through the environment only
    db_calls = runner.find("mariadb")
    assert len(db_calls) == 1
    assert db_calls[0].env == {"MYSQL_PWD": DB_PW}
    assert all(DB_PW not in arg for arg in db_calls[0].args)

    # second collection only reads appended lines (offset persisted in the data dir)
    with log.open("a") as fh:
        fh.write(mline("a.example.com", "2026-11-03 11:00:00.000000+00:00", 500_000))
        fh.write('{"site": "a.example.com", "dura')  # partial line, completed later
    r2 = ctx.usage.collect(now=NOW)
    assert r2["lines_read"] == 1
    assert r2["sites"]["a.example.com"]["days"]["2026-11-03"] == {"cpu_seconds": 2.0, "requests": 2, "jobs": 0}
    state = json.loads((ctx.settings.data_dir / "usage_state.json").read_text())
    assert state["monitor"]["erp-v15"]["offset"] < log.stat().st_size
    # sizes are cached: no second du / mariadb call within the cache window
    assert len(runner.find("du -sb")) == 1 and len(runner.find("mariadb")) == 1

    # next UTC day: new bucket, yesterday kept
    with log.open("a") as fh:
        fh.write('ration": 1}\n')  # completes the garbage line (ignored)
        fh.write(mline("a.example.com", "2026-11-04 00:00:05.000000+00:00", 3_000_000))
    r3 = ctx.usage.collect(now=datetime(2026, 11, 4, 0, 10, tzinfo=timezone.utc))
    days = r3["sites"]["a.example.com"]["days"]
    assert days["2026-11-03"]["cpu_seconds"] == 2.0 and days["2026-11-04"]["cpu_seconds"] == 3.0
    block = compact_usage(r3)
    assert block["date"] == "2026-11-04"
    assert set(block["sites"]["a.example.com"]["days"]) == {"2026-11-03", "2026-11-04"}
    assert block["sites"]["a.example.com"]["days"]["2026-11-04"] == {"cpu_seconds": 3.0, "requests": 1}
    assert "jobs" not in block["sites"]["a.example.com"]["days"]["2026-11-04"]


def test_site_usage_job(ctx, worker, runner):
    _setup_usage(ctx, runner)
    monitor_log(ctx).write_text(mline("a.example.com", datetime.now(timezone.utc).isoformat(), 1_000_000))
    job = run_job(ctx, worker, "site.usage", {"site": "a.example.com"})
    assert job["status"] == "success", job
    r = job["result"]
    assert list(r["sites"]) == ["a.example.com"]
    assert r["usage"]["sites"]["a.example.com"]["database_mb"] == 5.0
    assert DB_PW not in json.dumps(job)
    # ensure_monitor turns the monitor on where missing (b) and leaves a alone
    job = run_job(ctx, worker, "site.usage", {"ensure_monitor": True})
    assert job["status"] == "success", job
    set_calls = [c.args for c in runner.find("set-config")]
    assert len(set_calls) == 1 and set_calls[0][-5:] == ["b.example.com", "set-config", "-p", "monitor", "1"]
    with pytest.raises(Exception):
        ctx.enqueue("site.usage", {"site": "zz.example.com"}, precheck=True)


def test_site_create_enables_monitor(ctx, worker, runner):
    make_bench(ctx, "erp-v15")
    runner.on("list-apps", stdout="frappe 15.0.0 version-15\n")
    make_site(ctx, "erp-v15", "erp.example.com")  # existing usable site: new-site skipped
    job = run_job(ctx, worker, "site.create",
                  {"bench": "erp-v15", "name": "erp.example.com", "admin_password": "Adm1nPass!"})
    assert job["status"] == "success", job
    assert [s["name"] for s in job["steps"]] == ["check", "new_site", "install_apps", "enable_monitor", "write_route"]
    assert runner.find("set-config")[0].args[-3:] == ["-p", "monitor", "1"]


def test_heartbeat_usage_throttle(ctx):
    calls = []

    class Fake:
        def collect(self):
            calls.append(1)
            return {"date": "2026-11-03", "collected_at": "x", "sites": {
                "a.example.com": {"database_mb": 1.0, "storage_mb": None,
                                  "days": {"2026-11-03": {"cpu_seconds": 1.0, "requests": 2, "jobs": 0}}}}}

    clock = [1000.0]
    hb = HeartbeatUsage(lambda: Fake(), clock=lambda: clock[0])
    first = hb.maybe_collect()
    assert first["sites"]["a.example.com"] == {
        "days": {"2026-11-03": {"cpu_seconds": 1.0, "requests": 2}}, "database_mb": 1.0}
    clock[0] += 60
    assert hb.maybe_collect() is None
    clock[0] += 15 * 60
    assert hb.maybe_collect() is not None and len(calls) == 2


def test_heartbeat_carries_usage_block(settings, ctx, runner):
    settings.control_plane.url = "https://cp.example.com"
    settings.control_plane.token = "srv-token"
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"ok": True}})

    client = ControlPlaneClient(settings, client=httpx.Client(transport=httpx.MockTransport(handler)),
                                sleep=lambda s: None)
    _setup_usage(ctx, runner)
    svc = ControlPlaneService(ctx, client)
    svc.heartbeat_once()
    svc.heartbeat_once()
    assert "usage" in bodies[0] and "a.example.com" in bodies[0]["usage"]["sites"]
    assert "usage" not in bodies[1]  # throttled to every 15 minutes
    assert DB_PW not in json.dumps(bodies)


# --------------------------------------------------------------------------- config
def test_get_config_redacts(ctx, worker):
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", "a.example.com", extra={
        "encryption_key": "ENC-SECRET", "mail_password": "MAILPW", "maintenance_mode": 0, "limits": {"x": 1}})
    job = run_job(ctx, worker, "site.get_config", {"site": "a.example.com"})
    assert job["status"] == "success", job
    cfg = job["result"]["config"]
    assert cfg["db_password"] == MASK and cfg["encryption_key"] == MASK and cfg["mail_password"] == MASK
    assert cfg["db_name"] == "_abc" and cfg["maintenance_mode"] == 0 and cfg["limits"] == {"x": 1}
    assert job["result"]["redacted"] == ["db_password", "encryption_key", "mail_password"]
    assert "ENC-SECRET" not in json.dumps(job) and "MAILPW" not in json.dumps(job)


def test_update_config_batch(ctx, worker, runner):
    make_bench(ctx, "erp-v15")
    d = make_site(ctx, "erp-v15", "a.example.com", extra={"maintenance_mode": 0, "old_key": "x"})
    job = run_job(ctx, worker, "site.update_config", {
        "site": "a.example.com",
        "set": {"maintenance_mode": 1, "rate_limit": {"limit": 600, "window": 86400}, "mail_password": "S3CRETPW"},
        "remove": ["old_key", "absent_key"],
    })
    assert job["status"] == "success", job
    cfg = json.loads((d / "site_config.json").read_text())
    assert cfg["maintenance_mode"] == 1 and cfg["rate_limit"] == {"limit": 600, "window": 86400}
    assert cfg["mail_password"] == "S3CRETPW" and "old_key" not in cfg
    assert cfg["db_password"] == "x"  # untouched
    assert sorted(job["result"]["changed"]) == ["mail_password", "maintenance_mode", "rate_limit"]
    assert job["result"]["removed"] == ["old_key"]
    # Frappe's on-disk format, no container exec needed
    assert (d / "site_config.json").read_text() == json.dumps(cfg, indent=1, sort_keys=True)
    assert not runner.find("set-config")
    # the secret value is scrubbed from the stored params once finished
    assert "S3CRETPW" not in json.dumps(job)
    assert job["params"]["set"]["mail_password"] == MASK

    # idempotent: same change again does not rewrite the file
    mtime = (d / "site_config.json").stat().st_mtime_ns
    job = run_job(ctx, worker, "site.update_config", {"site": "a.example.com", "set": {"maintenance_mode": 1}})
    assert job["status"] == "success" and job["result"]["changed"] == []
    assert (d / "site_config.json").stat().st_mtime_ns == mtime


@pytest.mark.parametrize("params", [
    {"set": {"db_password": "x"}},
    {"set": {"encryption_key": "x"}},
    {"remove": ["db_name"]},
    {"set": {"bad key": 1}},
    {"set": {"a": 1}, "remove": ["a"]},
])
def test_update_config_validation(ctx, params):
    with pytest.raises((PydanticValidationError, ValueError)):
        ctx.enqueue("site.update_config", {"site": "a.example.com", **params})


def test_config_api_routes(ctx):
    from fastapi.testclient import TestClient

    from nexaploy_agent.api import create_app

    from .conftest import TOKEN

    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", "a.example.com")
    client = TestClient(create_app(ctx))
    h = {"Authorization": f"Bearer {TOKEN}"}
    r = client.get("/v1/sites/a.example.com/config", headers=h)
    assert r.status_code == 200 and r.json()["config"]["db_password"] == MASK
    r = client.patch("/v1/sites/a.example.com/config", headers=h, json={"set": {"maintenance_mode": 1}})
    assert r.status_code == 202
    job = client.get(f"/v1/jobs/{r.json()['job_id']}", headers=h).json()
    assert job["kind"] == "site.update_config" and job["params"]["set"] == {"maintenance_mode": 1}
    assert client.post("/v1/usage", headers=h, json={"bench": "erp-v15"}).status_code == 202
    assert client.post("/v1/usage", headers=h, json={"bench": "nope-bench"}).status_code == 404


# --------------------------------------------------------------------------- backup files
def test_backup_result_file_list(ctx, worker, runner, settings):
    make_bench(ctx, "erp-v15")
    site_dir = make_site(ctx, "erp-v15", "a.example.com")
    bdir = site_dir / "private" / "backups"
    bdir.mkdir(parents=True)

    def write(call):
        stamp = "20261103_120000-a_example_com"
        (bdir / f"{stamp}-database.sql.gz").write_bytes(b"DB")
        (bdir / f"{stamp}-files.tar").write_bytes(b"PUB")
        (bdir / f"{stamp}-private-files.tar").write_bytes(b"PRIV")
        (bdir / f"{stamp}-site_config_backup.json").write_bytes(b"{}")

    runner.on(["backup", "--with-files"], result=side_effect(write))
    settings.rclone_conf.write_text("[nexaploy-backups]\ntype = alias\n")
    job = run_job(ctx, worker, "site.backup", {"site": "a.example.com"})
    assert job["status"] == "success", job
    r = job["result"]
    files = {f["file_type"]: f for f in r["backup_files"]}
    assert set(files) == {"database", "public", "private", "site_config"}
    base = f"srv-test/erp-v15/a.example.com/{r['backup_id']}"
    assert files["database"]["remote_key"] == f"{base}/20261103_120000-a_example_com-database.sql.gz"
    assert files["public"]["size"] == 3 and len(files["private"]["sha256"]) == 64
    # without upload there is no remote key yet
    runner.on(["backup", "--with-files"], result=side_effect(write))
    job = run_job(ctx, worker, "site.backup", {"site": "a.example.com", "upload": False})
    assert all(f["remote_key"] is None for f in job["result"]["backup_files"])
