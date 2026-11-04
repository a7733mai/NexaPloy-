"""Portal API of "Safe update" (/dashboard/safe-update?site=): test an update on a copy of the
site before applying it. Logic: nexaploy.update_test (chain), nexaploy.update_test_utils (pure).

Access (nexaploy.teams.check_team_access on the site's team): read -> get_overview / get_test;
write -> start / apply / cancel / delete the test copy; the release group's auto-update gate needs
write on the group's team.

    get_overview(site)                       update available?, candidate benches, recent tests
    start_test(site, target_bench=None)      -> {name}
    get_test(name)                           status, checks, log, apply_block_reason
    apply_update(name, delete_test_site=0)   move production (fresh backup first)
    delete_test_site(name)                   drop the copy (and the bench made for it when empty)
    cancel_test(name)
    set_test_before_auto_update(release_group, enabled)
"""

import frappe
from frappe import _

from nexaploy import update_test as ut_core
from nexaploy import update_test_utils as ut

TEST = "Site Update Test"
TEST_FIELDS = [
	"name", "site", "team", "status", "mode", "auto_apply", "requested_by", "started_at", "finished_at",
	"applied_at", "server", "release_group", "source_bench", "source_image", "target_bench", "deploy_candidate",
	"target_image", "upgrade", "created_bench", "test_site", "test_site_dropped", "clone_job", "smoke_job",
	"apply_backup", "failed_stage", "result_summary", "creation",
]


def _require_user():
	if not frappe.session.user or frappe.session.user == "Guest":
		raise frappe.PermissionError(_("Please log in"))


def _check(team, action):
	from nexaploy.teams import check_team_access

	return check_team_access(team, action)


def _site(site, action="read"):
	_require_user()
	row = frappe.db.get_value(
		"Site", site, ["name", "site_name", "team", "bench", "server", "status", "is_staging"], as_dict=True
	) if site else None
	if not row:
		raise frappe.DoesNotExistError(_("Site {0} not found").format(site))
	row.role = _check(row.team, action)
	return row


def _test(name, action="read"):
	_require_user()
	doc = frappe.get_doc(TEST, name) if name and frappe.db.exists(TEST, name) else None
	if not doc:
		raise frappe.DoesNotExistError(_("Update test {0} not found").format(name))
	_check(doc.team, action)
	return doc


def _bench_info(name):
	if not name:
		return None
	b = frappe.db.get_value(
		"Bench", name, ["name", "bench_name", "status", "image_tag", "release_group", "server"], as_dict=True
	)
	if not b:
		return None
	b["frappe_major"] = frappe.db.get_value("Release Group", b.release_group, "frappe_major") if b.release_group else None
	return b


# --------------------------------------------------------------------------- read


@frappe.whitelist()
def get_overview(site):
	"""What the Safe update page needs for `site`."""
	from nexaploy.team_utils import role_allows

	row = _site(site, "read")
	can_write = role_allows(row.role, "write")
	source = _bench_info(row.bench) or frappe._dict()
	group = frappe.db.get_value(
		"Release Group", source.get("release_group"), ["name", "title", "team", "auto_update", "update_window",
			"test_before_auto_update", "frappe_major"], as_dict=True,
	) if source.get("release_group") else None
	latest_dc, latest_image = ut_core.latest_build(group.name) if group else (None, None)
	targets = []
	if can_write and not row.is_staging:
		from nexaploy.api.site_lifecycle import _move_targets

		for t in _move_targets(frappe._dict(row)):
			if t["same_server"] and t["compatible"]:
				info = _bench_info(t["name"]) or {}
				targets.append({**t, "image_tag": info.get("image_tag"), "release_group": info.get("release_group")})
	tests = frappe.get_all(
		TEST, filters={"site": row.name}, fields=TEST_FIELDS, order_by="creation desc", limit_page_length=10
	)
	group_writable = False
	if group:
		from nexaploy.teams import has_team_action, is_platform_admin

		group_writable = is_platform_admin() or (bool(group.team) and has_team_action(group.team, "write"))
	return {
		"site": row.name,
		"status": row.status,
		"is_staging": bool(row.is_staging),
		"source": source,
		"release_group": group,
		"latest_build": {"deploy_candidate": latest_dc, "image_tag": latest_image},
		"update_available": ut.update_available(source.get("image_tag"), latest_image),
		"targets": targets,
		"tests": tests,
		"active_test": ut_core.active_test(row.name),
		"test_site_name": _safe(ut.copy_site_name, row.site_name),
		"can_write": can_write,
		"group_writable": group_writable,
	}


