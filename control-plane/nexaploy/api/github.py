"""GitHub App integration for custom apps (private repos in customer release groups).

Endpoints (all under /api/method/nexaploy.api.github.*):

- install_url()                                   -> GitHub App install link (state = signed team token)
- installation_callback(installation_id, setup_action, state, code)   guest; Setup/Callback URL
- list_installations()
- list_repos(installation)
- list_branches(installation, repo)
- add_custom_app(installation, repo, branch=None, title=None, app_name=None, frappe_majors=None)
- add_custom_app_from_url(repo_url, branch, app_name, ...)   non-GitHub providers (token field)
- list_custom_apps()
- webhook()                                       guest; X-Hub-Signature-256 verified

Team scoping goes through nexaploy.teams (ARCHITECTURE.md §9), imported lazily.
Pure helpers live in nexaploy/github_utils.py.
"""

import hashlib
import json

import frappe
from frappe import _
from frappe.utils import now_datetime

from nexaploy.github_utils import (
	GITHUB_API,
	GitHubError,
	build_app_jwt,
	candidate_app_dirs,
	detect_app_name,
	frappe_dependency,
	install_link,
	is_valid_branch,
	majors_from_branch,
	majors_from_spec,
	parse_push_event,
	redact_text,
	repo_full_name,
	sign_state,
	verify_state,
	verify_webhook_signature,
)
from nexaploy.utils import is_valid_app_name, parse_frappe_majors

SETTINGS = "GitHub Settings"
TIMEOUT = 20
TOKEN_CACHE_SECONDS = 50 * 60
MAX_PAGES = 10
DASHBOARD = "/dashboard/benches"


# --------------------------------------------------------------------------- teams (§9)


def _team(action="read"):
	from nexaploy.teams import check_team_access, get_current_team

	team = get_current_team()
	check_team_access(team, action)
	return team


def _installation(name, action="read"):
	"""GitHub Installation row of the current team (PermissionError otherwise)."""
	team = _team(action)
	row = frappe.db.get_value(
		"GitHub Installation",
		{"name": str(name or "")},
		["name", "installation_id", "team", "account_login", "account_type", "status"],
		as_dict=True,
	)
	if not row:
		raise frappe.DoesNotExistError(_("GitHub installation {0} not found").format(name))
	if row.team != team:
		raise frappe.PermissionError(_("GitHub installation {0} belongs to another team").format(name))
	if row.status != "Active":
		frappe.throw(_("GitHub installation {0} is {1}").format(row.account_login or name, _(row.status)))
	return team, row


# --------------------------------------------------------------------------- GitHub HTTP


def _settings():
	return frappe.get_single(SETTINGS)


def _secret(fieldname):
	from frappe.utils.password import get_decrypted_password

	return get_decrypted_password(SETTINGS, SETTINGS, fieldname, raise_exception=False)


def app_jwt():
	settings = _settings()
	if not settings.github_app_id:
		frappe.throw(_("GitHub integration is not configured (GitHub Settings)"))
	try:
		return build_app_jwt(settings.github_app_id, _secret("private_key") or "")
	except GitHubError as e:
		frappe.throw(str(e))


def _request(method, path, token=None, bearer=None, json_body=None, params=None, accept=None, ok_404=False):
	import requests

	url = path if path.startswith("https://") else GITHUB_API + path
	headers = {
		"Accept": accept or "application/vnd.github+json",
		"X-GitHub-Api-Version": "2022-11-28",
		"User-Agent": "nexaploy-control-plane",
	}
	if bearer:
		headers["Authorization"] = f"Bearer {bearer}"
	elif token:
		headers["Authorization"] = f"token {token}"
	try:
		resp = requests.request(method, url, headers=headers, json=json_body, params=params, timeout=TIMEOUT)
	except requests.RequestException as e:
		frappe.throw(_("Could not reach GitHub: {0}").format(redact_text(str(e))))
	if ok_404 and resp.status_code == 404:
		return None
	if resp.status_code >= 400:
		try:
			message = resp.json().get("message")
		except ValueError:
			message = resp.text[:200]
		frappe.throw(_("GitHub API error {0}: {1}").format(resp.status_code, redact_text(message or "")))
	if accept and "raw" in accept:
		return resp.text
	if resp.status_code == 204 or not resp.content:
		return {}
	return resp.json()


