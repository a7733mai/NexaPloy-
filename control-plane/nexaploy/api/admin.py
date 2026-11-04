"""Platform Administrator dashboard API (/dashboard/admin; pure logic in nexaploy/admin_utils.py).

Every whitelisted method starts with `_admin()`, which raises frappe.PermissionError unless the
session user is a platform admin (nexaploy.teams.is_platform_admin: Administrator / System
Manager) - the browser is never trusted. Secrets are write-only: reads return {set, hint}
(masked, see admin_utils.mask_secret), writes take a `secrets` map where "" keeps the stored
value and "__clear__" removes it. Every change is recorded with
nexaploy.activity.log_platform_activity (the affected team, else the platform marker).

| Section | Methods |
|---|---|
| Overview | overview(), list_platform_activity(limit) |
| Features & pricing | get_features(), save_plan(values, name), update_platform_settings(values, secrets), update_billing_settings(values, secrets), update_storage_settings(values, addon_prices) |
| Storage & backups | get_storage(), save_storage_profile(values, team), delete_storage_profile(name), test_storage_profile(name, server), use_profile_for_backups(profile), update_oauth_apps(values, secrets), get_job(name) |
| E-mail & brands | get_email_brands(), update_email_relay(values, secrets), save_brand(values, name), delete_brand(name) |
| Agent releases | get_agents(), save_agent_release(values, name), set_release_published(name, published), upgrade_outdated_agents(channel, dry_run), upgrade_server_agent(server) |
| Marketplace | get_marketplace(), review_listing(app, action, notes), set_release_status(app, frappe_major, status, note), verify_release(app, frappe_major), hide_review(review, hidden, reason) |
| Teams & servers | list_teams(query), set_team_flags(team, values), suspend_team(team), unsuspend_team(team), grant_credits(team, amount, type, description), list_servers() |
"""

import json

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, get_url, now_datetime

from nexaploy import admin_utils as au

NEXAPLOY_SETTINGS = "Nexaploy Settings"
BILLING_SETTINGS = "Billing Settings"
EMAIL_SETTINGS = "Email Relay Settings"
PLAN = "Nexaploy Plan"
BRAND = "Brand"
RELEASE = "Agent Release"
SERVER_FIELDS = [
	"name", "hostname", "team", "status", "ownership", "last_heartbeat", "disk_used_percent", "agent_version",
	"agent_channel", "agent_upgrade_status", "agent_upgrade_error", "hosts_control_plane", "is_build_server",
	"cpu_count", "memory_total_mb", "disk_total_gb", "unhealthy_containers", "public_ipv4",
]
BRAND_LIST_FIELDS = ["name", "title", "enabled", "team", "is_default", "primary_color", "logo", "favicon",
                     "portal_domains", "support_email", "footer_text", "email_sender_name", "email_sender_address"]
TEST_RESULT_KINDS = ("backup.test_remote",)


# --------------------------------------------------------------------------- access & helpers


def _admin():
	"""The session user, or frappe.PermissionError unless a platform admin."""
	from nexaploy.teams import is_platform_admin

	user = frappe.session.user
	if not user or user == "Guest":
		raise frappe.PermissionError(_("Please log in"))
	if not is_platform_admin(user):
		raise frappe.PermissionError(_("Only platform administrators can do this"))
	return user


def _parse(value):
	if value in (None, ""):
		return {}
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except ValueError:
			frappe.throw(_("Invalid request"))
	if not isinstance(value, dict):
		frappe.throw(_("Invalid request"))
	return value


def _throw(exc):
	if isinstance(exc, au.AdminError):
		frappe.throw(_(exc.message).format(*exc.params))
	frappe.throw(_(str(exc)))


def _clean(values, spec):
	try:
		return au.clean_fields(_parse(values), spec)
	except au.AdminError as exc:
		_throw(exc)


def _secrets(secrets, allowed):
	try:
		return au.split_secrets(_parse(secrets), allowed)
	except au.AdminError as exc:
		_throw(exc)


def _log(action, doctype=None, name=None, details=None, team=None):
	from nexaploy.activity import log_platform_activity

	log_platform_activity(action, doctype, name, details, team=team)


