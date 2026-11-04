"""Managed e-mail relay and white-label brands: pure logic (no frappe) + static checks."""

import ast
import json
import os
import re

import jinja2
import pytest

import test_doctypes as td
from nexaploy import brand_utils as bu
from nexaploy import email_utils as eu

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "nexaploy")

# register the new DocTypes with the generic checks of test_doctypes.py
td.EXPECTED.update({"Email Relay Settings", "Site Email", "Brand"})


def read(path):
	with open(path, encoding="utf-8") as fh:
		return fh.read()


# =========================================================================== frappe-free modules


@pytest.mark.parametrize("module", ["email_utils.py", "brand_utils.py"])
def test_pure_modules_do_not_import_frappe(module):
	tree = ast.parse(read(os.path.join(APP, module)))
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			assert not any(a.name.split(".")[0] == "frappe" for a in node.names)
		if isinstance(node, ast.ImportFrom):
			assert (node.module or "").split(".")[0] != "frappe"


# =========================================================================== e-mail relay settings


def test_provider_defaults():
	ses = eu.provider_defaults("Amazon SES", "eu-west-1")
	assert ses["host"] == "email-smtp.eu-west-1.amazonaws.com" and ses["port"] == 587
	assert eu.provider_defaults("Amazon SES")["host"] == "email-smtp.us-east-1.amazonaws.com"
	assert eu.provider_defaults("Postmark")["spf"] == "include:spf.mtasv.net"
	assert eu.provider_defaults("unknown")["host"] == ""  # -> Generic SMTP
	with pytest.raises(ValueError):
		eu.provider_defaults("Amazon SES", "eu west;1")


def test_validate_settings_fills_presets():
	clean = eu.validate_settings({"provider": "Mailgun", "enabled": 1, "tls_mode": "SSL/TLS"})
	assert clean["smtp_host"] == "smtp.mailgun.org" and clean["smtp_port"] == 465
	assert clean["spf_include"] == "include:mailgun.org" and clean["dkim_selector"] == "smtp"
	assert clean["dmarc_policy"] == "none" and clean["daily_send_limit"] == 0
	clean = eu.validate_settings({"provider": "Generic SMTP", "smtp_host": "Relay.Example.COM", "smtp_port": "2525",
	                              "spf_include": "relay.example.com ip4:1.2.3.4", "default_from_domain": "Mail.Example.com.",
	                              "dmarc_policy": "Quarantine", "dmarc_rua": "dmarc@example.com", "daily_send_limit": "500"})
	assert clean["smtp_host"] == "relay.example.com" and clean["smtp_port"] == 2525
	assert clean["spf_include"] == "include:relay.example.com ip4:1.2.3.4"
	assert clean["default_from_domain"] == "mail.example.com" and clean["dmarc_policy"] == "quarantine"
	assert clean["daily_send_limit"] == 500


@pytest.mark.parametrize("values", [
	{"enabled": 1},  # generic without a host
	{"smtp_host": "bad host"},
	{"smtp_host": "smtp.example.com", "smtp_port": 70000},
	{"smtp_host": "smtp.example.com", "smtp_port": "x"},
	{"smtp_host": "smtp.example.com", "tls_mode": "TLS1.3"},
	{"smtp_host": "smtp.example.com", "default_from_domain": "not a domain"},
	{"smtp_host": "smtp.example.com", "spf_include": "include:x.com; rm"},
	{"smtp_host": "smtp.example.com", "dmarc_policy": "block"},
	{"smtp_host": "smtp.example.com", "dmarc_rua": "nope"},
	{"smtp_host": "smtp.example.com", "daily_send_limit": -1},
	{"smtp_host": "smtp.example.com", "dkim_selector": "bad selector"},
])
def test_validate_settings_rejects(values):
	with pytest.raises(ValueError):
		eu.validate_settings(values)


def test_sender_address():
	assert eu.sender_address("shop.nexaploy.cloud") == "noreply@shop.nexaploy.cloud"
	assert eu.sender_address("shop.nexaploy.cloud", default_from_domain="mail.nexaploy.cloud") == "noreply@mail.nexaploy.cloud"
	# an unverified custom domain is never used
	assert eu.sender_address("shop.nexaploy.cloud", "acme.com", False, "mail.nexaploy.cloud") == "noreply@mail.nexaploy.cloud"
	assert eu.sender_address("shop.nexaploy.cloud", "Acme.com", True, "mail.nexaploy.cloud") == "noreply@acme.com"


