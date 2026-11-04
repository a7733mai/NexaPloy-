"""Callbacks run by `job_update` when an Agent Job changes status.

Dispatch is keyed by job kind. Each handler receives the Agent Job doc and updates the
document it references (reference_doctype / reference_name). Handlers are idempotent: the
dispatcher only calls them on a status change, and terminal statuses are recorded with
`callback_done` so retries from the agent never apply a callback twice.
"""

import json

import frappe
import frappe.utils

from nexaploy import ops_utils
from nexaploy.utils import TERMINAL_JOB_STATUSES, extract_backup_info

HANDLERS = {}


def handles(*kinds):
	def wrap(fn):
		for kind in kinds:
			HANDLERS[kind] = fn
		return fn

	return wrap


def _json(value, default=None):
	if value in (None, ""):
		return default
	if isinstance(value, (dict, list)):
		return value
	try:
		return json.loads(value)
	except (TypeError, ValueError):
		return default


def dispatch_callback(job):
	"""Run the handler for job.kind. Errors are logged, never raised to the agent."""
	if job.status in TERMINAL_JOB_STATUSES:
		if job.callback_done:
			return
		frappe.db.set_value("Agent Job", job.name, "callback_done", 1, update_modified=False)
		job.callback_done = 1

	handler = HANDLERS.get(job.kind)
	if not handler or not job.reference_doctype or not job.reference_name:
		return
	if not frappe.db.exists(job.reference_doctype, job.reference_name):
		return
	try:
		handler(job)
	except Exception:
		frappe.log_error(title=f"Nexaploy callback failed: {job.kind} {job.name}")
		if job.status in TERMINAL_JOB_STATUSES:
			# allow retry_failed_callbacks() to run it again (e.g. after a deploy fixing a bug)
			attempts = (job.get("callback_attempts") or 0) + 1
			frappe.db.set_value(
				"Agent Job", job.name, {"callback_done": 0, "callback_attempts": attempts}, update_modified=False
			)


MAX_CALLBACK_ATTEMPTS = 5


def retry_failed_callbacks():
	"""Scheduler (every 5 min): re-run callbacks of finished jobs whose callback raised,
	at most MAX_CALLBACK_ATTEMPTS times, so a failed callback never leaves a document stuck
	(e.g. a Site in "Renaming" after the server-side rename succeeded)."""
	names = frappe.get_all(
		"Agent Job",
		# ("between" is date-only in Frappe filters)
		filters=[
			["status", "in", list(TERMINAL_JOB_STATUSES)],
			["callback_done", "=", 0],
			["callback_attempts", ">=", 1],
			["callback_attempts", "<", MAX_CALLBACK_ATTEMPTS],
		],
		pluck="name",
		limit=50,
	)
	for name in names:
		dispatch_callback(frappe.get_doc("Agent Job", name))
		frappe.db.commit()


def _ref(job):
	return frappe.get_doc(job.reference_doctype, job.reference_name)


def _job_log(job):
	parts = []
	for step in job.steps:
		parts.append(f"== [{step.step_idx}] {step.step_name or ''} ({step.status})")
		if step.output:
			parts.append(step.output)
	if job.error:
		parts.append(f"ERROR: {job.error}")
	return "\n".join(parts)


def _save(doc):
	doc.flags.ignore_permissions = True
	doc.flags.ignore_validate_update_after_submit = True
	doc.save()


def _sync_billing(doc):
	from nexaploy.ops import sync_billing

	sync_billing(doc)


# --------------------------------------------------------------------------- images


@handles("image.build")
def on_image_build(job):
	dc = _ref(job)
	if job.status in ("Queued", "Running"):
		dc.status = "Building"
	elif job.status == "Success":
		dc.status = "Success"
		result = _json(job.result, {}) or {}
		dc.image_tag = result.get("tag") or result.get("image") or dc.image_tag
		dc.build_log = _job_log(job)
	elif job.status == "Failure":
		dc.status = "Failure"
		dc.build_log = _job_log(job)
	_save(dc)

	# auto updates: roll Active benches of the group to the new image (inside update_window;
	# outside it nexaploy.tasks.apply_auto_updates picks them up later)
	if job.status == "Success" and frappe.db.get_value("Release Group", dc.release_group, "auto_update"):
		from nexaploy.ops import apply_auto_updates

		apply_auto_updates(dc.release_group)


# --------------------------------------------------------------------------- benches


