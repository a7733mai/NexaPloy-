"""Pure-logic tests for nexaploy.ops_utils + static checks of the operations wiring."""

import ast
import json
import os
from datetime import datetime, timedelta

import pytest

from nexaploy import ops_utils as ou

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "nexaploy")
DT = os.path.join(APP, "nexaploy", "doctype")
NOW = datetime(2026, 11, 3, 12, 0, 0)


def doctype(folder):
	with open(os.path.join(DT, folder, folder + ".json")) as fh:
		return json.load(fh)


def field(folder, name):
	return next(f for f in doctype(folder)["fields"] if f["fieldname"] == name)


def test_ops_utils_does_not_import_frappe():
	tree = ast.parse(open(os.path.join(APP, "ops_utils.py")).read())
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			assert all(not a.name.startswith("frappe") for a in node.names)
		if isinstance(node, ast.ImportFrom):
			assert not (node.module or "").startswith("frappe")


# ------------------------------------------------------------------ registry / tags


def test_build_image_tag():
	assert ou.build_image_tag("erp-v15", "20261103T213000") == "nexaploy/bench:erp-v15-20261103T213000"
	assert ou.build_image_tag("erp-v15", "1", "https://GHCR.io/Acme/") == "ghcr.io/acme/nexaploy/bench:erp-v15-1"
	assert ou.build_image_tag("erp-v15", "1", "registry.example.com:5000") == (
		"registry.example.com:5000/nexaploy/bench:erp-v15-1"
	)
	assert ou.build_image_tag("erp-v15", "1", "") == "nexaploy/bench:erp-v15-1"
	with pytest.raises(ValueError):
		ou.build_image_tag("erp-v15", "1", "ghcr.io/acme bad")


def test_registry_covers():
	assert ou.registry_covers("ghcr.io/acme", "ghcr.io/acme/nexaploy/bench:x-1")
	assert not ou.registry_covers("ghcr.io/acme", "nexaploy/bench:x-1")
	assert not ou.registry_covers("ghcr.io/acme", "ghcr.io/acmecorp/nexaploy/bench:x-1")
	assert not ou.registry_covers(None, "ghcr.io/acme/nexaploy/bench:x-1")
	assert not ou.registry_covers("bad url", "x")


# ------------------------------------------------------------------ backups due


@pytest.mark.parametrize(
	"freq,last,due",
	[
		("Daily", None, True),
		("Daily", NOW - timedelta(hours=23, minutes=55), True),  # slack: hourly scheduler drift
		("Daily", NOW - timedelta(hours=23), False),
		("Every 12 hours", NOW - timedelta(hours=12), True),
		("Every 12 hours", NOW - timedelta(hours=6), False),
		("Weekly", NOW - timedelta(days=6), False),
		("Weekly", NOW - timedelta(days=7), True),
		("Off", None, False),
		(None, None, True),
	],
)
def test_is_backup_due(freq, last, due):
	assert ou.is_backup_due(freq, last, NOW) is due


def test_unknown_frequency():
	with pytest.raises(ValueError):
		ou.is_backup_due("Hourly", None, NOW)


# ------------------------------------------------------------------ retention


def ts(days_ago, now=NOW):
	return (now - timedelta(days=days_ago, minutes=1)).strftime(ou.TS_FORMAT)


def test_select_expired_backups():
	names = [ts(d) + "/" for d in (0, 1, 5, 15, 20, 40)] + ["junk/", "20261301T000000Z/"]
	expired = ou.select_expired_backups(names, keep_days=14, keep_min=3, now=NOW)
	assert expired == sorted([ts(15), ts(20), ts(40)])
	# keep_min wins over age
	assert ou.select_expired_backups([ts(30), ts(40)], keep_days=7, keep_min=3, now=NOW) == []
	assert ou.select_expired_backups([ts(30), ts(40)], keep_days=7, keep_min=1, now=NOW) == [ts(40)]
	assert ou.parse_backup_ts("20261103T213000Z/") == datetime(2026, 11, 3, 21, 30)
	assert ou.parse_backup_ts("nope") is None


