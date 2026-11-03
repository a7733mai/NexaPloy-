"""Row-level permissions: a "Nexaploy Customer" only sees documents of its own team.

`team` is a Link to User (the customer account). System Managers see everything.
"""

import frappe

CUSTOMER_ROLE = "Nexaploy Customer"


def _is_restricted(user=None):
	user = user or frappe.session.user
	if user == "Administrator":
		return False
	roles = frappe.get_roles(user)
	return CUSTOMER_ROLE in roles and "System Manager" not in roles


def team_query(user=None, doctype=None):
	user = user or frappe.session.user
	if not _is_restricted(user):
		return ""
	table = f"`tab{doctype}`" if doctype else None
	col = f"{table}.`team`" if table else "`team`"
	return f"{col} = {frappe.db.escape(user)}"


def server_query(user=None):
	return team_query(user, "Server")


def deploy_candidate_query(user=None):
	user = user or frappe.session.user
	if not _is_restricted(user):
		return ""
	return (
		"`tabDeploy Candidate`.`release_group` in "
		f"(select name from `tabRelease Group` where team = {frappe.db.escape(user)})"
	)


def agent_job_query(user=None):
	user = user or frappe.session.user
	if not _is_restricted(user):
		return ""
	return (
		"`tabAgent Job`.`server` in "
		f"(select name from `tabServer` where team = {frappe.db.escape(user)})"
	)


def has_team_permission(doc, ptype=None, user=None):
	user = user or frappe.session.user
	if not _is_restricted(user):
		return True
	if doc.is_new() and ptype == "create":
		return True
	return doc.get("team") == user