def test_configure_params_use_secret_placeholder():
	settings = {"smtp_host": "smtp.postmarkapp.com", "smtp_port": 587, "tls_mode": "STARTTLS", "username": "tok",
	            "verify_smtp": 1, "password": "never-copied"}
	ref = "__secret__:Email Relay Settings:Email Relay Settings:password"
	p = eu.configure_params("a.example.com", settings, "noreply@a.example.com", True, ref)
	assert p == {"site": "a.example.com", "enabled": True, "account_name": "Nexaploy Relay",
	             "email_id": "noreply@a.example.com", "smtp_server": "smtp.postmarkapp.com", "smtp_port": 587,
	             "tls_mode": "starttls", "username": "tok", "password": ref, "verify_smtp": True}
	assert "never-copied" not in json.dumps(p)
	p = eu.configure_params("a.example.com", dict(settings, username="", tls_mode="SSL/TLS", smtp_port=None), "x@y.zz", True, ref)
	assert p["password"] is None and p["tls_mode"] == "ssl" and p["smtp_port"] == 465
	assert eu.configure_params("a.example.com", settings, None, False, ref) == {
		"site": "a.example.com", "enabled": False, "account_name": "Nexaploy Relay"}


def test_relay_fingerprint():
	a = eu.relay_fingerprint({"smtp_host": "h", "smtp_port": 587}, "pw1")
	assert a == eu.relay_fingerprint({"smtp_host": "h", "smtp_port": 587, "dmarc_policy": "reject"}, "pw1")
	assert a != eu.relay_fingerprint({"smtp_host": "h", "smtp_port": 587}, "pw2")
	assert a != eu.relay_fingerprint({"smtp_host": "h2", "smtp_port": 587}, "pw1")


# =========================================================================== DNS records


def test_dns_records_generic():
	settings = {"provider": "Postmark", "dkim_selector": "20261104pm", "dkim_value": "k=rsa; p=MIGf",
	            "dmarc_rua": "dmarc@ops.test"}
	recs = {r["purpose"]: r for r in eu.dns_records("Acme.com", settings)}
	assert recs["SPF"] == {"purpose": "SPF", "type": "TXT", "host": "acme.com", "value": "v=spf1 include:spf.mtasv.net ~all",
	                       "required": True, "include": "include:spf.mtasv.net", "note": recs["SPF"]["note"]}
	assert recs["DKIM"]["host"] == "20261104pm._domainkey.acme.com" and recs["DKIM"]["type"] == "TXT"
	assert recs["DMARC"]["value"] == "v=DMARC1; p=none; rua=mailto:dmarc@ops.test"
	assert recs["Return-Path"] == {"purpose": "Return-Path", "type": "CNAME", "host": "pm-bounces.acme.com",
	                               "value": "pm.mtasv.net", "required": False, "note": recs["Return-Path"]["note"]}


def test_dns_records_placeholders_and_no_dkim():
	ses = eu.dns_records("acme.com", {"provider": "Amazon SES", "dkim_selector": "{domain_dashed}",
	                                  "dkim_value": "{domain_dashed}.dkim.amazonses.com", "dkim_record_type": "CNAME"})
	dkim = next(r for r in ses if r["purpose"] == "DKIM")
	assert dkim == {"purpose": "DKIM", "type": "CNAME", "host": "acme-com._domainkey.acme.com",
	                "value": "acme-com.dkim.amazonses.com", "required": True, "note": ""}
	generic = eu.dns_records("acme.com", {"provider": "Generic SMTP"})
	assert [r["purpose"] for r in generic] == ["DMARC"]  # nothing known about SPF/DKIM
	with pytest.raises(ValueError):
		eu.dns_records("not a domain", {})


def _lookup(table):
	def lookup(host, qtype):
		return table.get((host, qtype), [])
	return lookup


def test_check_records_verified():
	settings = {"provider": "Mailgun", "dkim_value": "k=rsa; p=ABC"}
	records = eu.dns_records("acme.com", settings)
	table = {
		("acme.com", "TXT"): ['"v=spf1 include:_spf.google.com " "include:mailgun.org ~all"', '"google-site-verification=x"'],
		("smtp._domainkey.acme.com", "TXT"): ['"k=rsa; p=ABC"'],
		("_dmarc.acme.com", "TXT"): ["v=DMARC1; p=reject"],
	}
	res = eu.check_records(records, _lookup(table))
	assert res["verified"] and res["checked"]
	by = {r["purpose"]: r for r in res["records"]}
	assert by["SPF"]["ok"] and by["DKIM"]["ok"] and by["DMARC"]["ok"]
	assert not by["Return-Path"]["ok"] and not by["Return-Path"]["required"]  # optional


