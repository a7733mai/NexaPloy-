"""Site lifecycle for the customer portal: staging sites, moves between servers, transfers
between teams and renames (ARCHITECTURE.md §10, owner: lifecycle).

Access goes through nexaploy.teams.check_team_access on the site's team (§9):
read -> get_lifecycle, write -> staging / move, admin -> transfer / rename. Every mutation
is recorded with nexaploy.activity.log_activity. Pure logic lives in
nexaploy.lifecycle_utils; the agent job chains live on the Site controller.

    get_lifecycle(site)
    create_staging(site) / refresh_staging(site)        # site.clone; drop with site_action drop
    move_site(site, target_bench, allow_upgrade=0)        # Site.move_to_bench
    request_transfer(site, email_or_team) / cancel_transfer(name)
    get_transfer(token) / accept_transfer(token, team=None) / reject_transfer(token)
    rename_site(site, new_subdomain_or_name, keep_old_domain=1)   # site.rename
"""

import frappe
from frappe import _
from frappe.utils import get_url, now_datetime

from nexaploy import lifecycle_utils as lu

TRANSFER = "Site Transfer"
SITE_FIELDS = ("name", "site_name", "team", "bench", "server", "status", "is_staging", "staging_of", "staging_site")


# --------------------------------------------------------------------------- helpers


def _require_user():
	user = frappe.session.user
	if not user or user == "Guest":
		raise frappe.PermissionError(_("Please log in"))
	return user


def _check(team, action):
	from nexaploy.teams import check_team_access

	return check_team_access(team, action)


def _site(site, action="read"):
	_require_user()
	if not site:
		frappe.throw(_("Site is required"))
	row = frappe.db.get_value("Site", site, list(SITE_FIELDS), as_dict=True)
	if not row:
		raise frappe.DoesNotExistError(_("Site {0} not found").format(site))
	role = _check(row.team, action)
	row.role = role
	return row


def _log(team, action, site=None, details=None, doctype="Site"):
	"""nexaploy.activity.log_activity when available (never raises)."""
	try:
		from nexaploy.activity import log_activity
	except ImportError:
		return
	try:
		log_activity(team, action, doctype if site else None, site, details)
	except Exception:
		pass


def _value_error(fn, *args, **kwargs):
	try:
		return fn(*args, **kwargs)
	except ValueError as exc:
		frappe.throw(_(str(exc)))


def _root_domain():
	from nexaploy.api.dashboard import _root_domain as root

	return root()


def _is_admin():
	from nexaploy.teams import is_platform_admin

	return is_platform_admin()


def _role_allows(role, action):
	from nexaploy.team_utils import role_allows

	return role_allows(role, action)


def _major(bench):
	group = frappe.db.get_value("Bench", bench, "release_group")
	return frappe.db.get_value("Release Group", group, "frappe_major") if group else None


def _hosting(bench):
	"""{bench_team, server_team, ownership, server, hostname} of a bench."""
	b = frappe.db.get_value("Bench", bench, ["team", "server"], as_dict=True) or frappe._dict()
	s = frappe.db.get_value(
		"Server", b.server, ["team", "ownership", "hostname", "status", "facts"], as_dict=True
	) if b.server else None
	s = s or frappe._dict()
	return frappe._dict(
		bench_team=b.team, server=b.server, server_team=s.team, ownership=s.ownership,
		hostname=s.hostname, server_status=s.status, facts=s.facts,
	)


def _server_ip(facts):
	facts = frappe.parse_json(facts or "{}") or {}
	if not isinstance(facts, dict):
		return None
	return facts.get("public_ip") or facts.get("ip") or facts.get("ipv4")


# --------------------------------------------------------------------------- overview


