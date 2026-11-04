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
		from nexaploy import backup_destination_utils as bdu

		try:
			self.microsoft_tenant = bdu.normalize_tenant(self.microsoft_tenant)
		except bdu.DestinationError as e:
			frappe.throw(_(str(e)))
		for field in ("backup_retention_min_days", "backup_retention_max_days", "backup_retention_included_days",
		              "backup_local_retention_max_days", "backup_keep_monthly_max"):
			if (self.get(field) or 0) < 0:
				frappe.throw(_("Backup retention limits must be 0 or more"))

	def on_update(self):
		"""Push backup storage to every server when the backup fields (incl. secret) change."""
		from nexaploy.ops import backup_settings_values, configure_backups_everywhere

		self.push_oauth_clients_if_changed()
		values = backup_settings_values(self)
		fingerprint = ou.config_fingerprint(values) if ou.backup_configured(values) else ""
		if fingerprint == (self.backup_config_hash or ""):
			return
		self.db_set("backup_config_hash", fingerprint, update_modified=False)
		if fingerprint:
			configure_backups_everywhere()
			frappe.msgprint(_("Backup storage configuration queued for all active servers"), alert=True)

	def push_oauth_clients_if_changed(self):
		"""rclone refreshes OAuth tokens with the platform's client id/secret: when those change,
		re-push the connected Google Drive / OneDrive / Dropbox destinations."""
		from nexaploy import backup_destination_utils as bdu

		values = {}
		for id_field, secret_field in bdu.SETTINGS_CLIENT_FIELDS.values():
			values[id_field] = self.get(id_field)
			values[secret_field] = self.get_password(secret_field, raise_exception=False)
		values["microsoft_tenant"] = self.microsoft_tenant
		fingerprint = ou.config_fingerprint(values)
		if fingerprint == (self.oauth_config_hash or ""):
			return
		self.db_set("oauth_config_hash", fingerprint, update_modified=False)
		from nexaploy.ops import push_destination_if_changed

		for name in frappe.get_all(
			"Backup Destination",
			filters={"destination_type": ["in", list(bdu.OAUTH_DESTINATION_TYPES)], "status": bdu.CONNECTED},
			pluck="name",
		):
			try:
				push_destination_if_changed(frappe.get_doc("Backup Destination", name))
			except Exception:
				frappe.log_error(title=f"Nexaploy: re-push of backup destination {name} failed")
