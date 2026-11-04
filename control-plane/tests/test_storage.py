"""Offsite attachments, DR replica, trash bin (ARCHITECTURE.md §11): pure logic + static checks."""

import ast
import csv
import glob
import json
import os
import re
from datetime import datetime, timedelta

import pytest

from nexaploy import storage_utils as su
from nexaploy.billing import pricing

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "nexaploy")
REPO = os.path.dirname(ROOT)
DT = os.path.join(APP, "nexaploy", "doctype")
AGENT_STORAGE = os.path.join(REPO, "agent", "nexaploy_agent", "runtime", "storage.py")
AGENT_JOBS = os.path.join(REPO, "agent", "nexaploy_agent", "jobs", "storage_jobs.py")


def read(path):
	with open(path, encoding="utf-8") as fh:
		return fh.read()


def doctype(folder):
	return json.loads(read(os.path.join(DT, folder, folder + ".json")))


def fields(folder):
	return {f["fieldname"]: f for f in doctype(folder)["fields"]}


# --------------------------------------------------------------------------- pure logic


def test_storage_utils_has_no_frappe_import():
	tree = ast.parse(read(os.path.join(APP, "storage_utils.py")))
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			assert all(not a.name.startswith("frappe") for a in node.names)
		if isinstance(node, ast.ImportFrom):
			assert not (node.module or "").startswith("frappe")


def test_presets_match_agent_and_doctype():
	tree = ast.parse(read(AGENT_STORAGE))
	agent = None
	for node in tree.body:
		target = getattr(node, "target", None) or (node.targets[0] if isinstance(node, ast.Assign) else None)
		if getattr(target, "id", None) == "PROVIDER_PRESETS":
			agent = [k.value for k in node.value.keys]
	assert agent and set(agent) == set(su.PROVIDER_PRESETS)
	assert fields("storage_profile")["provider"]["options"].split("\n") == list(su.PROVIDER_PRESETS)
	assert set(fields("storage_profile")["addressing"]["options"].split("\n")) == set(su.ADDRESSING)


def _profile(**kw):
	p = {"profile_name": "de-primary", "provider": "Hetzner", "region": "fsn1", "bucket": "acme-files",
	     "access_key": "AK", "has_secret": True}
	p.update(kw)
	return p


def test_validate_profile():
	assert su.validate_profile(_profile()) == []
	assert su.validate_profile(_profile(provider="MinIO", endpoint="http://minio:9000", region="")) == []
	assert "Hetzner Object Storage needs a region" in su.validate_profile(_profile(region=""))
	assert "Cloudflare R2 needs a endpoint" in su.validate_profile(_profile(provider="R2"))
	assert su.validate_profile(_profile(profile_name="De_Primary"))
	assert "Invalid bucket name" in su.validate_profile(_profile(bucket="Bad Bucket"))
	assert "Invalid path prefix" in su.validate_profile(_profile(path_prefix="a/../b"))
	assert "Invalid endpoint" in su.validate_profile(_profile(endpoint="http://x y"))
	assert "Secret key is required" in su.validate_profile(_profile(has_secret=False))
	assert su.validate_profile(_profile(provider="Dropbox")) == ["Unknown storage provider"]


def test_profile_job_params():
	p = su.profile_job_params(
		{"name": "us-replica", "provider": "Wasabi", "region": "us-east-1", "bucket": " b ", "access_key": "AK",
		 "addressing": "Virtual-hosted style", "path_prefix": "/files/"},
		"__secret__:Storage Profile:us-replica:secret_key",
	)
	assert p == {
		"name": "us-replica", "preset": "Wasabi", "bucket": "b", "access_key_id": "AK",
		"secret_access_key": "__secret__:Storage Profile:us-replica:secret_key", "endpoint": None,
		"region": "us-east-1", "path_style": False, "prefix": "files",
	}
	assert su.profile_job_params({"name": "x", "addressing": "Preset default"}, "s")["path_style"] is None


@pytest.mark.parametrize("preset,kw,provider,endpoint,region", [
	("Hetzner", {"region": "fsn1"}, "Other", "fsn1.your-objectstorage.com", "fsn1"),
	("B2", {"region": "us-west-004"}, "Other", "s3.us-west-004.backblazeb2.com", "us-west-004"),
	("R2", {"endpoint": "https://acc.r2.cloudflarestorage.com"}, "Cloudflare", "https://acc.r2.cloudflarestorage.com", "auto"),
	("AWS", {"region": "eu-central-1"}, "AWS", None, "eu-central-1"),
	("IDrive", {"endpoint": "e2.example"}, "IDrive", "e2.example", None),
])
def test_backup_settings_from_profile(preset, kw, provider, endpoint, region):
	values = su.backup_settings_from_profile({"provider": preset, "bucket": "bk", "access_key": "AK", **kw})
	assert values["backup_provider"] == provider
	assert values["backup_endpoint"] == endpoint
	assert values["backup_region"] == region
	assert values["backup_bucket"] == "bk" and values["backup_remote_type"] == "s3"