def installation_token(installation, repositories=None, cache=True):
	"""Installation access token (1 h). `repositories` (names, same owner) scopes it; builds
	always ask for a fresh read-only token (cache=False)."""
	inst_id = frappe.db.get_value("GitHub Installation", installation, "installation_id") or installation
	key = f"nexaploy:github:token:{inst_id}"
	if cache and not repositories:
		cached = frappe.cache().get_value(key)
		if cached:
			return cached
	body = {"permissions": {"contents": "read", "metadata": "read"}}
	if repositories:
		body["repositories"] = list(repositories)
	data = _request("POST", f"/app/installations/{inst_id}/access_tokens", bearer=app_jwt(), json_body=body)
	token = data.get("token")
	if not token:
		frappe.throw(_("GitHub did not return an installation token"))
	if cache and not repositories:
		frappe.cache().set_value(key, token, expires_in_sec=TOKEN_CACHE_SECONDS)
	return token


def _paginate(path, token, item_key=None, params=None):
	items = []
	params = dict(params or {}, per_page=100)
	for page in range(1, MAX_PAGES + 1):
		params["page"] = page
		data = _request("GET", path, token=token, params=params)
		batch = data.get(item_key, []) if item_key else data
		items.extend(batch or [])
		if len(batch or []) < 100:
			break
	return items


# --------------------------------------------------------------------------- install flow


def _state_key():
	from frappe.utils.password import get_encryption_key

	return hashlib.sha256(("nexaploy-github-state:" + get_encryption_key()).encode()).digest()


@frappe.whitelist()
def install_url():
	"""Link to install the Nexaploy GitHub App on a user/org; state binds team + user (1 h)."""
	team = _team("write")
	slug = _settings().app_slug
	if not slug:
		frappe.throw(_("GitHub integration is not configured (GitHub Settings)"))
	try:
		return {"url": install_link(slug, sign_state(team, frappe.session.user, _state_key()))}
	except GitHubError as e:
		frappe.throw(str(e))


def _redirect(location):
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = location


def _user_can_access_installation(code, installation_id):
	"""OAuth code (user authorization during installation) -> the user's installations of our
	app must include installation_id. Prevents claiming someone else's installation id."""
	settings = _settings()
	secret = _secret("client_secret")
	if not (settings.client_id and secret and code):
		return False
	import requests

	try:
		resp = requests.post(
			"https://github.com/login/oauth/access_token",
			data={"client_id": settings.client_id, "client_secret": secret, "code": code},
			headers={"Accept": "application/json"},
			timeout=TIMEOUT,
		)
		user_token = resp.json().get("access_token")
	except Exception:
		return False
	if not user_token:
		return False
	installs = _paginate("/user/installations", user_token, item_key="installations")
	return any(str(i.get("id")) == str(installation_id) for i in installs)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def installation_callback(installation_id=None, setup_action=None, state=None, code=None, **kwargs):
	"""GitHub redirects here after installing/updating the app (Setup URL / Callback URL).

	Verifies the signed `state` (team + user, 1 h), that the logged-in user is the one who
	started the flow and may still write to the team, and - via the OAuth `code` - that the
	user really has access to `installation_id`. Then stores the GitHub Installation.
	"""
	if frappe.session.user == "Guest":
		request = getattr(frappe.local, "request", None)
		target = request.full_path if request else "/dashboard/benches"
		from nexaploy.portal_utils import login_redirect_url

		path, _sep, query = target.partition("?")
		return _redirect(login_redirect_url(path, query or None))

	if setup_action == "request":
		return _redirect(DASHBOARD + "?github=requested")

	claim = verify_state(state, _state_key())
	if not claim or claim["user"] != frappe.session.user:
		return _redirect(DASHBOARD + "?github=invalid_state")

	from nexaploy.teams import check_team_access, is_platform_admin

	team = claim["team"]
	check_team_access(team, "write")
	installation_id = str(installation_id or "").strip()
	if not installation_id.isdigit():
		return _redirect(DASHBOARD + "?github=missing_installation")

	if not _user_can_access_installation(code, installation_id) and not is_platform_admin():
		# Without client_secret + OAuth-during-install we cannot prove ownership.
		return _redirect(DASHBOARD + "?github=unverified")

	info = _request("GET", f"/app/installations/{installation_id}", bearer=app_jwt())
	account = info.get("account") or {}
	existing = frappe.db.get_value("GitHub Installation", installation_id, ["name", "team", "status"], as_dict=True)
	if existing and existing.team != team and existing.status != "Deleted":
		# one installation belongs to one team; the other team must remove it first
		return _redirect(DASHBOARD + "?github=claimed")
	values = {
		"team": team,
		"account_login": account.get("login"),
		"account_type": "Organization" if account.get("type") == "Organization" else "User",
		"status": "Suspended" if info.get("suspended_at") else "Active",
		"installed_by": frappe.session.user,
		"repository_selection": info.get("repository_selection"),
	}
	if existing:
		doc = frappe.get_doc("GitHub Installation", installation_id)
		doc.update(values)
		doc.save(ignore_permissions=True)
	else:
		frappe.get_doc({"doctype": "GitHub Installation", "installation_id": installation_id, **values}).insert(
			ignore_permissions=True
		)
	frappe.db.commit()  # GET requests are not auto-committed
	return _redirect(DASHBOARD + "?github=connected")