def test_check_records_failures():
	settings = {"provider": "Amazon SES", "dkim_selector": "tok1", "dkim_value": "tok1.dkim.amazonses.com",
	            "dkim_record_type": "CNAME"}
	records = eu.dns_records("acme.com", settings)
	table = {
		("acme.com", "TXT"): ["v=spf1 include:a.com ~all", "v=spf1 include:amazonses.com ~all"],
		("tok1._domainkey.acme.com", "CNAME"): ["other.example.net."],
	}
	res = eu.check_records(records, _lookup(table))
	by = {r["purpose"]: r for r in res["records"]}
	assert not res["verified"] and res["checked"]
	assert by["SPF"]["message"] == "More than one SPF record"
	assert by["DKIM"]["message"] == "DKIM CNAME points elsewhere"
	assert by["DMARC"]["message"] == "No DMARC record"
	table[("acme.com", "TXT")] = ["v=spf1 include:a.com ~all"]
	table[("tok1._domainkey.acme.com", "CNAME")] = ["TOK1.dkim.amazonses.com."]
	table[("_dmarc.acme.com", "TXT")] = ["v=DMARC1; p=none"]
	res = eu.check_records(records, _lookup(table))
	by = {r["purpose"]: r for r in res["records"]}
	assert by["SPF"]["message"] == "SPF record does not include include:amazonses.com"
	assert by["DKIM"]["ok"] and by["DMARC"]["ok"] and not res["verified"]


def test_check_records_dns_unavailable():
	records = eu.dns_records("acme.com", {"provider": "Brevo", "dkim_value": "k=rsa; p=X"})
	res = eu.check_records(records, lambda host, qtype: None)
	assert not res["verified"] and not res["checked"]
	assert {r["message"] for r in res["records"]} == {"DNS lookup unavailable"}


def test_parse_doh_answer():
	payload = {"Status": 0, "Answer": [{"name": "acme.com.", "type": 16, "data": '"v=spf1 -all"'},
	                                    {"name": "x", "type": 5, "data": "y.example.net."}]}
	assert eu.parse_doh_answer(payload, "TXT") == ['"v=spf1 -all"']
	assert eu.parse_doh_answer(payload, "CNAME") == ["y.example.net."]
	assert eu.parse_doh_answer({"Status": 3}, "TXT") == []
	assert eu.parse_doh_answer({"Status": 2}, "TXT") is None
	assert eu.parse_doh_answer("garbage", "TXT") is None


# =========================================================================== brands


@pytest.mark.parametrize("raw,expected", [("#ABC", "#aabbcc"), ("0a7d5a", "#0a7d5a"), (" #2F5BEA ", "#2f5bea")])
def test_normalize_color(raw, expected):
	assert bu.normalize_color(raw) == expected


@pytest.mark.parametrize("raw", ["", "red", "#12345", "#ggg", "url(x)", "#123456;}body{x"])
def test_normalize_color_rejects(raw):
	with pytest.raises(ValueError):
		bu.normalize_color(raw)
	assert bu.normalize_color(raw, "#000000") == "#000000"


def test_css_variables():
	v = bu.css_variables("#0a7d5a")
	assert v["light"]["--nx-primary"] == "#0a7d5a"
	assert v["light"]["--nx-on-primary"] == "#ffffff"
	assert bu.css_variables("#ffe14d")["light"]["--nx-on-primary"] == "#111111"
	assert bu.relative_luminance(v["light"]["--nx-primary-hover"]) < bu.relative_luminance("#0a7d5a")
	assert bu.relative_luminance(v["light"]["--nx-primary-soft"]) > 0.8
	assert bu.css_block(bu.DEFAULT_PRIMARY) == ""
	block = bu.css_block("#0a7d5a")
	assert ":root { --nx-primary: #0a7d5a;" in block and "prefers-color-scheme: dark" in block
	assert "<" not in block and bu.css_block("bad-color;}") == ""  # invalid -> default -> nothing


