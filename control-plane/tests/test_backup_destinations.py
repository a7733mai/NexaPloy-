"""Backup destinations (S3 / Google Drive / OneDrive / Dropbox), retention and restore: pure
helpers and static checks (no frappe)."""

import ast
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from nexaploy import backup_destination_utils as bdu
from nexaploy import backup_policy_utils as bpu

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "nexaploy")
DT = os.path.join(APP, "nexaploy", "doctype")
AGENT = os.path.join(os.path.dirname(ROOT), "agent", "nexaploy_agent")
SECRET = b"k" * 32
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def read(*parts):
	with open(os.path.join(*parts), encoding="utf-8") as fh:
		return fh.read()


def fields(folder):
	d = json.loads(read(DT, folder, folder + ".json"))
	return {f["fieldname"]: f for f in d["fields"]}


# --------------------------------------------------------------------------- pure modules


@pytest.mark.parametrize("module", ["backup_destination_utils.py", "backup_policy_utils.py"])
def test_pure_modules_have_no_frappe_import(module):
	tree = ast.parse(read(APP, module))
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			assert all(not a.name.startswith("frappe") for a in node.names)
		if isinstance(node, ast.ImportFrom):
			assert not (node.module or "").startswith("frappe")


# --------------------------------------------------------------------------- state + PKCE


def test_state_roundtrip_and_binding():
	claims = {"n": "nonce", "u": "a@b.c", "t": "acme", "d": "dest1", "p": "google"}
	state = bdu.sign_state(claims, SECRET, now=1000)
	got = bdu.verify_state(state, SECRET, now=1100)
	assert {k: got[k] for k in claims} == claims and got["exp"] == 1000 + bdu.STATE_TTL


@pytest.mark.parametrize("mutate", [
	lambda s: s + "x",
	lambda s: s.replace(".", ".A", 1),
	lambda s: "",
	lambda s: None,
	lambda s: "a.b.c",
	lambda s: s.split(".")[0],
])
def test_state_rejects_tampering(mutate):
	state = bdu.sign_state({"u": "a@b.c"}, SECRET, now=1000)
	assert bdu.verify_state(mutate(state), SECRET, now=1001) is None


def test_state_rejects_forged_payload_and_expiry():
	state = bdu.sign_state({"u": "a@b.c", "t": "acme"}, SECRET, now=1000)
	payload, sig = state.split(".")
	forged = bdu._b64(json.dumps({"u": "evil@x", "t": "acme", "exp": 9999999999}).encode())
	assert bdu.verify_state(f"{forged}.{sig}", SECRET, now=1001) is None
	assert bdu.verify_state(state, b"other-secret", now=1001) is None
	assert bdu.verify_state(state, SECRET, now=1000 + bdu.STATE_TTL + 1) is None


def test_pkce_s256():
	verifier, challenge = bdu.make_pkce(b"\x00" * 32)
	import base64
	import hashlib

	expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
	assert challenge == expected and 43 <= len(verifier) <= 128
	assert bdu.make_pkce()[0] != bdu.make_pkce()[0]


# --------------------------------------------------------------------------- authorization URLs


def _qs(url):
	parsed = urlparse(url)
	return parsed, {k: v[0] for k, v in parse_qs(parsed.query).items()}


def test_google_authorization_url_offline_drive_file():
	url = bdu.authorization_url("google", "cid.apps.googleusercontent.com", "https://cp.example/cb", "ST", "CH")
	parsed, q = _qs(url)
	assert parsed.netloc == "accounts.google.com"
	assert q["access_type"] == "offline" and q["prompt"] == "consent"
	assert q["scope"] == "https://www.googleapis.com/auth/drive.file"
	assert q["response_type"] == "code" and q["state"] == "ST" and q["redirect_uri"] == "https://cp.example/cb"
	assert q["code_challenge"] == "CH" and q["code_challenge_method"] == "S256"


