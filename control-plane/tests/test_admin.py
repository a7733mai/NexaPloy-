"""Platform Administrator dashboard: pure logic (nexaploy.admin_utils) + static checks of the API
(every method platform-admin only, secrets write-only, changes logged), page, JS and translations."""

import ast
import csv
import glob
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from decimal import Decimal

import jinja2
import pytest

from nexaploy import admin_utils as au

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "nexaploy")
DOCTYPES = os.path.join(APP, "nexaploy", "doctype")
PAGE = os.path.join(APP, "www", "dashboard", "admin.html")
PAGE_PY = os.path.join(APP, "www", "dashboard", "admin.py")
JS = os.path.join(APP, "public", "js", "admin.js")
API = os.path.join(APP, "api", "admin.py")
UTILS = os.path.join(APP, "admin_utils.py")
BASE = os.path.join(APP, "templates", "dashboard_base.html")
NOW = datetime(2026, 11, 4, 12, 0, 0)


def read(path):
	with open(path, encoding="utf-8") as fh:
		return fh.read()


def fields_of(doctype):
	folder = doctype.lower().replace(" ", "_")
	data = json.loads(read(os.path.join(DOCTYPES, folder, folder + ".json")))
	return {f["fieldname"]: f for f in data["fields"]}


# --------------------------------------------------------------------------- pure: forms


def test_utils_has_no_frappe_import():
	tree = ast.parse(read(UTILS))
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			assert all(not a.name.startswith("frappe") for a in node.names)
		if isinstance(node, ast.ImportFrom):
			assert not (node.module or "").startswith("frappe")


def test_clean_fields_coerces_and_whitelists():
	out = au.clean_fields(
		{"allow_customer_agent_upgrade": "1", "backup_retention_max_days": "90", "alert_email": " ops@example.com ",
		 "agent_repo_url": "", "registry_username": None},
		au.PLATFORM_FIELDS,
	)
	assert out == {"allow_customer_agent_upgrade": 1, "backup_retention_max_days": 90,
	               "alert_email": "ops@example.com", "agent_repo_url": None, "registry_username": None}
	assert au.clean_fields({"tax_percent": "14.5", "currency": "egp"}, au.BILLING_FIELDS) == {"tax_percent": 14.5,
	                                                                                         "currency": "EGP"}
	assert au.clean_fields({"plan_type": "Add-on"}, au.PLAN_FIELDS) == {"plan_type": "Add-on"}


@pytest.mark.parametrize(
	"values,spec,message",
	[
		({"registry_password": "x"}, au.PLATFORM_FIELDS, "Unknown setting {0}"),  # secrets only via split_secrets
		({"backup_config_hash": "x"}, au.PLATFORM_FIELDS, "Unknown setting {0}"),
		({"backup_retention_max_days": "abc"}, au.PLATFORM_FIELDS, "{0} must be a number"),
		({"grace_days": -1}, au.BILLING_FIELDS, "{0} cannot be negative"),
		({"tax_percent": -2}, au.BILLING_FIELDS, "{0} cannot be negative"),
		({"alert_email": "nope"}, au.PLATFORM_FIELDS, "{0} must be an e-mail address"),
		({"agent_repo_url": "http://x.example/a"}, au.PLATFORM_FIELDS, "{0} must be an https URL"),
		({"currency": "dollars"}, au.BILLING_FIELDS, "{0} must be a 3-letter currency code"),
		({"plan_type": "Free lunch"}, au.PLAN_FIELDS, "{0} must be one of {1}"),
		({"description": "x" * 6000}, au.PLAN_FIELDS, "{0} is too long"),
	],
)
def test_clean_fields_rejects(values, spec, message):
	with pytest.raises(au.AdminError) as exc:
		au.clean_fields(values, spec)
	assert exc.value.message == message
	assert exc.value.params and exc.value.params[0] == next(iter(values))