def _single(doctype, fields, secrets=()):
	doc = frappe.get_single(doctype)
	values = {f: doc.get(f) for f in fields}
	plain = {s: doc.get_password(s, raise_exception=False) for s in secrets}
	return values, au.secret_states(plain, secrets)


def _save_single(doctype, values, secrets, clear, action):
	"""Apply cleaned values + write-only secrets to a single and log the change."""
	doc = frappe.get_single(doctype)
	before = {k: doc.get(k) for k in values}
	doc.update(values)
	for field, value in secrets.items():
		doc.set(field, value)
	for field in clear:
		doc.set(field, None)
	doc.flags.ignore_permissions = True
	doc.save()
	if clear:
		from frappe.utils.password import remove_encrypted_password

		for field in clear:
			remove_encrypted_password(doctype, doctype, field)
	frappe.clear_document_cache(doctype, doctype)
	changes = au.change_summary(before, values)
	changes.update({f: "changed" for f in secrets})
	changes.update({f: "cleared" for f in clear})
	_log(action, doctype, doctype, changes)
	return changes


def _group_count(doctype, filters=None, field="team"):
	# counted in Python: Frappe v16 rejects SQL functions as strings in `fields` and v15 has no
	# dict syntax for them; these per-team counts are small
	counts = {}
	for value in frappe.get_all(doctype, filters=filters or {}, pluck=field):
		counts[value] = counts.get(value, 0) + 1
	return counts


def _team_exists(team):
	if not team or not frappe.db.exists("Team", team):
		raise frappe.DoesNotExistError(_("Team {0} not found").format(team))
	return team


# --------------------------------------------------------------------------- 1. overview


@frappe.whitelist()
def overview():
	"""Counts and health across the platform (servers, sites, failed jobs, tickets, revenue)."""
	_admin()
	from nexaploy import agent_upgrades

	now = now_datetime()
	servers = frappe.get_all("Server", fields=SERVER_FIELDS)
	releases = agent_upgrades.published_releases()
	groups = {g.name: g.frappe_major for g in frappe.get_all("Release Group", fields=["name", "frappe_major"])}
	bench_major = {b.name: groups.get(b.release_group) for b in frappe.get_all("Bench", fields=["name", "release_group"])}
	sites = frappe.get_all("Site", fields=["name", "status", "bench"])
	jobs = frappe.get_all(
		"Agent Job",
		filters={"status": "Failure", "creation": [">=", add_to_date(now, hours=-24)]},
		fields=["name", "kind", "server", "reference_doctype", "reference_name", "error", "creation"],
		order_by="creation desc", limit=500,
	)
	tickets = frappe.get_all(
		"Support Ticket", filters={"status": ["in", list(au.OPEN_TICKET_STATUSES)]},
		fields=["status", "priority", "first_response_due", "first_response_at"],
	)
	currency = frappe.db.get_single_value(BILLING_SETTINGS, "currency") or "USD"
	subs = frappe.get_all("Nexaploy Subscription", filters={"status": "Active"},
	                      fields=["team", "status", "price_monthly", "currency"])
	exempt = frappe.get_all("Team", filters={"billing_exempt": 1}, pluck="name")
	invoices = frappe.get_all("Nexaploy Invoice", filters={"status": "Overdue"},
	                          fields=["team", "status", "currency", "amount_due"])
	return {
		"servers": au.server_summary(servers, releases, now),
		"sites": au.site_summary(sites, bench_major),
		"failed_jobs": au.failed_jobs(jobs),
		"tickets": au.ticket_summary(tickets, now),
		"mrr": au.mrr(subs, exempt, currency),
		"overdue": au.overdue_summary(invoices, currency),
		"teams": frappe.db.count("Team"),
		"marketplace_pending": frappe.db.count("Marketplace App", {"status": "In Review"}),
		"currency": currency,
		"activity": _platform_activity(10),
	}


def _platform_activity(limit):
	return frappe.get_all(
		"Team Activity",
		filters={"team": ["is", "not set"]},
		fields=["name", "user", "action", "reference_doctype", "reference_name", "details", "creation"],
		order_by="creation desc", limit=limit,
	)


