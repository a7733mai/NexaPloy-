"""controlplane.update (backup -> pull -> switch -> migrate -> health, rollback) and the control plane
backup tarball / import (install.sh --restore-control-plane)."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from nexaploy_agent.api import create_app
from nexaploy_agent.jobs.base import Conflict, NotFound
from nexaploy_agent.jobs.controlplane_jobs import ControlPlaneUpdate
from nexaploy_agent.jobs.registry import REGISTRY
from nexaploy_agent.runtime import controlplane as rc
from nexaploy_agent.runtime.backup import BackupError

from .conftest import DB_PW, TOKEN, bench_running, make_bench, make_site, run_job, side_effect

CP = "cp.example.com"
BENCH = "nexaploy-cp"
OLD = "ghcr.io/a7733mai/nexaploy-control-plane:0.1.0"
NEW = "ghcr.io/a7733mai/nexaploy-control-plane:0.2.0"
KEY = "enc-key-SECRET-789"


# --------------------------------------------------------------------------- pure
def test_rollback_plan():
    assert rc.rollback_plan("pull", switched=False, migrate_started=False, backup_id="b") == []
    assert rc.rollback_plan("backup", switched=True, migrate_started=True, backup_id="b") == []
    assert rc.rollback_plan("switch", switched=True, migrate_started=False, backup_id="b") == ["switch_back", "health"]
    assert rc.rollback_plan("migrate", switched=True, migrate_started=True, backup_id="b") == [
        "switch_back", "restore_database", "health"]
    # no backup taken: the image goes back, the database cannot
    assert rc.rollback_plan("migrate", switched=True, migrate_started=True, backup_id=None) == ["switch_back", "health"]
    assert rc.rollback_plan("health", switched=True, migrate_started=True, backup_id="b")[1] == "restore_database"
    # up to date (never switched): a failed health check changes nothing
    assert rc.rollback_plan("health", switched=False, migrate_started=False, backup_id="b") == []


def test_up_to_date_and_pong_and_tags():
    assert rc.up_to_date("img:16", "img:16", "sha256:a", "sha256:a")
    assert not rc.up_to_date("img:16", "img:16", "sha256:a", "sha256:b")  # floating tag moved
    assert not rc.up_to_date("img:0.1.0", "img:0.2.0", "sha256:a", "sha256:a")
    assert not rc.up_to_date("img:16", "img:16", None, None)
    assert rc.is_pong(200, '{"message": "pong"}')
    assert not rc.is_pong(200, "<html>")
    assert not rc.is_pong(502, '{"message": "pong"}')
    assert rc.rollback_tag("01JABCDEF") == "nexaploy-cp-rollback:01jabcdef"
    assert rc.image_for_version("v0.2.0") == "ghcr.io/a7733mai/nexaploy-control-plane:0.2.0"
    assert rc.ping_url(8090) == "http://127.0.0.1:8090/api/method/ping"


def test_pick_release():
    rels = [
        {"tag_name": "v0.1.0", "prerelease": False},
        {"tag_name": "v0.3.0-beta.1", "prerelease": True},
        {"tag_name": "v0.2.0", "prerelease": False},
        {"tag_name": "v0.9.0", "draft": True},
        {"tag_name": "nightly", "prerelease": False},
    ]
    assert rc.pick_release(rels)["tag_name"] == "v0.2.0"
    assert rc.pick_release(rels, "beta")["tag_name"] == "v0.3.0-beta.1"
    assert rc.pick_release([{"tag_name": "v1.0.0-rc.1", "prerelease": True}, {"tag_name": "v1.0.0"}], "beta")["tag_name"] == "v1.0.0"
    assert rc.pick_release([]) is None


def test_source_kind(tmp_path):
    assert rc.source_kind("https://example.com/b.tar") == "url"
    assert rc.source_kind("/root/backup.tar") == "path"
    assert rc.source_kind(str(tmp_path)) == "path"
    assert rc.source_kind("nexaploy-backups:srv-1/nexaploy-cp/cp.example.com/20261104T120000Z") == "rclone"
    assert rc.source_kind(":s3,provider=AWS,env_auth=true:bucket/path") == "rclone"


def test_cp_env_roundtrip(tmp_path):
    assert rc.read_cp_env(tmp_path) == {}
    assert rc.set_cp_env(tmp_path, IMAGE="x") is False
    (tmp_path / "control-plane.env").write_text("DOMAIN=cp.example.com\nIMAGE=old\nPORT=8099\nADMIN_PASSWORD=pw\n")
    assert rc.set_cp_env(tmp_path, IMAGE=NEW)
    env = rc.read_cp_env(tmp_path)
    assert env["IMAGE"] == NEW and env["ADMIN_PASSWORD"] == "pw" and rc.cp_port(env) == 8099
    assert (tmp_path / "control-plane.env").stat().st_mode & 0o777 == 0o600
    assert rc.cp_port({}) == 8090 and rc.cp_port({"PORT": "x"}) == 8090 and rc.cp_port({}, 9000) == 9000


# --------------------------------------------------------------------------- job
def _frappe_backup(site_dir: Path, stamp="20261104_120000"):
    d = site_dir / "private" / "backups"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stamp}-cp_example_com-database.sql.gz").write_bytes(b"SQL")
    (d / f"{stamp}-cp_example_com-files.tar").write_bytes(b"PUB")
    (d / f"{stamp}-cp_example_com-private-files.tar").write_bytes(b"PRIV")
    (d / f"{stamp}-cp_example_com-site_config_backup.json").write_text(json.dumps({"encryption_key": KEY}))


@pytest.fixture
def cp(ctx, runner, settings, monkeypatch):
    make_bench(ctx, BENCH, image=OLD)
    site_dir = make_site(ctx, BENCH, CP, extra={"encryption_key": KEY})
    (settings.config_dir / "control-plane.env").write_text(
        f"DOMAIN={CP}\nBENCH={BENCH}\nIMAGE={OLD}\nPORT=8091\nADMIN_PASSWORD=pw\n")
    runner.on(["backup", "--with-files"], result=side_effect(lambda c: _frappe_backup(site_dir)))
    runner.on(["image", "inspect", "--format", "{{.Id}}", OLD], stdout="sha256:old\n")
    runner.on(["image", "inspect", "--format", "{{.Id}}", NEW], stdout="sha256:new\n")
    bench_running(runner)
    monkeypatch.setattr(ControlPlaneUpdate, "HEALTH_INTERVAL", 0)
    probes = []

    def probe(url, site, timeout=10.0):
        probes.append((url, site))
        return 200, '{"message": "pong"}', None

    monkeypatch.setattr(ControlPlaneUpdate, "probe", staticmethod(probe))
    return probes


def _idx(runner, needle, start=0):
    for i, line in enumerate(runner.cmdlines()):
        if i >= start and needle in line:
            return i
    raise AssertionError(f"{needle!r} not called:\n" + "\n".join(runner.cmdlines()))


def test_kind_registered():
    assert REGISTRY["controlplane.update"] is ControlPlaneUpdate
    assert ControlPlaneUpdate.steps == ("prepare", "backup", "pull", "switch", "migrate", "health", "finalize")


def test_update_success(ctx, worker, runner, settings, cp):
    job = run_job(ctx, worker, "controlplane.update", {"bench": BENCH, "site": CP, "image": NEW, "previous_image": OLD})
    assert job["status"] == "success", job
    r = job["result"]
    assert r["previous_image"] == OLD and r["image"] == NEW and r["updated"] is True
    assert r["rollback_image"].startswith("nexaploy-cp-rollback:")
    assert ctx.benches.image(BENCH) == NEW
    assert rc.read_cp_env(settings.config_dir)["IMAGE"] == NEW
    # order: pin old image -> backup -> pull -> configurator -> up -> migrate
    tag = _idx(runner, f"docker tag {OLD} nexaploy-cp-rollback:")
    backup = _idx(runner, f"--site {CP} backup --with-files")
    pull = _idx(runner, f"docker pull {NEW}")
    up = _idx(runner, "up -d", pull)
    mig = _idx(runner, f"--site {CP} migrate")
    assert tag < backup < pull < up < mig
    assert runner.called(f"docker image rm {r['rollback_image']}")
    assert cp == [("http://127.0.0.1:8091/api/method/ping", CP)]
    # the backup is staged like site.backup (kept locally: no backup storage configured)
    assert (Path(r["path"]) / "manifest.json").is_file() and r["uploaded"] is False
    blob = json.dumps(job)
    assert KEY not in blob and DB_PW not in blob


def test_update_up_to_date_skips_switch(ctx, worker, runner, cp):
    runner.on(["image", "inspect", "--format", "{{.Id}}", OLD], stdout="sha256:same\n")
    job = run_job(ctx, worker, "controlplane.update", {"bench": BENCH, "site": CP, "image": OLD, "backup": False})
    assert job["status"] == "success", job
    steps = {s["name"]: s["status"] for s in job["steps"]}
    assert job["result"]["up_to_date"] is True and job["result"]["updated"] is False
    assert not runner.called(f"--site {CP} migrate") and not runner.called("backup --with-files")
    assert "already up to date" in next(s["output"] for s in job["steps"] if s["name"] == "switch")
    assert steps["health"] == "success"


def test_update_pull_failure_changes_nothing(ctx, worker, runner, cp):
    runner.on(["pull", NEW], returncode=1, stderr="manifest unknown")
    runner.on(["image", "inspect", "--format", "{{.Id}}", NEW], returncode=1)
    job = run_job(ctx, worker, "controlplane.update", {"bench": BENCH, "site": CP, "image": NEW})
    assert job["status"] == "failure" and "cannot pull" in job["error"]
    assert ctx.benches.image(BENCH) == OLD
    assert not runner.called("up -d") and "rollback" not in job["result"]


def test_migrate_failure_rolls_back_and_restores(ctx, worker, runner, settings, cp):
    runner.on(["--site", CP, "migrate"], returncode=1, stderr="Traceback: boom", times=1)
    job = run_job(ctx, worker, "controlplane.update", {"bench": BENCH, "site": CP, "image": NEW})
    assert job["status"] == "failure", job
    r = job["result"]
    assert r["rollback"]["failed_step"] == "migrate"
    assert r["rollback"]["done"] == ["switch_back", "restore_database", "health"] and r["rolled_back"] is True
    assert "rolled back to " + OLD in job["error"] and "restored the database" in job["error"]
    assert ctx.benches.image(BENCH) == OLD
    assert rc.read_cp_env(settings.config_dir)["IMAGE"] == OLD  # finalize never ran
    # floating tag restored to the pinned content, old image up, then the DB-only restore
    back = _idx(runner, f"docker tag {r['rollback_image']} {OLD}")
    up = _idx(runner, "up -d", back)
    restore = _idx(runner, f"--site {CP} restore", up)
    args = runner.calls[restore].args
    assert args[args.index("restore") + 1].endswith("-database.sql.gz")
    assert "--with-public-files" not in args and args[args.index("--db-root-password") + 1] == DB_PW
    assert [s["status"] for s in job["steps"]][-3:] == ["failure", "skipped", "skipped"]
    assert DB_PW not in json.dumps(job)


def test_health_timeout_rolls_back(ctx, worker, runner, monkeypatch, cp):
    calls = {"n": 0}

    def probe(url, site, timeout=10.0):
        calls["n"] += 1
        # new image never answers; after the switch back the old one does
        return (200, '{"message": "pong"}', None) if ctx.benches.image(BENCH) == OLD else (None, "", "ConnectError")

    monkeypatch.setattr(ControlPlaneUpdate, "probe", staticmethod(probe))
    monkeypatch.setattr(ControlPlaneUpdate, "wait_healthy", _fast_wait(ControlPlaneUpdate.wait_healthy))
    job = run_job(ctx, worker, "controlplane.update", {"bench": BENCH, "site": CP, "image": NEW})
    assert job["status"] == "failure"
    assert job["result"]["rollback"]["failed_step"] == "health"
    assert job["result"]["rollback"]["done"] == ["switch_back", "restore_database", "health"]
    assert ctx.benches.image(BENCH) == OLD


def _fast_wait(orig):
    def wait(self, timeout=None):
        return orig(self, timeout=0)
    return wait


def test_switch_failure_rolls_back_without_restore(ctx, worker, runner, cp):
    runner.on(["up", "-d"], returncode=1, stderr="pull access denied", times=1)  # the new image only
    job = run_job(ctx, worker, "controlplane.update", {"bench": BENCH, "site": CP, "image": NEW})
    assert job["status"] == "failure", job
    assert job["result"]["rollback"]["done"] == ["switch_back", "health"]
    assert not runner.called(f"--site {CP} migrate") and not runner.called(f"--site {CP} restore")
    assert ctx.benches.image(BENCH) == OLD


def test_incomplete_rollback_is_reported(ctx, worker, runner, monkeypatch, cp):
    runner.on(["--site", CP, "migrate"], returncode=1, stderr="boom")
    monkeypatch.setattr(ControlPlaneUpdate, "probe", staticmethod(lambda url, site, timeout=10.0: (None, "", "down")))
    monkeypatch.setattr(ControlPlaneUpdate, "wait_healthy", _fast_wait(ControlPlaneUpdate.wait_healthy))
    job = run_job(ctx, worker, "controlplane.update", {"bench": BENCH, "site": CP, "image": NEW})
    assert job["status"] == "failure" and "ROLLBACK INCOMPLETE" in job["error"]
    assert job["result"]["rolled_back"] is False


def test_prechecks_and_api(ctx, settings, cp):
    with pytest.raises(NotFound):
        ctx.enqueue("controlplane.update", {"bench": "nope-bench", "site": CP, "image": NEW}, precheck=True)
    with pytest.raises(NotFound):
        ctx.enqueue("controlplane.update", {"bench": BENCH, "site": "other.example.com", "image": NEW}, precheck=True)
    client = TestClient(create_app(ctx=ctx))
    res = client.post("/v1/controlplane/update", json={"bench": BENCH, "site": CP, "image": NEW},
                      headers={"Authorization": f"Bearer {TOKEN}"})
    assert res.status_code == 202 and res.json()["kind"] == "controlplane.update"
    with pytest.raises(Conflict):
        ctx.enqueue("controlplane.update", {"bench": BENCH, "site": CP, "image": NEW}, precheck=True)
    bad = client.post("/v1/controlplane/update", json={"bench": BENCH, "site": CP, "image": "no tag!"},
                      headers={"Authorization": f"Bearer {TOKEN}"})
    assert bad.status_code == 422


# --------------------------------------------------------------------------- backup tarball / import
def _staged(ctx, site_dir):
    _frappe_backup(site_dir)
    src = site_dir / "private" / "backups"
    names = [p.name for p in src.iterdir()]
    ctx.backups.stage(BENCH, CP, "20261104T120000Z", src, names, OLD)
    return ctx.backups.backup_dir(BENCH, CP, "20261104T120000Z")


def test_export_and_import_roundtrip(ctx, tmp_path):
    make_bench(ctx, BENCH, image=OLD)
    d = _staged(ctx, make_site(ctx, BENCH, CP))
    tar = rc.export_tarball(d, tmp_path / "out", CP)
    assert tar.name == f"nexaploy-control-plane-{CP}-20261104T120000Z.tar"
    assert tar.stat().st_mode & 0o777 == 0o600
    # restore on a "new server" (another backups root), same domain
    dest_root = tmp_path / "new" / BENCH / CP
    ts, path = rc.import_backup(tar, dest_root, site=CP, bench=BENCH)
    assert ts == "20261104T120000Z" and path == dest_root / ts
    m = ctx.backups.verify(path)
    assert {f["type"] for f in m["files"]} == {"database", "public", "private", "config"}
    assert not any(p.name.startswith(".import") for p in dest_root.parent.iterdir())
    # a staged dir and a dir of staged dirs work too (newest wins)
    ts2, _ = rc.import_backup(d.parent, tmp_path / "n2" / BENCH / CP, site=CP, bench=BENCH)
    assert ts2 == "20261104T120000Z"


def test_import_plain_frappe_dir_and_rejects(ctx, tmp_path):
    raw = tmp_path / "raw"
    _frappe_backup(raw, stamp="20261101_010000")
    _frappe_backup(raw, stamp="20261103_010000")
    ts, path = rc.import_backup(raw / "private" / "backups", tmp_path / "dst" / BENCH / CP, site=CP, bench=BENCH)
    names = sorted(p.name for p in path.iterdir())
    assert all("20261103_010000" in n for n in names if n != "manifest.json")  # newest of each kind
    ctx.backups.verify(path)
    (tmp_path / "empty").mkdir()
    with pytest.raises(BackupError, match="no control plane backup"):
        rc.import_backup(tmp_path / "empty", tmp_path / "x", site=CP, bench=BENCH)
    evil = tmp_path / "evil.tar"
    with tarfile.open(evil, "w") as t:
        f = tmp_path / "payload"
        f.write_text("x")
        t.add(f, arcname="../escape-database.sql.gz")
    with pytest.raises(BackupError, match="unsafe"):
        rc.import_backup(evil, tmp_path / "y" / BENCH / CP, site=CP, bench=BENCH)


def test_import_detects_corruption(ctx, tmp_path):
    make_bench(ctx, BENCH, image=OLD)
    d = _staged(ctx, make_site(ctx, BENCH, CP))
    next(d.glob("*-database.sql.gz")).write_bytes(b"truncated")
    with pytest.raises(BackupError, match="sha256 mismatch"):
        rc.import_backup(d, tmp_path / "z" / BENCH / CP, site=CP, bench=BENCH)


# --------------------------------------------------------------------------- CLI
def test_cli_import_and_repoint(ctx, settings, tmp_path, monkeypatch):
    from nexaploy_agent import cli as cli_mod
    from nexaploy_agent.config import dump_toml

    make_bench(ctx, BENCH, image=OLD)
    d = _staged(ctx, make_site(ctx, BENCH, CP))
    tar = rc.export_tarball(d, tmp_path / "out", CP)
    cfg = tmp_path / "agent.toml"
    cfg.write_text(dump_toml({"agent": {"root": str(tmp_path / "newroot"), "token": "t"},
                              "control_plane": {"url": "http://127.0.0.1:8090", "token": "srv", "site_name": CP}}))
    monkeypatch.setattr("nexaploy_agent.fsutil.chown", lambda *a, **k: None)
    res = CliRunner().invoke(cli_mod.app, ["--config", str(cfg), "controlplane", "import-backup", str(tar),
                                           "--bench", BENCH, "--site", CP])
    assert res.exit_code == 0, res.output
    assert res.output.strip().splitlines()[-1] == "20261104T120000Z"
    assert (tmp_path / "newroot" / "backups" / BENCH / CP / "20261104T120000Z" / "manifest.json").is_file()

    res = CliRunner().invoke(cli_mod.app, ["--config", str(cfg), "controlplane", "repoint", "--url", "https://cp.example.com/"])
    assert res.exit_code == 0, res.output
    import tomllib
    data = tomllib.loads(cfg.read_text())
    assert data["control_plane"] == {"url": "https://cp.example.com", "token": "srv", "site_name": ""}


def test_cli_update_without_cp_env_explains(tmp_path):
    from nexaploy_agent import cli as cli_mod
    from nexaploy_agent.config import dump_toml

    cfg = tmp_path / "agent.toml"
    cfg.write_text(dump_toml({"agent": {"root": str(tmp_path), "token": "t"}}))
    res = CliRunner().invoke(cli_mod.app, ["--config", str(cfg), "controlplane", "update", "--image", NEW])
    assert res.exit_code == 1 and "no control plane on this server" in res.output
