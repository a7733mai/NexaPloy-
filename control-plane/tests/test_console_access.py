"""Scoped console permissions + Console Audit Log: pure logic (nexaploy.console_access_utils) and
static checks of the enforcement in nexaploy.api.observability (no Frappe install needed)."""

import ast
import os
import re
import shutil
import subprocess
from datetime import datetime

import jinja2
import pytest

from nexaploy import console_access_utils as cu

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "nexaploy")
OBS = os.path.join(APP, "api", "observability.py")
API = os.path.join(APP, "api", "console_access.py")
CORE = os.path.join(APP, "console_access.py")
JS = os.path.join(APP, "public", "js", "console_access.js")


def read(path):
	with open(path, encoding="utf-8") as fh:
		return fh.read()


# --------------------------------------------------------------------------- capabilities


def test_role_defaults():
	assert cu.effective_capabilities("Owner") == {c: True for c in cu.CAPABILITIES}
	assert cu.effective_capabilities("Admin") == {c: True for c in cu.CAPABILITIES}
	assert cu.effective_capabilities("Developer") == {
		"can_view_logs": True, "can_run_sql": True, "can_run_sql_write": False, "can_use_console": False,
	}
	assert cu.effective_capabilities("Billing") == {c: False for c in cu.CAPABILITIES}
	assert cu.effective_capabilities(None) == {c: False for c in cu.CAPABILITIES}
	assert cu.effective_capabilities(None, platform_admin=True)["can_use_console"]


def test_custom_capabilities():
	member = {"custom_capabilities": 1, "can_view_logs": 0, "can_run_sql": 0, "can_run_sql_write": 1, "can_use_console": 1}
	caps = cu.effective_capabilities("Developer", member)
	assert caps == {"can_view_logs": False, "can_run_sql": True, "can_run_sql_write": True, "can_use_console": True}
	# flags are ignored while custom_capabilities is off
	assert cu.effective_capabilities("Billing", {**member, "custom_capabilities": 0}) == cu.default_capabilities("Billing")
	# an Owner can never be restricted (no lock-out)
	assert all(cu.effective_capabilities("Owner", {"custom_capabilities": 1}).values())
	assert cu.normalize_capabilities({"can_run_sql_write": "1", "bogus": 1}) == {
		"can_view_logs": False, "can_run_sql": True, "can_run_sql_write": True, "can_use_console": False,
	}


def test_required_capability_and_allows():
	assert cu.required_capability("site.logs") == "can_view_logs"
	assert cu.required_capability("site.slow_queries") == "can_view_logs"
	assert cu.required_capability("site.sql", True) == "can_run_sql"
	assert cu.required_capability("site.sql", False) == "can_run_sql_write"
	assert cu.required_capability("site.console") == "can_use_console"
	assert cu.required_capability("site.analytics") is None
	with pytest.raises(ValueError):
		cu.required_capability("site.drop")
	dev = cu.effective_capabilities("Developer")
	assert cu.capability_allows(dev, "site.sql", True) and not cu.capability_allows(dev, "site.sql", False)
	assert not cu.capability_allows(dev, "site.console") and cu.capability_allows(dev, "site.logs")
	billing = cu.effective_capabilities("Billing")
	assert cu.capability_allows(billing, "site.analytics") and not cu.capability_allows(billing, "site.logs")


def test_two_factor_rule():
	assert cu.needs_two_factor("site.console", True) and cu.needs_two_factor("site.sql", True)
	assert not cu.needs_two_factor("site.logs", True) and not cu.needs_two_factor("site.console", False)


def test_who_edits_whom():
	assert cu.can_edit_capabilities("Owner", "Admin") and cu.can_edit_capabilities("Owner", "Developer")
	assert not cu.can_edit_capabilities("Owner", "Owner")
	assert cu.can_edit_capabilities("Admin", "Developer") and cu.can_edit_capabilities("Admin", "Billing")
	assert not cu.can_edit_capabilities("Admin", "Admin") and not cu.can_edit_capabilities("Developer", "Billing")
	assert cu.can_edit_capabilities(None, "Admin", platform_admin=True)
	assert not cu.can_edit_capabilities(None, "Owner", platform_admin=True)