def test_split_secrets_write_only():
	new, clear = au.split_secrets(
		{"stripe_secret_key": "sk_live_abc", "stripe_webhook_secret": "", "paymob_api_key": None,
		 "paymob_hmac_secret": au.CLEAR},
		au.BILLING_SECRETS,
	)
	assert new == {"stripe_secret_key": "sk_live_abc"}
	assert clear == ["paymob_hmac_secret"]
	with pytest.raises(au.AdminError):
		au.split_secrets({"tax_percent": "1"}, au.BILLING_SECRETS)
	with pytest.raises(au.AdminError):
		au.split_secrets({"stripe_secret_key": "a\nb"}, au.BILLING_SECRETS)


def test_mask_never_reveals_the_secret():
	assert au.mask_secret(None) == "" and au.mask_secret("") == ""
	assert au.mask_secret("short") == au.MASK
	long = "sk_live_0123456789abcdefWXYZ"
	hint = au.mask_secret(long)
	assert hint == au.MASK + "WXYZ" and long not in hint
	states = au.secret_states({"a": long, "b": None}, ("a", "b"))
	assert states == {"a": {"set": True, "hint": au.MASK + "WXYZ"}, "b": {"set": False, "hint": ""}}
	assert long not in json.dumps(states)


def test_specs_match_doctypes_and_keep_secrets_apart():
	pairs = [
		("Nexaploy Settings", au.PLATFORM_FIELDS, au.PLATFORM_SECRETS),
		("Nexaploy Settings", au.OAUTH_FIELDS, au.OAUTH_SECRETS),
		("Billing Settings", au.BILLING_FIELDS, au.BILLING_SECRETS),
		("Email Relay Settings", au.EMAIL_FIELDS, au.EMAIL_SECRETS),
		("Nexaploy Plan", au.PLAN_FIELDS, ()),
		("Brand", au.BRAND_FIELDS, ()),
		("Agent Release", au.RELEASE_FIELDS, ()),
	]
	for doctype, spec, secrets in pairs:
		fields = fields_of(doctype)
		assert set(spec) <= set(fields), (doctype, set(spec) - set(fields))
		for name in spec:  # never a Password field through the plain path
			assert fields[name]["fieldtype"] != "Password", (doctype, name)
		for name in secrets:
			assert fields[name]["fieldtype"] == "Password", (doctype, name)
		assert not set(spec) & set(secrets)
	team = fields_of("Team")
	assert all(team[f]["fieldtype"] == "Check" for f in au.TEAM_FLAGS)
	plan_types = fields_of("Nexaploy Plan")["plan_type"]["options"].split("\n")
	assert set(au.PLAN_TYPES) == set(plan_types)


def test_retention_and_billing_checks():
	au.check_retention_limits({"backup_retention_min_days": 1, "backup_retention_max_days": 0})
	au.check_retention_limits({"backup_retention_min_days": 7, "backup_retention_max_days": 30,
	                           "backup_retention_included_days": 14})
	with pytest.raises(au.AdminError):
		au.check_retention_limits({"backup_retention_min_days": 40, "backup_retention_max_days": 30})
	with pytest.raises(au.AdminError):
		au.check_retention_limits({"backup_retention_included_days": 60, "backup_retention_max_days": 30})
	au.check_billing({"tax_percent": 14, "invoice_day": 1, "min_topup": 5, "max_topup": 100})
	for bad in ({"tax_percent": 101}, {"invoice_day": 0}, {"min_topup": 50, "max_topup": 10}):
		with pytest.raises(au.AdminError):
			au.check_billing(bad)


def test_clean_plan():
	assert au.clean_plan({"title": "Site Pro", "plan_type": "Site", "price_monthly": "25"}, new=True) == {
		"title": "Site Pro", "plan_type": "Site", "price_monthly": 25.0}
	assert au.clean_plan({"price_monthly": 9}) == {"price_monthly": 9.0}
	for values in ({"plan_type": "Site"}, {"title": "Site"}, {"title": "<b>", "plan_type": "Site"}):
		with pytest.raises(au.AdminError):
			au.clean_plan(values, new=True)


