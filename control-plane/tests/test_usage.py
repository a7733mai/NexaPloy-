"""Plan limits / usage, S3 presigned downloads and the site config allow-list (pure logic)."""

import ast
import json
import os
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from nexaploy import ops_utils as ou

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nexaploy")
DOCTYPES = os.path.join(APP, "nexaploy", "doctype")

PLAN = {"cpu_seconds_per_day": 1000, "max_database_mb": 500, "max_storage_mb": 2000, "overage_action": "Notify"}


def test_ops_utils_has_no_frappe_import():
	tree = ast.parse(open(os.path.join(APP, "ops_utils.py")).read())
	for node in ast.walk(tree):
		if isinstance(node, (ast.Import, ast.ImportFrom)):
			names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
			assert not any(n and n.split(".")[0] == "frappe" for n in names)


# --------------------------------------------------------------------------- usage_status
def test_usage_status_levels():
	ok = ou.usage_status(PLAN, {"cpu_seconds": 10, "database_mb": 100, "storage_mb": 10})
	assert ok["status"] == "ok" and ok["over"] == [] and ok["warn"] == []
	assert ok["metrics"]["cpu"] == {"used": 10.0, "limit": 1000.0, "percent": 1.0, "status": "ok"}

	warn = ou.usage_status(PLAN, {"cpu_seconds": 800, "database_mb": 100})
	assert warn["status"] == "warn" and warn["warn"] == ["cpu"]  # exactly 80 % warns

	over = ou.usage_status(PLAN, {"cpu_seconds": 1000.5, "database_mb": 450, "storage_mb": 2001})
	assert over["status"] == "over" and over["over"] == ["cpu", "storage"] and over["warn"] == ["database"]

	at_limit = ou.usage_status(PLAN, {"cpu_seconds": 1000})
	assert at_limit["metrics"]["cpu"]["status"] == "warn"  # over means strictly above


def test_usage_status_unlimited_and_garbage():
	st = ou.usage_status({"cpu_seconds_per_day": 0, "max_database_mb": None}, {"cpu_seconds": 10 ** 9, "database_mb": "x"})
	assert st["status"] == "ok"
	assert st["metrics"]["cpu"] == {"used": 1e9, "limit": 0, "percent": None, "status": "ok"}
	assert ou.usage_status(None, None)["status"] == "ok"
	assert ou.usage_status(PLAN, {"cpu_seconds": -5})["metrics"]["cpu"]["used"] == 0


def _site(**kw):
	base = {"status": "Active", "throttled": 0, "usage_suspended": 0, "suspended_for_billing": 0}
	base.update(kw)
	return base


def test_enforcement_actions():
	over_cpu = ou.usage_status(PLAN, {"cpu_seconds": 2000})
	over_disk = ou.usage_status(PLAN, {"storage_mb": 9999})
	ok = ou.usage_status(PLAN, {})
	warn = ou.usage_status(PLAN, {"cpu_seconds": 900})
	notify = dict(PLAN, overage_action="Notify")
	throttle = dict(PLAN, overage_action="Throttle")
	suspend = dict(PLAN, overage_action="Suspend")

	assert ou.enforcement_actions(notify, over_cpu, _site()) == ["notify_over"]
	assert ou.enforcement_actions(notify, warn, _site()) == ["notify_warn"]
	assert ou.enforcement_actions(notify, ok, _site()) == []
	# throttle only for CPU, once
	assert ou.enforcement_actions(throttle, over_cpu, _site()) == ["notify_over", "throttle"]
	assert ou.enforcement_actions(throttle, over_cpu, _site(throttled=1)) == ["notify_over"]
	assert ou.enforcement_actions(throttle, over_disk, _site()) == ["notify_over"]
	# daily reset: next UTC day CPU is 0 -> lift
	assert ou.enforcement_actions(throttle, ok, _site(throttled=1)) == ["unthrottle"]
	# plan changed away from Throttle (e.g. upgrade) -> lift too
	assert "unthrottle" in ou.enforcement_actions(notify, over_cpu, _site(throttled=1))
	# suspend on any metric, never twice, never on billing-suspended sites
	assert ou.enforcement_actions(suspend, over_disk, _site()) == ["notify_over", "suspend"]
	assert ou.enforcement_actions(suspend, over_disk, _site(status="Suspended", usage_suspended=1)) == ["notify_over"]
	assert "suspend" not in ou.enforcement_actions(suspend, over_disk, _site(suspended_for_billing=1))
	assert ou.enforcement_actions(suspend, ok, _site(status="Suspended", usage_suspended=1)) == ["unsuspend"]
	# unknown action falls back to Notify
	assert ou.enforcement_actions(dict(PLAN, overage_action="Explode"), over_cpu, _site()) == ["notify_over"]


