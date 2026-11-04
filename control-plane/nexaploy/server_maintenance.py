"""Server maintenance & health (ARCHITECTURE.md §17; pure logic in server_health_utils).

* heartbeat ingest (`on_heartbeat`): OS update status (`facts.os_updates`) -> Server os_* fields,
  Rebooting -> Succeeded when the boot time is newer than the update start; health issues
  (`evaluate_health`) -> health_status / health_issues; automatic safe cleanup when a disk is
  critical (at most once per 12 h)
* actions: `start_os_update`, `start_reboot`, `set_unattended`, `save_maintenance`,
  `start_diagnose`, `start_cleanup` (platform admins: any server; team Owner/Admin: own BYOS
  servers when Nexaploy Settings.allow_customer_os_updates)
* schedulers: `check_server_health` (5 min: notifications, Offline, stuck updates) and
  `schedule_maintenance` (hourly: maintenance windows, weekly cleanup policy)
* Agent Job callbacks for server.os_update / os_reboot / os_configure / diagnose / cleanup
"""

from datetime import datetime, timezone

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from nexaploy import notify_utils as nu
from nexaploy import server_health_utils as hu
from nexaploy.agent_callbacks import handles

SETTINGS = "Nexaploy Settings"
ACTIVE = ["Pending", "Queued", "Running"]
MAINT_KINDS = ("server.os_update", "server.os_reboot", "server.os_configure", "server.diagnose", "server.cleanup")
SERVER_FIELDS = [
	"name", "server_id", "hostname", "team", "ownership", "status", "agent_version", "agent_channel",
	"os_updates_supported", "os_pending_updates", "os_security_updates", "os_reboot_required", "os_running_kernel",
	"os_newest_kernel", "os_boot_time", "os_updates_checked_at", "unattended_security", "os_update_status",
	"os_update_job", "os_update_started_at", "os_update_error", "last_os_update_at", "maintenance_enabled",
	"maintenance_day", "maintenance_hour", "maintenance_mode", "maintenance_reboot", "last_maintenance_at",
	"auto_cleanup_enabled", "health_status", "health_checked_at", "health_issues", "health_diagnosed_at",
	"health_alert_state", "last_cleanup_at", "last_cleanup_freed", "last_cleanup_job", "last_auto_cleanup_at",
	"last_scheduled_cleanup_at",
]


# --------------------------------------------------------------------------- time / settings


def _system_tz():
	from zoneinfo import ZoneInfo

	from frappe.utils import get_system_timezone

	try:
		return ZoneInfo(get_system_timezone() or "UTC")
	except Exception:
		return timezone.utc


def to_utc(dt):
	"""System-time-zone naive datetime (how Frappe stores them) -> aware UTC."""
	if not dt:
		return None
	if isinstance(dt, str):
		dt = frappe.utils.get_datetime(dt)
	if dt.tzinfo is None:
		dt = dt.replace(tzinfo=_system_tz())
	return dt.astimezone(timezone.utc)


def to_system(dt):
	"""Aware datetime -> naive system time for storing."""
	if not dt:
		return None
	return dt.astimezone(_system_tz()).replace(tzinfo=None)


def utcnow():
	return datetime.now(timezone.utc)


def get_settings():
	def get(field, default=None):
		try:
			value = frappe.db.get_single_value(SETTINGS, field)
		except Exception:
			value = None
		return default if value in (None, "") else value

	row = {f: get(f) for f in hu.SETTINGS_THRESHOLDS}
	return frappe._dict(
		allow_customers=cint(get("allow_customer_os_updates", 1)),
		thresholds=hu.thresholds_from_settings(row),
		auto_on_critical=cint(get("auto_cleanup_on_disk_critical", 1)),
		policy_enabled=cint(get("auto_cleanup_policy_enabled", 1)),
		policy_day=get("auto_cleanup_day", "Sunday"),
		policy_hour=cint(get("auto_cleanup_hour", 2)),
		policy_targets=get("auto_cleanup_targets", ""),
		log_retention_days=cint(get("cleanup_log_retention_days", 14)) or 14,
		backup_local_keep_days=cint(get("cleanup_backup_local_keep_days", 0)),
	)