def test_profile_usable():
	assert su.profile_usable({"enabled": 1, "team": None}, "t1")
	assert su.profile_usable({"enabled": 1, "team": "t1"}, "t1")
	assert not su.profile_usable({"enabled": 1, "team": "t2"}, "t1")
	assert not su.profile_usable({"enabled": 0}, "t1")
	assert not su.profile_usable(None, "t1")


def test_validate_policy():
	assert su.validate_policy("Local", None, None, None, None) == []
	assert su.validate_policy("Offloaded", "a", "b", "c", 30) == []
	assert su.validate_policy("Offloaded", None, None, None, None) == ["Choose a primary storage profile"]
	assert "The trash bin needs a replica" in su.validate_policy("Offloaded", "a", None, "c", 30)
	assert su.validate_policy("Offloaded", "a", "a", None, None)
	assert su.validate_policy("Offloaded", "a", "b", "b", 30)
	assert su.validate_policy("Offloaded", "a", "b", "c", 400, 365) == ["Trash retention must be between 1 and 365 days"]
	assert su.validate_policy("Cloud", "a", None, None, None) == ["Unknown attachments mode"]


def test_policy_job_params():
	profiles = {n: {"name": n} for n in ("p", "r", "t")}
	params = su.policy_job_params(
		{"site": "a.example.com", "primary_profile": "p", "replica_profile": "r", "trash_profile": "t",
		 "trash_retention_days": 14, "active_target": "replica"}, profiles, {"cache_max_size": "1G"},
	)
	assert params["primary"] == "p" and params["replica"] == "r" and params["trash"] == "t"
	assert params["active"] == "replica" and params["trash_retention_days"] == 14
	assert [p["name"] for p in params["profiles"]] == ["p", "r", "t"]
	assert params["cache_max_size"] == "1G"
	assert "cache_max_size" not in su.policy_job_params({"site": "a.example.com", "primary_profile": "p"}, profiles,
	                                                    {"cache_max_size": "huge"})


def test_desired_addons_and_features():
	plans = {"offload": "Offsite Attachments", "replica": "DR Replica", "trash": "Trash Bin"}
	assert su.desired_addons({"attachments_mode": "Local", "replica_profile": "r"}, plans) == {}
	assert su.desired_addons({"attachments_mode": "Offloaded"}, plans) == {"offload": "Offsite Attachments"}
	assert su.desired_addons({"attachments_mode": "Offloaded", "replica_profile": "r", "trash_profile": "t"}, plans) == plans
	assert su.desired_addons({"attachments_mode": "Offloaded", "replica_profile": "r"}, {"offload": None, "replica": "R"}) == {"replica": "R"}
	assert su.desired_addons(None, plans) == {}
	pol = {"attachments_mode": "Offloaded", "replica_profile": "r", "trash_profile": "t"}
	assert len(su.feature_errors(pol, {"enabled": 0})) == 3
	assert su.feature_errors(pol, {"enabled": 1, "replica_enabled": 1, "trash_enabled": 1}) == []


def test_replication_scheduling():
	now = datetime(2026, 11, 4, 12, 0)
	assert su.replication_due(None, 15, now)
	assert su.replication_due(now - timedelta(minutes=15), 15, now)
	assert su.replication_due(now - timedelta(minutes=14, seconds=30), 15, now)  # scheduler slack
	assert not su.replication_due(now - timedelta(minutes=10), 15, now)
	assert not su.replication_due(now - timedelta(minutes=3), 1, now)  # floor of 5 minutes
	pol = {"site": "a.example.com", "attachments_mode": "Offloaded", "status": "Offloaded", "primary_profile": "p",
	       "replica_profile": "r", "trash_profile": "t", "active_target": "primary"}
	assert su.replicate_site_params(pol, {"replication_max_delete": 50, "replication_compare": "checksum"}) == {
		"site": "a.example.com", "primary": "p", "replica": "r", "trash": "t", "max_delete": 50, "compare": "checksum",
	}
	# never sync from a stale primary
	assert su.replicate_site_params({**pol, "active_target": "replica"}, {}) is None
	assert su.replicate_site_params({**pol, "status": "Migrating"}, {}) is None
	assert su.replicate_site_params({**pol, "replica_profile": None}, {}) is None
	assert su.replicate_site_params(pol, {"replication_compare": "bogus"})["compare"] == "size"
	assert su.group_by_server([{"server": "s1", "x": 1}, {"server": "s1"}, {"server": "s2"}, {"server": None}]).keys() == {"s1", "s2"}


