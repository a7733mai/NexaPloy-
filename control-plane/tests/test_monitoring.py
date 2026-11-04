"""External uptime monitoring and public status pages (ARCHITECTURE.md §16).

Pure logic (nexaploy.monitor_utils, nexaploy.probe) plus static checks of the DocTypes, hooks,
API modules, portal page, public status page and translations. No frappe needed.
"""

import ast
import csv
import glob
import http.server
import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone

import jinja2
import pytest

import test_doctypes as td
from nexaploy import monitor_utils as mu
from nexaploy import notify_utils as nu
from nexaploy import probe

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "nexaploy")
DOCTYPES = os.path.join(APP, "nexaploy", "doctype")
API = os.path.join(APP, "api", "monitoring.py")
PROBE_API = os.path.join(APP, "api", "monitoring_probe.py")
MONITORING = os.path.join(APP, "monitoring.py")
PAGE = os.path.join(APP, "www", "dashboard", "monitoring.html")
PAGE_PY = os.path.join(APP, "www", "dashboard", "monitoring.py")
JS = os.path.join(APP, "public", "js", "monitoring.js")
STATUS_HTML = os.path.join(APP, "www", "status.html")
STATUS_PY = os.path.join(APP, "www", "status.py")
STATUS_BASE = os.path.join(APP, "templates", "status_base.html")

NEW_DOCTYPES = {
	"Uptime Monitor", "Uptime Probe", "Uptime Check Result", "Uptime Rollup", "Uptime Incident",
	"Status Incident Update", "Status Page", "Status Page Monitor",
}
NEW_CHILD_TABLES = {"Status Incident Update", "Status Page Monitor"}
# register the new DocTypes with the generic checks of test_doctypes.py
td.EXPECTED.update(NEW_DOCTYPES)
td.CHILD_TABLES.update(NEW_CHILD_TABLES)

NOW = datetime(2026, 11, 4, 12, 0, 0)


def read(path):
	with open(path, encoding="utf-8") as fh:
		return fh.read()


def doctype(name):
	folder = name.lower().replace(" ", "_")
	with open(os.path.join(DOCTYPES, folder, folder + ".json")) as fh:
		return json.load(fh)


# =========================================================================== pure modules


@pytest.mark.parametrize("path", ["monitor_utils.py", "probe.py"])
def test_pure_modules_do_not_import_frappe(path):
	tree = ast.parse(read(os.path.join(APP, path)))
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			assert not any(a.name.split(".")[0] == "frappe" for a in node.names)
		if isinstance(node, ast.ImportFrom):
			assert (node.module or "").split(".")[0] != "frappe"
			assert not (node.module or "").startswith("nexaploy")  # probe.py ships standalone


# --------------------------------------------------------------------------- slugs & domains


@pytest.mark.parametrize("raw,expected", [("acme", "acme"), (" Acme-Status ", "acme-status"), ("a1b", "a1b"), ("x" * 50, "x" * 50)])
def test_validate_slug(raw, expected):
	assert mu.validate_slug(raw) == expected


@pytest.mark.parametrize("raw", ["", "ab", "-acme", "acme-", "ac me", "acme--x", "x" * 51, "admin", "status", "dashboard", "café"])
def test_validate_slug_rejects(raw):
	with pytest.raises(mu.MonitorError):
		mu.validate_slug(raw)


def test_suggest_slug():
	assert mu.suggest_slug("Acme Corp!") == "acme-corp"
	assert mu.suggest_slug("A") == "a-status"
	assert mu.suggest_slug("Admin") == "admin-status"
	assert mu.validate_slug(mu.suggest_slug("ŞŞ shop")) == "shop"


def test_validate_domain():
	assert mu.validate_domain("") == ""
	assert mu.validate_domain("Status.Example.com.") == "status.example.com"
	assert mu.validate_domain("https://status.example.com/x") == "status.example.com"
	for bad in ("localhost", "bad_host.com", "-a.com", "a..b"):
		with pytest.raises(mu.MonitorError):
			mu.validate_domain(bad)


def test_probe_name():
	assert mu.validate_probe_name(" FRA-1 ") == "fra-1"
	for bad in ("", "-x", "x" * 41, "a b"):
		with pytest.raises(mu.MonitorError):
			mu.validate_probe_name(bad)


# --------------------------------------------------------------------------- expected status


def test_parse_expected_and_matches():
	assert mu.parse_expected("200") == [(200, 200)]
	assert mu.parse_expected("2xx, 301-302") == [(200, 299), (301, 302)]
	assert mu.parse_expected("") == [(200, 399)]
	assert mu.status_matches(204, "2xx") and not mu.status_matches(404, "2xx")
	assert mu.status_matches(302, "200,301-302") and not mu.status_matches(303, "200,301-302")
	assert not mu.status_matches(None, "200")
	assert mu.status_matches(200, "garbage")  # stored garbage falls back to the default
	for bad in ("20", "600", "299-200", "abc", "6xx", ",".join(["200"] * 11)):
		with pytest.raises(mu.MonitorError):
			mu.parse_expected(bad)