def _server(name):
	row = frappe.db.get_value("Server", name, SERVER_FIELDS, as_dict=True)
	if not row:
		raise frappe.DoesNotExistError(_("Server {0} not found").format(name))
	return row


# --------------------------------------------------------------------------- permissions


def can_manage(server, user=None):
	from nexaploy.team_utils import role_allows
	from nexaploy.teams import get_team_role, is_platform_admin

	admin = is_platform_admin(user)
	role = None if admin or not server.team else get_team_role(server.team, user)
	return hu.manage_allowed(admin, get_settings().allow_customers, server.ownership,
	                         role_allows(role, "admin") if role else False)


def check_manage(server, user=None):
	if not can_manage(server, user):
		raise frappe.PermissionError(_("You are not allowed to run maintenance on this server"))


def _require_active(server):
	if server.status != "Active":
		frappe.throw(_("The server must be Active (online) for this action"))


def _active_job(server, kinds):
	return frappe.db.get_value(
		"Agent Job", {"server": server, "kind": ["in", list(kinds)], "status": ["in", ACTIVE]}, "name"
	)


def _log(server, action, details=None):
	try:
		from nexaploy.activity import log_platform_activity

		log_platform_activity(action, "Server", server.name, details, team=server.team)
	except Exception:
		pass


# --------------------------------------------------------------------------- actions


def start_os_update(name, mode="all", reboot="if_required", delay=60, check=True, origin="manual"):
	from nexaploy.agent_job import create_agent_job

	server = _server(name)
	if check:
		check_manage(server)
	_require_active(server)
	if server.os_update_status in hu.OS_BUSY or _active_job(server.name, ("server.os_update", "server.os_reboot")):
		frappe.throw(_("A system update or reboot is already in progress on this server"))
	try:
		params = hu.validate_os_update(mode, reboot, delay)
	except ValueError as exc:
		frappe.throw(_(str(exc)))
	job = create_agent_job(server.name, "server.os_update", params, "Server", server.name)
	frappe.db.set_value("Server", server.name, {
		"os_update_status": hu.IN_PROGRESS, "os_update_job": job.name, "os_update_started_at": now_datetime(),
		"os_update_error": None,
	}, update_modified=False)
	_log(server, "server.os_update", {**params, "job": job.name, "origin": origin})
	return job.name


def start_reboot(name, delay=60):
	from nexaploy.agent_job import create_agent_job

	server = _server(name)
	check_manage(server)
	_require_active(server)
	if server.os_update_status in hu.OS_BUSY:
		frappe.throw(_("A system update or reboot is already in progress on this server"))
	delay = max(10, min(3600, cint(delay) or 60))
	job = create_agent_job(server.name, "server.os_reboot", {"delay_seconds": delay}, "Server", server.name)
	frappe.db.set_value("Server", server.name, {
		"os_update_status": hu.IN_PROGRESS, "os_update_job": job.name, "os_update_started_at": now_datetime(),
		"os_update_error": None,
	}, update_modified=False)
	_log(server, "server.os_reboot", {"delay_seconds": delay, "job": job.name})
	return job.name


def set_unattended(name, enabled):
	from nexaploy.agent_job import create_agent_job

	server = _server(name)
	check_manage(server)
	_require_active(server)
	enabled = bool(cint(enabled))
	job = create_agent_job(server.name, "server.os_configure", {"unattended_security": enabled}, "Server", server.name)
	_log(server, "server.os_configure", {"unattended_security": enabled, "job": job.name})
	return job.name


def save_maintenance(name, values):
	server = _server(name)
	check_manage(server)
	values = frappe.parse_json(values) if isinstance(values, str) else (values or {})
	clean = {}
	if "maintenance_enabled" in values:
		clean["maintenance_enabled"] = 1 if cint(values["maintenance_enabled"]) else 0
	if "maintenance_day" in values:
		if values["maintenance_day"] not in hu.DAYS:
			frappe.throw(_("Invalid maintenance day"))
		clean["maintenance_day"] = values["maintenance_day"]
	if "maintenance_hour" in values:
		hour = cint(values["maintenance_hour"])
		if not 0 <= hour <= 23:
			frappe.throw(_("Maintenance hour must be between 0 and 23 (UTC)"))
		clean["maintenance_hour"] = hour
	if "maintenance_mode" in values:
		if values["maintenance_mode"] not in hu.MODES:
			frappe.throw(_("Update mode must be all or security"))
		clean["maintenance_mode"] = values["maintenance_mode"]
	if "maintenance_reboot" in values:
		if values["maintenance_reboot"] not in hu.REBOOTS:
			frappe.throw(_("Reboot must be never, if_required or always"))
		clean["maintenance_reboot"] = values["maintenance_reboot"]
	if "auto_cleanup_enabled" in values:
		clean["auto_cleanup_enabled"] = 1 if cint(values["auto_cleanup_enabled"]) else 0
	if not clean:
		frappe.throw(_("Nothing to change"))
	frappe.db.set_value("Server", server.name, clean, update_modified=True)
	_log(server, "server.maintenance_window", clean)
	return clean


