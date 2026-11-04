"""Agent releases and upgrades (nexaploy/agent_release_utils.py) + static checks."""

import json
import os
import re
from datetime import datetime, timedelta

import pytest

from nexaploy import agent_release_utils as aru

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPO = os.path.dirname(ROOT)
APP = os.path.join(ROOT, "nexaploy")


def rel(version, channel="stable", published=1, **kw):
	return {"version": version, "channel": channel, "published": published, **kw}


# --------------------------------------------------------------------------- versions
def test_parse_and_compare_versions():
	assert aru.parse_version("v1.2.3")[:3] == (1, 2, 3)
	assert aru.parse_version("1.2") is None and aru.parse_version("01.2.3") is None
	order = ["0.1.0", "0.1.1", "0.2.0-beta.1", "0.2.0-beta.2", "0.2.0-rc.1", "0.2.0", "0.10.0", "1.0.0"]
	for a, b in zip(order, order[1:]):
		assert aru.compare_versions(a, b) == -1, (a, b)
		assert aru.compare_versions(b, a) == 1
	assert aru.compare_versions("v0.2.0", "0.2.0") == 0
	assert aru.compare_versions(None, "0.1.0") == -1
	assert aru.is_older(None, "0.1.0") and aru.is_older("0.1.0", "0.2.0")
	assert not aru.is_older("0.2.0", "0.2.0") and not aru.is_older("0.3.0", "0.2.0")
	assert not aru.is_older("0.1.0", None)


def test_normalize_and_validate_release():
	assert aru.normalize_version(" v0.2.0 ") == "0.2.0"
	with pytest.raises(ValueError):
		aru.normalize_version("latest")
	r = aru.validate_release("v0.2.0")
	assert r == {"version": "0.2.0", "git_ref": "v0.2.0", "channel": "stable", "repo_url": None,
	             "wheel_url": None, "wheel_sha256": None}
	assert aru.validate_release("0.3.0-beta.1", "main", "beta")["git_ref"] == "main"
	for bad in (
		dict(version="0.2.0", channel="nightly"),
		dict(version="0.2.0", git_ref="--upload-pack=x"),
		dict(version="0.2.0", git_ref="a..b"),
		dict(version="0.2.0", repo_url="http://example.com/x.git"),
		dict(version="0.2.0", repo_url="ssh://git@example.com/x.git"),
		dict(version="0.2.0", wheel_url="https://x.io/a.zip"),
		dict(version="0.2.0", wheel_sha256="a" * 64),  # sha without wheel
		dict(version="0.2.0", wheel_url="https://x.io/a-0.2.0-py3-none-any.whl", wheel_sha256="xyz"),
	):
		with pytest.raises(ValueError):
			aru.validate_release(**bad)


# --------------------------------------------------------------------------- releases
def test_latest_release_per_channel():
	releases = [rel("0.1.0"), rel("0.2.0"), rel("0.3.0-beta.1", "beta"), rel("0.9.0", published=0), rel("bogus")]
	assert aru.latest_release(releases, "stable")["version"] == "0.2.0"
	assert aru.latest_release(releases, "beta")["version"] == "0.3.0-beta.1"
	assert aru.latest_release(releases + [rel("0.3.0")], "beta")["version"] == "0.3.0"
	assert aru.latest_release([], "stable") is None
	assert aru.latest_release(releases, None)["version"] == "0.2.0"


def test_upgrade_params_match_agent_contract():
	p = aru.upgrade_params({"version": "0.2.0", "git_ref": "v0.2.0"}, "https://github.com/acme/nx.git")
	assert p == {"version": "0.2.0", "ref": "v0.2.0", "repo_url": "https://github.com/acme/nx.git"}
	p = aru.upgrade_params({"version": "0.2.0", "repo_url": "https://git.example.com/nx.git"}, None)
	assert p["ref"] == "v0.2.0" and p["repo_url"] == "https://git.example.com/nx.git"
	p = aru.upgrade_params({"version": "0.2.0", "wheel_url": "https://x.io/n-0.2.0-py3-none-any.whl",
	                        "wheel_sha256": "a" * 64}, watchdog_seconds=0)
	assert p == {"version": "0.2.0", "wheel_url": "https://x.io/n-0.2.0-py3-none-any.whl",
	             "wheel_sha256": "a" * 64, "watchdog_seconds": 0}
	# every key is a field of the agent's UpgradeParams
	src = open(os.path.join(REPO, "agent", "nexaploy_agent", "jobs", "agent_jobs.py")).read()
	for key in ("version", "ref", "repo_url", "wheel_url", "wheel_sha256", "watchdog_seconds"):
		assert re.search(rf"^    {key}: ", src, re.M), key


# --------------------------------------------------------------------------- permissions
def test_upgrade_allowed():
	assert aru.upgrade_allowed(True, False, "Hosted", False)
	assert not aru.upgrade_allowed(False, False, "BYOS", True)  # setting off
	assert aru.upgrade_allowed(False, True, "BYOS", True)
	assert not aru.upgrade_allowed(False, True, "Hosted", True)  # never hosted servers
	assert not aru.upgrade_allowed(False, True, "BYOS", False)  # Developer/Billing role