@pytest.mark.parametrize("code,expected,ok", [(200, "200", True), (204, "2xx", True), (500, "200-399", False), (301, "300-399", True)])
def test_probe_status_ok_matches_monitor_utils(code, expected, ok):
	assert probe._status_ok(code, expected) is ok
	assert mu.status_matches(code, expected) is ok


def test_evaluate_response():
	assert mu.evaluate_response(200, b"hello world", "200", "world") == (True, "")
	assert mu.evaluate_response(200, b"hello", "200", "world") == (False, "Keyword not found")
	assert mu.evaluate_response(502, b"", "2xx") == (False, "Unexpected status 502")
	assert mu.evaluate_response(None, None, "200") == (False, "No response")
	assert mu.evaluate_response(None, None, "200", error="Timeout after 10s") == (False, "Timeout after 10s")


# --------------------------------------------------------------------------- monitor validation

HOSTS = ["shop.nexaploy.cloud", "www.shop.com"]


def test_validate_monitor_defaults():
	clean = mu.validate_monitor({"title": "  Shop  front ", "path": "/products?x=1#frag"}, HOSTS, ["control-plane"])
	assert clean["title"] == "Shop front"
	assert clean["hostname"] == "shop.nexaploy.cloud"
	assert clean["url"] == "https://shop.nexaploy.cloud/products?x=1"
	assert clean["method"] == "GET" and clean["expected_status"] == "200-399"
	assert clean["interval_minutes"] == 5 and clean["timeout_seconds"] == 10 and clean["failure_threshold"] == 2
	assert clean["probes"] == "" and clean["quorum"] == 0 and clean["enabled"] == 1


def test_validate_monitor_custom_domain_and_probes():
	clean = mu.validate_monitor(
		{"title": "Shop", "hostname": "WWW.Shop.com", "path": "/", "method": "head", "interval_minutes": "15",
		 "probes": "fra-1, control-plane", "quorum": 2, "enabled": "0"},
		HOSTS, ["control-plane", "fra-1"],
	)
	assert clean["hostname"] == "www.shop.com" and clean["method"] == "HEAD" and clean["interval_minutes"] == 15
	assert clean["probes"] == "fra-1,control-plane" and clean["quorum"] == 2 and clean["enabled"] == 0


@pytest.mark.parametrize("data", [
	{"title": ""},
	{"title": "x" * 81},
	{"title": "x", "hostname": "evil.example.com"},  # not one of the site's hosts (no SSRF)
	{"title": "x", "hostname": "169.254.169.254"},
	{"title": "x", "path": "no-slash"},
	{"title": "x", "path": "/a b"},
	{"title": "x", "method": "POST"},
	{"title": "x", "expected_status": "abc"},
	{"title": "x", "keyword": "k", "method": "HEAD"},
	{"title": "x", "keyword": "k" * 201},
	{"title": "x", "interval_minutes": 2},
	{"title": "x", "timeout_seconds": 1},
	{"title": "x", "timeout_seconds": 31},
	{"title": "x", "failure_threshold": 0},
	{"title": "x", "probes": "nowhere"},
	{"title": "x", "probes": "control-plane", "quorum": 2},
])
def test_validate_monitor_rejects(data):
	with pytest.raises(mu.MonitorError):
		mu.validate_monitor(data, HOSTS, ["control-plane"])


def test_plan_limits():
	assert mu.limits(False) == {"min_interval": 5, "max_per_site": 3}
	assert mu.limits(True)["min_interval"] == 1
	mu.check_plan_limits(5, 3, False)
	mu.check_plan_limits(1, 4, True)
	with pytest.raises(mu.MonitorError, match="advanced"):
		mu.check_plan_limits(1, 1, False)
	with pytest.raises(mu.MonitorError, match="advanced"):
		mu.check_plan_limits(5, 4, False)
	with pytest.raises(mu.MonitorError):
		mu.check_plan_limits(5, mu.ADVANCED_MAX_PER_SITE + 1, True)


def test_default_monitor_is_valid():
	d = mu.default_monitor("shop.nexaploy.cloud")
	assert d["url"] == "https://shop.nexaploy.cloud/api/method/ping" and d["is_default"] == 1
	clean = mu.validate_monitor(d, ["shop.nexaploy.cloud"], ["control-plane"])
	assert clean["url"] == d["url"] and clean["expected_status"] == "200"
	mu.check_plan_limits(d["interval_minutes"], 1, False)


def test_assigned_probes_and_due():
	assert mu.assigned_probes("", ["a", "b"]) == ["a", "b"]
	assert mu.assigned_probes("b, c", ["a", "b"]) == ["b"]
	assert mu.is_due(None, 5, NOW)
	assert not mu.is_due(NOW - timedelta(minutes=4), 5, NOW)
	assert mu.is_due(NOW - timedelta(minutes=4, seconds=50), 5, NOW)  # scheduler slack
	assert mu.is_due(NOW - timedelta(minutes=1), 1, NOW)


def test_probe_location():
	builtin = {"is_builtin": 1}
	assert mu.probe_location(builtin, "srv-1", "srv-1") == "same_host"
	assert mu.probe_location(builtin, "srv-2", "srv-1") == "external"
	assert mu.probe_location(builtin, "srv-1", None) == "external"  # hosted control plane
	assert mu.probe_location({"is_builtin": 0}, "srv-1", "srv-1") == "external"


