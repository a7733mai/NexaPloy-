"""Support tickets: pure logic (nexaploy.support_utils) + static checks of the DocTypes, API,
portal page, scheduler task and translations."""

import ast
import csv
import glob
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta

import jinja2
import pytest

from nexaploy import support_utils as su

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "nexaploy")
DOCTYPES = os.path.join(APP, "nexaploy", "doctype")
PAGE = os.path.join(APP, "www", "dashboard", "support.html")
PAGE_PY = os.path.join(APP, "www", "dashboard", "support.py")
JS = os.path.join(APP, "public", "js", "support.js")
API = os.path.join(APP, "api", "support.py")
GLUE = os.path.join(APP, "support.py")


def read(path):
	with open(path, encoding="utf-8") as fh:
		return fh.read()


def doctype(name):
	folder = name.lower().replace(" ", "_")
	return json.loads(read(os.path.join(DOCTYPES, folder, folder + ".json")))


T0 = datetime(2026, 11, 4, 9, 0, 0)


# --------------------------------------------------------------------------- SLA


def test_first_response_due_by_priority():
	assert su.first_response_due(T0, "Urgent") == T0 + timedelta(hours=1)
	assert su.first_response_due(T0, "High") == T0 + timedelta(hours=4)
	assert su.first_response_due(T0, "Normal") == T0 + timedelta(hours=12)
	assert su.first_response_due(T0, "Low") == T0 + timedelta(hours=24)
	assert su.first_response_due("2026-11-04 09:00:00.123456", None) == datetime(2026, 11, 4, 21, 0, 0, 123456)
	assert su.first_response_due("2026-11-04T09:00:00Z", "urgent") == T0 + timedelta(hours=1)
	with pytest.raises(su.TicketError):
		su.first_response_due(T0, "Critical")
	# stricter priorities answer sooner
	hours = [su.FIRST_RESPONSE_HOURS[p] for p in su.PRIORITIES]
	assert hours == sorted(hours, reverse=True)


def test_sla_state():
	s = su.sla_state
	assert s(T0, "High", now=T0 + timedelta(hours=1))["state"] == "pending"
	r = s(T0, "High", now=T0 + timedelta(hours=3, minutes=30))
	assert r["state"] == "at_risk" and r["remaining_seconds"] == 1800
	r = s(T0, "Urgent", now=T0 + timedelta(hours=2))
	assert r["state"] == "breached" and r["remaining_seconds"] == -3600
	assert s(T0, "Urgent", first_response_at=T0 + timedelta(minutes=59))["state"] == "met"
	assert s(T0, "Urgent", first_response_at=T0 + timedelta(hours=1))["state"] == "met"  # on the dot
	r = s(T0, "Urgent", first_response_at=T0 + timedelta(hours=5), now=T0)
	assert r["state"] == "missed" and r["remaining_seconds"] is None
	assert s(T0, "Low", now=T0)["due"] == T0 + timedelta(hours=24)


def test_auto_close():
	resolved = T0
	assert not su.should_auto_close("Resolved", resolved, resolved + timedelta(days=6, hours=23))
	assert su.should_auto_close("Resolved", resolved, resolved + timedelta(days=7))
	assert su.should_auto_close("Resolved", "2026-11-04 09:00:00", "2026-11-12 00:00:00")
	assert not su.should_auto_close("Waiting on Customer", resolved, resolved + timedelta(days=30))
	assert not su.should_auto_close("Resolved", None, resolved + timedelta(days=30))
	assert not su.should_auto_close("Closed", resolved, resolved + timedelta(days=30))


# --------------------------------------------------------------------------- state machine