def test_microsoft_authorization_url_tenant_and_scopes():
	url = bdu.authorization_url("microsoft", "app-id", "https://cp/cb", "ST", tenant="organizations")
	parsed, q = _qs(url)
	assert parsed.path == "/organizations/oauth2/v2.0/authorize"
	assert set(q["scope"].split()) == {"offline_access", "Files.ReadWrite", "User.Read"}
	assert "/common/" in bdu.authorization_url("microsoft", "app-id", "https://cp/cb", "ST")
	with pytest.raises(bdu.DestinationError):
		bdu.authorization_url("microsoft", "app-id", "https://cp/cb", "ST", tenant="evil/../x")


def test_dropbox_authorization_url_offline():
	_parsed, q = _qs(bdu.authorization_url("dropbox", "key", "https://cp/cb", "ST"))
	assert q["token_access_type"] == "offline" and "files.content.write" in q["scope"]


def test_authorization_url_needs_client_and_known_provider():
	with pytest.raises(bdu.DestinationError):
		bdu.authorization_url("google", "", "https://cp/cb", "ST")
	with pytest.raises(bdu.DestinationError):
		bdu.authorization_url("box", "id", "https://cp/cb", "ST")


def test_token_request():
	url, data = bdu.token_request("google", "CODE", "id", "sec", "https://cp/cb", "VER")
	assert url == "https://oauth2.googleapis.com/token"
	assert data == {"grant_type": "authorization_code", "code": "CODE", "redirect_uri": "https://cp/cb",
	                "client_id": "id", "client_secret": "sec", "code_verifier": "VER"}
	url, data = bdu.token_request("microsoft", "CODE", "id", "sec", "https://cp/cb", tenant="common")
	assert url.endswith("/common/oauth2/v2.0/token") and "offline_access" in data["scope"]
	with pytest.raises(bdu.DestinationError):
		bdu.token_request("google", "", "id", "sec", "https://cp/cb")


def test_callback_url():
	assert bdu.callback_url("https://cp.example.com/") == (
		"https://cp.example.com/api/method/nexaploy.api.backup_destinations.oauth_callback"
	)


# --------------------------------------------------------------------------- tokens


def test_rclone_token_shape():
	tok = bdu.rclone_token({"access_token": "A", "refresh_token": "R", "expires_in": 3600, "token_type": "bearer"}, NOW)
	assert tok == {"access_token": "A", "token_type": "Bearer", "refresh_token": "R", "expiry": "2026-11-04T12:59:00Z"}
	assert json.loads(bdu.token_json(tok)) == tok


@pytest.mark.parametrize("resp,match", [
	({"error": "invalid_grant", "error_description": "Bad code"}, "Bad code"),
	({"refresh_token": "R"}, "no access token"),
	({"access_token": "A"}, "no refresh token"),
	("nope", "Unexpected"),
])
def test_rclone_token_errors(resp, match):
	with pytest.raises(bdu.DestinationError, match=match):
		bdu.rclone_token(resp, NOW)


@pytest.mark.parametrize("bad", [
	"x", "[]", json.dumps({"access_token": "a"}), json.dumps({"access_token": "a\n", "refresh_token": "r"}),
	json.dumps({"access_token": "a", "refresh_token": "r", "expiry": "soon"}),
])
def test_parse_token_rejects(bad):
	with pytest.raises(bdu.DestinationError):
		bdu.parse_token(bad)


# --------------------------------------------------------------------------- account info


def test_parse_account_google():
	acc = bdu.parse_account("google", [{"user": {"emailAddress": "a@gmail.com", "displayName": "A"}}])
	assert acc == {"email": "a@gmail.com", "name": "A", "drive_id": None, "drive_type": None}


def test_parse_account_microsoft():
	acc = bdu.parse_account("microsoft", [
		{"mail": None, "userPrincipalName": "a@corp.onmicrosoft.com", "displayName": "A"},
		{"id": "b!AbC_1-2", "driveType": "business"},
	])
	assert acc["email"] == "a@corp.onmicrosoft.com" and acc["drive_id"] == "b!AbC_1-2"
	with pytest.raises(bdu.DestinationError):
		bdu.parse_account("microsoft", [{"mail": "a@b.c"}, {"id": "x y", "driveType": "personal"}])
	with pytest.raises(bdu.DestinationError):
		bdu.parse_account("microsoft", [{"mail": "a@b.c"}, {"id": "abc", "driveType": "sharepoint"}])


