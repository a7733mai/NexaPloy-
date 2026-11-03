"""Row-level permissions: users only see documents of the teams they are members of.

`team` is a Link to Team (ARCHITECTURE.md §9). Membership lives in `tabTeam Member`
(child of Team); roles map to actions with nexaploy.team_utils.role_allows. Platform
admins (System Manager / Administrator) see everything. The function names are the
hooks.py targets (permission_query_conditions / has_permission).
"""

import frappe

from nexaploy.team_utils import required_action, role_allows
from nexaploy.teams import get_team_role, is_platform_admin

CUSTOMER_ROLE = "Nexaploy Customer"


def _is_restricted(user=None):
	return not is_platform_admin(user or frappe.session.user)


def _member_teams_sql(user):
	"""Subquery: names of the enabled teams `user` is a member of."""
	return (
		"(select `tabTeam Member`.`parent` from `tabTeam Member` "
		"inner join `tabTeam` on `tabTeam`.`name` = `tabTeam Member`.`parent` "
		"where `tabTeam Member`.`parenttype` = 'Team' "
		f"and `tabTeam Member`.`user` = {frappe.db.escape(user)} "
		"and `tabTeam`.`enabled` = 1)"
	)


def team_query(user=None, doctype=None):
	"""`team` in the user's teams (for every DocType with a `team` field)."""
	user = user or frappe.session.user
	if not _is_restricted(user):
		return ""
	col = f"`tab{doctype}`.`team`" if doctype else "`team`"
	return f"{col} in {_member_teams_sql(user)}"


def server_query(user=None):
	return team_query(user, "Server")


def team_doc_query(user=None):
	"""Team list: only teams the user belongs to."""
	user = user or frappe.session.user
	if not _is_restricted(user):
		return ""
	return f"`tabTeam`.`name` in {_member_teams_sql(user)}"


def team_invitation_query(user=None):
	return team_query(user, "Team Invitation")


def deploy_candidate_query(user=None):
	user = user or frappe.session.user
	if not _is_restricted(user):
		return ""
	return (
		"`tabDeploy Candidate`.`release_group` in "
		f"(select name from `tabRelease Group` where team in {_member_teams_sql(user)})"
	)


def agent_job_query(user=None):
	"""Jobs on the user's servers, plus jobs referencing their sites (shared hosted benches)."""
	user = user or frappe.session.user
	if not _is_restricted(user):
		return ""
	teams = _member_teams_sql(user)
	return (
		f"(`tabAgent Job`.`server` in (select name from `tabServer` where team in {teams}) "
		"or (`tabAgent Job`.`reference_doctype` = 'Site' and `tabAgent Job`.`reference_name` in "
		f"(select name from `tabSite` where team in {teams})))"
	)


def _doc_team(doc):
	if doc.doctype == "Team":
		return doc.name
	return doc.get("team")


def has_team_permission(doc, ptype=None, user=None):
	"""Allow `ptype` on `doc` when the user's role in the document's team allows the
	mapped action (read/select/print/export -> read; write/create/submit/cancel/delete ->
	write; billing documents -> billing; servers, tokens, teams, invitations -> admin)."""
	user = user or frappe.session.user
	if not _is_restricted(user):
		return True
	team = _doc_team(doc)
	if doc.is_new() and ptype == "create" and not team:
		return True  # controllers fill `team` from the current team
	if not team:
		return False
	return role_allows(get_team_role(team, user), required_action(doc.doctype, ptype))


def has_team_doc_permission(doc, ptype=None, user=None):
	"""has_permission for Team itself (team = doc.name)."""
	return has_team_permission(doc, ptype, user)
