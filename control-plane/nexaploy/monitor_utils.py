"""External uptime monitoring and public status pages: pure logic.

This module MUST NOT import frappe (unit-tested with plain pytest in
control-plane/tests/test_monitoring.py). nexaploy/monitoring.py does the database work,
nexaploy/probe.py performs the HTTP checks (also usable as the standalone `nexaploy-probe`),
nexaploy/api/monitoring.py is the portal API and nexaploy/api/monitoring_probe.py the API of
external probe workers. ARCHITECTURE.md §16.

Vocabulary
    monitor     one URL on a host of one of the team's sites, checked every `interval` minutes
    probe       a place checks run from: the control plane itself (built-in, the default) or a
                `nexaploy-probe` worker on another VPS / region
    probe state per monitor: {probe: {fails, ok, at, code, ms, error}} - consecutive failures
    decision    "down" when >= quorum fresh probes saw `threshold` consecutive failures, "up"
                when >= quorum fresh probes saw a success, else None (keep the current status)
    incident    opened on Up/Pending -> Down, resolved on Down -> Up
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

# --------------------------------------------------------------------------- constants

METHODS = ("GET", "HEAD")
INTERVALS = (1, 5, 15)  # minutes
DEFAULT_INTERVAL = 5
DEFAULT_TIMEOUT = 10
MIN_TIMEOUT, MAX_TIMEOUT = 2, 30
DEFAULT_THRESHOLD = 2  # consecutive failures per probe before it counts as failing
MAX_THRESHOLD = 10
DEFAULT_EXPECTED = "200-399"
DEFAULT_SITE_PATH = "/api/method/ping"  # the default monitor of every Active site
KEYWORD_MAX = 200
TITLE_MAX = 80
PATH_MAX = 500
BODY_LIMIT = 1024 * 1024  # bytes read for the keyword match

# plan feature "advanced monitoring" (Team.advanced_monitoring or the site's Nexaploy Plan)
BASIC_MIN_INTERVAL = 5
ADVANCED_MIN_INTERVAL = 1
BASIC_MAX_PER_SITE = 3
ADVANCED_MAX_PER_SITE = 20

# storage
RAW_RETENTION_HOURS = 48
ROLLUP_RETENTION_DAYS = 90
INCIDENT_RETENTION_DAYS = 365
STATUS_DAYS = 90
STATUS_CACHE_SECONDS = 60

# statuses
UP, DOWN, PENDING, PAUSED = "Up", "Down", "Pending", "Paused"
MONITOR_STATUSES = (PENDING, UP, DOWN, PAUSED)
OPEN, RESOLVED = "Open", "Resolved"
INCIDENT_STATUSES = (OPEN, RESOLVED)
UPDATE_STATUSES = ("Investigating", "Identified", "Monitoring", "Resolved")

BUILTIN_PROBE = "control-plane"
PROBE_TOKEN_HEADER = "X-Nexaploy-Probe-Token"

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,48}[a-z0-9])$")
RESERVED_SLUGS = frozenset({
	"admin", "api", "app", "assets", "dashboard", "files", "login", "logout", "private", "status",
	"signup", "www", "nexaploy", "support", "help", "docs", "static", "new", "edit",
})
_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
HOST_RE = re.compile(rf"^(?=.{{1,253}}$)(?:{_LABEL}\.)+{_LABEL}$")
PROBE_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")


class MonitorError(ValueError):
	"""Invalid monitor / status page input (message is user-facing and translatable)."""


# --------------------------------------------------------------------------- small helpers


def _int(value: Any, default: int) -> int:
	try:
		return int(value)
	except (TypeError, ValueError):
		return default


def utc(value: Any) -> datetime | None:
	"""datetime / ISO string -> naive UTC datetime (Frappe stores naive datetimes)."""
	if value in (None, ""):
		return None
	if isinstance(value, datetime):
		dt = value
	elif isinstance(value, date):
		dt = datetime(value.year, value.month, value.day)
	else:
		text = str(value).strip().replace("Z", "+00:00")
		try:
			dt = datetime.fromisoformat(text)
		except ValueError:
			return None
	if dt.tzinfo is not None:
		dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
	return dt


def iso(value: Any) -> str | None:
	dt = utc(value)
	return dt.isoformat(timespec="seconds") + "Z" if dt else None


def display_time(value: Any) -> str:
	"""'2026-11-04 18:05 UTC' ('' when empty)."""
	dt = utc(value)
	return f"{dt:%Y-%m-%d %H:%M} UTC" if dt else ""


def truncate(text: Any, limit: int) -> str:
	text = "" if text is None else str(text)
	return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


# --------------------------------------------------------------------------- slugs / hosts


def validate_slug(value: Any) -> str:
	"""Status page slug: 3-50 chars, lowercase letters, digits and inner hyphens."""
	slug = str(value or "").strip().lower()
	if not SLUG_RE.match(slug) or "--" in slug:
		raise MonitorError("The slug must be 3-50 lowercase letters, digits or hyphens")
	if slug in RESERVED_SLUGS:
		raise MonitorError("This slug is reserved")
	return slug


def suggest_slug(title: Any) -> str:
	slug = re.sub(r"[^a-z0-9]+", "-", str(title or "").lower()).strip("-")
	slug = re.sub(r"-{2,}", "-", slug)[:50].strip("-")
	if len(slug) < 3:
		slug = (slug + "-status").strip("-")
	if slug in RESERVED_SLUGS:
		slug = slug + "-status"
	return slug


def normalize_host(value: Any) -> str:
	text = str(value or "").strip().lower()
	if "://" in text:
		text = text.split("://", 1)[1]
	text = text.split("/", 1)[0].rsplit(":", 1)[0] if not text.startswith("[") else text
	return text.rstrip(".")


def validate_domain(value: Any) -> str:
	"""Optional custom status domain (status.example.com); '' when empty."""
	host = normalize_host(value)
	if not host:
		return ""
	if not HOST_RE.match(host):
		raise MonitorError("Enter a valid domain name, e.g. status.example.com")
	return host


def validate_probe_name(value: Any) -> str:
	name = str(value or "").strip().lower()
	if not PROBE_NAME_RE.match(name):
		raise MonitorError("Probe names are 1-40 lowercase letters, digits or hyphens")
	return name


# --------------------------------------------------------------------------- monitors


def parse_expected(spec: Any) -> list[tuple[int, int]]:
	"""'200' / '200-299' / '2xx' / '200,301-302' -> [(lo, hi), ...]; MonitorError if invalid."""
	text = str(spec or "").strip().lower() or DEFAULT_EXPECTED
	ranges: list[tuple[int, int]] = []
	for part in re.split(r"[\s,]+", text):
		if not part:
			continue
		m = re.fullmatch(r"([1-5])xx", part)
		if m:
			base = int(m.group(1)) * 100
			ranges.append((base, base + 99))
			continue
		m = re.fullmatch(r"(\d{3})(?:-(\d{3}))?", part)
		if not m:
			raise MonitorError("Expected status: a code (200), a range (200-299) or a class (2xx)")
		lo = int(m.group(1))
		hi = int(m.group(2) or lo)
		if not (100 <= lo <= hi <= 599):
			raise MonitorError("Expected status: a code (200), a range (200-299) or a class (2xx)")
		ranges.append((lo, hi))
	if not ranges or len(ranges) > 10:
		raise MonitorError("Expected status: a code (200), a range (200-299) or a class (2xx)")
	return ranges


def status_matches(code: Any, spec: Any) -> bool:
	try:
		code = int(code)
	except (TypeError, ValueError):
		return False
	try:
		ranges = parse_expected(spec)
	except MonitorError:
		ranges = parse_expected(DEFAULT_EXPECTED)
	return any(lo <= code <= hi for lo, hi in ranges)


def normalize_path(value: Any) -> str:
	path = str(value or "").strip() or "/"
	if not path.startswith("/") or any(c in path for c in " \t\r\n\"'<>\\") or len(path) > PATH_MAX:
		raise MonitorError("The path must start with / and contain no spaces or quotes")
	if "#" in path:
		path = path.split("#", 1)[0] or "/"
	return path


def monitor_url(host: str, path: str) -> str:
	return f"https://{host}{path}"


def limits(advanced: bool) -> dict[str, int]:
	"""Plan limits of the monitoring feature (min interval in minutes, monitors per site)."""
	if advanced:
		return {"min_interval": ADVANCED_MIN_INTERVAL, "max_per_site": ADVANCED_MAX_PER_SITE}
	return {"min_interval": BASIC_MIN_INTERVAL, "max_per_site": BASIC_MAX_PER_SITE}


def check_plan_limits(interval: int, site_monitor_count: int, advanced: bool) -> None:
	"""`site_monitor_count`: monitors of the site including the one being saved."""
	lim = limits(advanced)
	if interval < lim["min_interval"]:
		raise MonitorError("Checks more often than every 5 minutes need the advanced monitoring plan feature")
	if site_monitor_count > lim["max_per_site"]:
		if advanced:
			raise MonitorError("Too many monitors for this site")
		raise MonitorError("More than 3 monitors per site need the advanced monitoring plan feature")


def validate_monitor(
	data: Mapping[str, Any],
	site_hosts: Iterable[str],
	probes: Iterable[str] = (),
) -> dict[str, Any]:
	"""Validate portal input. `site_hosts`: the primary and Active custom domains of the
	monitor's site (the only hosts a monitor may target: no arbitrary URLs, no SSRF).
	`probes`: names of enabled probes. Returns the clean field dict."""
	hosts = [normalize_host(h) for h in site_hosts if h]
	title = re.sub(r"\s+", " ", str(data.get("title") or "")).strip()
	if not title:
		raise MonitorError("Name is required")
	if len(title) > TITLE_MAX:
		raise MonitorError("Name is too long")
	host = normalize_host(data.get("hostname") or (hosts[0] if hosts else ""))
	if host not in hosts:
		raise MonitorError("Monitors can only check the site's own address or its active custom domains")
	path = normalize_path(data.get("path"))
	method = str(data.get("method") or "GET").upper()
	if method not in METHODS:
		raise MonitorError("Method must be GET or HEAD")
	expected = str(data.get("expected_status") or DEFAULT_EXPECTED).strip().lower()
	parse_expected(expected)
	keyword = str(data.get("keyword") or "").strip()
	if len(keyword) > KEYWORD_MAX:
		raise MonitorError("The keyword is too long")
	if keyword and method == "HEAD":
		raise MonitorError("A keyword check needs the GET method")
	interval = _int(data.get("interval_minutes"), DEFAULT_INTERVAL)
	if interval not in INTERVALS:
		raise MonitorError("Interval must be 1, 5 or 15 minutes")
	timeout = _int(data.get("timeout_seconds"), DEFAULT_TIMEOUT)
	if not MIN_TIMEOUT <= timeout <= MAX_TIMEOUT:
		raise MonitorError("Timeout must be between 2 and 30 seconds")
	threshold = _int(data.get("failure_threshold"), DEFAULT_THRESHOLD)
	if not 1 <= threshold <= MAX_THRESHOLD:
		raise MonitorError("Failures before alerting must be between 1 and 10")
	known = set(probes)
	chosen = parse_probe_list(data.get("probes"))
	unknown = [p for p in chosen if p not in known]
	if unknown:
		raise MonitorError("Unknown probe")
	quorum = _int(data.get("quorum"), 0)
	if quorum < 0 or (chosen and quorum > len(chosen)):
		raise MonitorError("Quorum cannot exceed the number of probes")
	return {
		"title": title,
		"hostname": host,
		"path": path,
		"url": monitor_url(host, path),
		"method": method,
		"expected_status": expected,
		"keyword": keyword,
		"interval_minutes": interval,
		"timeout_seconds": timeout,
		"failure_threshold": threshold,
		"probes": ",".join(chosen),
		"quorum": quorum,
		"enabled": 1 if _truthy(data.get("enabled", True)) else 0,
	}


def _truthy(value: Any) -> bool:
	if isinstance(value, str):
		return value.strip().lower() not in ("", "0", "false", "no", "off")
	return bool(value)


def parse_probe_list(value: Any) -> list[str]:
	if not value:
		return []
	items = value if isinstance(value, (list, tuple)) else re.split(r"[\s,]+", str(value))
	out: list[str] = []
	for item in items:
		item = str(item or "").strip().lower()
		if item and item not in out:
			out.append(item)
	return out


def default_monitor(site: str) -> dict[str, Any]:
	"""The monitor every Active site gets (replaces the old 5-minute ping check)."""
	return {
		"title": site,
		"hostname": site,
		"path": DEFAULT_SITE_PATH,
		"url": monitor_url(site, DEFAULT_SITE_PATH),
		"method": "GET",
		"expected_status": "200",
		"keyword": "",
		"interval_minutes": DEFAULT_INTERVAL,
		"timeout_seconds": DEFAULT_TIMEOUT,
		"failure_threshold": DEFAULT_THRESHOLD,
		"probes": "",
		"quorum": 0,
		"enabled": 1,
		"is_default": 1,
	}


def assigned_probes(monitor_probes: Any, enabled_probes: Iterable[str]) -> list[str]:
	"""Probes that check a monitor: its own list (if any) intersected with the enabled probes;
	an empty list means every enabled probe."""
	enabled = list(enabled_probes)
	chosen = parse_probe_list(monitor_probes)
	if not chosen:
		return enabled
	return [p for p in chosen if p in enabled]


def is_due(last_checked_at: Any, interval_minutes: Any, now: datetime, slack_seconds: int = 15) -> bool:
	"""A check is due when `interval` minutes (minus scheduler slack) have elapsed."""
	last = utc(last_checked_at)
	if last is None:
		return True
	interval = max(1, _int(interval_minutes, DEFAULT_INTERVAL))
	return (utc(now) - last).total_seconds() >= interval * 60 - slack_seconds


def probe_location(probe: Mapping[str, Any], site_server: str | None, cp_host_server: str | None) -> str:
	"""'same_host' when the built-in probe checks a site of the server that also runs the
	control plane (all-in-one install: not an outside view), else 'external'."""
	if probe.get("is_builtin") and cp_host_server and site_server == cp_host_server:
		return "same_host"
	return "external"


# --------------------------------------------------------------------------- check results


def evaluate_response(
	status_code: Any,
	body: bytes | str | None,
	expected: Any,
	keyword: Any = "",
	error: Any = None,
) -> tuple[bool, str]:
	"""(ok, error message) for one HTTP response (or transport error)."""
	if error:
		return False, truncate(error, 300)
	if status_code is None:
		return False, "No response"
	if not status_matches(status_code, expected):
		return False, f"Unexpected status {status_code}"
	keyword = str(keyword or "")
	if keyword:
		text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body or "")
		if keyword not in text:
			return False, "Keyword not found"
	return True, ""


def clean_result(raw: Mapping[str, Any], now: datetime) -> dict[str, Any] | None:
	"""Validate one result posted by a probe; None when unusable. Timestamps more than an hour
	old or in the future (clock skew > 2 min) are replaced with `now`."""
	monitor = str(raw.get("monitor") or "").strip()
	if not monitor or len(monitor) > 140:
		return None
	at = utc(raw.get("checked_at"))
	now = utc(now)
	if at is None or at > now + timedelta(minutes=2) or at < now - timedelta(hours=1):
		at = now
	code = raw.get("status_code")
	code = _int(code, 0) if code not in (None, "") else None
	if code is not None and not 100 <= code <= 599:
		code = None
	ms = raw.get("response_ms")
	ms = max(0, min(_int(ms, 0), 600_000)) if ms not in (None, "") else None
	tls = utc(raw.get("tls_expires_on"))
	return {
		"monitor": monitor,
		"checked_at": at,
		"ok": 1 if _truthy(raw.get("ok")) else 0,
		"status_code": code,
		"response_ms": ms,
		"error": truncate(raw.get("error") or "", 300),
		"tls_expires_on": tls,
	}


def tls_days_left(expires_on: Any, now: datetime) -> int | None:
	exp = utc(expires_on)
	if exp is None:
		return None
	return math.floor((exp - utc(now)).total_seconds() / 86400)


# --------------------------------------------------------------------------- state machine


def apply_result(state: Mapping[str, Any] | None, probe: str, result: Mapping[str, Any]) -> dict[str, Any]:
	"""New probe state after one result of `probe` (consecutive failure counting)."""
	state = {k: dict(v) for k, v in (state or {}).items() if isinstance(v, Mapping)}
	prev = state.get(probe) or {}
	at = utc(result.get("checked_at"))
	prev_at = utc(prev.get("at"))
	if prev_at and at and at < prev_at:
		return state  # late duplicate / out of order: ignore
	ok = bool(result.get("ok"))
	failing_before = _int(prev.get("fails"), 0) > 0
	state[probe] = {
		"fails": 0 if ok else _int(prev.get("fails"), 0) + 1,
		"ok": ok,
		"at": iso(at),
		# first failure of the current failing streak (incidents start there, not at the decision)
		"since": None if ok else (prev.get("since") if failing_before and prev.get("since") else iso(at)),
		"code": result.get("status_code"),
		"ms": result.get("response_ms"),
		"error": "" if ok else truncate(result.get("error") or "", 200),
	}
	return state


def freshness_seconds(interval_minutes: Any) -> int:
	"""A probe's last result counts for 3 intervals (min 5 minutes)."""
	return max(300, 3 * 60 * max(1, _int(interval_minutes, DEFAULT_INTERVAL)))


