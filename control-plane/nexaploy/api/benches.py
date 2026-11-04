"""Customer-managed Release Groups ("bench groups") for the portal.

A team creates a Release Group (Frappe major + marketplace apps + its own custom apps from
GitHub), builds images (Deploy Candidates) and deploys them as Benches on its own servers.
Every method is scoped to the current team through nexaploy.teams (ARCHITECTURE.md §9):
reads need `read`, everything else `write`.
"""

import json
import secrets

import frappe
from frappe import _

from nexaploy import marketplace_utils as mu
from nexaploy.utils import (
	CUSTOM_SOURCE,
	MARKETPLACE_SOURCE,
	SUPPORTED_MAJORS,
	is_valid_release_group_name,
	unique_bench_name,
)

GROUP_FIELDS = [
	"name", "title", "frappe_major", "frappe_branch", "team", "build_server", "auto_update",
	"update_window", "update_available", "creation", "modified",
]
DEPLOY_FIELDS = ["name", "build_id", "image_tag", "status", "build_server", "agent_job", "creation", "modified"]
BENCH_FIELDS = ["name", "bench_name", "server", "deploy_candidate", "image_tag", "status", "creation"]


# --------------------------------------------------------------------------- access


def _team(action="read"):
	from nexaploy.teams import check_team_access, get_current_team

	team = get_current_team()
	check_team_access(team, action)
	return team


def _group(name, action="read"):
	team = _team(action)
	if not name or not frappe.db.exists("Release Group", name):
		raise frappe.DoesNotExistError(_("Release Group {0} not found").format(name))
	doc = frappe.get_doc("Release Group", name)
	if doc.team != team:
		raise frappe.PermissionError(_("Release Group {0} belongs to another team").format(name))
	return doc


def _team_server(server, team, purpose):
	row = frappe.db.get_value("Server", server, ["name", "team", "status", "hostname"], as_dict=True)
	if not row:
		raise frappe.DoesNotExistError(_("Server {0} not found").format(server))
	if row.team != team:
		raise frappe.PermissionError(_("Server {0} belongs to another team").format(server))
	if row.status not in ("Active", "Offline"):
		frappe.throw(_("Server {0} is {1}; it cannot be used as {2}").format(row.hostname or server, _(row.status), purpose))
	return row


def _parse_apps(apps):
	if apps in (None, ""):
		return []
	if isinstance(apps, str):
		try:
			apps = json.loads(apps)
		except ValueError:
			frappe.throw(_("apps must be a JSON list"))
	if not isinstance(apps, list):
		frappe.throw(_("apps must be a list"))
	rows = []
	for item in apps:
		if isinstance(item, str):
			item = {"source_type": MARKETPLACE_SOURCE, "app": item}
		if not isinstance(item, dict):
			frappe.throw(_("Each app must be an object {source_type, app, branch}"))
		rows.append(_app_row(item.get("source_type"), item.get("app"), item.get("branch")))
	return rows


def _app_row(source_type, app, branch=None):
	source_type = source_type or MARKETPLACE_SOURCE
	if source_type not in (MARKETPLACE_SOURCE, CUSTOM_SOURCE):
		frappe.throw(_("Unknown app source {0}").format(source_type))
	if not app:
		frappe.throw(_("app is required"))
	row = {"source_type": source_type, "branch": (branch or "").strip() or None}
	row["custom_app" if source_type == CUSTOM_SOURCE else "app"] = str(app).strip()
	return row


def _with_requirements(rows, major, present=()):
	"""rows + the marketplace apps they require that are not in the group yet, required apps
	first (hrms -> erpnext, hrms). Returns (rows, auto_added app names). Refuses with a clear
	message when a requirement cannot be added for `major`."""
	market = [r["app"] for r in rows if r["source_type"] == MARKETPLACE_SOURCE]
	custom = [r for r in rows if r["source_type"] == CUSTOM_SOURCE]
	if not market:
		return rows, []
	from nexaploy.api.marketplace import load_catalog

	present = set(present)
	for r in custom:
		name = frappe.db.get_value("Custom App", r["custom_app"], "app_name")
		if name:
			present.add(name)
	dup = [a for a in market if a in present]
	if dup:
		frappe.throw(_("{0} is already in this bench group").format(", ".join(dup)))
	catalog = load_catalog()
	try:
		order = mu.apps_to_add(market, present, catalog, major)
	except mu.RequirementError as exc:
		frappe.throw(_(str(exc)), title=_("Missing required apps"))
	picked = {r["app"]: r for r in rows if r["source_type"] == MARKETPLACE_SOURCE}
	added = [a for a in order if a not in picked]
	out = [picked.get(a) or {"source_type": MARKETPLACE_SOURCE, "app": a, "branch": None} for a in order]
	return out + custom, added