def test_clean_result():
	r = mu.clean_result({"monitor": "m1", "checked_at": "2026-11-04T11:59:30Z", "ok": True, "status_code": 200,
	                     "response_ms": "123", "tls_expires_on": "2027-01-01T00:00:00Z"}, NOW)
	assert r["checked_at"] == datetime(2026, 11, 4, 11, 59, 30) and r["ok"] == 1 and r["response_ms"] == 123
	assert r["tls_expires_on"] == datetime(2027, 1, 1)
	assert mu.clean_result({"monitor": "m1", "checked_at": "2020-01-01T00:00:00Z"}, NOW)["checked_at"] == NOW
	assert mu.clean_result({"monitor": "m1", "checked_at": "2030-01-01T00:00:00Z"}, NOW)["checked_at"] == NOW
	bad = mu.clean_result({"monitor": "m1", "status_code": 999, "response_ms": -5, "ok": "false", "error": "x" * 900}, NOW)
	assert bad["status_code"] is None and bad["response_ms"] == 0 and bad["ok"] == 0 and len(bad["error"]) <= 300
	assert mu.clean_result({"monitor": ""}, NOW) is None


def test_tls_days_left():
	assert mu.tls_days_left(NOW + timedelta(days=10, hours=1), NOW) == 10
	assert mu.tls_days_left(NOW - timedelta(hours=1), NOW) == -1
	assert mu.tls_days_left(None, NOW) is None


# --------------------------------------------------------------------------- state machine


def _res(ok, minutes_ago=0, err="Timeout"):
	return {"ok": ok, "checked_at": NOW - timedelta(minutes=minutes_ago), "status_code": 200 if ok else None,
	        "response_ms": 100, "error": "" if ok else err}


def test_apply_result_counts_consecutive_failures():
	s = mu.apply_result({}, "a", _res(False, 3))
	s = mu.apply_result(s, "a", _res(False, 2))
	assert s["a"]["fails"] == 2 and not s["a"]["ok"] and s["a"]["error"] == "Timeout"
	s = mu.apply_result(s, "a", _res(True, 1))
	assert s["a"]["fails"] == 0 and s["a"]["ok"]
	# out of order (older than the last) results are ignored
	assert mu.apply_result(s, "a", _res(False, 10)) == s


def test_quorum_for():
	assert mu.quorum_for(0) == 0
	assert [mu.quorum_for(n) for n in (1, 2, 3, 4, 5)] == [1, 2, 2, 3, 3]
	assert mu.quorum_for(3, 1) == 1 and mu.quorum_for(3, 9) == 3


def test_decide_single_probe_threshold():
	s = mu.apply_result({}, "cp", _res(False, 1))
	assert mu.decide(s, NOW, 5, threshold=2) is None  # one failure is not enough
	s = mu.apply_result(s, "cp", _res(False, 0))
	assert mu.decide(s, NOW, 5, threshold=2) == "down"
	s = mu.apply_result(s, "cp", _res(True, 0))
	assert mu.decide(s, NOW, 5, threshold=2) == "up"


def test_decide_quorum_ignores_one_bad_region():
	s = {}
	for p in ("a", "b", "c"):
		s = mu.apply_result(s, p, _res(True, 1))
	for _ in range(3):
		s = mu.apply_result(s, "a", _res(False, 0))  # one probe's network is broken
	assert mu.decide(s, NOW, 5, threshold=2) == "up"
	s = mu.apply_result(s, "b", _res(False, 0))
	assert mu.decide(s, NOW, 5, threshold=2) is None  # b failed once: below threshold, no majority up
	s["b"]["fails"] = 2
	assert mu.decide(s, NOW, 5, threshold=2) == "down"  # 2 of 3


def test_decide_stale_and_unassigned_probes_do_not_count():
	s = mu.apply_result({}, "old", _res(False, 30))
	s["old"]["fails"] = 5
	s = mu.apply_result(s, "new", _res(True, 0))
	assert mu.decide(s, NOW, 5) == "up"  # old result (> 3 intervals) ignored
	assert mu.decide(s, NOW, 5, assigned=["old"]) is None
	assert mu.decide({}, NOW, 5) is None
	assert mu.freshness_seconds(1) == 300 and mu.freshness_seconds(15) == 2700


@pytest.mark.parametrize("current,decision,expected", [
	("Pending", "up", ("Up", None)),
	("Pending", "down", ("Down", "open")),
	("Up", "down", ("Down", "open")),
	("Down", "down", ("Down", None)),
	("Down", "up", ("Up", "close")),
	("Up", "up", ("Up", None)),
	("Up", None, ("Up", None)),
	("Down", None, ("Down", None)),
	("Paused", "down", ("Paused", None)),
	(None, "up", ("Up", None)),
])
def test_next_status(current, decision, expected):
	assert mu.next_status(current, decision) == expected


