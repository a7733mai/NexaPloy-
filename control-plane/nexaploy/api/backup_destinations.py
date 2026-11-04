"""Backups for the customer portal (/dashboard/backups): destinations, retention, restore.

Destinations (team-owned **Backup Destination**: S3, Google Drive, OneDrive, Dropbox). Team
Owners/Admins (action `admin`) manage them; Google Drive / OneDrive / Dropbox are connected
with an OAuth "Connect" button against the platform's OAuth apps (Nexaploy Settings):

    overview()                                   -> destinations, servers, providers, retention, sites
    save_destination(destination_type, title, name=None, folder=None, is_default=0,
                     root_folder_id=None, s3_provider=None, s3_endpoint=None, s3_region=None,
                     s3_bucket=None, s3_access_key=None, s3_secret_key=None)
    delete_destination(name)
    start_oauth(provider, destination)           -> {url}   (signed state, PKCE, 10 min)
    oauth_callback(code, state, error, ...)      guest-allowed GET; the provider redirects here
    disconnect_destination(name)
    test_destination(name, server=None)          -> {job}   (backup.test_remote)
    set_server_destination(server, destination)  -> {job}   ("" = team default / platform)

Retention (`write`): team default and per-site override, bounded by Nexaploy Settings limits;
retention beyond the included days needs the paid add-on (ops.retention_addon_active):

    set_team_retention(policy)                   -> policy
    set_site_retention(site, custom, policy=None)-> effective policy

Restore (`write`): any listed backup, everything / database only / files only, into the
same site or into a new site:

    list_site_backups(site)                      -> [{name, created_at, size, modes, ...}]
    restore_backup(backup, mode="all", new_site=None, bench=None)

Pure rules: nexaploy/backup_destination_utils.py and nexaploy/backup_policy_utils.py.
"""

import frappe
from frappe import _
from frappe.utils import get_url, now_datetime

from nexaploy import backup_destination_utils as bdu
from nexaploy import backup_policy_utils as bpu
from nexaploy.activity import log_activity
from nexaploy.api.dashboard import _ctx

DESTINATION = "Backup Destination"
DASHBOARD = "/dashboard/backups"
TIMEOUT = 30
LIST_FIELDS = [
	"name", "title", "destination_type", "status", "is_default", "folder", "account_email", "account_name",
	"connected_at", "s3_provider", "s3_endpoint", "s3_region", "s3_bucket", "s3_access_key", "root_folder_id",
	"last_tested_at", "last_test_server", "last_error", "quota_used", "quota_total",
]
CACHE_PREFIX = "nexaploy:backup-oauth:"


def _throw(exc):
	frappe.throw(_(str(exc)))


def _destination(name, c):
	"""Backup Destination doc of the caller's team (PermissionError otherwise)."""
	if not name or not frappe.db.exists(DESTINATION, name):
		raise frappe.DoesNotExistError(_("Backup destination {0} not found").format(name))
	doc = frappe.get_doc(DESTINATION, name)
	if doc.team != c.team and not c.admin:
		raise frappe.PermissionError(_("Backup destination {0} belongs to another team").format(name))
	return doc


def _server(name, c):
	row = frappe.db.get_value(
		"Server", name, ["name", "server_id", "team", "status", "hostname", "backup_destination"], as_dict=True
	)
	if not row:
		raise frappe.DoesNotExistError(_("Server {0} not found").format(name))
	if row.team != c.team and not c.admin:
		raise frappe.PermissionError(_("Server {0} belongs to another team").format(name))
	return row


def _state_key():
	from frappe.utils.password import get_encryption_key

	import hashlib

	return hashlib.sha256(("nexaploy-backup-oauth-state:" + get_encryption_key()).encode()).digest()


def _redirect(location):
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = location


# --------------------------------------------------------------------------- overview


def _destination_dict(row):
	out = {f: row.get(f) for f in LIST_FIELDS}
	out["provider"] = bdu.PROVIDER_FOR_TYPE.get(row.get("destination_type"))
	out["usable"] = bdu.destination_usable(row)
	return out


