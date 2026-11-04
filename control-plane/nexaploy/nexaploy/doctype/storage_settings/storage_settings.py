# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy import storage_utils as su


class StorageSettings(Document):
	"""Operator switches for offsite attachments, DR replica and trash (ARCHITECTURE.md §11)."""

	def validate(self):
		values = {k: self.get(k) for k in su.SETTINGS_FIELDS}
		try:
			su.clean_settings_update({k: v for k, v in values.items() if v not in (None, "")})
		except ValueError as e:
			frappe.throw(_(str(e)))
		for field in ("default_primary_profile", "default_replica_profile", "default_trash_profile",
		              "backup_storage_profile"):
			name = self.get(field)
			if name and frappe.db.get_value("Storage Profile", name, "team"):
				frappe.throw(_("{0} must be a platform profile (no team)").format(self.meta.get_label(field)))
		for key, field in su.ADDON_FIELDS.items():
			plan = self.get(field)
			if plan and frappe.db.get_value("Nexaploy Plan", plan, "plan_type") != "Add-on":
				frappe.throw(_("{0} must be an Add-on plan").format(self.meta.get_label(field)))

	def on_update(self):
		from nexaploy import storage

		if self.has_value_changed("backup_storage_profile") and self.backup_storage_profile:
			storage.apply_backup_profile(self.backup_storage_profile)
			frappe.msgprint(_("Database backups now go to {0}").format(self.backup_storage_profile), alert=True)
