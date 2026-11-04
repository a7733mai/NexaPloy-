"""Control plane self-update + setup checklist: pure logic (nexaploy.controlplane_utils) and static
checks of the API, job kind, settings fields, page and translations."""

import ast
import csv
import json
import os

import pytest

from nexaploy import controlplane_utils as cu

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "nexaploy")


def read(*parts):
	with open(os.path.join(APP, *parts), encoding="utf-8") as fh:
		return fh.read()


def test_utils_has_no_frappe_import():
	tree = ast.parse(read("controlplane_utils.py"))
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			assert all(not a.name.startswith("frappe") for a in node.names)
		if isinstance(node, ast.ImportFrom):
			assert not (node.module or "").startswith("frappe")


RELEASES = [
	{"tag_name": "v0.1.0", "prerelease": False, "html_url": "u1"},
	{"tag_name": "v0.3.0-beta.1", "prerelease": True},
	{"tag_name": "v0.2.0", "prerelease": False, "name": "Nexaploy 0.2.0", "body": "notes"},
	{"tag_name": "v9.0.0", "draft": True},
	{"tag_name": "latest"},
]


def test_pick_release_and_versions():
	rel = cu.pick_release(RELEASES)
	assert rel["version"] == "0.2.0" and rel["name"] == "Nexaploy 0.2.0" and rel["notes"] == "notes"
	assert cu.pick_release(RELEASES, "beta")["version"] == "0.3.0-beta.1"
	assert cu.pick_release([]) is None and cu.pick_release(None) is None
	assert cu.is_newer("0.2.0", "0.1.0") and not cu.is_newer("0.1.0", "0.1.0")
	assert cu.is_newer("1.0.0", "1.0.0-beta.2") and not cu.is_newer(None, "0.1.0")
	assert cu.image_for_version("v0.2.0") == "ghcr.io/a7733mai/nexaploy-control-plane:0.2.0"


def test_release_settings_defaults():
	rs = cu.release_settings({})
	assert rs == {"channel": "stable", "image_repo": cu.DEFAULT_IMAGE_REPO, "release_repo": cu.DEFAULT_RELEASE_REPO}
	rs = cu.release_settings({"control_plane_release_channel": "beta", "control_plane_image_repo": "reg.example.com/cp/",
	                          "control_plane_release_repo": "evil/../x y"})
	assert rs["channel"] == "beta" and rs["image_repo"] == "reg.example.com/cp"
	assert rs["release_repo"] == cu.DEFAULT_RELEASE_REPO
	assert cu.releases_api_url("a/b").startswith("https://api.github.com/repos/a/b/releases")


def test_update_mode_and_target():
	assert cu.update_mode(None)["can_update"] is False
	assert "docker compose pull" in cu.update_mode(None)["reason"]
	host = {"name": "srv-1", "status": "Active", "control_plane_bench": "nexaploy-cp", "control_plane_site": "cp.example.com"}
	assert cu.update_mode(host) == {"mode": "all-in-one", "can_update": True, "reason": None}
	assert cu.update_mode({**host, "status": "Offline"})["can_update"] is False
	latest = {"version": "0.2.0"}
	assert cu.resolve_target(None, None, latest, "r/cp") == "r/cp:0.2.0"
	assert cu.resolve_target(None, "v0.3.0", latest, "r/cp") == "r/cp:0.3.0"
	assert cu.resolve_target("reg.example.com/cp:9", "1.0.0", latest, "r/cp") == "reg.example.com/cp:9"
	for bad in (("no tag", None), (None, "abc")):
		with pytest.raises(cu.ControlPlaneError):
			cu.resolve_target(bad[0], bad[1], latest, "r/cp")
	with pytest.raises(cu.ControlPlaneError):
		cu.resolve_target(None, None, None, "r/cp")
	params = cu.update_params(host, "r/cp:0.2.0", "r/cp:0.1.0")
	assert params == {"bench": "nexaploy-cp", "site": "cp.example.com", "image": "r/cp:0.2.0",
	                  "previous_image": "r/cp:0.1.0", "backup": True, "upload": True}
	report = json.dumps([{"name": "erp", "image": "x:1"}, {"name": "nexaploy-cp", "image": "r/cp:0.1.0"}])
	assert cu.current_image(report, "nexaploy-cp") == "r/cp:0.1.0" and cu.current_image("garbage", "x") is None


