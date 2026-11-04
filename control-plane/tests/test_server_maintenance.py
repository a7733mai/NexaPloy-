"""Server maintenance & health: pure logic (server_health_utils) and static wiring checks."""

import ast
import csv
import glob
import json
import os
import re
from datetime import datetime, timedelta, timezone

import pytest

from nexaploy import server_health_utils as hu

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "nexaploy")
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def read(*parts):
	with open(os.path.join(APP, *parts), encoding="utf-8") as fh:
		return fh.read()


def facts(**kw):
	base = {
		"cpu_count": 4, "load_avg": [0.5, 0.5, 0.5], "mem_total": 1000, "mem_available": 600,
		"docker_version": "27.0", "agent_version": "0.3.0",
		"containers": [
			{"name": "nexaploy-db", "state": "running", "health": "healthy"},
			{"name": "nexaploy-traefik", "state": "running", "health": None},
		],
		"bench_health": {"erp": {"unhealthy": []}},
		"health": {"agent_time": NOW.isoformat(), "filesystems": [{"path": "/", "used_percent": 40, "inodes_used_percent": 10, "free": 10}],
		           "mem_used_percent": 40, "failed_units": [], "ntp_synchronized": True},
		"os_updates": {"supported": True, "pending": 3, "security": 0, "reboot_required": False},
	}
	for k, v in kw.items():
		if k == "health":
			base["health"].update(v)
		else:
			base[k] = v
	return base


def codes(issues):
	return [i["code"] for i in issues]


def test_pure_module():
	assert not re.search(r"^\s*(import|from)\s+frappe", read("server_health_utils.py"), re.M)


def test_healthy_server_is_ok():
	issues = hu.evaluate_health(facts(), context={"now": NOW})
	assert all(i["severity"] == hu.INFO for i in issues)
	assert hu.health_status(issues, "Active", True) == hu.OK
	assert hu.health_status([], "Offline", True) == hu.OFFLINE
	assert hu.health_status([], "Active", False) == hu.UNKNOWN


def test_disk_thresholds_and_settings():
	f = facts(health={"filesystems": [{"path": "/", "used_percent": 96, "inodes_used_percent": 91}]})
	issues = hu.evaluate_health(f, context={"now": NOW})
	assert codes(issues)[:1] == ["disk_critical"] and "inodes_warning" in codes(issues)
	assert hu.health_status(issues, "Active", True) == hu.CRIT
	t = hu.thresholds_from_settings({"health_disk_warning_percent": 97, "health_disk_critical_percent": 99})
	assert "disk_critical" not in codes(hu.evaluate_health(f, t, {"now": NOW}))
	f = facts(health={"filesystems": [{"path": "/", "used_percent": 88}]})
	assert "disk_warning" in codes(hu.evaluate_health(f, context={"now": NOW}))


def test_services_containers_clock_and_diagnose():
	f = facts(containers=[{"name": "nexaploy-db", "state": "exited"}, {"name": "x", "state": "restarting"}],
	          bench_health={"erp": {"unhealthy": ["erp-backend"]}},
	          health={"failed_units": ["nexaploy-agent-worker.service", "rclone-a.service"], "ntp_synchronized": False,
	                  "agent_time": (NOW + timedelta(minutes=10)).isoformat()},
	          load_avg=[20, 20, 20])
	c = codes(hu.evaluate_health(f, context={"now": NOW, "stale_backups": ["a.example.com"], "agent_outdated": True,
	                                         "latest_agent": "0.4.0"}))
	for code in ("nexaploy-db_down", "nexaploy-traefik_down", "units_failed", "mounts_failed", "containers_restarting",
	             "bench_unhealthy", "ntp", "clock_skew", "load_critical", "backups_stale", "agent_outdated"):
		assert code in c, code
	diag = {"oom_kills": 2, "mariadb": {"reachable": True, "threads_connected": 140, "max_connections": 151},
	        "tls": {"failing": ["a.example.com"], "expiring": []}}
	c = codes(hu.evaluate_health(facts(), context={"now": NOW, "diagnose": diag, "diagnosed_at": NOW.isoformat()}))
	assert {"oom_kills", "db_connections", "tls_failing"} <= set(c)
	old = (NOW - timedelta(days=3)).isoformat()
	assert "oom_kills" not in codes(hu.evaluate_health(facts(), context={"now": NOW, "diagnose": diag, "diagnosed_at": old}))


