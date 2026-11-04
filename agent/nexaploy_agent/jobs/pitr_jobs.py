"""Point-in-time recovery jobs (ARCHITECTURE.md §14).

* ``site.backup`` step ``pitr_dump`` (:func:`pitr_dump`) - replace bench's database dump with
  ``mariadb-dump --single-transaction --master-data=2 --gtid`` and record the snapshot's binlog
  coordinates in the result and the manifest (``binlog``).
* ``server.archive_binlogs`` ``{rotate, keep_days, prefix?}`` - FLUSH BINARY LOGS, upload the
  closed binlog files to ``<remote>:<prefix>/_binlogs/`` (``rclone copy --immutable``), delete
  archived files older than ``keep_days``.
* ``site.restore_pitr`` ``{site, backup_id|remote_path, target_time, target_site?, bench?,
  in_place, confirm, with_files, scrub}`` - restore the base backup into a new site (default
  ``<first label>-pitr-<YYYYmmddHHMM>.<rest>``, emails muted / scheduler paused) or, with
  ``in_place`` + ``confirm == site``, into the site itself (safety backup + maintenance mode
  first), then replay the binlog from the backup's position up to ``target_time`` (UTC).

Steps are check-then-act. The binlog apply is the one step that cannot simply run twice: it
records ``apply_started`` before it starts, and a resumed job restores the base backup again
before replaying.
"""

from __future__ import annotations

import os
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import field_validator, model_validator

from .. import validation as v
from ..config import FRAPPE_UID, MARIADB_UID
from ..fsutil import chown, ensure_dir
from ..runtime import pitr as pt
from ..runtime.backup import BackupError, classify
from ..runtime.lifecycle import RESTORE_STAGING, restore_args
from ..runtime.site import SiteManager
from .base import BaseParams, Conflict, Job, NotFound
from .site_jobs import _SiteJob

PITR_CONFIG_KEY = "nexaploy_pitr_of"
PITR_TARGET_KEY = "nexaploy_pitr_target"
# An inspection copy must not mail customers or run scheduled jobs (reversible site_config keys).
COPY_SCRUB = {"mute_emails": 1, "pause_scheduler": 1}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def db_datadir(job: Job) -> Path:
    return job.settings.root / "db" / "data"


def archive_remote(job: Job, prefix: str | None = None) -> str:
    base = v.remote_path(prefix) if prefix else job.settings.backup_prefix
    return f"{job.settings.backup.remote}:{base}/{pt.ARCHIVE_DIR}"


def binlog_status(job: Job, password: str) -> dict[str, Any]:
    res = job.ctx.runner.run(pt.db_query_command(pt.BINLOG_STATUS_SQL), env={"MYSQL_PWD": password}, timeout=60)
    job.check(res, "binlog status query")
    return pt.parse_binlog_status(res.stdout)


def flush_binlogs(job: Job, password: str) -> None:
    res = job.ctx.runner.run(pt.db_query_command(pt.FLUSH_SQL), env={"MYSQL_PWD": password}, timeout=120)
    job.check(res, "FLUSH BINARY LOGS")


def _root_password(job: Job) -> str:
    try:
        return job.secret(job.settings.db_root_password())  # type: ignore[return-value]
    except Exception as exc:  # ConfigError
        job.fail(str(exc))
        raise  # unreachable


