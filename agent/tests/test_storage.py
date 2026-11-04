"""Attachments offload / replication / trash (ARCHITECTURE.md §15)."""

import json
from pathlib import Path

import pytest

from nexaploy_agent import validation as v
from nexaploy_agent.jobs import storage_jobs
from nexaploy_agent.jobs.registry import REGISTRY
from nexaploy_agent.runtime.docker import Call, Result
from nexaploy_agent.runtime.storage import (
    PROVIDER_PRESETS, Profile, check_argv, mount_argv, parse_lsjson, parse_trash_item, propagation_ok,
    prune_trash_argv, rclone_profile_section, sync_argv, systemd_unit, trash_plan, unit_name, validate_policy,
)

from .conftest import make_bench, make_site, run_job, side_effect

SECRET = "S3-SECRET-abcdef0123456789"
SITE = "a.example.com"


def prof(name, preset="Other", **kw):
    data = {"name": name, "preset": preset, "bucket": f"{name}-bucket", "access_key_id": f"AK-{name}",
            "secret_access_key": SECRET, "endpoint": "s3.example.net"}
    data.update(kw)
    return data


PROFILES = [
    prof("de-primary", "Hetzner", region="fsn1", endpoint=None, prefix="files"),
    prof("us-replica", "Wasabi", region="us-east-1", endpoint=None),
    prof("us-trash", "B2", region="us-west-004", endpoint=None),
]
POLICY = {"site": SITE, "profiles": PROFILES, "primary": "de-primary", "replica": "us-replica",
          "trash": "us-trash", "trash_retention_days": 30}


# --------------------------------------------------------------------------- pure logic
@pytest.mark.parametrize("preset,kw,provider,endpoint,path_style", [
    ("AWS", {"region": "eu-central-1", "endpoint": None}, "AWS", None, "false"),
    ("Wasabi", {"region": "eu-central-2", "endpoint": None}, "Wasabi", "s3.eu-central-2.wasabisys.com", "true"),
    ("B2", {"region": "us-west-004", "endpoint": None}, "Other", "s3.us-west-004.backblazeb2.com", "true"),
    ("Hetzner", {"region": "fsn1", "endpoint": None}, "Other", "fsn1.your-objectstorage.com", "true"),
    ("R2", {"endpoint": "https://acc.r2.cloudflarestorage.com"}, "Cloudflare",
     "https://acc.r2.cloudflarestorage.com", "true"),
    ("MinIO", {"endpoint": "http://minio:9000"}, "Minio", "http://minio:9000", "true"),
    ("IDrive", {"endpoint": "x7a1.fra.idrivee2-30.com"}, "IDrive", "x7a1.fra.idrivee2-30.com", "true"),
])
def test_presets_to_rclone_section(preset, kw, provider, endpoint, path_style):
    p = Profile.from_params(prof("p1", preset, **kw))
    s = rclone_profile_section(p)
    assert s["type"] == "s3" and s["provider"] == provider and s["env_auth"] == "false"
    assert s.get("endpoint") == endpoint
    assert s["force_path_style"] == path_style
    assert s["secret_access_key"] == SECRET and s["acl"] == "private"
    if preset == "R2":
        assert s["region"] == "auto"


def test_preset_requirements_and_overrides():
    assert set(PROVIDER_PRESETS) == {"AWS", "IDrive", "Wasabi", "B2", "R2", "MinIO", "Hetzner", "Other"}
    with pytest.raises(v.ValidationError, match="needs a region"):
        Profile.from_params(prof("p1", "AWS", endpoint=None))
    with pytest.raises(v.ValidationError, match="needs a endpoint"):
        Profile.from_params(prof("p1", "MinIO", endpoint=None))
    with pytest.raises(v.ValidationError, match="unknown storage provider"):
        Profile.from_params(prof("p1", "Dropbox"))
    with pytest.raises(v.ValidationError):
        Profile.from_params(prof("Bad_Name"))
    with pytest.raises(v.ValidationError):
        Profile.from_params(prof("p1", bucket="UPPER"))
    with pytest.raises(v.ValidationError):
        Profile.from_params(prof("p1", secret_access_key="a\nb"))
    with pytest.raises(v.ValidationError):
        Profile.from_params(prof("p1", prefix="../x"))
    p = Profile.from_params(prof("p1", "AWS", region="us-east-1", endpoint=None, path_style=True))
    assert rclone_profile_section(p)["force_path_style"] == "true"