def fresh_probes(
	state: Mapping[str, Any], now: datetime, interval_minutes: Any, assigned: Iterable[str] | None = None
) -> dict[str, dict]:
	allowed = set(assigned) if assigned is not None else None
	limit = freshness_seconds(interval_minutes)
	now = utc(now)
	out = {}
	for probe, row in (state or {}).items():
		if allowed is not None and probe not in allowed:
			continue
		at = utc((row or {}).get("at"))
		if at is not None and (now - at).total_seconds() <= limit:
			out[probe] = row
	return out


def quorum_for(n_probes: int, configured: Any = 0) -> int:
	"""Configured quorum clamped to 1..n; 0 = majority (n // 2 + 1)."""
	if n_probes <= 0:
		return 0
	q = _int(configured, 0)
	if q <= 0:
		return n_probes // 2 + 1
	return max(1, min(q, n_probes))


def decide(
	state: Mapping[str, Any],
	now: datetime,
	interval_minutes: Any,
	threshold: Any = DEFAULT_THRESHOLD,
	quorum: Any = 0,
	assigned: Iterable[str] | None = None,
) -> str | None:
	"""'down' / 'up' / None from the fresh probe states (see module docstring)."""
	fresh = fresh_probes(state, now, interval_minutes, assigned)
	if not fresh:
		return None
	q = quorum_for(len(fresh), quorum)
	threshold = max(1, _int(threshold, DEFAULT_THRESHOLD))
	failing = sum(1 for r in fresh.values() if _int(r.get("fails"), 0) >= threshold)
	if failing >= q:
		return "down"
	if sum(1 for r in fresh.values() if r.get("ok")) >= q:
		return "up"
	return None


