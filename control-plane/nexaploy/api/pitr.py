"""Point-in-time recovery for the customer portal (ARCHITECTURE.md §14).

Access goes through nexaploy.teams.check_team_access on the site's team (§9): read ->
get_pitr_window, write (Owner/Admin/Developer) -> restore_to_point. Times cross the API as
UTC ISO strings (`2026-11-04T15:15:00Z`); the portal shows them in the browser's time zone.

    get_pitr_window(site)
        -> {available, start, end, reason, backups, retention_days, margin_minutes, copy_name, can_write}
    restore_to_point(site, target_time, mode="copy"|"in_place", confirm=None)
        -> {name, target_site, mode, target_time, backup, job}

`copy` (default) restores into a NEW site `<first label>-pitr-<YYYYmmddHHMM>.<root domain>` on the
same bench (emails muted, scheduler paused) so the customer can inspect it before switching;
`in_place` overwrites the site itself and needs `confirm` = the site name (the agent takes a
safety backup first).
"""

import frappe
from frappe import _

from nexaploy import pitr_utils as pu

SITE_FIELDS = ("name", "site_name", "team", "bench", "server", "status", "plan", "is_staging")
BACKUP_FIELDS = ["name", "status", "remote_path", "created_at", "binlog_file", "binlog_position",
                 "binlog_ready_at", "binlog_server"]


def _site(site, action):
	user = frappe.session.user
	if not user or user == "Guest":
		raise frappe.PermissionError(_("Please log in"))
	if not site:
		frappe.throw(_("Site is required"))
	row = frappe.db.get_value("Site", site, list(SITE_FIELDS), as_dict=True)
	if not row:
		raise frappe.DoesNotExistError(_("Site {0} not found").format(site))
	from nexaploy.teams import check_team_access

	row.role = check_team_access(row.team, action)
	return row


def _backups(site):
	return frappe.get_all(
		"Site Backup", filters={"site": site, "status": "Success", "binlog_file": ["is", "set"]},
		fields=BACKUP_FIELDS, order_by="creation desc", limit_page_length=500,
	)


def _window(row, now):
	return pu.pitr_window(_backups(row.name), now, server=row.server)


def _root_domain():
	from nexaploy.api.dashboard import _root_domain as root

	return root()


def _log(team, action, site, details):
	try:
		from nexaploy.activity import log_activity

		log_activity(team, action, "Site", site, details)
	except Exception:
		pass


@frappe.whitelist()
def get_pitr_window(site):
	"""The restorable window of a site, for the portal's datetime picker."""
	row = _site(site, "read")
	from nexaploy.team_utils import role_allows

	now = pu.utcnow()
	w = _window(row, now)
	try:
		copy_name = pu.pitr_site_name(row.site_name, w["end"], _root_domain())
	except ValueError:
		copy_name = None
	return {
		"site": row.name,
		"available": w["available"],
		"start": pu.iso_z(w["start"]),
		"end": pu.iso_z(w["end"]),
		"reason": _(w["reason"]) if w["reason"] else None,
		"backups": w["backups"],
		"retention_days": pu.RETENTION_DAYS,
		"margin_minutes": pu.MARGIN_MINUTES,
		"copy_name": copy_name,
		"can_write": role_allows(row.role, "write"),
	}


@frappe.whitelist(methods=["POST"])
def restore_to_point(site, target_time, mode="copy", confirm=None):
	"""Restore the site as it was at `target_time` (UTC ISO): into a new copy (default) or,
	with mode="in_place" and confirm=<site name>, over the site itself."""
	row = _site(site, "write")
	if mode not in ("copy", "in_place"):
		frappe.throw(_("Unknown restore mode {0}").format(mode))
	in_place = mode == "in_place"
	if in_place and (confirm or "").strip().lower() != row.site_name:
		frappe.throw(_("Type the site name to confirm that its current data will be replaced"))
	allowed = ("Active", "Broken") if in_place else ("Active", "Broken", "Suspended")
	if row.status not in allowed:
		frappe.throw(_("Site {0} is {1}; it must be Active").format(row.name, _(row.status)))
	if frappe.db.get_value("Server", row.server, "status") != "Active":
		frappe.throw(_("The server of this site is not active"))
	if frappe.db.exists("Agent Job", {"kind": "site.restore_pitr", "reference_doctype": "Site",
	                                  "reference_name": row.name, "status": ["in", ["Pending", "Queued", "Running"]]}):
		frappe.throw(_("A point-in-time restore of this site is already running"))
	try:
		target = pu.parse_target(target_time)
	except ValueError as exc:
		frappe.throw(_(str(exc)))
	now = pu.utcnow()
	backups = _backups(row.name)
	window = pu.pitr_window(backups, now, server=row.server)
	err = pu.target_error(target, window)
	if err:
		frappe.throw(_(err[0]).format(*err[1]))
	backup = pu.pick_base_backup(backups, target, now, server=row.server)
	if not backup:
		frappe.throw(_("No backup can be used for this time"))
	bench_name = frappe.db.get_value("Bench", row.bench, "bench_name")

	from nexaploy.agent_job import create_agent_job

	if in_place:
		try:
			params = pu.restore_params(row.site_name, bench_name, backup.remote_path, target, in_place=True)
		except ValueError as exc:
			frappe.throw(_(str(exc)))
		job = create_agent_job(row.server, "site.restore_pitr", params, "Site", row.name)
		name, target_site = row.name, row.site_name
	else:
		try:
			target_site = pu.pitr_site_name(row.site_name, target, _root_domain())
			params = pu.restore_params(row.site_name, bench_name, backup.remote_path, target, target_site)
		except ValueError as exc:
			frappe.throw(_(str(exc)))
		if frappe.db.exists("Site", target_site) or frappe.db.exists(
			"Site Domain", {"domain": target_site, "parenttype": "Site"}
		):
			frappe.throw(_("{0} is already taken").format(target_site))
		doc = _create_copy(row, target_site, params)
		name = doc.name
		job = frappe.db.get_value(
			"Agent Job", {"reference_doctype": "Site", "reference_name": doc.name, "kind": "site.restore_pitr"},
			"name", order_by="creation desc",
		)
		job = frappe._dict(name=job)
	_log(row.team, "site.restore_pitr", row.name, {
		"target_time": pu.iso_z(target), "mode": mode, "target_site": target_site, "backup": backup.name,
		"job": job.name,
	})
	return {"name": name, "target_site": target_site, "mode": mode, "target_time": pu.iso_z(target),
	        "backup": backup.name, "job": job.name}


def _create_copy(row, target_site, params):
	"""A new Site document for the restored copy; its after_insert queues site.restore_pitr
	(flags.pitr_params) instead of site.create. Billed like any site until it is dropped."""
	from nexaploy.portal_utils import generate_password

	source = frappe.get_doc("Site", row.name)
	doc = frappe.get_doc({
		"doctype": "Site",
		"site_name": target_site,
		"bench": row.bench,
		"server": row.server,
		"team": row.team,
		"plan": row.plan,
		"pitr_of": row.name,
		"backup_frequency": "Off",
		# replaced by the restored database
		"admin_password": generate_password(),
		"apps": [{"app": r.app} for r in source.apps if r.status == "Installed"],
	})
	doc.flags.pitr_params = params
	doc.insert(ignore_permissions=True)
	return doc