def test_throttle_rate_limit():
	assert ou.throttle_rate_limit({"cpu_seconds_per_day": 3600}) == {"limit": 360, "window": 86400}
	assert ou.throttle_rate_limit({"cpu_seconds_per_day": 100}) == {"limit": 60, "window": 86400}
	assert ou.throttle_rate_limit(None)["limit"] == 60


def test_usage_rows_from_heartbeat_block():
	block = {
		"collected_at": "2026-11-04T00:10:00Z",
		"date": "2026-11-04",
		"sites": {
			"a.example.com": {"database_mb": 5.0, "storage_mb": 3.25,
			                  "days": {"2026-11-03": {"cpu_seconds": 2.0, "requests": 2},
			                           "2026-11-04": {"cpu_seconds": 3.0, "requests": 1}}},
			"b.example.com": {"database_mb": 1.0, "days": {}},
			"c.example.com": {"days": {"bad-date": {"cpu_seconds": 1}}},
			7: {"days": {}},
		},
	}
	rows = ou.usage_rows(block)
	by = {(r["site"], r["date"]): r for r in rows}
	assert by[("a.example.com", "2026-11-04")] == {"site": "a.example.com", "date": "2026-11-04", "cpu_seconds": 3.0,
	                                              "requests": 1, "database_mb": 5.0, "storage_mb": 3.25}
	# sizes are point-in-time: only on the collection day
	assert "database_mb" not in by[("a.example.com", "2026-11-03")]
	assert by[("b.example.com", "2026-11-04")] == {"site": "b.example.com", "date": "2026-11-04", "database_mb": 1.0}
	assert not any(r["site"] == "c.example.com" for r in rows)
	assert ou.usage_rows(json.dumps(block)) == rows
	assert ou.usage_rows(None) == [] and ou.usage_rows("nope") == [] and ou.usage_rows({"sites": []}) == []


def test_upgrade_plans():
	plans = [
		{"name": "Pro", "plan_order": 2, "price_monthly": 25},
		{"name": "Basic", "plan_order": 1, "price_monthly": 5},
		{"name": "Business", "plan_order": 3, "price_monthly": 80},
	]
	assert [p["name"] for p in ou.upgrade_plans(plans, "Basic")] == ["Pro", "Business"]
	assert [p["name"] for p in ou.upgrade_plans(plans, "Business")] == []
	assert [p["name"] for p in ou.upgrade_plans(plans, None)] == ["Basic", "Pro", "Business"]


# --------------------------------------------------------------------------- presign
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def test_presign_aws_documented_vector():
	"""AWS SigV4 docs, "Example: GET object" presigned URL (query string auth)."""
	url = ou.presign_s3_get(None, "us-east-1", "examplebucket", "test.txt", AWS_KEY, AWS_SECRET,
	                        expires=86400, now=datetime(2013, 5, 24, 0, 0, 0))
	assert url == (
		"https://examplebucket.s3.amazonaws.com/test.txt"
		"?X-Amz-Algorithm=AWS4-HMAC-SHA256"
		"&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request"
		"&X-Amz-Date=20130524T000000Z&X-Amz-Expires=86400&X-Amz-SignedHeaders=host"
		"&X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"
	)
	# aware datetimes are converted to UTC
	aware = ou.presign_s3_get(None, "us-east-1", "examplebucket", "test.txt", AWS_KEY, AWS_SECRET,
	                          expires=86400, now=datetime(2013, 5, 24, tzinfo=timezone.utc))
	assert aware == url