def test_hosts_and_resolution():
	assert bu.normalize_host("Portal.Acme.COM:8443") == "portal.acme.com"
	assert bu.normalize_host("https://cloud.acme.com/x") == "cloud.acme.com"
	assert bu.parse_domains("cloud.acme.com\nCLOUD.acme.com, portal.acme.com:443") == ["cloud.acme.com", "portal.acme.com"]
	for bad in ("localhost", "bad_host.com", "a..b.com"):
		with pytest.raises(ValueError):
			bu.parse_domains(bad)
	brands = [
		{"name": "acme", "enabled": 1, "portal_domains": "cloud.acme.com"},
		{"name": "beta", "enabled": 1, "portal_domains": "beta.io\ncloud.acme.com"},
		{"name": "off", "enabled": 0, "portal_domains": "off.io"},
	]
	hosts = bu.host_map(brands)
	assert hosts == {"cloud.acme.com": "acme", "beta.io": "beta"}
	assert bu.resolve_brand_name("cloud.acme.com:443", hosts) == "acme"
	assert bu.resolve_brand_name("www.beta.io", hosts) == "beta"
	assert bu.resolve_brand_name("off.io", hosts, "default") == "default"
	assert bu.resolve_brand_name(None, hosts) is None
	assert bu.domain_conflicts("new", ["beta.io", "x.io"], brands) == ["beta.io"]
	assert bu.domain_conflicts("beta", ["beta.io"], brands) == []


def test_public_brand():
	default = bu.public_brand(None)
	assert default["title"] == "Nexaploy" and default["is_default"] and default["css"] == ""
	b = bu.public_brand({"name": "acme", "title": " Acme Cloud ", "primary_color": "#0A7D5A", "logo": "/files/acme.png",
	                     "favicon": "javascript:alert(1)", "is_default": 0, "portal_domains": "cloud.acme.com\nx.acme.com",
	                     "footer_text": "Acme Ltd."})
	assert b["title"] == "Acme Cloud" and b["primary_color"] == "#0a7d5a" and b["logo"] == "/files/acme.png"
	assert b["favicon"] is None and b["is_default"] is False and b["portal_domain"] == "cloud.acme.com"
	assert "--nx-primary: #0a7d5a" in b["css"]
	assert bu.portal_url(b, "/dashboard/team?invite=x", "https://cp.test") == "https://cloud.acme.com/dashboard/team?invite=x"
	assert bu.portal_url(default, "dashboard", "https://cp.test/") == "https://cp.test/dashboard"


def test_mail_helpers():
	assert bu.format_sender("Acme Cloud", "noreply@acme.com") == "Acme Cloud <noreply@acme.com>"
	assert bu.format_sender("Bad <name>", "noreply@acme.com") == "noreply@acme.com"
	assert bu.format_sender("Acme", None) is None
	assert bu.format_sender("Acme", "x@y\nBcc: z@w.com") is None
	with pytest.raises(ValueError):
		bu.normalize_sender_name("Acme\r\nBcc: x@y.z")
	assert bu.normalize_sender_name("  Acme   Cloud ") == "Acme Cloud"
	html = bu.apply_footer("<p>Hi</p>", "Acme <Ltd>\nSupport")
	assert html.startswith("<p>Hi</p>") and "Acme &lt;Ltd&gt;<br>Support" in html
	assert bu.apply_footer("<p>Hi</p>", "") == "<p>Hi</p>"
	assert bu.brand_subject("[Nexaploy] Disk almost full", "Acme Cloud") == "[Acme Cloud] Disk almost full"
	assert bu.brand_subject("[Nexaploy] x", "Nexaploy") == "[Nexaploy] x"
	assert bu.brand_subject("You are invited", "Acme") == "You are invited"


# =========================================================================== static wiring


def _doctype(folder):
	return json.loads(read(os.path.join(APP, "nexaploy", "doctype", folder, folder + ".json")))


def test_doctype_specs():
	relay = _doctype("email_relay_settings")
	fields = {f["fieldname"]: f for f in relay["fields"]}
	assert relay["issingle"] == 1
	assert fields["provider"]["options"].split("\n") == list(eu.PROVIDERS)
	assert fields["tls_mode"]["options"].split("\n") == list(eu.TLS_MODES)
	assert fields["password"]["fieldtype"] == "Password"
	for name in ("smtp_host", "smtp_port", "username", "default_from_domain", "daily_send_limit", "dkim_value"):
		assert name in fields
	site_email = _doctype("site_email")
	fields = {f["fieldname"]: f for f in site_email["fields"]}
	assert site_email["autoname"] == "field:site" and fields["site"]["unique"] == 1
	assert fields["team"]["options"] == "Team" and fields["team"]["fetch_from"] == "site.team"
	assert fields["status"]["options"].split("\n") == ["Disabled", "Pending", "Active", "Failed"]
	brand = _doctype("brand")
	fields = {f["fieldname"]: f for f in brand["fields"]}
	for name in ("title", "logo", "favicon", "primary_color", "support_email", "portal_domains", "footer_text",
	             "email_sender_name", "team"):
		assert name in fields
	assert fields["team"]["options"] == "Team" and not fields["team"].get("reqd")
	team = {f["fieldname"]: f for f in _doctype("team")["fields"]}
	assert team["reseller_brand"]["fieldtype"] == "Link" and team["reseller_brand"]["options"] == "Brand"
	for d in (site_email, brand):
		roles = {p["role"]: p for p in d["permissions"]}
		assert roles["Nexaploy Customer"]["read"] == 1 and not roles["Nexaploy Customer"]["write"]
	assert {p["role"] for p in relay["permissions"]} == {"System Manager"}


