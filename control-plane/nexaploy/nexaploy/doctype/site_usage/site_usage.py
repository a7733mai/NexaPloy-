# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class SiteUsage(Document):
	"""Usage of one site on one UTC day, upserted from agent heartbeats (nexaploy.ops.ingest_usage)."""

	def before_insert(self):
		if not self.team:
			self.team = frappe.db.get_value("Site", self.site, "team")
		if not self.server:
			self.server = frappe.db.get_value("Site", self.site, "server")