def test_presign_path_style_endpoints():
	key = "backups/srv-1/erp-v15/a.example.com/20261103T120000Z/20261103_120000-a_example_com-database.sql.gz"
	now = datetime(2026, 11, 3, 12, 0, 0)
	url = ou.presign_s3_get("https://minio.example.com:9000", "", "nx-backups", key, "AK", "SK", now=now)
	parts = urlsplit(url)
	assert parts.scheme == "https" and parts.netloc == "minio.example.com:9000"
	assert parts.path == "/nx-backups/" + key
	q = parse_qs(parts.query)
	assert q["X-Amz-Expires"] == ["900"] and q["X-Amz-Credential"] == ["AK/20261103/us-east-1/s3/aws4_request"]
	assert len(q["X-Amz-Signature"][0]) == 64
	# deterministic, and the signature depends on the secret / region
	assert url == ou.presign_s3_get("https://minio.example.com:9000", None, "nx-backups", key, "AK", "SK", now=now)
	assert url != ou.presign_s3_get("https://minio.example.com:9000", None, "nx-backups", key, "AK", "SK2", now=now)
	r2 = ou.presign_s3_get("acct.r2.cloudflarestorage.com", "auto", "b", "k/x y.txt", "AK", "SK", now=now)
	assert r2.startswith("https://acct.r2.cloudflarestorage.com/b/k/x%20y.txt?")
	assert "%2Fauto%2Fs3%2F" in r2
	http = ou.presign_s3_get("http://127.0.0.1:9000/", "us-east-1", "b", "k", "AK", "SK", now=now)
	assert http.startswith("http://127.0.0.1:9000/b/k?")
	# AWS regional endpoint -> virtual-hosted style
	aws = ou.presign_s3_get("https://s3.eu-west-1.amazonaws.com", "eu-west-1", "b", "k", "AK", "SK", now=now)
	assert aws.startswith("https://b.s3.eu-west-1.amazonaws.com/k?")
	assert ou.presign_s3_get(None, "eu-west-1", "b", "k", "AK", "SK", now=now).startswith("https://b.s3.eu-west-1.amazonaws.com/k?")
	# expiry clamp
	assert "X-Amz-Expires=604800" in ou.presign_s3_get(None, None, "b", "k", "AK", "SK", expires=10 ** 9, now=now)
	with pytest.raises(ValueError):
		ou.presign_s3_get(None, None, "b", "", "AK", "SK")


def test_backup_files_from_result():
	result = {
		"remote_path": "nexaploy-backups:pfx/srv-1/erp-v15/a.example.com/20261103T120000Z",
		"backup_files": [
			{"file_type": "database", "name": "x-database.sql.gz", "size": 10, "sha256": "a" * 64,
			 "remote_key": "pfx/srv-1/erp-v15/a.example.com/20261103T120000Z/x-database.sql.gz"},
			{"file_type": "site_config", "name": "x-site_config_backup.json", "size": 2, "sha256": "b" * 64,
			 "remote_key": None},
		],
	}
	rows = ou.backup_files_from_result(result)
	assert [r["file_type"] for r in rows] == ["database", "site_config"]
	assert rows[0]["file_name"] == "x-database.sql.gz" and rows[0]["size"] == 10
	# missing remote_key is derived from remote_path
	assert rows[1]["remote_key"] == "pfx/srv-1/erp-v15/a.example.com/20261103T120000Z/x-site_config_backup.json"
	# older agents: manifest only
	legacy = {"manifest": {"remote_path": "r:p/b/s/20261103T120000Z", "files": [
		{"name": "x-files.tar", "size": 5, "sha256": "c", "type": "public"},
		{"name": "x-private-files.tar", "size": 6, "sha256": "d"},
		{"name": "../evil-database.sql.gz", "size": 1}]}}
	rows = ou.backup_files_from_result(legacy)
	assert {r["file_type"]: r["remote_key"] for r in rows} == {
		"public": "p/b/s/20261103T120000Z/x-files.tar", "private": "p/b/s/20261103T120000Z/x-private-files.tar"}
	assert ou.backup_files_from_result({"manifest": {"files": [{"name": "x-database.sql.gz"}]}})[0]["remote_key"] is None
	assert ou.backup_files_from_result(None) == []


# --------------------------------------------------------------------------- site config
def test_config_allow_deny_list():
	clean, remove, warnings = ou.validate_config_changes(
		{"maintenance_mode": 1, "developer_mode": 1, "mail_server": "smtp.example.com",
		 "max_file_size": 10485760, "allowed_origins": ["https://a.example.com"], "custom_app": {"x": True}},
		["old_key"],
	)
	assert clean["maintenance_mode"] == 1 and clean["custom_app"] == {"x": True}
	assert remove == ["old_key"]
	assert any("developer_mode" in w for w in warnings) and any("maintenance_mode" in w for w in warnings)
	assert ou.validate_config_changes({"maintenance_mode": 0})[2] == []  # turning it off: no warning
	for key in ("db_name", "db_password", "db_type", "encryption_key", "host_name", "rate_limit", "monitor",
	            "domains", "db_anything", "nexaploy_internal", "bad key", "1abc", ""):
		with pytest.raises(ValueError):
			ou.validate_config_changes({key: 1})
		if key:
			with pytest.raises(ValueError):
				ou.validate_config_changes({}, [key])


