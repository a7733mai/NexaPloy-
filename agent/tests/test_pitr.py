"""Point-in-time recovery: pure helpers (runtime/pitr.py) and the jobs (jobs/pitr_jobs.py)."""

from __future__ import annotations

import gzip
import json
import os
import shlex
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from nexaploy_agent import validation as v
from nexaploy_agent.jobs.base import Conflict, NotFound
from nexaploy_agent.jobs.pitr_jobs import PITR_CONFIG_KEY, SiteRestorePitr, SiteRestorePitrParams
from nexaploy_agent.runtime import pitr as pt
from nexaploy_agent.runtime.docker import Result

from .conftest import DB_PW, IMAGE, bench_running, make_bench, make_site, run_job, side_effect

T0 = datetime(2026, 11, 4, 15, 15, tzinfo=timezone.utc)

DUMP_HEADER = """/*M!999999\\- enable the sandbox mode */
-- MariaDB dump 10.19-11.8.3-MariaDB, for debian-linux-gnu (x86_64)
--
-- Host: nexaploy-db    Database: _abc
-- ------------------------------------------------------
-- Server version	11.8.3-MariaDB-ubu2404-log

--
-- Position to start replication or point-in-time recovery from
--

-- CHANGE MASTER TO MASTER_LOG_FILE='mysql-bin.000042', MASTER_LOG_POS=1234567;

--
-- GTID to start replication from
--

-- CHANGE MASTER TO MASTER_USE_GTID=slave_pos;
-- SET GLOBAL gtid_slave_pos='0-1-9876';

--
-- Table structure for table `tabDefaultValue`
--
"""


# --------------------------------------------------------------------------- pure helpers
def test_parse_dump_coordinates():
    assert pt.parse_dump_coordinates(DUMP_HEADER) == {"file": "mysql-bin.000042", "position": 1234567,
                                                      "gtid": "0-1-9876"}
    # without --gtid; MySQL-style wording is accepted too
    text = "-- CHANGE REPLICATION SOURCE TO SOURCE_LOG_FILE='mysql-bin.000003', SOURCE_LOG_POS=4;\n"
    assert pt.parse_dump_coordinates(text) == {"file": "mysql-bin.000003", "position": 4, "gtid": None}
    with pytest.raises(pt.PitrError, match="no binlog coordinates"):
        pt.parse_dump_coordinates("-- MariaDB dump\nCREATE TABLE x (a int);\n")
    # --master-data=1 would change the replication state on restore: refused
    with pytest.raises(pt.PitrError, match="active CHANGE MASTER"):
        pt.parse_dump_coordinates("CHANGE MASTER TO MASTER_LOG_FILE='mysql-bin.000001', MASTER_LOG_POS=4;\n")
    with pytest.raises(pt.PitrError, match="gtid_slave_pos"):
        pt.parse_dump_coordinates(DUMP_HEADER.replace("-- SET GLOBAL", "SET GLOBAL"))
    with pytest.raises(v.ValidationError):
        pt.parse_dump_coordinates("-- CHANGE MASTER TO MASTER_LOG_FILE='../x', MASTER_LOG_POS=4;\n")


def test_read_dump_header(tmp_path):
    p = tmp_path / "x-database.sql.gz"
    with gzip.open(p, "wb") as fh:
        fh.write(DUMP_HEADER.encode() + b"INSERT INTO t VALUES (1);\n" * 1000)
    assert pt.parse_dump_coordinates(pt.read_dump_header(p))["position"] == 1234567


