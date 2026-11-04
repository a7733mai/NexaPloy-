"""Operations jobs: registry login, backup configuration, remote retention, health facts."""

import json
import os
import stat
import tomllib
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError as PydanticValidationError

from nexaploy_agent.redact import MASK, redact_params
from nexaploy_agent.runtime.backup import merge_rclone_conf, new_ts
from nexaploy_agent.runtime.registry import Registry, registry_host
from nexaploy_agent.runtime.server import host_facts, parse_containers, summarize_containers

from .conftest import IMAGE, make_bench, make_site, run_job

REG_PW = "ghp_registry-PASSWORD-123"
S3_SECRET = "s3-SECRET/access+key=="
REG_IMAGE = "ghcr.io/acme/nexaploy/bench:erp-v15-main-20261103T2130"
REGISTRY = {"url": "https://ghcr.io/acme/", "username": "acme-bot", "password": REG_PW}

BACKUP_PARAMS = {
    "remote": "nexaploy-backups", "type": "s3", "provider": "Cloudflare",
    "endpoint": "https://abc.r2.cloudflarestorage.com", "region": "auto", "bucket": "nx-backups",
    "access_key_id": "AKIAEXAMPLE", "secret_access_key": S3_SECRET, "prefix": "srv-abc12345",
}


def all_output(job):
    return "".join(s.get("output") or "" for s in job["steps"])


# ---- registry -----------------------------------------------------------------------------
def test_registry_url_and_host():
    assert Registry(url="https://ghcr.io/Acme/").url == "ghcr.io/acme"
    assert registry_host("ghcr.io/acme") == "ghcr.io"
    assert registry_host("registry.example.com:5000/team") == "registry.example.com:5000"
    assert registry_host("localhost/x") == "localhost"
    assert registry_host("acme") is None  # Docker Hub namespace
    with pytest.raises(PydanticValidationError):
        Registry(url="ghcr.io/acme; rm -rf /")
    with pytest.raises(PydanticValidationError):
        Registry(url="ghcr.io", username="--password=x")
    assert Registry(url="ghcr.io/acme").covers(REG_IMAGE)
    assert not Registry(url="ghcr.io/other").covers(REG_IMAGE)


def test_registry_password_redacted():
    params = {"tag": REG_IMAGE, "push": True, "registry": dict(REGISTRY)}
    red = redact_params(params)
    assert red["registry"]["password"] == MASK
    assert red["registry"]["username"] == "acme-bot"
    assert REG_PW not in json.dumps(red)


def _script(ctx):
    script = ctx.settings.build_script
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/bash\n")


def test_image_build_push_logs_in_via_stdin(ctx, worker, runner):
    _script(ctx)
    runner.on(["image", "inspect"], returncode=1)
    runner.on("build.sh", stdout=f"{REG_IMAGE}\n")
    job = run_job(ctx, worker, "image.build", {
        "major": 15, "tag": REG_IMAGE, "apps_json": [], "push": True, "registry": REGISTRY,
    })
    assert job["status"] == "success", job
    login = runner.find("docker login")
    assert len(login) == 1
    assert login[0].args == ["docker", "login", "ghcr.io", "--username", "acme-bot", "--password-stdin"]
    assert login[0].input == REG_PW + "\n"
    # never on argv, never in output, scrubbed from the store once final
    assert all(REG_PW not in c.cmdline for c in runner.calls)
    assert REG_PW not in all_output(job)
    assert ctx.store.get(job["id"])["params"]["registry"]["password"] == MASK
    build = runner.find("build.sh")[0].args
    assert "--push" in build and build[build.index("--tag") + 1] == REG_IMAGE
    assert runner.cmdlines().index(login[0].cmdline) < runner.cmdlines().index(runner.find("build.sh")[0].cmdline)
    assert job["result"]["pushed"] is True


def test_image_build_existing_image_is_pushed(ctx, worker, runner):
    _script(ctx)
    job = run_job(ctx, worker, "image.build", {
        "major": 15, "tag": REG_IMAGE, "apps_json": [], "push": True, "registry": REGISTRY,
    })
    assert job["status"] == "success", job
    assert not runner.called("build.sh")
    assert runner.called(f"docker push {REG_IMAGE}")