def start_diagnose(name):
	from nexaploy.agent_job import create_agent_job

	server = _server(name)
	check_manage(server)
	_require_active(server)
	existing = _active_job(server.name, ("server.diagnose",))
	if existing:
		return existing
	job = create_agent_job(server.name, "server.diagnose", {}, "Server", server.name)
	_log(server, "server.diagnose", {"job": job.name})
	return job.name


def start_cleanup(name, targets=None, dry_run=True, check=True, origin="manual"):
	from nexaploy.agent_job import create_agent_job

	server = _server(name)
	if check:
		check_manage(server)
	_require_active(server)
	if _active_job(server.name, ("server.cleanup",)):
		frappe.throw(_("A cleanup is already running on this server"))
	s = get_settings()
	try:
		params = hu.cleanup_params(targets, dry_run, s.log_retention_days, s.backup_local_keep_days)
	except ValueError as exc:
		frappe.throw(_(str(exc)))
	if origin == "auto-disk":
		params["images_scope"] = "dangling"
	job = create_agent_job(server.name, "server.cleanup", params, "Server", server.name)
	if not params["dry_run"]:
		_log(server, "server.cleanup", {"targets": params["targets"], "job": job.name, "origin": origin})
	return job.name


def cleanup_job(name, job):
	"""Status and summary of one cleanup/diagnose/update job of this server."""
	row = frappe.db.get_value("Agent Job", job, ["name", "server", "kind", "status", "error", "result", "creation"],
	                          as_dict=True)
	if not row or row.server != name or row.kind not in MAINT_KINDS:
		raise frappe.DoesNotExistError(_("Job not found"))
	result = frappe.parse_json(row.result) if row.result else {}
	out = {"name": row.name, "kind": row.kind, "status": row.status, "error": row.error, "creation": row.creation}
	if row.kind == "server.cleanup":
		out["summary"] = hu.cleanup_summary(result)
	return out


def server_state(name):
	s = _server(name)
	issues = frappe.parse_json(s.health_issues) if s.health_issues else []
	last_cleanup = None
	if s.last_cleanup_job:
		try:
			last_cleanup = cleanup_job(name, s.last_cleanup_job)
		except frappe.DoesNotExistError:
			pass
	return {
		"server": s.name,
		"os": {k: s.get(k) for k in (
			"os_updates_supported", "os_pending_updates", "os_security_updates", "os_reboot_required",
			"os_running_kernel", "os_newest_kernel", "os_boot_time", "os_updates_checked_at", "unattended_security",
			"os_update_status", "os_update_job", "os_update_started_at", "os_update_error", "last_os_update_at")},
		"maintenance": {k: s.get(k) for k in (
			"maintenance_enabled", "maintenance_day", "maintenance_hour", "maintenance_mode", "maintenance_reboot",
			"last_maintenance_at", "auto_cleanup_enabled")},
		"health": {"status": s.health_status or hu.UNKNOWN, "issues": issues, "checked_at": s.health_checked_at,
		           "diagnosed_at": s.health_diagnosed_at,
		           "diagnose_job": _active_job(s.name, ("server.diagnose",))},
		"cleanup": {"last_at": s.last_cleanup_at, "last_freed": s.last_cleanup_freed, "last": last_cleanup,
		            "running_job": _active_job(s.name, ("server.cleanup",)),
		            "targets": [{"target": t, "label": hu.TARGET_LABELS[t]} for t in hu.CLEANUP_TARGETS]},
		"days": list(hu.DAYS),
		"status": s.status,
		"can_manage": can_manage(s),
	}