def test_dump_command_and_script():
    argv = pt.dump_command(IMAGE, "/opt/x/private/backups", "20261104_151500-a_example_com-database.sql.gz",
                           "_abc", "nexaploy-db")
    assert argv[:3] == ["docker", "run", "--rm"]
    assert argv[argv.index("--network") + 1] == "nexaploy-db"
    # the password only via the docker CLI environment
    assert argv[argv.index("-e") + 1] == "MYSQL_PWD" and not any("MYSQL_PWD=" in a for a in argv)
    assert argv[argv.index("-v") + 1] == "/opt/x/private/backups:/out"
    assert argv[argv.index("--entrypoint") + 1] == "bash" and IMAGE in argv
    script = argv[-1]
    for flag in ("--single-transaction", "--master-data=2", "--gtid", "--quick", "--routines", "--triggers"):
        assert flag in script
    assert "set -euo pipefail" in script and "gzip -c" in script
    # written to a temp file and renamed over bench's dump only when complete
    assert "mv -f /out/.20261104_151500-a_example_com-database.sql.gz.pitr-tmp " \
           "/out/20261104_151500-a_example_com-database.sql.gz" in script
    with pytest.raises(v.ValidationError):
        pt.dump_script("_abc; drop", "nexaploy-db", "x-database.sql.gz")
    with pytest.raises(v.ValidationError):
        pt.dump_script("_abc", "nexaploy-db", "../x-database.sql.gz")
    with pytest.raises(v.ValidationError):
        pt.dump_script("_abc", "host;rm", "x-database.sql.gz")


def test_binlog_status_and_index():
    assert pt.parse_binlog_status("1\tROW\n") == {"enabled": True, "format": "ROW"}
    assert pt.parse_binlog_status("0\tMIXED\n") == {"enabled": False, "format": "MIXED"}
    assert pt.parse_binlog_status("") == {"enabled": False, "format": None}
    assert pt.pitr_ready({"enabled": True, "format": "ROW"}) is None
    assert "off" in pt.pitr_ready({"enabled": False, "format": "ROW"})
    assert "ROW is required" in pt.pitr_ready({"enabled": True, "format": "MIXED"})
    index = pt.parse_binlog_index("./mysql-bin.000001\n/var/lib/mysql/mysql-bin.000002\n\nmysql-bin.000003\njunk\n")
    assert index == ["mysql-bin.000001", "mysql-bin.000002", "mysql-bin.000003"]
    assert pt.closed_binlogs(index) == ["mysql-bin.000001", "mysql-bin.000002"]
    assert pt.closed_binlogs([]) == []


def test_parse_rclone_lsjson():
    text = json.dumps([
        {"Path": "mysql-bin.000001", "Name": "mysql-bin.000001", "Size": 10,
         "ModTime": "2026-11-04T15:00:00.123456789Z", "IsDir": False},
        {"Path": "notes.txt", "Name": "notes.txt", "Size": 1, "ModTime": "2026-11-04T15:00:00Z", "IsDir": False},
        {"Path": "sub", "Name": "sub", "IsDir": True},
    ])
    out = pt.parse_rclone_lsjson(text)
    assert list(out) == ["mysql-bin.000001"]
    assert out["mysql-bin.000001"]["mtime"] == datetime(2026, 11, 4, 15, 0, 0, 123456, tzinfo=timezone.utc)
    with pytest.raises(pt.PitrError):
        pt.parse_rclone_lsjson("not json")


def _files(*pairs):
    return {f"mysql-bin.{seq:06d}": (T0 + timedelta(minutes=m) if m is not None else None) for seq, m in pairs}


def test_select_binlogs():
    avail = _files((40, -120), (41, -60), (42, -30), (43, -5), (44, 3), (45, 30), (46, 60))
    # from the backup's file up to the first one written well after the target
    assert pt.select_binlogs(avail, "mysql-bin.000042", T0) == [
        "mysql-bin.000042", "mysql-bin.000043", "mysql-bin.000044", "mysql-bin.000045"]
    # the active file is never read (it may end in a half-written event)
    assert pt.select_binlogs(avail, "mysql-bin.000042", T0 + timedelta(hours=5), exclude=["mysql-bin.000046"]) == [
        "mysql-bin.000042", "mysql-bin.000043", "mysql-bin.000044", "mysql-bin.000045"]
    # unknown mtimes: keep going
    assert pt.select_binlogs(_files((1, None), (2, None)), "mysql-bin.000001", T0) == [
        "mysql-bin.000001", "mysql-bin.000002"]
    with pytest.raises(pt.PitrError, match="mysql-bin.000043 is missing"):
        pt.select_binlogs(_files((42, -30), (44, 30)), "mysql-bin.000042", T0)
    with pytest.raises(pt.PitrError, match="no longer available"):
        pt.select_binlogs(_files((43, -30)), "mysql-bin.000042", T0)
    with pytest.raises(v.ValidationError):
        pt.select_binlogs(avail, "../etc/passwd", T0)