def test_full_incident_lifecycle_alerts_once():
	"""Simulate two probes over time: exactly one open and one close."""
	state, status, actions = {}, "Pending", []
	timeline = [(True, True)] * 3 + [(False, False)] * 4 + [(True, False)] + [(True, True)] * 2
	for i, (a_ok, b_ok) in enumerate(timeline):
		at = NOW - timedelta(minutes=5 * (len(timeline) - i))
		for p, ok in (("a", a_ok), ("b", b_ok)):
			state = mu.apply_result(state, p, {"ok": ok, "checked_at": at, "error": "" if ok else "Unexpected status 502"})
		status, action = mu.next_status(status, mu.decide(state, at, 5, 2))
		if action:
			actions.append(action)
	assert actions == ["open", "close"] and status == "Up"


def test_failure_started_tracks_first_failure_of_streak():
	s = mu.apply_result({}, "a", _res(True, 20))
	s = mu.apply_result(s, "a", _res(False, 10))
	s = mu.apply_result(s, "a", _res(False, 5))
	s = mu.apply_result(s, "b", _res(False, 4))
	assert s["a"]["since"] == mu.iso(NOW - timedelta(minutes=10))
	assert mu.failure_started(s, NOW, 5) == NOW - timedelta(minutes=10)
	s = mu.apply_result(s, "a", _res(True, 0))
	assert s["a"]["since"] is None
	assert mu.failure_started(s, NOW, 5) == NOW - timedelta(minutes=4)
	assert mu.failure_started({}, NOW, 5, NOW) == NOW


def test_failing_summary():
	s = mu.apply_result({}, "a", _res(False, 0, "Unexpected status 502"))
	s = mu.apply_result(s, "b", _res(False, 0, "Unexpected status 502"))
	s = mu.apply_result(s, "c", _res(True, 0))
	assert mu.failing_summary(s, NOW, 5) == "Unexpected status 502 (2/3 probes)"
	assert mu.failing_summary({}, NOW, 5) == ""


# --------------------------------------------------------------------------- rollups & uptime


def test_rollup_and_merge():
	results = [
		{"monitor": "m", "checked_at": datetime(2026, 11, 4, 10, 1), "ok": 1, "response_ms": 100},
		{"monitor": "m", "checked_at": datetime(2026, 11, 4, 10, 59), "ok": 1, "response_ms": 300},
		{"monitor": "m", "checked_at": datetime(2026, 11, 4, 10, 30), "ok": 0, "response_ms": 10000},
		{"monitor": "m", "checked_at": datetime(2026, 11, 4, 11, 0), "ok": 1, "response_ms": None},
	]
	agg = mu.rollup(results)
	h10 = agg[("m", datetime(2026, 11, 4, 10))]
	assert h10 == {"checks": 3, "failures": 1, "response_ms_sum": 400, "response_count": 2, "max_response_ms": 300}
	assert agg[("m", datetime(2026, 11, 4, 11))]["response_count"] == 0
	merged = mu.merge_rollup(h10, h10)
	assert merged["checks"] == 6 and merged["max_response_ms"] == 300
	assert mu.merge_rollup(None, h10) == h10
	assert mu.rollup_key("abc", datetime(2026, 11, 4, 9)) == "abc-2026110409"


def test_response_series_fills_gaps():
	rows = [{"hour": datetime(2026, 11, 4, 10), "response_ms_sum": 400, "response_count": 2, "checks": 3, "failures": 1}]
	series = mu.response_series(rows, datetime(2026, 11, 4, 9, 30), 3)
	assert [p["ms"] for p in series] == [None, 200, None]
	assert series[1]["failures"] == 1 and series[0]["t"] == "2026-11-04T09:00:00Z"


def test_daily_uptime():
	now = datetime(2026, 11, 4, 12, 0)
	incidents = [
		{"started_at": datetime(2026, 11, 3, 23, 0), "resolved_at": datetime(2026, 11, 4, 1, 0)},  # spans midnight
		{"started_at": datetime(2026, 11, 4, 11, 30), "resolved_at": None},  # still open
	]
	days = mu.daily_uptime(incidents, now, days=3, monitored_since=datetime(2026, 11, 3, 0, 0))
	assert [d["date"] for d in days] == ["2026-11-02", "2026-11-03", "2026-11-04"]
	assert days[0]["uptime"] is None and days[0]["seconds"] == 0  # not monitored yet
	assert days[1]["down_seconds"] == 3600 and days[1]["incidents"] == 1
	assert days[1]["uptime"] == round(100 * (1 - 3600 / 86400), 3)
	# today: 1 h (00:00-01:00) + 30 min (open) out of 12 h observed
	assert days[2]["down_seconds"] == 5400 and days[2]["seconds"] == 12 * 3600 and days[2]["incidents"] == 2
	overall = mu.uptime_percent(days)
	assert overall == round(100 * (1 - 9000 / (86400 + 43200)), 3)
	assert mu.uptime_percent([{"uptime": None, "seconds": 0, "down_seconds": 0}]) is None


def test_daily_uptime_without_incidents_is_100():
	days = mu.daily_uptime([], NOW, days=90, monitored_since=NOW - timedelta(days=200))
	assert len(days) == 90 and all(d["uptime"] == 100.0 for d in days)
	assert mu.uptime_percent(days) == 100.0