# --------------------------------------------------------------------------- heartbeat ingest


def _context(server_row, now):
	ctx = {"now": now}
	try:
		from nexaploy import agent_release_utils as aru
		from nexaploy.agent_upgrades import latest_release

		release = latest_release(server_row.get("agent_channel"))
		if release and aru.is_older(server_row.get("agent_version"), release.version):
			ctx.update(agent_outdated=True, latest_agent=release.version)
	except Exception:
		pass
	try:
		from nexaploy import ops_utils as ou

		sites = frappe.get_all("Site", filters={"server": server_row["name"], "status": "Active",
		                                        "backup_frequency": ["!=", "Off"]},
		                       fields=["name", "backup_frequency", "last_backup_at"])
		for site in sites:
			site["last_backup_at"] = to_utc(site.last_backup_at)
		intervals = {k: v for k, v in ou.BACKUP_FREQUENCIES.items() if v}
		ctx["stale_backups"] = hu.stale_backups(sites, now, intervals)
	except Exception:
		pass
	return ctx


def evaluate(server_name, facts, diagnose=None, diagnosed_at=None):
	"""Store health_status / health_issues for `facts`; returns (status, issues)."""
	row = frappe.db.get_value("Server", server_name, ["name", "status", "agent_version", "agent_channel",
	                                                  "health_diagnose", "health_diagnosed_at"], as_dict=True)
	now = utcnow()
	ctx = _context(row, now)
	if diagnose is None and row.health_diagnose:
		diagnose = frappe.parse_json(row.health_diagnose)
		diagnosed_at = to_utc(row.health_diagnosed_at)
	ctx.update(diagnose=diagnose or {}, diagnosed_at=diagnosed_at)
	issues = hu.evaluate_health(facts, get_settings().thresholds, ctx)
	status = hu.health_status(issues, row.status, bool(facts))
	frappe.db.set_value("Server", server_name, {
		"health_status": status, "health_issues": frappe.as_json(issues), "health_checked_at": now_datetime(),
	}, update_modified=False)
	return status, issues


def on_heartbeat(server_name, facts):
	"""Called by api.agent.heartbeat (never raises)."""
	if not isinstance(facts, dict):
		return
	try:
		values = hu.os_fields(facts.get("os_updates"))
		for key in ("os_boot_time", "os_updates_checked_at"):
			if key in values:
				values[key] = to_system(values[key])
		status, started = frappe.db.get_value("Server", server_name, ["os_update_status", "os_update_started_at"])
		new = hu.heartbeat_os_transition(status, started, values.get("os_boot_time"))
		if new:
			values.update(os_update_status=new, last_os_update_at=now_datetime())
		frappe.db.set_value("Server", server_name, values, update_modified=False)
	except Exception:
		frappe.log_error(title=f"Nexaploy: OS update ingest failed for {server_name}")
	try:
		_status, issues = evaluate(server_name, facts)
		_maybe_auto_cleanup(server_name, issues)
	except Exception:
		frappe.log_error(title=f"Nexaploy: health evaluation failed for {server_name}")


def _maybe_auto_cleanup(server_name, issues):
	s = get_settings()
	if not s.auto_on_critical:
		return
	row = frappe.db.get_value("Server", server_name, ["status", "auto_cleanup_enabled", "last_auto_cleanup_at"], as_dict=True)
	if row.status != "Active" or not cint(row.auto_cleanup_enabled):
		return
	if not hu.auto_cleanup_due(issues, to_utc(row.last_auto_cleanup_at), utcnow()):
		return
	if _active_job(server_name, ("server.cleanup",)):
		return
	frappe.db.set_value("Server", server_name, "last_auto_cleanup_at", now_datetime(), update_modified=False)
	start_cleanup(server_name, list(hu.SAFE_TARGETS), dry_run=False, check=False, origin="auto-disk")


# --------------------------------------------------------------------------- schedulers