def test_customer_and_staff_replies():
	n = su.next_status
	assert n("Open", "customer_reply") == "Open"  # still waiting for the first answer
	assert n("Open", "staff_reply") == "Waiting on Customer"
	assert n("Waiting on Customer", "customer_reply") == "Waiting on Support"
	assert n("Waiting on Support", "customer_reply") == "Waiting on Support"
	assert n("Waiting on Support", "staff_reply") == "Waiting on Customer"
	assert n("Resolved", "customer_reply") == "Waiting on Support"  # a reply reopens
	assert n("Resolved", "staff_reply") == "Waiting on Customer"
	assert n(None, "staff_reply") == "Waiting on Customer"
	for event in ("customer_reply", "staff_reply"):
		with pytest.raises(su.TicketError, match="closed"):
			n("Closed", event)


def test_resolve_close_reopen():
	n = su.next_status
	for status in ("Open", "Waiting on Customer", "Waiting on Support"):
		assert n(status, "resolve") == "Resolved"
		assert n(status, "close") == "Closed"
	assert n("Resolved", "close") == "Closed"
	assert n("Resolved", "reopen") == "Waiting on Support"
	assert n("Closed", "reopen") == "Waiting on Support"
	for status, event in (("Closed", "close"), ("Resolved", "resolve"), ("Closed", "resolve"), ("Open", "reopen"), ("Waiting on Customer", "reopen")):
		with pytest.raises(su.TicketError):
			n(status, event)
	with pytest.raises(su.TicketError):
		n("Open", "escalate")


def test_every_status_reachable_and_left():
	targets = {t for m in su._TRANSITIONS.values() for t in m.values()}
	assert targets | {"Open"} == set(su.STATUSES)
	sources = {s for m in su._TRANSITIONS.values() for s in m}
	assert sources == set(su.STATUSES)  # no dead end: Closed can be reopened


def test_subject_priority_and_limits():
	assert su.validate_subject("  Site   down \n now ") == "Site down now"
	assert len(su.validate_subject("x" * 500)) == 140
	with pytest.raises(su.TicketError):
		su.validate_subject(" a ")
	assert su.normalize_priority(None) == "Normal"
	assert su.normalize_priority("urgent") == "Urgent"
	with pytest.raises(su.TicketError):
		su.normalize_priority("P1")
	assert su.can_open_ticket(0) and su.can_open_ticket(24) and not su.can_open_ticket(25)


# --------------------------------------------------------------------------- sanitizer


@pytest.mark.parametrize(
	"dirty",
	[
		'<script>alert(1)</script><p>hi</p>',
		'<p onclick="alert(1)">hi</p>',
		'<img src=x onerror=alert(1)><p>hi</p>',
		'<a href="javascript:alert(1)">hi</a>',
		'<a href=" JaVaScRiPt:alert(1)">hi</a>',
		'<iframe src="https://evil"></iframe><p>hi</p>',
		'<style>body{display:none}</style><p>hi</p>',
		'<svg><script>alert(1)</script></svg><p>hi</p>',
		'<p style="background:url(javascript:alert(1))">hi</p>',
		'<a href="data:text/html;base64,PHNjcmlwdD4=">hi</a>',
		'<scr<script>ipt>alert(1)</script>hi',
		'<!-- <script>alert(1)</script> --><p>hi</p>',
		'<form action="https://evil"><input name=x><button>go</button></form><p>hi</p>',
	],
)
def test_sanitize_removes_active_content(dirty):
	clean = su.sanitize_html(dirty)
	low = clean.lower()
	for bad in ("<script", "onerror", "onclick", "javascript:", "<iframe", "<style", "<img", "<svg", "style=", "data:", "<form", "<input", "<button"):
		assert bad not in low, (dirty, clean)
	assert "hi" in clean


def test_sanitize_keeps_formatting_and_safe_links():
	html = '<p>Hello <strong>there</strong> <em>x</em></p><ul><li>one</li></ul><pre><code>a &lt; b</code></pre>'
	assert su.sanitize_html(html) == html
	link = su.sanitize_html('<a href="https://docs.nexaploy.cloud/x?a=1&b=2" class="c" title="t">docs</a>')
	assert link == '<a href="https://docs.nexaploy.cloud/x?a=1&amp;b=2" target="_blank" rel="noopener noreferrer nofollow">docs</a>'
	assert su.sanitize_html('<a href="mailto:help@nexaploy.cloud">mail</a>').startswith('<a href="mailto:help@nexaploy.cloud"')


