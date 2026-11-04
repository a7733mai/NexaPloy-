"""Agent releases and upgrades: pure logic (no frappe import, tested with plain pytest).

An **Agent Release** (version, git ref, channel stable/beta, published) is what a server can
be upgraded to. A server follows one channel: `stable` sees stable releases, `beta` sees
stable and beta releases. The operator decides when a server upgrades (`agent.upgrade` job);
the control plane then waits for a heartbeat carrying the new `agent_version` and marks the
upgrade Failed when it does not arrive in time (the agent's own watchdog rolls back).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Iterable, Mapping

SEMVER_RE = re.compile(
	r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
	r"(?:-(?P<pre>[0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)
# Same rules as the agent (runtime/upgrade.py): no "..", never starts with "-"
REF_RE = re.compile(r"^(?!.*\.\.)(?!-)[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
REPO_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:\d+)?/[A-Za-z0-9._~/-]+$")
WHEEL_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:\d+)?/[A-Za-z0-9._~/%+-]+\.whl$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

CHANNELS = ("stable", "beta")
DEFAULT_REPO = "https://github.com/a7733mai/NexaPloy-.git"
DEFAULT_TIMEOUT_MINUTES = 15

# Server.agent_upgrade_status
IN_PROGRESS = "In Progress"
SUCCEEDED = "Succeeded"
FAILED = "Failed"
ROLLING_BACK = "Rolling Back"
ROLLED_BACK = "Rolled Back"
UPGRADE_STATUSES = ("", IN_PROGRESS, SUCCEEDED, FAILED, ROLLING_BACK, ROLLED_BACK)
BUSY = (IN_PROGRESS, ROLLING_BACK)


# --------------------------------------------------------------------------- versions
def parse_version(value):
	"""'v1.2.3-beta.1' -> (1, 2, 3, ('beta', 1)) sortable key; None if not semver.

	A pre-release sorts before its release (1.0.0-rc.1 < 1.0.0), per semver.
	"""
	m = SEMVER_RE.match((value or "").strip())
	if not m:
		return None
	pre = m.group("pre")
	if pre is None:
		pre_key = (1,)  # releases sort after any pre-release
	else:
		parts = []
		for part in pre.split("."):
			parts.append((0, int(part), "") if part.isdigit() else (1, 0, part))
		pre_key = (0, tuple(parts))
	return (int(m.group("major")), int(m.group("minor")), int(m.group("patch")), pre_key)


def normalize_version(value):
	"""Strip a leading 'v' and whitespace; raise ValueError if not semver."""
	value = (value or "").strip()
	if parse_version(value) is None:
		raise ValueError(f"Invalid version {value!r}: use semantic versioning, e.g. 0.2.0")
	return value[1:] if value.startswith("v") else value


def compare_versions(a, b):
	"""-1 / 0 / 1; an unparsable version sorts before every valid one."""
	ka, kb = parse_version(a), parse_version(b)
	if ka == kb:
		return 0
	if ka is None:
		return -1
	if kb is None:
		return 1
	return -1 if ka < kb else 1


def is_older(current, latest):
	"""True when `current` (reported by the agent) is older than `latest`.
	An unknown current version counts as older (offer the upgrade)."""
	if not latest or parse_version(latest) is None:
		return False
	return compare_versions(current, latest) < 0


# --------------------------------------------------------------------------- releases
def validate_release(version, git_ref=None, channel="stable", repo_url=None, wheel_url=None, wheel_sha256=None):
	"""Normalised release fields or ValueError. Default ref: v<version>."""
	version = normalize_version(version)
	if channel not in CHANNELS:
		raise ValueError(f"Channel must be one of {', '.join(CHANNELS)}")
	git_ref = (git_ref or "").strip() or f"v{version}"
	if not REF_RE.match(git_ref):
		raise ValueError(f"Invalid git ref {git_ref!r}")
	repo_url = (repo_url or "").strip() or None
	if repo_url and (not REPO_URL_RE.match(repo_url) or ".." in repo_url):
		raise ValueError("Repository URL must be an https:// git URL")
	wheel_url = (wheel_url or "").strip() or None
	if wheel_url and (not WHEEL_URL_RE.match(wheel_url) or ".." in wheel_url):
		raise ValueError("Wheel URL must be an https:// URL of a .whl file")
	wheel_sha256 = (wheel_sha256 or "").strip().lower() or None
	if wheel_sha256 and not SHA256_RE.match(wheel_sha256):
		raise ValueError("Wheel SHA-256 must be 64 hex characters")
	if wheel_sha256 and not wheel_url:
		raise ValueError("Wheel SHA-256 needs a wheel URL")
	return {
		"version": version,
		"git_ref": git_ref,
		"channel": channel,
		"repo_url": repo_url,
		"wheel_url": wheel_url,
		"wheel_sha256": wheel_sha256,
	}


def channel_includes(server_channel, release_channel):
	"""A beta server also gets stable releases; a stable server only stable ones."""
	server_channel = server_channel or "stable"
	if server_channel == "beta":
		return release_channel in CHANNELS
	return release_channel == "stable"


def latest_release(releases: Iterable[Mapping], channel="stable"):
	"""Highest published release visible on `channel` (dicts with version/channel/published)."""
	best = None
	for r in releases:
		if not r.get("published") or parse_version(r.get("version")) is None:
			continue
		if not channel_includes(channel, r.get("channel") or "stable"):
			continue
		if best is None or compare_versions(r["version"], best["version"]) > 0:
			best = r
	return best


def upgrade_params(release: Mapping, default_repo=DEFAULT_REPO, watchdog_seconds=None):
	"""Params of the `agent.upgrade` job for a release (agent: jobs/agent_jobs.py)."""
	params = {"version": release["version"]}
	if release.get("wheel_url"):
		params["wheel_url"] = release["wheel_url"]
		if release.get("wheel_sha256"):
			params["wheel_sha256"] = release["wheel_sha256"]
	else:
		params["ref"] = release.get("git_ref") or f"v{release['version']}"
		params["repo_url"] = release.get("repo_url") or default_repo or DEFAULT_REPO
	if watchdog_seconds is not None:
		params["watchdog_seconds"] = int(watchdog_seconds)
	return params


# --------------------------------------------------------------------------- permissions
def upgrade_allowed(is_platform_admin, allow_customers, ownership, team_role_allows_admin):
	"""Platform admins upgrade any server. Customers only their own BYOS servers, only when
	Nexaploy Settings allows it and their team role allows the admin action."""
	if is_platform_admin:
		return True
	return bool(allow_customers) and ownership == "BYOS" and bool(team_role_allows_admin)


def upgrade_blocker(server: Mapping, release: Mapping | None, allow_reinstall=False):
	"""Why `server` cannot be upgraded to `release` now (None = it can)."""
	if not release:
		return "No published agent release for this server's channel"
	if server.get("status") != "Active":
		return "The server must be Active (online) to upgrade its agent"
	if server.get("agent_upgrade_status") in BUSY:
		return "An agent upgrade or rollback is already in progress on this server"
	if not allow_reinstall and not is_older(server.get("agent_version"), release["version"]):
		return "The agent is already on this version or newer"
	return None


def bulk_candidates(servers: Iterable[Mapping], releases: Iterable[Mapping], channel=None):
	"""[(server, release)] for every Active server whose agent is older than the latest
	release of its channel (optionally only servers on `channel`)."""
	releases = list(releases)
	latest_by_channel = {c: latest_release(releases, c) for c in CHANNELS}
	out = []
	for s in servers:
		ch = s.get("agent_channel") or "stable"
		if channel and ch != channel:
			continue
		release = latest_by_channel.get(ch)
		if upgrade_blocker(s, release) is None:
			out.append((s, release))
	return out


# --------------------------------------------------------------------------- tracking
def heartbeat_transition(status, target_version, reported_version):
	"""New Server.agent_upgrade_status when a heartbeat reports `reported_version`, else None.

	In Progress + target reached -> Succeeded. Rolling Back + version changed away from the
	target -> Rolled Back.
	"""
	if not reported_version:
		return None
	if status == IN_PROGRESS and target_version and compare_versions(reported_version, target_version) == 0:
		return SUCCEEDED
	if status == ROLLING_BACK and target_version and compare_versions(reported_version, target_version) != 0:
		return ROLLED_BACK
	return None


def upgrade_timed_out(status, started_at: datetime | None, now: datetime, timeout_minutes=DEFAULT_TIMEOUT_MINUTES):
	"""True when an upgrade/rollback has been running longer than the timeout."""
	if status not in BUSY or not started_at:
		return False
	minutes = int(timeout_minutes or DEFAULT_TIMEOUT_MINUTES)
	return now - started_at > timedelta(minutes=max(1, minutes))


def job_transition(kind, job_status, current_status):
	"""Server.agent_upgrade_status after an agent.upgrade/agent.rollback job update, or None.

	A successful agent.upgrade only means the new venv is in place and a restart is
	scheduled: Succeeded comes from the heartbeat. A failed job fails the upgrade (the
	running agent was not replaced). A successful rollback is Rolled Back.
	"""
	if kind == "agent.upgrade":
		if job_status == "Failure" and current_status == IN_PROGRESS:
			return FAILED
		return None
	if kind == "agent.rollback":
		if job_status == "Success":
			return ROLLED_BACK
		if job_status == "Failure" and current_status == ROLLING_BACK:
			return FAILED
	return None