def test_parse_backup_remote_path():
	p = "nexaploy-backups:srv-ab12/erp-v15/a.example.com/20261103T213000Z"
	assert ou.parse_backup_remote_path(p) == {
		"remote": "nexaploy-backups", "prefix": "srv-ab12", "bench": "erp-v15",
		"site": "a.example.com", "ts": "20261103T213000Z",
	}
	assert ou.parse_backup_remote_path("r:base/srv-1/b/s.io/20261103T213000Z/")["prefix"] == "base/srv-1"
	assert ou.parse_backup_remote_path("r:b/s.io/20261103T213000Z")["prefix"] == ""
	for bad in (None, "", "no-colon/a/b/c", "r:b/s.io/latest", "r:s.io/20261103T213000Z"):
		assert ou.parse_backup_remote_path(bad) is None


def test_prune_targets_and_pruned_matching():
	paths = [
		"nexaploy-backups:srv-1/erp-v14/a.example.com/20261001T000000Z",
		"nexaploy-backups:srv-1/erp-v14/a.example.com/20261002T000000Z",
		"nexaploy-backups:srv-2/erp-v15/a.example.com/20261101T000000Z",
		"nexaploy-backups:srv-2/erp-v15/b.example.com/20261101T000000Z",
		None,
	]
	assert ou.prune_targets(paths, "a.example.com") == [("srv-1", "erp-v14"), ("srv-2", "erp-v15")]
	result = {"pruned": [{"ts": "20261001T000000Z", "remote_path": paths[0]}, {"bad": 1}]}
	pruned = ou.pruned_remote_paths(result)
	assert pruned == {("srv-1", "erp-v14", "20261001T000000Z")}
	assert ou.backup_was_pruned(paths[0] + "/", pruned)
	assert not ou.backup_was_pruned(paths[1], pruned)
	assert not ou.backup_was_pruned(None, pruned)
	assert ou.pruned_remote_paths("x") == set()


def test_backup_job_params_and_fingerprint():
	settings = {
		"backup_remote_type": "s3", "backup_provider": "", "backup_endpoint": "https://s3.example.com",
		"backup_region": "", "backup_bucket": "nx", "backup_access_key": "AK", "backup_prefix": "/prod/",
	}
	assert ou.backup_configured(settings)
	assert not ou.backup_configured({**settings, "backup_bucket": ""})
	p = ou.backup_job_params(settings, "srv-1", "__secret__:x")
	assert p == {
		"remote": "nexaploy-backups", "type": "s3", "provider": "Other", "endpoint": "https://s3.example.com",
		"region": None, "bucket": "nx", "access_key_id": "AK", "secret_access_key": "__secret__:x",
		"prefix": "prod/srv-1",
	}
	assert ou.backup_job_params({**settings, "backup_prefix": ""}, "srv-1", "s")["prefix"] == "srv-1"
	a = ou.config_fingerprint({"backup_bucket": "nx", "backup_secret_key": "s1"})
	assert a == ou.config_fingerprint({"backup_secret_key": "s1", "backup_bucket": "nx"})
	assert a != ou.config_fingerprint({"backup_bucket": "nx", "backup_secret_key": "s2"})
	assert "s1" not in a


# ------------------------------------------------------------------ monitoring

GB = 1024**3
FACTS = {
	"cpu_count": 4,
	"load_avg": [1.234, 0.5, 0.2],
	"mem_total": 8 * 1024 * 1024 * 1024,
	"mem_available": 6 * 1024 * 1024 * 1024,
	"mem_used": 2 * 1024 * 1024 * 1024,
	"disk_path": "/opt/nexaploy",
	"mounts": [
		{"path": "/", "total": 20 * GB, "used": 18 * GB},
		{"path": "/opt/nexaploy", "total": 100 * GB, "used": 40 * GB},
	],
	"unhealthy_containers": 2,
}