def next_status(current: Any, decision: str | None) -> tuple[str, str | None]:
	"""(new monitor status, action) with action 'open' / 'close' / None.

	Pending -> Up is silent; Pending/Up -> Down opens an incident; Down -> Up resolves it.
	Paused monitors never change."""
	current = current if current in MONITOR_STATUSES else PENDING
	if current == PAUSED or decision is None:
		return current, None
	if decision == "down":
		return (DOWN, None) if current == DOWN else (DOWN, "open")
	if decision == "up":
		return (UP, "close") if current == DOWN else (UP, None)
	return current, None


def failure_started(state: Mapping[str, Any], now: datetime, interval_minutes: Any, default: datetime | None = None) -> datetime | None:
	"""Earliest start of the current failing streak among fresh failing probes (else `default`)."""
	starts = [utc(r.get("since")) for r in fresh_probes(state, now, interval_minutes).values() if not r.get("ok")]
	starts = [s for s in starts if s is not None]
	return min(starts) if starts else default


def failing_summary(state: Mapping[str, Any], now: datetime, interval_minutes: Any) -> str:
	"""'Unexpected status 502 (2/3 probes)' style cause of an incident."""
	fresh = fresh_probes(state, now, interval_minutes)
	bad = [r for r in fresh.values() if not r.get("ok")]
	if not bad:
		return ""
	errors = [r.get("error") or "" for r in bad]
	common = max(set(errors), key=errors.count) or "Check failed"
	return f"{common} ({len(bad)}/{len(fresh)} probes)"


