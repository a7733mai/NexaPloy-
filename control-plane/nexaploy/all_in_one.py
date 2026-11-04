"""All-in-one installs: the control plane runs as a bench on a server it also manages.

`install.sh --with-control-plane <domain>` creates the control plane bench/site through the
local agent, then calls (via `bench --site <cp-site> execute`):

* bootstrap()  - operator team (billing exempt) + a short-lived registration token
* mark_host()  - after the agent registered: flag the Server that hosts the control plane

The control plane bench/site are not Bench/Site documents (the control plane must never
drop or move itself); backup_control_plane() backs its site up daily through the agent.
"""

import frappe
from frappe import _

PLATFORM_TEAM_TITLE = "Platform"
BOOTSTRAP_KEY = "nexaploy_bootstrap"


def _platform_team():
	"""The operator's team (owned by Administrator), created once and billing exempt."""
	name = frappe.db.get_value("Team", {"owner_user": "Administrator", "billing_exempt": 1}, "name")
	if name:
		return name
	from nexaploy.teams import create_team

	doc = create_team(PLATFORM_TEAM_TITLE, owner="Administrator")
	frappe.db.set_value("Team", doc.name, "billing_exempt", 1)
	return doc.name


def bootstrap():
	"""Operator team + a 2h single-use registration token for the local agent.
	Returns {"nexaploy_bootstrap": {"team", "token"}} (bench execute prints it as JSON)."""
	frappe.set_user("Administrator")
	from nexaploy.nexaploy.doctype.server_registration_token.server_registration_token import (
		generate_registration_token,
	)

	team = _platform_team()
	result = generate_registration_token(team=team, expires_in_hours=2)
	frappe.db.commit()
	return {BOOTSTRAP_KEY: {"team": team, "token": result["token"]}}


def mark_host(server_id, bench, site):
	"""Flag the registered Server as the control plane host."""
	frappe.set_user("Administrator")
	server = frappe.db.get_value("Server", {"server_id": server_id}, "name")
	if not server:
		frappe.throw(_("Server {0} is not registered").format(server_id))
	frappe.db.set_value(
		"Server",
		server,
		{"hosts_control_plane": 1, "control_plane_bench": bench, "control_plane_site": site},
	)
	frappe.db.commit()
	return {BOOTSTRAP_KEY: {"server": server}}


def control_plane_host():
	return frappe.db.get_value(
		"Server",
		{"hosts_control_plane": 1, "status": ["!=", "Archived"]},
		["name", "control_plane_bench", "control_plane_site"],
		as_dict=True,
	)


def backup_control_plane():
	"""Scheduler (daily): back up the control plane's own site through the local agent,
	uploading to the backup storage when one is configured (otherwise kept on the server)."""
	host = control_plane_host()
	if not host or not host.control_plane_site:
		return None
	from nexaploy import ops_utils
	from nexaploy.agent_job import create_agent_job
	from nexaploy.ops import get_settings

	try:
		upload = bool(ops_utils.backup_configured(get_settings().as_dict()))
	except Exception:
		upload = False
	return create_agent_job(
		host.name,
		"site.backup",
		{"site": host.control_plane_site, "with_files": True, "upload": upload},
	).name
