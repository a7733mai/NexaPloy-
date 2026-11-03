"""/dashboard/team - members, roles and invitations of the current team; ?invite=<token>
accepts an invitation (see nexaploy/api/teams.py)."""

import frappe
from frappe import _

from nexaploy.api.dashboard import prepare_page

no_cache = 1


def get_context(context):
	# a logged-in user without the portal role may open an invitation link: accepting it
	# grants the Nexaploy Customer role
	invite = bool(frappe.form_dict.get("invite"))
	return prepare_page(context, "team", _("Team"), require_role=not invite)