def _move_targets(row):
	"""Active benches the site may move to (team's own benches/servers + shared hosted)."""
	source_major = _major(row.bench)
	benches = frappe.get_all(
		"Bench",
		filters={"status": "Active", "name": ["!=", row.bench]},
		fields=["name", "bench_name", "server", "release_group", "team"],
		order_by="creation desc",
	)
	servers = {}
	if benches:
		for s in frappe.get_all(
			"Server",
			filters={"name": ["in", list({b.server for b in benches})]},
			fields=["name", "hostname", "ownership", "team", "status"],
		):
			servers[s.name] = s
	groups = {b.release_group for b in benches if b.release_group}
	majors = {}
	if groups:
		for g in frappe.get_all("Release Group", filters={"name": ["in", list(groups)]}, fields=["name", "frappe_major"]):
			majors[g.name] = g.frappe_major
	out = []
	for b in benches:
		s = servers.get(b.server) or frappe._dict()
		if s.status not in ("Active", "Offline"):
			continue
		own = bool(row.team) and (b.team == row.team or s.team == row.team)
		shared = lu.is_shared_hosted(b.team, s.team, s.ownership)
		if not (own or shared or _is_admin()):
			continue
		major = majors.get(b.release_group)
		compat = lu.move_compatibility(source_major, major, allow_upgrade=True)
		out.append(
			{
				"name": b.name,
				"bench_name": b.bench_name,
				"server": b.server,
				"server_hostname": s.hostname,
				"frappe_major": major,
				"same_server": b.server == row.server,
				"shared": shared,
				"compatible": compat["ok"],
				"upgrade": compat["upgrade"],
				"reason": compat["reason"],
			}
		)
	return out


@frappe.whitelist()
def get_lifecycle(site):
	"""Everything the portal's Lifecycle tab needs."""
	row = _site(site, "read")
	can_write = _role_allows(row.role, "write")
	can_admin = _role_allows(row.role, "admin")
	staging = None
	if row.staging_site:
		s = frappe.db.get_value("Site", row.staging_site, ["name", "status", "creation", "modified"], as_dict=True)
		if s and s.status != "Archived":
			staging = {**s, "url": f"https://{s.name}"}
	hosting = _hosting(row.bench)
	transfers = []
	if can_admin:
		transfers = frappe.get_all(
			TRANSFER,
			filters={"site": row.name, "status": "Pending"},
			fields=["name", "to_team", "to_email", "expires_on", "requested_by", "creation"],
			order_by="creation desc",
		)
	try:
		staging_name = lu.staging_site_name(row.site_name)
	except ValueError:
		staging_name = None
	return {
		"site": row.name,
		"status": row.status,
		"is_staging": bool(row.is_staging),
		"staging_of": row.staging_of,
		"staging": staging,
		"staging_name": staging_name,
		"frappe_major": _major(row.bench),
		"move_targets": _move_targets(row) if can_write and not row.is_staging else [],
		"backup_storage": _backup_storage(),
		"transfers": transfers,
		"transfer_block_reason": lu.transfer_block_reason(hosting.bench_team, hosting.server_team, hosting.ownership),
		"root_domain": _root_domain(),
		"custom_names": bool(_is_admin() or (row.team and row.team in (hosting.bench_team, hosting.server_team))),
		"can_write": can_write,
		"can_admin": can_admin,
	}


def _backup_storage():
	from nexaploy.nexaploy.doctype.site.site import Site

	return Site.backup_storage_configured()


# --------------------------------------------------------------------------- staging


