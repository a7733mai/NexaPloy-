""""Safe update": test an update on a copy of a site before applying it (Site Update Test).

Pure logic and the status flow: nexaploy.update_test_utils. Portal API:
nexaploy.api.update_test. The chain is driven by agent job callbacks (nexaploy.agent_callbacks
calls on_agent_job for bench.create / site.clone / site.smoke_check) plus sync_update_tests
(scheduler, every 5 minutes, and on every read) for the apply step, which reuses the site move:

    start_test(site, target_bench=None, deploy_candidate=None, mode="Manual", auto_apply=False)
        target: the given bench (same server, same or higher Frappe major), else an Active bench
        of the site's release group on the site's server already running the newest successful
        Deploy Candidate, else a new Bench for it (bench.create; team-owned groups only)
      -> site.clone {bench: production bench, site, target_site: uptest-<site>, target_bench}
         (backup, restore on the new image, scrub emails/scheduler, migrate)
      -> site.smoke_check on the copy -> Passed | Failed (production untouched either way)
    apply_test(test)       Site.move_to_bench(target_bench, allow_upgrade=True): fresh backup first
    drop_test_site(test)   site.drop {skip_backup} of the copy (+ the bench created for the test
                           once it is empty and production did not move there)
"""

import frappe
from frappe import _
from frappe.utils import now_datetime

from nexaploy import update_test_utils as ut
from nexaploy.agent_job import create_agent_job

TEST = "Site Update Test"


# --------------------------------------------------------------------------- helpers


def _log(team, action, site, details=None):
	try:
		from nexaploy.activity import log_activity

		log_activity(team, action, "Site", site, details)
	except Exception:
		pass


def _set(doc, **values):
	doc.update(values)
	doc.flags.ignore_permissions = True
	doc.flags.ignore_links = True
	doc.save()
	return doc


def _job_steps(job):
	return [
		{"step_idx": s.step_idx, "step_name": s.step_name, "status": s.status, "output": s.output}
		for s in (job.get("steps") or [])
	]


def _job_log(job):
	parts = []
	for s in job.get("steps") or []:
		parts.append(f"-- [{s.step_idx}] {s.step_name or ''} ({s.status})")
		if s.output:
			parts.append(s.output.rstrip())
	if job.get("error"):
		parts.append(f"ERROR: {job.error}")
	return "\n".join(parts)


def _fail(doc, stage, message, detail=None, log_title=None, log_text=None, **extra):
	values = {
		"status": "Failed",
		"failed_stage": stage,
		"result_summary": message,
		"error": detail or message,
		"finished_at": now_datetime(),
		**extra,
	}
	if log_title:
		values["log"] = ut.append_log(doc.log, log_title, log_text)
	_set(doc, **values)
	_log(doc.team, "site.update_test_failed", doc.site, {"test": doc.name, "stage": stage})
	return doc


def active_test(site):
	return frappe.db.get_value(TEST, {"site": site, "status": ["in", list(ut.IN_PROGRESS)]}, "name")


def _major(bench):
	group = frappe.db.get_value("Bench", bench, "release_group")
	return frappe.db.get_value("Release Group", group, "frappe_major") if group else None


def _site_apps(site):
	return [r.app for r in frappe.get_all(
		"Site App", filters={"parenttype": "Site", "parent": site, "status": "Installed"}, fields=["app"]
	)]


def latest_build(release_group):
	"""(deploy_candidate, image_tag) of the newest successful build of the group."""
	return frappe.get_doc("Release Group", release_group).latest_image()


def _check_target_owner(site_row, target):
	from nexaploy import lifecycle_utils as lu
	from nexaploy.teams import is_platform_admin

	if is_platform_admin():
		return
	server = frappe.db.get_value("Server", target.server, ["team", "ownership"], as_dict=True) or frappe._dict()
	if not (
		target.team == site_row.team
		or (server.team and server.team == site_row.team)
		or lu.is_shared_hosted(target.team, server.team, server.ownership)
	):
		raise frappe.PermissionError(_("Target bench belongs to another team"))


