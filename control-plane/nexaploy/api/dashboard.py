"""Customer portal API (/dashboard pages call these with POST /api/method/...).

Every method is scoped to the request's current team (nexaploy.teams.get_current_team:
header X-Nexaploy-Team, cookie nexaploy_team, else the user's first team). Lists filter on
that team; single-document methods check the document's team with
nexaploy.teams.check_team_access (read for get/list, write for mutations, admin for
registration tokens and server operations), raising frappe.PermissionError otherwise.
Platform admins (System Manager / Administrator) pass every check and may select any team.

Pure helpers live in nexaploy/portal_utils.py.
"""

import inspect

import frappe
from frappe import _
from frappe.utils import now_datetime

from nexaploy.portal_utils import (
	DEFAULT_ROOT_DOMAIN,
	ROOT_DOMAIN_CONFIG_KEY,
	build_site_name,
	filter_action_args,
	generate_password,
	has_portal_access,
	is_job_active,
	is_portal_admin,
	login_redirect_url,
	normalize_root_domain,
	parse_app_list,
	pick_default_plan,
	seconds_since,
	summarize_counts,
	text_direction,
	to_bool,
)
from nexaploy.team_utils import ROLE_MATRIX, role_allows
from nexaploy.teams import check_team_access, get_current_team, get_user_teams
from nexaploy.utils import SUPPORTED_MAJORS, redact_params

JOB_FIELDS = [
	"name",
	"kind",
	"status",
	"server",
	"reference_doctype",
	"reference_name",
	"creation",
	"queued_at",
	"started_at",
	"ended_at",
	"error",
]

# --------------------------------------------------------------------------- access helpers


def _ctx(action="read"):
	"""Portal context {user, team, role, admin} for the current team; raises
	frappe.PermissionError for guests, users without the portal role and members whose
	role does not allow `action` (read/write/billing/admin) in the current team."""
	user = frappe.session.user
	if not user or user == "Guest":
		raise frappe.PermissionError(_("Please log in to use the dashboard"))
	roles = frappe.get_roles(user)
	if not has_portal_access(roles, user):
		raise frappe.PermissionError(_("You need the Nexaploy Customer role to use the dashboard"))
	team = get_current_team(user)
	role = check_team_access(team, action, user)
	return frappe._dict(user=user, team=team, role=role, admin=is_portal_admin(roles, user))


def _team_filters(c, **extra):
	filters = dict(extra)
	filters["team"] = c.team
	return filters


def _check_team(doctype, name, c, action="read"):
	"""Raise unless `doctype/name` exists and the user's role in its team allows `action`.
	Returns the document's team and the user's role in it."""
	if not name:
		frappe.throw(_("{0} is required").format(_(doctype)))
	team = frappe.db.get_value(doctype, name, "team")
	if team is None and not frappe.db.exists(doctype, name):
		raise frappe.DoesNotExistError(_("{0} {1} not found").format(_(doctype), name))
	if not team and not c.admin:
		raise frappe.PermissionError(_("{0} {1} belongs to another team").format(_(doctype), name))
	role = check_team_access(team, action, c.user) if team else c.role
	return team, role


def _permissions(role):
	"""{read, write, billing, admin} booleans for the portal UI."""
	return {action: role_allows(role, action) for action in ROLE_MATRIX}


def _root_domain():
	"""site_config `nexaploy_root_domain`, else Nexaploy Settings.root_domain, else the default."""
	value = frappe.conf.get(ROOT_DOMAIN_CONFIG_KEY)
	if not value:
		try:
			value = frappe.db.get_single_value("Nexaploy Settings", "root_domain")
		except Exception:
			value = None
	return normalize_root_domain(value or DEFAULT_ROOT_DOMAIN)


def _default_site_plan():
	"""Billing Settings.default_site_plan when it is an enabled Site plan, else the cheapest."""
	plans = _site_plans()
	try:
		preferred = frappe.db.get_single_value("Billing Settings", "default_site_plan")
	except Exception:
		preferred = None
	if preferred and any(p.name == preferred for p in plans):
		return preferred
	return pick_default_plan(plans)


def _app_meta(app_names):
	if not app_names:
		return {}
	rows = frappe.get_all(
		"Marketplace App",
		filters={"name": ["in", list(app_names)]},
		fields=["app_name", "title", "category", "pricing", "price_monthly"],
	)
	return {r.app_name: r for r in rows}