@frappe.whitelist()
def list_platform_activity(limit=50):
	"""Recent platform-level admin changes (Team Activity rows without a team)."""
	_admin()
	return _platform_activity(min(max(cint(limit) or 50, 1), 500))


# --------------------------------------------------------------------------- 2. features & pricing


def _plans():
	return frappe.get_all(PLAN, fields=["name", *au.PLAN_FIELDS], order_by="plan_type asc, plan_order asc, price_monthly asc")


@frappe.whitelist()
def get_features():
	"""Plans (incl. add-ons), platform settings, billing settings and storage feature switches."""
	_admin()
	from nexaploy.api import storage as storage_api

	platform, platform_secrets = _single(NEXAPLOY_SETTINGS, au.PLATFORM_FIELDS, au.PLATFORM_SECRETS)
	billing, billing_secrets = _single(BILLING_SETTINGS, au.BILLING_FIELDS, au.BILLING_SECRETS)
	return {
		"plans": _plans(),
		"plan_types": list(au.PLAN_TYPES),
		"overage_actions": list(au.OVERAGE_ACTIONS),
		"platform": platform,
		"platform_secrets": platform_secrets,
		"billing": billing,
		"billing_secrets": billing_secrets,
		"storage": storage_api._settings_view(),
		"extended_retention_teams": frappe.db.count("Team", {"extended_backup_retention": 1}),
	}


@frappe.whitelist(methods=["POST"])
def save_plan(values, name=None):
	"""Create or update a Nexaploy Plan (price, limits, enabled; add-ons too)."""
	_admin()
	try:
		clean = au.clean_plan(_parse(values), new=not name)
	except au.AdminError as exc:
		_throw(exc)
	if name:
		if not frappe.db.exists(PLAN, name):
			raise frappe.DoesNotExistError(_("Plan {0} not found").format(name))
		doc = frappe.get_doc(PLAN, name)
		clean.pop("title", None)  # the title is the plan's name
		before = {k: doc.get(k) for k in clean}
		doc.update(clean)
		doc.save(ignore_permissions=True)
		_log("platform.plan_update", PLAN, doc.name, au.change_summary(before, clean))
	else:
		doc = frappe.get_doc({"doctype": PLAN, **clean})
		doc.insert(ignore_permissions=True)
		_log("platform.plan_create", PLAN, doc.name, clean)
	return _plans()


@frappe.whitelist(methods=["POST"])
def update_platform_settings(values=None, secrets=None):
	"""Nexaploy Settings: backup retention limits, agent upgrades, alert e-mail, registry."""
	_admin()
	clean = _clean(values, au.PLATFORM_FIELDS)
	new, clear = _secrets(secrets, au.PLATFORM_SECRETS)
	current, _states = _single(NEXAPLOY_SETTINGS, au.PLATFORM_FIELDS)
	try:
		au.check_retention_limits({**current, **clean})
	except au.AdminError as exc:
		_throw(exc)
	changes = _save_single(NEXAPLOY_SETTINGS, clean, new, clear, "platform.settings_update")
	return {"changes": changes, **dict(zip(("platform", "platform_secrets"),
	                                       _single(NEXAPLOY_SETTINGS, au.PLATFORM_FIELDS, au.PLATFORM_SECRETS)))}


@frappe.whitelist(methods=["POST"])
def update_billing_settings(values=None, secrets=None):
	"""Billing Settings: currency, grace/suspension days, tax, gateways (secrets write-only)."""
	_admin()
	clean = _clean(values, au.BILLING_FIELDS)
	new, clear = _secrets(secrets, au.BILLING_SECRETS)
	current, _states = _single(BILLING_SETTINGS, au.BILLING_FIELDS)
	try:
		au.check_billing({**current, **clean})
	except au.AdminError as exc:
		_throw(exc)
	for field in ("default_byos_plan", "default_site_plan"):
		if clean.get(field) and not frappe.db.exists(PLAN, clean[field]):
			frappe.throw(_("Plan {0} not found").format(clean[field]))
	changes = _save_single(BILLING_SETTINGS, clean, new, clear, "platform.billing_update")
	return {"changes": changes, **dict(zip(("billing", "billing_secrets"),
	                                       _single(BILLING_SETTINGS, au.BILLING_FIELDS, au.BILLING_SECRETS)))}