@frappe.whitelist()
def overview():
	"""Everything /dashboard/backups needs for the current team."""
	c = _ctx("read")
	from nexaploy import ops

	rows = frappe.get_all(DESTINATION, filters={"team": c.team}, fields=LIST_FIELDS, order_by="creation asc")
	default = next((r.name for r in rows if r.is_default), None)
	servers = []
	for s in frappe.get_all(
		"Server", filters={"team": c.team, "status": ["!=", "Archived"]},
		fields=["name", "hostname", "status", "ownership", "backup_destination"], order_by="creation asc",
	):
		effective = bdu.effective_destination(s.backup_destination, default)
		servers.append({**s, "effective_destination": effective})
	team_row = frappe.db.get_value("Team", c.team, list(bpu.DOC_FIELDS.values()), as_dict=True) or {}
	team_policy = bpu.from_doc(team_row)
	sites = []
	for s in frappe.get_all(
		"Site", filters={"team": c.team, "status": ["not in", ["Archived"]], "is_staging": 0},
		fields=["name", "status", "server", "backup_frequency", "last_backup_at", "backup_retention_custom",
		        *bpu.DOC_FIELDS.values()],
		order_by="creation asc",
	):
		sites.append({
			"name": s.name, "status": s.status, "server": s.server, "backup_frequency": s.backup_frequency,
			"last_backup_at": s.last_backup_at, "custom": bool(s.backup_retention_custom),
			"policy": bpu.effective_policy(s, team_row),
		})
	settings_platform = ops.get_settings()
	from nexaploy import ops_utils as ou

	return {
		"team": c.team,
		"can_admin": bool(c.admin) or _role_allows(c.role, "admin"),
		"can_write": bool(c.admin) or _role_allows(c.role, "write"),
		"destinations": [_destination_dict(r) for r in rows],
		"servers": servers,
		"providers": {p: ops.oauth_configured(p) for p in bdu.PROVIDERS},
		"platform_storage": ou.backup_configured(settings_platform.as_dict()),
		"redirect_uri": bdu.callback_url(get_url()),
		"retention": {
			"team": team_policy,
			"limits": ops.retention_limits(),
			"addon_active": ops.retention_addon_active(c.team),
		},
		"sites": sites,
	}


def _role_allows(role, action):
	from nexaploy.team_utils import role_allows

	return role_allows(role, action)


# --------------------------------------------------------------------------- destinations

S3_FIELDS = ("s3_provider", "s3_endpoint", "s3_region", "s3_bucket", "s3_access_key")


@frappe.whitelist(methods=["POST"])
def save_destination(destination_type=None, title=None, name=None, folder=None, is_default=0,
                     root_folder_id=None, s3_provider=None, s3_endpoint=None, s3_region=None,
                     s3_bucket=None, s3_access_key=None, s3_secret_key=None):
	"""Create (no `name`) or update a destination. OAuth types are then connected with
	start_oauth. An empty s3_secret_key keeps the stored one."""
	c = _ctx("admin")
	if name:
		doc = _destination(name, c)
		if destination_type and destination_type != doc.destination_type:
			frappe.throw(_("The type of a destination cannot change; add a new destination instead"))
	else:
		if destination_type not in bdu.DESTINATION_TYPES:
			frappe.throw(_("Choose S3, Google Drive, OneDrive or Dropbox"))
		doc = frappe.get_doc({"doctype": DESTINATION, "team": c.team, "destination_type": destination_type})
	doc.title = (title or "").strip()[:140] or doc.title or doc.destination_type
	doc.folder = folder or doc.folder
	doc.is_default = 1 if frappe.utils.cint(is_default) else 0
	if doc.destination_type == bdu.GOOGLE_DRIVE:
		doc.root_folder_id = (root_folder_id or "").strip() or None
	if doc.destination_type == bdu.S3:
		values = dict(s3_provider=s3_provider, s3_endpoint=s3_endpoint, s3_region=s3_region,
		              s3_bucket=s3_bucket, s3_access_key=s3_access_key)
		for field in S3_FIELDS:
			doc.set(field, (values[field] or "").strip() or None)
		if s3_secret_key:
			doc.s3_secret_key = s3_secret_key
		elif doc.is_new():
			frappe.throw(_("Secret access key is required"))
	doc.flags.ignore_permissions = True
	doc.save()
	log_activity(c.team, "backup_destination.create" if not name else "backup_destination.update",
	             DESTINATION, doc.name, {"type": doc.destination_type, "title": doc.title})
	return _destination_dict(frappe.db.get_value(DESTINATION, doc.name, LIST_FIELDS, as_dict=True))