# --------------------------------------------------------------------------- start


def start_test(site, target_bench=None, deploy_candidate=None, mode="Manual", auto_apply=False, user=None):
	"""Create a Site Update Test and start it. The caller checked team `write` on the site."""
	from nexaploy import lifecycle_utils as lu
	from nexaploy.teams import is_platform_admin

	row = frappe.db.get_value(
		"Site", site, ["name", "site_name", "team", "bench", "server", "status", "is_staging"], as_dict=True
	)
	if not row:
		raise frappe.DoesNotExistError(_("Site {0} not found").format(site))
	if row.is_staging:
		frappe.throw(_("Test the update on the production site, not on a copy"))
	if row.status != "Active":
		frappe.throw(_("Site {0} is {1}; it must be Active").format(row.name, _(row.status)))
	running = active_test(row.name)
	if running:
		frappe.throw(_("An update test is already running for {0}: {1}").format(row.name, running))
	source = frappe.db.get_value("Bench", row.bench, ["name", "bench_name", "release_group", "image_tag", "server"], as_dict=True)
	if not source:
		frappe.throw(_("Site {0} has no bench").format(row.name))
	try:
		ut.copy_site_name(row.site_name)
	except ValueError as exc:
		frappe.throw(_(str(exc)))

	values = {
		"doctype": TEST,
		"site": row.name,
		"team": row.team,
		"mode": mode if mode in ("Manual", "Auto") else "Manual",
		"auto_apply": int(bool(auto_apply)),
		"requested_by": user or frappe.session.user,
		"server": row.server,
		"release_group": source.release_group,
		"source_bench": source.name,
		"source_image": source.image_tag,
		"started_at": now_datetime(),
		"status": "Pending",
	}
	created = False
	if target_bench:
		target = frappe.db.get_value(
			"Bench", target_bench, ["name", "bench_name", "status", "server", "team", "release_group", "image_tag", "deploy_candidate"],
			as_dict=True,
		)
		try:
			ut.validate_target(row.server, row.bench, target)
		except ValueError as exc:
			frappe.throw(_(str(exc)))
		_check_target_owner(row, target)
		compat = lu.move_compatibility(_major(row.bench), _major(target.name), allow_upgrade=True)
		if not compat["ok"]:
			frappe.throw(_(compat["reason"]))
		from nexaploy.nexaploy.doctype.site.site import Site

		Site.validate_apps_in_release_group(target.name, _site_apps(row.name))
		values.update(
			target_bench=target.name, target_image=target.image_tag, deploy_candidate=target.deploy_candidate,
			upgrade=int(bool(compat["upgrade"])),
		)
	else:
		dc_name, image = (deploy_candidate, frappe.db.get_value("Deploy Candidate", deploy_candidate, "image_tag")) \
			if deploy_candidate else latest_build(source.release_group)
		if not image:
			frappe.throw(_("Release group {0} has no successful build to test").format(source.release_group))
		if not ut.update_available(source.image_tag, image):
			frappe.throw(_("The site already runs the newest build of {0}").format(source.release_group))
		benches = frappe.get_all(
			"Bench",
			filters={"release_group": source.release_group, "server": row.server, "status": "Active"},
			fields=["name", "status", "server", "release_group", "image_tag"],
			order_by="creation desc",
		)
		target_name = ut.pick_target_bench(benches, row.server, source.release_group, image, exclude=row.bench)
		if not target_name:
			group_team = frappe.db.get_value("Release Group", source.release_group, "team")
			if group_team != row.team and not is_platform_admin():
				frappe.throw(
					_("No bench with the new version exists on this server yet. Ask support to prepare one, or wait for the next platform update.")
				)
			target_name = _create_bench(source, row.server, dc_name)
			created = True
		values.update(target_bench=target_name, target_image=image, deploy_candidate=dc_name)
	values["created_bench"] = int(created)
	values["test_site"] = None
	doc = frappe.get_doc(values)
	doc.flags.ignore_links = True
	doc.insert(ignore_permissions=True)
	_log(row.team, "site.update_test", row.name, {"test": doc.name, "target_bench": doc.target_bench,
		"target_image": doc.target_image, "mode": doc.mode})
	if frappe.db.get_value("Bench", doc.target_bench, "status") == "Active":
		_start_clone(doc)
	else:
		_set(doc, status="Preparing Bench", log=ut.append_log(doc.log, "bench", f"creating bench {doc.target_bench} with {doc.target_image}"))
	return doc