# --------------------------------------------------------------------------- backup dump
def pitr_dump(job: _SiteJob, bench: str) -> None:
    """``site.backup`` step: dump with binlog coordinates over bench's database file.

    The backup never fails because of PITR: when binary logging is off or the dump cannot
    be made, bench's own dump is kept and ``binlog`` is None (``pitr_error`` says why).
    """
    if not getattr(job.p, "pitr", False):
        job.log("PITR dump not requested")
        return
    if "binlog" in job.result:
        job.skip(f"binlog position recorded: {job.result['binlog']}")
        return
    files = job.result.get("files") or []
    db_file = next((n for n in files if classify(n) == "database"), None)
    if not db_file:
        job.fail("bench backup produced no database dump")

    def unavailable(reason: str) -> None:
        job.log(f"warning: point-in-time recovery unavailable for this backup: {reason}; "
                "keeping the database dump of bench backup")
        job.result.update({"binlog": None, "pitr_error": reason})

    if not db_file.endswith(".sql.gz"):
        return unavailable(f"unexpected dump format {db_file}")
    try:
        database = pt.db_name(job.ctx.sites.site_config(bench, job.site).get("db_name"))
    except v.ValidationError as exc:
        return unavailable(str(exc))
    password = _root_password(job)
    reason = pt.pitr_ready(binlog_status(job, password))
    if reason:
        return unavailable(reason)
    image = job.ctx.benches.image(bench)
    if not image:
        return unavailable(f"bench {bench} has no IMAGE")
    db_host = job.ctx.benches.read_env(bench).get("DB_HOST") or job.settings.bench.db_host
    out_dir = job.ctx.backups.frappe_backup_dir(job.ctx.sites.site_dir(bench, job.site))
    try:
        argv = pt.dump_command(image, out_dir, db_file, database, db_host)
    except v.ValidationError as exc:
        return unavailable(str(exc))
    res = job.ctx.runner.run(argv, env={"MYSQL_PWD": password}, timeout=6 * 3600)
    if not res.ok:
        return unavailable(f"mariadb-dump failed (exit {res.returncode}): {(res.stderr or '').strip()[-300:]}")
    ready_at = pt.iso(utcnow())  # >= the snapshot: the earliest time this backup can restore to
    try:
        coords = pt.parse_dump_coordinates(pt.read_dump_header(out_dir / db_file))
    except (OSError, EOFError, pt.PitrError) as exc:
        return unavailable(f"cannot read the binlog position from the dump: {exc}")
    job.result["binlog"] = {
        **coords,
        "db_name": database,
        "ready_at": ready_at,
        "server_id": job.settings.agent.server_id,
    }
    job.log(f"database dumped at binlog {coords['file']}:{coords['position']} (gtid {coords['gtid']}); "
            f"restorable to any time after {ready_at}")


# --------------------------------------------------------------------------- archive
class ArchiveBinlogsParams(BaseParams):
    rotate: bool = True
    keep_days: int = 7
    prefix: str | None = None

    @field_validator("keep_days")
    @classmethod
    def _days(cls, value: int) -> int:
        if value < 1:
            raise ValueError("keep_days must be >= 1")
        return value

    @field_validator("prefix")
    @classmethod
    def _prefix(cls, value: str | None) -> str | None:
        return v.remote_path(value) if value else None


class ServerArchiveBinlogs(Job):
    """Rotate the binlog, upload closed files, prune old archived files. Idempotent:
    ``rclone copy`` skips files already uploaded; ``--immutable`` refuses to overwrite a
    different file of the same name (a re-initialised database restarting at 000001)."""

    kind = "server.archive_binlogs"
    steps = ("status", "rotate", "upload", "prune")
    Params = ArchiveBinlogsParams

    def _enabled(self) -> bool:
        return bool(self.result.get("enabled"))

    def step_status(self) -> None:
        password = _root_password(self)
        status = binlog_status(self, password)
        reason = pt.pitr_ready(status)
        self.result.update({"enabled": reason is None, "binlog_format": status.get("format"),
                            "archive": archive_remote(self, self.p.prefix), "archived": [],
                            "configured": self.settings.rclone_remote_configured()})
        if reason:
            self.result["reason"] = reason
            self.log(f"nothing to archive: {reason}")
        else:
            self.log(f"binary log on ({status.get('format')})")

    def step_rotate(self) -> None:
        if not self._enabled() or not self.p.rotate:
            self.skip("rotation not needed")
            return
        if self.result.get("rotated"):
            self.skip("binlog already rotated")
            return
        flush_binlogs(self, _root_password(self))
        self.result["rotated"] = True
        self.log("FLUSH BINARY LOGS: the current file is closed and can be archived")

    def step_upload(self) -> None:
        if not self._enabled():
            self.skip("binary logging is off")
            return
        if not self.result.get("configured"):
            self.log(f"warning: rclone remote {self.settings.backup.remote!r} not configured; binlogs stay local")
            return
        datadir = db_datadir(self)
        try:
            index = pt.parse_binlog_index((datadir / f"{pt.BINLOG_BASENAME}.index").read_text())
        except OSError as exc:
            self.fail(f"cannot read the binlog index in {datadir}: {exc}")
        closed = [n for n in pt.closed_binlogs(index) if (datadir / n).is_file()]
        if not closed:
            self.skip("no closed binlog file")
            return
        listing = self.settings.data_dir / f"binlog-archive-{self.id}.list"
        ensure_dir(listing.parent, mode=0o700)
        listing.write_text("".join(f"{n}\n" for n in closed))
        try:
            res = self.ctx.backups.rclone("copy", "--immutable", "--files-from", str(listing), str(datadir),
                                          self.result["archive"], timeout=6 * 3600)
        finally:
            listing.unlink(missing_ok=True)
        self.check(res, "rclone copy (binlogs)")
        self.result.update({"archived": closed, "oldest_local": closed[0], "newest_archived": closed[-1]})
        self.log(f"{len(closed)} closed binlog files present in {self.result['archive']} "
                 f"({closed[0]} .. {closed[-1]})")

    def step_prune(self) -> None:
        if not self._enabled() or not self.result.get("configured"):
            self.skip("nothing to prune")
            return
        res = self.ctx.backups.rclone("delete", "--min-age", f"{self.p.keep_days}d", self.result["archive"],
                                      timeout=3600)
        if not res.ok and "not found" in (res.stderr or res.stdout).lower():
            self.skip("archive is empty")
            return
        self.check(res, "rclone delete (old binlogs)")
        self.log(f"archived binlogs older than {self.p.keep_days} days deleted")


