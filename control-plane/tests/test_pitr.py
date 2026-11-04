"""Point-in-time recovery (ARCHITECTURE.md §14): pure helpers in nexaploy.pitr_utils + static checks."""

import ast
import csv
import json
import os
import re
from datetime import datetime, timedelta

import pytest

from nexaploy import pitr_utils as pu

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "nexaploy")
NOW = datetime(2026, 11, 4, 16, 0, 0)


def read(*parts):
	with open(os.path.join(APP, *parts), encoding="utf-8") as fh:
		return fh.read()


def backup(name, hours_ago, status="Success", server="srv-1", binlog=True, remote="r:p/b/s/20261104T120000Z"):
	return {
		"name": name,
		"status": status,
		"remote_path": remote,
		"binlog_file": "mysql-bin.000042" if binlog else None,
		"binlog_position": 4 if binlog else None,
		"binlog_ready_at": NOW - timedelta(hours=hours_ago) if binlog else None,
		"binlog_server": server,
	}


# --------------------------------------------------------------------------- backup result


def test_binlog_fields():
	result = {"binlog": {"file": "mysql-bin.000042", "position": 1234567, "gtid": "0-1-9", "db_name": "_abc",
	                     "ready_at": "2026-11-04T12:00:05Z", "server_id": "srv-1"}}
	assert pu.binlog_fields(result) == {
		"binlog_file": "mysql-bin.000042", "binlog_position": 1234567, "binlog_gtid": "0-1-9",
		"binlog_db": "_abc", "binlog_ready_at": datetime(2026, 11, 4, 12, 0, 5),
	}
	# offsets become naive UTC; the manifest copy is accepted too
	nested = {"manifest": {"binlog": {**result["binlog"], "ready_at": "2026-11-04T15:00:05+03:00"}}}
	assert pu.binlog_fields(nested)["binlog_ready_at"] == datetime(2026, 11, 4, 12, 0, 5)
	# binary logging off / garbage: nothing recorded
	assert pu.binlog_fields({"binlog": None, "pitr_error": "off"}) == {}
	assert pu.binlog_fields({}) == {} and pu.binlog_fields(None) == {} and pu.binlog_fields("x") == {}
	assert pu.binlog_fields({"binlog": {**result["binlog"], "file": "../x"}}) == {}
	assert pu.binlog_fields({"binlog": {**result["binlog"], "position": "abc"}}) == {}
	assert pu.binlog_fields({"binlog": {**result["binlog"], "ready_at": None}}) == {}


# --------------------------------------------------------------------------- window


def test_window_and_base_backup():
	rows = [
		backup("old", 24 * 8),  # outside the 7-day binlog retention
		backup("b1", 30),
		backup("b2", 6),
		backup("failed", 3, status="Failure"),
		backup("nopos", 2, binlog=False),
		backup("other-server", 1, server="srv-2"),  # binlogs of another database server
		backup("local-only", 1, remote=None),
	]
	w = pu.pitr_window(rows, NOW, server="srv-1")
	assert w["available"] and w["backups"] == 2
	assert w["start"] == NOW - timedelta(hours=30)
	assert w["end"] == NOW - timedelta(minutes=pu.MARGIN_MINUTES)
	assert [r["name"] for r in pu.usable_backups(rows, NOW, "srv-1")] == ["b1", "b2"]
	# the latest backup restorable at the target: least binlog to replay
	assert pu.pick_base_backup(rows, NOW - timedelta(hours=10), NOW, "srv-1")["name"] == "b1"
	assert pu.pick_base_backup(rows, NOW - timedelta(hours=5), NOW, "srv-1")["name"] == "b2"
	assert pu.pick_base_backup(rows, NOW - timedelta(hours=40), NOW, "srv-1") is None
	assert pu.target_error(NOW - timedelta(hours=10), w) is None
	msg, args = pu.target_error(NOW - timedelta(hours=31), w)
	assert "earliest" in msg and args == ["2026-11-03T10:00:00Z"]
	msg, args = pu.target_error(NOW - timedelta(minutes=1), w)
	assert "latest" in msg and args == ["2026-11-04T15:55:00Z"]