def test_profile_paths():
    p = Profile.from_params(prof("de-primary", prefix="/files/"))
    assert p.remote == "nxs-de-primary"
    assert p.site_root(SITE) == "nxs-de-primary:de-primary-bucket/files/a.example.com"
    assert p.site_root(SITE, "private") == "nxs-de-primary:de-primary-bucket/files/a.example.com/private"
    with pytest.raises(v.ValidationError):
        p.site_root(SITE, "backups")
    with pytest.raises(v.ValidationError):
        p.site_root("../etc")


def test_validate_policy():
    validate_policy("a", None, None)
    validate_policy("a", "b", "c", 30)
    for args, msg in [
        ((None, None, None), "primary"),
        (("a", "a", None), "different profile"),
        (("a", None, "c", 30), "needs a replica"),
        (("a", "b", "b", 30), "different profile"),
        (("a", "b", "c", 0), "trash_retention_days"),
    ]:
        with pytest.raises(v.ValidationError, match=msg):
            validate_policy(*args)


def test_mount_argv_and_unit():
    argv = mount_argv("rclone", "/c/storage.conf", "nxs-p:b/a.example.com/public", "/s/public/files",
                      "/cache", "/log", cache_max_size="512M")
    assert argv[:6] == ["rclone", "--config", "/c/storage.conf", "mount", "nxs-p:b/a.example.com/public",
                        "/s/public/files"]
    joined = " ".join(argv)
    for flag in ("--allow-other", "--uid 3113", "--gid 3113", "--vfs-cache-mode full",
                 "--vfs-cache-max-size 512M", "--cache-dir /cache", "--poll-interval 0"):
        assert flag in joined
    assert "--allow-non-empty" not in argv
    with pytest.raises(v.ValidationError):
        mount_argv("rclone", "c", "r", "m", "c", "l", cache_max_size="1G; rm")
    unit = systemd_unit("files", argv, "/s/public/files", "/usr/bin/fusermount3")
    assert "Type=notify" in unit and "Restart=always" in unit and "Before=docker.service" in unit
    assert "ExecStart=rclone --config /c/storage.conf mount" in unit
    assert "ExecStartPre=-/usr/bin/fusermount3 -uz /s/public/files" in unit
    assert unit_name(SITE, "public") == "nexaploy-files-a-example-com-public.service"


def test_sync_argv():
    argv = sync_argv("rclone", "c", "nxs-a:b/s/public", "nxs-r:b/s/public", backup_dir="nxs-r:b/.t/s/T/public",
                     max_delete=100)
    assert argv[3:6] == ["sync", "nxs-a:b/s/public", "nxs-r:b/s/public"]
    assert argv[argv.index("--backup-dir") + 1] == "nxs-r:b/.t/s/T/public"
    assert argv[argv.index("--max-delete") + 1] == "100"
    assert "--size-only" in argv and "--fast-list" in argv
    assert "--checksum" in sync_argv("rclone", "c", "a:", "b:", compare="checksum")
    assert "--size-only" not in sync_argv("rclone", "c", "a:", "b:", compare="modtime")
    assert "--backup-dir" not in sync_argv("rclone", "c", "a:", "b:")
    with pytest.raises(v.ValidationError):
        sync_argv("rclone", "c", "a:", "b:", compare="fast")
    assert check_argv("rclone", "c", "/l", "r:")[3:] == ["check", "/l", "r:", "--one-way", "--size-only"]
    assert prune_trash_argv("rclone", "c", "t:b/s", 30)[3:] == ["delete", "t:b/s", "--min-age", "30d", "--rmdirs"]
    with pytest.raises(v.ValidationError):
        prune_trash_argv("rclone", "c", "t:b/s", 0)