def _create_bench(source, server, deploy_candidate):
	existing = frappe.get_all("Bench", pluck="bench_name")
	name = ut.new_bench_name(source.release_group, existing, frappe.generate_hash(length=5))
	bench = frappe.get_doc(
		{
			"doctype": "Bench",
			"bench_name": name,
			"server": server,
			"release_group": source.release_group,
			"deploy_candidate": deploy_candidate,
		}
	)
	bench.insert(ignore_permissions=True)  # enqueues bench.create
	return bench.name


# --------------------------------------------------------------------------- clone


def _start_clone(doc):
	"""Create (or reuse) the copy uptest-<site> on the target bench and clone production into it."""
	from nexaploy.portal_utils import generate_password

	production = frappe.get_doc("Site", doc.site)
	if production.status != "Active" or production.bench != doc.source_bench:
		return _fail(doc, "clone", _("The site changed (status {0}, bench {1}) before the copy was made").format(
			production.status, production.bench))
	name = ut.copy_site_name(production.site_name)
	apps = [r.app for r in production.apps if r.status == "Installed"]
	existing = frappe.db.get_value("Site", name, ["name", "is_staging", "staging_of", "status", "bench"], as_dict=True)
	if existing:
		if not (existing.is_staging and existing.staging_of == production.name):
			return _fail(doc, "clone", _("{0} is already taken").format(name))
		if existing.status not in ("Archived",) and existing.bench != doc.target_bench:
			return _fail(doc, "clone", _("An earlier test copy {0} still exists on bench {1}; delete it first").format(
				name, existing.bench))
		if existing.status in ("Pending", "Moving", "Renaming"):
			return _fail(doc, "clone", _("The earlier test copy {0} is busy ({1})").format(name, existing.status))
		site = frappe.get_doc("Site", name)
		site.bench, site.server, site.team, site.status = doc.target_bench, production.server, production.team, "Pending"
		site.set("apps", [{"app": a, "status": "Pending"} for a in apps])
		site.set("domains", [])
		site.flags.ignore_permissions = True
		site.save()
		job = site.enqueue_clone(production.name)
	else:
		if frappe.db.exists("Site Domain", {"domain": name, "parenttype": "Site"}):
			return _fail(doc, "clone", _("{0} is already taken").format(name))
		site = frappe.get_doc(
			{
				"doctype": "Site",
				"site_name": name,
				"bench": doc.target_bench,
				"server": production.server,
				"team": production.team,
				"plan": None,
				"is_staging": 1,
				"staging_of": production.name,
				"backup_frequency": "Off",
				"admin_password": generate_password(),
				"apps": [{"app": a} for a in apps],
			}
		)
		site.flags.clone_from = production.name  # after_insert -> enqueue_clone (target_bench)
		site.insert(ignore_permissions=True)
		job = frappe.db.get_value(
			"Agent Job", {"reference_doctype": "Site", "reference_name": site.name, "kind": "site.clone"}, "name",
			order_by="creation desc",
		)
	return _set(
		doc, status="Cloning", test_site=site.name, clone_job=job, test_site_dropped=0,
		log=ut.append_log(doc.log, "clone", f"cloning {production.name} -> {site.name} on bench {doc.target_bench} ({doc.target_image})"),
	)