@handles("bench.create", "bench.update", "bench.start", "bench.restart")
def on_bench_job(job):
	if job.status not in TERMINAL_JOB_STATUSES:
		return
	bench = _ref(job)
	if job.status == "Success":
		if job.kind == "bench.update":
			params = _json(job.params, {}) or {}
			bench.image_tag = params.get("image") or bench.image_tag
			dc = frappe.db.get_value(
				"Deploy Candidate", {"image_tag": bench.image_tag, "release_group": bench.release_group}, "name"
			)
			if dc:
				bench.deploy_candidate = dc
		bench.status = "Active"
	else:
		bench.status = "Broken"
	_save(bench)


@handles("bench.remove")
def on_bench_remove(job):
	if job.status == "Success":
		bench = _ref(job)
		bench.status = "Archived"
		_save(bench)


# --------------------------------------------------------------------------- sites


def _in_move(site, job):
	"""True when `job` is the current step of an in-progress site move, or the retry of the
	step a move failed at (the site is Broken and still carries its move state)."""
	if site.status not in ("Moving", "Broken") or not site.move_steps:
		return False
	steps = _json(site.move_steps, []) or []
	idx = site.move_step_idx or 0
	if not (idx < len(steps) and steps[idx] == job.kind):
		return False
	if site.status == "Broken":
		if job.status != "Success":
			return False  # a failed retry: leave it Broken with the move state for another retry
		site.db_set("status", "Moving")
	return True


@handles("site.create")
def on_site_create(job):
	if job.status not in TERMINAL_JOB_STATUSES:
		return
	site = _ref(job)
	if _in_move(site, job):
		return site.move_step_finished(job)
	ok = job.status == "Success"
	site.status = "Active" if ok else "Broken"
	for row in site.apps:
		if row.status == "Pending":
			row.status = "Installed" if ok else "Failed"
	_save(site)


@handles("site.restore")
def on_site_restore(job):
	if job.status not in TERMINAL_JOB_STATUSES:
		return
	site = _ref(job)
	if _in_move(site, job):
		return site.move_step_finished(job)
	site.status = "Active" if job.status == "Success" else "Broken"
	_save(site)


@handles("site.migrate")
def on_site_migrate(job):
	if job.status not in TERMINAL_JOB_STATUSES:
		return
	site = _ref(job)
	site.status = "Active" if job.status == "Success" else "Broken"
	_save(site)


@handles("site.drop")
def on_site_drop(job):
	if job.status not in TERMINAL_JOB_STATUSES:
		return
	site = _ref(job)
	if _in_move(site, job):
		return site.move_step_finished(job)
	if job.status == "Success":
		site.status = "Archived"
		for row in site.domains:
			row.status = "Removing"
		_save(site)
		_sync_billing(site)


@handles("site.install_app", "site.uninstall_app")
def on_site_app(job):
	if job.status not in TERMINAL_JOB_STATUSES:
		return
	site = _ref(job)
	app = (_json(job.params, {}) or {}).get("app")
	ok = job.status == "Success"
	if job.kind == "site.install_app":
		for row in site.apps:
			if row.app == app:
				row.status = "Installed" if ok else "Failed"
	elif ok:
		site.apps = [row for row in site.apps if row.app != app]
	_save(site)
	_sync_billing(site)


@handles("site.add_domain", "site.remove_domain")
def on_site_domain(job):
	if job.status not in TERMINAL_JOB_STATUSES:
		return
	site = _ref(job)
	domain = (_json(job.params, {}) or {}).get("domain")
	ok = job.status == "Success"
	if job.kind == "site.add_domain":
		for row in site.domains:
			if row.domain == domain:
				row.status = "Active" if ok else "Broken"
	elif ok:
		site.domains = [row for row in site.domains if row.domain != domain]
	else:
		for row in site.domains:
			if row.domain == domain:
				row.status = "Broken"
	_save(site)


# --------------------------------------------------------------------------- backups