# --------------------------------------------------------------------------- rollups & uptime


def hour_start(value: Any) -> datetime:
	dt = utc(value)
	return dt.replace(minute=0, second=0, microsecond=0)


def rollup_key(monitor: str, hour: datetime) -> str:
	return f"{monitor}-{hour:%Y%m%d%H}"


def rollup(results: Iterable[Mapping[str, Any]]) -> dict[tuple[str, datetime], dict[str, Any]]:
	"""Aggregate check results per (monitor, hour): checks, failures, response_ms_sum,
	response_count, max_response_ms. Merged into Uptime Rollup rows by addition."""
	out: dict[tuple[str, datetime], dict[str, Any]] = {}
	for r in results:
		key = (r["monitor"], hour_start(r["checked_at"]))
		agg = out.setdefault(key, {"checks": 0, "failures": 0, "response_ms_sum": 0, "response_count": 0, "max_response_ms": 0})
		agg["checks"] += 1
		if not r.get("ok"):
			agg["failures"] += 1
		ms = r.get("response_ms")
		if ms is not None and r.get("ok"):
			agg["response_ms_sum"] += int(ms)
			agg["response_count"] += 1
			agg["max_response_ms"] = max(agg["max_response_ms"], int(ms))
	return out


def merge_rollup(existing: Mapping[str, Any] | None, new: Mapping[str, Any]) -> dict[str, Any]:
	existing = existing or {}
	return {
		"checks": _int(existing.get("checks"), 0) + new["checks"],
		"failures": _int(existing.get("failures"), 0) + new["failures"],
		"response_ms_sum": _int(existing.get("response_ms_sum"), 0) + new["response_ms_sum"],
		"response_count": _int(existing.get("response_count"), 0) + new["response_count"],
		"max_response_ms": max(_int(existing.get("max_response_ms"), 0), new["max_response_ms"]),
	}