def _group_apps(groups):
	"""{release_group: [app, ...]} in row order."""
	if not groups:
		return {}
	rows = frappe.get_all(
		"Release Group App",
		filters={"parenttype": "Release Group", "parent": ["in", list(groups)]},
		fields=["parent", "app"],
		order_by="idx asc",
	)
	result = {}
	for row in rows:
		result.setdefault(row.parent, []).append(row.app)
	return result


def _group_majors(groups):
	if not groups:
		return {}
	rows = frappe.get_all(
		"Release Group", filters={"name": ["in", list(groups)]}, fields=["name", "frappe_major"]
	)
	return {r.name: r.frappe_major for r in rows}


def _site_plans():
	return frappe.get_all(
		"Nexaploy Plan",
		filters={"plan_type": "Site", "enabled": 1},
		fields=["name", "title", "price_monthly", "currency", "max_sites", "description", "enabled"],
		order_by="price_monthly asc",
	)


def _byos_plan():
	plans = frappe.get_all(
		"Nexaploy Plan",
		filters={"plan_type": "Server BYOS", "enabled": 1},
		fields=["name", "title", "price_monthly", "currency", "description", "enabled"],
	)
	name = pick_default_plan(plans)
	return next((p for p in plans if p.name == name), None)


# --------------------------------------------------------------------------- jobs


def _team_jobs(c, limit=20, server=None, site=None):
	"""Agent Jobs visible to the team, newest first.

	A customer sees jobs on their own servers plus jobs that reference their sites and
	site backups (sites on shared hosted benches run on servers they do not own).
	"""
	limit = int(limit)
	if site:
		backups = frappe.get_all("Site Backup", filters={"site": site}, pluck="name")
		queries = [{"reference_doctype": "Site", "reference_name": site}]
		if backups:
			queries.append({"reference_doctype": "Site Backup", "reference_name": ["in", backups]})
	elif server:
		queries = [{"server": server}]
	else:
		queries = []
		servers = frappe.get_all("Server", filters={"team": c.team}, pluck="name")
		if servers:
			queries.append({"server": ["in", servers]})
		sites = frappe.get_all("Site", filters={"team": c.team}, pluck="name")
		if sites:
			queries.append({"reference_doctype": "Site", "reference_name": ["in", sites]})
		backups = frappe.get_all("Site Backup", filters={"team": c.team}, pluck="name")
		if backups:
			queries.append({"reference_doctype": "Site Backup", "reference_name": ["in", backups]})

	jobs = {}
	for filters in queries:
		for row in frappe.get_all(
			"Agent Job", filters=filters, fields=JOB_FIELDS, order_by="creation desc", limit_page_length=limit
		):
			jobs[row.name] = row
	result = sorted(jobs.values(), key=lambda r: r.creation, reverse=True)[:limit]
	for row in result:
		row["active"] = is_job_active(row.status)
	return result


def _job_allowed(job, c):
	if c.admin:
		return True
	member_of = {t["name"] for t in get_user_teams(c.user)}

	def ok(team):
		return bool(team) and team in member_of

	if job.server and ok(frappe.db.get_value("Server", job.server, "team")):
		return True
	ref_dt, ref_name = job.reference_doctype, job.reference_name
	if not (ref_dt and ref_name):
		return False
	if ref_dt in ("Site", "Site Backup", "Bench", "Release Group"):
		return ok(frappe.db.get_value(ref_dt, ref_name, "team"))
	if ref_dt == "Deploy Candidate":
		group = frappe.db.get_value("Deploy Candidate", ref_name, "release_group")
		return bool(group) and ok(frappe.db.get_value("Release Group", group, "team"))
	return False


def _job_for(reference_doctype, reference_name, kind=None):
	filters = {"reference_doctype": reference_doctype, "reference_name": reference_name}
	if kind:
		filters["kind"] = kind
	return frappe.db.get_value("Agent Job", filters, "name", order_by="creation desc")


# --------------------------------------------------------------------------- overview