def test_clean_credit_grant():
	assert au.clean_credit_grant("10.005") == (Decimal("10.01"), "Promo")
	assert au.clean_credit_grant(-5, "Adjustment") == (Decimal("-5.00"), "Adjustment")
	for amount, kind in (("0", "Promo"), ("-1", "Promo"), ("abc", "Promo"), ("1000000", "Promo"), ("5", "TopUp")):
		with pytest.raises(au.AdminError):
			au.clean_credit_grant(amount, kind)


# --------------------------------------------------------------------------- pure: overview

RELEASES = [
	{"version": "0.3.0", "channel": "stable", "published": 1},
	{"version": "0.4.0-beta.1", "channel": "beta", "published": 1},
	{"version": "0.5.0", "channel": "stable", "published": 0},
]


def test_server_summary():
	servers = [
		{"name": "a", "status": "Active", "last_heartbeat": NOW - timedelta(minutes=1), "agent_version": "0.3.0",
		 "disk_used_percent": 40},
		{"name": "b", "status": "Active", "last_heartbeat": NOW - timedelta(hours=1), "agent_version": "0.2.0",
		 "disk_used_percent": 91.26, "hostname": "b.example"},
		{"name": "c", "status": "Offline", "last_heartbeat": None, "agent_version": None, "agent_channel": "beta"},
		{"name": "d", "status": "Pending"},
		{"name": "z", "status": "Archived", "agent_version": "0.1.0", "disk_used_percent": 99},
	]
	s = au.server_summary(servers, RELEASES, NOW)
	assert s["total"] == 4
	assert s["by_status"] == {"Active": 2, "Offline": 1, "Pending": 1}
	assert s["online"] == 1 and s["offline"] == 2  # b has a stale heartbeat
	assert s["disk_alerts"] == [{"name": "b", "hostname": "b.example", "disk_used_percent": 91.3}]
	assert s["latest"] == {"stable": "0.3.0", "beta": "0.4.0-beta.1"}
	# b (0.2.0 < 0.3.0), c (unknown on beta), d (unknown on stable)
	assert s["outdated_agents"] == 3
	assert s["agent_versions"] == {"0.3.0": 1, "0.2.0": 1, "?": 2}


def test_agent_rows_no_release():
	rows = au.agent_rows([{"name": "a", "status": "Active", "agent_version": "0.1.0"}], [])
	assert rows[0]["latest"] is None and rows[0]["outdated"] is False


def test_site_summary():
	sites = [
		{"name": "a", "status": "Active", "bench": "b15"},
		{"name": "b", "status": "Active", "bench": "b16"},
		{"name": "c", "status": "Suspended", "bench": "b15"},
		{"name": "d", "status": "Archived", "bench": "b15"},
		{"name": "e", "status": "Pending", "bench": None},
	]
	s = au.site_summary(sites, {"b15": "15", "b16": 16})
	assert s["total"] == 4
	assert s["by_status"] == {"Active": 2, "Suspended": 1, "Archived": 1, "Pending": 1}
	assert s["by_major"] == {"?": 1, "v15": 2, "v16": 1}


def test_failed_jobs():
	jobs = [
		{"name": "J1", "kind": "site.backup", "creation": NOW - timedelta(hours=3), "error": "a\nTraceback\nboom"},
		{"name": "J2", "kind": "site.backup", "creation": NOW - timedelta(hours=1), "error": None},
		{"name": "J3", "kind": "bench.update", "creation": NOW - timedelta(hours=2)},
	]
	f = au.failed_jobs(jobs, limit=2)
	assert f["count"] == 3
	assert f["by_kind"] == {"site.backup": 2, "bench.update": 1}
	assert [j["name"] for j in f["items"]] == ["J2", "J3"]
	assert f["items"][0]["url"] == "/app/agent-job/J2"
	assert au.failed_jobs(jobs)["items"][2]["error"] == "boom"