@frappe.whitelist(methods=["POST"])
def create_staging(site):
	"""Create (or re-create after a drop) the staging copy `staging-<site>` on the same bench:
	site.clone with scrub (emails muted, scheduler paused, email accounts disabled)."""
	row = _site(site, "write")
	if row.is_staging:
		frappe.throw(_("{0} is a staging site").format(row.name))
	if row.status != "Active":
		frappe.throw(_("Site {0} is {1}; it must be Active").format(row.name, _(row.status)))
	if row.staging_site:
		current = frappe.db.get_value("Site", row.staging_site, "status")
		if current and current != "Archived":
			frappe.throw(_("{0} already has a staging site: {1}").format(row.name, row.staging_site))
	name = _value_error(lu.staging_site_name, row.site_name)
	source = frappe.get_doc("Site", row.name)
	apps = [r.app for r in source.apps if r.status == "Installed"]

	existing = frappe.db.get_value("Site", name, ["name", "is_staging", "staging_of", "status"], as_dict=True)
	if existing:
		if not (existing.is_staging and existing.staging_of == row.name and existing.status == "Archived"):
			frappe.throw(_("{0} is already taken").format(name))
		doc = frappe.get_doc("Site", name)
		doc.bench, doc.server, doc.team, doc.status = source.bench, source.server, source.team, "Pending"
		doc.set("apps", [{"app": a, "status": "Pending"} for a in apps])
		doc.set("domains", [])
		doc.flags.ignore_permissions = True
		doc.save()
		job = doc.enqueue_clone(row.name)
	else:
		if frappe.db.exists("Site Domain", {"domain": name, "parenttype": "Site"}):
			frappe.throw(_("{0} is already taken").format(name))
		from nexaploy.portal_utils import generate_password

		doc = frappe.get_doc(
			{
				"doctype": "Site",
				"site_name": name,
				"bench": source.bench,
				"server": source.server,
				"team": source.team,
				"plan": None,
				"is_staging": 1,
				"staging_of": row.name,
				"backup_frequency": "Off",
				# replaced by the production database on restore; only used if someone resets it
				"admin_password": generate_password(),
				"apps": [{"app": a} for a in apps],
			}
		)
		doc.flags.clone_from = row.name
		doc.insert(ignore_permissions=True)
		job = frappe.db.get_value(
			"Agent Job", {"reference_doctype": "Site", "reference_name": doc.name, "kind": "site.clone"}, "name",
			order_by="creation desc",
		)
	frappe.db.set_value("Site", row.name, "staging_site", doc.name)
	_log(row.team, "site.staging_create", row.name, {"staging_site": doc.name})
	return {"name": doc.name, "url": f"https://{doc.name}", "job": job}


@frappe.whitelist(methods=["POST"])
def refresh_staging(site):
	"""Re-clone the staging site from production (its data is replaced). `site` may be the
	production site or the staging site itself."""
	row = _site(site, "write")
	staging_name = row.name if row.is_staging else row.staging_site
	if not staging_name:
		frappe.throw(_("{0} has no staging site").format(row.name))
	staging = frappe.get_doc("Site", staging_name)
	production = frappe.db.get_value("Site", staging.staging_of, ["name", "status", "team"], as_dict=True)
	if not staging.is_staging or not production:
		frappe.throw(_("{0} is not a staging site").format(staging_name))
	_check(production.team, "write")
	if production.status != "Active":
		frappe.throw(_("Site {0} is {1}; it must be Active").format(production.name, _(production.status)))
	if staging.status not in ("Active", "Broken"):
		frappe.throw(_("Staging site {0} is {1}").format(staging.name, _(staging.status)))
	staging.db_set("status", "Pending")
	job = staging.enqueue_clone(production.name)
	_log(production.team, "site.staging_refresh", production.name, {"staging_site": staging.name})
	return {"name": staging.name, "job": job}


# --------------------------------------------------------------------------- move


@frappe.whitelist(methods=["POST"])
def move_site(site, target_bench, allow_upgrade=0):
	"""Move a site to another bench of its team (or a shared hosted bench), on this or
	another server. A higher Frappe major needs allow_upgrade=1; downgrades are refused.
	Cross-server moves need backup storage. Returns the move plan and, for another
	server, a `dns_hint` (every host of the site must point to the new server)."""
	row = _site(site, "write")
	allow = bool(frappe.parse_json(allow_upgrade)) if isinstance(allow_upgrade, str) else bool(allow_upgrade)
	doc = frappe.get_doc("Site", row.name)
	result = doc.move_to_bench(target_bench, allow_upgrade=allow)
	hint = None
	if not result.get("same_server"):
		hosting = _hosting(target_bench)
		hint = lu.dns_hint(
			doc.site_name, [d.domain for d in doc.domains], _root_domain(), hosting.hostname, _server_ip(hosting.facts)
		)
	result["dns_hint"] = hint
	result["job"] = frappe.db.get_value(
		"Agent Job", {"reference_doctype": "Site Backup", "reference_name": result.get("backup")}, "name"
	)
	_log(row.team, "site.move", row.name, {"target_bench": target_bench, "from_bench": row.bench,
		"upgrade": result.get("upgrade"), "same_server": result.get("same_server")})
	return result