@frappe.whitelist()
def list_installations():
	team = _team("read")
	settings = _settings()
	return {
		"configured": bool(settings.github_app_id and settings.app_slug),
		"installations": frappe.get_all(
			"GitHub Installation",
			filters={"team": team, "status": ["!=", "Deleted"]},
			fields=["name", "installation_id", "account_login", "account_type", "status", "repository_selection"],
			order_by="creation asc",
		),
	}


# --------------------------------------------------------------------------- repos / branches


@frappe.whitelist()
def list_repos(installation):
	_team_name, inst = _installation(installation)
	token = installation_token(inst.name)
	repos = _paginate("/installation/repositories", token, item_key="repositories")
	return [
		{
			"full_name": r.get("full_name"),
			"name": r.get("name"),
			"private": bool(r.get("private")),
			"default_branch": r.get("default_branch"),
			"html_url": r.get("html_url"),
			"description": r.get("description"),
		}
		for r in repos
	]


def _repo(repo):
	try:
		return repo_full_name(repo)
	except GitHubError as e:
		frappe.throw(str(e))


@frappe.whitelist()
def list_branches(installation, repo):
	_team_name, inst = _installation(installation)
	full = _repo(repo)
	token = installation_token(inst.name)
	branches = _paginate(f"/repos/{full}/branches", token)
	return [{"name": b.get("name"), "commit": (b.get("commit") or {}).get("sha")} for b in branches]


def _file_text(full, path, ref, token):
	return _request(
		"GET", f"/repos/{full}/contents/{path}", token=token, params={"ref": ref},
		accept="application/vnd.github.raw+json", ok_404=True,
	)