def response_series(rows: Iterable[Mapping[str, Any]], start: datetime, hours: int) -> list[dict[str, Any]]:
	"""Hourly average response time points [{t, ms|None, checks, failures}] for a chart,
	one per hour from `start` (gaps = None)."""
	start = hour_start(start)
	by_hour: dict[datetime, dict] = {}
	for r in rows:
		h = hour_start(r["hour"])
		agg = by_hour.setdefault(h, {"sum": 0, "n": 0, "checks": 0, "failures": 0})
		agg["sum"] += _int(r.get("response_ms_sum"), 0)
		agg["n"] += _int(r.get("response_count"), 0)
		agg["checks"] += _int(r.get("checks"), 0)
		agg["failures"] += _int(r.get("failures"), 0)
	out = []
	for i in range(hours):
		h = start + timedelta(hours=i)
		agg = by_hour.get(h)
		out.append({
			"t": iso(h),
			"ms": round(agg["sum"] / agg["n"]) if agg and agg["n"] else None,
			"checks": agg["checks"] if agg else 0,
			"failures": agg["failures"] if agg else 0,
		})
	return out


def _overlap(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> float:
	return max(0.0, (min(a1, b1) - max(a0, b0)).total_seconds())


def daily_uptime(
	incidents: Iterable[Mapping[str, Any]],
	now: datetime,
	days: int = STATUS_DAYS,
	monitored_since: Any = None,
) -> list[dict[str, Any]]:
	"""Per-day uptime (UTC days, oldest first) from incidents {started_at, resolved_at}.

	Each day: {date, uptime (percent, None = not monitored yet), down_seconds, incidents}.
	A day only counts the time the monitor existed (`monitored_since`) and up to `now`."""
	now = utc(now)
	since = utc(monitored_since)
	spans = []
	for inc in incidents:
		s = utc(inc.get("started_at"))
		if s is None:
			continue
		e = utc(inc.get("resolved_at")) or now
		spans.append((s, max(s, e)))
	today = now.replace(hour=0, minute=0, second=0, microsecond=0)
	out = []
	for i in range(days - 1, -1, -1):
		d0 = today - timedelta(days=i)
		d1 = min(d0 + timedelta(days=1), now)
		w0 = max(d0, since) if since else d0
		window = (d1 - w0).total_seconds()
		if window <= 0:
			out.append({"date": d0.date().isoformat(), "uptime": None, "down_seconds": 0, "seconds": 0, "incidents": 0})
			continue
		down = sum(_overlap(w0, d1, s, e) for s, e in spans)
		d_end = d0 + timedelta(days=1)
		count = sum(1 for s, e in spans if _overlap(d0, d_end, s, e) > 0 or d0 <= s < d_end)
		down = min(down, window)
		out.append({
			"date": d0.date().isoformat(),
			"uptime": round(100.0 * (1 - down / window), 3),
			"down_seconds": int(down),
			"seconds": int(window),
			"incidents": count,
		})
	return out


def uptime_percent(days_list: Iterable[Mapping[str, Any]]) -> float | None:
	"""Overall uptime of daily_uptime() rows weighted by observed seconds (None = no data)."""
	total = down = 0
	for d in days_list:
		if d.get("uptime") is None:
			continue
		total += _int(d.get("seconds"), 0)
		down += _int(d.get("down_seconds"), 0)
	if total <= 0:
		return None
	return round(100.0 * (1 - down / total), 3)


def bar_level(uptime: float | None) -> str:
	"""CSS class of a status bar: none / up / minor (< 99.9 %) / major (< 99 %) / down (< 95 %)."""
	if uptime is None:
		return "none"
	if uptime >= 99.9:
		return "up"
	if uptime >= 99.0:
		return "minor"
	if uptime >= 95.0:
		return "major"
	return "down"


def format_duration(seconds: Any) -> str:
	s = max(0, _int(seconds, 0))
	if s < 60:
		return f"{s}s"
	m, s = divmod(s, 60)
	if m < 60:
		return f"{m}m" + (f" {s}s" if s else "")
	h, m = divmod(m, 60)
	if h < 48:
		return f"{h}h" + (f" {m}m" if m else "")
	d, h = divmod(h, 24)
	return f"{d}d" + (f" {h}h" if h else "")


def overall_status(statuses: Iterable[str]) -> str:
	"""Status page banner: 'operational' / 'partial' / 'major' / 'unknown'."""
	statuses = [s for s in statuses if s != PAUSED]
	if not statuses or all(s == PENDING for s in statuses):
		return "unknown"
	down = sum(1 for s in statuses if s == DOWN)
	if not down:
		return "operational"
	return "major" if down == len([s for s in statuses if s != PENDING]) else "partial"


# --------------------------------------------------------------------------- status pages


def validate_status_page(data: Mapping[str, Any], team_monitors: Iterable[str]) -> dict[str, Any]:
	"""Portal input for a team's status page. `team_monitors`: names of the team's monitors.
	`monitors` = [{monitor, display_name}] in display order."""
	title = re.sub(r"\s+", " ", str(data.get("title") or "")).strip()
	if not title:
		raise MonitorError("Title is required")
	if len(title) > TITLE_MAX:
		raise MonitorError("Title is too long")
	slug = validate_slug(data.get("slug") or suggest_slug(title))
	description = str(data.get("description") or "").strip()
	if len(description) > 500:
		raise MonitorError("The description is too long")
	known = set(team_monitors)
	rows = []
	seen = set()
	for row in data.get("monitors") or []:
		if isinstance(row, str):
			row = {"monitor": row}
		name = str(row.get("monitor") or "").strip()
		if not name or name in seen:
			continue
		if name not in known:
			raise MonitorError("Unknown monitor")
		seen.add(name)
		display = re.sub(r"\s+", " ", str(row.get("display_name") or "")).strip()
		if len(display) > TITLE_MAX:
			raise MonitorError("Name is too long")
		rows.append({"monitor": name, "display_name": display})
	if len(rows) > 50:
		raise MonitorError("A status page can show at most 50 monitors")
	return {
		"title": title,
		"slug": slug,
		"description": description,
		"status_domain": validate_domain(data.get("status_domain")),
		"enabled": 1 if _truthy(data.get("enabled", True)) else 0,
		"show_incident_history": 1 if _truthy(data.get("show_incident_history", True)) else 0,
		"monitors": rows,
	}


def validate_incident_update(data: Mapping[str, Any]) -> dict[str, Any]:
	status = str(data.get("status") or "").strip().title()
	if status not in UPDATE_STATUSES:
		raise MonitorError("Choose a status for the update")
	message = str(data.get("message") or "").strip()
	if not message:
		raise MonitorError("Message is required")
	if len(message) > 2000:
		raise MonitorError("The message is too long")
	title = re.sub(r"\s+", " ", str(data.get("title") or "")).strip()
	if len(title) > 140:
		raise MonitorError("Title is too long")
	return {"status": status, "message": message, "title": title}


def public_monitor(monitor: Mapping[str, Any], display_name: Any, days: list[dict]) -> dict[str, Any]:
	"""What a status page shows of a monitor: no URL, host, path, error text or probe data."""
	return {
		"name": str(display_name or monitor.get("title") or "")[:TITLE_MAX] or "Service",
		"status": monitor.get("status") if monitor.get("status") in MONITOR_STATUSES else PENDING,
		"uptime": uptime_percent(days),
		"days": [{"date": d["date"], "uptime": d["uptime"], "level": bar_level(d["uptime"]),
		          "down": format_duration(d["down_seconds"]) if d["down_seconds"] else ""} for d in days],
	}


def public_incident(incident: Mapping[str, Any], display_name: Any, updates: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
	"""Incident as shown publicly: no cause/error text unless written as an update."""
	started = utc(incident.get("started_at"))
	resolved = utc(incident.get("resolved_at"))
	return {
		"title": str(incident.get("public_title") or "").strip() or f"{display_name} is unavailable",
		"service": str(display_name or ""),
		"status": incident.get("status") if incident.get("status") in INCIDENT_STATUSES else OPEN,
		"started_at": iso(started),
		"resolved_at": iso(resolved),
		"started_display": display_time(started),
		"resolved_display": display_time(resolved),
		"duration": format_duration((resolved - started).total_seconds()) if started and resolved else "",
		"updates": [
			{"status": u.get("status"), "message": str(u.get("message") or ""), "posted_at": iso(u.get("posted_at")),
			 "posted_display": display_time(u.get("posted_at"))}
			for u in sorted(updates, key=lambda u: utc(u.get("posted_at")) or datetime.min, reverse=True)
		],
	}


def status_cache_key(slug_or_host: str, lang: str = "en") -> str:
	return f"nexaploy:status_page:{slug_or_host}:{lang or 'en'}"