def test_metric_from_facts():
	m = ou.metric_from_facts(FACTS)
	assert m == {
		"cpu_count": 4, "load_1": 1.23, "memory_used_mb": 2048, "memory_total_mb": 8192,
		"disk_used_gb": 40.0, "disk_total_gb": 100.0, "disk_used_percent": 90.0, "unhealthy_containers": 2,
	}
	# legacy facts (no mounts / mem_used)
	legacy = ou.metric_from_facts({"mem_total": 4 * 1024**3, "mem_available": 1024**3,
	                               "disk_total": 50 * GB, "disk_free": 10 * GB, "load_avg": [0.5]})
	assert legacy["memory_used_mb"] == 3072 and legacy["disk_used_percent"] == 80.0
	assert ou.metric_from_facts(None)["disk_used_percent"] == 0.0


def test_should_record_metric():
	assert ou.should_record_metric(None, NOW)
	assert not ou.should_record_metric(NOW - timedelta(minutes=4), NOW)
	assert ou.should_record_metric(NOW - timedelta(minutes=5), NOW)


def test_server_alerts():
	base = {"name": "srv-1", "hostname": "h", "status": "Active", "disk_used_percent": 50, "unhealthy_containers": 0}
	assert ou.server_alerts(base) == []
	kinds = [k for k, _ in ou.server_alerts({**base, "disk_used_percent": 85.5, "unhealthy_containers": 1})]
	assert kinds == ["disk", "containers"]
	assert [k for k, _ in ou.server_alerts({**base, "status": "Offline", "unhealthy_containers": 3})] == ["offline"]
	assert ou.server_alerts({**base, "status": "Archived", "disk_used_percent": 99}) == []
	assert ou.server_alerts({**base, "disk_used_percent": 85}) == []  # strictly greater


def test_alert_rate_limit():
	state = ou.load_alert_state(None)
	assert ou.should_alert(state, "disk", NOW)
	state = ou.record_alert(state, "disk", NOW)
	assert not ou.should_alert(state, "disk", NOW + timedelta(hours=5, minutes=59))
	assert ou.should_alert(state, "disk", NOW + timedelta(hours=6))
	assert ou.should_alert(state, "offline", NOW + timedelta(minutes=1))  # per kind
	# round-trips through the JSON field
	assert ou.load_alert_state(json.dumps(state)) == state
	assert ou.load_alert_state("garbage") == {}
	assert ou.should_alert({"disk": "garbage"}, "disk", NOW)


def test_uptime_transition():
	assert ou.uptime_transition(None, False, False) is None  # first check
	assert ou.uptime_transition(1, True, False) == "down"
	assert ou.uptime_transition(0, True, True) == "up"
	assert ou.uptime_transition(1, True, True) is None
	assert ou.uptime_transition(0, True, False) is None
	assert ou.ping_url("a.example.com") == "https://a.example.com/api/method/ping"


# ------------------------------------------------------------------ auto updates


def test_update_window():
	assert ou.parse_update_window("02:00-05:00") == (120, 300)
	assert ou.parse_update_window("") is None
	assert ou.in_update_window(None, NOW)
	assert ou.in_update_window("02:00-05:00", NOW.replace(hour=2, minute=0))
	assert not ou.in_update_window("02:00-05:00", NOW.replace(hour=5, minute=0))
	assert ou.in_update_window("22:00-04:00", NOW.replace(hour=23))
	assert ou.in_update_window("22:00-04:00", NOW.replace(hour=1))
	assert not ou.in_update_window("22:00-04:00", NOW.replace(hour=12))
	for bad in ("2am", "25:00-01:00", "02:00-02:00", "02:00"):
		with pytest.raises(ValueError):
			ou.parse_update_window(bad)