def test_parse_account_dropbox_and_requests():
	acc = bdu.parse_account("dropbox", [{"email": "d@x.io", "name": {"display_name": "D"}}])
	assert acc["email"] == "d@x.io" and acc["name"] == "D"
	assert [r["method"] for r in bdu.account_requests("dropbox")] == ["POST"]
	assert len(bdu.account_requests("microsoft")) == 2
	assert "drive/v3/about" in bdu.account_requests("google")[0]["url"]
	assert bdu.revoke_request("google", {"refresh_token": "R"})["data"] == {"token": "R"}
	assert bdu.revoke_request("microsoft", {"refresh_token": "R"}) is None


# --------------------------------------------------------------------------- agent params


TOKEN_REF = "__secret__:Backup Destination:d1:oauth_token"
CLIENT = {"client_id": "cid", "client_secret": "__secret__:Nexaploy Settings:Nexaploy Settings:google_client_secret",
          "tenant": "common"}


def test_drive_params():
	dest = {"destination_type": "Google Drive", "folder": "", "status": "Connected", "root_folder_id": "Fold_1"}
	p = bdu.destination_job_params(dest, "srv-1", TOKEN_REF, CLIENT)
	assert p == {"remote": "nexaploy-backups", "type": "drive", "prefix": "nexaploy-backups/srv-1",
	             "token": TOKEN_REF, "client_id": "cid", "client_secret": CLIENT["client_secret"],
	             "root_folder_id": "Fold_1"}


def test_onedrive_params_need_drive_id():
	dest = {"destination_type": "OneDrive", "folder": "erp/backups", "drive_id": "b!x", "drive_type": "business"}
	p = bdu.destination_job_params(dest, "srv-1", TOKEN_REF, {**CLIENT, "tenant": "contoso.onmicrosoft.com"})
	assert p["type"] == "onedrive" and p["prefix"] == "erp/backups/srv-1"
	assert p["drive_id"] == "b!x" and p["drive_type"] == "business" and p["tenant"] == "contoso.onmicrosoft.com"
	assert "tenant" not in bdu.destination_job_params(dest, "srv-1", TOKEN_REF, CLIENT)
	with pytest.raises(bdu.DestinationError):
		bdu.destination_job_params({**dest, "drive_id": None}, "srv-1", TOKEN_REF, CLIENT)


def test_dropbox_params_without_client():
	p = bdu.destination_job_params({"destination_type": "Dropbox"}, "srv-1", TOKEN_REF, {"client_id": ""})
	assert p == {"remote": "nexaploy-backups", "type": "dropbox", "prefix": "nexaploy-backups/srv-1", "token": TOKEN_REF}


def test_s3_params_match_agent_contract():
	dest = {"destination_type": "S3", "folder": "bk", "s3_bucket": "acme-bk", "s3_access_key": "AK",
	        "s3_provider": "Wasabi", "s3_endpoint": "https://s3.wasabisys.com", "s3_region": ""}
	p = bdu.destination_job_params(dest, "srv-1", "__secret__:Backup Destination:d:s3_secret_key")
	assert p["type"] == "s3" and p["bucket"] == "acme-bk" and p["access_key_id"] == "AK"
	assert p["secret_access_key"].startswith("__secret__:") and p["region"] is None and p["prefix"] == "bk/srv-1"
	assert "token" not in p


def test_agent_validates_cp_params():
	"""The params the control plane builds pass the agent's own validation."""
	import sys

	sys.path.insert(0, os.path.dirname(AGENT))
	try:
		from nexaploy_agent.jobs.server_jobs import ConfigureBackupsParams
	except ImportError:  # agent deps not installed
		pytest.skip("agent not importable")
	tok = bdu.token_json({"access_token": "A", "refresh_token": "R", "expiry": "2026-11-04T12:59:00Z"})
	client = {"client_id": "cid.apps.googleusercontent.com", "client_secret": "sec", "tenant": "common"}
	for dest in (
		{"destination_type": "Google Drive", "root_folder_id": "Fold_1"},
		{"destination_type": "OneDrive", "drive_id": "b!x", "drive_type": "personal"},
		{"destination_type": "Dropbox"},
	):
		ConfigureBackupsParams.model_validate(bdu.destination_job_params(dest, "srv-1", tok, client))
	s3 = {"destination_type": "S3", "s3_bucket": "acme-bk", "s3_access_key": "AK"}
	ConfigureBackupsParams.model_validate(bdu.destination_job_params(s3, "srv-1", "secret"))