@frappe.whitelist(methods=["POST"])
def update_storage_settings(values=None, addon_prices=None):
	"""Storage Settings + add-on prices (nexaploy.api.storage.update_storage_settings), logged."""
	_admin()
	from nexaploy.api import storage as storage_api

	values, addon_prices = _parse(values), _parse(addon_prices)
	view = storage_api.update_storage_settings(values=values, addon_prices=addon_prices)
	_log("platform.storage_settings_update", "Storage Settings", "Storage Settings",
	     {"values": values, "addon_prices": addon_prices})
	return view


# --------------------------------------------------------------------------- 3. storage & backups


@frappe.whitelist()
def get_storage():
	"""Storage profiles, platform backup storage and the OAuth apps for Drive/OneDrive/Dropbox."""
	_admin()
	from nexaploy import backup_destination_utils as bdu
	from nexaploy.api import storage as storage_api

	oauth, oauth_secrets = _single(NEXAPLOY_SETTINGS, au.OAUTH_FIELDS, au.OAUTH_SECRETS)
	backup, backup_secret = _single(
		NEXAPLOY_SETTINGS,
		("backup_provider", "backup_endpoint", "backup_region", "backup_bucket", "backup_prefix", "backup_access_key"),
		("backup_secret_key",),
	)
	servers = frappe.get_all("Server", filters={"status": "Active"},
	                         fields=["name", "hostname", "team", "ownership", "hosts_control_plane", "status"])
	return {
		**storage_api._settings_view(),
		"oauth": oauth,
		"oauth_secrets": oauth_secrets,
		"redirect_uri": bdu.callback_url(get_url()),
		"backup": backup,
		"backup_secret": backup_secret["backup_secret_key"],
		"servers": servers,
		"teams": frappe.get_all("Team", fields=["name", "title"], order_by="title asc", limit=1000),
	}


@frappe.whitelist(methods=["POST"])
def save_storage_profile(values, team=None):
	"""Create/update a Storage Profile (platform when `team` is empty); secret_key write-only."""
	_admin()
	from nexaploy.api import storage as storage_api

	if team:
		_team_exists(team)
	profile = storage_api.admin_save_storage_profile(_parse(values), team or None)
	_log("platform.storage_profile_save", "Storage Profile", profile["name"],
	     {k: profile.get(k) for k in ("provider", "bucket", "region", "endpoint", "enabled")}, team=team or None)
	return profile


@frappe.whitelist(methods=["POST"])
def delete_storage_profile(name):
	_admin()
	from nexaploy.api import storage as storage_api

	team = frappe.db.get_value("Storage Profile", name, "team")
	storage_api.admin_delete_storage_profile(name)
	_log("platform.storage_profile_delete", "Storage Profile", name, team=team or None)
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def test_storage_profile(name, server=None):
	"""Write/delete a marker file in the profile's bucket from a server (backup.test_remote with
	a throw-away rclone config). Platform credentials only go to platform (Hosted) servers."""
	_admin()
	from nexaploy.agent_job import create_agent_job
	from nexaploy.utils import secret_ref

	row = frappe.db.get_value("Storage Profile", name, ["name", "profile_name", "provider", "endpoint", "region",
	                                                    "addressing", "bucket", "path_prefix", "access_key", "team"],
	                          as_dict=True)
	if not row:
		raise frappe.DoesNotExistError(_("Storage profile {0} not found").format(name))
	if server:
		srv = frappe.db.get_value("Server", server, ["name", "status", "team", "ownership", "hosts_control_plane"],
		                          as_dict=True)
		if not srv or not au.may_test_on(srv, row.team):
			frappe.throw(_("Server {0} cannot be used to test this bucket").format(server))
	else:
		server = au.pick_test_server(
			frappe.get_all("Server", filters={"status": "Active"},
			               fields=["name", "status", "team", "ownership", "hosts_control_plane"]),
			row.team,
		)
		if not server:
			frappe.throw(_("No active server can test this bucket"))
	params = au.profile_test_params(row, secret_ref("Storage Profile", row.name, "secret_key"))
	job = create_agent_job(server, "backup.test_remote", params, "Storage Profile", row.name)
	_log("platform.storage_profile_test", "Storage Profile", row.name, {"server": server}, team=row.team or None)
	return {"job": job.name, "server": server}