def test_image_build_without_registry_does_not_login(ctx, worker, runner):
    _script(ctx)
    runner.on(["image", "inspect"], returncode=1)
    runner.on("build.sh", stdout=f"{IMAGE}\n")
    job = run_job(ctx, worker, "image.build", {"major": 15, "tag": IMAGE, "apps_json": []})
    assert job["status"] == "success"
    assert not runner.called("docker login")


def test_image_build_login_failure_fails_job(ctx, worker, runner):
    _script(ctx)
    runner.on("docker login", returncode=1, stderr=f"denied for {REG_PW}")
    job = run_job(ctx, worker, "image.build", {
        "major": 15, "tag": REG_IMAGE, "apps_json": [], "push": True, "registry": REGISTRY,
    })
    assert job["status"] == "failure"
    assert "docker login failed" in job["error"]
    assert REG_PW not in job["error"] and REG_PW not in all_output(job)
    assert not runner.called("build.sh")


def test_bench_create_logs_in_before_pull(ctx, worker, runner):
    runner.on(["image", "inspect"], returncode=1, times=1)
    job = run_job(ctx, worker, "bench.create", {"name": "erp-v15", "image": REG_IMAGE, "registry": REGISTRY})
    assert job["status"] == "success", job
    lines = runner.cmdlines()
    login = next(i for i, l in enumerate(lines) if "docker login" in l)
    pull = next(i for i, l in enumerate(lines) if f"docker pull {REG_IMAGE}" in l)
    assert login < pull
    assert runner.find("docker login")[0].input == REG_PW + "\n"
    assert ctx.store.get(job["id"])["params"]["registry"]["password"] == MASK


def test_bench_update_skips_login_when_image_present(ctx, worker, runner):
    make_bench(ctx, "erp-v15")
    job = run_job(ctx, worker, "bench.update", {"bench": "erp-v15", "image": REG_IMAGE, "registry": REGISTRY})
    assert job["status"] == "success", job
    assert not runner.called("docker login")
    assert f"IMAGE={REG_IMAGE}" in (ctx.benches.path("erp-v15") / ".env").read_text()


# ---- configure_backups ------------------------------------------------------------------------
def test_configure_backups_writes_rclone_conf(ctx, worker, settings):
    conf = settings.rclone_conf
    conf.write_text("[other]\ntype = local\n\n[nexaploy-backups]\ntype = local\n")
    job = run_job(ctx, worker, "server.configure_backups", BACKUP_PARAMS)
    assert job["status"] == "success", job
    assert stat.S_IMODE(os.stat(conf).st_mode) == 0o600
    text = conf.read_text()
    import configparser

    cp = configparser.RawConfigParser(interpolation=None)
    cp.read_string(text)
    assert cp.get("other", "type") == "local"  # other remotes kept
    s3 = dict(cp.items("nexaploy-backups-s3"))
    assert s3["type"] == "s3" and s3["provider"] == "Cloudflare"
    assert s3["access_key_id"] == "AKIAEXAMPLE" and s3["secret_access_key"] == S3_SECRET
    assert s3["endpoint"] == "https://abc.r2.cloudflarestorage.com" and s3["region"] == "auto"
    assert dict(cp.items("nexaploy-backups")) == {"type": "alias", "remote": "nexaploy-backups-s3:nx-backups"}
    assert settings.rclone_remote_configured()
    # agent.toml [backup] + live settings
    data = tomllib.loads((settings.config_dir / "agent.toml").read_text())
    assert data["backup"] == {"remote": "nexaploy-backups", "prefix": "srv-abc12345"}
    assert stat.S_IMODE(os.stat(settings.config_dir / "agent.toml").st_mode) == 0o600
    assert settings.backup_prefix == "srv-abc12345"
    # secrets: not in output, scrubbed from store
    assert S3_SECRET not in all_output(job) and S3_SECRET not in json.dumps(job["result"])
    stored = ctx.store.get(job["id"])["params"]
    assert stored["secret_access_key"] == MASK and stored["access_key_id"] == MASK


