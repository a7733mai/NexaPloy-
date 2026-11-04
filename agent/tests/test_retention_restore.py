"""Per-site backup retention (local after upload, remote GFS) and partial restores."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from nexaploy_agent.runtime.backup import new_ts, select_prunable_gfs

from .conftest import make_bench, make_site, run_job, side_effect
from .test_backup import _staged_backup, _write_frappe_backup

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def ts(days_ago: float) -> str:
    return new_ts(NOW - timedelta(days=days_ago))


# ---- remote GFS ---------------------------------------------------------------------------
def test_gfs_without_keeps_equals_plain():
    stamps = [ts(d) for d in range(0, 60, 2)]
    assert select_prunable_gfs(stamps, 14, 3, now=NOW) == sorted(t for t in stamps if t < ts(14))


def test_gfs_keeps_daily_weekly_monthly():
    stamps = [ts(d) for d in range(0, 400)]  # one backup per day for 400 days
    doomed = set(select_prunable_gfs(stamps, 7, 3, keep_daily=10, keep_weekly=6, keep_monthly=12, now=NOW))
    kept = sorted(set(stamps) - doomed)
    # 7 days by age, 10 daily, then roughly 6 weekly and 12 monthly snapshots
    assert all(ts(d) in kept for d in range(0, 10))
    assert ts(11) not in kept
    assert len(kept) <= 10 + 6 + 12
    months = {k[:6] for k in kept}
    assert len(months) >= 12
    assert ts(399) in doomed  # older than 12 months


# ---- local retention after upload ---------------------------------------------------------
def _backup_job(ctx, worker, runner, settings, local_days, upload=True):
    make_bench(ctx, "erp-v15")
    site_dir = make_site(ctx, "erp-v15", "a.example.com")
    runner.on(["backup", "--with-files"], result=side_effect(lambda c: _write_frappe_backup(site_dir)))
    settings.rclone_conf.write_text("[nexaploy-backups]\ntype = s3\n")
    params = {"site": "a.example.com", "with_files": True, "upload": upload}
    if local_days is not None:
        params["local_retention_days"] = local_days
    return run_job(ctx, worker, "site.backup", params)


def test_local_retention_zero_deletes_after_upload(ctx, worker, runner, settings):
    job = _backup_job(ctx, worker, runner, settings, 0)
    assert job["status"] == "success", job
    r = job["result"]
    assert r["uploaded"] is True and r["pruned"] == [r["backup_id"]] and r["kept_locally"] is False
    assert ctx.backups.local_timestamps("erp-v15", "a.example.com") == []
    # the Site Backup still has its remote files for restore/download
    assert r["remote_path"] and r["backup_files"]


def test_local_retention_zero_keeps_unuploaded(ctx, worker, runner, settings):
    job = _backup_job(ctx, worker, runner, settings, 0, upload=False)
    assert job["status"] == "success"
    assert job["result"]["kept_locally"] is True
    assert ctx.backups.local_timestamps("erp-v15", "a.example.com") == [job["result"]["backup_id"]]


def test_local_retention_days(ctx, settings):
    for d in (10, 5, 1):
        p = ctx.backups.backup_dir("erp-v15", "a.example.com", ts(d))
        p.mkdir(parents=True)
        (p / "manifest.json").write_text("{}")
        ctx.backups.mark_uploaded("erp-v15", "a.example.com", ts(d))
    # an old backup that never reached the remote: kept (agent default 14 days, keep 3)
    p = ctx.backups.backup_dir("erp-v15", "a.example.com", ts(12))
    p.mkdir(parents=True)
    pruned = ctx.backups.prune("erp-v15", "a.example.com", now=NOW, local_retention_days=3)
    assert pruned == sorted([ts(10), ts(5)])
    assert ctx.backups.local_timestamps("erp-v15", "a.example.com") == sorted([ts(12), ts(1)])


def test_uploaded_marker_not_in_manifest(ctx, worker, runner, settings):
    job = _backup_job(ctx, worker, runner, settings, None)
    r = job["result"]
    d = ctx.backups.backup_dir("erp-v15", "a.example.com", r["backup_id"])
    assert (d / ".uploaded").is_file()
    ctx.backups.verify(d)  # marker is not part of the manifest
    assert ".uploaded" not in json.dumps(json.loads((d / "manifest.json").read_text()))


def test_local_retention_validation(ctx):
    with pytest.raises(Exception):
        ctx.enqueue("site.backup", {"site": "a.example.com", "local_retention_days": -1})


def test_prune_remote_gfs_params(ctx, worker, runner, settings):
    settings.rclone_conf.write_text("[nexaploy-backups]\ntype = local\n")
    stamps = [new_ts(datetime.now(timezone.utc) - timedelta(days=d, minutes=1)) for d in range(0, 40)]
    runner.on("lsf", stdout="".join(f"{s}/\n" for s in stamps))
    job = run_job(ctx, worker, "backup.prune_remote",
                  {"site": "a.example.com", "bench": "erp-v15", "keep_days": 7, "keep_min": 3,
                   "keep_weekly": 4})
    assert job["status"] == "success", job
    kept = job["result"]["kept"]
    assert len(kept) >= 7 and len(kept) <= 7 + 4
    with pytest.raises(Exception):
        ctx.enqueue("backup.prune_remote", {"site": "a.example.com", "bench": "erp-v15", "keep_daily": -1})


# ---- partial restore ------------------------------------------------------------------------
def test_restore_database_only(ctx, worker, runner):
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", "a.example.com")
    stamp, _ = _staged_backup(ctx)
    job = run_job(ctx, worker, "site.restore",
                  {"site": "a.example.com", "backup_id": stamp, "restore": "database"})
    assert job["status"] == "success", job
    args = runner.find(" restore ")[0].args
    assert "--with-public-files" not in args and "--with-private-files" not in args
    assert runner.called("--site a.example.com migrate")


def test_restore_files_only(ctx, worker, runner):
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", "a.example.com")
    stamp, _ = _staged_backup(ctx)
    job = run_job(ctx, worker, "site.restore",
                  {"site": "a.example.com", "backup_id": stamp, "restore": "files"})
    assert job["status"] == "success", job
    assert not runner.called(" restore /")  # no bench restore (database untouched)
    extracts = runner.find("frappe.installer.extract_files")
    assert len(extracts) == 2
    targets = [json.loads(c.args[c.args.index("--args") + 1]) for c in extracts]
    assert all(t[0] == "a.example.com" for t in targets)
    assert {t[1].rsplit("-", 1)[-1] for t in targets} == {"files.tar"}
    assert not runner.called("migrate") and not runner.called("set-config encryption_key")
    assert job["result"]["restore"] == "files"


def test_restore_files_only_needs_existing_site(ctx):
    make_bench(ctx, "erp-v15")
    with pytest.raises(Exception):
        ctx.enqueue("site.restore", {"site": "new.example.com", "bench": "erp-v15",
                                     "remote_path": "nexaploy-backups:p/erp-v15/a.example.com/20261103T120000Z",
                                     "restore": "files"}, precheck=True)


def test_restore_mode_validation(ctx):
    with pytest.raises(Exception):
        ctx.enqueue("site.restore", {"site": "a.example.com", "backup_id": "20261103T120000Z", "restore": "tables"})
