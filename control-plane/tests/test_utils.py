"""Pure-logic tests for nexaploy.utils (no frappe needed)."""

import ast
import os
from datetime import datetime, timedelta

import pytest

from nexaploy import utils as u

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "nexaploy")


def test_utils_does_not_import_frappe():
	tree = ast.parse(open(os.path.join(APP, "utils.py")).read())
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			assert all(not a.name.startswith("frappe") for a in node.names)
		if isinstance(node, ast.ImportFrom):
			assert not (node.module or "").startswith("frappe")


# ------------------------------------------------------------------ names


@pytest.mark.parametrize(
	"name",
	["erp.example.com", "a1.io", "my-site.customer.nexaploy.cloud", "x-y.example.co"],
)
def test_valid_site_names(name):
	assert u.is_valid_site_name(name)


@pytest.mark.parametrize(
	"name",
	[
		"",
		None,
		"localhost",
		"Erp.Example.com",
		"-bad.example.com",
		"bad-.example.com",
		"a..b.com",
		"under_score.example.com",
		"example.c",
		"a.b",  # too short (<4)
		("a" * 64) + ".com",  # label > 63
		"site.example.com.",
		"../etc.com",
	],
)
def test_invalid_site_names(name):
	assert not u.is_valid_site_name(name)


def test_site_name_max_length():
	label = "a" * 60
	name = ".".join([label] * 4) + ".com"  # 248 chars
	assert len(name) <= 253 and u.is_valid_site_name(name)
	assert not u.is_valid_site_name(".".join([label] * 5) + ".com")


def test_site_slug():
	assert u.site_slug("erp.example.com") == "erp-example-com"


@pytest.mark.parametrize("name,ok", [
	("erp-v15-main", True), ("abc", True), ("ab", False), ("1abc", False),
	("Erp", False), ("a" * 31, True), ("a" * 32, False), ("erp_v15", False),
])
def test_bench_names(name, ok):
	assert u.is_valid_bench_name(name) is ok
	assert u.is_valid_release_group_name(name) is ok


def test_app_names():
	assert u.is_valid_app_name("erpnext")
	assert u.is_valid_app_name("india_compliance")
	assert not u.is_valid_app_name("ERPNext")
	assert not u.is_valid_app_name("my-app")


# ------------------------------------------------------------------ tokens


def test_token_hash_and_verify():
	t = u.generate_token()
	assert ":" not in t and len(t) >= 40
	h = u.hash_token(t)
	assert len(h) == 64 and h != t
	assert u.verify_token(t, h)
	assert not u.verify_token(t + "x", h)
	assert not u.verify_token("", h)
	assert not u.verify_token(t, None)
	assert not u.verify_token(None, h)


def test_tokens_unique():
	assert len({u.generate_token() for _ in range(200)}) == 200