def test_folder_and_s3_validation():
	assert bdu.normalize_folder(" /a/b/ ") == "a/b"
	assert bdu.normalize_folder(None) == bdu.DEFAULT_FOLDER
	for bad in ("../x", "a/../b", "a b", "a;b"):
		with pytest.raises(bdu.DestinationError):
			bdu.normalize_folder(bad)
	bdu.validate_s3({"s3_bucket": "acme-bk", "s3_access_key": "AK"})
	for bad in ({"s3_bucket": "Bad_Bucket", "s3_access_key": "AK"}, {"s3_bucket": "acme", "s3_access_key": ""},
	            {"s3_bucket": "acme", "s3_access_key": "AK", "s3_endpoint": "ftp://x y"}):
		with pytest.raises(bdu.DestinationError):
			bdu.validate_s3(bad)


def test_usable_and_fingerprint():
	assert bdu.destination_usable({"destination_type": "S3", "s3_bucket": "b", "s3_access_key": "k"})
	assert not bdu.destination_usable({"destination_type": "Dropbox", "status": "Not Connected"})
	assert bdu.destination_usable({"destination_type": "Dropbox", "status": "Connected"})
	d = {"destination_type": "Dropbox", "folder": "x"}
	a = bdu.destination_fingerprint(d, "tok1")
	assert a == bdu.destination_fingerprint(d, "tok1")
	assert a != bdu.destination_fingerprint(d, "tok2")
	assert a != bdu.destination_fingerprint(d, "tok1", {"client_id": "new"})
	assert "tok1" not in a
	assert bdu.effective_destination(None, "team-default") == "team-default"
	assert bdu.effective_destination("own", "team-default") == "own"


def test_remote_test_params():
	params = bdu.destination_job_params({"destination_type": "Dropbox"}, "srv-1", TOKEN_REF)
	assert bdu.remote_test_params(params) == {"destination": params, "prefix": "nexaploy-backups/srv-1"}


# --------------------------------------------------------------------------- retention policy


def test_policy_defaults_and_effective():
	assert bpu.from_doc(None) == bpu.DEFAULT_POLICY
	team = {"backup_retention_days": 30, "backup_local_retention_days": 0}
	site = {"backup_retention_custom": 0, "backup_retention_days": 7}
	assert bpu.effective_policy(site, team)["retention_days"] == 30
	assert bpu.effective_policy(site, team)["local_retention_days"] == 0
	assert bpu.effective_policy({**site, "backup_retention_custom": 1}, team)["retention_days"] == 7
	assert bpu.to_doc(bpu.DEFAULT_POLICY)["backup_local_retention_days"] == 14


@pytest.mark.parametrize("bad", [{"retention_days": -1}, {"keep_daily": "x"}, {"keep_monthly": 1000}])
def test_policy_normalize_rejects(bad):
	with pytest.raises(bpu.PolicyError):
		bpu.normalize_policy(bad)


def test_policy_limits():
	p = bpu.normalize_policy({"retention_days": 30, "keep_monthly": 6})
	assert bpu.retention_span_days(p) == 186
	assert bpu.check_policy(p, {})["requires_addon"] is False
	with pytest.raises(bpu.PolicyError, match="at most 90"):
		bpu.check_policy(p, {"backup_retention_max_days": 90})
	with pytest.raises(bpu.PolicyError, match="at least 7"):
		bpu.check_policy(bpu.normalize_policy({"retention_days": 3}), {"backup_retention_min_days": 7})
	with pytest.raises(bpu.PolicyError, match="on the server"):
		bpu.check_policy(bpu.normalize_policy({"local_retention_days": 10}), {"backup_local_retention_max_days": 3})
	with pytest.raises(bpu.PolicyError, match="monthly"):
		bpu.check_policy(p, {"backup_keep_monthly_max": 3})
	# forever is beyond any max
	with pytest.raises(bpu.PolicyError):
		bpu.check_policy(bpu.normalize_policy({"retention_days": 0}), {"backup_retention_max_days": 365})


