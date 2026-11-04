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
		# pitr: the dump carries its binlog position (point-in-time recovery, §14); the agent
		# skips it when binary logging is off on the server
		params = {"site": self.site, "with_files": bool(self.with_files), "upload": True, "pitr": True}
		if self.purpose != "Move":
			# a same-server move restores the local copy: keep it whatever the site's policy
			try:
				from nexaploy.ops import site_backup_policy

				params["local_retention_days"] = site_backup_policy(self.site)["local_retention_days"]
			except Exception:
				frappe.log_error(title=f"Nexaploy: backup policy of {self.site} unreadable; agent default used")
		create_agent_job(
			server,
			"site.backup",
			params,
			reference_doctype=self.doctype,
			reference_name=self.name,
		)