def test_bar_levels_and_format():
	assert [mu.bar_level(v) for v in (None, 100, 99.95, 99.5, 97, 50)] == ["none", "up", "up", "minor", "major", "down"]
	assert mu.format_duration(45) == "45s"
	assert mu.format_duration(125) == "2m 5s"
	assert mu.format_duration(3600 * 3 + 60) == "3h 1m"
	assert mu.format_duration(86400 * 3) == "3d"


def test_overall_status():
	assert mu.overall_status([]) == "unknown"
	assert mu.overall_status(["Pending"]) == "unknown"
	assert mu.overall_status(["Up", "Up", "Paused"]) == "operational"
	assert mu.overall_status(["Up", "Down"]) == "partial"
	assert mu.overall_status(["Down", "Down", "Pending"]) == "major"


# --------------------------------------------------------------------------- status pages


def test_validate_status_page():
	clean = mu.validate_status_page(
		{"title": " Acme  status ", "monitors": [{"monitor": "m1", "display_name": "Shop"}, "m2", {"monitor": "m1"}],
		 "status_domain": "Status.Acme.com", "enabled": True, "show_incident_history": 0},
		["m1", "m2"],
	)
	assert clean["slug"] == "acme-status" and clean["title"] == "Acme status"
	assert clean["monitors"] == [{"monitor": "m1", "display_name": "Shop"}, {"monitor": "m2", "display_name": ""}]
	assert clean["status_domain"] == "status.acme.com" and clean["show_incident_history"] == 0
	for bad in ({"title": ""}, {"title": "x", "slug": "admin"}, {"title": "x", "monitors": ["other-team"]},
	            {"title": "x", "description": "d" * 501}, {"title": "x", "status_domain": "bad domain"}):
		with pytest.raises(mu.MonitorError):
			mu.validate_status_page(bad, ["m1"])


def test_validate_incident_update():
	assert mu.validate_incident_update({"status": "investigating", "message": " Looking "}) == {
		"status": "Investigating", "message": "Looking", "title": ""}
	for bad in ({"status": "Fixed", "message": "x"}, {"status": "Resolved", "message": ""},
	            {"status": "Resolved", "message": "x" * 2001}):
		with pytest.raises(mu.MonitorError):
			mu.validate_incident_update(bad)


def test_public_views_leak_nothing_internal():
	monitor = {"name": "abc123", "title": "shop.nexaploy.cloud", "status": "Down", "url": "https://shop/x",
	           "last_error": "Connection refused 10.0.0.5", "probe_state": "{}", "site": "shop.nexaploy.cloud"}
	days = mu.daily_uptime([], NOW, 3, NOW - timedelta(days=5))
	view = mu.public_monitor(monitor, "Online shop", days)
	assert set(view) == {"name", "status", "uptime", "days"}
	assert view["name"] == "Online shop" and view["status"] == "Down" and len(view["days"]) == 3
	text = json.dumps(view)
	for secret in ("https://", "10.0.0.5", "abc123", "shop.nexaploy.cloud"):
		assert secret not in text
	inc = {"status": "Resolved", "started_at": NOW - timedelta(hours=1), "resolved_at": NOW,
	       "cause": "Unexpected status 502 (2/2 probes) internal-host", "monitor": "abc123"}
	pub = mu.public_incident(inc, "Online shop", [{"status": "Resolved", "message": "Fixed", "posted_at": NOW}])
	assert pub["title"] == "Online shop is unavailable" and pub["duration"] == "1h"
	assert pub["updates"][0]["posted_display"] == "2026-11-04 12:00 UTC"
	assert "internal-host" not in json.dumps(pub) and "abc123" not in json.dumps(pub)
	assert mu.public_incident({**inc, "public_title": "Payment outage"}, "x", [])["title"] == "Payment outage"


# --------------------------------------------------------------------------- probe (real HTTP)