# --------------------------------------------------------------------------- serialisation


def _apps_of(doc):
	custom = [r.custom_app for r in doc.apps if r.source_type == CUSTOM_SOURCE and r.custom_app]
	cmeta = {}
	if custom:
		for c in frappe.get_all(
			"Custom App",
			filters={"name": ["in", custom]},
			fields=["name", "title", "repo_full_name", "last_commit", "last_commit_at", "last_commit_message", "status", "branch"],
		):
			cmeta[c.name] = c
	market = [r.app for r in doc.apps if r.source_type != CUSTOM_SOURCE and r.app]
	mmeta = {}
	if market:
		for m in frappe.get_all("Marketplace App", filters={"name": ["in", market]}, fields=["name", "title", "pricing", "price_monthly"]):
			mmeta[m.name] = m
	out = []
	for r in doc.apps:
		item = {
			"source_type": r.source_type or MARKETPLACE_SOURCE,
			"app": r.custom_app if r.source_type == CUSTOM_SOURCE else r.app,
			"app_name": r.app_name or r.app,
			"branch": r.branch,
			"repo_url": r.repo_url,
		}
		if r.source_type == CUSTOM_SOURCE:
			c = cmeta.get(r.custom_app) or {}
			item.update(
				title=c.get("title") or r.app_name,
				repo=c.get("repo_full_name"),
				last_commit=c.get("last_commit"),
				last_commit_at=c.get("last_commit_at"),
				last_commit_message=c.get("last_commit_message") if c.get("branch") == r.branch else None,
				status=c.get("status"),
			)
		else:
			m = mmeta.get(r.app) or {}
			item.update(title=m.get("title") or r.app, pricing=m.get("pricing"), price_monthly=m.get("price_monthly"))
		out.append(item)
	return out


def _deploys(group, limit=20):
	return frappe.get_all(
		"Deploy Candidate",
		filters={"release_group": group},
		fields=DEPLOY_FIELDS,
		order_by="creation desc",
		limit_page_length=int(limit),
	)


def _benches(group):
	benches = frappe.get_all(
		"Bench",
		filters={"release_group": group, "status": ["!=", "Archived"]},
		fields=BENCH_FIELDS,
		order_by="creation desc",
	)
	servers = {b.server for b in benches}
	hostnames = {}
	if servers:
		hostnames = {
			s.name: s.hostname
			for s in frappe.get_all("Server", filters={"name": ["in", list(servers)]}, fields=["name", "hostname"])
		}
	for b in benches:
		b["server_hostname"] = hostnames.get(b.server)
		b["sites"] = frappe.db.count("Site", {"bench": b.name, "status": ["!=", "Archived"]})
	return benches


def _group_dict(doc, full=False):
	data = {f: doc.get(f) for f in GROUP_FIELDS}
	data["update_available"] = bool(doc.update_available)
	data["auto_update"] = bool(doc.auto_update)
	data["apps"] = _apps_of(doc)
	latest = _deploys(doc.name, 1)
	data["last_deploy"] = latest[0] if latest else None
	latest_ok = doc.latest_image()
	data["latest_image"] = {"deploy_candidate": latest_ok[0], "image_tag": latest_ok[1]} if latest_ok[0] else None
	if full:
		data["deploys"] = _deploys(doc.name)
		data["benches"] = _benches(doc.name)
		data["servers"] = _servers(doc.team)
		from nexaploy import ops

		# without a registry an image only exists on its build server
		data["registry_configured"] = bool(ops.registry_url())
		data["build_server_effective"] = doc.build_server or ops.default_build_server()
	else:
		data["benches"] = frappe.db.count("Bench", {"release_group": doc.name, "status": ["!=", "Archived"]})
	return data


def _servers(team):
	return frappe.get_all(
		"Server",
		filters={"team": team, "status": ["in", ["Active", "Offline"]]},
		fields=["name", "hostname", "status", "ownership", "is_build_server"],
		order_by="creation asc",
	)


# --------------------------------------------------------------------------- read


@frappe.whitelist()
def list_release_groups():
	team = _team("read")
	names = frappe.get_all("Release Group", filters={"team": team}, pluck="name", order_by="creation desc")
	return [_group_dict(frappe.get_doc("Release Group", n)) for n in names]


@frappe.whitelist()
def get_release_group(name):
	return _group_dict(_group(name, "read"), full=True)