def test_trash_plan_staging_vs_same_account():
    replica = Profile.from_params(PROFILES[1])
    trash = Profile.from_params(PROFILES[2])
    plan = trash_plan(replica, trash, SITE, "20261104T101500Z")
    # different accounts: staged in the replica bucket, outside <prefix>/<site>, then moved
    assert plan.backup_dir == "nxs-us-replica:us-replica-bucket/.nexaploy-trash-staging/a.example.com/20261104T101500Z"
    assert plan.staging_root == "nxs-us-replica:us-replica-bucket/.nexaploy-trash-staging/a.example.com"
    assert plan.trash_root == "nxs-us-trash:us-trash-bucket/a.example.com"
    assert not plan.backup_dir.startswith(replica.site_root(SITE))
    # same account (same provider/endpoint/region/key): --backup-dir straight into the trash bucket
    same = Profile.from_params({**PROFILES[1], "name": "us-trash2", "bucket": "trash-b"})
    plan2 = trash_plan(replica, same, SITE, "20261104T101500Z")
    assert plan2.staging_root is None
    assert plan2.backup_dir == "nxs-us-replica:trash-b/a.example.com/20261104T101500Z"
    with pytest.raises(v.ValidationError):
        trash_plan(replica, trash, SITE, "yesterday")


def test_parse_trash_item_and_lsjson():
    assert parse_trash_item("20261104T101500Z") == ("20261104T101500Z", None, None)
    assert parse_trash_item("20261104T101500Z/public/a/b.pdf") == ("20261104T101500Z", "public", "a/b.pdf")
    for bad in ("", "x/public/a", "20261104T101500Z/backups", "20261104T101500Z/public/../x",
                "20261104T101500Z/private//x"):
        with pytest.raises(v.ValidationError):
            parse_trash_item(bad)
    rows = [{"Path": "T/public/a.pdf", "Size": 3, "ModTime": "2026", "IsDir": False},
            {"Path": "T", "IsDir": True}]
    assert parse_lsjson(json.dumps(rows)) == [{"path": "T/public/a.pdf", "size": 3, "mod_time": "2026"}]
    assert parse_lsjson("garbage") == []
    assert propagation_ok("shared") and propagation_ok("private,slave") and not propagation_ok("private")


def test_count_dirty(tmp_path):
    meta = tmp_path / "vfsMeta" / "nxs-p" / "b"
    meta.mkdir(parents=True)
    (meta / "a.pdf").write_text(json.dumps({"Dirty": True, "Size": 3}))
    (meta / "b.pdf").write_text(json.dumps({"Dirty": False}))
    (meta / "c.pdf").write_text("not json")
    assert storage_jobs.count_dirty(tmp_path / "vfsMeta") == 1
    assert storage_jobs.count_dirty(tmp_path / "missing") == 0


def test_job_kinds_registered():
    for kind in ("site.storage_configure", "site.storage_migrate", "site.storage_status",
                 "site.storage_failover", "site.storage_trash_list", "site.storage_trash_restore",
                 "server.storage_replicate", "server.storage_prune_trash"):
        assert kind in REGISTRY


# --------------------------------------------------------------------------- jobs
class FakeHost:
    """Scripts findmnt/systemctl on the FakeRunner: a set of mounted paths."""

    def __init__(self, ctx, runner, propagation="shared"):
        self.ctx, self.runner = ctx, runner
        self.mounted: set[str] = set()
        self.fail_mount = False
        runner.on(["findmnt", "-n", "-o", "PROPAGATION"], stdout=propagation + "\n")
        runner.on(["findmnt", "-n", "-o", "FSTYPE"], result=self.findmnt)
        runner.on(["systemctl", "restart"], result=self.restart)
        runner.on(["systemctl", "is-active"], result=self.is_active)
        runner.on(["systemctl", "stop"], result=self.stop)
        runner.on(["systemctl", "disable", "--now"], result=self.stop)

    def _mp(self, unit):
        site_dir = self.ctx.sites.site_dir(self.ctx.sites.find_bench(SITE), SITE)
        kind = "public" if unit.endswith("-public.service") else "private"
        return str(site_dir / kind / "files")

    def findmnt(self, call: Call):
        return Result(0, "fuse.rclone\n" if call.args[-1] in self.mounted else "", "")

    def restart(self, call: Call):
        if not self.fail_mount:
            self.mounted.add(self._mp(call.args[-1]))
        return Result(0, "", "")

    def stop(self, call: Call):
        self.mounted.discard(self._mp(call.args[-1]))
        return Result(0, "", "")

    def is_active(self, call: Call):
        return Result(0 if self._mp(call.args[-1]) in self.mounted else 3,
                      "active\n" if self._mp(call.args[-1]) in self.mounted else "inactive\n", "")