def test_capability_changes():
	assert cu.capability_changes("Developer", {}, None, reset=True) == {
		"custom_capabilities": 0, "can_view_logs": 0, "can_run_sql": 0, "can_run_sql_write": 0, "can_use_console": 0,
	}
	# starts from the effective (role default) values, overrides what was sent
	values = cu.capability_changes("Developer", {}, {"can_use_console": True})
	assert values == {"custom_capabilities": 1, "can_view_logs": 1, "can_run_sql": 1, "can_run_sql_write": 0, "can_use_console": 1}
	values = cu.capability_changes("Billing", {}, {"can_run_sql_write": True})
	assert values["can_run_sql"] == 1 and values["can_view_logs"] == 0


# --------------------------------------------------------------------------- audit text


@pytest.mark.parametrize("text, secret", [
	("UPDATE `tabUser` SET api_secret='s3cr3t-value' WHERE name='x'", "s3cr3t-value"),
	("CREATE USER bob IDENTIFIED BY 'hunter2'", "hunter2"),
	("SET PASSWORD FOR bob = PASSWORD('hunter2')", "hunter2"),
	("doc.new_password = \"Pa55word!\"", "Pa55word!"),
	("frappe.utils.password.update_password('a@b.c', 'Pa55word!')", "Pa55word!"),
	("requests.get(url, headers={'Authorization': 'token abcdef123:abcdef456'})", "abcdef456"),
	("requests.get('https://bob:topsecret@example.com/x')", "topsecret"),
	("stripe.api_key = 'sk_live_abcdefgh12345678'", "sk_live_abcdefgh12345678"),
	("key = 'AKIAABCDEFGHIJKLMNOP'", "AKIAABCDEFGHIJKLMNOP"),
	("conf = {\"db_password\": \"x y z\"}", "x y z"),
	("-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----", "MIIabc"),
])
def test_redact_command_masks_secrets(text, secret):
	out = cu.redact_command(text)
	assert secret not in out and cu.MASK in out


@pytest.mark.parametrize("text", [
	"SELECT name, creation FROM `tabUser` LIMIT 20",
	"select token from `tabOAuth Bearer Token` limit 5",
	"print(frappe.db.count('User'))",
])
def test_redact_command_keeps_plain_text(text):
	assert cu.redact_command(text) == text


def test_audit_command_truncates():
	text, cut = cu.audit_command("site.sql", {"query": "select 1 -- " + "x" * 20000})
	assert cut and len(text) < cu.MAX_COMMAND_CHARS + 100 and "truncated" in text
	text, cut = cu.audit_command("site.console", {"code": "print(1)"})
	assert text == "print(1)" and not cut
	assert cu.audit_command("site.logs", {"log": "web", "lines": 200, "since": "1h"})[0] == "logs log=web lines=200 since=1h"
	assert cu.audit_command("site.slow_queries", {"limit": 50, "min_seconds": 1})[0] == "slow_queries limit=50 min_seconds=1"
	assert cu.audit_kind("site.slow_queries") == "logs" and cu.audit_kind("site.sql") == "sql"
	with pytest.raises(ValueError):
		cu.audit_kind("site.analytics")


def test_audit_status_error_duration():
	assert cu.audit_status("Pending") == "Queued" and cu.audit_status("Running") == "Running"
	assert cu.audit_status("Success", {"ok": True}) == "Success"
	assert cu.audit_status("Success", {"ok": False, "error": "ZeroDivisionError"}) == "Error"
	assert cu.audit_status("Success", {"columns": [], "error": "1064 syntax"}) == "Error"
	assert cu.audit_status("Failure", None) == "Failed"
	assert cu.audit_error(None, {"error": "x password='p'"}) == "x password='" + cu.MASK + "'"
	assert cu.audit_error("job died", None) == "job died" and cu.audit_error(None, None) is None
	assert cu.audit_duration(None, None, {"duration": 0.1234}) == 0.123
	assert cu.audit_duration(datetime(2026, 1, 1, 0, 0, 0), datetime(2026, 1, 1, 0, 0, 2, 500000)) == 2.5
	assert cu.audit_duration("2026-01-01 00:00:00", "2026-01-01 00:00:03.000001") == 3.0
	assert cu.audit_duration(None, "2026-01-01 00:00:03") is None