# --------------------------------------------------------------------------- restore
class SiteRestorePitrParams(BaseParams):
    site: str
    target_time: str
    backup_id: str | None = None
    remote_path: str | None = None
    target_site: str | None = None
    bench: str | None = None
    in_place: bool = False
    confirm: str | None = None
    with_files: bool = True
    scrub: bool = True
    binlog_prefix: str | None = None

    @field_validator("site")
    @classmethod
    def _site(cls, value: str) -> str:
        return v.site_name(value.lower())

    @field_validator("target_site")
    @classmethod
    def _target(cls, value: str | None) -> str | None:
        return v.site_name(value.lower()) if value else None

    @field_validator("target_time")
    @classmethod
    def _time(cls, value: str) -> str:
        return pt.iso(pt.parse_time(value))

    @field_validator("backup_id")
    @classmethod
    def _bid(cls, value: str | None) -> str | None:
        return v.backup_id(value) if value else None

    @field_validator("bench")
    @classmethod
    def _bench(cls, value: str | None) -> str | None:
        return v.bench_name(value) if value else None

    @field_validator("remote_path")
    @classmethod
    def _remote(cls, value: str | None) -> str | None:
        if not value:
            return None
        v.remote_path(value.partition(":")[2] if ":" in value else value)
        return value

    @field_validator("binlog_prefix")
    @classmethod
    def _prefix(cls, value: str | None) -> str | None:
        return v.remote_path(value) if value else None

    @model_validator(mode="after")
    def _shape(self) -> "SiteRestorePitrParams":
        if bool(self.backup_id) == bool(self.remote_path):
            raise ValueError("exactly one of backup_id or remote_path is required")
        if self.in_place:
            if (self.confirm or "").strip().lower() != self.site:
                raise ValueError("in_place overwrites the site: confirm must repeat the site name")
            if self.target_site and self.target_site != self.site:
                raise ValueError("in_place restores into the site itself; target_site must be empty")
            self.target_site = self.site
        else:
            if not self.target_site:
                self.target_site = pt.pitr_site_name(self.site, pt.parse_time(self.target_time))
            if self.target_site == self.site:
                raise ValueError("target_site equals site: pass in_place with confirm to overwrite it")
        return self


COPY_STEPS = ("check", "locate", "fetch", "verify", "collect_binlogs", "ensure_site", "restore",
              "restore_config", "scrub", "sync_files", "decode", "apply", "migrate", "write_route", "cleanup")
IN_PLACE_STEPS = ("check", "locate", "fetch", "verify", "collect_binlogs", "safety_backup", "maintenance_on",
                  "restore", "restore_config", "decode", "apply", "migrate", "maintenance_off", "write_route",
                  "cleanup")