@frappe.whitelist()
def overview():
	c = _ctx()
	servers = frappe.get_all(
		"Server",
		filters=_team_filters(c, status=["!=", "Archived"]),
		fields=["name", "hostname", "status", "ownership", "last_heartbeat"],
		order_by="creation desc",
	)
	sites = frappe.get_all(
		"Site",
		filters=_team_filters(c, status=["!=", "Archived"]),
		fields=["name", "status", "bench", "server", "plan", "creation"],
		order_by="creation desc",
	)
	jobs = _team_jobs(c, limit=10)
	return {
		"user": c.user,
		"is_admin": c.admin,
		"team": c.team,
		"team_title": frappe.db.get_value("Team", c.team, "title"),
		"role": c.role,
		"permissions": _permissions(c.role),
		"teams": get_user_teams(c.user),
		"servers": {"total": len(servers), "by_status": summarize_counts(servers), "recent": servers[:5]},
		"sites": {"total": len(sites), "by_status": summarize_counts(sites), "recent": sites[:5]},
		"recent_jobs": jobs,
		"active_jobs": sum(1 for j in jobs if j.active),
	}


# --------------------------------------------------------------------------- sites


@frappe.whitelist()
def list_sites(include_archived=False):
	c = _ctx()
	extra = {} if to_bool(include_archived) else {"status": ["!=", "Archived"]}
	sites = frappe.get_all(
		"Site",
		filters=_team_filters(c, **extra),
		fields=["name", "status", "bench", "server", "plan", "team", "creation"],
		order_by="creation desc",
	)
	benches = {s.bench for s in sites}
	bench_groups = {}
	if benches:
		for b in frappe.get_all("Bench", filters={"name": ["in", list(benches)]}, fields=["name", "release_group"]):
			bench_groups[b.name] = b.release_group
	majors = _group_majors(set(bench_groups.values()))
	for s in sites:
		s["release_group"] = bench_groups.get(s.bench)
		s["frappe_major"] = majors.get(s["release_group"])
	return sites


@frappe.whitelist()
def get_site(name):
	c = _ctx()
	site_team, role = _check_team("Site", name, c, "read")
	doc = frappe.get_doc("Site", name)

	bench = frappe.db.get_value(
		"Bench", doc.bench, ["name", "bench_name", "release_group", "server", "status"], as_dict=True
	) or frappe._dict()
	frappe_major = frappe.db.get_value("Release Group", bench.release_group, "frappe_major") if bench else None
	group_apps = _group_apps([bench.release_group]).get(bench.release_group, []) if bench else []
	installed_names = [row.app for row in doc.apps]
	meta = _app_meta(set(group_apps) | set(installed_names))

	def app_info(app, status=None):
		m = meta.get(app) or {}
		return {
			"app": app,
			"title": m.get("title") or app,
			"category": m.get("category"),
			"pricing": m.get("pricing"),
			"price_monthly": m.get("price_monthly"),
			"status": status,
		}

	server = frappe.db.get_value(
		"Server", doc.server, ["name", "hostname", "ownership", "status", "team"], as_dict=True
	) or frappe._dict()
	backups = frappe.get_all(
		"Site Backup",
		filters={"site": name},
		fields=["name", "status", "with_files", "purpose", "created_at", "size", "creation", "remote_path"],
		order_by="creation desc",
		limit_page_length=20,
	)
	for b in backups:
		b["restorable"] = b.status == "Success" and bool(b.remote_path)
		b.pop("remote_path", None)

	plan = None
	if doc.plan:
		plan = frappe.db.get_value(
			"Nexaploy Plan", doc.plan, ["name", "title", "price_monthly", "currency"], as_dict=True
		)

	return {
		"name": doc.name,
		"site_name": doc.site_name,
		"status": doc.status,
		"team": doc.team,
		"creation": doc.creation,
		"url": f"https://{doc.site_name}",
		"bench": bench,
		"frappe_major": frappe_major,
		"server": {
			"name": server.name,
			"hostname": server.hostname,
			"ownership": server.ownership,
			"status": server.status,
			"own": bool(server.team) and (c.admin or server.team == site_team),
		},
		"plan": plan,
		"apps": [app_info(row.app, row.status) for row in doc.apps],
		"available_apps": [app_info(a) for a in group_apps if a not in installed_names],
		"domains": [{"domain": row.domain, "status": row.status} for row in doc.domains],
		"backups": backups,
		"jobs": _team_jobs(c, limit=20, site=name),
		"role": role,
		"permissions": _permissions(role),
		"can_write": role_allows(role, "write"),
		"can_drop": role_allows(role, "write") and bool(frappe.has_permission("Site", "delete", doc)),
		"moving": doc.status == "Moving",
	}