class _Handler(http.server.BaseHTTPRequestHandler):
	def do_GET(self):  # noqa: N802
		code = 200 if self.path.startswith("/ok") else 503
		body = b"hello nexaploy" if code == 200 else b"down"
		self.send_response(code)
		self.send_header("Content-Length", str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	def do_HEAD(self):  # noqa: N802
		self.send_response(204)
		self.end_headers()

	def log_message(self, *args):
		pass


@pytest.fixture(scope="module")
def http_url():
	server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
	thread = threading.Thread(target=server.serve_forever, daemon=True)
	thread.start()
	yield f"http://127.0.0.1:{server.server_port}"
	server.shutdown()


def test_perform_check_against_local_server(http_url):
	ok = probe.perform_check({"monitor": "m", "url": http_url + "/ok", "expected_status": "200", "keyword": "nexaploy", "timeout": 5})
	assert ok["ok"] and ok["status_code"] == 200 and ok["response_ms"] >= 0 and ok["error"] == ""
	kw = probe.perform_check({"monitor": "m", "url": http_url + "/ok", "expected_status": "200", "keyword": "missing"})
	assert not kw["ok"] and kw["error"] == "Keyword not found"
	down = probe.perform_check({"monitor": "m", "url": http_url + "/down", "expected_status": "2xx"})
	assert not down["ok"] and down["status_code"] == 503 and down["error"] == "Unexpected status 503"
	head = probe.perform_check({"monitor": "m", "url": http_url + "/", "method": "HEAD", "expected_status": "204"})
	assert head["ok"]
	assert probe.perform_check({"monitor": "m", "url": "ftp://x"})["error"] == "Invalid URL"
	refused = probe.perform_check({"monitor": "m", "url": "http://127.0.0.1:1/", "timeout": 2})
	assert not refused["ok"] and refused["error"]
	results = probe.run_checks([{"monitor": str(i), "url": http_url + "/ok"} for i in range(5)], workers=3)
	assert [r["monitor"] for r in results] == ["0", "1", "2", "3", "4"] and all(r["ok"] for r in results)
	assert mu.clean_result(ok, datetime.now(timezone.utc).replace(tzinfo=None))["ok"] == 1  # probe output is accepted by the control plane


def test_probe_due_checks_and_cli():
	checks = [{"monitor": "a", "interval": 1}, {"monitor": "b", "interval": 5}]
	assert [c["monitor"] for c in probe.due_checks(checks, {}, 1000)] == ["a", "b"]
	assert [c["monitor"] for c in probe.due_checks(checks, {"a": 950, "b": 950}, 1000)] == ["a"]
	with pytest.raises(SystemExit):
		probe.main([])  # --url / --token required
	src = read(os.path.join(APP, "probe.py"))
	assert src.startswith("#!/usr/bin/env python3") and 'if __name__ == "__main__":' in src
	assert probe.TOKEN_HEADER == mu.PROBE_TOKEN_HEADER
	assert "Authorization" not in re.sub(r'""".*?"""', "", src, flags=re.S)


# =========================================================================== static checks


def test_doctypes():
	m = {f["fieldname"]: f for f in doctype("Uptime Monitor")["fields"]}
	for name in ("title", "team", "site", "hostname", "path", "url", "method", "expected_status", "keyword",
	             "interval_minutes", "timeout_seconds", "failure_threshold", "probes", "quorum", "enabled", "is_default",
	             "status", "last_checked_at", "last_response_ms", "tls_expires_on", "open_incident", "probe_state"):
		assert name in m, name
	assert m["status"]["options"].split("\n") == list(mu.MONITOR_STATUSES)
	assert m["method"]["options"].split("\n") == list(mu.METHODS)
	assert m["team"]["options"] == "Team" and m["site"]["options"] == "Site"
	inc = {f["fieldname"]: f for f in doctype("Uptime Incident")["fields"]}
	assert inc["status"]["options"].split("\n") == list(mu.INCIDENT_STATUSES)
	assert inc["updates"]["options"] == "Status Incident Update"
	upd = {f["fieldname"]: f for f in doctype("Status Incident Update")["fields"]}
	assert upd["status"]["options"].split("\n") == list(mu.UPDATE_STATUSES)
	page = {f["fieldname"]: f for f in doctype("Status Page")["fields"]}
	assert page["slug"]["unique"] == 1 and page["team"]["unique"] == 1 and "status_domain" in page
	assert page["monitors"]["options"] == "Status Page Monitor"
	rollup = doctype("Uptime Rollup")
	assert rollup["autoname"] == "field:rollup_key"
	assert {f["fieldname"] for f in rollup["fields"]} >= {"checks", "failures", "response_ms_sum", "response_count", "max_response_ms", "hour"}
	probe_dt = {f["fieldname"]: f for f in doctype("Uptime Probe")["fields"]}
	assert probe_dt["token_hash"].get("hidden") == 1
	# internal tables: platform admins only; customers read them through the API
	for name in ("Uptime Probe", "Uptime Check Result", "Uptime Rollup"):
		assert {p["role"] for p in doctype(name)["permissions"]} == {"System Manager"}, name
	for name in ("Uptime Monitor", "Uptime Incident", "Status Page"):
		assert "Nexaploy Customer" in {p["role"] for p in doctype(name)["permissions"]}, name
	for dt in ("Team", "Nexaploy Plan"):
		assert "advanced_monitoring" in {f["fieldname"] for f in doctype(dt)["fields"]}, dt


def test_hooks():
	hooks = read(os.path.join(APP, "hooks.py"))
	for dt in ("Uptime Monitor", "Uptime Incident", "Status Page"):
		assert hooks.count(f'"{dt}":') == 2, dt  # permission_query_conditions + has_permission
	assert '"nexaploy.monitoring.run_builtin_probe"' in hooks
	assert '"nexaploy.monitoring.prune_monitoring_data"' in hooks
	assert '"nexaploy.tasks.check_sites_uptime"' in hooks
	assert '{"from_route": "/status/<slug>", "to_route": "status"}' in hooks
	tasks = read(os.path.join(APP, "tasks.py"))
	fn = next(n for n in ast.parse(tasks).body if isinstance(n, ast.FunctionDef) and n.name == "check_sites_uptime")
	assert "ensure_default_monitors" in ast.unparse(fn) and "requests" not in ast.unparse(fn)  # no second checker


def test_monitoring_module():
	src = read(MONITORING)
	funcs = {n.name: n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef)}
	for name in ("ensure_default_monitors", "run_builtin_probe", "ingest", "prune_monitoring_data", "status_page_data",
	             "resolve_status_page", "monitors_for_probe", "site_hosts", "advanced_monitoring"):
		assert name in funcs, name
	notify = ast.unparse(funcs["_notify"])
	for const in ("nu.SITE_DOWN", "nu.SITE_RECOVERED", "nu.MONITOR_DOWN", "nu.MONITOR_UP"):
		assert const in notify, const
	assert "m.is_default" in notify
	advance = ast.unparse(funcs["_advance"])
	assert "for_update=True" in advance and "mu.decide(" in advance and "mu.next_status(" in advance
	assert "'is_up'" in advance  # Site.is_up kept for compatibility
	ingest = ast.unparse(funcs["ingest"])
	assert "monitors_for_probe(probe_name)" in ingest and "mu.clean_result" in ingest
	prune = ast.unparse(funcs["prune_monitoring_data"])
	assert "RAW_RETENTION_HOURS" in prune and "ROLLUP_RETENTION_DAYS" in prune
	cache = ast.unparse(funcs["status_page_data"])
	assert "STATUS_CACHE_SECONDS" in cache and "frappe.cache()" in cache
	from nexaploy.probe import perform_check  # the built-in probe runs the same check as nexaploy-probe

	assert "from nexaploy.probe import run_checks" in ast.unparse(funcs["run_builtin_probe"]) and perform_check