@frappe.whitelist()
def get_job(name):
	"""Status of an Agent Job (result only for connection tests: other results may hold data)."""
	_admin()
	job = frappe.db.get_value("Agent Job", name, ["name", "kind", "status", "error", "result", "server", "creation"],
	                          as_dict=True)
	if not job:
		raise frappe.DoesNotExistError(_("Job {0} not found").format(name))
	result = None
	if job.kind in TEST_RESULT_KINDS and job.result:
		try:
			result = frappe.parse_json(job.result)
		except Exception:
			result = None
	return {"name": job.name, "kind": job.kind, "status": job.status, "error": job.error, "result": result,
	        "server": job.server, "creation": job.creation}


@frappe.whitelist(methods=["POST"])
def use_profile_for_backups(profile=None):
	"""Point the platform database backups at a platform Storage Profile ("" = keep manual settings)."""
	_admin()
	from nexaploy.api import storage as storage_api

	view = storage_api.admin_use_profile_for_backups(profile or None)
	_log("platform.backup_storage", "Storage Settings", "Storage Settings", {"profile": profile or None})
	return view


@frappe.whitelist(methods=["POST"])
def update_oauth_apps(values=None, secrets=None):
	"""Google Drive / OneDrive / Dropbox OAuth apps (Nexaploy Settings), secrets write-only."""
	_admin()
	clean = _clean(values, au.OAUTH_FIELDS)
	new, clear = _secrets(secrets, au.OAUTH_SECRETS)
	changes = _save_single(NEXAPLOY_SETTINGS, clean, new, clear, "platform.oauth_update")
	oauth, oauth_secrets = _single(NEXAPLOY_SETTINGS, au.OAUTH_FIELDS, au.OAUTH_SECRETS)
	return {"changes": changes, "oauth": oauth, "oauth_secrets": oauth_secrets}


# --------------------------------------------------------------------------- 4. e-mail relay & brands


def _brands():
	return frappe.get_all(BRAND, fields=BRAND_LIST_FIELDS, order_by="is_default desc, title asc")


@frappe.whitelist()
def get_email_brands():
	"""Email Relay Settings (password write-only) and every Brand."""
	_admin()
	from nexaploy import email_utils as eu

	relay, relay_secrets = _single(EMAIL_SETTINGS, au.EMAIL_FIELDS, au.EMAIL_SECRETS)
	return {
		"relay": relay,
		"relay_secrets": relay_secrets,
		"providers": list(eu.PROVIDERS),
		"tls_modes": list(eu.TLS_MODES),
		"brands": _brands(),
		"teams": frappe.get_all("Team", fields=["name", "title"], order_by="title asc", limit=1000),
	}


@frappe.whitelist(methods=["POST"])
def update_email_relay(values=None, secrets=None):
	"""Save the relay (Email Relay Settings re-pushes the account to every enabled site)."""
	_admin()
	from nexaploy import email_utils as eu

	clean = _clean(values, au.EMAIL_FIELDS)
	new, clear = _secrets(secrets, au.EMAIL_SECRETS)
	current, _states = _single(EMAIL_SETTINGS, au.EMAIL_FIELDS)
	try:
		eu.validate_settings({**current, **clean})
	except ValueError as exc:
		_throw(exc)
	changes = _save_single(EMAIL_SETTINGS, clean, new, clear, "platform.email_relay_update")
	relay, relay_secrets = _single(EMAIL_SETTINGS, au.EMAIL_FIELDS, au.EMAIL_SECRETS)
	return {"changes": changes, "relay": relay, "relay_secrets": relay_secrets}