def test_mrr_and_overdue():
	subs = [
		{"team": "a", "status": "Active", "price_monthly": 10, "currency": "usd"},
		{"team": "a", "status": "Active", "price_monthly": "2.5", "currency": "USD"},
		{"team": "b", "status": "Active", "price_monthly": 100, "currency": "EGP"},
		{"team": "x", "status": "Active", "price_monthly": 999, "currency": "USD"},  # exempt
		{"team": "a", "status": "Cancelled", "price_monthly": 50, "currency": "USD"},
		{"team": "c", "status": "Active", "price_monthly": 1, "currency": None},
	]
	m = au.mrr(subs, ["x"], "USD")
	assert m == {"by_currency": {"EGP": 100.0, "USD": 13.5}, "subscriptions": 4}
	inv = [
		{"team": "a", "status": "Overdue", "currency": "USD", "amount_due": 10},
		{"team": "a", "status": "Overdue", "currency": "USD", "amount_due": "5.5"},
		{"team": "b", "status": "Unpaid", "currency": "USD", "amount_due": 7},
	]
	assert au.overdue_summary(inv) == {"count": 2, "teams": 1, "by_currency": {"USD": 15.5}}


def test_ticket_summary():
	tickets = [
		{"status": "Open", "priority": "High", "first_response_due": NOW - timedelta(hours=1)},
		{"status": "Waiting on Support", "priority": "Normal", "first_response_due": NOW - timedelta(hours=1),
		 "first_response_at": NOW - timedelta(hours=2)},
		{"status": "Closed", "priority": "Urgent"},
	]
	t = au.ticket_summary(tickets, NOW)
	assert t["open"] == 2 and t["first_response_overdue"] == 1
	assert t["by_priority"] == {"High": 1, "Normal": 1}


# --------------------------------------------------------------------------- pure: storage test


def test_profile_test_params_use_placeholder():
	p = {"name": "wasabi-eu", "provider": "Wasabi", "region": "eu-central-1", "bucket": "files",
	     "access_key": "AK", "path_prefix": "/prod/"}
	params = au.profile_test_params(p, "__secret__:Storage Profile:wasabi-eu:secret_key")
	d = params["destination"]
	assert d["type"] == "s3" and d["bucket"] == "files" and d["access_key_id"] == "AK"
	assert d["endpoint"] == "s3.eu-central-1.wasabisys.com"
	assert d["secret_access_key"].startswith("__secret__:")
	assert params["prefix"] == d["prefix"] == "prod/.nexaploy-test"
	assert au.profile_test_params({"provider": "R2", "endpoint": "x.r2.cloudflarestorage.com", "bucket": "b"},
	                              "s")["prefix"] == ".nexaploy-test"


def test_platform_credentials_never_go_to_customer_servers():
	servers = [
		{"name": "byos", "status": "Active", "team": "acme", "ownership": "BYOS"},
		{"name": "hosted", "status": "Active", "team": None, "ownership": "Hosted"},
		{"name": "cp", "status": "Active", "team": None, "ownership": "Hosted", "hosts_control_plane": 1},
		{"name": "down", "status": "Offline", "team": "acme", "ownership": "BYOS"},
	]
	assert au.pick_test_server(servers, None) == "cp"
	assert au.pick_test_server(servers[:2], None) == "hosted"
	assert au.pick_test_server(servers[:1], None) is None
	assert au.pick_test_server(servers, "acme") == "byos"
	assert au.pick_test_server(servers, "other") is None
	assert not au.may_test_on(servers[0], None)
	assert not au.may_test_on(servers[3], "acme")
	assert not au.may_test_on(servers[1], "acme")


def test_change_summary():
	before = {"a": 1, "b": "x", "c": None, "d": 2.0, "s": "old"}
	after = {"a": 1, "b": "y", "c": "", "d": 2, "s": "new"}
	assert au.change_summary(before, after, ("s",)) == {"b": {"from": "x", "to": "y"}, "s": "changed"}


