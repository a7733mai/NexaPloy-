"""Verification of Marketplace App Releases by a test build.

A release added or changed by a publisher (nexaploy.api.publisher.save_release, or the
releases generated when a listing is approved) becomes "Pending verification" and an
`image.build` job is enqueued on Nexaploy Settings.default_build_server with a minimal
apps.json: the app's required apps at their Verified release for the major (dependency
order) + the app at the release branch; frappe comes from `frappe_branch` (version-<major>).
The image is never pushed. The job references the Marketplace App; the image.build callback
(nexaploy.agent_callbacks.on_image_build) hands it to `on_verification_job`, which marks the
release Verified or Failed. Customers only see Verified releases (marketplace_utils.release_visible).

Notes:
- The agent's apps.json has no commit pinning: a release's `commit` is recorded but the test
  build (like customer builds) uses the branch head.
- Without a default build server, releases stay Pending until a platform admin verifies them
  by hand (desk, or nexaploy.api.publisher.set_release_status).
"""

import frappe
import frappe.utils

from nexaploy import marketplace_utils as mu
from nexaploy.utils import frappe_branch_for, make_build_id

NO_BUILD_SERVER = "No build server is configured; a platform administrator will verify this release."


def snapshot(doc):
	"""{major: {branch, commit, status, verified_on, verification_job, verification_note}}."""
	return {
		str(r.frappe_major): {
			"branch": r.branch,
			"commit": r.commit,
			"status": r.status,
			"verified_on": r.verified_on,
			"verification_job": r.verification_job,
			"verification_note": r.verification_note,
		}
		for r in doc.releases
	}


def mark_changed(doc, before):
	"""After rows were (re)generated: rows identical to a previous one keep its verification
	state, every other row becomes Pending verification. Returns the pending majors."""
	pending = []
	for row in doc.releases:
		prev = before.get(str(row.frappe_major))
		if prev and not mu.release_changed(prev, row.as_dict()):
			row.status = prev["status"] or mu.VERIFIED
			row.verified_on = prev["verified_on"]
			row.verification_job = prev["verification_job"]
			row.verification_note = prev["verification_note"]
			continue
		set_pending(row)
		pending.append(str(row.frappe_major))
	return pending


def set_pending(row, note=None):
	row.status = mu.PENDING
	row.verified_on = None
	row.verification_job = None
	row.verification_note = note


def start_verification(doc, majors=None):
	"""Enqueue a test build for each Pending release (or only `majors`). Saves `doc`.
	Returns {major: agent_job or None}."""
	from nexaploy import ops
	from nexaploy.agent_job import create_agent_job
	from nexaploy.api.marketplace import load_catalog

	majors = {str(m) for m in majors} if majors else None
	server = ops.default_build_server()
	server_ok = bool(server) and frappe.db.get_value("Server", server, "status") not in (None, "Archived")
	catalog = load_catalog()
	app = {"app_name": doc.app_name, "repo_url": doc.repo_url, "required_apps": doc.required_app_list()}
	started = {}
	for row in doc.releases:
		major = str(row.frappe_major)
		if row.status != mu.PENDING or (majors and major not in majors):
			continue
		started[major] = None
		if not server_ok:
			row.verification_note = NO_BUILD_SERVER
			continue
		try:
			apps_json = mu.verification_apps_json(app, row.branch, catalog, major)
		except mu.RequirementError as exc:
			row.status = mu.FAILED
			row.verification_note = str(exc)
			continue
		tag = mu.verification_tag(doc.app_name, major, make_build_id(frappe.utils.now_datetime()))
		job = create_agent_job(
			server,
			"image.build",
			{"major": int(major), "apps_json": apps_json, "tag": tag, "frappe_branch": frappe_branch_for(major)},
			reference_doctype=doc.doctype,
			reference_name=doc.name,
		)
		row.verification_job = job.name
		row.verification_note = None
		started[major] = job.name
	doc.flags.ignore_permissions = True
	doc.save()
	return started


def on_verification_job(job):
	"""image.build callback for a Marketplace App test build."""
	if job.status not in ("Success", "Failure"):
		return
	doc = frappe.get_doc("Marketplace App", job.reference_name)
	row = next((r for r in doc.releases if r.verification_job == job.name), None)
	if row is None or row.status != mu.PENDING:
		return  # release changed or was overridden meanwhile
	if job.status == "Success":
		row.status = mu.VERIFIED
		row.verified_on = frappe.utils.now_datetime()
		row.verification_note = None
	else:
		row.status = mu.FAILED
		row.verification_note = ((job.error or "").strip() or "Test build failed; see the build log.")[:1000]
	doc.flags.ignore_permissions = True
	doc.save()
	team = frappe.db.get_value("Marketplace Publisher", doc.publisher_profile, "team") if doc.publisher_profile else None
	if team:
		try:
			from nexaploy.activity import log_activity

			log_activity(
				team, "marketplace.release_verification", doc.doctype, doc.name,
				{"frappe_major": row.frappe_major, "branch": row.branch, "status": row.status, "job": job.name},
			)
		except Exception:
			pass


def job_log(job_name):
	"""Redacted build log of a verification job: {status, error, steps:[{name, status, output}]}."""
	if not job_name or not frappe.db.exists("Agent Job", job_name):
		return None
	from nexaploy.github_utils import redact_text

	job = frappe.get_doc("Agent Job", job_name)
	return {
		"job": job.name,
		"status": job.status,
		"error": redact_text(job.error),
		"steps": [
			{"idx": s.step_idx, "name": s.step_name, "status": s.status, "output": redact_text(s.output)}
			for s in sorted(job.steps, key=lambda s: (s.step_idx or 0, s.idx))
		],
	}