@frappe.whitelist(methods=["POST"])
def save_brand(values, name=None):
	"""Create or update a white-label Brand (Brand.validate checks colours, domains, URLs)."""
	_admin()
	clean = _clean(values, au.BRAND_FIELDS)
	if clean.get("team"):
		_team_exists(clean["team"])
	if name:
		if not frappe.db.exists(BRAND, name):
			raise frappe.DoesNotExistError(_("Brand {0} not found").format(name))
		doc = frappe.get_doc(BRAND, name)
		clean.pop("brand_name", None)
		before = {k: doc.get(k) for k in clean}
		doc.update(clean)
		doc.save(ignore_permissions=True)
		details = au.change_summary(before, clean)
	else:
		if not clean.get("brand_name"):
			frappe.throw(_("Brand ID is required"))
		doc = frappe.get_doc({"doctype": BRAND, **clean})
		doc.insert(ignore_permissions=True)
		details = {k: v for k, v in clean.items() if k in ("title", "team", "is_default", "enabled")}
	_log("platform.brand_save", BRAND, doc.name, details, team=doc.team or None)
	return _brands()


@frappe.whitelist(methods=["POST"])
def delete_brand(name):
	_admin()
	team = frappe.db.get_value(BRAND, name, "team")
	if frappe.db.exists("Team", {"reseller_brand": name}):
		frappe.throw(_("Teams still use brand {0}; disable it instead").format(name))
	frappe.delete_doc(BRAND, name, ignore_permissions=True)
	_log("platform.brand_delete", BRAND, name, team=team or None)
	return _brands()


# --------------------------------------------------------------------------- 5. agent releases


def _releases():
	return frappe.get_all(
		RELEASE,
		fields=["name", "version", "channel", "git_ref", "repo_url", "wheel_url", "published", "released_on",
		        "release_notes", "creation"],
		order_by="creation desc",
	)


@frappe.whitelist()
def get_agents():
	"""Agent releases, the latest per channel and each server's agent status."""
	_admin()
	from nexaploy import agent_upgrades

	servers = frappe.get_all("Server", fields=SERVER_FIELDS, order_by="hostname asc")
	published = agent_upgrades.published_releases()
	return {
		"releases": _releases(),
		"latest": au.latest_by_channel(published),
		"servers": au.agent_rows(servers, published),
		"channels": list(au.aru.CHANNELS),
		"allow_customer_agent_upgrade": bool(cint(frappe.db.get_single_value(NEXAPLOY_SETTINGS,
		                                                                     "allow_customer_agent_upgrade"))),
	}


@frappe.whitelist(methods=["POST"])
def save_agent_release(values, name=None):
	"""Create or update an Agent Release (Agent Release.validate normalises version/ref/URLs)."""
	_admin()
	clean = _clean(values, au.RELEASE_FIELDS)
	if name:
		if not frappe.db.exists(RELEASE, name):
			raise frappe.DoesNotExistError(_("Agent release {0} not found").format(name))
		doc = frappe.get_doc(RELEASE, name)
		clean.pop("version", None)
		before = {k: doc.get(k) for k in clean}
		doc.update(clean)
		doc.save(ignore_permissions=True)
		details = au.change_summary(before, clean)
	else:
		if not clean.get("version"):
			frappe.throw(_("Version is required"))
		doc = frappe.get_doc({"doctype": RELEASE, **clean})
		doc.insert(ignore_permissions=True)
		details = {k: clean.get(k) for k in ("version", "channel", "published")}
	_log("platform.agent_release_save", RELEASE, doc.name, details)
	return _releases()


@frappe.whitelist(methods=["POST"])
def set_release_published(name, published=1):
	_admin()
	doc = frappe.get_doc(RELEASE, name)
	doc.published = 1 if cint(published) else 0
	doc.save(ignore_permissions=True)
	_log("platform.agent_release_publish" if doc.published else "platform.agent_release_unpublish", RELEASE, name)
	return _releases()


@frappe.whitelist(methods=["POST"])
def upgrade_outdated_agents(channel=None, dry_run=0):
	"""Upgrade every Active server whose agent is older than the latest release of its channel
	(nexaploy.agent_upgrades.bulk_upgrade); dry_run only lists them."""
	_admin()
	from nexaploy import agent_upgrades

	rows = agent_upgrades.bulk_upgrade(channel or None, dry_run=cint(dry_run))
	if not cint(dry_run):
		_log("platform.agent_bulk_upgrade", details={"channel": channel or None,
		                                             "servers": [r["server"] for r in rows if r.get("job")]})
	return {"servers": rows}