def inspect_repo(full, branch, token):
	"""{app_name, frappe_dependency, frappe_majors, commit} for owner/repo@branch."""
	root = _request("GET", f"/repos/{full}/contents/", token=token, params={"ref": branch}) or []
	if not isinstance(root, list):
		root = []
	names = {e.get("name") for e in root}
	contents = {}
	for fname in ("pyproject.toml", "setup.py"):
		if fname in names:
			contents[fname] = _file_text(full, fname, branch, token) or ""
	files = [e.get("name") for e in root if e.get("type") == "file"]
	from nexaploy.github_utils import pyproject_name, setup_py_name

	declared = [n for n in (pyproject_name(contents.get("pyproject.toml")), setup_py_name(contents.get("setup.py"))) if n]
	dirs = candidate_app_dirs(root)
	dirs.sort(key=lambda d: (d not in declared, d))
	for d in dirs[:5]:
		if _file_text(full, f"{d}/hooks.py", branch, token) is not None:
			files.append(f"{d}/hooks.py")
	spec = frappe_dependency(contents.get("pyproject.toml"))
	majors = majors_from_spec(spec) or majors_from_branch(branch)
	head = _request("GET", f"/repos/{full}/commits/{branch}", token=token, ok_404=True) or {}
	return {
		"app_name": detect_app_name(files, contents),
		"frappe_dependency": spec,
		"frappe_majors": majors,
		"commit": head.get("sha"),
		"commit_message": ((head.get("commit") or {}).get("message") or "").split("\n", 1)[0][:140],
	}


@frappe.whitelist(methods=["POST"])
def add_custom_app(installation, repo, branch=None, title=None, app_name=None, frappe_majors=None):
	"""Register owner/repo@branch (accessible through `installation`) as a team Custom App.
	The app name and the Frappe majors are detected from pyproject.toml / setup.py /
	<app>/hooks.py; `app_name` / `frappe_majors` override the detection."""
	team, inst = _installation(installation, "write")
	full = _repo(repo)
	token = installation_token(inst.name)
	meta = _request("GET", f"/repos/{full}", token=token)
	branch = (branch or meta.get("default_branch") or "").strip()
	if not is_valid_branch(branch):
		frappe.throw(_("Invalid branch name {0}").format(branch))
	found = inspect_repo(full, branch, token)
	name = (app_name or found["app_name"] or "").strip()
	if not is_valid_app_name(name):
		frappe.throw(
			_("Could not detect the Frappe app in {0}@{1} (no <app>/hooks.py). Enter the app name.").format(full, branch)
		)
	majors = parse_frappe_majors(frappe_majors) if frappe_majors else found["frappe_majors"]
	doc = frappe.get_doc(
		{
			"doctype": "Custom App",
			"team": team,
			"title": title or meta.get("name") or name,
			"app_name": name,
			"provider": "GitHub",
			"repo_url": f"https://github.com/{full}",
			"installation": inst.name,
			"branch": branch,
			"frappe_majors": ",".join(majors),
			"frappe_dependency": found["frappe_dependency"],
			"last_commit": found["commit"],
			"last_commit_message": found["commit_message"],
			"last_commit_at": now_datetime() if found["commit"] else None,
			"status": "Active",
		}
	)
	doc.insert(ignore_permissions=True)
	return _custom_app_dict(doc)


@frappe.whitelist(methods=["POST"])
def add_custom_app_from_url(repo_url, branch, app_name, title=None, access_token=None, access_username=None, frappe_majors=None):
	"""Non-GitHub (GitLab, Gitea, ...) repo with an optional read-only access token."""
	team = _team("write")
	doc = frappe.get_doc(
		{
			"doctype": "Custom App",
			"team": team,
			"title": title or app_name,
			"app_name": (app_name or "").strip(),
			"provider": "Other",
			"repo_url": (repo_url or "").strip(),
			"branch": (branch or "").strip(),
			"frappe_majors": ",".join(parse_frappe_majors(frappe_majors)),
			"access_username": access_username or "oauth2",
			"access_token": access_token or None,
			"status": "Active",
		}
	)
	doc.insert(ignore_permissions=True)
	return _custom_app_dict(doc)


CUSTOM_APP_FIELDS = [
	"name", "title", "app_name", "provider", "repo_url", "repo_full_name", "installation", "branch",
	"frappe_majors", "frappe_dependency", "last_commit", "last_commit_at", "last_commit_message", "status",
]


def _custom_app_dict(doc):
	return {f: doc.get(f) for f in CUSTOM_APP_FIELDS}


