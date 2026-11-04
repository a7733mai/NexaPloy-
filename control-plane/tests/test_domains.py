"""Custom domain DNS verification + TLS health: pure logic (nexaploy.domain_utils) and wiring."""

import ast
import csv
import glob
import json
import os
import re
from datetime import datetime, timedelta

import pytest

from nexaploy import domain_utils as du

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nexaploy")
DOCTYPES = os.path.join(APP, "nexaploy", "doctype")

SRV4, SRV6 = "203.0.113.10", "2001:db8::10"
PRIMARY = "erp.nexaploy.cloud"


def read(path):
	with open(path, encoding="utf-8") as fh:
		return fh.read()


def doctype(name):
	with open(os.path.join(DOCTYPES, name, name + ".json")) as fh:
		return json.load(fh)


def ans(cname=None, a=None, aaaa=None, error=None):
	return {"cname": cname or [], "a": a or [], "aaaa": aaaa or [], "error": error}


# --------------------------------------------------------------------------- purity


def test_domain_utils_has_no_frappe_import():
	tree = ast.parse(read(os.path.join(APP, "domain_utils.py")))
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			assert all(not a.name.startswith("frappe") for a in node.names)
		if isinstance(node, ast.ImportFrom):
			assert not (node.module or "").startswith("frappe")


# --------------------------------------------------------------------------- targets


def test_parse_ip_list():
	assert du.parse_ip_list("1.2.3.4, 2001:db8::1\n1.2.3.4 nope") == ["1.2.3.4", "2001:db8::1"]
	assert du.parse_ip_list(None) == [] and du.parse_ip_list(["5.6.7.8"]) == ["5.6.7.8"]


def test_server_dns_targets():
	# override wins
	assert du.server_dns_targets("198.51.100.1", SRV4, SRV6, ["8.8.4.4"]) == ["198.51.100.1"]
	# provisioning fields + global agent facts (private ones dropped)
	assert du.server_dns_targets(None, SRV4, None, ["10.0.0.2", "2a01:4f8::1"]) == [SRV4, "2a01:4f8::1"]
	# fallback: the agent's source address, only when public
	assert du.server_dns_targets(None, None, None, [], "8.8.8.8") == ["8.8.8.8"]
	assert du.server_dns_targets(None, None, None, [], "192.168.1.5") == []
	assert du.server_dns_targets("", "", "", None, None) == []


def test_cloudflare_and_apex():
	assert du.is_cloudflare_ip("104.21.3.4") and du.is_cloudflare_ip("2606:4700:3030::1")
	assert not du.is_cloudflare_ip(SRV4) and not du.is_cloudflare_ip("nope")
	assert du.is_apex("example.com") and du.is_apex("shop.com.qa") and du.is_apex("example.co.uk")
	assert not du.is_apex("erp.example.com") and not du.is_apex("erp.shop.com.qa")


def test_dns_instructions():
	sub = du.dns_instructions("ERP.Example.com.", PRIMARY, [SRV4, SRV6])
	assert sub[0] == {"type": "CNAME", "name": "erp.example.com", "value": PRIMARY, "recommended": True}
	assert [(r["type"], r["value"], r["recommended"]) for r in sub[1:]] == [("A", SRV4, False), ("AAAA", SRV6, False)]
	apex = du.dns_instructions("example.com", PRIMARY, [SRV4, SRV6])
	assert [(r["type"], r["recommended"]) for r in apex] == [("A", True), ("AAAA", False)]
	assert du.dns_instructions("example.com", PRIMARY, [SRV6])[0] == {
		"type": "AAAA", "name": "example.com", "value": SRV6, "recommended": True}
	assert du.dns_instructions("example.com", PRIMARY, []) == []


# --------------------------------------------------------------------------- DNS decision


def test_cname_to_primary_ok_even_with_other_ips():
	r = du.evaluate_dns("shop.example.com", PRIMARY, [SRV4], ans(["edge.cdn.net", PRIMARY + "."], ["192.0.2.99"]))
	assert r["ok"] and r["verification_type"] == "CNAME" and r["code"] == "ok_cname"
	assert r["text"] == f"shop.example.com is a CNAME to {PRIMARY}. DNS is correct."


def test_a_record_ok():
	r = du.evaluate_dns("example.com", PRIMARY, [SRV4, SRV6], ans(a=[SRV4], aaaa=[SRV6]))
	assert r["ok"] and r["verification_type"] == "A" and SRV6 in r["text"]
	# a CNAME to some other name that resolves to the server is fine too
	r = du.evaluate_dns("www.example.com", PRIMARY, [SRV4], ans(["lb.example.com"], [SRV4]))
	assert r["ok"] and r["verification_type"] == "A"