def test_health_notification_throttle():
	ev, st = hu.health_notification({}, hu.CRIT, NOW)
	assert ev == "server_health_critical"
	ev2, st2 = hu.health_notification(st, hu.CRIT, NOW + timedelta(hours=1))
	assert ev2 is None and st2 == st
	ev3, _ = hu.health_notification(st, hu.CRIT, NOW + timedelta(hours=7))
	assert ev3 == "server_health_critical"
	assert hu.health_notification(st, hu.WARN, NOW)[0] is None
	ev4, st4 = hu.health_notification(st, hu.OK, NOW)
	assert ev4 == "server_health_ok" and hu.health_notification(st4, hu.OK, NOW)[0] is None


def test_os_tracking_and_windows():
	o = hu.os_fields({"supported": True, "pending": 5, "security": 2, "reboot_required": True, "boot_time": 1762200000,
	                  "unattended_security": True, "checked_at": "2026-11-04T10:00:00Z"})
	assert o["os_pending_updates"] == 5 and o["os_reboot_required"] == 1 and o["os_boot_time"].year == 2025
	assert hu.os_fields({"supported": False}) == {"os_updates_supported": 0}
	assert hu.os_job_transition("server.os_update", "Success", {"reboot_scheduled": True}) == hu.REBOOTING
	assert hu.os_job_transition("server.os_update", "Success", {}) == hu.SUCCEEDED
	assert hu.os_job_transition("server.os_update", "Failure", {}) == hu.FAILED
	assert hu.os_job_transition("server.os_reboot", "Running", {}) is None
	started = NOW - timedelta(minutes=5)
	assert hu.heartbeat_os_transition(hu.REBOOTING, started, NOW) == hu.SUCCEEDED
	assert hu.heartbeat_os_transition(hu.REBOOTING, started, started - timedelta(days=1)) is None
	assert hu.validate_os_update("security", "never")["mode"] == "security"
	with pytest.raises(ValueError):
		hu.validate_os_update("dist", "never")
	wed = datetime(2026, 11, 4, 3, 20)  # a Wednesday
	assert hu.window_due(wed, "Wednesday", 3, None)
	assert hu.window_due(wed, "Every day", 3, wed - timedelta(days=1))
	assert not hu.window_due(wed, "Wednesday", 3, wed.replace(minute=5))  # once per window
	assert not hu.window_due(wed, "Sunday", 3, None) and not hu.window_due(wed, "Wednesday", 4, None)
	assert hu.manage_allowed(True, 0, "Hosted", False)
	assert hu.manage_allowed(False, 1, "BYOS", True)
	assert not hu.manage_allowed(False, 1, "Hosted", True) and not hu.manage_allowed(False, 0, "BYOS", True)


def test_cleanup_helpers():
	assert hu.clean_targets("journal\napt, journal") == ["journal", "apt"]
	assert hu.clean_targets(None) == list(hu.CLEANUP_TARGETS)
	with pytest.raises(ValueError):
		hu.clean_targets(["/etc"])
	p = hu.cleanup_params(["apt"], True, 0, 0)
	assert p == {"targets": ["apt"], "dry_run": True, "log_retention_days": 14}
	assert hu.cleanup_params(["apt"], False, 7, 3)["backup_local_keep_days"] == 3
	s = hu.cleanup_summary({"dry_run": False, "targets": {"apt": {"freed_bytes": 10, "items": 2, "errors": ["x"]},
	                                                     "journal": {"freed_bytes": 5}}})
	assert s["freed_bytes"] == 15 and s["items"] == 2 and s["errors"] == 1 and s["targets"][0]["target"] == "journal"
	crit = [{"code": "disk_critical"}]
	assert hu.auto_cleanup_due(crit, None, NOW)
	assert not hu.auto_cleanup_due(crit, NOW - timedelta(hours=2), NOW)
	assert hu.auto_cleanup_due(crit, NOW - timedelta(hours=13), NOW)
	assert not hu.auto_cleanup_due([{"code": "disk_warning"}], None, NOW)
	assert set(hu.SAFE_TARGETS) <= set(hu.CLEANUP_TARGETS)
	sites = [{"name": "a", "backup_frequency": "Daily", "last_backup_at": NOW - timedelta(days=3)},
	         {"name": "b", "backup_frequency": "Daily", "last_backup_at": NOW - timedelta(hours=5)}]
	assert hu.stale_backups(sites, NOW, {"Daily": timedelta(days=1)}) == ["a"]