@frappe.whitelist(methods=["POST"])
def upgrade_server_agent(server, version=None):
	_admin()
	from nexaploy import agent_upgrades

	job = agent_upgrades.start_upgrade(server, version or None)
	team = frappe.db.get_value("Server", server, "team")
	_log("platform.agent_upgrade", "Server", server, {"version": version, "job": job}, team=team or None)
	return {"job": job}


# --------------------------------------------------------------------------- 6. marketplace review


@frappe.whitelist()
def get_marketplace():
	"""Listings in review, releases waiting for / failing verification, and recent reviews."""
	_admin()
	listings = frappe.get_all(
		"Marketplace App", filters={"status": "In Review"},
		fields=["name", "title", "publisher_profile", "pricing", "price_monthly", "category", "repo_url",
		        "submitted_on", "review_notes", "status"],
		order_by="submitted_on asc",
	)
	releases = frappe.get_all(
		"Marketplace App Release",
		filters={"parenttype": "Marketplace App", "status": ["in", ["Pending verification", "Failed"]]},
		fields=["parent as app", "frappe_major", "branch", "commit", "status", "verification_note", "verification_job"],
		order_by="modified desc", limit=200,
	)
	reviews = frappe.get_all(
		"Marketplace App Review",
		fields=["name", "app", "team", "rating", "review", "hidden", "hidden_reason", "publisher_reply", "creation"],
		order_by="creation desc", limit=100,
	)
	return {
		"listings": listings,
		"releases": releases,
		"reviews": reviews,
		"published": frappe.db.count("Marketplace App", {"status": "Published"}),
		"release_statuses": ["Verified", "Pending verification", "Failed"],
	}


@frappe.whitelist(methods=["POST"])
def review_listing(app, action, notes=None):
	"""approve / reject / unpublish a listing (nexaploy.api.publisher review flow)."""
	_admin()
	from nexaploy.api import publisher

	if action not in ("approve", "reject", "unpublish"):
		frappe.throw(_("Unknown review action {0}").format(action))
	result = publisher._review(app, action, notes)
	_log(f"platform.listing_{action}", "Marketplace App", app, {"notes": notes, "status": result.get("status")})
	return result


@frappe.whitelist(methods=["POST"])
def set_release_status(app, frappe_major, status, note=None):
	"""Override a release's verification status."""
	_admin()
	from nexaploy.api import publisher

	result = publisher.set_release_status(app, frappe_major, status, note)
	_log("platform.release_status", "Marketplace App", app, {"frappe_major": frappe_major, "status": status})
	return result


@frappe.whitelist(methods=["POST"])
def verify_release(app, frappe_major):
	"""Run a release's test build again."""
	_admin()
	from nexaploy.api import publisher

	result = publisher.verify_release(app, frappe_major)
	_log("platform.release_verify", "Marketplace App", app, {"frappe_major": frappe_major})
	return result


@frappe.whitelist(methods=["POST"])
def hide_review(review, hidden=1, reason=None):
	_admin()
	from nexaploy.api import marketplace

	result = marketplace.hide_review(review, hidden, reason)
	team = frappe.db.get_value("Marketplace App Review", review, "team")
	_log("platform.review_hide" if result.get("hidden") else "platform.review_show", "Marketplace App Review",
	     review, {"reason": reason}, team=team or None)
	return result


# --------------------------------------------------------------------------- 7. teams & servers