def test_replication_outcome_and_trash_items():
	assert su.replication_outcome({"status": "success", "trashed": 3}) == {
		"last_sync_status": "Success", "last_sync_error": "", "last_trashed": 3}
	out = su.replication_outcome({"status": "failure", "error": "AccessDenied"})
	assert out["last_sync_status"] == "Failure" and out["last_sync_error"] == "AccessDenied"
	assert su.replication_outcome({"status": "skipped", "reason": "failed over"})["last_sync_status"] == "Skipped"
	assert su.valid_trash_item("20261104T101500Z/public/a b.pdf")
	assert su.valid_trash_item("20261104T101500Z")
	for bad in ("", None, "../x", "20261104T101500Z/backups/x", "20261104T101500Z/public/../../x", "x/public/a"):
		assert not su.valid_trash_item(bad)
	assert su.parse_agent_ts("20261104T101500Z") == datetime(2026, 11, 4, 10, 15)
	assert su.parse_agent_ts("nope") is None


def test_clean_settings_update():
	out = su.clean_settings_update({"enabled": "1", "replica_enabled": False, "replication_interval_minutes": "30",
	                                "cache_max_size": "512M", "default_primary_profile": " de-primary "})
	assert out == {"enabled": 1, "replica_enabled": 0, "replication_interval_minutes": 30, "cache_max_size": "512M",
	               "default_primary_profile": "de-primary"}
	assert su.clean_settings_update({"default_trash_profile": ""}) == {"default_trash_profile": None}
	for bad, msg in [({"secret": 1}, "Unknown setting"), ({"replication_interval_minutes": 1}, "at least 5"),
	                 ({"replication_compare": "md5"}, "Compare mode"), ({"cache_max_size": "lots"}, "Cache size"),
	                 ({"replication_max_delete": -1}, "negative"), ({"max_trash_retention_days": "x"}, "number")]:
		with pytest.raises(ValueError, match=msg):
			su.clean_settings_update(bad)
	# every settings key is a Storage Settings field
	assert set(su.SETTINGS_FIELDS) <= set(fields("storage_settings"))
	assert set(su.ADDON_FIELDS.values()) <= set(fields("storage_settings"))


# --------------------------------------------------------------------------- billing


def test_addon_billing():
	assert "Nexaploy Plan" in pricing.SUBSCRIBABLE
	assert pricing.describe({"document_type": "Nexaploy Plan", "document_name": "DR Replica", "site": "a.example.com"}) == \
		"Add-on DR Replica on a.example.com"
	items = pricing.build_invoice_items([
		{"document_type": "Site", "document_name": "a.example.com", "price_monthly": 5, "start_date": "2026-10-01"},
		{"document_type": "Nexaploy Plan", "document_name": "Trash Bin", "site": "a.example.com", "price_monthly": 2,
		 "start_date": "2026-10-01"},
	], ("2026-10-01", "2026-10-31"))
	assert [i["document_type"] for i in items] == ["Site", "Nexaploy Plan"]
	assert str(items[1]["amount"]) == "2.00"
	assert "Add-on" in fields("nexaploy_plan")["plan_type"]["options"].split("\n")
	src = read(os.path.join(APP, "billing", "subscriptions.py"))
	assert "def sync_site_addons" in src and "cancel_site_addon_subscriptions(doc.name)" in src
	install = read(os.path.join(APP, "install.py"))
	for plan in su.ADDON_DEFAULT_PLANS.values():
		assert f'"title": "{plan}"' in install


# --------------------------------------------------------------------------- doctypes / wiring


def test_doctypes():
	prof = fields("storage_profile")
	assert prof["secret_key"]["fieldtype"] == "Password"
	assert prof["profile_name"]["unique"] == 1
	assert doctype("storage_profile")["autoname"] == "field:profile_name"
	for f in ("provider", "endpoint", "region", "bucket", "path_prefix", "access_key", "addressing", "team", "enabled"):
		assert f in prof
	pol = fields("site_storage_policy")
	for f in ("site", "team", "attachments_mode", "primary_profile", "replica_profile", "trash_profile",
	          "trash_retention_days", "status", "active_target", "last_sync_at", "last_sync_status", "server"):
		assert f in pol
	assert pol["attachments_mode"]["options"].split("\n") == list(su.ATTACHMENT_MODES)
	assert pol["site"]["unique"] == 1
	assert doctype("storage_settings")["issingle"] == 1
	assert pol["replica_profile"]["options"] == "Storage Profile"


