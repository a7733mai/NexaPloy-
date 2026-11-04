# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy.team_utils import normalize_email, normalize_role


class TeamInvitation(Document):
	"""Invitation to join a Team. Only the sha256 of the token is stored; create them with
	nexaploy.api.teams.invite_member (which emails the accept link)."""

	def validate(self):
		if not self.token_hash:
			frappe.throw(_("Use nexaploy.api.teams.invite_member to create invitations"))
		try:
			self.email = normalize_email(self.email)
			self.role = normalize_role(self.role)
		except ValueError as exc:
			frappe.throw(_(str(exc)))