def test_sanitize_escapes_and_balances():
	assert su.sanitize_html("<p>open <b>bold") == "<p>open <b>bold</b></p>"
	assert su.sanitize_html("</div><p>x</p></p>") == "<p>x</p>"
	assert su.sanitize_html('<p>"quotes" & <unknown>tags</unknown></p>') == '<p>"quotes" &amp; tags</p>'
	# a quote smuggled into href makes the URL unsafe: the link loses its href
	assert su.sanitize_html('<a href="https://x.dev/&quot;onmouseover=alert(1)">x</a>') == "<a>x</a>"


def test_sanitize_plain_text():
	assert su.sanitize_html("line 1\nline 2 & <3") == "line 1<br>line 2 &amp; &lt;3"
	assert su.sanitize_html(None) == ""
	assert len(su.sanitize_html("a" * 50000)) == su.MAX_MESSAGE_LENGTH
	long_html = "<p>" + "a" * 30000 + "</p>"
	out = su.sanitize_html(long_html)
	assert out.startswith("<p>") and out.endswith("</p>") and len(out) <= su.MAX_MESSAGE_LENGTH + 4


def test_html_to_text_and_blank():
	assert su.html_to_text("<p>Hello&nbsp;<b>you</b></p><p>Line&amp;2</p><script>x()</script>") == "Hello\xa0you\nLine&2"
	assert su.html_to_text("<p>" + "a" * 100 + "</p>", 10) == "aaaaaaaaa…"
	assert su.is_blank_message("<p><br></p>")
	assert su.is_blank_message("   ")
	assert not su.is_blank_message("<p>ok</p>")


# --------------------------------------------------------------------------- static checks


def test_pure_module():
	assert not re.search(r"^\s*(import|from)\s+frappe", read(os.path.join(APP, "support_utils.py")), re.M)


def test_doctypes():
	d = doctype("Support Ticket")
	fields = {f["fieldname"]: f for f in d["fields"]}
	for name in ("team", "subject", "site", "server", "priority", "status", "raised_by", "assigned_to",
	             "first_response_at", "resolved_at", "first_response_due", "replies"):
		assert name in fields, name
	assert fields["status"]["options"].split("\n") == list(su.STATUSES)
	assert fields["priority"]["options"].split("\n") == list(su.PRIORITIES)
	assert fields["site"]["options"] == "Site" and fields["server"]["options"] == "Server"
	assert fields["assigned_to"]["options"] == "User"
	assert fields["replies"]["options"] == "Support Ticket Reply"
	roles = {p["role"]: p for p in d["permissions"]}
	assert roles["Nexaploy Support"]["write"] == 1 and roles["Nexaploy Support"]["delete"] == 0
	assert roles["Nexaploy Customer"]["read"] == 1 and not roles["Nexaploy Customer"]["write"]
	reply = {f["fieldname"]: f for f in doctype("Support Ticket Reply")["fields"]}
	assert reply["message"]["fieldtype"] == "Text Editor"
	assert {"author", "is_staff", "created"} <= set(reply)
	ctrl = read(os.path.join(DOCTYPES, "support_ticket", "support_ticket.py"))
	assert "su.sanitize_html(" in ctrl and "next_status(" in ctrl


