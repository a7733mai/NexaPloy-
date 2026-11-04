"""Agent upgrades from the control plane (operator-controlled; pure logic in agent_release_utils).

* platform admins publish **Agent Release** documents (version, git ref, channel)
* "Upgrade agent" (desk button / portal / bulk) creates an `agent.upgrade` Agent Job and
  sets Server.agent_upgrade_status = In Progress
* the heartbeat carrying the target `agent_version` marks it Succeeded; a failed job or no
  such heartbeat within `Nexaploy Settings.agent_upgrade_timeout_minutes` marks it Failed
  (the agent's watchdog rolls back by itself when the new agent does not come up)
* "Roll back agent" creates `agent.rollback` (needs a running agent; otherwise run
  `nexaploy-agent rollback` on the server, see docs)
"""

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from nexaploy import agent_release_utils as aru
from nexaploy.agent_callbacks import handles

SERVER_FIELDS = [
	"name", "server_id", "hostname", "team", "ownership", "status", "agent_version", "agent_channel",
	"agent_upgrade_status", "agent_upgrade_target", "agent_upgrade_job", "agent_upgrade_started_at",
	"agent_upgrade_error",
]


# --------------------------------------------------------------------------- settings / releases


def _settings():
	def get(field, default=None):
		try:
			value = frappe.db.get_single_value("Nexaploy Settings", field)
		except Exception:
			value = None
		return default if value in (None, "") else value

	return frappe._dict(
		allow_customers=cint(get("allow_customer_agent_upgrade", 0)),
		timeout_minutes=cint(get("agent_upgrade_timeout_minutes", aru.DEFAULT_TIMEOUT_MINUTES))
		or aru.DEFAULT_TIMEOUT_MINUTES,
		repo_url=get("agent_repo_url", aru.DEFAULT_REPO),
	)


def published_releases():
	return frappe.get_all(
		"Agent Release",
		filters={"published": 1},
		fields=["name", "version", "channel", "git_ref", "repo_url", "wheel_url", "wheel_sha256",
		        "published", "release_notes", "released_on"],
	)


def latest_release(channel="stable"):
	return aru.latest_release(published_releases(), channel or "stable")


def _server(name):
	row = frappe.db.get_value("Server", name, SERVER_FIELDS, as_dict=True)
	if not row:
		raise frappe.DoesNotExistError(_("Server {0} not found").format(name))
	return row


def check_upgrade_permission(server, user=None):
	"""Raise frappe.PermissionError unless `user` may upgrade/roll back this server's agent."""
	from nexaploy.team_utils import role_allows
	from nexaploy.teams import get_team_role, is_platform_admin

	admin = is_platform_admin(user)
	role = None if admin or not server.team else get_team_role(server.team, user)
	if not aru.upgrade_allowed(admin, _settings().allow_customers, server.ownership, role_allows(role, "admin") if role else False):
		raise frappe.PermissionError(_("You are not allowed to upgrade the agent on this server"))
	return admin


def server_status(name):
	"""{agent_version, channel, latest, upgrade_available, status, ...} for one server."""
	s = _server(name)
	release = latest_release(s.agent_channel)
	return {
		"server": s.name,
		"agent_version": s.agent_version,
		"channel": s.agent_channel or "stable",
		"latest": release and {
			"version": release.version, "channel": release.channel, "release_notes": release.release_notes,
			"released_on": release.released_on,
		},
		"upgrade_available": bool(release) and aru.is_older(s.agent_version, release.version),
		"blocker": aru.upgrade_blocker(s, release),
		"upgrade_status": s.agent_upgrade_status or "",
		"upgrade_target": s.agent_upgrade_target,
		"upgrade_job": s.agent_upgrade_job,
		"upgrade_error": s.agent_upgrade_error,
		"has_previous": s.agent_upgrade_status in (aru.SUCCEEDED, aru.FAILED),
	}


# --------------------------------------------------------------------------- actions


def _release_for(server, version=None):
	if version:
		version = aru.normalize_version(version)
		name = frappe.db.get_value("Agent Release", {"version": version, "published": 1}, "name")
		if not name:
			frappe.throw(_("Agent release {0} is not published").format(version))
		return frappe.db.get_value(
			"Agent Release", name,
			["name", "version", "channel", "git_ref", "repo_url", "wheel_url", "wheel_sha256", "published"],
			as_dict=True,
		)
	return latest_release(server.agent_channel)


def start_upgrade(server_name, version=None, user=None, check_permission=True):
	"""Create the `agent.upgrade` job for one server; returns the Agent Job name."""
	from nexaploy.agent_job import create_agent_job

	server = _server(server_name)
	admin = check_upgrade_permission(server, user) if check_permission else True
	release = _release_for(server, version)
	# an explicit version may be a re-install or downgrade (platform admins only)
	blocker = aru.upgrade_blocker(server, release, allow_reinstall=bool(version) and admin)
	if blocker:
		frappe.throw(_(blocker))
	params = aru.upgrade_params(release, _settings().repo_url)
	job = create_agent_job(server.name, "agent.upgrade", params, "Server", server.name)
	frappe.db.set_value(
		"Server",
		server.name,
		{
			"agent_upgrade_status": aru.IN_PROGRESS,
			"agent_upgrade_target": release.version,
			"agent_upgrade_job": job.name,
			"agent_upgrade_started_at": now_datetime(),
			"agent_upgrade_error": None,
		},
		update_modified=False,
	)
	_log(server, "server.agent_upgrade", {"from": server.agent_version, "to": release.version, "job": job.name})
	return job.name