def test_decode_and_apply_scripts():
    files = ["mysql-bin.000042", "mysql-bin.000043"]
    script = pt.decode_script(files, 1234567, T0, "_abc", "_new")
    line = next(l for l in script.splitlines() if l.startswith("mariadb-binlog"))
    args = shlex.split(line)
    assert "--disable-log-bin" in args
    assert "--start-position=1234567" in args
    assert "--stop-datetime=2026-11-04 15:15:00" in args
    # filter by the SOURCE name: MariaDB 11.8 applies --database before --rewrite-db (verified)
    assert "--rewrite-db=_abc->_new" in args and "--database=_abc" in args
    assert "/binlogs/mysql-bin.000042" in args and args.index("/binlogs/mysql-bin.000042") < args.index(
        "/binlogs/mysql-bin.000043")
    assert "/work/replay.sql" in line
    same = pt.decode_script(files, 4, T0, "_abc", "_abc")
    assert "--rewrite-db" not in same and "--database=_abc" in same
    with pytest.raises(pt.PitrError):
        pt.decode_script([], 4, T0, "_abc", "_abc")
    with pytest.raises(v.ValidationError):
        pt.decode_script(files, 4, T0, "_abc", "x`y")
    apply = pt.apply_script("_new")
    assert "mariadb --binary-mode" in apply and "-h nexaploy-db -u root _new < /work/replay.sql" in apply
    assert pt.parse_decode_counts("x\nnexaploy-pitr: bytes=10 row_events=3 commits=2\n") == {
        "bytes": 10, "row_events": 3, "commits": 2}
    assert pt.parse_decode_counts("nothing") is None


def test_replay_container():
    argv = pt.replay_container("mariadb:11.8", "echo", "/b", "/w")
    assert argv[argv.index("--user") + 1] == "3101:3101"
    envs = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    assert envs == ["MYSQL_PWD", "TZ=UTC"]  # --stop-datetime is read in the client's time zone
    vols = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert vols == ["/b:/binlogs:ro", "/w:/work"]
    with pytest.raises(v.ValidationError):
        pt.replay_container("--privileged", "echo", "/b", "/w")


def test_times_and_names():
    assert pt.parse_time("2026-11-04T15:15:00Z") == T0
    assert pt.parse_time("2026-11-04 17:15:00+02:00") == T0
    assert pt.parse_time("2026-11-04T15:15:00") == T0  # naive = UTC
    with pytest.raises(v.ValidationError):
        pt.parse_time("yesterday")
    assert pt.iso(T0) == "2026-11-04T15:15:00Z"
    assert pt.stop_datetime(pt.parse_time("2026-11-04T18:15:00+03:00")) == "2026-11-04 15:15:00"
    assert pt.pitr_site_name("erp.example.com", T0) == "erp-pitr-202611041515.example.com"
    long = pt.pitr_site_name("a" * 60 + ".example.com", T0)
    assert len(long.split(".")[0]) <= 63 and long.endswith("-pitr-202611041515.example.com")