@pytest.fixture
def storage_ctx(ctx, monkeypatch, tmp_path):
    monkeypatch.setattr(storage_jobs._StorageJob, "mount_wait_seconds", 0)
    ctx.settings.storage.systemd_dir = tmp_path / "systemd"
    make_bench(ctx, "erp-v15")
    site_dir = make_site(ctx, "erp-v15", SITE)
    (site_dir / "public" / "files").mkdir(parents=True)
    (site_dir / "public" / "files" / "logo.png").write_bytes(b"png")
    (site_dir / "private" / "files").mkdir(parents=True)
    (site_dir / "private" / "files" / "invoice.pdf").write_bytes(b"pdf")
    return ctx


def _offload(ctx, worker, runner, **extra):
    host = FakeHost(ctx, runner)
    job = run_job(ctx, worker, "site.storage_migrate", {**POLICY, "direction": "to_bucket", **extra})
    return host, job


def test_migrate_to_bucket(storage_ctx, worker, runner):
    ctx = storage_ctx
    host, job = _offload(ctx, worker, runner)
    assert job["status"] == "success", job
    site_dir = ctx.sites.site_dir("erp-v15", SITE)
    # remotes in their own rclone config, mode 600
    conf = ctx.settings.storage_rclone_conf
    assert oct(conf.stat().st_mode & 0o777) == "0o600"
    text = conf.read_text()
    assert "[nxs-de-primary]" in text and "[nxs-us-replica]" in text and "[nxs-us-trash]" in text
    assert "endpoint = fsn1.your-objectstorage.com" in text
    # bench follows host mounts
    assert ctx.benches.read_env("erp-v15")["SITES_PROPAGATION"] == "rslave"
    compose = (ctx.benches.path("erp-v15") / "docker-compose.yml").read_text()
    assert "${SITES_PROPAGATION:-rprivate}" in compose
    assert runner.called("up -d")
    # copy + delta + check before moving the local files aside
    remote = "nxs-de-primary:de-primary-bucket/files/a.example.com/public"
    copies = [c.args for c in runner.find(f"copy {site_dir}/public/files {remote}")]
    assert len(copies) == 2
    assert runner.called(f"check {site_dir}/public/files {remote} --one-way --size-only")
    # units written and started; mountpoints are mounted
    unit = ctx.settings.storage.systemd_dir / unit_name(SITE, "public")
    assert f"mount {remote} {site_dir}/public/files" in unit.read_text()
    assert runner.called("systemctl enable nexaploy-files-a-example-com-public.service")
    assert host.mounted == {str(site_dir / "public" / "files"), str(site_dir / "private" / "files")}
    # local copies removed, server stays (almost) empty
    assert not list((site_dir / "public").glob("files.pre-offload-*"))
    assert job["result"]["mode"] == "Offloaded"
    state = ctx.storage.read_state(SITE)
    assert state["mode"] == "Offloaded" and state["active"] == "primary"
    assert ctx.storage.is_offloaded(SITE)
    # secrets never in job output/params
    assert SECRET not in json.dumps(job)
    assert SECRET not in json.dumps(state)


def test_migrate_refuses_private_propagation(storage_ctx, worker, runner):
    FakeHost(storage_ctx, runner, propagation="private")
    job = run_job(storage_ctx, worker, "site.storage_migrate", {**POLICY})
    assert job["status"] == "failure"
    assert "shared mount" in job["error"]
    assert (storage_ctx.sites.site_dir("erp-v15", SITE) / "public" / "files" / "logo.png").exists()