def test_no_record_and_dangling_cname_and_lookup_error():
	r = du.evaluate_dns("example.com", PRIMARY, [SRV4], ans())
	assert not r["ok"] and r["code"] == "no_record" and r["retry"]
	r = du.evaluate_dns("www.example.com", PRIMARY, [SRV4], ans(["gone.example.net"]))
	assert r["code"] == "dangling_cname" and r["args"] == ["www.example.com", "gone.example.net", PRIMARY]
	r = du.evaluate_dns("example.com", PRIMARY, [SRV4], ans(error="timeout"))
	assert r["code"] == "lookup_failed" and "timeout" in r["text"]


def test_wrong_ip_and_extra_records():
	r = du.evaluate_dns("example.com", PRIMARY, [SRV4], ans(a=["192.0.2.1"]))
	assert not r["ok"] and r["code"] == "wrong_ip" and r["args"] == ["example.com", "192.0.2.1", SRV4]
	r = du.evaluate_dns("example.com", PRIMARY, [SRV4], ans(a=[SRV4, "192.0.2.1"]))
	assert r["code"] == "extra_records"
	# Let's Encrypt prefers IPv6: a stray AAAA breaks HTTP-01 even with a correct A
	r = du.evaluate_dns("example.com", PRIMARY, [SRV4], ans(a=[SRV4], aaaa=["2001:db8::99"]))
	assert r["code"] == "extra_aaaa" and "2001:db8::99" in r["text"]
	r = du.evaluate_dns("www.example.com", PRIMARY, [SRV4], ans(["other.example.net"], ["192.0.2.1"]))
	assert r["code"] == "wrong_cname"


def test_cloudflare_proxied_is_explained():
	r = du.evaluate_dns("shop.example.com", PRIMARY, [SRV4], ans(a=["104.21.3.4", "172.67.1.1"]))
	assert not r["ok"] and r["proxied"] and r["code"] == "cloudflare"
	assert "DNS only" in r["text"] and "104.21.3.4" in r["text"]
	# a Cloudflare IP that *is* our target (operator override) is not "proxied"
	r = du.evaluate_dns("shop.example.com", PRIMARY, ["104.21.3.4"], ans(a=["104.21.3.4"]))
	assert r["ok"] and not r["proxied"]


def test_unknown_server_ip_asks_for_cname():
	r = du.evaluate_dns("example.com", PRIMARY, [], ans(a=["192.0.2.1"]))
	assert not r["ok"] and r["code"] == "no_server_ip" and PRIMARY in r["text"]


def test_skipped_gave_up_agent_failed():
	assert du.skipped_result()["ok"] and du.skipped_result()["verification_type"] == "Skipped"
	g = du.gave_up_result("Example.com")
	assert not g["ok"] and not g["retry"] and "72 hours" in g["text"] and "example.com" in g["text"]
	f = du.agent_failed_result("example.com", "boom")
	assert f["text"] == "The server could not add example.com: boom"


# --------------------------------------------------------------------------- scheduling


def test_backoff_and_give_up():
	t0 = datetime(2026, 11, 4, 10, 0)
	assert du.dns_check_due(t0, None, t0)
	assert not du.dns_check_due(t0, t0, t0 + timedelta(minutes=3))
	assert du.dns_check_due(t0, t0, t0 + timedelta(minutes=5))
	# after 2 h: every 15 min
	last = t0 + timedelta(hours=2)
	assert not du.dns_check_due(t0, last, last + timedelta(minutes=10))
	assert du.dns_check_due(t0, last, last + timedelta(minutes=15))
	# after 2 days: hourly
	last = t0 + timedelta(days=2)
	assert not du.dns_check_due(t0, last, last + timedelta(minutes=45))
	assert du.dns_check_due(t0, last, last + timedelta(minutes=60))
	assert not du.dns_gave_up(t0, t0 + timedelta(hours=71, minutes=59))
	assert du.dns_gave_up(t0, t0 + timedelta(hours=72))
	assert not du.dns_gave_up(None, t0)
	# total lookups over 72 h stay bounded
	checks, now, last = 0, t0, None
	while not du.dns_gave_up(t0, now):
		if du.dns_check_due(t0, last, now):
			checks, last = checks + 1, now
		now += timedelta(minutes=5)
	assert checks < 120


