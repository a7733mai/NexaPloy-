"""Point-in-time recovery: pure logic (no frappe import), ARCHITECTURE.md §11.

All datetimes here are naive UTC (as stored from the agent by utils.parse_agent_datetime).

* binlog_fields(result)        site.backup result -> Site Backup binlog_* fields
* usable_backups(...)          Success backups with a binlog position on this server, inside retention
* pitr_window(...)             {available, start, end, reason}: oldest usable backup .. now - margin
* pick_base_backup(...)        latest usable backup restorable at the target
* target_error(...)            (message, args) unless the target is inside the window
* pitr_site_name(...)          <first label>-pitr-<YYYYmmddHHMM>.<root domain>
* restore_params(...)          site.restore_pitr params for the agent
* failure_status(...)          Site status after a failed site.restore_pitr
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from nexaploy.utils import is_valid_site_name, parse_agent_datetime

# Archived binlogs (and local ones, DB_BINLOG_EXPIRE_SECONDS) are kept this long: the PITR window.
RETENTION_DAYS = 7
# The newest restorable time: binlog events of the last minutes may still be in flight / unflushed.
MARGIN_MINUTES = 5
ARCHIVE_EVERY_MINUTES = 15
BINLOG_FILE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\.\d{6,}$")
BACKUP_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")
# Steps of site.restore_pitr (in place) that change nothing on the site when they fail.
IN_PLACE_HARMLESS_STEPS = ("check", "locate", "fetch", "verify", "collect_binlogs", "safety_backup")


def utcnow() -> datetime:
	return datetime.now(timezone.utc).replace(tzinfo=None)


def iso_z(dt: datetime | None) -> str | None:
	if dt is None:
		return None
	return dt.replace(microsecond=0).isoformat() + "Z"


def parse_target(value: Any) -> datetime:
	"""User input (ISO 8601 with Z/offset; naive = UTC) -> naive UTC, whole seconds."""
	dt = parse_agent_datetime(value) if value not in (None, "") else None
	if dt is None:
		raise ValueError("Choose a valid date and time")
	return dt.replace(microsecond=0)


def binlog_fields(result: Any) -> dict[str, Any]:
	"""``site.backup`` result ``binlog`` -> Site Backup fields (empty when the backup has none)."""
	if not isinstance(result, Mapping):
		return {}
	binlog = result.get("binlog")
	if not isinstance(binlog, Mapping) and isinstance(result.get("manifest"), Mapping):
		binlog = result["manifest"].get("binlog")
	if not isinstance(binlog, Mapping):
		return {}
	file = binlog.get("file")
	try:
		position = int(binlog.get("position"))
	except (TypeError, ValueError):
		return {}
	ready_at = parse_agent_datetime(binlog.get("ready_at"))
	if not (isinstance(file, str) and BINLOG_FILE_RE.match(file)) or position < 0 or ready_at is None:
		return {}
	return {
		"binlog_file": file,
		"binlog_position": position,
		"binlog_gtid": binlog.get("gtid") or None,
		"binlog_db": binlog.get("db_name") or None,
		"binlog_ready_at": ready_at,
	}


def _get(row: Any, key: str) -> Any:
	return row.get(key) if isinstance(row, Mapping) else getattr(row, key, None)


def _ready(row: Any) -> datetime | None:
	value = _get(row, "binlog_ready_at")
	return parse_agent_datetime(value) if value else None


def usable_backups(backups: Iterable[Any], now: datetime, server: str | None = None,
                   retention_days: int = RETENTION_DAYS) -> list[Any]:
	"""Backups a PITR can start from, oldest first: Success, a binlog position, taken on
	``server`` (binlogs are per database server) and inside the binlog retention."""
	cutoff = now - timedelta(days=retention_days)
	out = []
	for row in backups:
		ready = _ready(row)
		if _get(row, "status") != "Success" or not _get(row, "binlog_file") or ready is None:
			continue
		if not _get(row, "remote_path"):
			continue
		if server and _get(row, "binlog_server") and _get(row, "binlog_server") != server:
			continue
		if ready < cutoff or ready > now:
			continue
		out.append(row)
	return sorted(out, key=_ready)


def pitr_window(backups: Iterable[Any], now: datetime, server: str | None = None,
                retention_days: int = RETENTION_DAYS, margin_minutes: int = MARGIN_MINUTES) -> dict[str, Any]:
	"""{available, start, end, reason, backups}: any time in [start, end] can be restored."""
	usable = usable_backups(backups, now, server, retention_days)
	end = (now - timedelta(minutes=margin_minutes)).replace(microsecond=0)
	if not usable:
		return {"available": False, "start": None, "end": end, "backups": 0,
		        "reason": "No backup with a binlog position yet. Point-in-time recovery starts with the next backup."}
	start = _ready(usable[0])
	start = start.replace(microsecond=0) + (timedelta(seconds=1) if start.microsecond else timedelta())
	if start >= end:
		return {"available": False, "start": start, "end": end, "backups": len(usable),
		        "reason": "The first restorable point is only a few minutes old. Try again shortly."}
	return {"available": True, "start": start, "end": end, "backups": len(usable), "reason": None}


def pick_base_backup(backups: Iterable[Any], target: datetime, now: datetime, server: str | None = None,
                     retention_days: int = RETENTION_DAYS) -> Any | None:
	"""The latest usable backup restorable at ``target`` (least binlog to replay)."""
	best = None
	for row in usable_backups(backups, now, server, retention_days):
		if _ready(row) <= target:
			best = row
	return best


def target_error(target: datetime, window: Mapping[str, Any]) -> tuple[str, list[str]] | None:
	"""(message, format args) when ``target`` is outside the window, else None."""
	if not window.get("available"):
		return (window.get("reason") or "Point-in-time recovery is not available for this site", [])
	if target < window["start"]:
		return ("The earliest time you can restore to is {0} (UTC)", [iso_z(window["start"])])
	if target > window["end"]:
		return ("The latest time you can restore to is {0} (UTC)", [iso_z(window["end"])])
	return None


def pitr_site_name(site_name: str, target: datetime, root_domain: str) -> str:
	"""Name of the inspection copy: ``<first label>-pitr-<YYYYmmddHHMM>.<root domain>``
	(the root domain has a wildcard DNS record and certificate resolver, a custom domain may not)."""
	first = (site_name or "").split(".", 1)[0].lower()
	suffix = "-pitr-" + target.strftime("%Y%m%d%H%M")
	label = (first[: 63 - len(suffix)].rstrip("-") or "site") + suffix
	name = f"{label}.{(root_domain or '').strip('.').lower()}"
	if not is_valid_site_name(name):
		raise ValueError("Cannot build a site name for the restored copy")
	return name


def backup_source(remote_path: str | None) -> dict[str, str]:
	"""Agent restore source of a Site Backup: ``remote_path`` (uploaded) or the local
	``backup_id`` (``/opt/nexaploy/backups/<bench>/<site>/<ts>`` without backup storage)."""
	if not remote_path:
		raise ValueError("This backup cannot be restored")
	if remote_path.startswith("/"):
		ts = remote_path.rstrip("/").rsplit("/", 1)[-1]
		if not BACKUP_ID_RE.match(ts):
			raise ValueError("This backup cannot be restored")
		return {"backup_id": ts}
	return {"remote_path": remote_path}


def restore_params(site_name: str, bench_name: str, remote_path: str, target: datetime,
                   target_site: str | None = None, in_place: bool = False) -> dict[str, Any]:
	"""``site.restore_pitr`` params (agent §11)."""
	params: dict[str, Any] = {
		"site": site_name,
		"bench": bench_name,
		"target_time": iso_z(target),
		**backup_source(remote_path),
		"in_place": bool(in_place),
		"with_files": True,
	}
	if in_place:
		params["confirm"] = site_name
	else:
		if not target_site or target_site == site_name:
			raise ValueError("A restored copy needs its own name")
		params["target_site"] = target_site
	return params


def archive_params(retention_days: int = RETENTION_DAYS) -> dict[str, Any]:
	return {"rotate": True, "keep_days": int(retention_days)}


def failed_step(steps: Iterable[Mapping[str, Any]]) -> str | None:
	for step in sorted(steps, key=lambda s: s.get("step_idx") or 0):
		if step.get("status") == "failure":
			return step.get("step_name")
	return None


def failure_status(in_place: bool, step_name: str | None, previous: str) -> str:
	"""Site status after a failed site.restore_pitr: an in-place restore that failed before
	touching the site keeps its status; anything else is Broken."""
	if in_place and step_name in IN_PLACE_HARMLESS_STEPS:
		return previous
	return "Broken"
