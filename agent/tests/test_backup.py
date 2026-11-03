import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nexaploy_agent.runtime.backup import (
    BackupError, classify, new_ts, parse_ts, select_prunable,
)

from .conftest import DB_PW, IMAGE, make_bench, make_site, run_job, side_effect

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def ts(days_ago: float) -> str:
    return new_ts(NOW - timedelta(days=days_ago))


def test_ts_roundtrip():
    assert new_ts(NOW) == "20261103T120000Z"
    assert parse_ts("20261103T120000Z") == NOW
    assert parse_ts("garbage") is None


def test_classify():
    assert classify("20261103_1200-a_io-database.sql.gz") == "database"
    assert classify("20261103_1200-a_io-files.tar") == "public"
    assert classify("20261103_1200-a_io-private-files.tar") == "private"
    assert classify("20261103_1200-a_io-site_config_backup.json") == "config"
    assert classify("random.txt") is None


def test_retention_keeps_latest_three():
    stamps = [ts(d) for d in (40, 30, 20)]
    assert select_prunable(stamps, retention_days=14, keep_min=3, now=NOW) == []
    stamps = [ts(d) for d in (40, 30, 20, 16, 1)]
    assert select_prunable(stamps, 14, 3, NOW) == sorted([ts(40), ts(30)])
    stamps = [ts(d) for d in (13, 10, 5, 3, 1)]
    assert select_prunable(stamps, 14, 3, NOW) == []  # all within retention
    assert select_prunable(stamps + ["notatimestamp"], 0, 3, NOW) == sorted([ts(13), ts(10)])


def _write_frappe_backup(site_dir: Path, stamp="20261103_120000", site="a_example_com"):
    d = site_dir / "private" / "backups"
    d.mkdir(parents=True, exist_ok=True)
    files = {
        f"{stamp}-{site}-database.sql.gz": b"SQLDUMP",
        f"{stamp}-{site}-files.tar": b"PUBLIC",
        f"{stamp}-{site}-private-files.tar": b"PRIVATE",
        f"{stamp}-{site}-site_config_backup.json": json.dumps({"encryption_key": "ENCKEY123"}).encode(),
    }
    for name, data in files.items():
        (d / name).write_bytes(data)
    return files


def test_site_backup_job(ctx, worker, runner, settings):
    make_bench(ctx, "erp-v15")
    site_dir = make_site(ctx, "erp-v15", "a.example.com")
    # a stale backup from frappe's own scheduler must not be picked up
    (site_dir / "private" / "backups").mkdir(parents=True)
    (site_dir / "private" / "backups" / "old-a_example_com-database.sql.gz").write_bytes(b"old")
    runner.on(["backup", "--with-files"], result=side_effect(lambda c: _write_frappe_backup(site_dir)))
    settings.rclone_conf.write_text("[nexaploy-backups]\ntype = s3\n")
    job = run_job(ctx, worker, "site.backup", {"site": "a.example.com", "with_files": True, "upload": True})
    assert job["status"] == "success", job
    r = job["result"]
    dest = settings.backups_dir / "erp-v15" / "a.example.com" / r["backup_id"]
    assert Path(r["path"]) == dest and (dest / "manifest.json").exists()
    manifest = json.loads((dest / "manifest.json").read_text())
    assert manifest["site"] == "a.example.com" and manifest["bench"] == "erp-v15" and manifest["image"] == IMAGE
    names = {f["name"] for f in manifest["files"]}
    assert len(names) == 4 and not any(n.startswith("old") for n in names)
    for f in manifest["files"]:
        assert len(f["sha256"]) == 64 and f["size"] > 0
    # files moved out of the site dir
    assert not (site_dir / "private" / "backups" / "20261103_120000-a_example_com-database.sql.gz").exists()
    # uploaded with rclone to <remote>:<prefix>/<bench>/<site>/<ts>
    expected_remote = f"nexaploy-backups:srv-test/erp-v15/a.example.com/{r['backup_id']}"
    rc = runner.find("rclone")[0].args
    assert rc == ["rclone", "--config", str(settings.rclone_conf), "copy", str(dest), expected_remote]
    assert r["remote_path"] == expected_remote and r["uploaded"] is True
    assert r["size"] == sum(f["size"] for f in manifest["files"]) and r["created_at"].endswith("Z")
    # listing
    local = ctx.backups.list_local("a.example.com")
    assert [b["id"] for b in local] == [r["backup_id"]]


