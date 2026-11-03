# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class SiteTransfer(Document):
	"""Request to hand a Site over to another Team. Only the sha256 of the token is stored;
	create them with nexaploy.api.site_lifecycle.request_transfer (which emails the link
	/dashboard/transfer?token=...). Customers only reach these rows through that API."""

	def validate(self):
		if not self.token_hash:
			frappe.throw(_("Use nexaploy.api.site_lifecycle.request_transfer to create site transfers"))
		if not self.to_team and not self.to_email:
			frappe.throw(_("A transfer needs a target team or email"))
		if self.to_team and self.to_team == self.from_team and self.status == "Pending":
			frappe.throw(_("The site already belongs to team {0}").format(self.to_team))