# --------------------------------------------------------------------------- transfer


def _transfer_by_token(token):
	if not token:
		frappe.throw(_("Transfer token is required"))
	name = frappe.db.get_value(TRANSFER, {"token_hash": lu.hash_transfer_token(token)}, "name")
	if not name:
		raise frappe.DoesNotExistError(_("This transfer link is invalid"))
	return frappe.get_doc(TRANSFER, name)


def _transfer_blocked(site_doc):
	h = _hosting(site_doc.bench)
	return lu.transfer_block_reason(h.bench_team, h.server_team, h.ownership)


def _send_transfer_email(recipients, site, from_title, url):
	if not recipients:
		return False
	try:
		frappe.sendmail(
			recipients=recipients,
			subject=_("Site transfer: {0}").format(site),
			message=_(
				"<p>Team <b>{0}</b> wants to transfer the site <b>{1}</b> to you.</p>"
				"<p><a href=\"{2}\">Review and accept the transfer</a> (the link expires in {3} days).</p>"
				"<p>Billing for the site moves to the team that accepts it.</p>"
			).format(frappe.utils.escape_html(from_title), site, url, lu.TRANSFER_TTL_DAYS),
			now=True,
		)
		return True
	except Exception:
		frappe.log_error(title=f"Nexaploy: site transfer email for {site} failed")
		return False


@frappe.whitelist(methods=["POST"])
def request_transfer(site, email_or_team):
	"""Offer a site to another team (by team name) or person (by email). Team admin.
	Returns {name, transfer_url, email_sent, expires_on}; the link is shown once."""
	row = _site(site, "admin")
	if row.is_staging:
		frappe.throw(_("Staging sites move together with their production site"))
	if row.status != "Active":
		frappe.throw(_("Site {0} is {1}; it must be Active").format(row.name, _(row.status)))
	doc = frappe.get_doc("Site", row.name)
	reason = _transfer_blocked(doc)
	if reason:
		frappe.throw(_(reason))
	kind, value = _value_error(lu.parse_transfer_target, email_or_team)
	to_team, to_email = (value, None) if kind == "team" else (None, value)
	if to_team:
		if not frappe.db.exists("Team", to_team):
			frappe.throw(_("Team {0} not found").format(to_team))
		if to_team == row.team:
			frappe.throw(_("The site already belongs to team {0}").format(to_team))

	for old in frappe.get_all(TRANSFER, filters={"site": row.name, "status": "Pending"}, pluck="name"):
		frappe.db.set_value(TRANSFER, old, "status", "Cancelled")

	token = lu.generate_transfer_token()
	tr = frappe.get_doc(
		{
			"doctype": TRANSFER,
			"site": row.name,
			"from_team": row.team,
			"to_team": to_team,
			"to_email": to_email,
			"status": "Pending",
			"expires_on": lu.transfer_expiry(now_datetime()),
			"requested_by": frappe.session.user,
			"token_hash": lu.hash_transfer_token(token),
		}
	)
	tr.insert(ignore_permissions=True)
	url = lu.transfer_url(get_url(), token)
	if to_team:
		from nexaploy.teams import team_notification_emails

		recipients = team_notification_emails(to_team, "ops")
	else:
		recipients = [to_email]
	from_title = frappe.db.get_value("Team", row.team, "title") or row.team
	sent = _send_transfer_email(recipients, row.name, from_title, url)
	_log(row.team, "site.transfer_request", row.name, {"to_team": to_team, "to_email": to_email, "transfer": tr.name})
	return {"name": tr.name, "transfer_url": url, "email_sent": sent, "expires_on": str(tr.expires_on)}