def _deployable_bench(bench, c):
	"""Bench row (+ server info) if the user may create sites on it, else PermissionError."""
	row = frappe.db.get_value(
		"Bench", bench, ["name", "bench_name", "server", "release_group", "team", "status"], as_dict=True
	)
	if not row:
		raise frappe.DoesNotExistError(_("Bench {0} not found").format(bench))
	server = frappe.db.get_value("Server", row.server, ["hostname", "ownership", "team", "status"], as_dict=True)
	server = server or frappe._dict()
	row.server_hostname = server.hostname
	row.ownership = server.ownership
	row.server_team = server.team
	row.server_status = server.status
	row.own = bool(c.team and (row.team == c.team or server.team == c.team))
	row.shared = not row.team and not server.team and server.ownership == "Hosted"
	if not (c.admin or row.own or row.shared):
		raise frappe.PermissionError(_("You cannot deploy on bench {0}").format(bench))
	return row


@frappe.whitelist()
def list_benches(server=None):
	"""Active benches the user can deploy sites on: benches of their team / on their servers,
	plus shared hosted benches (Bench.team empty on a Hosted server without a team)."""
	c = _ctx()
	filters = {"status": "Active"}
	if server:
		filters["server"] = server
	benches = frappe.get_all(
		"Bench",
		filters=filters,
		fields=["name", "bench_name", "server", "release_group", "team", "status"],
		order_by="creation desc",
	)
	server_names = {b.server for b in benches}
	servers = {}
	if server_names:
		for s in frappe.get_all(
			"Server",
			filters={"name": ["in", list(server_names)]},
			fields=["name", "hostname", "ownership", "team", "status"],
		):
			servers[s.name] = s

	result = []
	for b in benches:
		s = servers.get(b.server) or frappe._dict()
		if s.status == "Archived":
			continue
		own = bool(c.team) and (b.team == c.team or s.team == c.team)
		shared = not b.team and not s.team and s.ownership == "Hosted"
		if not (c.admin or own or shared):
			continue
		b.server_hostname = s.hostname
		b.ownership = s.ownership
		b.server_status = s.status
		b.own = bool(own)
		b.shared = bool(shared)
		b.allow_custom_domain = bool(c.admin or own)
		b.pop("team", None)
		result.append(b)

	groups = {b.release_group for b in result}
	majors = _group_majors(groups)
	apps_by_group = _group_apps(groups)
	meta = _app_meta({a for apps in apps_by_group.values() for a in apps})
	for b in result:
		b.frappe_major = majors.get(b.release_group)
		b.apps = [
			{
				"app": a,
				"title": (meta.get(a) or {}).get("title") or a,
				"pricing": (meta.get(a) or {}).get("pricing"),
				"price_monthly": (meta.get(a) or {}).get("price_monthly"),
			}
			for a in apps_by_group.get(b.release_group, [])
		]
	return result


@frappe.whitelist()
def list_plans(plan_type="Site"):
	_ctx()
	if plan_type not in ("Site", "Server BYOS", "Server Hosted"):
		frappe.throw(_("Unknown plan type {0}").format(plan_type))
	return frappe.get_all(
		"Nexaploy Plan",
		filters={"plan_type": plan_type, "enabled": 1},
		fields=["name", "title", "price_monthly", "currency", "max_sites", "description"],
		order_by="price_monthly asc",
	)