def test_known_sha256():
	assert u.hash_token("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


@pytest.mark.parametrize("header,expected", [
	("token abc123", "abc123"),
	("Token abc123", "abc123"),
	("token   abc123  ", "abc123"),
	("Bearer abc123", None),
	("token key:secret", None),
	("token", None),
	("", None),
	(None, None),
	("token a b", None),
])
def test_parse_authorization_header(header, expected):
	assert u.parse_authorization_header(header) == expected


def test_server_id_format():
	sid = u.generate_server_id()
	assert sid.startswith("srv-") and len(sid) == 12


def test_install_command():
	cmd = u.install_command("https://cp.example.com/", "TOK")
	assert cmd == (
		"curl -fsSL https://cp.example.com/install.sh | sudo bash -s -- "
		"--token TOK --control-plane https://cp.example.com"
	)


def test_token_expired():
	now = datetime(2026, 11, 3, 12, 0)
	assert u.token_expired(None, now)
	assert u.token_expired(now, now)
	assert u.token_expired(now - timedelta(seconds=1), now)
	assert not u.token_expired(now + timedelta(hours=1), now)


# ------------------------------------------------------------------ statuses


@pytest.mark.parametrize("agent,cp", [
	("queued", "Queued"), ("running", "Running"), ("success", "Success"),
	("failure", "Failure"), ("cancelled", "Failure"), ("SUCCESS", "Success"),
])
def test_map_agent_status(agent, cp):
	assert u.map_agent_status(agent) == cp


@pytest.mark.parametrize("bad", ["", None, "done", "pending"])
def test_map_agent_status_unknown(bad):
	with pytest.raises(ValueError):
		u.map_agent_status(bad)


def test_merge_job_status_forward_only():
	assert u.merge_job_status("Pending", "Queued") == "Queued"
	assert u.merge_job_status("Queued", "Running") == "Running"
	assert u.merge_job_status("Running", "Queued") == "Running"
	assert u.merge_job_status("Running", "Success") == "Success"
	assert u.merge_job_status("Pending", "Failure") == "Failure"
	# terminal is sticky
	assert u.merge_job_status("Success", "Running") == "Success"
	assert u.merge_job_status("Success", "Failure") == "Success"
	assert u.merge_job_status("Failure", "Success") == "Failure"


def test_normalize_step_status():
	assert u.normalize_step_status("SKIPPED") == "skipped"
	assert u.normalize_step_status(None) == "pending"
	assert u.normalize_step_status("weird") == "pending"


def test_parse_agent_datetime():
	assert u.parse_agent_datetime("2026-11-03T21:30:00Z") == datetime(2026, 11, 3, 21, 30)
	assert u.parse_agent_datetime("2026-11-03T23:30:00+02:00") == datetime(2026, 11, 3, 21, 30)
	assert u.parse_agent_datetime("2026-11-03T21:30:00") == datetime(2026, 11, 3, 21, 30)
	assert u.parse_agent_datetime(None) is None
	assert u.parse_agent_datetime("garbage") is None


def test_heartbeat_stale():
	now = datetime(2026, 11, 3, 12, 0)
	assert u.heartbeat_stale(None, now)
	assert not u.heartbeat_stale(now - timedelta(minutes=4, seconds=59), now)
	assert not u.heartbeat_stale(now - timedelta(minutes=5), now)
	assert u.heartbeat_stale(now - timedelta(minutes=5, seconds=1), now)


# ------------------------------------------------------------------ marketplace / release groups

ERPNEXT = {
	"app_name": "erpnext",
	"approved": True,
	"repo_url": "https://github.com/frappe/erpnext",
	"releases": [
		{"frappe_major": "14", "branch": "version-14"},
		{"frappe_major": "15", "branch": "version-15"},
	],
}
HRMS = {
	"app_name": "hrms",
	"approved": True,
	"repo_url": "https://github.com/frappe/hrms",
	"releases": [{"frappe_major": "15", "branch": "version-15"}],
}
UNAPPROVED = {
	"app_name": "shady",
	"approved": False,
	"repo_url": "https://example.com/shady",
	"releases": [{"frappe_major": "15", "branch": "main"}],
}


def test_compatibility_ok():
	assert u.check_compatibility("15", [ERPNEXT, HRMS]) == []
	assert u.check_compatibility(15, [ERPNEXT]) == []


def test_compatibility_missing_release():
	errors = u.check_compatibility("14", [ERPNEXT, HRMS])
	assert errors == ["App hrms has no release for Frappe 14"]


def test_compatibility_unapproved_duplicate_frappe_and_major():
	errors = u.check_compatibility("15", [UNAPPROVED, ERPNEXT, ERPNEXT, {"app_name": "frappe"}])
	assert "App shady is not approved in the marketplace" in errors
	assert "App erpnext is listed more than once" in errors
	assert any("frappe" in e for e in errors)
	assert any("Unsupported" in e for e in u.check_compatibility("13", []))


def test_build_apps_json_defaults_branch_from_release():
	assert u.build_apps_json([ERPNEXT, HRMS], "15") == [
		{"url": "https://github.com/frappe/erpnext", "branch": "version-15"},
		{"url": "https://github.com/frappe/hrms", "branch": "version-15"},
	]


def test_build_apps_json_explicit_branch_and_skips_frappe():
	apps = [{**ERPNEXT, "branch": "my-fork"}, {"app_name": "frappe", "repo_url": "x", "branch": "y"}]
	assert u.build_apps_json(apps, "14") == [{"url": "https://github.com/frappe/erpnext", "branch": "my-fork"}]


def test_build_apps_json_missing_branch():
	with pytest.raises(ValueError):
		u.build_apps_json([HRMS], "14")


def test_compatible_apps_filter():
	all_apps = [ERPNEXT, HRMS, UNAPPROVED]
	assert [a["app_name"] for a in u.compatible_apps(all_apps)] == ["erpnext", "hrms"]
	v14 = u.compatible_apps(all_apps, "14")
	assert [a["app_name"] for a in v14] == ["erpnext"]
	assert v14[0]["releases"] == [{"frappe_major": "14", "branch": "version-14"}]
	# input not mutated
	assert len(ERPNEXT["releases"]) == 2


def test_frappe_branch_for():
	assert u.frappe_branch_for(15) == "version-15"
	assert u.frappe_branch_for("16", "develop") == "develop"


def test_image_tag_and_build_id():
	bid = u.make_build_id(datetime(2026, 11, 3, 21, 30, 5))
	assert bid == "20261103T213005"
	assert u.image_tag("erp-v15-main", bid) == "nexaploy/bench:erp-v15-main-20261103T213005"


# ------------------------------------------------------------------ moves / backups / secrets


def test_plan_site_move():
	assert u.plan_site_move(True) == ["site.backup", "site.drop", "site.create", "site.restore"]
	assert u.plan_site_move(False) == ["site.backup", "site.create", "site.restore", "site.drop"]
	for same in (True, False):
		steps = u.plan_site_move(same)
		assert steps[0] == "site.backup"
		assert steps.index("site.create") < steps.index("site.restore")


def test_extract_backup_info():
	info = u.extract_backup_info({
		"remote_path": "nexaploy-backups:srv-ab12/erp/a.example.com/20261103T2130/",
		"manifest": {"created_at": "2026-11-03T21:30:00Z", "files": [{"name": "db.sql.gz", "size": 10}, {"name": "f.tar", "size": 5}]},
	})
	assert info == {
		"remote_path": "nexaploy-backups:srv-ab12/erp/a.example.com/20261103T2130/",
		"size": 15,
		"created_at": datetime(2026, 11, 3, 21, 30),
	}
	assert u.extract_backup_info(None) == {"remote_path": None, "size": None, "created_at": None}
	assert u.extract_backup_info({"remote": "r:x", "size": 7})["size"] == 7


def test_redact_params():
	p = {"name": "a.example.com", "admin_password": "s3cret", "nested": [{"api_key": "k", "x": 1}]}
	assert u.redact_params(p) == {"name": "a.example.com", "admin_password": "*****", "nested": [{"api_key": "*****", "x": 1}]}


def test_secret_placeholders():
	ref = u.secret_ref("Site", "a.example.com", "admin_password")
	assert ref == "__secret__:Site:a.example.com:admin_password"
	assert u.parse_secret_ref(ref) == ("Site", "a.example.com", "admin_password")
	assert u.parse_secret_ref("plain") is None
	assert u.parse_secret_ref("__secret__:a:b") is None
	params = {"name": "a.example.com", "admin_password": ref, "apps": ["erpnext"]}
	resolved = u.resolve_secrets(params, lambda dt, n, f: f"{dt}/{n}/{f}")
	assert resolved == {"name": "a.example.com", "admin_password": "Site/a.example.com/admin_password", "apps": ["erpnext"]}
	assert params["admin_password"] == ref
	with pytest.raises(ValueError):
		u.secret_ref("Site", "a:b", "x")