def _string_tuple(path, name):
	for node in ast.parse(read(path)).body:
		if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == name:
			return ast.literal_eval(node.value)
	return None


def test_job_kinds_match_agent():
	agent_kinds = set(re.findall(r'kind = "((?:site|server)\.storage_[a-z_]+)"', read(AGENT_JOBS)))
	assert len(agent_kinds) == 8
	cp_kinds = set(_string_tuple(os.path.join(APP, "agent_job.py"), "JOB_KINDS"))
	assert agent_kinds <= cp_kinds
	src = read(os.path.join(APP, "storage.py"))
	for kind in agent_kinds:
		assert f'"{kind}"' in src  # a callback for every kind
	assert "_storage.register(handles)" in read(os.path.join(APP, "agent_callbacks.py"))


def test_scheduler_hooks():
	src = read(os.path.join(APP, "hooks.py"))
	assert '"nexaploy.storage.schedule_replication"' in src
	assert '"nexaploy.storage.prune_trash"' in src


def _functions(path):
	return {n.name: n for n in ast.parse(read(path)).body if isinstance(n, ast.FunctionDef)}


def _whitelisted(fn):
	for d in fn.decorator_list:
		target = d.func if isinstance(d, ast.Call) else d
		if isinstance(target, ast.Attribute) and target.attr == "whitelist":
			assert not (isinstance(d, ast.Call) and any(k.arg == "allow_guest" for k in d.keywords))
			return True
	return False


def _calls(fn):
	return {getattr(n.func, "id", getattr(n.func, "attr", None)) for n in ast.walk(fn) if isinstance(n, ast.Call)}


def test_api_access_checks():
	fns = _functions(os.path.join(APP, "api", "storage.py"))
	admin = ["get_storage_settings", "update_storage_settings", "admin_save_storage_profile",
	         "admin_delete_storage_profile", "admin_use_profile_for_backups"]
	customer = ["get_site_storage", "update_site_storage", "failover_site_storage", "refresh_site_storage_status",
	            "list_site_trash", "restore_site_trash", "get_storage_job"]
	team = ["list_team_profiles", "save_team_profile", "delete_team_profile"]
	for name in admin + customer + team:
		assert name in fns and _whitelisted(fns[name]), name
	for name in admin:
		assert "_require_platform_admin" in _calls(fns[name]), name
	for name in customer:
		assert "_site" in _calls(fns[name]), name
	for name in team:
		assert "_check" in _calls(fns[name]), name
	# paid add-ons: changing them needs the billing role too
	upd = fns["update_site_storage"]
	assert any(isinstance(n, ast.Constant) and n.value == "billing" for n in ast.walk(upd))
	assert any(isinstance(n, ast.Constant) and n.value == "admin" for n in ast.walk(fns["save_team_profile"]))


def test_portal_storage_tab_and_translations():
	html = read(os.path.join(APP, "www", "dashboard", "site.html"))
	assert "/assets/nexaploy/js/storage.js" in html and 'x-data="nxSiteStorage"' in html
	js = read(os.path.join(APP, "public", "js", "storage.js"))
	assert 'Alpine.data("nxSiteStorage"' in js
	rows = []
	for path in [os.path.join(APP, "translations", "ar.csv")] + glob.glob(os.path.join(APP, "translations", "ar.*.part")):
		with open(path, encoding="utf-8", newline="") as fh:
			rows += [r for r in csv.reader(fh) if r]
	translated = {r[0] for r in rows}
	js_strings = set(re.findall(r'\bt\("((?:[^"\\]|\\.)*)"', js))
	assert js_strings and js_strings <= translated, js_strings - translated
	api_strings = set(re.findall(r'_\("((?:[^"\\]|\\.)*)"', read(os.path.join(APP, "api", "storage.py"))))
	assert api_strings <= translated, api_strings - translated
	errors = set(re.findall(r'errors\.append\("([^"{]+)"\)', read(os.path.join(APP, "storage_utils.py"))))
	assert errors <= translated, errors - translated
	for status in ("Local", "Migrating", "Migrating Back", "Offloaded", "Failed Over", "Error", "Skipped"):
		assert status in translated