def _safe(fn, *args):
	try:
		return fn(*args)
	except ValueError:
		return None


@frappe.whitelist()
def get_test(name):
	"""One test with checks, log and whether it can be applied now."""
	doc = _test(name, "read")
	if doc.status in ut.IN_PROGRESS or (doc.cleanup_requested and not doc.bench_removed):
		try:
			ut_core.sync_test(doc)
			doc.reload()
		except Exception:
			frappe.log_error(title=f"Nexaploy: syncing update test {name} failed")
	out = {f: doc.get(f) for f in TEST_FIELDS}
	out.update(
		{
			"checks": frappe.parse_json(doc.checks) if doc.checks else [],
			"error": doc.error,
			"log": doc.log,
			"apply_block_reason": ut_core.apply_block_reason(doc) if doc.status == "Passed" else None,
			"test_site_status": frappe.db.get_value("Site", doc.test_site, "status") if doc.test_site else None,
			"test_site_url": f"https://{doc.test_site}" if doc.test_site and not doc.test_site_dropped else None,
			"in_progress": doc.status in ut.IN_PROGRESS,
		}
	)
	return out


# --------------------------------------------------------------------------- mutations


@frappe.whitelist(methods=["POST"])
def start_test(site, target_bench=None):
	"""Clone the site onto a bench with the new image, migrate and smoke-check it. Production is
	not touched. `target_bench`: a bench on the same server (default: newest build of the
	site's release group, on a new bench if needed)."""
	row = _site(site, "write")
	doc = ut_core.start_test(row.name, target_bench=target_bench or None)
	return {"name": doc.name, "status": doc.status}


@frappe.whitelist(methods=["POST"])
def apply_update(name, delete_test_site=0):
	"""Apply a passed test: back up production and move it to the tested bench."""
	doc = _test(name, "write")
	result = ut_core.apply_test(doc)
	if str(delete_test_site).lower() in ("1", "true", "yes"):
		doc.reload()
		ut_core.drop_test_site(doc, remove_bench=False)
	return {"name": doc.name, "status": "Applying", "backup": result.get("backup")}


@frappe.whitelist(methods=["POST"])
def delete_test_site(name):
	"""Drop the test copy (no backup). The bench created for the test is removed once empty."""
	doc = _test(name, "write")
	if doc.status in ut.RUNNING:
		frappe.throw(_("Wait for the test to finish, or cancel it first"))
	job = ut_core.drop_test_site(doc, remove_bench=True)
	ut_core._log(doc.team, "site.update_test_cleanup", doc.site, {"test": doc.name, "test_site": doc.test_site})
	return {"name": doc.name, "job": job}


@frappe.whitelist(methods=["POST"])
def cancel_test(name):
	doc = _test(name, "write")
	ut_core.cancel_test(doc)
	return {"name": doc.name, "status": "Cancelled"}


@frappe.whitelist(methods=["POST"])
def set_test_before_auto_update(release_group, enabled):
	"""Only auto-update the group's benches after every site passed an update test."""
	_require_user()
	team = frappe.db.get_value("Release Group", release_group, "team")
	if team is None and not frappe.db.exists("Release Group", release_group):
		raise frappe.DoesNotExistError(_("Release group {0} not found").format(release_group))
	from nexaploy.teams import is_platform_admin

	if not team and not is_platform_admin():
		raise frappe.PermissionError(_("Only the platform can change shared release groups"))
	if team:
		_check(team, "write")
	value = 1 if str(enabled).strip().lower() in ("1", "true", "yes", "on") else 0
	frappe.db.set_value("Release Group", release_group, "test_before_auto_update", value)
	if team:
		from nexaploy.activity import log_activity

		log_activity(team, "release_group.safe_auto_update", "Release Group", release_group, {"enabled": bool(value)})
	return {"release_group": release_group, "test_before_auto_update": bool(value)}