def test_policy_paid_addon():
	limits = {"backup_retention_included_days": 30}
	p = bpu.normalize_policy({"retention_days": 14, "keep_weekly": 8})  # 56 days span
	with pytest.raises(bpu.PolicyError, match="add-on"):
		bpu.check_policy(p, limits, addon_active=False)
	assert bpu.check_policy(p, limits, addon_active=True)["requires_addon"] is True
	assert bpu.check_policy(bpu.normalize_policy({"retention_days": 14}), limits)["requires_addon"] is False


def test_limits_from_settings():
	assert bpu.limits_from({}) == bpu.LIMIT_FIELDS
	assert bpu.limits_from({"backup_retention_max_days": "90", "backup_keep_monthly_max": -3})[
		"backup_retention_max_days"] == 90


def test_prune_params():
	assert bpu.prune_params(bpu.normalize_policy({"retention_days": 0})) is None
	assert bpu.prune_params(bpu.normalize_policy({"retention_days": 30, "keep_weekly": 4})) == {
		"keep_days": 30, "keep_min": 3, "keep_daily": 0, "keep_weekly": 4, "keep_monthly": 0,
	}


def test_restore_params():
	assert bpu.restore_params("a.example.com", "r:p/b/a/ts") == {
		"site": "a.example.com", "remote_path": "r:p/b/a/ts", "restore": "all"}
	db = bpu.restore_params("a.example.com", "r:p", "database", bench="erp-v15")
	assert db["with_files"] is False and db["bench"] == "erp-v15"
	for args in (("x", "r:p", "tables"), ("x", "", "all"), ("x", "r:p", "files", "erp")):
		with pytest.raises(bpu.PolicyError):
			bpu.restore_params(*args)
	assert bpu.restorable_parts([{"file_type": "database"}], 0) == ["all", "database"]
	assert "files" in bpu.restorable_parts([{"file_type": "database"}, {"file_type": "public"}], 1)


# --------------------------------------------------------------------------- static checks


def test_backup_destination_doctype():
	f = fields("backup_destination")
	assert f["team"]["options"] == "Team" and f["team"]["reqd"] == 1
	assert f["destination_type"]["options"].split("\n") == list(bdu.DESTINATION_TYPES)
	assert f["status"]["options"].split("\n") == list(bdu.STATUSES)
	for secret in bdu.SECRET_FIELDS:
		assert f[secret]["fieldtype"] == "Password", secret
	for name in ("account_email", "account_name", "folder", "is_default", "drive_id", "drive_type", "config_hash"):
		assert name in f
	perms = json.loads(read(DT, "backup_destination", "backup_destination.json"))["permissions"]
	customer = next(p for p in perms if p["role"] == "Nexaploy Customer")
	assert customer["write"] == 0 and customer["create"] == 0  # portal API only


def test_related_fields():
	assert fields("server")["backup_destination"]["options"] == "Backup Destination"
	settings = fields("nexaploy_settings")
	for id_field, secret_field in bdu.SETTINGS_CLIENT_FIELDS.values():
		assert settings[id_field]["fieldtype"] == "Data" and settings[secret_field]["fieldtype"] == "Password"
	for name in bpu.LIMIT_FIELDS:
		assert settings[name]["fieldtype"] == "Int"
	for doc in ("site", "team"):
		f = fields(doc)
		for name in bpu.DOC_FIELDS.values():
			assert f[name]["fieldtype"] == "Int", (doc, name)
	assert fields("site")["backup_retention_custom"]["fieldtype"] == "Check"
	assert fields("team")["extended_backup_retention"]["read_only"] == 1