@frappe.whitelist(methods=["POST"])
def cancel_transfer(name):
	"""Cancel a pending transfer (admin of the site's current team)."""
	_require_user()
	tr = frappe.db.get_value(TRANSFER, name, ["name", "site", "from_team", "status"], as_dict=True)
	if not tr:
		raise frappe.DoesNotExistError(_("Transfer {0} not found").format(name))
	_check(tr.from_team, "admin")
	if tr.status != "Pending":
		frappe.throw(_("This transfer is {0}").format(_(tr.status)))
	frappe.db.set_value(TRANSFER, tr.name, "status", "Cancelled")
	_log(tr.from_team, "site.transfer_cancel", tr.site, {"transfer": tr.name})
	return {"name": tr.name, "status": "Cancelled"}


def _user_email(user):
	return (frappe.db.get_value("User", user, "email") or user).lower()


def _admin_teams(user):
	from nexaploy.teams import get_user_teams

	return [t for t in get_user_teams(user) if _role_allows(t["role"], "admin")]


@frappe.whitelist()
def get_transfer(token):
	"""Preview for /dashboard/transfer?token=: site, teams, status, where it can go."""
	user = _require_user()
	tr = _transfer_by_token(token)
	status = tr.status
	if status == "Pending" and lu.transfer_expired(tr.expires_on, now_datetime()):
		status = "Expired"
	admin_teams = [t for t in _admin_teams(user) if t["name"] != tr.from_team]
	if tr.to_team:
		admin_teams = [t for t in admin_teams if t["name"] == tr.to_team]
	matches = bool(admin_teams) and (not tr.to_email or tr.to_email in (_user_email(user), user.lower()))
	site = frappe.db.get_value("Site", tr.site, ["name", "status", "bench"], as_dict=True) or frappe._dict()
	reason = _transfer_blocked(frappe._dict(bench=site.bench)) if site.bench else None
	return {
		"site": tr.site,
		"site_status": site.status,
		"from_team_title": frappe.db.get_value("Team", tr.from_team, "title") or tr.from_team,
		"to_team": tr.to_team,
		"to_team_title": frappe.db.get_value("Team", tr.to_team, "title") if tr.to_team else None,
		"to_email": tr.to_email,
		"status": status,
		"expires_on": str(tr.expires_on),
		"matches": matches,
		"teams": admin_teams,
		"block_reason": reason,
	}


@frappe.whitelist(methods=["POST"])
def accept_transfer(token, team=None):
	"""Take over the site: Site.team (and its staging site, backups and usage rows) move to
	the target team; billing follows through the Site on_update hook. The user must be an
	Owner/Admin of the target team (and the addressee when the transfer was sent by email)."""
	user = _require_user()
	tr = _transfer_by_token(token)
	frappe.db.get_value(TRANSFER, tr.name, "name", for_update=True)
	tr.reload()
	if tr.status != "Pending":
		frappe.throw(_("This transfer is {0}").format(_(tr.status)))
	if lu.transfer_expired(tr.expires_on, now_datetime()):
		frappe.throw(_("This transfer has expired; ask for a new one"))
	if tr.to_email and tr.to_email not in (_user_email(user), user.lower()):
		raise frappe.PermissionError(_("This transfer was sent to {0}").format(tr.to_email))
	if tr.to_team:
		target = tr.to_team
	else:
		from nexaploy.teams import get_current_team

		target = team or get_current_team(user)
	_check(target, "admin")
	if target == tr.from_team:
		frappe.throw(_("The site already belongs to this team"))
	if not frappe.db.get_value("Team", target, "enabled"):
		frappe.throw(_("Team {0} is disabled").format(target))

	site = frappe.get_doc("Site", tr.site)
	if site.team != tr.from_team:
		frappe.db.set_value(TRANSFER, tr.name, "status", "Cancelled")
		frappe.throw(_("The site no longer belongs to the team that offered it"))
	if site.status not in ("Active", "Suspended"):
		frappe.throw(_("Site {0} is {1}; try again when it is Active").format(site.name, _(site.status)))
	reason = _transfer_blocked(site)
	if reason:
		frappe.throw(_(reason))

	names = [site.name]
	if site.staging_site and frappe.db.exists("Site", site.staging_site):
		names.append(site.staging_site)
	for name in names:
		doc = frappe.get_doc("Site", name)
		doc.team = target
		doc.flags.ignore_permissions = True
		doc.save()  # on_update -> billing subscription follows the new team
		for doctype in ("Site Backup", "Site Usage"):
			if frappe.db.has_column(doctype, "team"):
				frappe.db.sql(f"update `tab{doctype}` set team=%s where site=%s", (target, name))
	tr.db_set({"status": "Accepted", "to_team": target, "accepted_by": user, "accepted_on": now_datetime()})
	details = {"from_team": tr.from_team, "to_team": target, "transfer": tr.name}
	_log(tr.from_team, "site.transfer", site.name, details)
	_log(target, "site.transfer", site.name, details)
	try:
		from nexaploy.teams import set_team_cookie

		set_team_cookie(target)
	except Exception:
		pass
	return {"site": site.name, "team": target}