@frappe.whitelist(methods=["POST"])
def delete_destination(name):
	c = _ctx("admin")
	doc = _destination(name, c)
	doc.flags.ignore_permissions = True
	_revoke(doc)
	frappe.delete_doc(DESTINATION, doc.name, ignore_permissions=True)
	log_activity(c.team, "backup_destination.delete", DESTINATION, name, {"type": doc.destination_type})
	return {"deleted": name}


# --------------------------------------------------------------------------- OAuth


@frappe.whitelist(methods=["POST"])
def start_oauth(provider, destination):
	"""Authorization URL for connecting `destination`. The signed `state` binds user, team,
	destination and provider (10 min); its nonce and the PKCE verifier live server-side and
	are used once."""
	c = _ctx("admin")
	doc = _destination(destination, c)
	try:
		p = bdu.get_provider(provider)
		if bdu.PROVIDER_FOR_TYPE.get(doc.destination_type) != p.name:
			raise bdu.DestinationError(f"{doc.destination_type} is not connected with {provider}")
	except bdu.DestinationError as e:
		_throw(e)
	from nexaploy import ops

	client = ops.oauth_client(p.name, with_secret=True)
	if not (client["client_id"] and client["client_secret"]):
		frappe.throw(_("Connecting {0} is not available yet; ask the platform operator").format(doc.destination_type))
	nonce = bdu.new_nonce()
	verifier, challenge = bdu.make_pkce()
	frappe.cache().set_value(
		CACHE_PREFIX + nonce,
		{"verifier": verifier, "user": c.user, "team": doc.team, "destination": doc.name, "provider": p.name},
		expires_in_sec=bdu.STATE_TTL,
	)
	state = bdu.sign_state(
		{"n": nonce, "u": c.user, "t": doc.team, "d": doc.name, "p": p.name}, _state_key()
	)
	url = bdu.authorization_url(
		p.name, client["client_id"], bdu.callback_url(get_url()), state, challenge, client["tenant"]
	)
	log_activity(doc.team, "backup_destination.connect_start", DESTINATION, doc.name, {"provider": p.name})
	return {"url": url}


def _http(method, url, bearer=None, **kwargs):
	import requests

	headers = {"Accept": "application/json"}
	if bearer:
		headers["Authorization"] = f"Bearer {bearer}"
	resp = requests.request(method, url, headers=headers, timeout=TIMEOUT, **kwargs)
	try:
		data = resp.json()
	except ValueError:
		data = {}
	if resp.status_code >= 400 and not (isinstance(data, dict) and data.get("error")):
		raise bdu.DestinationError(f"{url.split('?')[0]} answered HTTP {resp.status_code}")
	return data


def _fail(doc, message):
	doc.db_set({"status": bdu.ERROR, "last_error": str(message)[:1000]}, update_modified=False)
	frappe.db.commit()  # GET requests are not auto-committed