@handles("site.backup")
def on_site_backup(job):
	backup = _ref(job)
	if job.status in ("Queued", "Running"):
		backup.status = "Running"
		_save(backup)
		return
	if job.status == "Success":
		info = extract_backup_info(_json(job.result, {}))
		backup.status = "Success"
		backup.remote_path = info["remote_path"] or backup.remote_path
		if info["size"] is not None:
			backup.size = info["size"]
		backup.created_at = info["created_at"] or job.ended_at or backup.created_at
		# per-file object keys (database / public / private / site_config) for downloads
		backup.set("files", [])
		for row in ops_utils.backup_files_from_result(_json(job.result, {})):
			backup.append("files", row)
		from nexaploy.pitr import backup_finished

		backup_finished(backup, job)  # binlog position (point-in-time recovery)
		# system-time clock (created_at from the agent is UTC); compared with now_datetime()
		frappe.db.set_value(
			"Site", backup.site, "last_backup_at", job.ended_at or frappe.utils.now_datetime(),
			update_modified=False,
		)
	else:
		backup.status = "Failure"
	_save(backup)

	# move_to_bench chain: the first step of a move is this backup
	site = frappe.get_doc("Site", backup.site)
	if site.status == "Moving" and site.move_backup == backup.name and _in_move(site, job):
		site.move_step_finished(job)


@handles("backup.prune_remote")
def on_prune_remote(job):
	"""Mark Site Backups whose remote dir was purged as Deleted."""
	if job.status != "Success":
		if job.status == "Failure":
			frappe.log_error(title=f"Nexaploy: remote backup prune failed for {job.reference_name}",
				message=job.error or "")
		return
	pruned = ops_utils.pruned_remote_paths(_json(job.result, {}))
	if not pruned:
		return
	for row in frappe.get_all(
		"Site Backup",
		filters={"site": job.reference_name, "status": "Success"},
		fields=["name", "remote_path"],
	):
		if ops_utils.backup_was_pruned(row.remote_path, pruned):
			frappe.db.set_value("Site Backup", row.name, "status", "Deleted")


# --------------------------------------------------------------------------- usage / site config


@handles("site.usage")
def on_site_usage(job):
	"""site.usage result.usage -> Site Usage rows (same as the heartbeat usage block)."""
	if job.status != "Success":
		return
	result = _json(job.result, {}) or {}
	from nexaploy.ops import ingest_usage

	ingest_usage(job.server, result.get("usage") if isinstance(result, dict) else None)


@handles("site.get_config")
def on_site_get_config(job):
	"""Nothing to store: the portal reads the (agent-redacted) result from the Agent Job."""


@handles("site.update_config")
def on_site_update_config(job):
	"""Scrub secret values from the stored params once the job is final; log failures."""
	if job.status not in TERMINAL_JOB_STATUSES:
		return
	from nexaploy.utils import redact_params

	params = _json(job.params, {}) or {}
	scrubbed = redact_params(params)
	if scrubbed != params:
		frappe.db.set_value("Agent Job", job.name, "params", frappe.as_json(scrubbed), update_modified=False)
	if job.status == "Failure":
		frappe.log_error(title=f"Nexaploy: site config update failed for {job.reference_name}",
			message=job.error or "")


# --------------------------------------------------------------------------- servers


@handles("server.configure_backups")
def on_configure_backups(job):
	if job.status == "Failure":
		frappe.log_error(title=f"Nexaploy: configure_backups failed on {job.server}", message=job.error or "")


@handles("server.health")
def on_server_health(job):
	if job.status != "Success":
		return
	facts = _json(job.result, {}) or {}
	if not isinstance(facts, dict):
		return
	frappe.db.set_value("Server", job.server, "facts", frappe.as_json(facts), update_modified=False)
	from nexaploy.ops import record_heartbeat_metrics

	record_heartbeat_metrics(job.server, facts)


# --------------------------------------------------------------------------- lifecycle (staging, rename)


@handles("site.clone")
def on_site_clone(job):
	"""site.clone references the staging Site: Active (apps Installed) or Broken."""
	if job.status not in TERMINAL_JOB_STATUSES:
		return
	_ref(job).clone_finished(job)


@handles("site.rename")
def on_site_rename(job):
	"""site.rename: rename the Site document (old name kept as a domain) or report failure."""
	if job.status not in TERMINAL_JOB_STATUSES:
		return
	_ref(job).rename_finished(job)


# --------------------------------------------------------------------------- point-in-time recovery (§11)


@handles("site.restore_pitr")
def on_site_restore_pitr(job):
	if job.status in TERMINAL_JOB_STATUSES:
		from nexaploy.pitr import restore_finished

		restore_finished(job)


@handles("server.archive_binlogs")
def on_archive_binlogs(job):
	from nexaploy.pitr import archive_finished

	archive_finished(job)