def check_server_health():
	"""Every 5 min: Offline servers -> health Offline; stuck updates -> Failed; notify the team
	on transitions to Critical (at most every 6 h while it lasts) and back to OK."""
	from frappe.utils import get_url

	from nexaploy.notify import notify

	now = utcnow()
	rows = frappe.get_all("Server", filters={"status": ["in", ["Active", "Offline"]]},
	                      fields=["name", "hostname", "team", "status", "health_status", "health_issues",
	                              "health_alert_state", "os_update_status", "os_update_started_at"])
	for row in rows:
		values = {}
		status = row.health_status or hu.UNKNOWN
		if row.status == "Offline" and status != hu.OFFLINE:
			status = values["health_status"] = hu.OFFLINE
		if hu.reboot_timed_out(row.os_update_status, to_utc(row.os_update_started_at), now):
			values.update(os_update_status=hu.FAILED, os_update_error=_(
				"The server did not report back within 2 hours after the update / reboot."))
		state = frappe.parse_json(row.health_alert_state) if row.health_alert_state else {}
		event, new_state = hu.health_notification(state, status, now)
		if event:
			host = row.hostname or row.name
			issues = frappe.parse_json(row.health_issues) if row.health_issues else []
			if event == nu.SERVER_HEALTH_CRITICAL:
				lines = [f"- {i['title']}: {i.get('detail') or ''}" for i in issues if i.get("severity") == hu.CRITICAL]
				notify(row.team, nu.SERVER_HEALTH_CRITICAL, f"{host}: server health critical",
				       "\n".join(lines[:10]) or "Critical health issues", {"server": row.name, "hostname": host},
				       get_url(f"/dashboard/server?name={row.name}"))
			else:
				notify(row.team, nu.SERVER_HEALTH_OK, f"{host}: server health back to OK",
				       f"Server {row.name} ({host}) has no critical issues any more.",
				       {"server": row.name, "hostname": host}, get_url(f"/dashboard/server?name={row.name}"))
			values["health_alert_state"] = frappe.as_json(new_state)
		if values:
			frappe.db.set_value("Server", row.name, values, update_modified=False)
	frappe.db.commit()


def schedule_maintenance():
	"""Hourly: OS updates in each server's maintenance window (UTC, one run per window) and
	the platform's weekly automatic cleanup (servers with auto_cleanup_enabled)."""
	now = utcnow()
	s = get_settings()
	rows = frappe.get_all("Server", filters={"status": "Active"},
	                      fields=["name", "maintenance_enabled", "maintenance_day", "maintenance_hour", "maintenance_mode",
	                              "maintenance_reboot", "last_maintenance_at", "auto_cleanup_enabled",
	                              "last_scheduled_cleanup_at", "os_update_status"])
	for row in rows:
		if cint(row.maintenance_enabled) and hu.window_due(now, row.maintenance_day, row.maintenance_hour,
		                                                    to_utc(row.last_maintenance_at)):
			frappe.db.set_value("Server", row.name, "last_maintenance_at", now_datetime(), update_modified=False)
			try:
				start_os_update(row.name, row.maintenance_mode or "security", row.maintenance_reboot or "if_required",
				                check=False, origin="maintenance-window")
			except Exception:
				frappe.log_error(title=f"Nexaploy: maintenance window failed for {row.name}")
		if s.policy_enabled and cint(row.auto_cleanup_enabled) and hu.window_due(
			now, s.policy_day, s.policy_hour, to_utc(row.last_scheduled_cleanup_at)
		):
			frappe.db.set_value("Server", row.name, "last_scheduled_cleanup_at", now_datetime(), update_modified=False)
			try:
				start_cleanup(row.name, s.policy_targets or None, dry_run=False, check=False, origin="policy")
			except Exception:
				frappe.log_error(title=f"Nexaploy: scheduled cleanup failed for {row.name}")
		frappe.db.commit()


# --------------------------------------------------------------------------- bulk (platform admins)


def _require_platform_admin():
	from nexaploy.teams import is_platform_admin

	if not is_platform_admin():
		raise frappe.PermissionError(_("Only platform administrators can do this"))


def bulk_os_update(dry_run=False):
	"""Security updates without reboot on every Active server with pending updates."""
	_require_platform_admin()
	out = []
	for row in frappe.get_all("Server", filters={"status": "Active"},
	                          fields=["name", "hostname", "os_pending_updates", "os_update_status", "os_updates_supported"]):
		if not cint(row.os_updates_supported) or not cint(row.os_pending_updates) or row.os_update_status in hu.OS_BUSY:
			continue
		item = {"server": row.name, "hostname": row.hostname, "pending": row.os_pending_updates}
		if not cint(dry_run):
			try:
				item["job"] = start_os_update(row.name, "security", "never", check=False, origin="bulk")
			except Exception as exc:
				item["error"] = str(exc)
		out.append(item)
	return out