def test_migrate_rolls_back_when_mount_fails(storage_ctx, worker, runner):
    host = FakeHost(storage_ctx, runner)
    host.fail_mount = True
    job = run_job(storage_ctx, worker, "site.storage_migrate", {**POLICY})
    assert job["status"] == "failure"
    site_dir = storage_ctx.sites.site_dir("erp-v15", SITE)
    assert (site_dir / "public" / "files" / "logo.png").read_bytes() == b"png"
    assert (site_dir / "private" / "files" / "invoice.pdf").read_bytes() == b"pdf"
    assert not (storage_ctx.settings.storage.systemd_dir / unit_name(SITE, "public")).exists()
    assert storage_ctx.storage.read_state(SITE)["mode"] == "Local"
    assert not storage_ctx.storage.is_offloaded(SITE)  # backups include files again


def test_migrate_keep_local_copy(storage_ctx, worker, runner):
    _, job = _offload(storage_ctx, worker, runner, keep_local_copy=True)
    assert job["status"] == "success", job
    site_dir = storage_ctx.sites.site_dir("erp-v15", SITE)
    assert list((site_dir / "public").glob("files.pre-offload-*"))


def test_offloaded_backup_is_database_only_and_drop_tears_down(storage_ctx, worker, runner):
    ctx = storage_ctx
    host, job = _offload(ctx, worker, runner)
    assert job["status"] == "success"
    site_dir = ctx.sites.site_dir("erp-v15", SITE)

    def backup(call):
        d = site_dir / "private" / "backups"
        d.mkdir(parents=True, exist_ok=True)
        (d / "20261103_213000-a_example_com-database.sql.gz").write_bytes(b"sql")

    runner.on(["--site", SITE, "backup"], result=side_effect(backup))
    job = run_job(ctx, worker, "site.backup", {"site": SITE, "with_files": True})
    assert job["status"] == "success", job
    assert job["result"]["files_offloaded"] is True
    assert not runner.called("backup --with-files")

    runner.on("drop-site", result=side_effect(lambda c: None))
    job = run_job(ctx, worker, "site.drop", {"site": SITE, "skip_backup": True})
    assert job["status"] == "success", job
    assert cmd_before(runner, "disable --now nexaploy-files-a-example-com-public.service", "drop-site")
    assert not (ctx.settings.storage.systemd_dir / unit_name(SITE, "public")).exists()
    assert not ctx.storage.read_state(SITE)


def cmd_before(runner, first, second):
    lines = runner.cmdlines()
    a = next(i for i, c in enumerate(lines) if first in c)
    b = next(i for i, c in enumerate(lines) if second in c)
    return a < b


def test_rename_refused_when_offloaded(storage_ctx, worker, runner):
    _offload(storage_ctx, worker, runner)
    job = run_job(storage_ctx, worker, "site.rename",
                  {"bench": "erp-v15", "site": SITE, "new_name": "b.example.com"})
    assert job["status"] == "failure" and "offloaded" in job["error"]


def test_migrate_to_local(storage_ctx, worker, runner):
    ctx = storage_ctx
    host, job = _offload(ctx, worker, runner)
    site_dir = ctx.sites.site_dir("erp-v15", SITE)

    def copy_back(call):
        dest = Path(call.args[-3])  # copy <src> <dest> --retries 3
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "restored.txt").write_text("x")

    runner.on(lambda a: a[3:4] == ["copy"] and a[4].startswith("nxs-"), result=copy_back)
    job = run_job(ctx, worker, "site.storage_migrate", {**POLICY, "direction": "to_local"})
    assert job["status"] == "success", job
    assert (site_dir / "public" / "files" / "restored.txt").exists()
    assert not host.mounted
    assert not (ctx.settings.storage.systemd_dir / unit_name(SITE, "private")).exists()
    assert ctx.storage.read_state(SITE)["mode"] == "Local"


def test_migrate_to_local_waits_for_dirty_uploads(storage_ctx, worker, runner, monkeypatch):
    ctx = storage_ctx
    _offload(ctx, worker, runner)
    meta = ctx.storage.cache_dir(SITE, "public") / "vfsMeta"
    meta.mkdir(parents=True)
    (meta / "x").write_text(json.dumps({"Dirty": True}))
    monkeypatch.setattr(storage_jobs, "CLEAN_WAIT_ATTEMPTS", 2)
    job = run_job(ctx, worker, "site.storage_migrate", {**POLICY, "direction": "to_local"})
    assert job["status"] == "failure" and "not uploaded" in job["error"]
    # the unit is kept so rclone keeps uploading
    assert (ctx.settings.storage.systemd_dir / unit_name(SITE, "public")).exists()