def test_configure_backups_is_idempotent(ctx, worker, settings):
    run_job(ctx, worker, "server.configure_backups", BACKUP_PARAMS)
    before = settings.rclone_conf.read_text()
    mtime = settings.rclone_conf.stat().st_mtime_ns
    job = run_job(ctx, worker, "server.configure_backups", BACKUP_PARAMS)
    assert job["status"] == "success"
    assert settings.rclone_conf.read_text() == before
    assert settings.rclone_conf.stat().st_mtime_ns == mtime
    assert all("already" in s["output"] for s in job["steps"])
    # a changed secret rewrites only our sections
    job = run_job(ctx, worker, "server.configure_backups", {**BACKUP_PARAMS, "secret_access_key": "new-secret-1"})
    assert "new-secret-1" in settings.rclone_conf.read_text()
    assert before.count("[") == settings.rclone_conf.read_text().count("[")


def test_configure_backups_validation(ctx):
    for bad in ({"type": "gcs"}, {"bucket": "Bad_Bucket"}, {"remote": "a b"},
                {"secret_access_key": "x\n[evil]"}, {"prefix": "../up"}, {"endpoint": "ftp://x y"}):
        with pytest.raises(Exception):
            ctx.enqueue("server.configure_backups", {**BACKUP_PARAMS, **bad})


def test_merge_rclone_conf_preserves_case_and_percent():
    text = merge_rclone_conf("", {"r": {"type": "s3", "secret_access_key": "a%b%%c"}})
    assert "secret_access_key = a%b%%c" in text


# ---- prune_remote -----------------------------------------------------------------------------
def _ts(days_ago):
    return new_ts(datetime.now(timezone.utc) - timedelta(days=days_ago, minutes=1))


def test_prune_remote_selects_old_dirs(ctx, worker, runner, settings):
    settings.rclone_conf.write_text("[nexaploy-backups]\ntype = local\n")
    stamps = [_ts(d) for d in (0, 1, 10, 20, 30, 40)]
    listing = "".join(f"{t}/\n" for t in stamps) + "junk/\n"
    runner.on(["lsf", "--dirs-only"], stdout=listing)
    job = run_job(ctx, worker, "backup.prune_remote",
                  {"site": "a.example.com", "bench": "erp-v15", "keep_days": 14, "keep_min": 3})
    assert job["status"] == "success", job
    lsf = runner.find("rclone")[0].args
    assert lsf[-1] == "nexaploy-backups:srv-test/erp-v15/a.example.com"
    purged = [c.args[-1] for c in runner.find(" purge ")]
    old = sorted(stamps[3:])
    assert purged == [f"nexaploy-backups:srv-test/erp-v15/a.example.com/{t}" for t in old]
    assert [p["ts"] for p in job["result"]["pruned"]] == old
    assert sorted(job["result"]["kept"]) == sorted(stamps[:3])


def test_prune_remote_keep_min_and_prefix(ctx, worker, runner, settings):
    settings.rclone_conf.write_text("[nexaploy-backups]\ntype = local\n")
    stamps = [_ts(d) for d in (30, 40)]
    runner.on(["lsf", "--dirs-only"], stdout="\n".join(t + "/" for t in stamps))
    job = run_job(ctx, worker, "backup.prune_remote",
                  {"site": "a.example.com", "bench": "erp-v15", "keep_days": 7, "keep_min": 1,
                   "prefix": "srv-other"})
    assert job["status"] == "success"
    purged = [c.args[-1] for c in runner.find(" purge ")]
    assert purged == [f"nexaploy-backups:srv-other/erp-v15/a.example.com/{min(stamps)}"]


def test_prune_remote_unconfigured_is_noop(ctx, worker, runner):
    job = run_job(ctx, worker, "backup.prune_remote", {"site": "a.example.com", "bench": "erp-v15"})
    assert job["status"] == "success"
    assert not runner.called("rclone")
    assert job["result"]["pruned"] == []


def test_prune_remote_rclone_error_fails(ctx, worker, runner, settings):
    settings.rclone_conf.write_text("[nexaploy-backups]\ntype = local\n")
    runner.on(["lsf"], returncode=1, stderr="AccessDenied")
    job = run_job(ctx, worker, "backup.prune_remote", {"site": "a.example.com", "bench": "erp-v15"})
    assert job["status"] == "failure" and "rclone lsf failed" in job["error"]
    with pytest.raises(PydanticValidationError):
        ctx.enqueue("backup.prune_remote", {"site": "a.example.com", "bench": "erp-v15", "keep_days": 0})