def test_notification_events():
	assert nu.MONITOR_DOWN in nu.EVENTS and nu.MONITOR_UP in nu.EVENTS
	assert nu.DEFAULT_EMAIL[nu.MONITOR_DOWN] == "ops"
	assert nu.SITE_DOWN in nu.EVENTS and nu.SITE_RECOVERED in nu.EVENTS  # backwards compatible
	assert nu.MONITOR_DOWN not in nu.PLATFORM_COPY_EVENTS


def _whitelisted(path):
	out = {}
	for fn in ast.parse(read(path)).body:
		if isinstance(fn, ast.FunctionDef):
			for dec in fn.decorator_list:
				if "whitelist" in ast.unparse(dec):
					out[fn.name] = (fn, ast.unparse(dec))
	return out


def test_portal_api():
	wl = _whitelisted(API)
	expected = {"overview", "get_monitor", "save_monitor", "delete_monitor", "list_incidents", "update_incident",
	            "add_incident_update", "get_status_page", "save_status_page"}
	assert expected <= set(wl)
	for name, (fn, dec) in wl.items():
		assert "allow_guest" not in dec, name
		body = ast.unparse(fn)
		assert "_ctx(" in body or "_monitor(" in body or "_incident(" in body, name
	for name in ("save_monitor", "delete_monitor", "update_incident", "add_incident_update", "save_status_page"):
		body = ast.unparse(wl[name][0])
		assert "'write'" in body, name
		assert "_log(" in body, name
		assert "POST" in wl[name][1], name
	src = read(API)
	assert "mu.validate_monitor(" in src and "mon.site_hosts(" in src and "mu.validate_status_page(" in src
	assert "check_team_access(doc.team, action)" in src


def test_probe_api():
	wl = _whitelisted(PROBE_API)
	for name in ("checks", "results", "download_script"):
		assert "allow_guest=True" in wl[name][1], name
	for name in ("checks", "results"):
		assert "_authenticate()" in ast.unparse(wl[name][0]), name
	assert "allow_guest" not in wl["rotate_token"][1]
	assert "frappe.only_for('System Manager')" in ast.unparse(wl["rotate_token"][0])
	src = read(PROBE_API)
	assert "hmac.compare_digest" in src and "sha256" in src and "mu.PROBE_TOKEN_HEADER" in src


def test_controllers():
	mon = read(os.path.join(DOCTYPES, "uptime_monitor", "uptime_monitor.py"))
	assert "mu.validate_monitor(" in mon and "mu.check_plan_limits(" in mon and "site_hosts(self.site)" in mon
	page = read(os.path.join(DOCTYPES, "status_page", "status_page.py"))
	assert "mu.validate_slug(" in page and "clear_status_cache" in page and 'get_value("Uptime Monitor", row.monitor, "team") != self.team' in page
	probe_ctrl = read(os.path.join(DOCTYPES, "uptime_probe", "uptime_probe.py"))
	assert "set_new_token" in probe_ctrl


# --------------------------------------------------------------------------- portal page


def _env():
	env = jinja2.Environment(loader=jinja2.FileSystemLoader(APP), undefined=jinja2.StrictUndefined)
	env.globals["_"] = lambda s, *a: s
	return env


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_portal_page_renders(lang):
	html = _env().get_template("www/dashboard/monitoring.html").render(
		title="Monitoring", page="monitoring", lang=lang, dir="rtl" if lang == "ar" else "ltr", csrf_token="tok",
		boot={"user": "a@b.c", "messages": {}}, user="a@b.c", user_fullname="A", is_admin=False,
		team="acme", team_role="Developer", teams=[{"name": "acme", "title": "Acme", "role": "Developer"}],
	)
	assert '<script src="/assets/nexaploy/js/monitoring.js"></script>' in html
	assert 'x-data="nxMonitoring"' in html and 'href="/dashboard/monitoring"' in html
	assert f'dir="{"rtl" if lang == "ar" else "ltr"}"' in html
	assert html.count("<template") == html.count("</template>")
	assert "x-html" not in html and "cdn" not in html.lower()
	assert "<svg class=\"nx-mon-chart\"" in html
	page_py = read(PAGE_PY)
	assert "no_cache = 1" in page_py and 'prepare_page(context, "monitoring"' in page_py