def _job(status, steps, result=None, error=None):
	return {"name": "JOB-1", "status": status, "error": error, "result": json.dumps(result or {}),
	        "params": {"image": "r/cp:0.2.0"},
	        "steps": [{"step_idx": i, "step_name": n, "status": s} for i, (n, s) in enumerate(steps)]}


def test_job_progress_states():
	running = cu.job_progress(_job("Running", [("prepare", "Success"), ("backup", "Success"), ("pull", "Running")]))
	assert running["state"] == "running" and running["step"] == "pull" and running["done"] == ["prepare", "backup"]
	assert running["image"] == "r/cp:0.2.0" and running["finished"] is False
	assert cu.job_progress(_job("Success", [], {"image": "r/cp:0.2.0"}))["state"] == "updated"
	assert cu.job_progress(_job("Success", [], {"up_to_date": True}))["state"] == "up_to_date"
	rb = cu.job_progress(_job("Failure", [], {"rolled_back": True, "rollback": {"failed_step": "migrate"}}, "boom"))
	assert rb["state"] == "rolled_back" and rb["rollback"]["failed_step"] == "migrate" and rb["finished"]
	assert cu.job_progress(_job("Failure", [], {"rolled_back": False, "rollback": {"errors": ["x"]}}))["state"] == "rollback_incomplete"
	assert cu.job_progress(_job("Failure", [], {}))["state"] == "failed"
	assert cu.job_progress(None) is None


def test_setup_checklist():
	empty = cu.setup_checklist({})
	assert empty["completed"] == 0 and empty["show"] and empty["total"] == 8
	assert [i["key"] for i in empty["items"]] == [k for k, _, _ in cu.SETUP_ITEMS]
	full = cu.setup_checklist({
		"platform_name": "Acme Cloud", "root_domain": "acme.cloud", "billing_enabled": 1, "currency": "USD",
		"paymob_ready": True, "storage_profiles": 1, "outgoing_account": "Ops", "plans": 2,
		"external_probes_seen": 1, "active_sites": 1,
	})
	assert full["complete"] and not full["show"]
	partial = cu.setup_checklist({"platform_name": "Acme", "root_domain": None, "backup_oauth": True, "relay_enabled": 1})
	done = {i["key"]: i["done"] for i in partial["items"]}
	assert done["platform"] is False and done["backups"] and done["email"] and not done["payments"]
	assert cu.setup_checklist({"dismissed": 1})["show"] is False


def test_job_kind_api_and_fields():
	src = read("agent_job.py")
	assert '"controlplane.update"' in src
	kinds = json.loads(read("nexaploy", "doctype", "agent_job", "agent_job.json"))
	kind_field = next(f for f in kinds["fields"] if f["fieldname"] == "kind")
	assert kind_field["options"].split("\n")[-1] == "controlplane.update"
	assert '@handles("controlplane.update")' in read("agent_callbacks.py")
	settings = {f["fieldname"]: f for f in json.loads(read("nexaploy", "doctype", "nexaploy_settings", "nexaploy_settings.json"))["fields"]}
	for f in ("control_plane_release_channel", "control_plane_image_repo", "control_plane_release_repo", "setup_checklist_dismissed"):
		assert f in settings
	api = ast.parse(read("api", "controlplane.py"))
	for fn in [n for n in api.body if isinstance(n, ast.FunctionDef)]:
		assert any(isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "whitelist" for d in fn.decorator_list)
		first = fn.body[0]
		assert isinstance(first, ast.Expr) and getattr(first.value.func, "id", "") == "_admin", fn.name
	assert "hosts_control_plane\": 1, \"name\": [\"!=\", server]" in read("all_in_one.py")


def test_page_and_translations():
	page = read("www", "dashboard", "admin.html")
	assert "controlplane_admin.js" in page and "nxControlPlane" in read("templates", "includes", "controlplane_admin.html")
	js = read("public", "js", "controlplane_admin.js")
	for m in ("get_status", "start_update", "get_update", "get_setup_checklist", "dismiss_setup"):
		assert f'call("{m}"' in js, m
	assert '"nexaploy.api.controlplane."' in js
	with open(os.path.join(APP, "translations", "ar.csv"), encoding="utf-8") as fh:
		source = {row[0] for row in csv.reader(fh) if row}
	for s in ("Update control plane", "Setup checklist", "Dismiss", "Rolled back"):
		assert s in source, s