def test_job_kinds_and_doctypes():
	src = read("agent_job.py")
	tree = ast.parse(src)
	kinds = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == "JOB_KINDS")
	for kind in ("server.os_update", "server.os_reboot", "server.os_configure", "server.diagnose", "server.cleanup"):
		assert kind in kinds
	job = json.loads(read("nexaploy", "doctype", "agent_job", "agent_job.json"))
	options = next(f["options"] for f in job["fields"] if f["fieldname"] == "kind").split("\n")
	assert options == list(kinds)
	server = {f["fieldname"]: f for f in json.loads(read("nexaploy", "doctype", "server", "server.json"))["fields"]}
	for f in ("os_pending_updates", "os_security_updates", "os_update_status", "maintenance_enabled", "maintenance_day",
	          "maintenance_hour", "maintenance_reboot", "unattended_security", "health_status", "health_issues",
	          "health_checked_at", "last_cleanup_at", "last_cleanup_freed", "auto_cleanup_enabled"):
		assert f in server, f
	assert server["os_update_status"]["options"].split("\n") == list(hu.OS_UPDATE_STATUSES)
	assert server["health_status"]["options"].split("\n") == list(hu.HEALTH_STATUSES)
	assert server["maintenance_day"]["options"].split("\n") == list(hu.DAYS)
	settings = {f["fieldname"]: f for f in json.loads(read("nexaploy", "doctype", "nexaploy_settings", "nexaploy_settings.json"))["fields"]}
	assert settings["allow_customer_os_updates"]["default"] == "1"
	for field in hu.SETTINGS_THRESHOLDS:
		assert field in settings


STRING_RE = re.compile(r"""(?<![A-Za-z0-9.])_{1,2}\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')\s*[,)]""")


def test_translations():
	ar = set()
	for path in [os.path.join(APP, "translations", "ar.csv")] + sorted(glob.glob(os.path.join(APP, "translations", "ar.*.part"))):
		with open(path, encoding="utf-8", newline="") as fh:
			ar |= {r[0] for r in csv.reader(fh) if r}
	strings = set()
	for parts in (("server_maintenance.py",), ("api", "server_maintenance.py")):
		strings |= {m.group(1) or m.group(2) for m in STRING_RE.finditer(read(*parts))}
	for page in ("server.html", "admin.html"):
		html = read("www", "dashboard", page)
		start = html.index("server maintenance & health")
		strings |= {m.group(1) or m.group(2) for m in STRING_RE.finditer(html[start:])}
	js = read("public", "js", "server_maintenance.js")
	strings |= set(re.findall(r'\bt\(\s*"((?:[^"\\]|\\.)*)"\s*[,)]', js))
	strings |= set(hu.TARGET_LABELS.values()) | set(hu.DAYS) | {"OK", "Warning", "Critical", "Unknown"}
	missing = sorted(strings - ar)
	assert not missing, missing


def test_wiring():
	hooks = read("hooks.py")
	assert "nexaploy.server_maintenance.check_server_health" in hooks
	assert "nexaploy.server_maintenance.schedule_maintenance" in hooks
	assert "server_maintenance.on_heartbeat(" in read("api", "agent.py")
	assert "import nexaploy.server_maintenance" in read("agent_callbacks.py")
	src = read("server_maintenance.py")
	for kind in ("server.os_update", "server.os_reboot", "server.os_configure", "server.diagnose", "server.cleanup"):
		assert f'"{kind}"' in src
	assert "hu.manage_allowed(" in src and "allow_customer_os_updates" in src
	html = read("www", "dashboard", "server.html")
	assert '<script src="/assets/nexaploy/js/server_maintenance.js"></script>' in html
	for label in ("System updates", "Update now", "Reboot now", "Maintenance window", "Automatic security updates",
	              "Reboot required", "Health", "Run full check", "Clean up"):
		assert f'_("{label}")' in html, label