# --------------------------------------------------------------------------- TLS

NOW = datetime(2026, 11, 4, 12, 0)


def le(days, trusted=True):
	return {"trusted": trusted, "issuer": "Let's Encrypt (R11)", "issuer_cn": "R11",
	        "not_after": (NOW + timedelta(days=days)).isoformat() + "Z", "default_cert": False}


def test_evaluate_tls_valid_expiring_expired():
	v = du.evaluate_tls("a.example.com", le(60), NOW)
	assert v["status"] == "Valid" and v["days_left"] == 60 and v["issuer"] == "Let's Encrypt (R11)"
	assert v["expires_on"] == NOW + timedelta(days=60)
	e = du.evaluate_tls("a.example.com", le(10), NOW)
	assert e["status"] == "Expiring" and e["args"][1] == "10"
	x = du.evaluate_tls("a.example.com", le(-1), NOW)
	assert x["status"] == "Expired"
	u = du.evaluate_tls("a.example.com", dict(le(60, trusted=False), verify_error="unable to get local issuer"), NOW)
	assert u["status"] == "Untrusted" and "local issuer" in u["text"]


def test_evaluate_tls_default_cert_grace():
	info = {"trusted": False, "default_cert": True, "subject_cn": "TRAEFIK DEFAULT CERT",
	        "not_after": "2027-11-01T00:00:00Z"}
	activated = datetime(2026, 11, 4, 11, 50)
	p = du.evaluate_tls("shop.example.com", info, NOW, activated, activated + timedelta(minutes=10))
	assert p["status"] == "Pending" and p["expires_on"] is None
	d = du.evaluate_tls("shop.example.com", info, NOW, activated, activated + timedelta(minutes=16))
	assert d["status"] == "Default Certificate"
	assert "port 80" in d["text"] and "rate limit" in d["text"] and "DNS" in d["text"]
	# detected by subject CN as well
	d2 = du.evaluate_tls("x.example.com", {"subject_cn": du.TRAEFIK_DEFAULT_CN, "not_after": "2027-01-01T00:00:00Z"}, NOW)
	assert d2["status"] == "Default Certificate"


def test_evaluate_tls_errors():
	assert du.evaluate_tls("a.example.com", None, NOW) is None
	e = du.evaluate_tls("a.example.com", {"error": "ConnectionRefusedError: refused"}, NOW)
	assert e["status"] == "Error" and "refused" in e["text"]


def test_expiry_alert_thresholds():
	assert du.expiry_alert_threshold(30, None) is None
	assert du.expiry_alert_threshold(13, None) == 14
	assert du.expiry_alert_threshold(12, 14) is None  # already told at 14
	assert du.expiry_alert_threshold(6, 14) == 7
	assert du.expiry_alert_threshold(2, 7) == 3
	assert du.expiry_alert_threshold(-3, 1) == 0
	assert du.expiry_alert_threshold(-5, 0) is None
	assert du.expiry_alert_threshold(None, None) is None
	assert du.marker_to_threshold(0) is None and du.marker_to_threshold(15) == 14
	assert du.threshold_to_marker(None) == 0 and du.threshold_to_marker(0) == 1
	assert du.reset_expiry_alert(80) and not du.reset_expiry_alert(5) and not du.reset_expiry_alert(None)


def test_parse_utc():
	assert du.parse_utc("2026-12-30T00:00:00Z") == datetime(2026, 12, 30)
	assert du.parse_utc("2026-12-30T03:00:00+03:00") == datetime(2026, 12, 30)
	assert du.parse_utc("garbage") is None and du.parse_utc(None) is None


def test_render_and_messages_have_placeholders_only():
	assert du.render("{0} and {1}", ["a", 2]) == "a and 2"
	for key, msg in du.MSG.items():
		assert set(re.findall(r"\{(\w*)\}", msg)) <= {"0", "1", "2"}, key


# --------------------------------------------------------------------------- wiring (static)