def test_configure_refreshes_units_and_failover(storage_ctx, worker, runner):
    ctx = storage_ctx
    host, _ = _offload(ctx, worker, runner)
    runner.reset()
    job = run_job(ctx, worker, "site.storage_configure", POLICY)
    assert job["status"] == "success", job
    assert not runner.called("systemctl restart")  # nothing changed
    job = run_job(ctx, worker, "site.storage_failover", {**POLICY, "target": "replica"})
    assert job["status"] == "success", job
    unit = (ctx.settings.storage.systemd_dir / unit_name(SITE, "public")).read_text()
    assert "nxs-us-replica:us-replica-bucket/a.example.com/public" in unit
    assert ctx.storage.read_state(SITE)["active"] == "replica"
    # back to the primary: replica -> primary sync first, deletions captured to the trash
    runner.reset()
    job = run_job(ctx, worker, "site.storage_failover", {**POLICY, "target": "primary"})
    assert job["status"] == "success", job
    sync = runner.find("sync nxs-us-replica:us-replica-bucket/a.example.com/public")[0].args
    assert sync[5] == "nxs-de-primary:de-primary-bucket/files/a.example.com/public"
    assert sync[sync.index("--backup-dir") + 1].startswith(
        "nxs-de-primary:de-primary-bucket/files/.nexaploy-trash-staging/a.example.com/")
    assert runner.called("move nxs-de-primary:de-primary-bucket/files/.nexaploy-trash-staging/a.example.com "
                         "nxs-us-trash:us-trash-bucket/a.example.com")
    assert ctx.storage.read_state(SITE)["active"] == "primary"
    assert cmd_before(runner, "sync nxs-us-replica", "systemctl restart")


def test_status(storage_ctx, worker, runner):
    ctx = storage_ctx
    _offload(ctx, worker, runner)
    runner.on(["size", "--json"], stdout=json.dumps({"count": 2, "bytes": 6}))
    job = run_job(ctx, worker, "site.storage_status", {"site": SITE, "with_size": True, "profiles": PROFILES})
    assert job["status"] == "success", job
    r = job["result"]
    assert r["mode"] == "Offloaded" and r["healthy"] is True
    assert r["kinds"]["public"]["mounted"] and r["kinds"]["public"]["unit_active"]
    assert r["bucket"] == {"objects": 2, "bytes": 6}


REPL = {"profiles": PROFILES, "sites": [
    {"site": SITE, "primary": "de-primary", "replica": "us-replica", "trash": "us-trash", "max_delete": 50},
    {"site": "b.example.com", "primary": "de-primary", "replica": "us-replica"},
]}


def test_replicate_with_trash(ctx, worker, runner):
    runner.on(["lsf", "-R", "--files-only"], stdout="public/a.pdf\nprivate/b.pdf\n")
    job = run_job(ctx, worker, "server.storage_replicate", REPL)
    assert job["status"] == "success", job
    syncs = [c.args for c in runner.find(" sync ")]
    assert len(syncs) == 4
    a_pub = next(s for s in syncs if s[4] == "nxs-de-primary:de-primary-bucket/files/a.example.com/public")
    assert a_pub[5] == "nxs-us-replica:us-replica-bucket/a.example.com/public"
    assert a_pub[a_pub.index("--max-delete") + 1] == "50"
    bd = a_pub[a_pub.index("--backup-dir") + 1]
    assert bd.startswith("nxs-us-replica:us-replica-bucket/.nexaploy-trash-staging/a.example.com/")
    assert bd.endswith("/public")
    b_sync = next(s for s in syncs if "b.example.com" in s[4])
    assert "--backup-dir" not in b_sync
    assert runner.called("move nxs-us-replica:us-replica-bucket/.nexaploy-trash-staging/a.example.com "
                         "nxs-us-trash:us-trash-bucket/a.example.com --delete-empty-src-dirs")
    res = job["result"]["sites"]
    assert res[SITE]["status"] == "success" and res[SITE]["trashed"] == 2
    assert res["b.example.com"]["status"] == "success"
    assert SECRET not in json.dumps(job)