def test_prune_on_disk(ctx, settings):
    for d in (40, 30, 20, 16, 1):
        p = ctx.backups.backup_dir("erp-v15", "a.example.com", ts(d))
        p.mkdir(parents=True)
        (p / "manifest.json").write_text("{}")
    pruned = ctx.backups.prune("erp-v15", "a.example.com", now=NOW)
    assert pruned == sorted([ts(40), ts(30)])
    assert ctx.backups.local_timestamps("erp-v15", "a.example.com") == sorted([ts(20), ts(16), ts(1)])


def _staged_backup(ctx, bench="erp-v15", site="a.example.com"):
    src = ctx.settings.root / "tmp-src"
    files = _write_frappe_backup(src)
    stamp = "20261103T120000Z"
    ctx.backups.stage(bench, site, stamp, src / "private" / "backups", files.keys(), IMAGE)
    return stamp, ctx.backups.backup_dir(bench, site, stamp)


def test_verify_detects_tampering(ctx):
    stamp, d = _staged_backup(ctx)
    ctx.backups.verify(d)
    db = next(d.glob("*-database.sql.gz"))
    db.write_bytes(b"tampered")
    with pytest.raises(BackupError, match="sha256 mismatch"):
        ctx.backups.verify(d)


def test_restore_local_backup(ctx, worker, runner):
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", "a.example.com", extra={"encryption_key": "OLDKEY"})
    stamp, d = _staged_backup(ctx)
    job = run_job(ctx, worker, "site.restore", {"site": "a.example.com", "backup_id": stamp})
    assert job["status"] == "success", job
    args = runner.find(" restore ")[0].args
    i = args.index("restore")
    cdir = f"/home/frappe/frappe-bench/sites/.nexaploy-restore/{job['id']}"
    assert args[i + 1] == f"{cdir}/20261103_120000-a_example_com-database.sql.gz"
    assert args[args.index("--with-public-files") + 1].endswith("-files.tar")
    assert args[args.index("--with-private-files") + 1].endswith("-private-files.tar")
    assert args[args.index("--db-root-password") + 1] == DB_PW
    assert runner.called("--site a.example.com set-config encryption_key ENCKEY123")
    assert runner.called("--site a.example.com migrate")
    assert ctx.proxy.has_route("a.example.com")
    blob = json.dumps(job)
    assert DB_PW not in blob and "ENCKEY123" not in blob
    # staging dir cleaned up
    assert not (ctx.benches.sites_dir("erp-v15") / ".nexaploy-restore" / job["id"]).exists()


def test_restore_rejects_bad_checksum(ctx, worker, runner):
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", "a.example.com")
    stamp, d = _staged_backup(ctx)
    next(d.glob("*-files.tar")).write_bytes(b"corrupt")
    job = run_job(ctx, worker, "site.restore", {"site": "a.example.com", "backup_id": stamp})
    assert job["status"] == "failure" and "sha256 mismatch" in job["error"]
    assert [s["status"] for s in job["steps"]][2:4] == ["failure", "skipped"]
    assert not runner.called(" restore ")


def test_restore_from_remote(ctx, worker, runner, settings):
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", "a.example.com")
    stamp, staged = _staged_backup(ctx, bench="erp-v14")  # backup taken on another bench

    def fetch(call):
        import shutil
        shutil.copytree(staged, call.args[-1], dirs_exist_ok=True)

    runner.on(["rclone", "--config"], result=side_effect(fetch))
    remote = f"nexaploy-backups:srv-old/erp-v14/a.example.com/{stamp}"
    job = run_job(ctx, worker, "site.restore", {"site": "a.example.com", "remote_path": remote})
    assert job["status"] == "success", job
    rc = runner.find("rclone")[0].args
    assert rc[3] == "copy" and rc[4] == remote
    assert runner.called("--site a.example.com restore")
    # fetched copy removed after success
    assert not Path(job["result"]["dir"]).exists()


def test_restore_params():
    from pydantic import ValidationError
    from nexaploy_agent.jobs.site_jobs import SiteRestore

    with pytest.raises(ValidationError):
        SiteRestore.validate_params({"site": "a.example.com"})
    with pytest.raises(ValidationError):
        SiteRestore.validate_params({"site": "a.example.com", "backup_id": "20261103T120000Z",
                                     "remote_path": "x/y"})
    with pytest.raises(ValidationError):
        SiteRestore.validate_params({"site": "a.example.com", "remote_path": "r:../../etc"})