@frappe.whitelist(methods=["POST"])
def reject_transfer(token):
	"""Decline a transfer addressed to you or a team you administer."""
	user = _require_user()
	tr = _transfer_by_token(token)
	if tr.status != "Pending":
		frappe.throw(_("This transfer is {0}").format(_(tr.status)))
	if tr.to_team:
		_check(tr.to_team, "admin")
	elif tr.to_email not in (_user_email(user), user.lower()):
		raise frappe.PermissionError(_("This transfer was sent to {0}").format(tr.to_email))
	tr.db_set("status", "Rejected")
	_log(tr.from_team, "site.transfer_reject", tr.site, {"transfer": tr.name, "by": user})
	return {"name": tr.name, "status": "Rejected"}


# --------------------------------------------------------------------------- rename


@frappe.whitelist(methods=["POST"])
def rename_site(site, new_subdomain_or_name, keep_old_domain=1):
	"""Rename a site (team admin): site.rename on the agent (same database and files, new
	directory, host_name and route); on success the Site document is renamed and the old
	name stays as a domain so existing links keep working (keep_old_domain)."""
	row = _site(site, "admin")
	if row.is_staging:
		frappe.throw(_("Staging sites cannot be renamed"))
	if row.status != "Active":
		frappe.throw(_("Site {0} is {1}; it must be Active").format(row.name, _(row.status)))
	hosting = _hosting(row.bench)
	own = bool(row.team and row.team in (hosting.bench_team, hosting.server_team))
	from nexaploy.portal_utils import build_site_name

	new_name = _value_error(build_site_name, new_subdomain_or_name, _root_domain(), allow_custom=own or _is_admin())
	new_name = _value_error(lu.validate_rename, row.site_name, new_name)
	if frappe.db.exists("Site", new_name):
		frappe.throw(_("{0} is already taken").format(new_name))
	other = frappe.db.get_value("Site Domain", {"domain": new_name, "parenttype": "Site"}, "parent")
	if other and other != row.name:
		frappe.throw(_("{0} is already taken").format(new_name))
	keep = bool(frappe.parse_json(keep_old_domain)) if isinstance(keep_old_domain, str) else bool(keep_old_domain)

	from nexaploy.agent_job import create_agent_job

	bench_name = frappe.db.get_value("Bench", row.bench, "bench_name")
	job = create_agent_job(
		row.server, "site.rename", lu.rename_params(bench_name, row.site_name, new_name, keep), "Site", row.name
	)
	frappe.db.set_value("Site", row.name, "status", "Renaming")
	_log(row.team, "site.rename", row.name, {"new_name": new_name, "keep_old_domain": keep})
	hint = None
	if not new_name.endswith("." + _root_domain()):
		hint = lu.dns_hint(new_name, [], _root_domain(), hosting.hostname, _server_ip(hosting.facts))
	return {"name": row.name, "new_name": new_name, "job": job.name, "dns_hint": hint}