@frappe.whitelist(methods=["POST"])
def create_site(bench, subdomain_or_name, apps=None, plan=None):
	"""Create a Site on `bench`. Returns {name, admin_password, job}; the password is
	shown once (it is stored only in the Site's encrypted Password field)."""
	c = _ctx("write")
	b = _deployable_bench(bench, c)
	if b.status != "Active":
		frappe.throw(_("Bench {0} is not active").format(bench))
	if b.server_status == "Archived":
		frappe.throw(_("Server {0} is archived").format(b.server))

	try:
		site_name = build_site_name(subdomain_or_name, _root_domain(), allow_custom=c.admin or b.own)
		app_list = parse_app_list(apps)
	except ValueError as exc:
		frappe.throw(_(str(exc)))

	if frappe.db.exists("Site", site_name) or frappe.db.exists(
		"Site Domain", {"domain": site_name, "parenttype": "Site"}
	):
		frappe.throw(_("{0} is already taken").format(site_name))

	allowed = set(_group_apps([b.release_group]).get(b.release_group, []))
	missing = [a for a in app_list if a not in allowed]
	if missing:
		frappe.throw(_("Apps {0} are not available on this bench").format(", ".join(missing)))

	if plan:
		row = frappe.db.get_value("Nexaploy Plan", plan, ["plan_type", "enabled"], as_dict=True)
		if not row or row.plan_type != "Site" or not row.enabled:
			frappe.throw(_("Plan {0} is not an available Site plan").format(plan))
	elif b.shared:
		# sites on shared hosted benches are billed per site
		plan = _default_site_plan()
		if not plan:
			frappe.throw(_("No Site plan is available"))

	team = c.team

	password = generate_password()
	doc = frappe.get_doc(
		{
			"doctype": "Site",
			"site_name": site_name,
			"bench": b.name,
			"server": b.server,
			"team": team,
			"plan": plan or None,
			"admin_password": password,
			"apps": [{"app": a} for a in app_list],
		}
	)
	doc.insert(ignore_permissions=True)
	return {
		"name": doc.name,
		"url": f"https://{doc.site_name}",
		"admin_password": password,
		"job": _job_for("Site", doc.name, "site.create"),
	}


def _restore(doc, backup):
	row = frappe.db.get_value("Site Backup", backup, ["site", "status", "remote_path"], as_dict=True)
	if not row or row.site != doc.name:
		raise frappe.PermissionError(_("Backup {0} does not belong to site {1}").format(backup, doc.name))
	if row.status != "Success" or not row.remote_path:
		frappe.throw(_("Backup {0} cannot be restored").format(backup))

	method = getattr(doc, "restore", None)
	if callable(method):
		# Site.restore added by the operations work: pass what its signature asks for
		params = inspect.signature(method).parameters
		if "backup" in params:
			return method(backup=backup)
		if "remote_path" in params:
			return method(remote_path=row.remote_path)
		return method(backup)

	from nexaploy.agent_job import create_agent_job

	doc.check_permission("write")
	if doc.status not in ("Active", "Broken"):
		frappe.throw(_("Site {0} is {1}; it must be Active").format(doc.name, doc.status))
	return create_agent_job(
		doc.server, "site.restore", {"site": doc.site_name, "remote_path": row.remote_path}, doc.doctype, doc.name
	).name


@frappe.whitelist(methods=["POST"])
def site_action(name, action, args=None, **kwargs):
	"""Run a Site action: backup, install_app, uninstall_app, add_domain, remove_domain,
	migrate, drop, restore. Arguments come in `args` (dict/JSON) or as keyword arguments.
	Returns {action, result, job}."""
	c = _ctx()
	_check_team("Site", name, c, "write")
	merged = dict(frappe.parse_json(args) or {}) if args else {}
	merged.update({k: v for k, v in kwargs.items() if k not in ("cmd", "name", "action", "args")})
	try:
		clean = filter_action_args(action, merged)
	except ValueError as exc:
		frappe.throw(_(str(exc)))

	doc = frappe.get_doc("Site", name)
	if action == "restore":
		result = _restore(doc, clean["backup"])
	else:
		if "with_files" in clean:
			clean["with_files"] = int(to_bool(clean["with_files"], True))
		if "skip_backup" in clean:
			clean["skip_backup"] = int(to_bool(clean["skip_backup"]))
		result = getattr(doc, action)(**clean)

	job = None
	if action == "backup" and result:
		job = _job_for("Site Backup", result, "site.backup")
	elif isinstance(result, str) and frappe.db.exists("Agent Job", result):
		job = result
	return {"action": action, "result": result, "job": job}


# --------------------------------------------------------------------------- jobs


