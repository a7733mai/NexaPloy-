"""Pure helpers for operations: registry tags, scheduled backups, remote retention,
monitoring/alerts and auto-update windows.

This module MUST NOT import frappe (tested by control-plane/tests/test_ops.py).
Datetimes are naive; callers pass "now" in the same clock as the stored values.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

# --------------------------------------------------------------------------- registry / tags

REGISTRY_URL_RE = re.compile(
	r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*$"
)
IMAGE_REPO = "nexaploy/bench"


def normalize_registry_url(url: str | None) -> str | None:
	"""'https://GHCR.io/Org/' -> 'ghcr.io/org'; '' / None -> None. Raises ValueError if invalid."""
	url = (url or "").strip()
	if not url:
		return None
	for scheme in ("https://", "http://"):
		if url.lower().startswith(scheme):
			url = url[len(scheme) :]
	url = url.strip("/").lower()
	if not REGISTRY_URL_RE.match(url):
		raise ValueError(f"Invalid registry URL: {url!r} (expected e.g. ghcr.io/org or registry.example.com)")
	return url


def build_image_tag(release_group: str, build_id: str, registry_url: str | None = None) -> str:
	"""`[<registry_url>/]nexaploy/bench:<release_group>-<build_id>` (§2)."""
	tag = f"{IMAGE_REPO}:{release_group}-{build_id}"
	reg = normalize_registry_url(registry_url)
	return f"{reg}/{tag}" if reg else tag


def registry_covers(registry_url: str | None, image: str | None) -> bool:
	"""True when `image` is hosted on the configured registry (needs a login to pull)."""
	try:
		reg = normalize_registry_url(registry_url)
	except ValueError:
		return False
	return bool(reg and image and image.startswith(reg + "/"))


# --------------------------------------------------------------------------- scheduled backups

BACKUP_FREQUENCIES = {
	"Daily": timedelta(days=1),
	"Every 12 hours": timedelta(hours=12),
	"Weekly": timedelta(days=7),
	"Off": None,
}
# The scheduler runs hourly; allow a little drift so a daily backup does not slip an hour a day.
BACKUP_SLACK = timedelta(minutes=10)


def backup_interval(frequency: str | None) -> timedelta | None:
	if not frequency:
		frequency = "Daily"
	if frequency not in BACKUP_FREQUENCIES:
		raise ValueError(f"Unknown backup frequency {frequency!r}")
	return BACKUP_FREQUENCIES[frequency]


def is_backup_due(frequency: str | None, last_backup_at: datetime | None, now: datetime,
                  slack: timedelta = BACKUP_SLACK) -> bool:
	"""A scheduled backup is due when the last successful one is older than the interval."""
	interval = backup_interval(frequency)
	if interval is None:
		return False
	if last_backup_at is None:
		return True
	return now - last_backup_at >= interval - slack


# --------------------------------------------------------------------------- remote retention

TS_FORMAT = "%Y%m%dT%H%M%SZ"
BACKUP_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")
KEEP_MIN_DEFAULT = 3


def parse_backup_ts(name: str | None) -> datetime | None:
	"""Dated backup dir name ('20261103T213000Z' or '20261103T213000Z/') -> naive UTC datetime."""
	name = (name or "").strip().rstrip("/")
	if not BACKUP_ID_RE.match(name):
		return None
	try:
		return datetime.strptime(name, TS_FORMAT)
	except ValueError:
		return None


def select_expired_backups(names: Iterable[str], keep_days: int, keep_min: int = KEEP_MIN_DEFAULT,
                           now: datetime | None = None) -> list[str]:
	"""Dated dir names to delete: older than keep_days, never the newest keep_min.

	Names that are not dated backup dirs are never selected. `now` is naive UTC.
	"""
	now = now or datetime.utcnow()
	dated = sorted(
		((parse_backup_ts(n), n.strip().rstrip("/")) for n in names if parse_backup_ts(n)), reverse=True
	)
	cutoff = now - timedelta(days=keep_days)
	return sorted(name for i, (dt, name) in enumerate(dated) if i >= max(keep_min, 0) and dt < cutoff)


def parse_backup_remote_path(path: str | None) -> dict[str, str] | None:
	"""'<remote>:<prefix>/<bench>/<site>/<ts>[/]' -> {remote, prefix, bench, site, ts}.

	`prefix` may contain slashes (or be empty). Returns None when the path does not look like
	a §5.5 backup location.
	"""
	if not path or ":" not in path:
		return None
	remote, _, rel = path.partition(":")
	parts = [p for p in rel.strip("/").split("/") if p]
	if len(parts) < 3 or not parse_backup_ts(parts[-1]):
		return None
	return {
		"remote": remote,
		"prefix": "/".join(parts[:-3]),
		"bench": parts[-3],
		"site": parts[-2],
		"ts": parts[-1],
	}


def prune_targets(remote_paths: Iterable[str | None], site: str) -> list[tuple[str, str]]:
	"""Distinct (prefix, bench) locations holding backups of `site` (for backup.prune_remote)."""
	out: set[tuple[str, str]] = set()
	for p in remote_paths:
		info = parse_backup_remote_path(p)
		if info and info["site"] == site:
			out.add((info["prefix"], info["bench"]))
	return sorted(out)


def pruned_remote_paths(result: Any) -> set[tuple[str, str, str]]:
	"""backup.prune_remote result -> {(prefix, bench, ts)} that were deleted."""
	out: set[tuple[str, str, str]] = set()
	if not isinstance(result, Mapping):
		return out
	for item in result.get("pruned") or []:
		if isinstance(item, Mapping):
			info = parse_backup_remote_path(item.get("remote_path"))
			if info:
				out.add((info["prefix"], info["bench"], info["ts"]))
	return out


def backup_was_pruned(remote_path: str | None, pruned: set[tuple[str, str, str]]) -> bool:
	info = parse_backup_remote_path(remote_path)
	return bool(info) and (info["prefix"], info["bench"], info["ts"]) in pruned


# --------------------------------------------------------------------------- backup configuration

BACKUP_SETTINGS_FIELDS = (
	"backup_remote_type", "backup_provider", "backup_endpoint", "backup_region", "backup_bucket",
	"backup_access_key", "backup_prefix",
)
BACKUP_REMOTE = "nexaploy-backups"


def backup_configured(settings: Mapping[str, Any]) -> bool:
	return bool(settings.get("backup_bucket") and settings.get("backup_access_key"))


def backup_job_params(settings: Mapping[str, Any], server_id: str, secret: str) -> dict[str, Any]:
	"""server.configure_backups params for one server.

	`secret` is the secret access key or a secret placeholder resolved at poll time.
	Each server writes under `<backup_prefix>/<server_id>` so paths never collide.
	"""
	base = (settings.get("backup_prefix") or "").strip().strip("/")
	return {
		"remote": BACKUP_REMOTE,
		"type": settings.get("backup_remote_type") or "s3",
		"provider": settings.get("backup_provider") or "Other",
		"endpoint": settings.get("backup_endpoint") or None,
		"region": settings.get("backup_region") or None,
		"bucket": settings.get("backup_bucket"),
		"access_key_id": settings.get("backup_access_key"),
		"secret_access_key": secret,
		"prefix": f"{base}/{server_id}" if base else server_id,
	}


def config_fingerprint(values: Mapping[str, Any]) -> str:
	"""Stable sha256 of a settings subset (detect changes, incl. secrets, without storing them)."""
	blob = json.dumps({k: values.get(k) for k in sorted(values)}, sort_keys=True, default=str)
	return hashlib.sha256(blob.encode()).hexdigest()


# --------------------------------------------------------------------------- monitoring

MB = 1024 * 1024
GB = 1024 * 1024 * 1024
METRIC_INTERVAL = timedelta(minutes=5)
DISK_ALERT_PERCENT = 85.0
ALERT_INTERVAL = timedelta(hours=6)
METRIC_RETENTION_DAYS = 30


def _num(value: Any, default: float = 0.0) -> float:
	try:
		return float(value)
	except (TypeError, ValueError):
		return default


def metric_from_facts(facts: Mapping[str, Any] | None) -> dict[str, Any]:
	"""Agent heartbeat facts -> Server Metric / Server field values.

	disk_* describe the Nexaploy root mount (/opt/nexaploy, falling back to /);
	disk_used_percent is the worst of all reported mounts.
	"""
	facts = facts if isinstance(facts, Mapping) else {}
	load = facts.get("load_avg") or [0]
	mem_total = _num(facts.get("mem_total"))
	mem_used = _num(facts.get("mem_used"), max(0.0, mem_total - _num(facts.get("mem_available"))))
	mounts = [m for m in facts.get("mounts") or [] if isinstance(m, Mapping) and _num(m.get("total")) > 0]
	if mounts:
		main = next((m for m in mounts if m.get("path") == facts.get("disk_path")), None) or mounts[-1]
		disk_total, disk_used = _num(main.get("total")), _num(main.get("used"))
		percents = [100.0 * _num(m.get("used")) / _num(m.get("total")) for m in mounts]
	else:
		disk_total = _num(facts.get("disk_total"))
		disk_used = _num(facts.get("disk_used"), disk_total - _num(facts.get("disk_free")))
		percents = [100.0 * disk_used / disk_total] if disk_total else [0.0]
	return {
		"cpu_count": int(_num(facts.get("cpu_count"))),
		"load_1": round(_num(load[0] if isinstance(load, (list, tuple)) and load else load), 2),
		"memory_used_mb": int(mem_used // MB),
		"memory_total_mb": int(mem_total // MB),
		"disk_used_gb": round(disk_used / GB, 2),
		"disk_total_gb": round(disk_total / GB, 2),
		"disk_used_percent": round(max(percents), 1),
		"unhealthy_containers": int(_num(facts.get("unhealthy_containers"))),
	}


def should_record_metric(last_recorded: datetime | None, now: datetime,
                         interval: timedelta = METRIC_INTERVAL) -> bool:
	return last_recorded is None or now - last_recorded >= interval


def server_alerts(server: Mapping[str, Any], disk_threshold: float = DISK_ALERT_PERCENT) -> list[tuple[str, str]]:
	"""(kind, message) for every alert condition currently true on a server row."""
	name = server.get("name") or server.get("server_id") or "?"
	host = server.get("hostname") or name
	out: list[tuple[str, str]] = []
	if server.get("status") == "Archived":
		return out
	if server.get("status") == "Offline":
		out.append(("offline", f"Server {name} ({host}) is offline: no heartbeat since {server.get('last_heartbeat')}"))
	disk = _num(server.get("disk_used_percent"))
	if disk > disk_threshold:
		out.append(("disk", f"Server {name} ({host}) disk usage is {disk:.1f}% (threshold {disk_threshold:.0f}%)"))
	unhealthy = int(_num(server.get("unhealthy_containers")))
	if unhealthy > 0 and server.get("status") != "Offline":
		out.append(("containers", f"Server {name} ({host}) has {unhealthy} unhealthy container(s)"))
	return out


def load_alert_state(raw: Any) -> dict[str, str]:
	if isinstance(raw, Mapping):
		return {str(k): str(v) for k, v in raw.items()}
	try:
		data = json.loads(raw) if raw else {}
	except (TypeError, ValueError):
		return {}
	return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def should_alert(state: Mapping[str, str], kind: str, now: datetime, interval: timedelta = ALERT_INTERVAL) -> bool:
	"""Rate limit: at most one alert per kind per `interval` (state = {kind: iso datetime})."""
	last = state.get(kind)
	if not last:
		return True
	try:
		last_dt = datetime.fromisoformat(str(last))
	except ValueError:
		return True
	return now - last_dt >= interval


def record_alert(state: Mapping[str, str], kind: str, now: datetime) -> dict[str, str]:
	new = dict(state)
	new[kind] = now.isoformat(timespec="seconds")
	return new


def uptime_transition(previous_is_up: Any, previously_checked: bool, is_up: bool) -> str | None:
	"""'down' / 'up' when the state changed since the last check, else None (first check: None)."""
	if not previously_checked:
		return None
	was_up = bool(int(previous_is_up or 0))
	if was_up and not is_up:
		return "down"
	if not was_up and is_up:
		return "up"
	return None


def ping_url(site: str) -> str:
	return f"https://{site}/api/method/ping"


# --------------------------------------------------------------------------- auto updates

WINDOW_RE = re.compile(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*-\s*([01]?\d|2[0-3]|24):([0-5]\d)\s*$")


def parse_update_window(window: str | None) -> tuple[int, int] | None:
	"""'02:00-05:00' -> (120, 300) minutes; '' / None -> None (= any time). Raises ValueError."""
	if not window or not window.strip():
		return None
	m = WINDOW_RE.match(window)
	if not m:
		raise ValueError(f"Invalid update window {window!r}; expected HH:MM-HH:MM, e.g. 02:00-05:00")
	start = int(m.group(1)) * 60 + int(m.group(2))
	end = int(m.group(3)) * 60 + int(m.group(4))
	if end > 24 * 60 or start == end:
		raise ValueError(f"Invalid update window {window!r}")
	return start, end


def in_update_window(window: str | None, local_now: datetime) -> bool:
	"""True when local_now (server-local clock) is inside the window; wraps past midnight."""
	parsed = parse_update_window(window)
	if parsed is None:
		return True
	start, end = parsed
	minute = local_now.hour * 60 + local_now.minute
	if start < end:
		return start <= minute < end
	return minute >= start or minute < end


def server_local_time(utc_now: datetime, facts: Mapping[str, Any] | None) -> datetime:
	"""Server-local wall clock from the agent's reported utc_offset_minutes (UTC if unknown)."""
	offset = 0
	if isinstance(facts, Mapping):
		offset = int(_num(facts.get("utc_offset_minutes")))
	return utc_now + timedelta(minutes=offset)


def benches_to_update(benches: Iterable[Mapping[str, Any]], latest_image: str | None) -> list[str]:
	"""Active benches whose image differs from the latest successful build."""
	if not latest_image:
		return []
	return [
		b["name"]
		for b in benches
		if b.get("status") == "Active" and b.get("image_tag") != latest_image and not b.get("update_in_progress")
	]