def test_replicate_skips_failed_over_and_isolates_failures(storage_ctx, worker, runner):
    ctx = storage_ctx
    ctx.storage.update_state(SITE, mode="Offloaded", active="replica")
    runner.on(lambda a: "sync" in a and "b.example.com" in " ".join(a), returncode=1, stderr="AccessDenied")
    job = run_job(ctx, worker, "server.storage_replicate", REPL)
    assert job["status"] == "failure" and "b.example.com" in job["error"]
    res = job["result"]["sites"]
    assert res[SITE]["status"] == "skipped"
    assert res["b.example.com"]["status"] == "failure"
    assert not runner.find("sync nxs-de-primary:de-primary-bucket/files/a.example.com")


def test_replicate_refuses_empty_primary_with_full_replica(ctx, worker, runner):
    runner.on(lambda a: "sync" in a, returncode=3, stderr="directory not found")
    runner.on(["lsf", "--max-depth", "1"], stdout="a.pdf\n")
    job = run_job(ctx, worker, "server.storage_replicate", {"profiles": PROFILES, "sites": [REPL["sites"][1]]})
    assert job["status"] == "failure"
    assert "refusing" in job["result"]["sites"]["b.example.com"]["error"]
    # both empty: a site without files yet is fine
    runner.on(["lsf", "--max-depth", "1"], stdout="")
    job = run_job(ctx, worker, "server.storage_replicate", {"profiles": PROFILES, "sites": [REPL["sites"][1]]})
    assert job["status"] == "success", job


def test_replicate_params_validated(ctx):
    bad = {"profiles": PROFILES, "sites": [{"site": SITE, "primary": "de-primary", "replica": "de-primary"}]}
    with pytest.raises(Exception):
        ctx.enqueue("server.storage_replicate", bad)
    missing = {"profiles": PROFILES[:1], "sites": [{"site": SITE, "primary": "de-primary", "replica": "x-y"}]}
    with pytest.raises(Exception):
        ctx.enqueue("server.storage_replicate", missing)


def test_prune_trash(ctx, worker, runner):
    job = run_job(ctx, worker, "server.storage_prune_trash", {"profiles": PROFILES, "sites": [
        {"site": SITE, "trash": "us-trash", "retention_days": 30}]})
    assert job["status"] == "success", job
    assert runner.called("delete nxs-us-trash:us-trash-bucket/a.example.com --min-age 30d --rmdirs")


def test_trash_list_and_restore(storage_ctx, worker, runner):
    ctx = storage_ctx
    _offload(ctx, worker, runner)
    rows = [{"Path": "20261104T101500Z/public/logo.png", "Size": 3, "ModTime": "x", "IsDir": False}]
    runner.on(["lsjson"], stdout=json.dumps(rows))
    params = {"site": SITE, "profiles": PROFILES, "trash": "us-trash", "primary": "de-primary",
              "replica": "us-replica"}
    job = run_job(ctx, worker, "site.storage_trash_list", params)
    assert job["status"] == "success", job
    assert job["result"]["entries"][0]["path"] == "20261104T101500Z/public/logo.png"
    job = run_job(ctx, worker, "site.storage_trash_restore", {**params, "item": "20261104T101500Z/public/logo.png"})
    assert job["status"] == "success", job
    assert runner.called("copyto nxs-us-trash:us-trash-bucket/a.example.com/20261104T101500Z/public/logo.png "
                         "nxs-de-primary:de-primary-bucket/files/a.example.com/public/logo.png")
    assert runner.called("kill --signal=HUP nexaploy-files-a-example-com-public.service")
    with pytest.raises(Exception):
        ctx.enqueue("site.storage_trash_restore", {**params, "item": "../../etc"})


def test_usage_skips_mounted_dirs(storage_ctx, runner, monkeypatch):
    import os
    from datetime import datetime, timezone

    real = os.path.ismount
    monkeypatch.setattr(os.path, "ismount", lambda p: str(p).endswith("files") or real(p))
    assert storage_ctx.usage.files_size({"sizes": {}}, "erp-v15", SITE, datetime.now(timezone.utc)) == 0
    assert not any(c.args[0] == "du" for c in runner.calls)  # du would list the whole bucket