@frappe.whitelist()
def options(frappe_major=None):
	"""Everything the create form needs: majors, the team's servers, marketplace apps for
	the major, the team's custom apps and the GitHub connection state."""
	team = _team("read")
	major = str(frappe_major) if frappe_major not in (None, "", "all") else None
	if major and major not in SUPPORTED_MAJORS:
		frappe.throw(_("Unsupported Frappe version {0}").format(major))
	from nexaploy.api.github import list_custom_apps, list_installations
	from nexaploy.api.marketplace import list_apps

	github = list_installations()
	return {
		"majors": list(SUPPORTED_MAJORS),
		"servers": _servers(team),
		"marketplace_apps": list_apps(major),
		"custom_apps": [a for a in list_custom_apps(major) if a.status == "Active"],
		"github": github,
		"has_default_build_server": bool(frappe.db.get_single_value("Nexaploy Settings", "default_build_server")),
	}


@frappe.whitelist()
def list_deploys(release_group, limit=20):
	_group(release_group, "read")
	return _deploys(release_group, min(int(limit or 20), 100))


@frappe.whitelist()
def get_deploy(name):
	"""Deploy Candidate with its build log (live steps of the image.build job while running)."""
	dc = frappe.db.get_value("Deploy Candidate", name, DEPLOY_FIELDS + ["release_group", "build_log", "apps_json"], as_dict=True)
	if not dc:
		raise frappe.DoesNotExistError(_("Deploy Candidate {0} not found").format(name))
	_group(dc.release_group, "read")
	from nexaploy.github_utils import redact_apps_json, redact_text

	dc["apps_json"] = redact_apps_json(frappe.parse_json(dc.apps_json or "[]"))
	dc["build_log"] = redact_text(dc.build_log)
	dc["steps"] = []
	dc["job_status"] = None
	if dc.agent_job and frappe.db.exists("Agent Job", dc.agent_job):
		job = frappe.get_doc("Agent Job", dc.agent_job)
		dc["job_status"] = job.status
		dc["error"] = redact_text(job.error)
		dc["steps"] = [
			{
				"idx": s.step_idx,
				"name": s.step_name,
				"status": s.status,
				"output": redact_text(s.output),
				"started_at": s.started_at,
				"ended_at": s.ended_at,
			}
			for s in sorted(job.steps, key=lambda s: (s.step_idx or 0, s.idx))
		]
	dc["active"] = dc.status in ("Pending", "Building")
	return dc


# --------------------------------------------------------------------------- write


@frappe.whitelist(methods=["POST"])
def create_release_group(title, frappe_major, apps=None, build_server=None, auto_update=0, update_window=None):
	team = _team("write")
	title = (title or "").strip().lower()
	if not is_valid_release_group_name(title):
		frappe.throw(_("Name must match ^[a-z][a-z0-9-]{2,30}$ (it is used in image tags), e.g. acme-v15"))
	if frappe.db.exists("Release Group", title):
		frappe.throw(_("The name {0} is already taken").format(title))
	if str(frappe_major) not in SUPPORTED_MAJORS:
		frappe.throw(_("Unsupported Frappe version {0}").format(frappe_major))
	app_rows, auto_added = _with_requirements(_parse_apps(apps), str(frappe_major))
	if build_server:
		_team_server(build_server, team, _("build server"))
	elif not frappe.db.get_single_value("Nexaploy Settings", "default_build_server"):
		frappe.throw(_("Choose one of your servers as the build server"))
	doc = frappe.get_doc(
		{
			"doctype": "Release Group",
			"title": title,
			"frappe_major": str(frappe_major),
			"team": team,
			"build_server": build_server or None,
			"auto_update": 1 if str(auto_update) in ("1", "true", "True") else 0,
			"update_window": update_window or None,
			"apps": app_rows,
		}
	)
	doc.insert(ignore_permissions=True)
	data = _group_dict(doc, full=True)
	data["auto_added"] = auto_added
	return data


@frappe.whitelist(methods=["POST"])
def update_release_group(name, build_server=None, auto_update=None, update_window=None):
	doc = _group(name, "write")
	if build_server is not None:
		if build_server:
			_team_server(build_server, doc.team, _("build server"))
		doc.build_server = build_server or None
	if auto_update is not None:
		doc.auto_update = 1 if str(auto_update) in ("1", "true", "True") else 0
	if update_window is not None:
		doc.update_window = update_window or None
	doc.save(ignore_permissions=True)
	return _group_dict(doc, full=True)