def test_restore_pitr_params():
    base = {"site": "a.example.com", "backup_id": "20261104T120000Z", "target_time": "2026-11-04T15:15:00+00:00"}
    p = SiteRestorePitr.validate_params(base)
    assert p["target_site"] == "a-pitr-202611041515.example.com" and p["in_place"] is False
    assert p["target_time"] == "2026-11-04T15:15:00Z"
    with pytest.raises(ValidationError, match="confirm"):
        SiteRestorePitr.validate_params({**base, "in_place": True})
    with pytest.raises(ValidationError, match="confirm"):
        SiteRestorePitr.validate_params({**base, "in_place": True, "confirm": "b.example.com"})
    p = SiteRestorePitr.validate_params({**base, "in_place": True, "confirm": "A.example.com "})
    assert p["target_site"] == "a.example.com"
    with pytest.raises(ValidationError, match="in_place"):
        SiteRestorePitr.validate_params({**base, "target_site": "a.example.com"})
    with pytest.raises(ValidationError):
        SiteRestorePitr.validate_params({**base, "remote_path": "r:x/y"})
    with pytest.raises(ValidationError):
        SiteRestorePitrParams.model_validate({**base, "target_time": "soon"})
    assert SiteRestorePitr.step_names({"in_place": True})[5:8] == ["safety_backup", "maintenance_on", "restore"]
    assert "ensure_site" in SiteRestorePitr.step_names({"in_place": False})


# --------------------------------------------------------------------------- jobs
def _binlog_status(runner, value="1\tROW\n"):
    runner.on("SELECT @@log_bin, @@binlog_format", stdout=value)


def _write_gz(path: Path, text: str) -> None:
    with gzip.open(path, "wb") as fh:
        fh.write(text.encode())


def _frappe_backup(site_dir: Path, stamp="20261104_120000", slug="a_example_com"):
    d = site_dir / "private" / "backups"
    d.mkdir(parents=True, exist_ok=True)
    names = {
        "database": f"{stamp}-{slug}-database.sql.gz",
        "public": f"{stamp}-{slug}-files.tar",
        "private": f"{stamp}-{slug}-private-files.tar",
        "config": f"{stamp}-{slug}-site_config_backup.json",
    }
    _write_gz(d / names["database"], "-- MariaDB dump (bench)\n")
    (d / names["public"]).write_bytes(b"PUBLIC")
    (d / names["private"]).write_bytes(b"PRIVATE")
    (d / names["config"]).write_text(json.dumps({"encryption_key": "ENCKEY123"}))
    return d, names


def _script_bench_backup(runner, site_dir):
    n = iter(range(10, 60))  # a new file name per `bench backup`, like bench's timestamps

    runner.on(" backup", result=side_effect(lambda call: _frappe_backup(site_dir, stamp=f"20261104_1200{next(n)}")))


def _script_pitr_dump(runner):
    def dump(call):
        out = call.args[call.args.index("-v") + 1].split(":")[0]
        name = call.args[-1].rsplit("/out/", 1)[-1].strip()
        assert call.env == {"MYSQL_PWD": DB_PW}
        _write_gz(Path(out) / name, DUMP_HEADER + "CREATE TABLE `tabDefaultValue` (x int);\n")

    runner.on("--master-data=2", result=side_effect(dump))


def test_site_backup_records_binlog_position(ctx, worker, runner, settings):
    make_bench(ctx, "erp-v15")
    site_dir = make_site(ctx, "erp-v15", "a.example.com")
    bench_running(runner)
    _script_bench_backup(runner, site_dir)
    _binlog_status(runner)
    _script_pitr_dump(runner)
    job = run_job(ctx, worker, "site.backup", {"site": "a.example.com", "upload": False, "pitr": True})
    assert job["status"] == "success", job
    binlog = job["result"]["binlog"]
    assert binlog["file"] == "mysql-bin.000042" and binlog["position"] == 1234567
    assert binlog["gtid"] == "0-1-9876" and binlog["db_name"] == "_abc" and binlog["ready_at"].endswith("Z")
    assert job["result"]["manifest"]["binlog"] == binlog
    manifest = json.loads((Path(job["result"]["path"]) / "manifest.json").read_text())
    assert manifest["binlog"]["position"] == 1234567
    dump = runner.find("--master-data=2")[0]
    assert dump.args[dump.args.index("--entrypoint") + 2] == IMAGE  # the bench's own client
    assert DB_PW not in json.dumps(job) and DB_PW not in " ".join(dump.args)
    assert [s["name"] for s in job["steps"]] == ["backup", "pitr_dump", "stage", "upload", "prune"]


