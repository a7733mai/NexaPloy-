# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from nexaploy.agent_job import create_agent_job


class SiteBackup(Document):
	def before_insert(self):
		self.status = "Pending"
		self.team = frappe.db.get_value("Site", self.site, "team")

	def after_insert(self):
		"""Enqueue site.backup {site, with_files, upload} on the site's server."""
		server = frappe.db.get_value("Site", self.site, "server")
		create_agent_job(
			server,
			"site.backup",
			{"site": self.site, "with_files": bool(self.with_files), "upload": True},
			reference_doctype=self.doctype,
			reference_name=self.name,
		)