# --------------------------------------------------------------------------- callbacks


def on_agent_job(job):
	"""nexaploy.agent_callbacks hook (terminal jobs): advance the tests waiting for `job`."""
	if job.status not in ("Success", "Failure"):
		return
	if job.kind == "bench.create" and job.reference_doctype == "Bench":
		for name in frappe.get_all(TEST, filters={"status": "Preparing Bench", "target_bench": job.reference_name}, pluck="name"):
			_on_bench_ready(frappe.get_doc(TEST, name), job)
	elif job.kind == "site.clone" and job.reference_doctype == "Site":
		for name in frappe.get_all(TEST, filters={"status": "Cloning", "clone_job": job.name}, pluck="name"):
			_on_clone(frappe.get_doc(TEST, name), job)
	elif job.kind == "site.smoke_check" and job.reference_doctype == TEST:
		doc = frappe.get_doc(TEST, job.reference_name)
		if doc.status == "Testing" and doc.smoke_job == job.name:
			_on_smoke(doc, job)
	elif job.kind == "bench.update" and job.reference_doctype == "Bench" and job.status == "Success":
		finish_auto_update(job.reference_name)
	elif job.kind == "site.drop" and job.reference_doctype == "Site":
		for name in frappe.get_all(TEST, filters={"test_site": job.reference_name, "cleanup_requested": 1, "bench_removed": 0}, pluck="name"):
			_cleanup_bench(frappe.get_doc(TEST, name))


def _on_bench_ready(doc, job):
	if job.status != "Success":
		return _fail(doc, "bench", _("Creating bench {0} with the new image failed").format(doc.target_bench),
			job.error, "bench.create", _job_log(job))
	doc.log = ut.append_log(doc.log, "bench.create", _job_log(job))
	_safe_start_clone(doc)


def _safe_start_clone(doc):
	"""_start_clone from a callback / the scheduler: an error fails the test instead of leaving
	it waiting forever."""
	frappe.db.savepoint("nexaploy_update_test")
	try:
		return _start_clone(doc)
	except Exception as exc:
		frappe.db.rollback(save_point="nexaploy_update_test")
		doc.reload()
		return _fail(doc, "clone", _("Creating the test copy failed"), str(exc)[:2000])


def _on_clone(doc, job):
	if job.status != "Success":
		failure = ut.clone_failure(_job_steps(job), job.error)
		return _fail(doc, failure["stage"], _(failure["message"]), failure["detail"], "site.clone", _job_log(job))
	doc.log = ut.append_log(doc.log, "site.clone (backup, restore, migrate)", _job_log(job))
	smoke = create_agent_job(
		frappe.db.get_value("Bench", doc.target_bench, "server") or doc.server,
		"site.smoke_check",
		ut.smoke_params(doc.test_site, _site_apps(doc.test_site) or _site_apps(doc.site)),
		TEST,
		doc.name,
	)
	_set(doc, status="Testing", smoke_job=smoke.name)


def _on_smoke(doc, job):
	result = frappe.parse_json(job.result) if job.get("result") else None
	outcome = ut.smoke_outcome(result, job.error if job.status != "Success" else None)
	log = ut.append_log(doc.log, "site.smoke_check", _job_log(job))
	if job.status == "Success" and outcome["ok"]:
		_set(doc, status="Passed", checks=frappe.as_json(outcome["checks"]), result_summary=outcome["summary"],
			finished_at=now_datetime(), log=log, failed_stage=None, error=None)
		_log(doc.team, "site.update_test_passed", doc.site, {"test": doc.name})
		if doc.auto_apply:
			try:
				apply_test(doc, automatic=True)
			except Exception as exc:
				doc.reload()
				_set(doc, result_summary=_("Passed; applying automatically failed: {0}").format(str(exc)[:500]))
		elif doc.mode == "Auto":
			drop_test_site(doc, remove_bench=False)
		return
	_fail(doc, "smoke_check", _("Smoke check failed"), outcome["summary"] or job.error,
		log=log, checks=frappe.as_json(outcome["checks"]))