# --------------------------------------------------------------------------- API (static)


def _whitelisted():
	tree = ast.parse(read(API))
	out = {}
	for fn in tree.body:
		if isinstance(fn, ast.FunctionDef):
			for dec in fn.decorator_list:
				target = dec.func if isinstance(dec, ast.Call) else dec
				if isinstance(target, ast.Attribute) and target.attr == "whitelist":
					out[fn.name] = (fn, dec)
	return out


def _functions():
	return {n.name: n for n in ast.parse(read(API)).body if isinstance(n, ast.FunctionDef)}


EXPECTED = {
	"overview", "list_platform_activity", "get_features", "save_plan", "update_platform_settings",
	"update_billing_settings", "update_storage_settings", "get_storage", "save_storage_profile",
	"delete_storage_profile", "test_storage_profile", "get_job", "use_profile_for_backups", "update_oauth_apps",
	"get_email_brands", "update_email_relay", "save_brand", "delete_brand", "get_agents", "save_agent_release",
	"set_release_published", "upgrade_outdated_agents", "upgrade_server_agent", "get_marketplace", "review_listing",
	"set_release_status", "verify_release", "hide_review", "list_teams", "set_team_flags", "suspend_team",
	"unsuspend_team", "grant_credits", "list_servers",
}


def test_api_methods_present():
	assert EXPECTED <= set(_whitelisted())


def test_every_method_is_platform_admin_only():
	for name, (fn, dec) in _whitelisted().items():
		body = fn.body[1:] if isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant) else fn.body
		first = body[0]
		assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call), name
		assert getattr(first.value.func, "id", None) == "_admin", name
		if isinstance(dec, ast.Call):
			assert not any(k.arg == "allow_guest" for k in dec.keywords), name
	admin = ast.unparse(_functions()["_admin"])
	assert "is_platform_admin(user)" in admin and "frappe.PermissionError" in admin
	assert "Guest" in admin


def test_mutations_are_post_and_logged():
	read_only = {"overview", "list_platform_activity", "get_features", "get_storage", "get_job", "get_email_brands",
	             "get_agents", "get_marketplace", "list_teams", "list_servers"}
	funcs = _functions()
	for name, (fn, dec) in _whitelisted().items():
		if name in read_only:
			continue
		assert isinstance(dec, ast.Call) and "POST" in ast.unparse(dec), name
		src = ast.unparse(fn)
		assert "_log(" in src or "_save_single(" in src, name
	assert "_log(action" in ast.unparse(funcs["_save_single"])
	assert "log_platform_activity" in ast.unparse(funcs["_log"])


def test_secrets_never_returned():
	funcs = _functions()
	src = read(API)
	# decrypted secrets only ever pass through admin_utils.secret_states
	assert src.count("get_password(") == 1
	assert "au.secret_states(plain" in ast.unparse(funcs["_single"])
	assert "get_decrypted_password" not in src
	# connection-test results only (other job results may hold customer data)
	assert "TEST_RESULT_KINDS" in ast.unparse(funcs["get_job"])
	assert "may_test_on" in ast.unparse(funcs["test_storage_profile"])


def test_reuses_existing_operator_apis():
	src = read(API)
	for ref in ("storage_api.update_storage_settings(", "storage_api.admin_save_storage_profile(",
	            "storage_api.admin_delete_storage_profile(", "storage_api.admin_use_profile_for_backups(",
	            "agent_upgrades.bulk_upgrade(", "publisher._review(", "publisher.set_release_status(",
	            "publisher.verify_release(", "marketplace.hide_review(", "invoicing.suspend_team(",
	            "invoicing.unsuspend_team(", "credits.add_credits("):
		assert ref in src, ref


