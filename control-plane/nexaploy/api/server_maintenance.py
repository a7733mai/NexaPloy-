"""Server maintenance & health: portal and admin API (logic in nexaploy/server_maintenance.py).

Per server (team members read; actions: platform admins, or team Owner/Admin on their own
BYOS servers when Nexaploy Settings.allow_customer_os_updates is on):

    get_maintenance(server)                       update_os(server, mode, reboot)
    reboot_server(server, delay_seconds)          set_unattended(server, enabled)
    save_maintenance(server, values)              run_diagnose(server)
    cleanup(server, targets, dry_run)             get_job(server, job)

Platform admins: fleet_health(), bulk_os_update(dry_run), bulk_cleanup(dry_run),
get_policy(), save_policy(values).
"""

import json

import frappe
from frappe import _
from frappe.utils import cint

from nexaploy import server_health_utils as hu
from nexaploy import server_maintenance as sm

POLICY_FIELDS = {
	"allow_customer_os_updates": "check",
	"health_disk_warning_percent": "float",
	"health_disk_critical_percent": "float",
	"health_inode_warning_percent": "float",
	"health_memory_warning_percent": "float",
	"health_load_per_cpu": "float",
	"auto_cleanup_on_disk_critical": "check",
	"auto_cleanup_policy_enabled": "check",
	"auto_cleanup_day": "day",
	"auto_cleanup_hour": "hour",
	"auto_cleanup_targets": "targets",
	"cleanup_log_retention_days": "int",
	"cleanup_backup_local_keep_days": "int",
}


def _check_read(server):
	from nexaploy.teams import check_team_access, is_platform_admin

	if not frappe.db.exists("Server", server):
		raise frappe.DoesNotExistError(_("Server {0} not found").format(server))
	if is_platform_admin():
		return
	team = frappe.db.get_value("Server", server, "team")
	if not team:
		raise frappe.PermissionError(_("Not permitted"))
	check_team_access(team, "read")


def _admin():
	from nexaploy.teams import is_platform_admin

	if not is_platform_admin():
		raise frappe.PermissionError(_("Only platform administrators can do this"))


@frappe.whitelist()
def get_maintenance(server):
	_check_read(server)
	return sm.server_state(server)


@frappe.whitelist(methods=["POST"])
def update_os(server, mode="all", reboot="if_required"):
	_check_read(server)
	return {"job": sm.start_os_update(server, mode, reboot)}


@frappe.whitelist(methods=["POST"])
def reboot_server(server, delay_seconds=60):
	_check_read(server)
	return {"job": sm.start_reboot(server, delay_seconds)}


@frappe.whitelist(methods=["POST"])
def set_unattended(server, enabled):
	_check_read(server)
	return {"job": sm.set_unattended(server, enabled)}


@frappe.whitelist(methods=["POST"])
def save_maintenance(server, values):
	_check_read(server)
	return sm.save_maintenance(server, values)


@frappe.whitelist(methods=["POST"])
def run_diagnose(server):
	_check_read(server)
	return {"job": sm.start_diagnose(server)}


@frappe.whitelist(methods=["POST"])
def cleanup(server, targets=None, dry_run=1):
	_check_read(server)
	if isinstance(targets, str) and targets.startswith("["):
		targets = json.loads(targets)
	return {"job": sm.start_cleanup(server, targets, dry_run=bool(cint(dry_run)))}


@frappe.whitelist()
def get_job(server, job):
	_check_read(server)
	return sm.cleanup_job(server, job)


# --------------------------------------------------------------------------- platform admins


@frappe.whitelist()
def fleet_health():
	_admin()
	return sm.fleet_health()


@frappe.whitelist(methods=["POST"])
def bulk_os_update(dry_run=0):
	_admin()
	rows = sm.bulk_os_update(dry_run=cint(dry_run))
	_platform_log("platform.bulk_os_update", {"servers": len(rows), "dry_run": cint(dry_run)})
	return {"servers": rows}


@frappe.whitelist(methods=["POST"])
def bulk_cleanup(dry_run=0):
	_admin()
	rows = sm.bulk_cleanup(dry_run=cint(dry_run))
	_platform_log("platform.bulk_cleanup", {"servers": len(rows), "dry_run": cint(dry_run)})
	return {"servers": rows}


@frappe.whitelist()
def get_policy():
	_admin()
	doc = frappe.get_single(sm.SETTINGS)
	values = {f: doc.get(f) for f in POLICY_FIELDS}
	return {"values": values, "targets": [{"target": t, "label": hu.TARGET_LABELS[t]} for t in hu.CLEANUP_TARGETS],
	        "days": list(hu.DAYS)}


@frappe.whitelist(methods=["POST"])
def save_policy(values):
	_admin()
	values = frappe.parse_json(values) if isinstance(values, str) else (values or {})
	clean = {}
	for field, kind in POLICY_FIELDS.items():
		if field not in values:
			continue
		v = values[field]
		if kind == "check":
			clean[field] = 1 if cint(v) else 0
		elif kind == "float":
			clean[field] = max(0.0, float(v or 0))
		elif kind == "int":
			clean[field] = max(0, cint(v))
		elif kind == "hour":
			if not 0 <= cint(v) <= 23:
				frappe.throw(_("Maintenance hour must be between 0 and 23 (UTC)"))
			clean[field] = cint(v)
		elif kind == "day":
			if v not in hu.DAYS:
				frappe.throw(_("Invalid maintenance day"))
			clean[field] = v
		elif kind == "targets":
			try:
				clean[field] = "\n".join(hu.clean_targets(v)) if (v or "").strip() else ""
			except ValueError as exc:
				frappe.throw(_(str(exc)))
	doc = frappe.get_single(sm.SETTINGS)
	doc.update(clean)
	doc.save(ignore_permissions=True)
	_platform_log("platform.maintenance_policy", clean)
	return get_policy()


def _platform_log(action, details):
	try:
		from nexaploy.activity import log_platform_activity

		log_platform_activity(action, sm.SETTINGS, sm.SETTINGS, details)
	except Exception:
		pass