def test_site_backup_without_binlog_keeps_bench_dump(ctx, worker, runner):
    make_bench(ctx, "erp-v15")
    site_dir = make_site(ctx, "erp-v15", "a.example.com")
    bench_running(runner)
    _script_bench_backup(runner, site_dir)
    _binlog_status(runner, "0\tMIXED\n")
    job = run_job(ctx, worker, "site.backup", {"site": "a.example.com", "upload": False, "pitr": True})
    assert job["status"] == "success", job
    assert job["result"]["binlog"] is None and "off" in job["result"]["pitr_error"]
    assert "binlog" not in job["result"]["manifest"]
    assert not runner.called("--master-data")
    # a failing dump does not fail the backup either
    _binlog_status(runner)
    runner.on("--master-data=2", returncode=2, stderr="Access denied")
    job = run_job(ctx, worker, "site.backup", {"site": "a.example.com", "upload": False, "pitr": True})
    assert job["status"] == "success" and "Access denied" in job["result"]["pitr_error"]
    # not requested: no query at all
    runner.reset()
    time.sleep(1.1)  # a new backup_id (one per second)
    job = run_job(ctx, worker, "site.backup", {"site": "a.example.com", "upload": False})
    assert job["status"] == "success" and "binlog" not in job["result"], job["error"]
    assert not runner.called("@@log_bin")


def _datadir(settings, files: dict[str, datetime]) -> Path:
    d = settings.root / "db" / "data"
    d.mkdir(parents=True, exist_ok=True)
    for name, mtime in files.items():
        (d / name).write_bytes(b"\xfebin" + name.encode())
        os.utime(d / name, (mtime.timestamp(), mtime.timestamp()))
    (d / "mysql-bin.index").write_text("".join(f"./{n}\n" for n in files))
    return d


def test_archive_binlogs(ctx, worker, runner, settings):
    settings.rclone_conf.write_text("[nexaploy-backups]\ntype = s3\n")
    _datadir(settings, {f"mysql-bin.00000{i}": T0 for i in (1, 2, 3)})
    _binlog_status(runner)
    listed = []
    runner.on(["rclone", "--config"], result=side_effect(
        lambda call: listed.append(Path(call.args[call.args.index("--files-from") + 1]).read_text())
        if "--files-from" in call.args else None))
    job = run_job(ctx, worker, "server.archive_binlogs", {})
    assert job["status"] == "success", job
    assert runner.called("FLUSH BINARY LOGS")
    copy = next(c for c in runner.find("rclone") if "copy" in c.args)
    assert "--immutable" in copy.args
    assert copy.args[-2:] == [str(settings.root / "db" / "data"), "nexaploy-backups:srv-test/_binlogs"]
    # the active (last) file is not uploaded
    assert listed == ["mysql-bin.000001\nmysql-bin.000002\n"]
    assert job["result"]["archived"] == ["mysql-bin.000001", "mysql-bin.000002"]
    delete = next(c for c in runner.find("rclone") if "delete" in c.args)
    assert delete.args[-3:] == ["--min-age", "7d", "nexaploy-backups:srv-test/_binlogs"]
    assert not list(settings.data_dir.glob("binlog-archive-*.list"))
    assert DB_PW not in json.dumps(job)


def test_archive_binlogs_when_disabled_or_unconfigured(ctx, worker, runner, settings):
    _binlog_status(runner, "0\tROW\n")
    job = run_job(ctx, worker, "server.archive_binlogs", {"keep_days": 3})
    assert job["status"] == "success" and job["result"]["enabled"] is False
    assert not runner.called("FLUSH") and not runner.called("rclone")
    _binlog_status(runner)
    job = run_job(ctx, worker, "server.archive_binlogs", {"rotate": False})
    assert job["status"] == "success" and job["result"]["configured"] is False
    assert not runner.called("FLUSH") and not runner.called("rclone")
    with pytest.raises(ValidationError):
        ctx.enqueue("server.archive_binlogs", {"keep_days": 0})


