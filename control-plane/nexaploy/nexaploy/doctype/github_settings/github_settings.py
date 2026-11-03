# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class GitHubSettings(Document):
	def validate(self):
		if self.app_slug:
			self.app_slug = self.app_slug.strip().lower()
		if self.github_app_id:
			self.github_app_id = str(self.github_app_id).strip()
		key = self.private_key
		# Password fields hold "*****" after the first save; only check a newly typed key
		if key and set(key) != {"*"} and "PRIVATE KEY" not in key:
			frappe.throw(_("Private Key must be the PEM file contents (-----BEGIN RSA PRIVATE KEY-----)"))