def test_site_domain_fields():
	d = doctype("site_domain")
	fields = {f["fieldname"]: f for f in d["fields"]}
	status = fields["status"]["options"].split("\n")
	for s in (du.PENDING_DNS, du.VERIFYING, du.ACTIVE, du.BROKEN, du.REMOVING, du.LEGACY_PENDING):
		assert s in status
	assert fields["status"]["default"] == du.PENDING_DNS
	assert fields["verification_type"]["options"].split("\n") == list(du.VERIFICATION_TYPES)
	assert fields["tls_status"]["options"].split("\n") == list(du.TLS_STATUSES)
	for name in ("dns_check_message", "last_checked_at", "skip_dns_check", "dns_requested_at", "activated_at",
	             "tls_issuer", "tls_expires_on", "tls_checked_at", "tls_message", "proxied"):
		assert name in fields, name
	site = {f["fieldname"]: f for f in doctype("site")["fields"]}
	for name in ("tls_status", "tls_issuer", "tls_expires_on", "tls_checked_at", "tls_message", "tls_alert_marker"):
		assert name in site, name
	server = {f["fieldname"] for f in doctype("server")["fields"]}
	assert {"public_ipv4", "public_ipv6", "dns_target_ips", "agent_ip"} <= server


def test_site_row_fields_carried_on_rename():
	src = read(os.path.join(DOCTYPES, "site", "site.py"))
	tree = ast.parse(src)
	carried = next(
		ast.literal_eval(n.value) for n in tree.body
		if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None) == "DOMAIN_ROW_FIELDS"
	)
	fields = {f["fieldname"] for f in doctype("site_domain")["fields"]}
	assert set(carried) <= fields


def test_job_kind_hooks_and_callbacks():
	assert '"site.tls_status"' in read(os.path.join(APP, "agent_job.py"))
	assert '@handles("site.tls_status")' in read(os.path.join(APP, "agent_callbacks.py"))
	assert "nexaploy.domains.verify_pending_domains" in read(os.path.join(APP, "hooks.py"))
	hb = read(os.path.join(APP, "api", "agent.py"))
	assert 'kwargs.get("tls")' in hb and "agent_ip" in hb


def test_notify_is_optional():
	src = read(os.path.join(APP, "domains.py"))
	assert "from nexaploy.notify import notify\nexcept ImportError:\n\tnotify = None" in src
	assert '"ssl_expiring"' in src


def test_portal_api_checks_access():
	tree = ast.parse(read(os.path.join(APP, "api", "domains.py")))
	whitelisted = [
		fn for fn in tree.body if isinstance(fn, ast.FunctionDef)
		and any("whitelist" in ast.dump(d) for d in fn.decorator_list)
	]
	assert {fn.name for fn in whitelisted} >= {"get_domains", "add_domain", "verify_domain", "check_tls",
	                                            "set_skip_dns_check", "remove_domain", "preview_records"}
	for fn in whitelisted:
		calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
		assert any(c.func.id == "_site" for c in calls), fn.name
		assert "allow_guest" not in ast.dump(fn)


def test_portal_component_and_page():
	js = read(os.path.join(APP, "public", "js", "domains.js"))
	assert 'Alpine.data("nxDomains"' in js
	used = set(re.findall(r'call\("(\w+)"', js))
	api = read(os.path.join(APP, "api", "domains.py"))
	for name in used:
		assert f"def {name}(" in api, name
	html = read(os.path.join(APP, "www", "dashboard", "site.html"))
	assert 'x-data="nxDomains"' in html and "/assets/nexaploy/js/domains.js" in html
	assert "copy(r.value)" in html


def _ar():
	rows = []
	for path in [os.path.join(APP, "translations", "ar.csv")] + sorted(glob.glob(os.path.join(APP, "translations", "ar.*.part"))):
		with open(path, encoding="utf-8", newline="") as fh:
			rows += [r for r in csv.reader(fh) if r]
	return {r[0]: r[1] for r in rows}


def test_translations_cover_domain_strings():
	ar = _ar()
	strings = set(du.MSG.values()) | {s for s in du.TLS_STATUSES if s} | {du.PENDING_DNS, du.VERIFYING}
	js = read(os.path.join(APP, "public", "js", "domains.js"))
	strings |= set(re.findall(r'\bt\(\s*"((?:[^"\\]|\\.)*)"\s*[,)]', js))
	string_re = re.compile(r"""(?<![A-Za-z0-9.])_{1,2}\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')\s*[,)]""")
	for path in (os.path.join(APP, "api", "domains.py"), os.path.join(APP, "domains.py")):
		strings |= {m.group(1) or m.group(2) for m in string_re.finditer(read(path))}
	missing = sorted(strings - set(ar))
	assert not missing, missing
	for src in strings:
		assert sorted(re.findall(r"\{\d\}", src)) == sorted(re.findall(r"\{\d\}", ar[src])), src