# ---- facts / health ---------------------------------------------------------------------------
PS = "\n".join(json.dumps(x) for x in [
    {"Names": "nexaploy-db", "State": "running", "Status": "Up 3 hours (healthy)",
     "Labels": "com.docker.compose.project=nexaploy-db,com.docker.compose.service=db"},
    {"Names": "erp-v15-backend", "State": "running", "Status": "Up 1 hour (unhealthy)",
     "Labels": "com.docker.compose.project=erp-v15,com.docker.compose.service=backend"},
    {"Names": "erp-v15-frontend", "State": "running", "Status": "Up 1 hour (healthy)",
     "Labels": "com.docker.compose.project=erp-v15,com.docker.compose.service=frontend"},
    {"Names": "erp-v15-queue-long", "State": "exited", "Status": "Exited (1) 2 minutes ago",
     "Labels": "com.docker.compose.project=erp-v15,com.docker.compose.service=queue-long"},
    {"Names": "erp-v15-configurator-run-1", "State": "exited", "Status": "Exited (0)",
     "Labels": "com.docker.compose.project=erp-v15,com.docker.compose.service=configurator"},
    {"Names": "unrelated", "State": "exited", "Status": "Exited (1)", "Labels": ""},
])


def test_container_summary():
    rows = parse_containers(PS)
    assert rows[1]["health"] == "unhealthy" and rows[0]["health"] == "healthy"
    s = summarize_containers(rows, ["erp-v15", "erp-v16"])
    names = {c["name"] for c in s["containers"]}
    assert "unrelated" not in names and "nexaploy-db" in names
    assert s["unhealthy_containers"] == 2  # backend unhealthy + queue-long exited
    h = s["bench_health"]["erp-v15"]
    assert h["unhealthy"] == ["erp-v15-backend", "erp-v15-queue-long"] and h["healthy"] is False
    assert h["total"] == 3 and h["running"] == 2
    assert s["bench_health"]["erp-v16"] == {"total": 0, "running": 0, "unhealthy": [], "healthy": False}


def test_host_facts_extended(ctx, runner, settings):
    make_bench(ctx, "erp-v15")
    runner.on(["ps", "--all", "--no-trunc"], stdout=PS)
    facts = host_facts(ctx.docker, settings.root)
    assert facts["mem_used"] == max(0, facts["mem_total"] - facts["mem_available"])
    paths = [m["path"] for m in facts["mounts"]]
    assert paths == ["/", str(settings.root)]
    for m in facts["mounts"]:
        assert m["total"] >= m["used"] >= 0 and 0 <= m["used_percent"] <= 100
    assert len(facts["load_avg"]) == 3
    assert facts["unhealthy_containers"] == 2
    assert "erp-v15" in facts["bench_health"]
    assert isinstance(facts["utc_offset_minutes"], int)


def test_server_health_job(ctx, worker, runner):
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", "a.example.com")
    runner.on(["ps", "--all", "--no-trunc"], stdout=PS)
    job = run_job(ctx, worker, "server.health", {})
    assert job["status"] == "success", job
    assert job["result"]["server_id"] == "srv-test"
    assert job["result"]["unhealthy_containers"] == 2
    assert job["result"]["benches"][0]["name"] == "erp-v15"


def test_api_routes(ctx, settings):
    from fastapi.testclient import TestClient

    from nexaploy_agent.api import create_app

    from .conftest import TOKEN

    client = TestClient(create_app(ctx))
    h = {"Authorization": f"Bearer {TOKEN}"}
    assert client.post("/v1/server/health", headers=h).status_code == 202
    r = client.post("/v1/server/backup-config", headers=h, json=BACKUP_PARAMS)
    assert r.status_code == 202
    job = client.get(f"/v1/jobs/{r.json()['job_id']}", headers=h).json()
    assert job["params"]["secret_access_key"] == MASK
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", "a.example.com")
    r = client.post("/v1/sites/a.example.com/backups/prune-remote", headers=h, json={"keep_days": 7})
    assert r.status_code == 202
    job = client.get(f"/v1/jobs/{r.json()['job_id']}", headers=h).json()
    assert job["params"]["bench"] == "erp-v15" and job["params"]["keep_days"] == 7
    assert client.post("/v1/sites/b.example.com/backups/prune-remote", headers=h, json={}).status_code == 404