@frappe.whitelist()
def list_teams(query=None, limit=200):
	"""All teams with their flags, credit balance and site/server counts."""
	_admin()
	filters = {}
	q = (query or "").strip()
	or_filters = {"name": ["like", f"%{q}%"], "title": ["like", f"%{q}%"], "owner_user": ["like", f"%{q}%"]} if q else None
	teams = frappe.get_all(
		"Team", filters=filters, or_filters=or_filters,
		fields=["name", "title", "owner_user", "enabled", "billing_exempt", "extended_backup_retention",
		        "credit_balance", "currency", "reseller_brand", "creation"],
		order_by="creation desc", limit=min(max(cint(limit) or 200, 1), 1000),
	)
	sites = _group_count("Site", {"status": ["!=", "Archived"]})
	suspended = _group_count("Site", {"status": "Suspended"})
	servers = _group_count("Server", {"status": ["!=", "Archived"]})
	overdue = _group_count("Nexaploy Invoice", {"status": "Overdue"})
	for t in teams:
		t["sites"] = sites.get(t.name, 0)
		t["suspended_sites"] = suspended.get(t.name, 0)
		t["servers"] = servers.get(t.name, 0)
		t["overdue_invoices"] = overdue.get(t.name, 0)
	return {"teams": teams}


@frappe.whitelist(methods=["POST"])
def set_team_flags(team, values):
	"""billing_exempt / extended_backup_retention / enabled on a Team."""
	_admin()
	_team_exists(team)
	clean = {k: (1 if v in (True, 1, "1", "true") else 0) for k, v in _parse(values).items() if k in au.TEAM_FLAGS}
	if not clean:
		frappe.throw(_("Nothing to change"))
	doc = frappe.get_doc("Team", team)
	before = {k: doc.get(k) for k in clean}
	doc.update(clean)
	doc.save(ignore_permissions=True)
	_log("platform.team_flags", "Team", team, au.change_summary(before, clean), team=team)
	return {k: doc.get(k) for k in au.TEAM_FLAGS}


@frappe.whitelist(methods=["POST"])
def suspend_team(team):
	"""Suspend every Active site of the team (as for an overdue invoice: maintenance mode)."""
	_admin()
	from nexaploy.billing import invoicing

	invoicing.suspend_team(_team_exists(team))
	_log("platform.team_suspend", "Team", team, team=team)
	return {"suspended_sites": frappe.db.count("Site", {"team": team, "status": "Suspended"})}


@frappe.whitelist(methods=["POST"])
def unsuspend_team(team):
	"""Lift the billing suspension of the team's sites (usage suspensions stay)."""
	_admin()
	from nexaploy.billing import invoicing

	invoicing.unsuspend_team(_team_exists(team))
	_log("platform.team_unsuspend", "Team", team, team=team)
	return {"suspended_sites": frappe.db.count("Site", {"team": team, "status": "Suspended"})}


@frappe.whitelist(methods=["POST"])
def grant_credits(team, amount, type="Promo", description=None):
	"""Add (or, as an Adjustment, remove) credits; open invoices are paid from them."""
	_admin()
	from nexaploy.billing import credits

	_team_exists(team)
	try:
		value, tx_type = au.clean_credit_grant(amount, type)
	except au.AdminError as exc:
		_throw(exc)
	text = (description or "").strip()[:500] or _("Granted by a platform administrator")
	entry = credits.add_credits(team, value, tx_type, description=text)
	try:
		credits.apply_credits_to_open_invoices(team)
	except Exception:
		frappe.log_error(title=f"Nexaploy: applying granted credits for {team} failed")
	_log("platform.credits_grant", "Team Credit Transaction", entry,
	     {"amount": float(value), "type": tx_type, "description": text}, team=team)
	return {"entry": entry, "balance": frappe.db.get_value("Team", team, "credit_balance")}


@frappe.whitelist()
def list_servers():
	"""All servers with team, ownership, control-plane host badge, health and agent status."""
	_admin()
	from nexaploy import agent_upgrades

	servers = frappe.get_all("Server", fields=SERVER_FIELDS, order_by="status asc, hostname asc")
	latest = au.latest_by_channel(agent_upgrades.published_releases())
	titles = {t.name: t.title for t in frappe.get_all("Team", fields=["name", "title"])}
	for s in servers:
		s["team_title"] = titles.get(s.team) or s.team
		target = latest.get(s.agent_channel or "stable")
		s["latest"] = target
		s["outdated"] = bool(target) and au.aru.is_older(s.agent_version, target)
	return {"servers": servers}