class SiteRestorePitr(_SiteJob):
    """Restore ``site`` as it was at ``target_time`` (base backup + binlog replay)."""

    kind = "site.restore_pitr"
    steps = COPY_STEPS
    Params = SiteRestorePitrParams

    @classmethod
    def step_names(cls, params: dict[str, Any]) -> list[str]:
        return list(IN_PLACE_STEPS if params.get("in_place") else COPY_STEPS)

    @property
    def target(self) -> str:
        return self.p.target_site

    @classmethod
    def precheck(cls, ctx, params: dict[str, Any]) -> None:
        source_bench = ctx.sites.find_bench(params["site"])
        bench = params.get("bench") or source_bench
        if params.get("in_place"):
            if not source_bench:
                raise NotFound(f"site {params['site']!r} not found")
            if bench != source_bench:
                raise Conflict(f"site {params['site']} lives on bench {source_bench}")
        elif not bench:
            raise NotFound(f"site {params['site']!r} not found; pass bench to restore it as a new site")
        if not ctx.benches.exists(bench):
            raise NotFound(f"bench {bench!r} not found")
        if params.get("backup_id") and not ctx.backups.find_local(params["site"], params["backup_id"]):
            raise NotFound(f"backup {params['backup_id']} not found locally")
        if not params.get("in_place"):
            target = params["target_site"]
            owner = ctx.sites.find_bench(target)
            if owner and ctx.sites.site_config(owner, target).get(PITR_CONFIG_KEY) != params["site"]:
                raise Conflict(f"{target} already exists and is not a point-in-time copy of {params['site']}")
            host_owner = ctx.sites.all_hosts().get(target)
            if host_owner and host_owner != target:
                raise Conflict(f"{target} is already a domain of site {host_owner}")

    # ---- helpers -------------------------------------------------------------------
    @property
    def target_bench(self) -> str:
        return self.result["target_bench"]

    @property
    def target_dt(self) -> datetime:
        return pt.parse_time(self.p.target_time)

    def work_dir(self) -> Path:
        return self.settings.backups_dir / ".pitr" / self.id

    def _target_config(self) -> dict[str, Any]:
        return self.ctx.sites.site_config(self.target_bench, self.target)

    def _write_target_config(self, change) -> bool:
        current = self._target_config()
        new = change(dict(current))
        if new == current:
            return False
        self.ctx.sites.write_site_config(self.target_bench, self.target, new)
        return True

    def _copy_config(self, cfg: dict[str, Any]) -> dict[str, Any]:
        cfg[PITR_CONFIG_KEY] = self.site
        cfg[PITR_TARGET_KEY] = self.p.target_time
        if self.p.scrub:
            cfg.update(COPY_SCRUB)
        return cfg

    def _set_maintenance(self, value: int) -> bool:
        return self._write_target_config(
            lambda cfg: cfg if cfg.get("maintenance_mode", 0) == value else {**cfg, "maintenance_mode": value}
        )

    def _db_image(self) -> str:
        if self.result.get("db_image"):
            return self.result["db_image"]
        res = self.ctx.runner.run(
            ["docker", "inspect", "--format", "{{.Config.Image}}", pt.DB_CONTAINER], timeout=30
        )
        self.check(res, f"docker inspect {pt.DB_CONTAINER}")
        image = res.stdout.strip().splitlines()[-1].strip() if res.stdout.strip() else ""
        if not image:
            self.fail(f"cannot tell the image of {pt.DB_CONTAINER}")
        self.result["db_image"] = image
        return image

    def _restore_base(self) -> None:
        """``bench restore --force`` of the base backup into the target site."""
        bench, files = self.target_bench, self.result["restore_files"]
        src = Path(self.result["dir"])
        staging = self.ctx.benches.sites_dir(bench) / RESTORE_STAGING / self.id
        ensure_dir(staging, FRAPPE_UID, FRAPPE_UID, 0o750)
        try:
            for name in files.values():
                shutil.copy2(src / name, staging / name)
                chown(staging / name, FRAPPE_UID, FRAPPE_UID)
            cpath = lambda key: (  # noqa: E731
                self.ctx.sites.container_path(bench, staging / files[key])
                if key in files and (key == "database" or self.p.with_files) else None
            )
            args = restore_args(self.target, cpath("database"), cpath("public"), cpath("private"),
                                _root_password(self))
            self.check(self.ctx.sites.bench(bench, args, timeout=6 * 3600), "bench restore")
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        if self.p.in_place:
            self._set_maintenance(1)  # whatever bench restore did to site_config

    # ---- steps ---------------------------------------------------------------------
    def step_check(self) -> None:
        source_bench = self.ctx.sites.find_bench(self.site)
        bench = self.p.bench or source_bench
        if not bench or not self.ctx.benches.exists(bench):
            self.fail(f"bench {bench or '?'} not found (site {self.site} does not exist; pass bench)")
        if self.p.in_place and source_bench != bench:
            self.fail(f"site {self.site} not found on bench {bench}")
        now = utcnow()
        if self.target_dt >= now:
            self.fail(f"target time {self.p.target_time} is not in the past")
        if not self.p.in_place:
            owner = self.ctx.sites.find_bench(self.target)
            if owner and owner != bench:
                self.fail(f"{self.target} already exists on bench {owner}")
            if owner and not self.result.get("target_created") and \
                    self.ctx.sites.site_config(owner, self.target).get(PITR_CONFIG_KEY) != self.site:
                self.fail(f"{self.target} exists and is not a point-in-time copy of {self.site}; refusing to overwrite it")
            host_owner = self.ctx.sites.all_hosts().get(self.target)
            if host_owner and host_owner != self.target:
                self.fail(f"{self.target} is already a domain of site {host_owner}")
        self.result.update({
            "bench": source_bench or bench, "target_bench": bench, "target_site": self.target,
            "target_time": self.p.target_time, "in_place": self.p.in_place,
        })
        self.log(f"restoring {self.site} as of {self.p.target_time} (UTC) into "
                 f"{'the site itself' if self.p.in_place else self.target} on bench {bench}")

    def step_locate(self) -> None:
        if self.p.backup_id:
            loc = self.ctx.backups.find_local(self.site, self.p.backup_id)
            if not loc:
                self.fail(f"backup {self.p.backup_id} of {self.site} not found locally")
            self.result["dir"] = str(loc.path)
        else:
            self.result["dir"] = str(self.settings.backups_dir / ".restore" / self.site / self.id)
            self.result["remote"] = self.ctx.backups.remote_spec(self.p.remote_path)
        self.log(f"base backup: {self.result.get('remote') or self.result['dir']}")

    def step_fetch(self) -> None:
        d = Path(self.result["dir"])
        if self.p.backup_id:
            self.skip("local backup")
            return
        if (d / "manifest.json").exists():
            self.skip("already fetched")
            return
        self.check(self.ctx.backups.fetch(self.result["remote"], d), "rclone copy")

    def step_verify(self) -> None:
        try:
            manifest = self.ctx.backups.verify(Path(self.result["dir"]))
        except BackupError as exc:
            self.fail(str(exc))
        files: dict[str, str] = {}
        for f in manifest.get("files", []):
            kind = f.get("type") or classify(f["name"])
            if kind:
                files[kind] = f["name"]
        if "database" not in files:
            self.fail("backup has no database dump")
        base = manifest.get("binlog")
        if not isinstance(base, dict) or not base.get("file") or base.get("position") is None:
            self.fail("this backup has no binlog position (taken before point-in-time recovery was enabled); "
                      "choose a later backup")
        try:
            pt.binlog_name(base["file"])
            pt.db_name(base.get("db_name"))
            ready_at = pt.parse_time(base.get("ready_at") or manifest.get("created_at"))
        except v.ValidationError as exc:
            self.fail(f"invalid binlog position in the manifest: {exc}")
        if ready_at > self.target_dt:
            self.fail(f"the backup is restorable from {pt.iso(ready_at)} only, after the target time "
                      f"{self.p.target_time}; choose an earlier backup")
        self.result.update({"restore_files": files, "base": base})
        self.log(f"sha256 verified; base position {base['file']}:{base['position']} of database {base['db_name']}")

    def step_collect_binlogs(self) -> None:
        base = self.result["base"]
        password = _root_password(self)
        reason = pt.pitr_ready(binlog_status(self, password))
        if reason:
            self.fail(f"point-in-time recovery is unavailable: {reason}")
        # Every event before the target is in a closed file after this (target < now).
        flush_binlogs(self, password)
        datadir = db_datadir(self)
        basename, _ = pt.split_binlog(base["file"])
        try:
            index = pt.parse_binlog_index((datadir / f"{basename}.index").read_text())
        except OSError:
            index = []
        active = index[-1:] if index else []
        local: dict[str, datetime | None] = {}
        for name in index:
            try:
                local[name] = datetime.fromtimestamp((datadir / name).stat().st_mtime, timezone.utc)
            except OSError:
                continue
        archive = archive_remote(self, self.p.binlog_prefix)
        remote: dict[str, dict[str, Any]] = {}
        if self.settings.rclone_remote_configured():
            res = self.ctx.backups.rclone("lsjson", "--files-only", archive, timeout=300)
            if res.ok:
                try:
                    remote = pt.parse_rclone_lsjson(res.stdout)
                except pt.PitrError as exc:
                    self.log(f"warning: {exc}")
            elif "not found" not in (res.stderr or res.stdout).lower():
                self.log(f"warning: cannot list {archive}: {(res.stderr or '').strip()[-200:]}")
        available = {name: info.get("mtime") for name, info in remote.items()}
        available.update(local)
        try:
            selected = pt.select_binlogs(available, base["file"], self.target_dt, exclude=active)
        except pt.PitrError as exc:
            self.fail(str(exc))
        dest = ensure_dir(self.work_dir() / "binlogs", MARIADB_UID, MARIADB_UID, 0o750)
        ensure_dir(self.work_dir() / "work", MARIADB_UID, MARIADB_UID, 0o700)
        fetched = []
        for name in selected:
            target = dest / name
            if name in local:
                src = datadir / name
                if not target.exists() or target.stat().st_size != src.stat().st_size:
                    shutil.copy2(src, target)
            elif not target.exists():
                self.check(self.ctx.backups.rclone("copyto", f"{archive}/{name}", str(target), timeout=6 * 3600),
                           f"rclone copyto {name}")
                fetched.append(name)
            os.chmod(target, 0o640)
            chown(target, MARIADB_UID, MARIADB_UID)
        self.result.update({"binlogs": selected, "binlogs_from_archive": fetched})
        self.log(f"{len(selected)} binlog files ({selected[0]} .. {selected[-1]}), "
                 f"{len(fetched)} fetched from the archive")

    def step_safety_backup(self) -> None:
        """In place: back up the current state first (its own result keys)."""
        bench = self.target_bench
        self.take_backup(bench, with_files=True)
        if "path" not in self.result:
            self.stage_backup(bench)
        if "uploaded" not in self.result:
            self.upload_backup(bench, upload=True)
        self.result["safety_backup"] = {
            "backup_id": self.result.get("backup_id"), "path": self.result.get("path"),
            "remote_path": self.result.get("remote_path"),
        }

    def step_maintenance_on(self) -> None:
        if "maintenance_before" not in self.result:
            self.result["maintenance_before"] = self._target_config().get("maintenance_mode", 0) or 0
        if self._set_maintenance(1):
            self.log(f"maintenance_mode on for {self.target}")
        else:
            self.skip("maintenance_mode already on")

    def step_maintenance_off(self) -> None:
        before = int(self.result.get("maintenance_before", 0) or 0)
        if self._set_maintenance(before):
            self.log(f"maintenance_mode restored to {before}")
        else:
            self.skip(f"maintenance_mode already {before}")

    def step_ensure_site(self) -> None:
        bench = self.target_bench
        if self.ctx.sites.exists(bench, self.target):
            self.skip(f"{self.target} exists")
        else:
            password = self.secret(secrets.token_urlsafe(18))  # replaced by the restored database
            db_host = self.ctx.benches.read_env(bench).get("DB_HOST") or self.settings.bench.db_host
            args = SiteManager.new_site_args(self.target, password, _root_password(self), db_host,
                                             legacy_host_flag=self.ctx.sites.legacy_host_flag(bench))
            self.check(self.ctx.sites.bench(bench, args, timeout=3600), "bench new-site")
        self.result["target_created"] = True
        self._write_target_config(self._copy_config)

    def step_restore(self) -> None:
        if self.result.get("replayed"):
            self.skip("binlog already replayed on top of the restored backup")
            return
        self._restore_base()
        self.result["restored"] = True

    def step_restore_config(self) -> None:
        name = self.result["restore_files"].get("config")
        if not name:
            self.skip("backup has no site_config backup")
            return
        try:
            import json

            key = json.loads((Path(self.result["dir"]) / name).read_text()).get("encryption_key")
        except (OSError, ValueError) as exc:
            self.log(f"warning: cannot read {name}: {exc}")
            return
        if not key:
            self.skip("no encryption_key in backup")
            return
        self.secret(key)
        if self._target_config().get("encryption_key") == key:
            self.skip("encryption_key already matches")
            return
        res = self.ctx.sites.bench(self.target_bench, ["--site", self.target, "set-config", "encryption_key", key],
                                   timeout=300)
        self.check(res, "set encryption_key")

    def step_scrub(self) -> None:
        if self._write_target_config(self._copy_config):
            self.log(f"site_config: {PITR_CONFIG_KEY}={self.site}"
                     + (", mute_emails=1, pause_scheduler=1" if self.p.scrub else ""))
        else:
            self.skip("site_config already marked")

    def step_sync_files(self) -> None:
        """Files uploaded after the base backup: copy the ones the copy lacks from the live
        site (the replayed File rows point at them). Nothing is overwritten or deleted."""
        source_bench = self.ctx.sites.find_bench(self.site)
        if not self.p.with_files or not source_bench:
            self.skip("no live source site / files not requested")
            return
        copied = 0
        for sub in ("public/files", "private/files"):
            src_root = self.ctx.sites.site_dir(source_bench, self.site) / sub
            dst_root = self.ctx.sites.site_dir(self.target_bench, self.target) / sub
            if not src_root.is_dir():
                continue
            for dirpath, _dirs, names in os.walk(src_root):
                rel = Path(dirpath).relative_to(src_root)
                for name in names:
                    src = Path(dirpath) / name
                    dst = dst_root / rel / name
                    if dst.exists() or src.is_symlink() or not src.is_file():
                        continue
                    ensure_dir(dst.parent, FRAPPE_UID, FRAPPE_UID, 0o755)
                    shutil.copy2(src, dst)
                    chown(dst, FRAPPE_UID, FRAPPE_UID)
                    copied += 1
        self.result["files_synced"] = copied
        self.log(f"{copied} files copied from {self.site}" if copied else "already done: no file missing")

    def step_decode(self) -> None:
        base = self.result["base"]
        dst = self._target_config().get("db_name")
        try:
            pt.db_name(dst)
            script = pt.decode_script(self.result["binlogs"], int(base["position"]), self.target_dt,
                                      base["db_name"], dst)
            argv = pt.replay_container(self._db_image(), script, self.work_dir() / "binlogs",
                                       self.work_dir() / "work")
        except (v.ValidationError, pt.PitrError) as exc:
            self.fail(str(exc))
        res = self.check(self.ctx.runner.run(argv, timeout=6 * 3600), "mariadb-binlog")
        counts = pt.parse_decode_counts(res.stdout)
        self.result.update({"src_db": base["db_name"], "dst_db": dst, "decoded": counts})
        if counts is None:
            self.fail("mariadb-binlog produced no summary; see the step output")
        rename = f" (renamed {base['db_name']} -> {dst})" if base["db_name"] != dst else ""
        self.log(f"decoded {counts['commits']} transactions, {counts['row_events']} row events{rename}")
        if counts["row_events"] == 0 and counts["commits"] == 0:
            self.log("note: no change to the database between the backup and the target time")

    def step_apply(self) -> None:
        if self.result.get("replayed"):
            self.skip("binlog already replayed")
            return
        if self.result.get("apply_started"):
            self.log("a previous replay was interrupted: restoring the base backup again first")
            self._restore_base()
        self.result["apply_started"] = True
        self.save()
        dst = self.result["dst_db"]
        argv = pt.replay_container(self._db_image(), pt.apply_script(dst), self.work_dir() / "binlogs",
                                   self.work_dir() / "work")
        self.check(self.ctx.runner.run(argv, env={"MYSQL_PWD": _root_password(self)}, timeout=6 * 3600),
                   "replay binlog")
        self.result["replayed"] = True
        self.log(f"binlog replayed into {dst} up to {self.p.target_time}")

    def step_migrate(self) -> None:
        self.check(self.ctx.sites.migrate(self.target_bench, self.target), "bench migrate")

    def step_write_route(self) -> None:
        bench = self.target_bench
        domains = self.ctx.sites.domains(bench, self.target)
        changed = self.ctx.proxy.write_route(self.target, domains, bench)
        self.log(f"route {'written' if changed else 'unchanged'}: {self.target} -> {bench}-frontend")
        self.result["domains"] = domains

    def step_cleanup(self) -> None:
        shutil.rmtree(self.work_dir(), ignore_errors=True)
        if self.p.remote_path and self.result.get("dir"):
            shutil.rmtree(Path(self.result["dir"]), ignore_errors=True)
        self.log("work files removed")