def test_hooks_and_job_kind():
	hooks = read(os.path.join(APP, "hooks.py"))
	for dt in ("Site Email", "Brand"):
		assert hooks.count(f'"{dt}": "nexaploy.permissions.team_query"') == 1
		assert hooks.count(f'"{dt}": "nexaploy.permissions.has_team_permission"') == 1
	assert "nexaploy.branding.update_website_context" in hooks
	assert "nexaploy.email_relay.recheck_domains" in hooks
	assert '"site.configure_email"' in read(os.path.join(APP, "agent_job.py"))
	assert '@handles("site.configure_email")' in read(os.path.join(APP, "agent_callbacks.py"))


def test_api_methods_check_team_access():
	tree = ast.parse(read(os.path.join(APP, "api", "site_email.py")))
	methods = [n for n in tree.body if isinstance(n, ast.FunctionDef)
	           and any("whitelist" in ast.unparse(d) for d in n.decorator_list)]
	assert {m.name for m in methods} == {"get_site_email", "set_managed_email", "set_sender_domain", "check_sender_domain"}
	for fn in methods:
		assert "allow_guest" not in ast.unparse(fn.decorator_list[0])
		assert re.search(r"_site\(site, '(read|write)'\)", ast.unparse(fn)), fn.name
	js = read(os.path.join(APP, "public", "js", "site_email.js"))
	used = set(re.findall(r'call\("(\w+)"', js))
	assert used and used <= {m.name for m in methods}


def test_site_page_wires_email_tab():
	html = read(os.path.join(APP, "www", "dashboard", "site.html"))
	assert "/assets/nexaploy/js/site_email.js" in html
	assert 'x-data="nxSiteEmail"' in html and "tab === 'email'" in html


def _env():
	env = jinja2.Environment(loader=jinja2.FileSystemLoader(APP), undefined=jinja2.StrictUndefined)
	env.globals["_"] = lambda s, *a: s
	return env


BASE_CTX = {"title": "Sites", "page": "sites", "lang": "en", "dir": "ltr", "csrf_token": "t", "boot": {},
            "user": "a@b.c", "user_fullname": "A", "is_admin": False, "team": "acme", "team_role": "Owner",
            "teams": []}


def test_dashboard_renders_brand():
	brand = bu.public_brand({"name": "acme", "title": "Acme <Cloud>", "primary_color": "#0a7d5a", "logo": "/files/a.png",
	                         "favicon": "/files/f.ico", "footer_text": "Acme Ltd", "support_email": "help@acme.com"})
	html = _env().get_template("www/dashboard/sites.html").render(nx_brand=brand, **BASE_CTX)
	assert html.lstrip().startswith("<!doctype html>")
	assert "<title>Sites · Acme &lt;Cloud&gt;</title>" in html
	assert '<link rel="icon" href="/files/f.ico">' in html
	assert "--nx-primary: #0a7d5a" in html and 'src="/files/a.png"' in html
	assert "help@acme.com" in html and "Acme Ltd" in html
	default = _env().get_template("www/dashboard/sites.html").render(**BASE_CTX)
	assert "· Nexaploy</title>" in default and "nx-brand-css" not in default and '<span class="nx-logo">N</span>' in default


def test_signup_base_renders_brand():
	tpl = _env().from_string('{% extends "templates/signup_base.html" %}{% block content %}x{% endblock %}')
	html = tpl.render(title="Sign up", lang="en", dir="ltr", csrf_token="", signup_boot={},
	                  nx_brand=bu.public_brand({"name": "b", "title": "Beta", "primary_color": "#aa0000", "logo": "/files/b.png"}))
	assert "· Beta</title>" in html and "--nx-primary: #aa0000" in html and 'src="/files/b.png"' in html
