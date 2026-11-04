"""Pure helpers for operations: registry tags, scheduled backups, remote retention,
monitoring/alerts and auto-update windows.

This module MUST NOT import frappe (tested by control-plane/tests/test_ops.py).
Datetimes are naive; callers pass "now" in the same clock as the stored values.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
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


# --------------------------------------------------------------------------- plan limits & usage
# Nexaploy Plan limit fields (0 / empty = unlimited) and the usage value each one caps.
USAGE_LIMITS = (
	("cpu", "cpu_seconds_per_day", "cpu_seconds"),
	("database", "max_database_mb", "database_mb"),
	("storage", "max_storage_mb", "storage_mb"),
)
USAGE_WARN_PERCENT = 80.0
OVERAGE_ACTIONS = ("Notify", "Throttle", "Suspend")
USAGE_ALERT_INTERVAL = timedelta(hours=24)
# Frappe's built-in rate limiter (site_config `rate_limit`) counts request seconds per window.
THROTTLE_WINDOW = 86400
THROTTLE_MIN_LIMIT = 60
THROTTLE_FRACTION = 0.1


def usage_status(plan: Mapping[str, Any] | None, usage: Mapping[str, Any] | None) -> dict[str, Any]:
	"""Pure decision: compare a site's usage with its plan limits.

	`plan` carries cpu_seconds_per_day / max_database_mb / max_storage_mb (0 = unlimited),
	`usage` carries cpu_seconds (today, UTC) / database_mb / storage_mb.
	Returns {"status": ok|warn|over, "metrics": {cpu|database|storage: {used, limit, percent,
	status}}, "over": [...], "warn": [...]}; warn from USAGE_WARN_PERCENT (80 %), over above 100 %.
	"""
	plan = plan if isinstance(plan, Mapping) else {}
	usage = usage if isinstance(usage, Mapping) else {}
	metrics: dict[str, Any] = {}
	over: list[str] = []
	warn: list[str] = []
	for name, limit_field, usage_field in USAGE_LIMITS:
		limit = _num(plan.get(limit_field))
		used = max(0.0, _num(usage.get(usage_field)))
		if limit <= 0:
			metrics[name] = {"used": used, "limit": 0, "percent": None, "status": "ok"}
			continue
		percent = round(100.0 * used / limit, 1)
		status = "over" if used > limit else "warn" if percent >= USAGE_WARN_PERCENT else "ok"
		metrics[name] = {"used": used, "limit": limit, "percent": percent, "status": status}
		if status == "over":
			over.append(name)
		elif status == "warn":
			warn.append(name)
	overall = "over" if over else "warn" if warn else "ok"
	return {"status": overall, "metrics": metrics, "over": over, "warn": warn}


def normalize_overage_action(action: str | None) -> str:
	return action if action in OVERAGE_ACTIONS else "Notify"


def throttle_rate_limit(plan: Mapping[str, Any] | None) -> dict[str, int]:
	"""site_config `rate_limit` applied while a Throttle plan is over its CPU budget."""
	cpu = _num((plan or {}).get("cpu_seconds_per_day"))
	return {"limit": max(THROTTLE_MIN_LIMIT, int(cpu * THROTTLE_FRACTION)), "window": THROTTLE_WINDOW}


def enforcement_actions(plan: Mapping[str, Any] | None, status: Mapping[str, Any], site: Mapping[str, Any]) -> list[str]:
	"""Pure: what enforce_plan_limits should do for one site.

	`site` carries `throttled`, `usage_suspended`, `suspended_for_billing` and `status`.
	Actions: "notify_warn", "notify_over", "throttle", "unthrottle", "suspend", "unsuspend".
	- Throttle applies to CPU only (storage/database over -> notify).
	- Suspend applies to any metric over; lifted when nothing is over (e.g. next UTC day for CPU
	  or after an upgrade). Billing suspensions are never touched.
	Notifications are returned unconditionally; the caller rate-limits them.
	"""
	action = normalize_overage_action((plan or {}).get("overage_action"))
	over = list(status.get("over") or [])
	actions: list[str] = []
	if status.get("status") == "over":
		actions.append("notify_over")
	elif status.get("status") == "warn":
		actions.append("notify_warn")
	throttled = bool(int(site.get("throttled") or 0))
	suspended = bool(int(site.get("usage_suspended") or 0))
	billing = bool(int(site.get("suspended_for_billing") or 0))
	cpu_over = "cpu" in over
	if action == "Throttle" and cpu_over and not throttled:
		actions.append("throttle")
	if throttled and not (action == "Throttle" and cpu_over):
		actions.append("unthrottle")
	if action == "Suspend" and over and not suspended and not billing and site.get("status") == "Active":
		actions.append("suspend")
	if suspended and not (action == "Suspend" and over):
		actions.append("unsuspend")
	return actions


def usage_rows(usage: Any, max_days: int = 7) -> list[dict[str, Any]]:
	"""Heartbeat / site.usage `usage` block -> rows to upsert into Site Usage.

	Input: {date, sites: {site: {database_mb?, storage_mb?, days: {YYYY-MM-DD: {cpu_seconds,
	requests}}}}}. Output: [{site, date, cpu_seconds, requests[, database_mb, storage_mb]}];
	point-in-time sizes go on the block's `date` (the collection day) only. Invalid entries are
	dropped; at most `max_days` days per site.
	"""
	if isinstance(usage, str):
		try:
			usage = json.loads(usage)
		except ValueError:
			return []
	if not isinstance(usage, Mapping) or not isinstance(usage.get("sites"), Mapping):
		return []
	block_date = _parse_day(usage.get("date"))
	rows: list[dict[str, Any]] = []
	for site, info in usage["sites"].items():
		if not isinstance(site, str) or not isinstance(info, Mapping):
			continue
		days = info.get("days") if isinstance(info.get("days"), Mapping) else {}
		parsed = sorted(((d, v) for d, v in ((_parse_day(k), v) for k, v in days.items()) if d), reverse=True)
		seen_block = False
		for day, values in parsed[:max_days]:
			values = values if isinstance(values, Mapping) else {}
			row = {
				"site": site,
				"date": day,
				"cpu_seconds": round(max(0.0, _num(values.get("cpu_seconds"))), 3),
				"requests": int(max(0.0, _num(values.get("requests")))),
			}
			if day == block_date:
				seen_block = True
				_add_sizes(row, info)
			rows.append(row)
		if block_date and not seen_block and (info.get("database_mb") is not None or info.get("storage_mb") is not None):
			row = {"site": site, "date": block_date}
			_add_sizes(row, info)
			rows.append(row)
	return rows


def _add_sizes(row: dict[str, Any], info: Mapping[str, Any]) -> None:
	for key in ("database_mb", "storage_mb"):
		if info.get(key) is not None:
			row[key] = round(max(0.0, _num(info.get(key))), 2)


def _parse_day(value: Any) -> str | None:
	try:
		return datetime.strptime(str(value), "%Y-%m-%d").date().isoformat()
	except (TypeError, ValueError):
		return None


def upgrade_plans(plans: Iterable[Mapping[str, Any]], current: str | None) -> list[Mapping[str, Any]]:
	"""Enabled Site plans ordered by plan_order (then price); those above the current one first."""
	ordered = sorted(plans, key=lambda p: (_num(p.get("plan_order")), _num(p.get("price_monthly")), str(p.get("name"))))
	cur = next((p for p in ordered if p.get("name") == current), None)
	if cur is None:
		return ordered
	key = (_num(cur.get("plan_order")), _num(cur.get("price_monthly")))
	return [p for p in ordered if (_num(p.get("plan_order")), _num(p.get("price_monthly"))) > key]


# --------------------------------------------------------------------------- backup download (S3 SigV4)

def _uri_encode(value: str, safe: str = "") -> str:
	from urllib.parse import quote

	return quote(value, safe="-_.~" + safe)


def presign_s3_get(endpoint: str | None, region: str | None, bucket: str, key: str, access_key: str,
                   secret_key: str, expires: int = 900, now: datetime | None = None,
                   path_style: bool | None = None) -> str:
	"""AWS Signature V4 query-string presigned GET URL (UNSIGNED-PAYLOAD, SignedHeaders=host).

	- no endpoint: AWS virtual-hosted style `https://<bucket>.s3[.<region>].amazonaws.com/<key>`
	  (us-east-1 uses the global `s3.amazonaws.com` host).
	- an endpoint (MinIO, Wasabi, R2, ...): path style `<endpoint>/<bucket>/<key>` unless
	  `path_style=False`. `http://` is kept for local MinIO; default scheme is https.
	`now` is a UTC datetime (naive = UTC). `expires` is clamped to 1..604800 seconds.
	"""
	import hmac as _hmac
	from urllib.parse import urlsplit

	if not bucket or not key or not access_key or not secret_key:
		raise ValueError("bucket, key and credentials are required")
	region = (region or "").strip() or "us-east-1"
	expires = max(1, min(int(expires), 604800))
	now = now or datetime.utcnow()
	if now.tzinfo is not None:
		now = now.astimezone(timezone.utc).replace(tzinfo=None)
	amz_date = now.strftime("%Y%m%dT%H%M%SZ")
	datestamp = now.strftime("%Y%m%d")
	key_path = "/".join(_uri_encode(part) for part in key.lstrip("/").split("/"))

	if endpoint and endpoint.strip():
		raw = endpoint.strip()
		if "://" not in raw:
			raw = "https://" + raw
		parts = urlsplit(raw)
		scheme, host = parts.scheme or "https", parts.netloc
		base = parts.path.rstrip("/")
		if path_style is None:
			path_style = not host.endswith("amazonaws.com")
		if path_style:
			uri = f"{base}/{_uri_encode(bucket)}/{key_path}"
		else:
			host = f"{bucket}.{host}"
			uri = f"{base}/{key_path}"
	else:
		scheme = "https"
		host = f"{bucket}.s3.amazonaws.com" if region == "us-east-1" else f"{bucket}.s3.{region}.amazonaws.com"
		uri = f"/{key_path}"
		if path_style:
			host = "s3.amazonaws.com" if region == "us-east-1" else f"s3.{region}.amazonaws.com"
			uri = f"/{_uri_encode(bucket)}/{key_path}"

	scope = f"{datestamp}/{region}/s3/aws4_request"
	query = {
		"X-Amz-Algorithm": "AWS4-HMAC-SHA256",
		"X-Amz-Credential": f"{access_key}/{scope}",
		"X-Amz-Date": amz_date,
		"X-Amz-Expires": str(expires),
		"X-Amz-SignedHeaders": "host",
	}
	canonical_query = "&".join(f"{_uri_encode(k)}={_uri_encode(v)}" for k, v in sorted(query.items()))
	canonical_request = "\n".join(["GET", uri, canonical_query, f"host:{host}", "", "host", "UNSIGNED-PAYLOAD"])
	string_to_sign = "\n".join([
		"AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest(),
	])

	def _sign(k: bytes, msg: str) -> bytes:
		return _hmac.new(k, msg.encode(), hashlib.sha256).digest()

	k_signing = _sign(_sign(_sign(_sign(("AWS4" + secret_key).encode(), datestamp), region), "s3"), "aws4_request")
	signature = _hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
	return f"{scheme}://{host}{uri}?{canonical_query}&X-Amz-Signature={signature}"


BACKUP_FILE_TYPES = ("database", "public", "private", "site_config")
_AGENT_FILE_TYPES = {"database": "database", "public": "public", "private": "private",
                     "config": "site_config", "site_config": "site_config"}
_BACKUP_SUFFIXES = (
	("-database.sql.gz", "database"), ("-database.sql", "database"),
	("-private-files.tar", "private"), ("-private-files.tgz", "private"),
	("-files.tar", "public"), ("-files.tgz", "public"),
	("-site_config_backup.json", "site_config"),
)


def classify_backup_file(name: str | None) -> str | None:
	for suffix, kind in _BACKUP_SUFFIXES:
		if (name or "").endswith(suffix):
			return kind
	return None


def backup_files_from_result(result: Any) -> list[dict[str, Any]]:
	"""site.backup result -> Site Backup File rows {file_type, file_name, remote_key, size, sha256}.

	Prefers the agent's `backup_files`; falls back to `manifest.files` + `remote_path`
	(`<remote>:<key prefix>`; the remote is an alias of the bucket root, so the object key is
	`<path after ':'>/<name>`). Files without a remote key are kept (local-only backup).
	"""
	if not isinstance(result, Mapping):
		return []
	manifest = result.get("manifest") if isinstance(result.get("manifest"), Mapping) else {}
	remote_path = result.get("remote_path") or manifest.get("remote_path")
	rel = str(remote_path).partition(":")[2].strip("/") if remote_path and ":" in str(remote_path) else None
	source = result.get("backup_files")
	if not isinstance(source, list):
		source = manifest.get("files") if isinstance(manifest.get("files"), list) else []
	rows = []
	seen = set()
	for f in source:
		if not isinstance(f, Mapping):
			continue
		name = f.get("name") or f.get("file_name")
		kind = _AGENT_FILE_TYPES.get(f.get("file_type") or f.get("type") or "") or classify_backup_file(name)
		if kind not in BACKUP_FILE_TYPES or kind in seen or not name or "/" in str(name):
			continue
		seen.add(kind)
		key = f.get("remote_key") or (f"{rel}/{name}" if rel else None)
		size = f.get("size")
		rows.append({
			"file_type": kind,
			"file_name": str(name),
			"remote_key": key,
			"size": int(size) if isinstance(size, (int, float)) and not isinstance(size, bool) else None,
			"sha256": f.get("sha256"),
		})
	return rows


# --------------------------------------------------------------------------- site config editor

CONFIG_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")
CONFIG_MASK = "********"
# Never editable from the portal: database credentials / identity, encryption, routing, and keys
# managed by Nexaploy itself (domains, plan throttling, usage monitor).
CONFIG_DENY = frozenset({
	"db_name", "db_password", "db_type", "db_host", "db_port", "db_user", "db_socket",
	"encryption_key", "host_name", "admin_password", "root_password", "root_login",
	"domains", "rate_limit", "monitor", "limits", "installed_apps",
	"redis_cache", "redis_queue", "redis_socketio", "socketio_port", "webserver_port",
	"file_watcher_port", "use_redis_auth", "restart_supervisor_on_update", "restart_systemd_on_update",
})
CONFIG_DENY_PREFIXES = ("nexaploy_", "db_")
# Allowed but risky: the change goes through with a warning.
CONFIG_WARN = {
	"developer_mode": "developer_mode exposes debugging tools and slows the site; do not use it in production",
	"maintenance_mode": "maintenance_mode makes the site unavailable to users",
	"pause_scheduler": "pause_scheduler stops background jobs and scheduled tasks",
	"disable_scheduler": "disable_scheduler stops background jobs and scheduled tasks",
	"allow_tests": "allow_tests lets tests run against this site's data",
}
CONFIG_SECRET_RE = re.compile(r"password|secret|token|key", re.IGNORECASE)
CONFIG_REDACT = frozenset({"db_password", "encryption_key", "admin_password", "root_password"})
MAX_CONFIG_CHANGES = 50
MAX_CONFIG_VALUE_BYTES = 16 * 1024


def config_key_editable(key: str) -> bool:
	return bool(CONFIG_KEY_RE.match(key or "")) and key not in CONFIG_DENY and not key.startswith(CONFIG_DENY_PREFIXES)


def config_key_secret(key: str) -> bool:
	return key in CONFIG_REDACT or bool(CONFIG_SECRET_RE.search(key or ""))


def redact_site_config(config: Any) -> dict[str, Any]:
	"""Portal view of a site_config: values of secret keys masked, protected keys read-only.

	Returns {"config": {...}, "redacted": [keys], "readonly": [keys]} (keys sorted).
	"""
	if not isinstance(config, Mapping):
		config = {}
	out: dict[str, Any] = {}
	redacted, readonly = [], []
	for key in sorted(config):
		value = config[key]
		if config_key_secret(str(key)):
			out[key] = CONFIG_MASK if value not in (None, "") else value
			redacted.append(key)
		else:
			out[key] = value
		if not config_key_editable(str(key)):
			readonly.append(key)
	return {"config": out, "redacted": redacted, "readonly": readonly}


def validate_config_changes(changes: Any, remove: Any = None) -> tuple[dict[str, Any], list[str], list[str]]:
	"""Validate a portal config edit -> (set, remove, warnings). Raises ValueError.

	- keys: CONFIG_KEY_RE, not denied (CONFIG_DENY / prefixes db_, nexaploy_);
	- values: JSON types (str, int, float, bool, list, dict; not null - use `remove`), at most
	  16 KB serialised, never the redaction mask (the editor echoes it for secrets);
	- at most 50 keys; a key cannot be both set and removed.
	"""
	if isinstance(changes, str):
		changes = json.loads(changes) if changes.strip() else {}
	if isinstance(remove, str):
		remove = json.loads(remove) if remove.strip() else []
	changes = changes or {}
	remove = remove or []
	if not isinstance(changes, Mapping):
		raise ValueError("changes must be an object of {key: value}")
	if not isinstance(remove, (list, tuple)):
		raise ValueError("remove must be a list of keys")
	if len(changes) + len(remove) > MAX_CONFIG_CHANGES:
		raise ValueError(f"at most {MAX_CONFIG_CHANGES} keys per change")
	clean: dict[str, Any] = {}
	warnings: list[str] = []
	for key, value in changes.items():
		_check_config_key(key)
		if value is None:
			raise ValueError(f"{key}: use remove to delete a key")
		if not _json_value(value):
			raise ValueError(f"{key}: value must be a JSON string, number, boolean, list or object")
		if value == CONFIG_MASK:
			raise ValueError(f"{key}: enter the new value (the masked value cannot be saved)")
		if len(json.dumps(value)) > MAX_CONFIG_VALUE_BYTES:
			raise ValueError(f"{key}: value is too large")
		clean[key] = value
		if key in CONFIG_WARN and value not in (0, False, "0", ""):
			warnings.append(CONFIG_WARN[key])
	removed: list[str] = []
	for key in remove:
		_check_config_key(key)
		if key in clean:
			raise ValueError(f"{key} is both set and removed")
		if key not in removed:
			removed.append(key)
	return clean, removed, warnings


def _check_config_key(key: Any) -> None:
	if not isinstance(key, str) or not CONFIG_KEY_RE.match(key):
		raise ValueError(f"invalid config key {key!r}")
	if not config_key_editable(key):
		raise ValueError(f"{key} cannot be changed from the dashboard")


def _json_value(value: Any, depth: int = 0) -> bool:
	if depth > 8:
		return False
	if isinstance(value, bool) or isinstance(value, (str, int)):
		return True
	if isinstance(value, float):
		return value == value and value not in (float("inf"), float("-inf"))
	if isinstance(value, list):
		return all(_json_value(v, depth + 1) for v in value)
	if isinstance(value, dict):
		return all(isinstance(k, str) and _json_value(v, depth + 1) for k, v in value.items())
	return False