def _positioned_backup(ctx, bench="erp-v15", site="a.example.com", ready_at=T0 - timedelta(hours=3)):
    src = ctx.settings.root / "tmp-src"
    d, names = _frappe_backup(src)
    stamp = "20261104T120000Z"
    binlog = {"file": "mysql-bin.000042", "position": 1234567, "gtid": "0-1-9876", "db_name": "_abc",
              "ready_at": pt.iso(ready_at), "server_id": "srv-test"}
    ctx.backups.stage(bench, site, stamp, d, names.values(), IMAGE, extra={"binlog": binlog})
    return stamp


def _script_restore_env(ctx, runner, settings, target="a-pitr-202611041515.example.com", archive=None):
    bench_running(runner)
    _binlog_status(runner)
    runner.on(["docker", "inspect"], stdout="mariadb:11.8\n")
    runner.on("mariadb-binlog", stdout="nexaploy-pitr: bytes=2048 row_events=17 commits=5\n")
    if target:
        runner.on("new-site", result=side_effect(
            lambda call: make_site(ctx, "erp-v15", target, extra={"db_name": "_new"})))
    _datadir(settings, {
        "mysql-bin.000041": T0 - timedelta(hours=4),
        "mysql-bin.000042": T0 - timedelta(hours=2),
        "mysql-bin.000043": T0 + timedelta(minutes=20),
        "mysql-bin.000044": T0 + timedelta(hours=1),  # active after the FLUSH
    })


def _replay_calls(runner):
    decode = next(c for c in runner.find("docker run") if "mariadb-binlog" in c.cmdline)
    apply = next(c for c in runner.find("docker run") if "--binary-mode" in c.cmdline)
    return decode, apply


def test_restore_pitr_into_new_site(ctx, worker, runner, settings):
    make_bench(ctx, "erp-v15")
    src = make_site(ctx, "erp-v15", "a.example.com")
    (src / "public" / "files").mkdir(parents=True)
    (src / "public" / "files" / "late-upload.png").write_bytes(b"PNG")
    stamp = _positioned_backup(ctx)
    _script_restore_env(ctx, runner, settings)
    job = run_job(ctx, worker, "site.restore_pitr", {
        "site": "a.example.com", "backup_id": stamp, "target_time": "2026-11-04T15:15:00Z"})
    assert job["status"] == "success", (job["error"], [s["output"][-400:] for s in job["steps"]])
    target = "a-pitr-202611041515.example.com"
    assert job["result"]["target_site"] == target
    # the base backup is restored into the new site, never into the source
    restore = runner.find(" restore ")[0].args
    assert restore[restore.index("--site") + 1] == target
    decode, apply = _replay_calls(runner)
    script = decode.args[-1]
    assert "--start-position=1234567" in script and "'--stop-datetime=2026-11-04 15:15:00'" in script
    assert "'--rewrite-db=_abc->_new'" in script and "--database=_abc" in script
    # 000041 predates the backup; 000044 is the active file
    assert "/binlogs/mysql-bin.000042 /binlogs/mysql-bin.000043 >" in script
    assert job["result"]["binlogs"] == ["mysql-bin.000042", "mysql-bin.000043"]
    assert decode.args[decode.args.index("--entrypoint") + 2] == "mariadb:11.8"  # the DB server's own tools
    assert apply.env == {"MYSQL_PWD": DB_PW} and DB_PW not in apply.cmdline
    assert job["result"]["decoded"] == {"bytes": 2048, "row_events": 17, "commits": 5}
    assert runner.called("FLUSH BINARY LOGS")
    # order: flush before decode, restore before apply
    lines = runner.cmdlines()
    first = lambda needle: next(i for i, l in enumerate(lines) if needle in l)  # noqa: E731
    assert first("FLUSH BINARY LOGS") < first(" restore ") < first("mariadb-binlog") < first("--binary-mode")
    cfg = ctx.sites.site_config("erp-v15", target)
    assert cfg[PITR_CONFIG_KEY] == "a.example.com" and cfg["mute_emails"] == 1 and cfg["pause_scheduler"] == 1
    assert (ctx.sites.site_dir("erp-v15", target) / "public" / "files" / "late-upload.png").read_bytes() == b"PNG"
    assert ctx.proxy.has_route(target)
    assert runner.called(f"--site {target} migrate")
    assert not (settings.backups_dir / ".pitr" / job["id"]).exists()
    blob = json.dumps(job)
    assert DB_PW not in blob and "ENCKEY123" not in blob
    # the source site is untouched
    assert "maintenance_mode" not in ctx.sites.site_config("erp-v15", "a.example.com")