@frappe.whitelist()
def get_job(name):
	c = _ctx()
	job = frappe.db.get_value("Agent Job", name, JOB_FIELDS, as_dict=True)
	if not job:
		raise frappe.DoesNotExistError(_("Agent Job {0} not found").format(name))
	if not _job_allowed(job, c):
		raise frappe.PermissionError(_("Not allowed"))
	doc = frappe.get_doc("Agent Job", name)
	job["params"] = doc.get_redacted_params()
	job["result"] = redact_params(frappe.parse_json(doc.result) if doc.result else None)
	job["active"] = is_job_active(job.status)
	job["steps"] = [
		{
			"idx": s.step_idx,
			"name": s.step_name,
			"status": s.status,
			"output": s.output,
			"started_at": s.started_at,
			"ended_at": s.ended_at,
		}
		for s in sorted(doc.steps, key=lambda s: (s.step_idx or 0, s.idx))
	]
	return job


@frappe.whitelist()
def list_jobs(limit=50):
	c = _ctx()
	return _team_jobs(c, limit=min(int(limit or 50), 200))


# --------------------------------------------------------------------------- servers


@frappe.whitelist()
def list_servers(include_archived=False):
	c = _ctx()
	extra = {} if to_bool(include_archived) else {"status": ["!=", "Archived"]}
	servers = frappe.get_all(
		"Server",
		filters=_team_filters(c, **extra),
		fields=[
			"name", "server_id", "hostname", "ownership", "status", "plan", "team",
			"last_heartbeat", "agent_version", "creation",
		],
		order_by="creation desc",
	)
	names = [s.name for s in servers]
	bench_counts, site_counts = {}, {}
	if names:
		for b in frappe.get_all(
			"Bench", filters={"server": ["in", names], "status": ["!=", "Archived"]}, fields=["server"]
		):
			bench_counts[b.server] = bench_counts.get(b.server, 0) + 1
		for s in frappe.get_all(
			"Site", filters={"server": ["in", names], "status": ["!=", "Archived"]}, fields=["server"]
		):
			site_counts[s.server] = site_counts.get(s.server, 0) + 1
	now = now_datetime()
	for s in servers:
		s["benches"] = bench_counts.get(s.name, 0)
		s["sites"] = site_counts.get(s.name, 0)
		s["heartbeat_age"] = seconds_since(s.last_heartbeat, now)
	return {"servers": servers, "byos_plan": _byos_plan(), "can_add": role_allows(c.role, "admin")}


@frappe.whitelist()
def get_server(name):
	c = _ctx()
	_server_team, role = _check_team("Server", name, c, "read")
	s = frappe.db.get_value(
		"Server",
		name,
		[
			"name", "server_id", "hostname", "ownership", "status", "team", "plan", "is_build_server",
			"agent_version", "last_heartbeat", "facts", "benches_report", "creation",
		],
		as_dict=True,
	)
	s["facts"] = frappe.parse_json(s.facts) if s.facts else {}
	s["benches_report"] = frappe.parse_json(s.benches_report) if s.benches_report else None
	s["heartbeat_age"] = seconds_since(s.last_heartbeat, now_datetime())
	if s.plan:
		s["plan_info"] = frappe.db.get_value(
			"Nexaploy Plan", s.plan, ["name", "title", "price_monthly", "currency"], as_dict=True
		)
	benches = frappe.get_all(
		"Bench",
		filters={"server": name, "status": ["!=", "Archived"]},
		fields=["name", "bench_name", "release_group", "image_tag", "status", "creation"],
		order_by="creation desc",
	)
	majors = _group_majors({b.release_group for b in benches})
	counts = {}
	if benches:
		for site in frappe.get_all(
			"Site",
			filters={"bench": ["in", [b.name for b in benches]], "status": ["!=", "Archived"]},
			fields=["bench"],
		):
			counts[site.bench] = counts.get(site.bench, 0) + 1
	for b in benches:
		b["frappe_major"] = majors.get(b.release_group)
		b["sites"] = counts.get(b.name, 0)
	s["benches"] = benches
	s["jobs"] = _team_jobs(c, limit=20, server=name)
	s["role"] = role
	s["permissions"] = _permissions(role)
	return s


@frappe.whitelist(methods=["POST"])
def create_registration_token():
	"""Single-use BYOS registration token for the current team (needs the admin action, see
	Server Registration Token.generate_registration_token). The token is shown once."""
	c = _ctx("admin")
	from nexaploy.nexaploy.doctype.server_registration_token.server_registration_token import (
		generate_registration_token,
	)

	result = generate_registration_token(team=c.team)
	result["byos_plan"] = _byos_plan()
	return result