def test_api_whitelisted_checked_and_logged():
	tree = ast.parse(read(API))
	funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
	for name in ("options", "list_tickets", "get_ticket", "create_ticket", "reply", "close_ticket", "reopen_ticket", "staff_reply"):
		assert any("whitelist" in ast.unparse(d) for d in funcs[name].decorator_list), name
	for name in ("get_ticket", "reply", "close_ticket", "reopen_ticket"):
		assert "_ticket(" in ast.unparse(funcs[name]), name
	assert "check_team_access(doc.team, 'read')" in ast.unparse(funcs["_ticket"])
	for name in ("create_ticket", "reply"):
		assert "_log(" in ast.unparse(funcs[name]), name
	assert "_log(" in ast.unparse(funcs["_set_status"])
	assert "is_support_staff()" in ast.unparse(funcs["staff_reply"])
	assert "sanitize_html(" in ast.unparse(funcs["_ticket_dict"])


def test_glue_and_task():
	glue = read(GLUE)
	assert 'SUPPORT_ROLE = "Nexaploy Support"' in glue
	assert "team_notification_emails(ticket.team, \"ops\")" in glue
	for fn in ("support_ticket_query", "has_support_ticket_permission", "ensure_support_role", "notify_staff", "notify_customer"):
		assert f"def {fn}(" in glue, fn
	task = read(os.path.join(APP, "support_tasks.py"))
	assert "def auto_close_resolved_tickets(" in task and "should_auto_close(" in task
	install = read(os.path.join(APP, "install.py"))
	assert "create_support_role()" in install
	roles = json.loads(read(os.path.join(APP, "fixtures", "role.json")))
	assert any(r["role_name"] == "Nexaploy Support" and r["desk_access"] == 1 for r in roles)


def test_js_component():
	js = read(JS)
	assert 'Alpine.data("nxSupport"' in js
	tree = ast.parse(read(API))
	whitelisted = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
	used = set(re.findall(r'call\("(\w+)"', js)) | set(re.findall(r'_update\("(\w+)"', js))
	assert used and used <= whitelisted, used - whitelisted


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_js_syntax():
	subprocess.run(["node", "--check", JS], check=True, capture_output=True)


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_page_renders(lang):
	env = jinja2.Environment(loader=jinja2.FileSystemLoader(APP), undefined=jinja2.StrictUndefined)
	env.globals["_"] = lambda s, *a: s
	html = env.get_template("www/dashboard/support.html").render(
		title="Support", page="support", lang=lang, dir="rtl" if lang == "ar" else "ltr", csrf_token="tok",
		boot={"user": "a@b.c", "messages": {}}, user="a@b.c", user_fullname="A", is_admin=False,
		team="acme", team_role="Billing", teams=[{"name": "acme", "title": "Acme", "role": "Billing"}],
	)
	assert '<script src="/assets/nexaploy/js/support.js"></script>' in html
	assert 'x-data="nxSupport"' in html
	assert html.count("<template") == html.count("</template>")
	# the only x-html renders server-sanitised reply HTML
	assert re.findall(r'x-html="([^"]+)"', html) == ["r.message"]
	page_py = read(PAGE_PY)
	assert "no_cache = 1" in page_py and "prepare_page(" in page_py


STRING_RE = re.compile(r"""(?<![A-Za-z0-9.])_{1,2}\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')\s*[,)]""")


def test_translations_cover_page_api_and_js():
	ar = set()
	for path in [os.path.join(APP, "translations", "ar.csv")] + sorted(glob.glob(os.path.join(APP, "translations", "ar.*.part"))):
		with open(path, encoding="utf-8", newline="") as fh:
			ar |= {r[0] for r in csv.reader(fh) if r}
	strings = set()
	for path in (PAGE, PAGE_PY, API, GLUE, os.path.join(DOCTYPES, "support_ticket", "support_ticket.py")):
		strings |= {m.group(1) or m.group(2) for m in STRING_RE.finditer(read(path))}
	strings |= set(re.findall(r'\bt\(\s*"((?:[^"\\]|\\.)*)"\s*[,)]', read(JS)))
	strings |= set(su.STATUSES) | set(su.PRIORITIES)
	missing = sorted(strings - ar)
	assert not missing, missing
