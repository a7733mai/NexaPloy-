"""Agent upgrades: desk and portal API (logic in nexaploy/agent_upgrades.py).

    nexaploy.api.agent_releases.server_agent_status(server)
    nexaploy.api.agent_releases.upgrade_agent(server, version=None)
    nexaploy.api.agent_releases.rollback_agent(server)
    nexaploy.api.agent_releases.bulk_upgrade_agents(channel=None, dry_run=0)   platform admins
    nexaploy.api.agent_releases.list_releases()                                 platform admins

Platform admins can upgrade any server; customers only their own BYOS servers, and only when
Nexaploy Settings > "Allow customers to upgrade the agent on their BYOS servers" is on.
"""

import frappe
from frappe import _
from frappe.utils import cint

from nexaploy import agent_upgrades


def _check_read(server):
	from nexaploy.teams import check_team_access, is_platform_admin

	if is_platform_admin():
		return
	team = frappe.db.get_value("Server", server, "team")
	if not team:
		raise frappe.PermissionError(_("Not permitted"))
	check_team_access(team, "read")


@frappe.whitelist()
def server_agent_status(server):
	_check_read(server)
	status = agent_upgrades.server_status(server)
	try:
		agent_upgrades.check_upgrade_permission(agent_upgrades._server(server))
		status["can_upgrade"] = True
	except frappe.PermissionError:
		status["can_upgrade"] = False
	return status


@frappe.whitelist(methods=["POST"])
def upgrade_agent(server, version=None):
	_check_read(server)
	return {"job": agent_upgrades.start_upgrade(server, version or None)}


@frappe.whitelist(methods=["POST"])
def rollback_agent(server):
	_check_read(server)
	return {"job": agent_upgrades.start_rollback(server)}


@frappe.whitelist(methods=["POST"])
def bulk_upgrade_agents(channel=None, dry_run=0):
	return {"servers": agent_upgrades.bulk_upgrade(channel or None, dry_run=cint(dry_run))}


@frappe.whitelist()
def list_releases():
	from nexaploy.teams import is_platform_admin

	if not is_platform_admin():
		raise frappe.PermissionError(_("Not permitted"))
	return frappe.get_all(
		"Agent Release",
		fields=["name", "version", "channel", "git_ref", "published", "released_on"],
		order_by="creation desc",
	)