def test_restore_pitr_fetches_archived_binlogs_and_detects_gaps(ctx, worker, runner, settings):
    settings.rclone_conf.write_text("[nexaploy-backups]\ntype = s3\n")
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", "a.example.com")
    stamp = _positioned_backup(ctx)
    _script_restore_env(ctx, runner, settings)
    (settings.root / "db" / "data" / "mysql-bin.index").write_text("./mysql-bin.000043\n./mysql-bin.000044\n")
    os.unlink(settings.root / "db" / "data" / "mysql-bin.000042")  # expired locally, archived
    listing = [{"Name": "mysql-bin.000041", "Size": 1, "ModTime": "2026-11-04T11:00:00Z"},
               {"Name": "mysql-bin.000042", "Size": 1, "ModTime": "2026-11-04T13:30:00Z"}]
    runner.on(["lsjson"], stdout=json.dumps(listing))
    runner.on(["copyto"], result=side_effect(lambda call: Path(call.args[-1]).write_bytes(b"bin")))
    job = run_job(ctx, worker, "site.restore_pitr", {
        "site": "a.example.com", "backup_id": stamp, "target_time": "2026-11-04T15:15:00Z"})
    assert job["status"] == "success", job["error"]
    assert job["result"]["binlogs_from_archive"] == ["mysql-bin.000042"]
    copyto = runner.find("copyto")[0].args
    assert copyto[-2] == "nexaploy-backups:srv-test/_binlogs/mysql-bin.000042"

    # a gap (000042 neither local nor archived) fails before anything is restored
    runner.reset()
    runner.on(["lsjson"], stdout=json.dumps(listing[:1]))
    job = run_job(ctx, worker, "site.restore_pitr", {
        "site": "a.example.com", "backup_id": stamp, "target_time": "2026-11-04T15:16:00Z"})
    assert job["status"] == "failure" and "no longer available" in job["error"]
    assert not runner.called(" restore ") and not runner.called("new-site")


def test_restore_pitr_refuses_bad_targets(ctx, worker, runner, settings):
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", "a.example.com")
    stamp = _positioned_backup(ctx, ready_at=T0 - timedelta(hours=3))
    _script_restore_env(ctx, runner, settings)
    # target before the backup became restorable
    job = run_job(ctx, worker, "site.restore_pitr", {
        "site": "a.example.com", "backup_id": stamp, "target_time": "2026-11-04T11:00:00Z"})
    assert job["status"] == "failure" and "choose an earlier backup" in job["error"]
    # target in the future
    future = pt.iso(datetime.now(timezone.utc) + timedelta(hours=1))
    job = run_job(ctx, worker, "site.restore_pitr", {"site": "a.example.com", "backup_id": stamp,
                                                     "target_time": future})
    assert job["status"] == "failure" and "not in the past" in job["error"]
    # an unrelated existing site is never overwritten
    make_site(ctx, "erp-v15", "other.example.com")
    with pytest.raises(Conflict):
        ctx.enqueue("site.restore_pitr", {"site": "a.example.com", "backup_id": stamp,
                                          "target_time": "2026-11-04T15:15:00Z",
                                          "target_site": "other.example.com"}, precheck=True)
    with pytest.raises(NotFound):
        ctx.enqueue("site.restore_pitr", {"site": "gone.example.com", "remote_path": "r:x/y",
                                          "target_time": "2026-11-04T15:15:00Z"}, precheck=True)
    # a backup without a binlog position
    src = ctx.settings.root / "tmp-src2"
    d, names = _frappe_backup(src, stamp="20261104_130000")
    ctx.backups.stage("erp-v15", "a.example.com", "20261104T130000Z", d, names.values(), IMAGE)
    job = run_job(ctx, worker, "site.restore_pitr", {
        "site": "a.example.com", "backup_id": "20261104T130000Z", "target_time": "2026-11-04T15:15:00Z"})
    assert job["status"] == "failure" and "no binlog position" in job["error"]