# --------------------------------------------------------------------------- apply / cleanup


def apply_block_reason(doc):
	site = frappe.db.get_value("Site", doc.site, ["status", "bench"], as_dict=True) or {}
	return ut.apply_block_reason(doc.status, site, doc.source_bench, frappe.db.get_value("Bench", doc.target_bench, "status"))


def apply_test(doc, automatic=False):
	"""Move the production site to the tested bench (Site.move_to_bench takes a fresh backup
	first and keeps the site untouched if anything fails before the source is dropped)."""
	reason = apply_block_reason(doc)
	if reason:
		frappe.throw(_(reason))
	site = frappe.get_doc("Site", doc.site)
	if automatic:
		site.flags.ignore_permissions = True
	result = site.move_to_bench(doc.target_bench, allow_upgrade=True)
	_set(doc, status="Applying", apply_backup=result.get("backup"),
		log=ut.append_log(doc.log, "apply", f"moving {doc.site} to bench {doc.target_bench} (backup {result.get('backup')} first)"))
	_log(doc.team, "site.update_apply", doc.site, {"test": doc.name, "target_bench": doc.target_bench, "automatic": automatic})
	return result


def drop_test_site(doc, remove_bench=True):
	"""Delete the test copy (site.drop without backup). When the bench was created for this test
	and the production site did not move there, it is removed once empty."""
	values = {"cleanup_requested": int(bool(remove_bench and doc.created_bench))}
	job = None
	if doc.test_site and not doc.test_site_dropped:
		status = frappe.db.get_value("Site", doc.test_site, "status")
		if status and status not in ("Archived",):
			if status in ("Pending", "Moving", "Renaming"):
				frappe.throw(_("The test site is busy ({0}); try again in a moment").format(status))
			site = frappe.get_doc("Site", doc.test_site)
			job = site._job("site.drop", {"skip_backup": True})
		values["test_site_dropped"] = 1
	_set(doc, **values)
	if not job:
		_cleanup_bench(doc)
	return job


def _cleanup_bench(doc):
	"""Remove the bench created for a test once nothing lives on it."""
	if not (doc.created_bench and doc.cleanup_requested and not doc.bench_removed):
		return
	if frappe.db.get_value("Site", doc.site, "bench") == doc.target_bench:
		_set(doc, cleanup_requested=0)  # production runs there now: keep the bench
		return
	if frappe.db.exists("Site", {"bench": doc.target_bench, "status": ["!=", "Archived"]}):
		return
	bench = frappe.db.get_value("Bench", doc.target_bench, ["name", "status", "server", "bench_name"], as_dict=True)
	if bench and bench.status in ("Active", "Broken"):
		create_agent_job(bench.server, "bench.remove", {"bench": bench.bench_name, "force": False}, "Bench", bench.name)
	_set(doc, bench_removed=1)


def cancel_test(doc):
	if doc.status not in ut.CANCELLABLE:
		frappe.throw(_("A test that is {0} cannot be cancelled").format(_(doc.status)))
	_set(doc, status="Cancelled", finished_at=doc.finished_at or now_datetime())


# --------------------------------------------------------------------------- sync (scheduler / reads)