def test_no_frappe_import():
	assert "import frappe" not in read(os.path.join(APP, "console_access_utils.py"))


# --------------------------------------------------------------------------- enforcement (static)


def _whitelisted(path):
	tree = ast.parse(read(path))
	out = {}
	for fn in tree.body:
		if isinstance(fn, ast.FunctionDef):
			for dec in fn.decorator_list:
				target = dec.func if isinstance(dec, ast.Call) else dec
				if isinstance(target, ast.Attribute) and target.attr == "whitelist":
					out[fn.name] = (fn, dec)
	return out


def test_observability_enforces_capabilities_and_audits():
	src = read(OBS)
	assert "check_capability(row.team, kind, read_only" in src
	enqueue = src[src.index("def _enqueue("):]
	enqueue = enqueue[: enqueue.index("\n\n\n")]
	# the audit row is written for the new job and for a reused one
	assert enqueue.count("_audit(row, kind, params") == 2
	core = read(CORE)
	assert "record_denied(" in core and "frappe.db.commit()" in core
	assert "check_team_access(team, \"read\", user)" in core
	callbacks = read(os.path.join(APP, "agent_callbacks.py"))
	assert '@handles("site.sql", "site.console", "site.logs", "site.slow_queries")' in callbacks


def test_console_access_api():
	wl = _whitelisted(API)
	assert {"get_console_access", "set_member_capabilities", "set_console_two_factor", "get_console_audit",
	        "my_capabilities"} <= set(wl)
	src = read(API)
	for name, (fn, dec) in wl.items():
		body = ast.get_source_segment(src, fn)
		assert "_portal_user()" in body, name
		if name != "my_capabilities":
			assert "_admin_team(" in body, name
		if name.startswith("set_"):
			assert isinstance(dec, ast.Call) and any(k.arg == "methods" for k in dec.keywords), name
	assert 'check_team_access(team, "admin", user)' in src


def test_audit_log_doctype_is_append_only():
	ctrl = read(os.path.join(APP, "nexaploy", "doctype", "console_audit_log", "console_audit_log.py"))
	assert "read-only" in ctrl and "cannot be deleted" in ctrl
	hooks = read(os.path.join(APP, "hooks.py"))
	assert '"Console Audit Log": "nexaploy.console_access.console_audit_query"' in hooks
	assert '"Console Audit Log": "nexaploy.permissions.has_read_only_team_permission"' in hooks
	# SQL / console / log jobs are not readable through the desk / REST without the capability
	assert '"Agent Job": "nexaploy.console_access.has_agent_job_permission"' in hooks
	dashboard = read(os.path.join(APP, "api", "dashboard.py"))
	assert "capability_allows(get_capabilities(team), job.kind" in dashboard


def test_js_calls_known_methods():
	used = set(re.findall(r'call\("(\w+)"', read(JS)))
	assert used and used <= set(_whitelisted(API)), used - set(_whitelisted(API))
	insights = read(os.path.join(APP, "public", "js", "insights.js"))
	assert "p.logs ?" in insights


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_js_syntax():
	subprocess.run(["node", "--check", JS], check=True, capture_output=True)


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_page_renders(lang):
	env = jinja2.Environment(loader=jinja2.FileSystemLoader(APP), undefined=jinja2.StrictUndefined)
	env.globals["_"] = lambda s, *a: s
	html = env.get_template("www/dashboard/console_access.html").render(
		title="Console access", page="activity", lang=lang, dir="rtl" if lang == "ar" else "ltr", csrf_token="tok",
		boot={"user": "a@b.c", "messages": {}}, user="a@b.c", user_fullname="A", is_admin=False,
		team="acme", team_role="Owner", teams=[{"name": "acme", "title": "Acme", "role": "Owner"}],
	)
	assert '<script src="/assets/nexaploy/js/console_access.js"></script>' in html
	assert 'x-data="nxConsoleAccess"' in html
	assert html.count("<template") == html.count("</template>")
	assert "/dashboard/console_access" in read(os.path.join(APP, "www", "dashboard", "activity.html"))