def test_js_component():
	js = read(JS)
	assert 'Alpine.data("nxMonitoring"' in js
	used = set(re.findall(r'call\("(\w+)"', js))
	assert used and used <= set(_whitelisted(API)), used - set(_whitelisted(API))
	assert "http://" not in js and "https://" not in js  # no CDN / external requests


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_js_syntax():
	subprocess.run(["node", "--check", JS], check=True, capture_output=True)


# --------------------------------------------------------------------------- public status page


def _status_data():
	days = mu.daily_uptime([{"started_at": NOW - timedelta(hours=3), "resolved_at": NOW - timedelta(hours=2)}], NOW, 90,
	                       NOW - timedelta(days=100))
	inc_open = mu.public_incident({"status": "Open", "started_at": NOW - timedelta(minutes=5)}, "API <b>",
	                              [{"status": "Investigating", "message": "We <script>are</script> on it", "posted_at": NOW}])
	inc_old = mu.public_incident({"status": "Resolved", "started_at": NOW - timedelta(hours=3), "resolved_at": NOW - timedelta(hours=2)}, "Shop", [])
	monitors = [mu.public_monitor({"status": "Up"}, "Shop", days), mu.public_monitor({"status": "Down"}, "API <b>", days)]
	return {
		"title": "Acme <status>", "description": "Live status", "overall": mu.overall_status([m["status"] for m in monitors]),
		"brand": {"title": "Acme Cloud", "logo": "/files/acme.png", "favicon": None, "css": ":root{--nx-primary:#123456}",
		          "support_email": "help@acme.test", "footer_text": "Acme Ltd"},
		"monitors": monitors, "incidents": [inc_open, inc_old], "active_incidents": [inc_open], "past_incidents": [inc_old],
		"updated_at": "2026-11-04T12:00:00Z", "days": 90,
	}


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_status_page_renders(lang):
	data = _status_data()
	html = _env().get_template("www/status.html").render(
		lang=lang, dir="rtl" if lang == "ar" else "ltr", title=data["title"], status=data, status_brand=data["brand"],
	)
	assert html.lstrip().startswith("<!doctype html>")
	assert f'dir="{"rtl" if lang == "ar" else "ltr"}"' in html
	assert "Partial outage" in html and "Acme &lt;status&gt;" in html
	assert "<script>" not in html and "&lt;script&gt;" in html  # customer-written text is escaped
	assert "API &lt;b&gt;" in html
	assert html.count('class="up"') >= 89  # 90-day bars
	assert "/files/acme.png" in html and "--nx-primary:#123456" in html and "help@acme.test" in html
	assert "Ongoing incidents" in html and "Past incidents" in html
	assert "cdn" not in html.lower() and "x-data" not in html  # static, no JS needed


def test_status_page_controller():
	src = read(STATUS_PY)
	assert "no_cache = 1" in src and "status_page_data(name, lang)" in src
	assert "resolve_status_page(slug=slug)" in src and "resolve_status_page(host=" in src
	assert "frappe.DoesNotExistError" in src
	assert read(STATUS_HTML).startswith('{% extends "templates/status_base.html" %}')
	assert "{% block content %}" in read(STATUS_BASE)


# --------------------------------------------------------------------------- translations

STRING_RE = re.compile(r"""(?<![A-Za-z0-9.])_{1,2}\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')\s*[,)]""")


def test_translations():
	ar = set()
	for path in [os.path.join(APP, "translations", "ar.csv")] + sorted(glob.glob(os.path.join(APP, "translations", "ar.*.part"))):
		with open(path, encoding="utf-8", newline="") as fh:
			ar |= {r[0] for r in csv.reader(fh) if r}
	strings = set()
	files = [PAGE, PAGE_PY, API, PROBE_API, STATUS_HTML, STATUS_BASE, STATUS_PY,
	         os.path.join(DOCTYPES, "uptime_monitor", "uptime_monitor.py"),
	         os.path.join(DOCTYPES, "uptime_probe", "uptime_probe.py"),
	         os.path.join(DOCTYPES, "status_page", "status_page.py")]
	for path in files:
		strings |= {m.group(1) or m.group(2) for m in STRING_RE.finditer(read(path))}
	js = read(JS)
	strings |= set(re.findall(r'\bt\(\s*"((?:[^"\\]|\\.)*)"\s*[,)]', js))
	strings |= set(re.findall(r'label: "([^"]+)"', js))
	strings |= set(re.findall(r'MonitorError\(\s*"([^"{]+)"\s*\)', read(os.path.join(APP, "monitor_utils.py"))))
	strings |= set(mu.UPDATE_STATUSES) | set(mu.MONITOR_STATUSES) | set(mu.INCIDENT_STATUSES)
	strings |= {nu.EVENT_LABELS[nu.MONITOR_DOWN], nu.EVENT_LABELS[nu.MONITOR_UP]}
	missing = sorted(strings - ar)
	assert not missing, missing
