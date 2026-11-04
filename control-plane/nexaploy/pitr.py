"""Point-in-time recovery: scheduler + agent callbacks (ARCHITECTURE.md §11).

Pure logic lives in nexaploy.pitr_utils; the portal API in nexaploy.api.pitr.

    schedule_binlog_archiving()   # every 15 min: server.archive_binlogs per Active server
    backup_finished(backup, job)  # site.backup callback: binlog_* fields of the Site Backup
    archive_finished(job)         # server.archive_binlogs callback
    restore_finished(job)         # site.restore_pitr callback
"""

import frappe
from frappe import _

from nexaploy import pitr_utils as pu

ACTIVE_JOB_STATUSES = ["Pending", "Queued", "Running"]


def _json(value):
	if isinstance(value, (dict, list)):
		return value
	try:
		return frappe.parse_json(value) if value else {}
	except Exception:
		return {}


def schedule_binlog_archiving():
	"""Every 15 min: `server.archive_binlogs` (rotate, upload closed binlogs, prune the archive)
	for each Active server hosting an Active site, when backup storage is configured. A server
	with binary logging off answers `enabled: false` (no error)."""
	from nexaploy.agent_job import create_agent_job
	from nexaploy.nexaploy.doctype.site.site import Site

	if not Site.backup_storage_configured():
		return
	servers = frappe.get_all("Server", filters={"status": "Active"}, pluck="name")
	for server in servers:
		if not frappe.db.exists("Site", {"server": server, "status": "Active"}):
			continue
		if frappe.db.exists(
			"Agent Job",
			{"server": server, "kind": "server.archive_binlogs", "status": ["in", ACTIVE_JOB_STATUSES]},
		):
			continue
		try:
			create_agent_job(server, "server.archive_binlogs", pu.archive_params(), "Server", server)
		except Exception:
			frappe.log_error(title=f"Nexaploy: archive_binlogs failed for {server}")
	frappe.db.commit()


def backup_finished(backup, job):
	"""Copy the binlog position of a successful site.backup onto the Site Backup."""
	fields = pu.binlog_fields(_json(job.result))
	for key, value in fields.items():
		backup.set(key, value)
	if fields:
		backup.binlog_server = job.server


def archive_finished(job):
	if job.status == "Failure":
		frappe.log_error(title=f"Nexaploy: binlog archiving failed on {job.server}", message=job.error or "")


def restore_finished(job):
	"""site.restore_pitr references the restored Site: the new copy, or the site itself (in place)."""
	if job.status not in ("Success", "Failure"):
		return
	site = frappe.get_doc(job.reference_doctype, job.reference_name)
	params = _json(job.params) or {}
	in_place = bool(params.get("in_place"))
	if job.status == "Success":
		site.status = "Active"
		for row in site.apps:
			if row.status == "Pending":
				row.status = "Installed"
	else:
		steps = [{"step_idx": s.step_idx, "step_name": s.step_name, "status": s.status} for s in (job.get("steps") or [])]
		site.status = pu.failure_status(in_place, pu.failed_step(steps), site.status)
		if not in_place:
			for row in site.apps:
				if row.status == "Pending":
					row.status = "Failed"
		try:
			site.add_comment("Comment", _("Point-in-time restore to {0} failed: {1}").format(
				params.get("target_time"), job.error or ""))
		except Exception:
			pass
	site.flags.ignore_permissions = True
	site.save()