def start_rollback(server_name, user=None):
	"""Create `agent.rollback` (the agent must be running to pick it up)."""
	from nexaploy.agent_job import create_agent_job

	server = _server(server_name)
	check_upgrade_permission(server, user)
	if server.agent_upgrade_status in aru.BUSY:
		frappe.throw(_("An agent upgrade or rollback is already in progress on this server"))
	if server.status != "Active":
		frappe.throw(
			_("The agent is not online. Run `nexaploy-agent rollback` on the server (see the documentation).")
		)
	job = create_agent_job(server.name, "agent.rollback", {}, "Server", server.name)
	frappe.db.set_value(
		"Server",
		server.name,
		{
			"agent_upgrade_status": aru.ROLLING_BACK,
			"agent_upgrade_target": server.agent_version,
			"agent_upgrade_job": job.name,
			"agent_upgrade_started_at": now_datetime(),
			"agent_upgrade_error": None,
		},
		update_modified=False,
	)
	_log(server, "server.agent_rollback", {"from": server.agent_version, "job": job.name})
	return job.name


def bulk_upgrade(channel=None, dry_run=False):
	"""Platform admins: upgrade every Active server whose agent is older than the latest
	release of its channel. Returns [{server, from, to, job?}]."""
	from nexaploy.teams import is_platform_admin

	if not is_platform_admin():
		raise frappe.PermissionError(_("Only platform administrators can upgrade agents in bulk"))
	servers = frappe.get_all("Server", filters={"status": "Active"}, fields=SERVER_FIELDS)
	out = []
	for server, release in aru.bulk_candidates(servers, published_releases(), channel or None):
		row = {"server": server.name, "hostname": server.hostname, "from": server.agent_version, "to": release["version"]}
		if not dry_run:
			try:
				row["job"] = start_upgrade(server.name, check_permission=False)
			except Exception as exc:
				row["error"] = str(exc)
		out.append(row)
	return out


def _log(server, action, details):
	if not server.team:
		return
	try:
		from nexaploy.activity import log_activity

		log_activity(server.team, action, "Server", server.name, details)
	except Exception:
		pass


# --------------------------------------------------------------------------- tracking


def on_heartbeat(server_name, agent_version):
	"""Called by api.agent.heartbeat with the reported agent_version (never raises)."""
	try:
		status, target = frappe.db.get_value(
			"Server", server_name, ["agent_upgrade_status", "agent_upgrade_target"]
		) or (None, None)
		new = aru.heartbeat_transition(status, target, agent_version)
		if new:
			frappe.db.set_value("Server", server_name, {"agent_upgrade_status": new}, update_modified=False)
	except Exception:
		frappe.log_error(title=f"Nexaploy: agent upgrade tracking failed for {server_name}")


def check_agent_upgrades():
	"""Scheduler (every 5 min): upgrades/rollbacks without the expected heartbeat in time -> Failed."""
	timeout = _settings().timeout_minutes
	now = now_datetime()
	rows = frappe.get_all(
		"Server",
		filters={"agent_upgrade_status": ["in", list(aru.BUSY)]},
		fields=["name", "agent_upgrade_status", "agent_upgrade_target", "agent_upgrade_started_at", "agent_version"],
	)
	for row in rows:
		if not aru.upgrade_timed_out(row.agent_upgrade_status, row.agent_upgrade_started_at, now, timeout):
			continue
		error = _(
			"The agent did not report version {0} within {1} minutes (it reports {2}). "
			"The agent rolls back by itself when the new version does not start; otherwise "
			"use Roll back agent or run `nexaploy-agent rollback` on the server."
		).format(row.agent_upgrade_target, timeout, row.agent_version or "?")
		frappe.db.set_value(
			"Server", row.name, {"agent_upgrade_status": aru.FAILED, "agent_upgrade_error": error},
			update_modified=False,
		)


@handles("agent.upgrade", "agent.rollback")
def on_agent_upgrade_job(job):
	"""Agent Job callback (reference: the Server)."""
	if job.reference_doctype != "Server":
		return
	current = frappe.db.get_value("Server", job.reference_name, "agent_upgrade_status")
	new = aru.job_transition(job.kind, job.status, current)
	if not new:
		return
	values = {"agent_upgrade_status": new}
	if new == aru.FAILED:
		values["agent_upgrade_error"] = (job.error or _("The agent job failed"))[:1000]
	frappe.db.set_value("Server", job.reference_name, values, update_modified=False)