def test_platform_activity_variant():
	src = read(os.path.join(APP, "activity.py"))
	assert "def log_platform_activity(action, reference_doctype=None, reference_name=None, details=None, team=None)" in src
	assert "ignore_mandatory = True" in src
	# customers' Team Activity reads filter on their teams, so team-less rows stay platform-only
	assert "`team` in" in read(os.path.join(APP, "permissions.py"))


# --------------------------------------------------------------------------- page, JS, sidebar


def test_js_component():
	js = read(JS)
	assert 'Alpine.data("nxAdmin"' in js
	used = set(re.findall(r'call\("(\w+)"', js)) | set(re.findall(r'run\("(\w+)"', js))
	used |= {m for group in re.findall(r'\[([^\]]*)\]', js[js.index("LOADERS"):js.index("};", js.index("LOADERS"))])
	         for m in re.findall(r'"(\w+)"', group)}
	assert used and used <= set(_whitelisted()), used - set(_whitelisted())
	assert "nexaploy.api.admin." in js


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_js_syntax():
	subprocess.run(["node", "--check", JS], check=True, capture_output=True)


def _env():
	env = jinja2.Environment(loader=jinja2.FileSystemLoader(APP), undefined=jinja2.StrictUndefined)
	env.globals["_"] = lambda s, *a: s
	return env


def _ctx(lang="en", is_admin=True, page="admin"):
	return dict(title="Administration", page=page, lang=lang, dir="rtl" if lang == "ar" else "ltr", csrf_token="tok",
	            boot={"user": "Administrator", "messages": {}}, user="Administrator", user_fullname="Administrator",
	            is_admin=is_admin, team="acme", team_role="Owner",
	            teams=[{"name": "acme", "title": "Acme", "role": "Owner"}])


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_page_renders(lang):
	html = _env().get_template("www/dashboard/admin.html").render(**_ctx(lang))
	assert '<script src="/assets/nexaploy/js/admin.js"></script>' in html
	assert 'x-data="nxAdmin"' in html
	assert html.count("<template") == html.count("</template>")
	assert "x-html" not in html
	assert "{{" not in html and "{%" not in html
	for tab in ("overview", "features", "storage", "email", "agents", "marketplace", "teams"):
		assert f"show('{tab}')" in html, tab
	# secret inputs are password fields and never pre-filled from the server
	for m in re.finditer(r'<input[^>]*x-model="sec\.[^"]+"[^>]*>', html):
		assert 'type="password"' in m.group(0)
	page_py = read(PAGE_PY)
	assert "no_cache = 1" in page_py and "prepare_page(" in page_py
	assert "is_platform_admin()" in page_py and "frappe.PermissionError" in page_py


def test_sidebar_link_only_for_platform_admins():
	env = _env()
	admin = env.get_template("www/dashboard/support.html").render(**_ctx(is_admin=True, page="support"))
	customer = env.get_template("www/dashboard/support.html").render(**_ctx(is_admin=False, page="support"))
	assert 'href="/dashboard/admin"' in admin
	assert 'href="/dashboard/admin"' not in customer


STRING_RE = re.compile(r"""(?<![A-Za-z0-9.])_{1,2}\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')\s*[,)]""")


def test_translations_cover_page_api_js_and_errors():
	ar = set()
	for path in [os.path.join(APP, "translations", "ar.csv")] + sorted(glob.glob(os.path.join(APP, "translations", "ar.*.part"))):
		with open(path, encoding="utf-8", newline="") as fh:
			ar |= {r[0] for r in csv.reader(fh) if r}
	strings = set()
	for path in (PAGE, PAGE_PY, API, BASE):
		strings |= {m.group(1) or m.group(2) for m in STRING_RE.finditer(read(path))}
	strings |= set(re.findall(r'\bt\(\s*"((?:[^"\\]|\\.)*)"\s*[,)]', read(JS)))
	strings |= set(re.findall(r'AdminError\(\s*"([^"]+)"', read(UTILS)))
	strings |= set(au.PLAN_TYPES) | set(au.OVERAGE_ACTIONS)
	missing = sorted(strings - ar)
	assert not missing, missing
