"""Server maintenance: OS updates, health facts and cleanup (runtime/os_updates.py, health.py,
cleanup.py, jobs/maintenance_jobs.py)."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nexaploy_agent.jobs import maintenance_jobs as mj
from nexaploy_agent.jobs.registry import REGISTRY
from nexaploy_agent.runtime import cleanup as cl
from nexaploy_agent.runtime import health as hl
from nexaploy_agent.runtime import os_updates as ou

from .conftest import make_bench, make_site, run_job

UPGRADABLE = """Listing... Done
openssl/jammy-updates,jammy-security 3.0.2-0ubuntu1.15 amd64 [upgradable from: 3.0.2-0ubuntu1.14]
curl/jammy-updates 7.81.0-1ubuntu1.16 amd64 [upgradable from: 7.81.0-1ubuntu1.15]
libssl3/bookworm-security 3.0.14-1~deb12u1 amd64 [upgradable from: 3.0.13-1~deb12u1]
"""


# --------------------------------------------------------------------------- fixtures
@pytest.fixture
def os_paths(tmp_path: Path) -> ou.OsPaths:
    base = tmp_path / "host"
    (base / "boot").mkdir(parents=True)
    (base / "apt.conf.d").mkdir()
    (base / "os-release").write_text('ID=ubuntu\nID_LIKE=debian\nVERSION_ID="22.04"\n')
    (base / "stat").write_text("cpu 1 2 3\nbtime 1762200000\nprocesses 5\n")
    for k in ("5.15.0-91-generic", "5.15.0-101-generic", "5.15.0-97-generic"):
        (base / "boot" / f"vmlinuz-{k}").write_text("")
    return ou.OsPaths(os_release=base / "os-release", boot_dir=base / "boot",
                      reboot_required=base / "reboot-required", reboot_required_pkgs=base / "reboot-required.pkgs",
                      proc_stat=base / "stat", apt_conf_dir=base / "apt.conf.d")


@pytest.fixture
def osu(ctx, runner, os_paths):
    clock = [1_000_000.0]
    svc = ou.OsUpdates(ctx.settings.data_dir, ctx.runner, paths=os_paths, clock=lambda: clock[0],
                       which=lambda name: f"/usr/bin/{name}")
    svc.running_kernel = lambda: "5.15.0-97-generic"  # type: ignore[method-assign]
    svc._clock = clock  # type: ignore[attr-defined]
    ctx.os_updates = svc
    runner.on(["apt", "list", "--upgradable"], stdout=UPGRADABLE)
    return svc


# --------------------------------------------------------------------------- os updates (pure)
def test_parse_upgradable_counts_security():
    pkgs = ou.parse_upgradable(UPGRADABLE)
    assert [p["name"] for p in pkgs] == ["openssl", "curl", "libssl3"]
    assert [p["security"] for p in pkgs] == [True, False, True]


def test_kernel_natural_sort(os_paths):
    assert ou.newest_kernel(os_paths.boot_dir) == "5.15.0-101-generic"
    assert ou.natural_key("5.15.0-101-generic") > ou.natural_key("5.15.0-97-generic")


def test_boot_time_reboot_required_and_unattended(os_paths):
    assert ou.boot_time(os_paths.proc_stat) == 1762200000
    assert ou.read_reboot_required(os_paths.reboot_required, os_paths.reboot_required_pkgs) == (False, [])
    os_paths.reboot_required.write_text("*** System restart required ***\n")
    os_paths.reboot_required_pkgs.write_text("linux-base\nlibc6\nlinux-base\n")
    assert ou.read_reboot_required(os_paths.reboot_required, os_paths.reboot_required_pkgs) == (True, ["libc6", "linux-base"])
    assert ou.unattended_enabled(os_paths.apt_conf_dir) is False
    (os_paths.apt_conf_dir / "20auto-upgrades").write_text(ou.auto_upgrades_content(True))
    assert ou.unattended_enabled(os_paths.apt_conf_dir) is True
    assert 'Unattended-Upgrade::Automatic-Reboot "false";' in ou.unattended_content()


def test_status_cached_and_invalidated(osu, runner):
    st = osu.status()
    assert st["supported"] and st["pending"] == 3 and st["security"] == 2
    assert st["running_kernel"] == "5.15.0-97-generic" and st["newest_kernel"] == "5.15.0-101-generic"
    assert st["kernel_update_pending"] and st["reboot_required"]  # newer kernel installed
    assert st["boot_time"] == 1762200000 and st["checked_at"]
    assert len(runner.find("apt list")) == 1
    osu.status()
    assert len(runner.find("apt list")) == 1  # cached
    osu._clock[0] += ou.CACHE_TTL_SECONDS + 1
    osu.status()
    assert len(runner.find("apt list")) == 2  # expired
    osu.invalidate()
    osu.status()
    assert len(runner.find("apt list")) == 3


def test_status_unsupported(ctx, os_paths):
    os_paths.os_release.write_text("ID=fedora\n")
    svc = ou.OsUpdates(ctx.settings.data_dir, ctx.runner, paths=os_paths, which=lambda n: "/usr/bin/" + n)
    assert svc.status() == {"supported": False}


def test_heartbeat_carries_os_updates_and_health(ctx, osu):
    from nexaploy_agent.control_plane import ControlPlaneService

    sent = {}

    class Client:
        enabled = True

        def heartbeat(self, facts, benches, usage=None, tls=None):
            sent.update(facts)

    svc = ControlPlaneService(ctx, client=Client())
    svc.heartbeat_once()
    assert sent["os_updates"]["pending"] == 3
    assert "filesystems" in sent["health"] and sent["health"]["agent_time"].endswith("Z")


# --------------------------------------------------------------------------- os update jobs
def test_os_update_all_with_reboot_if_required(ctx, worker, runner, osu, monkeypatch):
    monkeypatch.setattr(mj, "systemd_available", lambda: True)
    job = run_job(ctx, worker, "server.os_update", {"mode": "all", "reboot": "if_required"})
    assert job["status"] == "success", job
    assert [s["name"] for s in job["steps"]] == ["update_lists", "upgrade", "cleanup", "status", "reboot"]
    up = runner.find("full-upgrade")[0]
    assert "DPkg::Lock::Timeout=600" in up.cmdline and "--force-confold" in up.cmdline
    assert up.env["DEBIAN_FRONTEND"] == "noninteractive" and up.env["NEEDRESTART_MODE"] == "a"
    assert runner.called("apt-get -o DPkg::Lock::Timeout=600 update")
    assert runner.called("autoremove --purge")
    reboot = runner.find("systemctl reboot")[0]
    assert reboot.args[0] == "systemd-run" and "--on-active=60s" in reboot.args
    assert job["result"]["reboot_scheduled"] is True


def test_os_update_never_reboots(ctx, worker, runner, osu, monkeypatch):
    monkeypatch.setattr(mj, "systemd_available", lambda: True)
    job = run_job(ctx, worker, "server.os_update", {"mode": "all", "reboot": "never"})
    assert job["status"] == "success"
    assert not runner.called("systemctl reboot") and job["result"]["reboot_scheduled"] is False


def test_os_update_security_needs_unattended(ctx, worker, runner, osu):
    runner.on(["which", "unattended-upgrade"], returncode=1)
    job = run_job(ctx, worker, "server.os_update", {"mode": "security", "reboot": "never"})
    assert job["status"] == "failure" and "unattended-upgrades is not installed" in job["error"]
    runner.on(["which", "unattended-upgrade"], stdout="/usr/bin/unattended-upgrade\n")
    job = run_job(ctx, worker, "server.os_update", {"mode": "security", "reboot": "never"})
    assert job["status"] == "success" and runner.called("unattended-upgrade -v")


def test_os_update_unsupported_fails(ctx, worker, osu, os_paths):
    os_paths.os_release.write_text("ID=alpine\n")
    job = run_job(ctx, worker, "server.os_update", {})
    assert job["status"] == "failure" and "apt based" in job["error"]


def test_reboot_fallback_shutdown(ctx, worker, runner, osu, monkeypatch):
    monkeypatch.setattr(mj, "systemd_available", lambda: False)
    job = run_job(ctx, worker, "server.os_reboot", {"delay_seconds": 30})
    assert job["status"] == "success" and runner.called("shutdown -r +1")


def test_os_configure_writes_apt_files(ctx, worker, runner, osu, os_paths):
    runner.on(["which", "unattended-upgrade"], stdout="/usr/bin/unattended-upgrade\n")
    job = run_job(ctx, worker, "server.os_configure", {"unattended_security": True})
    assert job["status"] == "success", job
    assert ou.unattended_enabled(os_paths.apt_conf_dir)
    assert "Automatic-Reboot \"false\"" in (os_paths.apt_conf_dir / "52nexaploy-unattended").read_text()
    job = run_job(ctx, worker, "server.os_configure", {"unattended_security": False})
    assert not ou.unattended_enabled(os_paths.apt_conf_dir)
    assert job["result"]["os_updates"]["unattended_security"] is False


def test_registry_has_maintenance_kinds():
    for kind in ("server.os_update", "server.os_reboot", "server.os_configure", "server.diagnose", "server.cleanup"):
        assert kind in REGISTRY


# --------------------------------------------------------------------------- health
def test_health_parsers():
    units = hl.parse_failed_units(
        "● nexaploy-agent-worker.service loaded failed failed Nexaploy worker\n"
        "snapd.service loaded failed failed Snap\n"
        "rclone-site-a.service loaded failed failed mount\n"
        "docker.service loaded failed failed Docker\n")
    assert units == ["docker.service", "nexaploy-agent-worker.service", "rclone-site-a.service"]
    assert hl.parse_ntp("yes\n") is True and hl.parse_ntp("no") is False and hl.parse_ntp("") is None
    assert hl.parse_size("1.5GB") == 1_500_000_000 and hl.parse_size("512kB") == 512_000 and hl.parse_size("x") == 0
    df = hl.parse_system_df(
        '{"Type":"Images","TotalCount":"5","Size":"4GB","Reclaimable":"1GB (25%)"}\n'
        '{"Type":"Build Cache","TotalCount":"10","Size":"2GB","Reclaimable":"2GB"}\n')
    assert df["images"]["reclaimable"] == 1_000_000_000 and df["build_cache"]["size"] == 2_000_000_000
    assert df["reclaimable"] == 3_000_000_000
    n, recent = hl.count_oom("[1] Out of memory: Killed process 123 (python)\n[2] ok\n[3] oom-kill:constraint\n")
    assert n == 2 and len(recent) == 2
    now = datetime(2026, 11, 4, tzinfo=timezone.utc)
    tls = hl.tls_summary({"hosts": {
        "a.example.com": {"trusted": True, "not_after": "2026-11-10T00:00:00Z"},
        "b.example.com": {"trusted": False},
        "c.example.com": {"trusted": True, "not_after": "2027-02-01T00:00:00Z"},
    }}, now)
    assert tls == {"hosts": 3, "failing": ["b.example.com"], "expiring": ["a.example.com"]}


def test_light_health(ctx, runner, tmp_path):
    mem = tmp_path / "meminfo"
    mem.write_text("MemTotal: 1000 kB\nMemAvailable: 250 kB\nSwapTotal: 100 kB\nSwapFree: 50 kB\n")
    runner.on(["systemctl", "list-units"], stdout="nexaploy-agent-api.service loaded failed failed x\n")
    runner.on(["timedatectl"], stdout="no\n")
    clock = [100.0]
    coll = hl.HealthCollector(ctx.settings, ctx.runner, clock=lambda: clock[0], meminfo_path=mem)
    h = coll.light()
    assert h["mem_used_percent"] == 75.0 and h["swap_used_percent"] == 50.0
    assert h["failed_units"] == ["nexaploy-agent-api.service"] and h["ntp_synchronized"] is False
    assert h["filesystems"] and {"used_percent", "inodes_used_percent"} <= set(h["filesystems"][0])
    coll.light()
    assert len(runner.find("systemctl list-units")) == 1  # throttled
    clock[0] += hl.UNIT_INTERVAL_SECONDS
    coll.light()
    assert len(runner.find("systemctl list-units")) == 2


def test_diagnose_job(ctx, worker, runner, osu):
    runner.on(["system", "df"], stdout='{"Type":"Images","TotalCount":"1","Size":"1GB","Reclaimable":"0B"}\n')
    runner.on(["mariadb"], stdout="threads_connected\t12\nmax_used_connections\t40\nmax_connections\t151\nlog_bin\t1\n")
    job = run_job(ctx, worker, "server.diagnose", {})
    assert job["status"] == "success", job
    h = job["result"]["health"]
    assert h["full"] and h["mariadb"]["threads_connected"] == 12 and h["mariadb"]["max_connections"] == 151
    mariadb_call = runner.find("mariadb")[0]
    assert mariadb_call.env == {"MYSQL_PWD": "db-root-pw-SECRET"} and "db-root-pw-SECRET" not in mariadb_call.cmdline
    assert job["result"]["os_updates"]["pending"] == 3


# --------------------------------------------------------------------------- cleanup: guard
def test_path_guard_refuses_protected_paths(settings, tmp_path):
    root = settings.root
    g = cl.PathGuard(root)
    site = root / "benches" / "erp-v15" / "sites" / "a.example.com"
    for d in ("logs", "private/backups", "public/files"):
        (site / d).mkdir(parents=True, exist_ok=True)
    (site / "logs" / "web.log").write_text("x")
    (site / "private" / "backups" / "db.sql.gz").write_text("x")
    (site / "public" / "files" / "a.log").write_text("x")
    (site / "site_config.json").write_text("{}")
    assert g.log_file(site / "logs" / "web.log")
    for bad in (site / "private" / "backups" / "db.sql.gz", site / "public" / "files" / "a.log",
                site / "site_config.json", root / "config" / "db.env"):
        with pytest.raises(cl.UnsafePath):
            g.log_file(bad)
    (root / "db" / "data").mkdir(parents=True)
    (root / "db" / "data" / "ibdata1").write_text("x")
    with pytest.raises(cl.UnsafePath):
        g.log_file(root / "db" / "data" / "ibdata1")
    (root / "proxy" / "data" / "certificates").mkdir(parents=True)
    with pytest.raises(cl.UnsafePath):
        g.backup_dir(root / "proxy" / "data" / "certificates")
    # a logs dir that is a symlink into private files is rejected by its real path
    other = root / "benches" / "erp-v15" / "sites" / "b.example.com"
    other.mkdir(parents=True)
    os.symlink(site / "private" / "backups", other / "logs")
    with pytest.raises(cl.UnsafePath):
        g.log_file(other / "logs" / "db.sql.gz")
    link = site / "logs" / "link.log"
    os.symlink(root / "config" / "db.env", link)
    with pytest.raises(cl.UnsafePath):
        g.log_file(link)
    with pytest.raises(cl.UnsafePath):
        g.docker_log(Path("/etc/passwd"), "a" * 64)


# --------------------------------------------------------------------------- cleanup: pure selection
def _img(id_, repo, tag, created, size="1GB"):
    return {"ID": id_, "Repository": repo, "Tag": tag, "CreatedAt": created, "Size": size}


def test_select_images_rules():
    now = datetime(2026, 11, 4, tzinfo=timezone.utc).timestamp()
    old = "2026-10-01 10:00:00 +0000 UTC"
    older = "2026-09-01 10:00:00 +0000 UTC"
    new = "2026-11-03 23:00:00 +0000 UTC"
    images = [
        _img("sha256:used", "nexaploy/bench", "erp-v15-20261001T1000", old),
        _img("sha256:env", "nexaploy/bench", "erp-v15-20260915T1000", older),
        _img("sha256:a", "nexaploy/bench", "erp-v15-20260901T1000", older),
        _img("sha256:b", "nexaploy/bench", "erp-v15-20260902T1000", "2026-09-02 10:00:00 +0000 UTC"),
        _img("sha256:fresh", "nexaploy/bench", "erp-v15-20261103T2300", new),
        _img("sha256:db", "mariadb", "10.6", older),
        _img("sha256:redis", "redis", "6.2-alpine", older),
        _img("sha256:dangling", "<none>", "<none>", older, "300MB"),
    ]
    chosen, notes = cl.select_images(images, {"sha256:used"}, {"nexaploy/bench:erp-v15-20260915T1000"}, now, 72)
    refs = [c["ref"] for c in chosen]
    # newest unused of the family (b) is kept as rollback target; a and the dangling one go
    assert refs == ["sha256:dangling", "nexaploy/bench:erp-v15-20260901T1000"]
    assert any("rollback target" in n for n in notes) and any("younger than" in n for n in notes)


def test_select_oneoff_containers():
    now = datetime(2026, 11, 4, tzinfo=timezone.utc).timestamp()
    rows = [
        {"ID": "1", "Names": "erp-v15-backend-run-abc", "State": "exited", "CreatedAt": "2026-11-01 00:00:00 +0000 UTC",
         "Labels": "com.docker.compose.project=erp-v15,com.docker.compose.oneoff=True", "Size": "10kB (virtual 1GB)"},
        {"ID": "2", "Names": "erp-v15-configurator", "State": "exited", "CreatedAt": "2026-11-01 00:00:00 +0000 UTC",
         "Labels": "com.docker.compose.project=erp-v15,com.docker.compose.service=configurator", "Size": "0B"},
        {"ID": "3", "Names": "erp-v15-backend", "State": "exited", "CreatedAt": "2026-11-01 00:00:00 +0000 UTC",
         "Labels": "com.docker.compose.project=erp-v15,com.docker.compose.service=backend", "Size": "0B"},
        {"ID": "4", "Names": "someone-else", "State": "exited", "CreatedAt": "2026-11-01 00:00:00 +0000 UTC",
         "Labels": "com.docker.compose.project=other,com.docker.compose.oneoff=True", "Size": "0B"},
        {"ID": "5", "Names": "erp-v15-backend-run-new", "State": "exited", "CreatedAt": "2026-11-03 23:59:00 +0000 UTC",
         "Labels": "com.docker.compose.project=erp-v15,com.docker.compose.oneoff=True", "Size": "0B"},
    ]
    picked = cl.select_oneoff_containers(rows, {"erp-v15"}, now)
    assert [c["id"] for c in picked] == ["1", "2"] and picked[0]["size"] == 10_000


def test_merge_daemon_json():
    new, _ = cl.merge_daemon_json(None)
    assert new == {"log-opts": {"max-size": "50m", "max-file": "3"}}
    new, _ = cl.merge_daemon_json('{"data-root": "/data/docker", "log-opts": {"labels": "x"}}')
    assert new["data-root"] == "/data/docker" and new["log-opts"] == {"labels": "x", "max-size": "50m", "max-file": "3"}
    assert cl.merge_daemon_json('{"log-opts": {"max-size": "10m"}}')[0] is None
    assert cl.merge_daemon_json('{"log-driver": "journald"}')[0] is None
    assert cl.merge_daemon_json("{broken")[0] is None


def test_select_backups_never_newest_or_not_uploaded():
    now = datetime(2026, 11, 4, tzinfo=timezone.utc)
    stamps = ["20261001T000000Z", "20261002T000000Z", "20261003T000000Z", "20261103T000000Z"]
    uploaded = {"20261001T000000Z", "20261003T000000Z", "20261103T000000Z"}
    assert cl.select_backups(stamps, uploaded, 14, now) == ["20261001T000000Z", "20261003T000000Z"]
    assert cl.select_backups(["20261001T000000Z"], {"20261001T000000Z"}, 1, now) == []


# --------------------------------------------------------------------------- cleanup: files
def _age(path: Path, days: float) -> None:
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def _cleaner(ctx, tmp_path, dry_run, **kw):
    opts = cl.CleanupOptions(dry_run=dry_run, **kw)
    return cl.Cleaner(ctx, opts, daemon_json=tmp_path / "daemon.json", docker_root=tmp_path / "docker",
                      apt_archives=tmp_path / "apt", which=lambda n: "/usr/bin/" + n)


def test_bench_logs(ctx, tmp_path):
    bench = make_bench(ctx, "erp-v15")
    site = make_site(ctx, "erp-v15", "a.example.com")
    (site / "logs").mkdir()
    (site / "private" / "files").mkdir(parents=True)
    old_rotated = bench / "logs" / "web.log.1"
    bench_logs = bench / "logs"
    bench_logs.mkdir(exist_ok=True)
    old_rotated.write_text("x" * 100)
    _age(old_rotated, 30)
    fresh_rotated = bench_logs / "worker.log.2"
    fresh_rotated.write_text("y")
    big = site / "logs" / "frappe.log"
    big.write_bytes(b"z" * (11 * 1024 * 1024))
    private = site / "private" / "files" / "old.log.1"
    private.write_text("keep")
    _age(private, 300)
    dry = _cleaner(ctx, tmp_path, True, bench_log_max_mb=10).run("bench_logs")
    assert dry["items"] == 2 and old_rotated.exists() and big.stat().st_size > 0
    res = _cleaner(ctx, tmp_path, False, bench_log_max_mb=10).run("bench_logs")
    assert res["items"] == 2 and res["freed_bytes"] >= 11 * 1024 * 1024
    assert not old_rotated.exists() and fresh_rotated.exists() and big.exists() and big.stat().st_size == 0
    assert private.exists() and (site / "site_config.json").exists()


def test_backups_local(ctx, tmp_path):
    base = ctx.settings.backups_dir / "erp-v15" / "a.example.com"
    now = datetime.now(timezone.utc)
    stamps = [(now - timedelta(days=d)).strftime("%Y%m%dT%H%M%SZ") for d in (40, 30, 20, 1)]
    for i, ts in enumerate(stamps):
        d = base / ts
        d.mkdir(parents=True)
        (d / "db.sql.gz").write_bytes(b"x" * 1000)
        if i != 1:  # stamps[1] never reached the remote: it is the only copy
            (d / ".uploaded").write_text("ok")
    res = _cleaner(ctx, tmp_path, False, backup_local_keep_days=14).run("backups_local")
    assert res["items"] == 2
    assert not (base / stamps[0]).exists() and (base / stamps[1]).exists()
    assert not (base / stamps[2]).exists() and (base / stamps[3]).exists()


def test_tmp_and_agent_data(ctx, tmp_path):
    pitr = ctx.settings.backups_dir / ".pitr"
    stale = pitr / "01JAAAAAAAAAAAAAAAAAAAAAAA"
    stale.mkdir(parents=True)
    (stale / "replay.sql").write_text("x" * 50)
    _age(stale, 3)
    running = ctx.store.create("site.restore_pitr", {}, ["x"])
    ctx.store.claim_next()
    live = pitr / running["id"]
    live.mkdir()
    _age(live, 3)
    res = _cleaner(ctx, tmp_path, False).run("tmp")
    assert not stale.exists() and live.exists() and res["items"] == 1
    old = ctx.store.create("site.usage", {}, [])
    ctx.store.finish(old["id"], "success")
    ctx.store._conn().execute("UPDATE jobs SET ended_at = '2020-01-01T00:00:00.000Z' WHERE id = ?", (old["id"],))
    dry = _cleaner(ctx, tmp_path, True).run("agent_data")
    assert dry["items"] == 1 and ctx.store.get(old["id"])
    res = _cleaner(ctx, tmp_path, False).run("agent_data")
    assert res["items"] == 1
    with pytest.raises(KeyError):
        ctx.store.get(old["id"])


def test_docker_targets(ctx, runner, tmp_path):
    make_bench(ctx, "erp-v15")
    runner.on(["ps", "-aq"], stdout="c1\nc2\n")
    runner.on(["inspect", "--format", "{{.Image}}"], stdout="sha256:used\nsha256:used\n")
    runner.on(["image", "ls"], stdout="\n".join(json.dumps(r) for r in (
        {"ID": "sha256:used", "Repository": "nexaploy/bench", "Tag": "erp-v15-20261001T1000",
         "CreatedAt": "2026-10-01 10:00:00 +0000 UTC", "Size": "2GB"},
        {"ID": "sha256:dangling", "Repository": "<none>", "Tag": "<none>",
         "CreatedAt": "2026-10-01 10:00:00 +0000 UTC", "Size": "500MB"},
    )) + "\n")
    res = _cleaner(ctx, tmp_path, False).run("docker_images")
    assert res["items"] == 1 and res["freed_bytes"] == 500_000_000
    rm = runner.find("image rm")
    assert [c.args for c in rm] == [["docker", "image", "rm", "sha256:dangling"]]
    assert not any("-f" in c.args for c in runner.calls if "rm" in c.args)
    # build cache
    runner.on(["builder", "prune"], stdout="ID\nabcdefabcdefabcdefabcdef\nTotal:\t1.5GB\n")
    res = _cleaner(ctx, tmp_path, False).run("docker_build_cache")
    assert res["freed_bytes"] == 1_500_000_000
    # docker logs: truncate a big json log of a running container, write daemon.json, never restart docker
    cid = "a" * 64
    log = tmp_path / "docker" / "containers" / cid / f"{cid}-json.log"
    log.parent.mkdir(parents=True)
    log.write_bytes(b"l" * (11 * 1024 * 1024))
    runner.on(["ps", "-q", "--no-trunc"], stdout=cid + "\n")
    runner.on(["info", "--format"], stdout=str(tmp_path / "docker") + "\n")
    runner.on(lambda a: "{{.Id}}|{{.LogPath}}|{{.HostConfig.LogConfig.Type}}" in a,
              stdout=f"{cid}|{log}|json-file\n")
    res = _cleaner(ctx, tmp_path, False, docker_log_max_mb=10).run("docker_logs")
    assert log.stat().st_size == 0 and res["items"] == 1
    assert json.loads((tmp_path / "daemon.json").read_text())["log-opts"]["max-size"] == "50m"
    assert not any("restart" in c.cmdline and "docker" in c.cmdline and "systemctl" in c.cmdline for c in runner.calls)
    assert any("docker restart" in n for n in res["notes"])


def test_cleanup_job_dry_run_changes_nothing(ctx, worker, runner, osu, tmp_path, monkeypatch):
    monkeypatch.setattr(cl, "DAEMON_JSON", tmp_path / "daemon.json")
    bench = make_bench(ctx, "erp-v15")
    (bench / "logs").mkdir(exist_ok=True)
    f = bench / "logs" / "web.log.3"
    f.write_text("x" * 10)
    _age(f, 60)
    job = run_job(ctx, worker, "server.cleanup", {"targets": ["journal", "bench_logs"], "dry_run": True})
    assert job["status"] == "success", job
    assert [s["name"] for s in job["steps"]] == ["bench_logs", "journal", "summary"]
    assert f.exists() and job["result"]["dry_run"] is True
    assert job["result"]["targets"]["bench_logs"]["freed_bytes"] == 10
    assert not runner.called("--vacuum")
    job = run_job(ctx, worker, "server.cleanup", {"targets": ["bench_logs"], "dry_run": False})
    assert not f.exists() and job["result"]["freed_bytes"] == 10


def test_cleanup_rejects_unknown_target(ctx):
    with pytest.raises(Exception):
        ctx.enqueue("server.cleanup", {"targets": ["/etc"]})
