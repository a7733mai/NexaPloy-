"""Server health and maintenance: pure logic (no frappe import; tests/test_server_maintenance.py).

* ``evaluate_health(facts, thresholds, context)`` -> issues
  ``[{code, severity: critical|warning|info, title, detail, hint}]`` from the heartbeat facts
  (``facts.health`` light facts, ``facts.os_updates``, containers) plus the last full check
  (``server.diagnose``) and control-plane context (latest agent, stale backups, clock).
* ``health_status`` / ``health_notification`` (OK / Warning / Critical / Unknown / Offline,
  throttled ``server_health_critical`` / ``server_health_ok``)
* OS update tracking, maintenance windows, cleanup targets / params / summaries.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

CRITICAL, WARNING, INFO = "critical", "warning", "info"
SEVERITY_ORDER = {CRITICAL: 0, WARNING: 1, INFO: 2}
OK, WARN, CRIT, UNKNOWN, OFFLINE = "OK", "Warning", "Critical", "Unknown", "Offline"
HEALTH_STATUSES = ("", OK, WARN, CRIT, UNKNOWN, OFFLINE)
STATUS_RANK = {CRIT: 0, OFFLINE: 1, WARN: 2, UNKNOWN: 3, OK: 4, "": 5}

DEFAULT_THRESHOLDS = {
	"disk_warning": 85.0,
	"disk_critical": 95.0,
	"inode_warning": 90.0,
	"inode_critical": 98.0,
	"memory_warning": 90.0,
	"memory_critical": 98.0,
	"load_per_cpu": 2.0,
	"swap_warning": 80.0,
	"clock_skew_seconds": 120.0,
	"db_connections_percent": 80.0,
	"binlog_bytes": 20 * 1024 ** 3,
}
SETTINGS_THRESHOLDS = {
	"health_disk_warning_percent": "disk_warning",
	"health_disk_critical_percent": "disk_critical",
	"health_inode_warning_percent": "inode_warning",
	"health_memory_warning_percent": "memory_warning",
	"health_load_per_cpu": "load_per_cpu",
}
SHARED = {"nexaploy-traefik": "Traefik (proxy)", "nexaploy-db": "MariaDB (nexaploy-db)"}
DIAGNOSE_MAX_AGE = timedelta(hours=24)
HEALTH_ALERT_INTERVAL = timedelta(hours=6)
HEALTH_CRITICAL_EVENT = "server_health_critical"
HEALTH_OK_EVENT = "server_health_ok"


def _num(value: Any, default: float = 0.0) -> float:
	try:
		return float(value)
	except (TypeError, ValueError):
		return default


def thresholds_from_settings(row: Mapping[str, Any] | None) -> dict[str, float]:
	out = dict(DEFAULT_THRESHOLDS)
	for field, key in SETTINGS_THRESHOLDS.items():
		value = _num((row or {}).get(field), 0.0)
		if value > 0:
			out[key] = value
	if out["disk_critical"] < out["disk_warning"]:
		out["disk_critical"] = out["disk_warning"]
	return out


def _issue(code, severity, title, detail="", hint=""):
	return {"code": code, "severity": severity, "title": title, "detail": detail, "hint": hint}


def _parse_dt(value: Any) -> datetime | None:
	if isinstance(value, datetime):
		return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
	try:
		dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
	except (TypeError, ValueError):
		return None
	return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _filesystems(facts: Mapping[str, Any]) -> list[dict[str, Any]]:
	health = facts.get("health") or {}
	rows = health.get("filesystems")
	if isinstance(rows, list) and rows:
		return [r for r in rows if isinstance(r, dict)]
	return [r for r in facts.get("mounts") or [] if isinstance(r, dict)]


def evaluate_health(facts: Mapping[str, Any] | None, thresholds: Mapping[str, float] | None = None,
                    context: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
	"""Issues for one server, worst first. `context` keys (all optional): now (aware UTC),
	latest_agent (version), agent_outdated (bool), stale_backups ([site]), diagnose (the
	``health`` of the last server.diagnose), diagnosed_at."""
	facts = facts or {}
	t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
	ctx = context or {}
	now = ctx.get("now") or datetime.now(timezone.utc)
	health = dict(facts.get("health") or {})
	diag = ctx.get("diagnose") or {}
	diag_at = _parse_dt(ctx.get("diagnosed_at"))
	if diag and (diag_at is None or now - diag_at <= DIAGNOSE_MAX_AGE):
		for key in ("oom_kills", "oom_recent", "mariadb", "tls", "docker_df", "docker_root_fs"):
			if health.get(key) is None and diag.get(key) is not None:
				health[key] = diag[key]
	issues: list[dict[str, Any]] = []

	# disk space / inodes
	seen = set()
	for fs in _filesystems(facts) + ([health["docker_root_fs"]] if isinstance(health.get("docker_root_fs"), dict) else []):
		path = fs.get("path") or "?"
		if path in seen:
			continue
		seen.add(path)
		pct = _num(fs.get("used_percent"))
		free = fs.get("free")
		detail = f"{path} is {pct:.1f}% full" + (f" ({_fmt_bytes(free)} free)" if free is not None else "")
		hint = "Run Clean up (Docker images, build cache, logs) or grow the disk."
		if pct >= t["disk_critical"]:
			issues.append(_issue("disk_critical", CRITICAL, "Disk almost full", detail, hint))
		elif pct >= t["disk_warning"]:
			issues.append(_issue("disk_warning", WARNING, "Disk filling up", detail, hint))
		ipct = _num(fs.get("inodes_used_percent"))
		if ipct >= t["inode_critical"]:
			issues.append(_issue("inodes_critical", CRITICAL, "Out of inodes", f"{path}: {ipct:.1f}% of inodes used",
			                     "Many small files: clean up logs and Docker leftovers."))
		elif ipct >= t["inode_warning"]:
			issues.append(_issue("inodes_warning", WARNING, "Inodes running out", f"{path}: {ipct:.1f}% of inodes used",
			                     "Many small files: clean up logs and Docker leftovers."))

	# memory / swap / load
	mem = health.get("mem_used_percent")
	if mem is None and _num(facts.get("mem_total")):
		mem = 100.0 * (1 - _num(facts.get("mem_available")) / _num(facts.get("mem_total")))
	if mem is not None:
		mem = _num(mem)
		hint = "Reduce workers, move sites to another server or add memory."
		if mem >= t["memory_critical"]:
			issues.append(_issue("memory_critical", CRITICAL, "Memory exhausted", f"{mem:.0f}% of memory in use", hint))
		elif mem >= t["memory_warning"]:
			issues.append(_issue("memory_warning", WARNING, "Memory almost full", f"{mem:.0f}% of memory in use", hint))
	if _num(health.get("swap_used_percent")) >= t["swap_warning"]:
		issues.append(_issue("swap_heavy", WARNING, "Heavy swap use", f"{_num(health.get('swap_used_percent')):.0f}% of swap in use",
		                     "The server is short of memory."))
	cpus = _num(facts.get("cpu_count"))
	load = facts.get("load_avg") or []
	if cpus and isinstance(load, list) and len(load) >= 2:
		per_cpu = _num(load[1]) / cpus
		if per_cpu >= 2 * t["load_per_cpu"]:
			issues.append(_issue("load_critical", CRITICAL, "Server overloaded", f"5-minute load {load[1]} on {int(cpus)} CPU(s)",
			                     "Check the busiest benches (Insights) or add CPUs."))
		elif per_cpu >= t["load_per_cpu"]:
			issues.append(_issue("load_warning", WARNING, "High load", f"5-minute load {load[1]} on {int(cpus)} CPU(s)",
			                     "Check the busiest benches (Insights) or add CPUs."))
	oom = health.get("oom_kills")
	if isinstance(oom, (int, float)) and oom > 0:
		issues.append(_issue("oom_kills", WARNING, "Processes killed for lack of memory",
		                     f"{int(oom)} OOM kill(s) since boot", "Add memory or reduce workers."))

	# docker, containers, shared stacks
	if facts and "docker_version" in facts and not facts.get("docker_version"):
		issues.append(_issue("docker_down", CRITICAL, "Docker is not responding", "", "Check `systemctl status docker`."))
	containers = [c for c in facts.get("containers") or [] if isinstance(c, dict)]
	by_name = {c.get("name"): c for c in containers}
	for name, label in SHARED.items():
		c = by_name.get(name)
		if containers and (c is None or c.get("state") != "running" or c.get("health") == "unhealthy"):
			state = "missing" if c is None else f"{c.get('state')}{' (unhealthy)' if c.get('health') == 'unhealthy' else ''}"
			issues.append(_issue(f"{name}_down", CRITICAL, f"{label} is down", f"{name}: {state}",
			                     f"Check `docker logs {name}` on the server."))
	restarting = sorted(c.get("name") for c in containers if c.get("state") == "restarting")
	if restarting:
		issues.append(_issue("containers_restarting", WARNING, "Containers restarting", ", ".join(restarting[:10]),
		                     "A container keeps crashing: check its logs."))
	for bench, info in sorted((facts.get("bench_health") or {}).items()):
		bad = [n for n in (info or {}).get("unhealthy") or [] if n not in restarting]
		if bad:
			issues.append(_issue("bench_unhealthy", WARNING, f"Unhealthy containers in {bench}", ", ".join(bad[:10]),
			                     "Restart the bench or check the container logs."))

	# systemd / time
	units = health.get("failed_units") or []
	ours = [u for u in units if u.startswith(("nexaploy", "docker", "containerd"))]
	if ours:
		issues.append(_issue("units_failed", CRITICAL, "Nexaploy services failed", ", ".join(ours),
		                     "Check `systemctl status <unit>` and `journalctl -u <unit>`."))
	others = [u for u in units if u not in ours]
	if others:
		issues.append(_issue("mounts_failed", WARNING, "Mounts or services failed", ", ".join(others[:10]),
		                     "Offsite attachments may be unavailable: check the rclone mounts."))
	if health.get("ntp_synchronized") is False:
		issues.append(_issue("ntp", WARNING, "Clock not synchronized", "NTPSynchronized=no",
		                     "Enable time sync: `timedatectl set-ntp true`."))
	agent_time = _parse_dt(health.get("agent_time"))
	if agent_time is not None:
		skew = abs((agent_time - now).total_seconds())
		if skew >= t["clock_skew_seconds"]:
			issues.append(_issue("clock_skew", WARNING, "Server clock is off", f"{int(skew)} s difference with the control plane",
			                     "Enable time sync: `timedatectl set-ntp true`."))

	# MariaDB / certificates
	db = health.get("mariadb") or {}
	if db.get("reachable") is False:
		issues.append(_issue("db_unreachable", CRITICAL, "MariaDB not answering", str(db.get("error") or "")[:200],
		                     "Check `docker logs nexaploy-db`."))
	if _num(db.get("max_connections")) and _num(db.get("threads_connected")) / _num(db.get("max_connections")) * 100 >= t["db_connections_percent"]:
		issues.append(_issue("db_connections", WARNING, "MariaDB connections almost exhausted",
		                     f"{int(_num(db.get('threads_connected')))} of {int(_num(db.get('max_connections')))}",
		                     "Raise max_connections or reduce workers."))
	if _num(db.get("binlog_bytes")) >= t["binlog_bytes"]:
		issues.append(_issue("binlog_large", WARNING, "Binary logs are large", _fmt_bytes(db.get("binlog_bytes")),
		                     "Check point-in-time recovery archiving (binlogs are purged after archiving)."))
	tls = health.get("tls") or {}
	if tls.get("failing"):
		issues.append(_issue("tls_failing", WARNING, "Certificate problems", ", ".join(tls["failing"][:10]),
		                     "Check DNS of the domains and Traefik logs."))
	if tls.get("expiring"):
		issues.append(_issue("tls_expiring", WARNING, "Certificates expiring soon", ", ".join(tls["expiring"][:10]),
		                     "Renewal is failing: check DNS and Traefik logs."))

	# backups, agent, OS
	stale = ctx.get("stale_backups") or []
	if stale:
		issues.append(_issue("backups_stale", WARNING, "Backups are late", ", ".join(sorted(stale)[:10]),
		                     "Check the failed backup jobs and the backup storage."))
	if ctx.get("agent_outdated"):
		issues.append(_issue("agent_outdated", INFO, "Agent update available",
		                     f"{facts.get('agent_version') or '?'} -> {ctx.get('latest_agent')}", "Use Upgrade agent."))
	os_up = facts.get("os_updates") or {}
	if os_up.get("supported"):
		if os_up.get("reboot_required"):
			issues.append(_issue("reboot_required", INFO, "Reboot required",
			                     ", ".join((os_up.get("reboot_required_pkgs") or [])[:10]), "Use Reboot now or a maintenance window."))
		if _num(os_up.get("security")):
			issues.append(_issue("security_updates", INFO, "Security updates available",
			                     f"{int(_num(os_up.get('security')))} package(s)", "Use Update now or enable automatic security updates."))
	issues.sort(key=lambda i: SEVERITY_ORDER.get(i["severity"], 9))
	return issues


def health_status(issues: Iterable[Mapping[str, Any]], server_status: str | None, has_facts: bool) -> str:
	if server_status == "Offline":
		return OFFLINE
	if not has_facts:
		return UNKNOWN
	sev = {i.get("severity") for i in issues}
	if CRITICAL in sev:
		return CRIT
	if WARNING in sev:
		return WARN
	return OK


def health_notification(state: Mapping[str, Any] | None, status: str, now: datetime,
                        interval: timedelta = HEALTH_ALERT_INTERVAL) -> tuple[str | None, dict[str, Any]]:
	"""(event to send or None, new state). Critical alerts at most once per `interval`;
	OK is sent once after a Critical alert."""
	state = dict(state or {})
	if status == CRIT:
		last = _parse_dt(state.get("at"))
		if state.get("notified") != CRIT or last is None or now - last >= interval:
			return HEALTH_CRITICAL_EVENT, {"notified": CRIT, "at": now.isoformat(timespec="seconds")}
		return None, state
	if status == OK and state.get("notified") == CRIT:
		return HEALTH_OK_EVENT, {"notified": OK, "at": now.isoformat(timespec="seconds")}
	return None, state


def worst_first(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
	return sorted(rows, key=lambda r: (STATUS_RANK.get(r.get("health_status") or "", 5),
	                                   -len(r.get("issues") or []), str(r.get("hostname") or r.get("name"))))


def _fmt_bytes(n: Any) -> str:
	n = _num(n)
	for unit in ("B", "KB", "MB", "GB", "TB"):
		if abs(n) < 1024 or unit == "TB":
			return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
		n /= 1024
	return f"{n:.1f} TB"


fmt_bytes = _fmt_bytes


# --------------------------------------------------------------------------- OS updates
IN_PROGRESS, REBOOTING, SUCCEEDED, FAILED = "In Progress", "Rebooting", "Succeeded", "Failed"
OS_UPDATE_STATUSES = ("", IN_PROGRESS, REBOOTING, SUCCEEDED, FAILED)
OS_BUSY = (IN_PROGRESS, REBOOTING)
MODES = ("all", "security")
REBOOTS = ("never", "if_required", "always")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DAYS = ("Every day", *WEEKDAYS)
REBOOT_TIMEOUT = timedelta(minutes=30)


def os_fields(os_updates: Mapping[str, Any] | None) -> dict[str, Any]:
	"""Server values from facts.os_updates (datetimes as aware UTC; boot_time from unix)."""
	o = os_updates or {}
	if not o.get("supported"):
		return {"os_updates_supported": 0}
	boot = o.get("boot_time")
	return {
		"os_updates_supported": 1,
		"os_pending_updates": int(_num(o.get("pending"))),
		"os_security_updates": int(_num(o.get("security"))),
		"os_reboot_required": 1 if o.get("reboot_required") else 0,
		"os_running_kernel": (o.get("running_kernel") or "")[:140] or None,
		"os_newest_kernel": (o.get("newest_kernel") or "")[:140] or None,
		"os_boot_time": datetime.fromtimestamp(int(boot), timezone.utc) if isinstance(boot, (int, float)) and boot else None,
		"os_updates_checked_at": _parse_dt(o.get("checked_at")),
		"unattended_security": 1 if o.get("unattended_security") else 0,
	}


def validate_os_update(mode: str | None, reboot: str | None, delay: Any = 60) -> dict[str, Any]:
	mode = (mode or "all").strip()
	reboot = (reboot or "if_required").strip()
	if mode not in MODES:
		raise ValueError("Update mode must be all or security")
	if reboot not in REBOOTS:
		raise ValueError("Reboot must be never, if_required or always")
	delay = int(_num(delay, 60))
	if not 10 <= delay <= 3600:
		raise ValueError("Reboot delay must be between 10 and 3600 seconds")
	return {"mode": mode, "reboot": reboot, "reboot_delay_seconds": delay}


def os_job_transition(kind: str, job_status: str, result: Mapping[str, Any] | None) -> str | None:
	if job_status == "Failure":
		return FAILED
	if job_status != "Success":
		return None
	if kind == "server.os_reboot" or (result or {}).get("reboot_scheduled"):
		return REBOOTING
	return SUCCEEDED


def heartbeat_os_transition(status: str | None, started_at: datetime | None, boot_time: datetime | None) -> str | None:
	"""Rebooting -> Succeeded once the server reports a boot after the job started (both in the
	same time zone)."""
	if status == REBOOTING and started_at and boot_time and boot_time > started_at:
		return SUCCEEDED
	return None


def reboot_timed_out(status: str | None, started_at: datetime | None, now: datetime,
                     timeout: timedelta = REBOOT_TIMEOUT * 4) -> bool:
	return status in OS_BUSY and started_at is not None and now - started_at > timeout


def window_due(now_utc: datetime, day: str | None, hour: Any, last_run_utc: datetime | None) -> bool:
	"""True inside the hour window (UTC) when it has not run in this window yet."""
	day = day or "Sunday"
	if day not in DAYS:
		return False
	if day != "Every day" and WEEKDAYS[now_utc.weekday()] != day:
		return False
	if now_utc.hour != int(_num(hour, -1)):
		return False
	start = now_utc.replace(minute=0, second=0, microsecond=0)
	return last_run_utc is None or last_run_utc < start


def manage_allowed(is_platform_admin: bool, allow_customers: Any, ownership: str | None, team_admin: bool) -> bool:
	"""Platform admins: any server. Team Owner/Admin: own BYOS servers when allowed."""
	if is_platform_admin:
		return True
	return bool(allow_customers) and ownership == "BYOS" and bool(team_admin)


# --------------------------------------------------------------------------- cleanup
CLEANUP_TARGETS = ("docker_images", "docker_build_cache", "docker_containers", "docker_logs", "bench_logs",
                   "backups_local", "agent_data", "journal", "apt", "tmp")
# automatic cleanup when the disk is critical: nothing anyone may still want
SAFE_TARGETS = ("docker_images", "docker_build_cache", "bench_logs", "journal", "apt")
TARGET_LABELS = {
	"docker_images": "Unused Docker images",
	"docker_build_cache": "Docker build cache",
	"docker_containers": "Exited one-off containers",
	"docker_logs": "Oversized container logs",
	"bench_logs": "Old bench and site logs",
	"backups_local": "Uploaded local backups",
	"agent_data": "Agent job history and build logs",
	"journal": "System journal",
	"apt": "Package cache",
	"tmp": "Temporary files",
}
AUTO_CLEANUP_INTERVAL = timedelta(hours=12)


def clean_targets(raw: Any) -> list[str]:
	if raw in (None, "", []):
		return list(CLEANUP_TARGETS)
	if isinstance(raw, str):
		items = [x.strip() for x in raw.replace(",", "\n").splitlines() if x.strip()]
	else:
		items = [str(x).strip() for x in raw]
	unknown = [x for x in items if x not in CLEANUP_TARGETS]
	if unknown:
		raise ValueError(f"Unknown cleanup target: {', '.join(unknown)}")
	out = [t for t in CLEANUP_TARGETS if t in items]
	if not out:
		raise ValueError("Choose at least one cleanup target")
	return out


def cleanup_params(targets: Any, dry_run: bool, log_retention_days: Any = 14,
                   backup_local_keep_days: Any = None) -> dict[str, Any]:
	params: dict[str, Any] = {
		"targets": clean_targets(targets),
		"dry_run": bool(dry_run),
		"log_retention_days": max(1, min(3650, int(_num(log_retention_days, 14)) or 14)),
	}
	keep = int(_num(backup_local_keep_days, 0))
	if keep > 0:
		params["backup_local_keep_days"] = min(keep, 3650)
	return params


def cleanup_summary(result: Mapping[str, Any] | None) -> dict[str, Any]:
	result = result or {}
	targets = result.get("targets") or {}
	rows = []
	for name in CLEANUP_TARGETS:
		r = targets.get(name)
		if isinstance(r, dict):
			rows.append({"target": name, "label": TARGET_LABELS[name], "freed_bytes": int(_num(r.get("freed_bytes"))),
			             "items": int(_num(r.get("items"))), "notes": list(r.get("notes") or [])[:20],
			             "errors": list(r.get("errors") or [])[:20]})
	return {
		"dry_run": bool(result.get("dry_run", True)),
		"freed_bytes": sum(r["freed_bytes"] for r in rows),
		"items": sum(r["items"] for r in rows),
		"errors": sum(len(r["errors"]) for r in rows),
		"targets": rows,
	}


def auto_cleanup_due(issues: Iterable[Mapping[str, Any]], last_auto: datetime | None, now: datetime,
                     interval: timedelta = AUTO_CLEANUP_INTERVAL) -> bool:
	if not any(i.get("code") == "disk_critical" for i in issues):
		return False
	return last_auto is None or now - last_auto >= interval


def stale_backups(sites: Iterable[Mapping[str, Any]], now: datetime, intervals: Mapping[str, timedelta],
                  factor: float = 2.0) -> list[str]:
	"""Sites whose last backup is older than `factor` x their frequency interval."""
	out = []
	for s in sites:
		interval = intervals.get(s.get("backup_frequency") or "")
		last = s.get("last_backup_at")
		if not interval or not last:
			continue
		if now - last > interval * factor:
			out.append(s.get("name"))
	return out