def test_upgrade_blocker_and_bulk():
	r = rel("0.2.0")
	active = {"name": "s1", "status": "Active", "agent_version": "0.1.0"}
	assert aru.upgrade_blocker(active, r) is None
	assert "No published" in aru.upgrade_blocker(active, None)
	assert "Active" in aru.upgrade_blocker({**active, "status": "Offline"}, r)
	assert "in progress" in aru.upgrade_blocker({**active, "agent_upgrade_status": "In Progress"}, r)
	assert "already" in aru.upgrade_blocker({**active, "agent_version": "0.2.0"}, r)
	assert aru.upgrade_blocker({**active, "agent_version": "0.2.0"}, r, allow_reinstall=True) is None

	servers = [
		{**active, "name": "old"},
		{**active, "name": "new", "agent_version": "0.2.0"},
		{**active, "name": "beta", "agent_channel": "beta"},
		{**active, "name": "busy", "agent_upgrade_status": "Rolling Back"},
		{**active, "name": "off", "status": "Offline"},
	]
	releases = [rel("0.2.0"), rel("0.3.0-beta.1", "beta")]
	got = {s["name"]: r["version"] for s, r in aru.bulk_candidates(servers, releases)}
	assert got == {"old": "0.2.0", "beta": "0.3.0-beta.1"}
	assert {s["name"] for s, _ in aru.bulk_candidates(servers, releases, "stable")} == {"old"}


# --------------------------------------------------------------------------- tracking
def test_heartbeat_and_job_transitions():
	assert aru.heartbeat_transition("In Progress", "0.2.0", "0.2.0") == "Succeeded"
	assert aru.heartbeat_transition("In Progress", "0.2.0", "0.1.0") is None  # not restarted yet
	assert aru.heartbeat_transition("Rolling Back", "0.2.0", "0.1.0") == "Rolled Back"
	assert aru.heartbeat_transition("Succeeded", "0.2.0", "0.1.0") is None
	assert aru.heartbeat_transition("In Progress", "0.2.0", None) is None

	assert aru.job_transition("agent.upgrade", "Success", "In Progress") is None  # wait for heartbeat
	assert aru.job_transition("agent.upgrade", "Failure", "In Progress") == "Failed"
	assert aru.job_transition("agent.upgrade", "Failure", "Succeeded") is None
	assert aru.job_transition("agent.rollback", "Success", "Rolling Back") == "Rolled Back"
	assert aru.job_transition("agent.rollback", "Failure", "Rolling Back") == "Failed"
	assert aru.job_transition("site.create", "Failure", "In Progress") is None


def test_upgrade_timeout():
	now = datetime(2026, 11, 4, 12, 0)
	assert aru.upgrade_timed_out("In Progress", now - timedelta(minutes=16), now, 15)
	assert not aru.upgrade_timed_out("In Progress", now - timedelta(minutes=14), now, 15)
	assert aru.upgrade_timed_out("Rolling Back", now - timedelta(minutes=20), now, None)
	assert not aru.upgrade_timed_out("Succeeded", now - timedelta(days=1), now, 15)
	assert not aru.upgrade_timed_out("In Progress", None, now, 15)


# --------------------------------------------------------------------------- static
def _doctype(folder):
	with open(os.path.join(APP, "nexaploy", "doctype", folder, folder + ".json")) as fh:
		return json.load(fh)


def test_agent_release_doctype():
	d = _doctype("agent_release")
	names = {f["fieldname"] for f in d["fields"]}
	assert {"version", "channel", "git_ref", "repo_url", "wheel_url", "wheel_sha256", "published",
	        "release_notes", "released_on"} <= names
	assert d["autoname"] == "field:version"
	assert [p["role"] for p in d["permissions"]] == ["System Manager"]  # platform admins only
	channel = next(f for f in d["fields"] if f["fieldname"] == "channel")
	assert channel["options"].split("\n") == list(aru.CHANNELS)


def test_server_and_settings_fields():
	server = {f["fieldname"]: f for f in _doctype("server")["fields"]}
	status = server["agent_upgrade_status"]
	assert tuple(status["options"].split("\n")) == aru.UPGRADE_STATUSES
	assert server["agent_channel"]["options"].split("\n") == list(aru.CHANNELS)
	assert server["agent_upgrade_job"]["options"] == "Agent Job"
	settings = {f["fieldname"] for f in _doctype("nexaploy_settings")["fields"]}
	assert {"allow_customer_agent_upgrade", "agent_upgrade_timeout_minutes", "agent_repo_url"} <= settings


def test_job_kinds_and_handlers_wired():
	src = open(os.path.join(APP, "agent_job.py")).read()
	assert '"agent.upgrade"' in src and '"agent.rollback"' in src
	cb = open(os.path.join(APP, "agent_callbacks.py")).read()
	assert "import nexaploy.agent_upgrades" in cb
	up = open(os.path.join(APP, "agent_upgrades.py")).read()
	assert '@handles("agent.upgrade", "agent.rollback")' in up
	hb = open(os.path.join(APP, "api", "agent.py")).read()
	assert "on_heartbeat(server, agent_version)" in hb
	agent = open(os.path.join(REPO, "agent", "nexaploy_agent", "jobs", "agent_jobs.py")).read()
	assert 'kind = "agent.upgrade"' in agent and 'kind = "agent.rollback"' in agent