@frappe.whitelist(allow_guest=True, methods=["GET"])
def oauth_callback(code=None, state=None, error=None, error_description=None, **kwargs):
	"""The provider redirects here. Checks the signed state (same logged-in user, unexpired,
	nonce unused), exchanges the code (PKCE), stores the token JSON in the destination's
	Password field, reads the account email (and OneDrive drive id) and pushes the
	destination to the servers using it. Always redirects back to /dashboard/backups."""
	if frappe.session.user == "Guest":
		request = getattr(frappe.local, "request", None)
		from nexaploy.portal_utils import login_redirect_url

		target = request.full_path if request else DASHBOARD
		path, _sep, query = target.partition("?")
		return _redirect(login_redirect_url(path, query or None))

	claims = bdu.verify_state(state, _state_key())
	if not claims or claims.get("u") != frappe.session.user:
		return _redirect(DASHBOARD + "?oauth=invalid_state")
	cache = frappe.cache()
	key = CACHE_PREFIX + str(claims.get("n"))
	stored = cache.get_value(key)
	cache.delete_value(key)  # one use only
	if not stored or stored.get("destination") != claims.get("d") or stored.get("user") != claims.get("u"):
		return _redirect(DASHBOARD + "?oauth=expired")

	from nexaploy.teams import check_team_access

	team, name, provider = claims.get("t"), claims.get("d"), claims.get("p")
	try:
		check_team_access(team, "admin")
	except frappe.PermissionError:
		return _redirect(DASHBOARD + "?oauth=forbidden")
	if not frappe.db.exists(DESTINATION, name):
		return _redirect(DASHBOARD + "?oauth=missing")
	doc = frappe.get_doc(DESTINATION, name)
	if doc.team != team or bdu.PROVIDER_FOR_TYPE.get(doc.destination_type) != provider:
		return _redirect(DASHBOARD + "?oauth=invalid_state")
	if error:
		_fail(doc, f"{error}: {error_description or ''}".strip(": "))
		return _redirect(f"{DASHBOARD}?oauth=denied&destination={name}")

	from nexaploy import ops

	client = ops.oauth_client(provider, with_secret=True)
	try:
		url, form = bdu.token_request(
			provider, code, client["client_id"], client["client_secret"] or "", bdu.callback_url(get_url()),
			stored.get("verifier"), client["tenant"],
		)
		token = bdu.rclone_token(_http("POST", url, data=form))
		responses = []
		for req in bdu.account_requests(provider):
			kwargs = {"json": req["json"]} if "json" in req else {}
			responses.append(_http(req["method"], req["url"], bearer=token["access_token"], **kwargs))
		account = bdu.parse_account(provider, responses)
	except bdu.DestinationError as e:
		_fail(doc, e)
		return _redirect(f"{DASHBOARD}?oauth=failed&destination={name}")
	except Exception:
		frappe.log_error(title=f"Nexaploy: OAuth callback failed for backup destination {name}")
		_fail(doc, "Could not reach the provider; try again")
		return _redirect(f"{DASHBOARD}?oauth=failed&destination={name}")

	doc.oauth_token = bdu.token_json(token)
	doc.account_email = account["email"]
	doc.account_name = account["name"]
	if doc.destination_type == bdu.ONEDRIVE:
		doc.drive_id, doc.drive_type = account["drive_id"], account["drive_type"]
	doc.status = bdu.CONNECTED
	doc.last_error = None
	doc.connected_at = now_datetime()
	doc.connected_by = frappe.session.user
	doc.flags.ignore_permissions = True
	doc.save()  # on_update pushes the new token to the servers using this destination
	log_activity(team, "backup_destination.connect", DESTINATION, name,
	             {"provider": provider, "account": account["email"]})
	frappe.db.commit()
	return _redirect(f"{DASHBOARD}?connected={name}")


def _revoke(doc):
	"""Best-effort revocation of an OAuth token at the provider."""
	if doc.destination_type not in bdu.OAUTH_DESTINATION_TYPES:
		return
	raw = doc.get_password("oauth_token", raise_exception=False)
	if not raw:
		return
	try:
		req = bdu.revoke_request(bdu.provider_for_type(doc.destination_type), bdu.parse_token(raw))
		if req:
			import requests

			headers = {"Authorization": f"Bearer {req['bearer']}"} if req.get("bearer") else {}
			kwargs = {"json": req["json"]} if "json" in req else {"data": req.get("data")}
			requests.request(req["method"], req["url"], headers=headers, timeout=TIMEOUT, **kwargs)
	except Exception:
		pass


@frappe.whitelist(methods=["POST"])
def disconnect_destination(name):
	"""Forget (and revoke where the provider allows) the OAuth token. Servers using the
	destination fall back to the platform storage."""
	c = _ctx("admin")
	doc = _destination(name, c)
	if doc.destination_type not in bdu.OAUTH_DESTINATION_TYPES:
		frappe.throw(_("Only Google Drive, OneDrive and Dropbox destinations can be disconnected"))
	_revoke(doc)
	doc.oauth_token = None
	doc.status = bdu.NOT_CONNECTED
	doc.account_email = doc.account_name = doc.drive_id = doc.drive_type = None
	doc.flags.ignore_permissions = True
	doc.save()
	log_activity(c.team, "backup_destination.disconnect", DESTINATION, name)
	return _destination_dict(frappe.db.get_value(DESTINATION, name, LIST_FIELDS, as_dict=True))


@frappe.whitelist(methods=["POST"])
def test_destination(name, server=None):
	"""backup.test_remote from one of the team's Active servers (temporary rclone config: the
	server's backups are not affected). The result updates the destination's status."""
	c = _ctx("admin")
	doc = _destination(name, c)
	if not bdu.destination_usable(doc.as_dict()) and doc.destination_type != bdu.S3:
		frappe.throw(_("Connect the account first"))
	if server:
		row = _server(server, c)
		if row.status != "Active":
			frappe.throw(_("Server {0} is not active").format(server))
	else:
		server = frappe.db.get_value(
			"Server", {"team": doc.team, "status": "Active", "backup_destination": doc.name}, "name"
		) or frappe.db.get_value("Server", {"team": doc.team, "status": "Active"}, "name")
		if not server:
			frappe.throw(_("Add an active server to your team to test this destination"))
	from nexaploy import ops

	job = ops.test_destination_job(doc, server)
	log_activity(c.team, "backup_destination.test", DESTINATION, name, {"server": server})
	return {"job": job, "server": server}