def test_restore_pitr_in_place(ctx, worker, runner, settings):
    make_bench(ctx, "erp-v15")
    site_dir = make_site(ctx, "erp-v15", "a.example.com", extra={"maintenance_mode": 0})
    stamp = _positioned_backup(ctx)
    _script_restore_env(ctx, runner, settings, target=None)
    _script_bench_backup(runner, site_dir)
    seen = {}

    def apply(call):
        seen["maintenance"] = ctx.sites.site_config("erp-v15", "a.example.com").get("maintenance_mode")

    runner.on("--binary-mode", result=side_effect(apply))
    params = {"site": "a.example.com", "backup_id": stamp, "target_time": "2026-11-04T15:15:00Z",
              "in_place": True, "confirm": "a.example.com"}
    job = run_job(ctx, worker, "site.restore_pitr", params)
    assert job["status"] == "success", (job["error"], [s["output"][-400:] for s in job["steps"]])
    assert [s["name"] for s in job["steps"]][5:7] == ["safety_backup", "maintenance_on"]
    # the current state was backed up before it was overwritten
    assert job["result"]["safety_backup"]["backup_id"]
    assert Path(job["result"]["safety_backup"]["path"]).is_dir()
    decode, _ = _replay_calls(runner)
    assert "--rewrite-db" not in decode.args[-1] and "--database=_abc" in decode.args[-1]
    assert not runner.called("new-site")
    assert seen["maintenance"] == 1  # replayed while in maintenance
    cfg = ctx.sites.site_config("erp-v15", "a.example.com")
    assert cfg["maintenance_mode"] == 0 and PITR_CONFIG_KEY not in cfg and "mute_emails" not in cfg


def test_restore_pitr_interrupted_apply_restores_base_again(ctx, worker, runner, settings):
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", "a.example.com")
    stamp = _positioned_backup(ctx)
    _script_restore_env(ctx, runner, settings)
    runner.on("--binary-mode", returncode=1, stderr="ERROR 2013: Lost connection", times=1)
    job = run_job(ctx, worker, "site.restore_pitr", {
        "site": "a.example.com", "backup_id": stamp, "target_time": "2026-11-04T15:15:00Z"})
    assert job["status"] == "failure" and "Lost connection" in job["error"]
    restores = len(runner.find(" restore "))
    assert ctx.store.retry(job["id"])
    assert worker.run_once()
    job = ctx.store.get(job["id"])
    assert job["status"] == "success", job["error"]
    # replaying on top of a partially replayed database would apply changes twice
    assert len(runner.find(" restore ")) == restores + 1
    assert len(runner.find("--binary-mode")) == 2
    apply_step = next(s for s in job["steps"] if s["name"] == "apply")
    assert "restoring the base backup again" in apply_step["output"]


def test_restore_pitr_api(ctx, settings):
    from fastapi.testclient import TestClient

    from nexaploy_agent.api import create_app

    from .conftest import TOKEN

    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", "a.example.com")
    stamp = _positioned_backup(ctx)
    client = TestClient(create_app(ctx))
    h = {"Authorization": f"Bearer {TOKEN}"}
    r = client.post("/v1/sites/a.example.com/restore-pitr", headers=h,
                    json={"backup_id": stamp, "target_time": "2026-11-04T15:15:00Z"})
    assert r.status_code == 202 and r.json()["kind"] == "site.restore_pitr"
    r = client.post("/v1/sites/a.example.com/restore-pitr", headers=h,
                    json={"backup_id": stamp, "target_time": "2026-11-04T15:15:00Z", "in_place": True})
    assert r.status_code == 422
    r = client.post("/v1/server/archive-binlogs", headers=h, json={"keep_days": 7})
    assert r.status_code == 202 and r.json()["kind"] == "server.archive_binlogs"