def test_window_unavailable():
	w = pu.pitr_window([backup("nopos", 2, binlog=False)], NOW, server="srv-1")
	assert not w["available"] and "next backup" in w["reason"] and w["start"] is None
	assert pu.target_error(NOW, w)[0] == w["reason"]
	# a backup that only became restorable a minute ago: nothing to pick yet
	w = pu.pitr_window([backup("fresh", 1 / 60)], NOW)
	assert not w["available"] and "few minutes" in w["reason"]
	# sub-second ready_at rounds up to the next restorable second
	row = backup("b", 2)
	row["binlog_ready_at"] = NOW - timedelta(hours=2) + timedelta(microseconds=500)
	assert pu.pitr_window([row], NOW)["start"] == NOW - timedelta(hours=2) + timedelta(seconds=1)


def test_parse_target_and_iso():
	assert pu.parse_target("2026-11-04T15:15:00Z") == datetime(2026, 11, 4, 15, 15)
	assert pu.parse_target("2026-11-04T18:15:30.5+03:00") == datetime(2026, 11, 4, 15, 15, 30)
	for bad in ("", None, "tomorrow"):
		with pytest.raises(ValueError):
			pu.parse_target(bad)
	assert pu.iso_z(datetime(2026, 11, 4, 15, 15, 0, 999)) == "2026-11-04T15:15:00Z"
	assert pu.iso_z(None) is None


# --------------------------------------------------------------------------- params / names


def test_pitr_site_name():
	t = datetime(2026, 11, 4, 15, 15)
	assert pu.pitr_site_name("erp.nexaploy.cloud", t, "nexaploy.cloud") == "erp-pitr-202611041515.nexaploy.cloud"
	# custom domains get a name under the root domain (wildcard DNS + certificate)
	assert pu.pitr_site_name("erp.example.com", t, "nexaploy.cloud") == "erp-pitr-202611041515.nexaploy.cloud"
	long = pu.pitr_site_name("a" * 63 + ".example.com", t, "nexaploy.cloud")
	assert len(long.split(".")[0]) <= 63 and long.endswith("-pitr-202611041515.nexaploy.cloud")
	with pytest.raises(ValueError):
		pu.pitr_site_name("erp.example.com", t, "")


def test_restore_params():
	t = datetime(2026, 11, 4, 15, 15)
	p = pu.restore_params("a.example.com", "erp-v15", "nexaploy-backups:srv/erp-v15/a.example.com/20261104T120000Z",
	                      t, "a-pitr-202611041515.nexaploy.cloud")
	assert p == {
		"site": "a.example.com", "bench": "erp-v15", "target_time": "2026-11-04T15:15:00Z",
		"remote_path": "nexaploy-backups:srv/erp-v15/a.example.com/20261104T120000Z",
		"in_place": False, "with_files": True, "target_site": "a-pitr-202611041515.nexaploy.cloud",
	}
	p = pu.restore_params("a.example.com", "erp-v15", "/opt/nexaploy/backups/erp-v15/a.example.com/20261104T120000Z/",
	                      t, in_place=True)
	assert p["backup_id"] == "20261104T120000Z" and "remote_path" not in p
	assert p["in_place"] is True and p["confirm"] == "a.example.com" and "target_site" not in p
	with pytest.raises(ValueError):
		pu.restore_params("a.example.com", "erp-v15", "r:x", t, "a.example.com")
	with pytest.raises(ValueError):
		pu.backup_source(None)
	with pytest.raises(ValueError):
		pu.backup_source("/opt/nexaploy/backups/x/y/not-a-ts")
	assert pu.archive_params() == {"rotate": True, "keep_days": pu.RETENTION_DAYS}


def test_failure_status():
	steps = [{"step_idx": 0, "step_name": "check", "status": "success"},
	         {"step_idx": 4, "step_name": "collect_binlogs", "status": "failure"},
	         {"step_idx": 5, "step_name": "safety_backup", "status": "skipped"}]
	assert pu.failed_step(steps) == "collect_binlogs"
	# an in-place restore that failed before touching the site leaves it as it was
	assert pu.failure_status(True, "collect_binlogs", "Active") == "Active"
	assert pu.failure_status(True, "apply", "Active") == "Broken"
	assert pu.failure_status(False, "check", "Pending") == "Broken"
	assert pu.failed_step([{"step_idx": 0, "step_name": "x", "status": "success"}]) is None