def test_server_local_time_and_benches_to_update():
	assert ou.server_local_time(NOW, {"utc_offset_minutes": 180}) == NOW + timedelta(hours=3)
	assert ou.server_local_time(NOW, None) == NOW
	benches = [
		{"name": "a", "status": "Active", "image_tag": "old"},
		{"name": "b", "status": "Active", "image_tag": "new"},
		{"name": "c", "status": "Broken", "image_tag": "old"},
		{"name": "d", "status": "Active", "image_tag": "old", "update_in_progress": True},
	]
	assert ou.benches_to_update(benches, "new") == ["a"]
	assert ou.benches_to_update(benches, None) == []


# ------------------------------------------------------------------ wiring (static)


def test_doctype_fields():
	settings = doctype("nexaploy_settings")
	assert settings["issingle"] == 1
	names = {f["fieldname"] for f in settings["fields"]}
	assert {"registry_url", "registry_username", "registry_password", "root_domain", "default_build_server",
	        "backup_remote_type", "backup_endpoint", "backup_region", "backup_bucket", "backup_access_key",
	        "backup_secret_key"} <= names
	assert field("nexaploy_settings", "registry_password")["fieldtype"] == "Password"
	assert field("nexaploy_settings", "backup_secret_key")["fieldtype"] == "Password"
	assert field("nexaploy_settings", "root_domain")["default"] == "nexaploy.cloud"

	metric = {f["fieldname"] for f in doctype("server_metric")["fields"]}
	assert {"server", "timestamp", "load_1", "memory_used_mb", "memory_total_mb", "disk_used_gb",
	        "disk_total_gb", "unhealthy_containers", "raw"} <= metric

	server = {f["fieldname"] for f in doctype("server")["fields"]}
	assert {"cpu_count", "memory_total_mb", "disk_total_gb", "disk_used_percent", "unhealthy_containers",
	        "last_alert_at"} <= server

	assert field("site", "backup_frequency")["options"].split("\n") == ["Daily", "Every 12 hours", "Weekly", "Off"]
	assert field("site", "backup_frequency")["default"] == "Daily"
	assert field("site", "backup_retention_days")["default"] == "14"
	assert field("site", "backup_with_files")["default"] == "1"
	for f in ("last_backup_at", "last_uptime_check", "is_up", "suspended_for_billing"):
		field("site", f)
	assert "Suspended" in field("site", "status")["options"].split("\n")
	assert "Deleted" in field("site_backup", "status")["options"].split("\n")
	assert field("release_group", "auto_update")["fieldtype"] == "Check"
	assert field("release_group", "update_window")["fieldtype"] == "Data"
	kinds = field("agent_job", "kind")["options"].split("\n")
	for k in ("server.configure_backups", "server.health", "backup.prune_remote"):
		assert k in kinds


def _functions(path):
	tree = ast.parse(open(path).read())
	return {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


def test_scheduler_functions_exist():
	tasks = _functions(os.path.join(APP, "tasks.py"))
	assert {"schedule_backups", "prune_remote_backups", "check_alerts", "prune_server_metrics",
	        "check_sites_uptime", "apply_auto_updates"} <= tasks


def test_callbacks_cover_ops_kinds():
	src = open(os.path.join(APP, "agent_callbacks.py")).read()
	for kind in ("backup.prune_remote", "server.configure_backups", "server.health"):
		assert f'"{kind}"' in src


def test_release_group_deploy_latest_whitelisted():
	src = open(os.path.join(DT, "release_group", "release_group.py")).read()
	assert "@frappe.whitelist()\n\tdef deploy_latest(self):" in src


def test_registry_password_never_in_params():
	"""Jobs carry a secret placeholder for registry/backup secrets, resolved only at poll time."""
	src = open(os.path.join(APP, "ops.py")).read()
	assert 'secret_ref(SETTINGS, SETTINGS, "registry_password")' in src
	assert 'secret_ref(SETTINGS, SETTINGS, "backup_secret_key")' in src
	assert "get_password(\"registry_password\"" not in src
