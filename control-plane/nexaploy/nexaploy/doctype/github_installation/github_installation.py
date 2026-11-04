# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class GitHubInstallation(Document):
	def validate(self):
		self.installation_id = str(self.installation_id or "").strip()
		if not self.installation_id.isdigit():
			frappe.throw(_("Installation ID must be numeric"))

	def on_trash(self):
		if frappe.db.exists("Custom App", {"installation": self.name, "status": "Active"}):
			frappe.throw(_("Custom apps still use installation {0}; disable them first").format(self.name))