@frappe.whitelist(methods=["POST"])
def set_server_destination(server, destination=None):
	"""Choose where `server` uploads backups ("" = team default, else platform storage)."""
	c = _ctx("admin")
	row = _server(server, c)
	if destination:
		doc = _destination(destination, c)
		if doc.team != row.team:
			frappe.throw(_("Backup destination {0} belongs to another team").format(destination))
		if not bdu.destination_usable(doc.as_dict()):
			frappe.throw(_("Connect {0} first").format(doc.title))
	frappe.db.set_value("Server", row.name, "backup_destination", destination or None)
	from nexaploy import ops

	job = None
	if row.status in ("Active", "Offline"):
		job = ops.configure_backups(row.name)
	log_activity(c.team, "server.backup_destination", "Server", row.name, {"destination": destination or None})
	return {"server": row.name, "destination": destination or None, "job": job}


# --------------------------------------------------------------------------- retention


def _checked_policy(policy, team):
	from nexaploy import ops

	try:
		clean = bpu.normalize_policy(frappe.parse_json(policy) if isinstance(policy, str) else policy)
		bpu.check_policy(clean, ops.retention_limits(), ops.retention_addon_active(team))
	except bpu.PolicyError as e:
		_throw(e)
	return clean


@frappe.whitelist(methods=["POST"])
def set_team_retention(policy):
	"""Team default retention (sites without their own)."""
	c = _ctx("write")
	clean = _checked_policy(policy, c.team)
	frappe.db.set_value("Team", c.team, bpu.to_doc(clean))
	log_activity(c.team, "backup.retention_default", "Team", c.team, clean)
	return clean


@frappe.whitelist(methods=["POST"])
def set_site_retention(site, custom=0, policy=None):
	"""Per-site retention: custom=0 follows the team default, custom=1 stores `policy`."""
	c = _ctx("write")
	row = frappe.db.get_value("Site", site, ["name", "team"], as_dict=True)
	if not row:
		raise frappe.DoesNotExistError(_("Site {0} not found").format(site))
	if row.team != c.team and not c.admin:
		raise frappe.PermissionError(_("Site {0} belongs to another team").format(site))
	values = {"backup_retention_custom": 1 if frappe.utils.cint(custom) else 0}
	if values["backup_retention_custom"]:
		values.update(bpu.to_doc(_checked_policy(policy or {}, row.team)))
	frappe.db.set_value("Site", row.name, values)
	from nexaploy import ops

	effective = ops.site_backup_policy(row.name)
	log_activity(row.team, "site.backup_retention", "Site", row.name,
	             {"custom": values["backup_retention_custom"], **effective})
	return {"site": row.name, "custom": bool(values["backup_retention_custom"]), "policy": effective}


# --------------------------------------------------------------------------- restore


@frappe.whitelist()
def list_site_backups(site):
	"""Restorable (uploaded, not pruned) backups of a team site, newest first."""
	c = _ctx("read")
	row = frappe.db.get_value("Site", site, ["name", "team", "server", "bench"], as_dict=True)
	if not row or (row.team != c.team and not c.admin):
		raise frappe.PermissionError(_("Site {0} not found").format(site))
	backups = frappe.get_all(
		"Site Backup", filters={"site": row.name, "status": "Success", "remote_path": ["is", "set"]},
		fields=["name", "created_at", "size", "with_files", "purpose", "remote_path"],
		order_by="creation desc", limit_page_length=200,
	)
	files = {}
	if backups:
		for f in frappe.get_all(
			"Site Backup File", filters={"parenttype": "Site Backup", "parent": ["in", [b.name for b in backups]]},
			fields=["parent", "file_type", "size"],
		):
			files.setdefault(f.parent, []).append({"file_type": f.file_type, "size": f.size})
	out = []
	for b in backups:
		out.append({
			"name": b.name, "created_at": b.created_at, "size": b.size, "purpose": b.purpose,
			"files": files.get(b.name, []), "modes": bpu.restorable_parts(files.get(b.name, []), b.with_files),
		})
	return out