@pytest.mark.parametrize("changes,remove", [
	({"a": None}, None),                    # null -> use remove
	({"a": float("nan")}, None),
	({"a": {1: 2}}, None),
	({"a": object()}, None),
	({"a": ou.CONFIG_MASK}, None),          # the masked secret echoed back
	({"a": "x" * 20000}, None),
	({"a": 1}, ["a"]),                      # set and removed
	({f"k{i}": i for i in range(51)}, None),
	([1, 2], None),
	({}, "notalist"),
])
def test_config_value_validation(changes, remove):
	with pytest.raises((ValueError, TypeError)):
		ou.validate_config_changes(changes, remove)


def test_config_json_strings_accepted():
	clean, remove, _ = ou.validate_config_changes('{"a": 1}', '["b"]')
	assert clean == {"a": 1} and remove == ["b"]


def test_redact_site_config():
	view = ou.redact_site_config({
		"db_name": "_abc", "db_password": "pw", "encryption_key": "k", "mail_password": "m",
		"google_api_key": "", "maintenance_mode": 0, "monitor": 1,
	})
	cfg = view["config"]
	assert cfg["db_password"] == cfg["encryption_key"] == cfg["mail_password"] == ou.CONFIG_MASK
	assert cfg["google_api_key"] == "" and cfg["db_name"] == "_abc" and cfg["maintenance_mode"] == 0
	assert view["redacted"] == ["db_password", "encryption_key", "google_api_key", "mail_password"]
	assert view["readonly"] == ["db_name", "db_password", "encryption_key", "monitor"]
	assert ou.redact_site_config(None) == {"config": {}, "redacted": [], "readonly": []}


# --------------------------------------------------------------------------- doctypes / wiring
def _doctype(name):
	folder = name.lower().replace(" ", "_")
	with open(os.path.join(DOCTYPES, folder, f"{folder}.json")) as fh:
		return json.load(fh)


def _fields(name):
	return {f["fieldname"]: f for f in _doctype(name)["fields"]}


def test_doctype_fields():
	plan = _fields("Nexaploy Plan")
	for f in ("cpu_seconds_per_day", "max_storage_mb", "max_database_mb", "overage_action", "plan_order"):
		assert f in plan
	assert plan["overage_action"]["options"].split("\n") == list(ou.OVERAGE_ACTIONS)
	site = _fields("Site")
	for f in ("current_cpu_seconds_today", "database_mb", "storage_mb", "over_limit", "throttled", "usage_suspended"):
		assert f in site
	assert _fields("Site Backup")["files"]["options"] == "Site Backup File"
	child = _doctype("Site Backup File")
	assert child["istable"] == 1
	assert _fields("Site Backup File")["file_type"]["options"].split("\n") == list(ou.BACKUP_FILE_TYPES)
	usage = _fields("Site Usage")
	for f in ("site", "date", "cpu_seconds", "database_mb", "storage_mb", "requests"):
		assert f in usage
	assert usage["team"]["options"] == "Team"
	for name in ("Nexaploy Plan", "Site", "Site Backup", "Site Usage", "Site Backup File"):
		d = _doctype(name)
		assert d["field_order"] == [f["fieldname"] for f in d["fields"]]


def test_new_job_kinds_and_callbacks():
	kinds = _fields("Agent Job")["kind"]["options"].split("\n")
	src = open(os.path.join(APP, "agent_callbacks.py")).read()
	for kind in ("site.usage", "site.get_config", "site.update_config"):
		assert kind in kinds
		assert f'"{kind}"' in src


def test_site_tools_api_whitelisted():
	tree = ast.parse(open(os.path.join(APP, "api", "site_tools.py")).read())
	funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
	for name in ("get_usage", "change_plan", "list_backup_files", "download_backup", "get_site_config",
	             "get_site_config_result", "update_site_config"):
		assert name in funcs
		assert any("whitelist" in ast.unparse(d) for d in funcs[name].decorator_list), name
	js = open(os.path.join(APP, "public", "js", "site_tools.js")).read()
	used = set(__import__("re").findall(r'call\("(\w+)"', js))
	assert used and used <= set(funcs)


def test_tasks_exist():
	src = open(os.path.join(APP, "tasks.py")).read()
	assert "def enforce_plan_limits" in src and "def prune_site_usage" in src
	ops = open(os.path.join(APP, "ops.py")).read()
	assert "def ingest_usage" in ops and "def enforce_site_limits" in ops