@frappe.whitelist()
def registration_status(name):
	"""Poll target for the "Add your own server" flow: has the token been used yet?"""
	c = _ctx()
	_check_team("Server Registration Token", name, c, "admin")
	row = frappe.db.get_value(
		"Server Registration Token", name, ["used", "server", "expires_on"], as_dict=True
	)
	server = None
	if row.server:
		server = frappe.db.get_value(
			"Server", row.server, ["name", "hostname", "status", "last_heartbeat"], as_dict=True
		)
	return {
		"used": bool(row.used),
		"expired": bool(row.expires_on and row.expires_on <= now_datetime()),
		"expires_on": row.expires_on,
		"server": server,
	}


# --------------------------------------------------------------------------- marketplace


@frappe.whitelist()
def marketplace(frappe_major=None):
	_ctx()
	from nexaploy.api.marketplace import list_apps

	major = str(frappe_major) if frappe_major not in (None, "", "all") else None
	if major and major not in SUPPORTED_MAJORS:
		frappe.throw(_("Unsupported Frappe version {0}").format(major))
	apps = list_apps(major)
	categories = sorted({a.get("category") for a in apps if a.get("category")})
	return {"apps": apps, "categories": categories, "majors": list(SUPPORTED_MAJORS)}


# --------------------------------------------------------------------------- www page context


def _portal_messages(lang):
	"""Translations of this app for JS __() (only the nexaploy CSV/PO, not all of frappe)."""
	if not lang or lang == "en":
		return {}
	try:
		from frappe.translate import get_translations_from_apps

		return get_translations_from_apps(lang, ["nexaploy"]) or {}
	except Exception:
		return {}


def prepare_page(context, page, title, require_role=True):
	"""Common get_context() for /dashboard/* pages: login + role gate, no cache, RTL, boot.

	Adds the current team to the context and NX_BOOT (`team`, `team_title`, `teams`,
	`role`, `permissions`) for the sidebar team switcher and role-based buttons.
	require_role=False lets a logged-in user without the portal role through (team page
	accepting an invitation, which grants the role)."""
	user = frappe.session.user
	if not user or user == "Guest":
		request = getattr(frappe.local, "request", None)
		path = request.path if request else "/dashboard"
		query = request.query_string if request else None
		frappe.local.flags.redirect_location = login_redirect_url(path, query)
		raise frappe.Redirect
	roles = frappe.get_roles(user)
	portal_access = has_portal_access(roles, user)
	if not portal_access and require_role:
		frappe.throw(_("You need the Nexaploy Customer role to use the dashboard"), frappe.PermissionError)

	lang = getattr(frappe.local, "lang", None) or "en"
	context.no_cache = 1
	context.show_sidebar = 0
	context.page = page
	context.title = title
	context.lang = lang
	context.dir = text_direction(lang)
	try:
		from frappe.sessions import get_csrf_token

		context.csrf_token = get_csrf_token()
	except Exception:
		context.csrf_token = getattr(frappe.local.session.data, "csrf_token", "") if frappe.local.session else ""
	context.user_fullname = frappe.utils.get_fullname(user)
	context.user = user
	context.is_admin = is_portal_admin(roles, user)
	context.root_domain = _root_domain()
	team, role, teams = None, None, []
	if portal_access:
		team = get_current_team(user)
		teams = get_user_teams(user)
		role = next((t["role"] for t in teams if t["name"] == team), None)
		if role is None and context.is_admin:
			role = "Owner"
		if team and not any(t["name"] == team for t in teams):
			teams.append({"name": team, "title": frappe.db.get_value("Team", team, "title") or team, "role": role})
	context.team = team
	context.team_role = role
	context.teams = teams
	context.boot = {
		"user": user,
		"fullname": context.user_fullname,
		"is_admin": context.is_admin,
		"lang": lang,
		"dir": context.dir,
		"root_domain": context.root_domain,
		"page": page,
		"messages": _portal_messages(lang),
		"team": team,
		"team_title": next((t["title"] for t in teams if t["name"] == team), team),
		"teams": teams,
		"role": role,
		"permissions": _permissions(role),
		"portal_access": portal_access,
	}
	return context