@frappe.whitelist()
def list_custom_apps(frappe_major=None):
	team = _team("read")
	apps = frappe.get_all(
		"Custom App", filters={"team": team}, fields=CUSTOM_APP_FIELDS, order_by="title asc"
	)
	for a in apps:
		a["majors"] = parse_frappe_majors(a.frappe_majors)
		a["compatible"] = not frappe_major or not a["majors"] or str(frappe_major) in a["majors"]
	return apps


@frappe.whitelist(methods=["POST"])
def set_custom_app_status(name, status):
	team = _team("write")
	if status not in ("Active", "Disabled"):
		frappe.throw(_("Invalid status"))
	doc = frappe.get_doc("Custom App", name)
	if doc.team != team:
		raise frappe.PermissionError(_("Custom app {0} belongs to another team").format(name))
	doc.status = status
	doc.save(ignore_permissions=True)
	return _custom_app_dict(doc)


# --------------------------------------------------------------------------- webhook


@frappe.whitelist(allow_guest=True, methods=["POST"])
def webhook():
	"""GitHub App webhook: push -> Custom App.last_commit + Release Group.update_available
	(auto_update groups also build); installation deleted/suspend/unsuspend -> status."""
	request = frappe.local.request
	body = request.get_data() or b""
	if not verify_webhook_signature(_secret("webhook_secret"), body, request.headers.get("X-Hub-Signature-256")):
		frappe.local.response["http_status_code"] = 401
		return {"ok": False, "error": "invalid signature"}
	try:
		payload = json.loads(body or b"{}")
	except ValueError:
		frappe.local.response["http_status_code"] = 400
		return {"ok": False, "error": "invalid json"}
	event = request.headers.get("X-GitHub-Event") or ""
	if event == "ping":
		return {"ok": True, "pong": True}
	if event == "installation":
		return {"ok": True, "updated": _on_installation_event(payload)}
	if event == "push":
		return {"ok": True, **_on_push(payload)}
	return {"ok": True, "ignored": event}


def _on_installation_event(payload):
	inst_id = str((payload.get("installation") or {}).get("id") or "")
	if not inst_id or not frappe.db.exists("GitHub Installation", inst_id):
		return False
	status = {"deleted": "Deleted", "suspend": "Suspended", "unsuspend": "Active"}.get(payload.get("action"))
	if not status:
		return False
	frappe.db.set_value("GitHub Installation", inst_id, "status", status)
	return True


def _on_push(payload):
	push = parse_push_event(payload)
	if not push:
		return {"apps": [], "release_groups": []}
	filters = {"provider": "GitHub", "repo_full_name": push["repo"], "status": "Active"}
	apps = frappe.get_all("Custom App", filters=filters, fields=["name", "branch", "installation"])
	if push["installation_id"]:
		# only apps whose installation is the one GitHub says sent the event
		apps = [a for a in apps if a.installation == push["installation_id"]]
	if not apps:
		return {"apps": [], "release_groups": []}
	updated = []
	for a in apps:
		if a.branch == push["branch"]:
			frappe.db.set_value(
				"Custom App",
				a.name,
				{
					"last_commit": push["commit"],
					"last_commit_message": push["message"],
					"last_commit_at": now_datetime(),
				},
			)
			updated.append(a.name)
	from nexaploy.utils import custom_app_needs_update

	rows = frappe.get_all(
		"Release Group App",
		filters={"parenttype": "Release Group", "custom_app": ["in", [a.name for a in apps]]},
		fields=["parent", "branch", "custom_app"],
	)
	for r in rows:
		r["repo"] = push["repo"]
	groups = custom_app_needs_update(rows, push["repo"], push["branch"])
	built = []
	for group in groups:
		frappe.db.set_value("Release Group", group, "update_available", 1)
		if frappe.db.get_value("Release Group", group, "auto_update"):
			try:
				dc = frappe.get_doc({"doctype": "Deploy Candidate", "release_group": group})
				dc.insert(ignore_permissions=True)
				built.append(dc.name)
			except Exception:
				frappe.log_error(title=f"Nexaploy: auto build after push failed for {group}")
	return {"apps": updated, "release_groups": groups, "deploy_candidates": built}