def _storage_key(server):
	"""Servers with the same key read the same backup storage (restore across servers)."""
	from nexaploy import ops

	row = frappe.db.get_value("Server", server, ["team", "backup_destination"], as_dict=True)
	dest = ops.server_destination(row) if row else None
	return dest.name if dest else "platform"


@frappe.whitelist(methods=["POST"])
def restore_backup(backup, mode="all", new_site=None, bench=None, plan=None):
	"""Restore a backup of a team site: mode all | database | files. Into the same site, or -
	with `new_site` (subdomain or name) and `bench` - into a new site (everything or database)."""
	c = _ctx("write")
	b = frappe.db.get_value("Site Backup", backup, ["name", "site", "status", "remote_path"], as_dict=True)
	if not b:
		raise frappe.DoesNotExistError(_("Backup {0} not found").format(backup))
	source = frappe.get_doc("Site", b.site)
	if source.team != c.team and not c.admin:
		raise frappe.PermissionError(_("Backup {0} belongs to another team").format(backup))
	if b.status != "Success":
		frappe.throw(_("Backup {0} cannot be restored").format(backup))
	from nexaploy.agent_job import create_agent_job

	if not new_site:
		try:
			params = bpu.restore_params(source.site_name, b.remote_path, mode)
		except bpu.PolicyError as e:
			_throw(e)
		if source.status not in ("Active", "Broken"):
			frappe.throw(_("Site {0} is {1}; it must be Active").format(source.name, _(source.status)))
		job = create_agent_job(source.server, "site.restore", params, "Site", source.name).name
		log_activity(c.team, "site.restore", "Site", source.name, {"backup": backup, "mode": mode})
		return {"site": source.name, "job": job}

	return _restore_as_new_site(c, source, b, mode, new_site, bench, plan)


def _restore_as_new_site(c, source, b, mode, new_site, bench, plan):
	from nexaploy.api import dashboard as d
	from nexaploy.portal_utils import build_site_name, generate_password

	target = d._deployable_bench(bench, c)
	if target.status != "Active" or target.server_status != "Active":
		frappe.throw(_("Bench {0} is not active").format(bench))
	if target.server != source.server and _storage_key(target.server) != _storage_key(source.server):
		frappe.throw(_("The target server uses another backup storage; restore on a server with the same destination"))
	try:
		params = bpu.restore_params("placeholder.invalid", b.remote_path, mode, bench=target.bench_name)
		site_name = build_site_name(new_site, d._root_domain(), allow_custom=c.admin or target.own)
	except (bpu.PolicyError, ValueError) as e:
		_throw(e)
	if frappe.db.exists("Site", site_name) or frappe.db.exists("Site Domain", {"domain": site_name, "parenttype": "Site"}):
		frappe.throw(_("{0} is already taken").format(site_name))
	apps = [r.app for r in source.apps if r.status == "Installed"]
	allowed = set(d._group_apps([target.release_group]).get(target.release_group, []))
	missing = [a for a in apps if a not in allowed]
	if missing:
		frappe.throw(_("Apps {0} are not available on this bench").format(", ".join(missing)))
	if plan:
		prow = frappe.db.get_value("Nexaploy Plan", plan, ["plan_type", "enabled"], as_dict=True)
		if not prow or prow.plan_type != "Site" or not prow.enabled:
			frappe.throw(_("Plan {0} is not an available Site plan").format(plan))
	elif target.shared:
		plan = d._default_site_plan()
		if not plan:
			frappe.throw(_("No Site plan is available"))
	params.pop("site", None)
	params.pop("bench", None)
	doc = frappe.get_doc({
		"doctype": "Site",
		"site_name": site_name,
		"bench": target.name,
		"server": target.server,
		"team": c.team,
		"plan": plan or None,
		"admin_password": generate_password(),  # replaced by the backup's
		"apps": [{"app": a} for a in apps],
	})
	doc.flags.restore_params = params
	doc.insert(ignore_permissions=True)
	log_activity(c.team, "site.restore_new", "Site", doc.name,
	             {"from_site": source.name, "backup": b.name, "mode": mode, "bench": target.name})
	job = frappe.db.get_value("Agent Job", {"reference_doctype": "Site", "reference_name": doc.name,
	                                        "kind": "site.restore"}, "name")
	return {"site": doc.name, "url": f"https://{doc.site_name}", "job": job}