def test_permission_hooks_and_admin_action():
	hooks = read(APP, "hooks.py")
	assert '"Backup Destination": "nexaploy.permissions.team_query"' in hooks
	assert '"Backup Destination": "nexaploy.permissions.has_team_permission"' in hooks
	from nexaploy.team_utils import required_action

	assert required_action("Backup Destination", "write") == "admin"
	assert required_action("Backup Destination", "read") == "read"


def test_resolve_secret_allow_list():
	src = read(APP, "api", "agent.py")
	assert 'doctype == "Backup Destination"' in src and "SECRET_FIELDS" in src
	assert "dest_team != server_team" in src
	assert bdu.SECRET_FIELDS == {"oauth_token", "s3_secret_key"}


def test_job_kind_in_sync_with_agent():
	cp = read(APP, "agent_job.py")
	assert '"backup.test_remote"' in cp
	kinds = next(f for f in json.loads(read(DT, "agent_job", "agent_job.json"))["fields"] if f["fieldname"] == "kind")
	assert "backup.test_remote" in kinds["options"].split("\n")
	assert 'kind = "backup.test_remote"' in read(AGENT, "jobs", "server_jobs.py")
	assert '"backup.test_remote"' in read(APP, "agent_callbacks.py")


def _whitelisted(path):
	tree = ast.parse(read(path))
	out = {}
	for fn in tree.body:
		if not isinstance(fn, ast.FunctionDef):
			continue
		for dec in fn.decorator_list:
			target = dec.func if isinstance(dec, ast.Call) else dec
			if isinstance(target, ast.Attribute) and target.attr == "whitelist":
				guest = isinstance(dec, ast.Call) and any(
					k.arg == "allow_guest" and getattr(k.value, "value", False) for k in dec.keywords
				)
				out[fn.name] = (fn, guest)
	return out


API = os.path.join(APP, "api", "backup_destinations.py")


def test_api_methods():
	wl = _whitelisted(API)
	expected = {
		"overview", "save_destination", "delete_destination", "start_oauth", "oauth_callback",
		"disconnect_destination", "test_destination", "set_server_destination", "set_team_retention",
		"set_site_retention", "list_site_backups", "restore_backup",
	}
	assert expected <= set(wl)
	assert [a.arg for a in wl["start_oauth"][0].args.args] == ["provider", "destination"]
	# only the OAuth redirect target is guest-allowed; every other method resolves the team first
	assert {n for n, (_fn, guest) in wl.items() if guest} == {"oauth_callback"}
	for name, (fn, guest) in wl.items():
		if guest:
			continue
		body = fn.body[1] if isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant) else fn.body[0]
		calls = [n for n in ast.walk(body) if isinstance(n, ast.Call)]
		assert any(isinstance(c.func, ast.Name) and c.func.id == "_ctx" for c in calls), name


def test_oauth_callback_checks():
	src = read(API)
	cb = src[src.index("def oauth_callback"):src.index("def _revoke")]
	assert "verify_state" in cb and 'claims.get("u") != frappe.session.user' in cb
	assert "delete_value(key)" in cb  # nonce is single-use
	assert 'check_team_access(team, "admin")' in cb
	assert "frappe.db.commit()" in cb  # GET is not auto-committed
	assert "access_token" not in re.sub(r'token\["access_token"\]', "", cb)  # never logged/redirected


def test_js_calls_known_methods():
	js = read(APP, "public", "js", "backups.js")
	used = set(re.findall(r'call\("(\w+)"', js))
	assert used and used <= set(_whitelisted(API)), used - set(_whitelisted(API))


def test_sidebar_link():
	base = read(APP, "templates", "dashboard_base.html")
	assert 'href="/dashboard/backups"' in base
	assert "/assets/nexaploy/js/backups.js" in read(APP, "www", "dashboard", "backups.html")


def test_site_backup_passes_local_retention_but_not_for_moves():
	src = read(DT, "site_backup", "site_backup.py")
	assert 'params["local_retention_days"]' in src and 'self.purpose != "Move"' in src


def test_patch_registered():
	assert "nexaploy.patches.v0_3.backup_retention_custom" in read(APP, "patches.txt")