@frappe.whitelist(methods=["POST"])
def add_app(release_group, source_type, app, branch=None):
	doc = _group(release_group, "write")
	row = _app_row(source_type, app, branch)
	rows, auto_added = _with_requirements([row], doc.frappe_major, [r.app_name or r.app for r in doc.apps])
	for r in rows:
		doc.append("apps", r)
	doc.save(ignore_permissions=True)
	data = _group_dict(doc, full=True)
	data["auto_added"] = auto_added
	return data


@frappe.whitelist(methods=["POST"])
def remove_app(release_group, app):
	"""Remove by python app name (or Marketplace/Custom App name). Sites on existing benches
	keep the app until the next deploy; sites that have it installed block the removal."""
	doc = _group(release_group, "write")
	keep = [r for r in doc.apps if app not in (r.app_name, r.app, r.custom_app)]
	if len(keep) == len(doc.apps):
		frappe.throw(_("App {0} is not in release group {1}").format(app, doc.name))
	removed = [r for r in doc.apps if r not in keep]
	app_names = {r.app_name or r.app for r in removed}
	from nexaploy.api.marketplace import load_catalog

	catalog = load_catalog()
	kept_names = [r.app_name or r.app for r in keep]
	needed = sorted({d for a in app_names for d in mu.dependents(a, kept_names, catalog)})
	if needed:
		frappe.throw(
			_("{0} is required by {1}; remove those first").format(", ".join(sorted(app_names)), ", ".join(needed))
		)
	benches = frappe.get_all("Bench", filters={"release_group": doc.name, "status": ["!=", "Archived"]}, pluck="name")
	if benches:
		sites = frappe.get_all(
			"Site", filters={"bench": ["in", benches], "status": ["!=", "Archived"]}, pluck="name"
		)
		if sites and frappe.get_all(
			"Site App",
			filters={"parenttype": "Site", "parent": ["in", sites], "app": ["in", list(app_names)]},
			limit=1,
		):
			frappe.throw(_("Uninstall {0} from the sites of this group first").format(", ".join(sorted(app_names))))
	doc.apps = keep
	for i, r in enumerate(doc.apps, start=1):
		r.idx = i
	doc.save(ignore_permissions=True)
	return _group_dict(doc, full=True)


@frappe.whitelist(methods=["POST"])
def deploy(release_group):
	"""Build a new image (Deploy Candidate -> image.build on the build server)."""
	doc = _group(release_group, "write")
	if frappe.db.exists("Deploy Candidate", {"release_group": doc.name, "status": ["in", ["Pending", "Building"]]}):
		frappe.throw(_("A build of {0} is already in progress").format(doc.name))
	dc = frappe.get_doc({"doctype": "Deploy Candidate", "release_group": doc.name})
	dc.insert(ignore_permissions=True)
	return {"deploy_candidate": dc.name, "agent_job": dc.agent_job, "image_tag": dc.image_tag}


@frappe.whitelist(methods=["POST"])
def deploy_to_server(release_group, server, bench_name=None):
	"""Run the latest successful image of the group on one of the team's servers: creates a
	Bench (bench.create), or rolls the existing bench of this group there (bench.update)."""
	doc = _group(release_group, "write")
	srv = _team_server(server, doc.team, _("deploy target"))
	dc_name, image_tag = doc.latest_image()
	if not dc_name:
		frappe.throw(_("Release Group {0} has no successful build yet; deploy it first").format(doc.name))
	existing = frappe.db.get_value(
		"Bench", {"release_group": doc.name, "server": srv.name, "status": ["!=", "Archived"]}, "name"
	)
	if existing:
		bench = frappe.get_doc("Bench", existing)
		if bench.image_tag == image_tag:
			frappe.throw(_("Bench {0} already runs the latest image").format(bench.bench_name))
		bench.flags.ignore_permissions = True
		job = bench.update_image(dc_name)
		return {"bench": bench.name, "action": "update", "agent_job": job}
	if bench_name:
		bench_name = bench_name.strip().lower()
	else:
		taken = frappe.get_all("Bench", filters={"bench_name": ["like", f"{doc.name[:20]}%"]}, pluck="bench_name")
		bench_name = unique_bench_name(doc.name, taken, secrets.token_hex(2))
	bench = frappe.get_doc(
		{
			"doctype": "Bench",
			"bench_name": bench_name,
			"server": srv.name,
			"release_group": doc.name,
			"deploy_candidate": dc_name,
			"team": doc.team,
		}
	)
	bench.insert(ignore_permissions=True)
	job = frappe.db.get_value(
		"Agent Job", {"reference_doctype": "Bench", "reference_name": bench.name}, "name", order_by="creation desc"
	)
	return {"bench": bench.name, "action": "create", "agent_job": job}