def bulk_cleanup(dry_run=False):
	_require_platform_admin()
	s = get_settings()
	out = []
	for row in frappe.get_all("Server", filters={"status": "Active"}, fields=["name", "hostname"]):
		item = {"server": row.name, "hostname": row.hostname}
		try:
			item["job"] = start_cleanup(row.name, s.policy_targets or None, dry_run=bool(cint(dry_run)), check=False,
			                            origin="bulk")
		except Exception as exc:
			item["error"] = str(exc)
		out.append(item)
	return out


def fleet_health():
	_require_platform_admin()
	rows = frappe.get_all("Server", filters={"status": ["!=", "Archived"]},
	                      fields=["name", "hostname", "team", "status", "health_status", "health_issues", "health_checked_at",
	                              "os_pending_updates", "os_security_updates", "os_reboot_required", "last_cleanup_at",
	                              "last_cleanup_freed"])
	for r in rows:
		r["issues"] = [i for i in (frappe.parse_json(r.pop("health_issues")) if r.get("health_issues") else [])
		               if i.get("severity") != hu.INFO]
	counts = {}
	for r in rows:
		key = r.health_status or hu.UNKNOWN
		counts[key] = counts.get(key, 0) + 1
	return {"servers": hu.worst_first(rows), "counts": counts}


# --------------------------------------------------------------------------- job callbacks


@handles("server.os_update", "server.os_reboot")
def on_os_job(job):
	if job.reference_doctype != "Server":
		return
	result = frappe.parse_json(job.result) if job.result else {}
	new = hu.os_job_transition(job.kind, job.status, result)
	if not new:
		return
	values = {"os_update_status": new}
	if new == hu.FAILED:
		values["os_update_error"] = (job.error or _("The agent job failed"))[-1000:]
	if new == hu.SUCCEEDED:
		values["last_os_update_at"] = now_datetime()
	os_status = (result or {}).get("os_updates")
	if isinstance(os_status, dict):
		fields = hu.os_fields(os_status)
		for key in ("os_boot_time", "os_updates_checked_at"):
			if key in fields:
				fields[key] = to_system(fields[key])
		values.update(fields)
	frappe.db.set_value("Server", job.reference_name, values, update_modified=False)


@handles("server.os_configure")
def on_os_configure(job):
	if job.status != "Success" or job.reference_doctype != "Server":
		return
	result = frappe.parse_json(job.result) if job.result else {}
	if isinstance(result.get("os_updates"), dict):
		fields = hu.os_fields(result["os_updates"])
		for key in ("os_boot_time", "os_updates_checked_at"):
			if key in fields:
				fields[key] = to_system(fields[key])
		frappe.db.set_value("Server", job.reference_name, fields, update_modified=False)


@handles("server.diagnose")
def on_diagnose(job):
	if job.status != "Success" or job.reference_doctype != "Server":
		return
	result = frappe.parse_json(job.result) if job.result else {}
	health = result.get("health") or {}
	frappe.db.set_value("Server", job.reference_name, {
		"health_diagnose": frappe.as_json(health), "health_diagnosed_at": now_datetime(),
	}, update_modified=False)
	facts = frappe.db.get_value("Server", job.reference_name, "facts")
	facts = frappe.parse_json(facts) if facts else {}
	facts = {**facts, **{k: v for k, v in result.items() if k != "health"}, "health": {**(facts.get("health") or {}), **health}}
	evaluate(job.reference_name, facts, health, utcnow())


@handles("server.cleanup")
def on_cleanup(job):
	if job.status != "Success" or job.reference_doctype != "Server":
		return
	result = frappe.parse_json(job.result) if job.result else {}
	summary = hu.cleanup_summary(result)
	if summary["dry_run"]:
		return
	frappe.db.set_value("Server", job.reference_name, {
		"last_cleanup_at": now_datetime(), "last_cleanup_freed": summary["freed_bytes"], "last_cleanup_job": job.name,
	}, update_modified=False)