def sync_test(doc):
	"""Reconcile one test with the documents it waits for (missed callbacks, the apply move)."""
	if doc.status == "Applying":
		site = frappe.db.get_value("Site", doc.site, ["status", "bench", "move_steps"], as_dict=True) or {}
		outcome = ut.apply_outcome(site, doc.source_bench, doc.target_bench)
		if outcome == "Applied":
			_set(doc, status="Applied", applied_at=now_datetime(), result_summary=_("Update applied: the site runs on {0}").format(doc.target_bench))
			_log(doc.team, "site.update_applied", doc.site, {"test": doc.name})
		elif outcome == "Apply Failed":
			note = _("The move failed after the site was removed from the old bench; restore backup {0} or retry the failed job").format(doc.apply_backup) \
				if site.get("status") == "Broken" else _("The move was aborted; the site keeps running on {0}").format(doc.source_bench)
			_set(doc, status="Apply Failed", result_summary=note, finished_at=now_datetime())
		return doc
	job_name = {"Cloning": doc.clone_job, "Testing": doc.smoke_job}.get(doc.status)
	if job_name:
		job = frappe.get_doc("Agent Job", job_name) if frappe.db.exists("Agent Job", job_name) else None
		if job and job.status in ("Success", "Failure"):
			on_agent_job(job)
			doc.reload()
	elif doc.status == "Preparing Bench":
		status = frappe.db.get_value("Bench", doc.target_bench, "status")
		if status == "Active":
			_safe_start_clone(doc)
		elif status in ("Broken", "Archived", None):
			_fail(doc, "bench", _("Creating bench {0} with the new image failed").format(doc.target_bench))
	if doc.cleanup_requested and not doc.bench_removed and doc.test_site_dropped:
		if frappe.db.get_value("Site", doc.test_site, "status") in ("Archived", None):
			_cleanup_bench(doc)
	return doc


def sync_update_tests():
	"""Scheduler (every 5 min)."""
	names = frappe.get_all(
		TEST,
		filters=[["status", "in", list(ut.IN_PROGRESS)]],
		pluck="name",
		limit=100,
	) + frappe.get_all(TEST, filters={"cleanup_requested": 1, "bench_removed": 0, "test_site_dropped": 1}, pluck="name", limit=50)
	for name in dict.fromkeys(names):
		try:
			sync_test(frappe.get_doc(TEST, name))
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Nexaploy: syncing update test {name} failed")


# --------------------------------------------------------------------------- automatic updates


def auto_update_gate(release_group, bench, deploy_candidate):
	"""ops.apply_auto_updates: True when every Active production site on `bench` passed a test
	against `deploy_candidate`; starts the missing tests (mode Auto) otherwise."""
	sites = frappe.get_all(
		"Site", filters={"bench": bench, "status": "Active", "is_staging": 0}, pluck="name"
	)
	tests = {}
	for site in sites:
		tests[site] = frappe.db.get_value(
			TEST, {"site": site, "deploy_candidate": deploy_candidate, "mode": "Auto"}, "status", order_by="creation desc"
		)
	decision = ut.auto_gate(sites, tests)
	for site in decision["start"]:
		try:
			start_test(site, deploy_candidate=deploy_candidate, mode="Auto", user="Administrator")
		except Exception as exc:
			frappe.log_error(title=f"Nexaploy: automatic update test of {site} could not start", message=str(exc))
	for site in decision["failed"]:
		name = frappe.db.get_value(
			TEST, {"site": site, "deploy_candidate": deploy_candidate, "mode": "Auto", "auto_notified": 0}, "name"
		)
		if name:
			frappe.db.set_value(TEST, name, "auto_notified", 1, update_modified=False)
			team = frappe.db.get_value("Site", site, "team")
			_log(team, "site.auto_update_blocked", site, {"test": name, "release_group": release_group, "bench": bench})
	return decision["action"] == "apply"


def finish_auto_update(bench):
	"""After an automatic bench.update succeeded, the passed Auto tests of its sites are Applied
	and their copies removed."""
	for name in frappe.get_all(
		TEST, filters={"source_bench": bench, "mode": "Auto", "status": "Passed"}, pluck="name"
	):
		doc = frappe.get_doc(TEST, name)
		_set(doc, status="Applied", applied_at=now_datetime(),
			result_summary=_("Applied by the automatic update of bench {0}").format(bench))
		try:
			drop_test_site(doc, remove_bench=True)
		except Exception:
			frappe.log_error(title=f"Nexaploy: cleaning up update test {name} failed")
