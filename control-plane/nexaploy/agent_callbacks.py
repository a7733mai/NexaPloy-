"""Callbacks run by `job_update` when an Agent Job changes status.

Dispatch is keyed by job kind. Each handler receives the Agent Job doc and updates the
document it references (reference_doctype / reference_name). Handlers are idempotent: the
dispatcher only calls them on a status change, and terminal statuses are recorded with
`callback_done` so retries from the agent never apply a callback twice.
"""

import json

import frappe

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
	"""True when `job` is the current step of an in-progress site move."""
	if site.status != "Moving":
		return False
	steps = _json(site.move_steps, []) or []
	idx = site.move_step_idx or 0
	return idx < len(steps) and steps[idx] == job.kind


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
	else:
		backup.status = "Failure"
	_save(backup)

	# move_to_bench chain: the first step of a move is this backup
	site = frappe.get_doc("Site", backup.site)
	if site.status == "Moving" and site.move_backup == backup.name and _in_move(site, job):
		site.move_step_finished(job)
