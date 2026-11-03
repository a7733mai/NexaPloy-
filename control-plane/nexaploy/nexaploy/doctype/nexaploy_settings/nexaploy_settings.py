# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy import ops_utils as ou


class NexaploySettings(Document):
	def validate(self):
		try:
			self.registry_url = ou.normalize_registry_url(self.registry_url)
		except ValueError as e:
			frappe.throw(str(e))
		if self.registry_url and self.registry_username and not self.get_password(
			"registry_password", raise_exception=False
		):
			frappe.throw(_("Set the registry password (or token) for {0}").format(self.registry_username))
		if self.root_domain:
			self.root_domain = self.root_domain.strip().strip(".").lower()
		if self.default_build_server and frappe.db.get_value(
			"Server", self.default_build_server, "status"
		) == "Archived":
			frappe.throw(_("Build server {0} is archived").format(self.default_build_server))
		if self.backup_bucket and not self.backup_access_key:
			frappe.throw(_("Backup storage needs an access key"))

	def on_update(self):
		"""Push backup storage to every server when the backup fields (incl. secret) change."""
		from nexaploy.ops import backup_settings_values, configure_backups_everywhere

		values = backup_settings_values(self)
		fingerprint = ou.config_fingerprint(values) if ou.backup_configured(values) else ""
		if fingerprint == (self.backup_config_hash or ""):
			return
		self.db_set("backup_config_hash", fingerprint, update_modified=False)
		if fingerprint:
			configure_backups_everywhere()
			frappe.msgprint(_("Backup storage configuration queued for all active servers"), alert=True)