# --------------------------------------------------------------------------- static wiring


def _kinds():
	tree = ast.parse(read("agent_job.py"))
	for node in tree.body:
		if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "JOB_KINDS":
			return ast.literal_eval(node.value)


def test_job_kinds_callbacks_and_scheduler():
	assert {"site.restore_pitr", "server.archive_binlogs"} <= set(_kinds())
	callbacks = read("agent_callbacks.py")
	assert '@handles("site.restore_pitr")' in callbacks and '@handles("server.archive_binlogs")' in callbacks
	assert "backup_finished(backup, job)" in callbacks
	hooks = read("hooks.py")
	cron = hooks[hooks.index('"*/15 * * * *"'):]
	assert "nexaploy.pitr.schedule_binlog_archiving" in cron[: cron.index("]")]


def test_backups_request_pitr_and_doctype_fields():
	assert '"pitr": True' in read("nexaploy", "doctype", "site_backup", "site_backup.py")
	sb = json.loads(read("nexaploy", "doctype", "site_backup", "site_backup.json"))
	fields = {f["fieldname"]: f for f in sb["fields"]}
	for name in ("binlog_file", "binlog_position", "binlog_gtid", "binlog_db", "binlog_ready_at", "binlog_server"):
		assert name in fields and fields[name].get("read_only") == 1, name
		assert name in sb["field_order"]
	assert fields["binlog_ready_at"]["fieldtype"] == "Datetime"
	assert fields["binlog_server"]["options"] == "Server"
	site = json.loads(read("nexaploy", "doctype", "site", "site.json"))
	pitr_of = next(f for f in site["fields"] if f["fieldname"] == "pitr_of")
	assert pitr_of["fieldtype"] == "Link" and pitr_of["options"] == "Site"
	# a point-in-time copy is restored by the agent, never created with new-site
	src = read("nexaploy", "doctype", "site", "site.py")
	after = src[src.index("def after_insert"):src.index("def _create_params")]
	assert "self.flags.pitr_params" in after and '"site.restore_pitr"' in after
	assert after.index("pitr_params") < after.index('"site.create"')


def test_api_access_checks():
	src = read("api", "pitr.py")
	tree = ast.parse(src)
	funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
	for name in ("get_pitr_window", "restore_to_point"):
		assert any("whitelist" in ast.unparse(d) for d in funcs[name].decorator_list), name
	assert "methods=['POST']" in ast.unparse(funcs["restore_to_point"].decorator_list[0])
	assert "_site(site, 'read')" in ast.unparse(funcs["get_pitr_window"])
	assert "_site(site, 'write')" in ast.unparse(funcs["restore_to_point"])
	assert "check_team_access" in ast.unparse(funcs["_site"])
	body = ast.unparse(funcs["restore_to_point"])
	# overwriting needs the typed site name; activity is logged
	assert "confirm" in body and "site.restore_pitr" in body and "_log(" in body


def test_portal_card_and_translations():
	html = read("www", "dashboard", "site.html")
	assert '<script src="/assets/nexaploy/js/pitr.js"></script>' in html
	assert 'x-data="nxSitePitr"' in html and 'type="datetime-local"' in html
	assert ':min="pitrMin()"' in html and ':max="pitrMax()"' in html
	js = read("public", "js", "pitr.js")
	assert 'Alpine.data("nxSitePitr"' in js and "nexaploy.api.pitr." in js
	assert "toISOString()" in js  # the API gets UTC
	with open(os.path.join(APP, "translations", "ar.csv"), encoding="utf-8", newline="") as fh:
		translated = {r[0] for r in csv.reader(fh) if r}
	used = set(re.findall(r'\bt\("((?:[^"\\]|\\.)*)"', js))
	used |= set(re.findall(r'_\("((?:[^"\\]|\\.)*)"', read("api", "pitr.py")))
	used |= set(re.findall(r'_\("((?:[^"\\]|\\.)*)"', read("pitr.py")))
	utils = read("pitr_utils.py")
	used |= set(re.findall(r'"reason": "([^"]+)"', utils))
	used |= set(re.findall(r'raise ValueError\("([^"]+)"\)', utils))
	used |= set(re.findall(r'return \("([^"]+)"', utils))
	assert len(used) > 15
	missing = sorted(used - translated)
	assert not missing, missing
