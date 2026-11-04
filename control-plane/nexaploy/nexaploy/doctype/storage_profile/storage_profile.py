# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy import storage_utils as su


class StorageProfile(Document):
	"""An S3-compatible bucket (ARCHITECTURE.md §11). Secrets reach agents only at poll time."""

	def validate(self):
		self.profile_name = (self.profile_name or "").strip().lower()
		if not self.is_new() and self.has_value_changed("profile_name"):
			frappe.throw(_("A storage profile cannot be renamed"))
		for field in ("endpoint", "region", "bucket", "path_prefix", "access_key"):
			if self.get(field):
				self.set(field, self.get(field).strip())
		if self.path_prefix:
			self.path_prefix = self.path_prefix.strip("/")
		has_secret = bool(self.get_password("secret_key", raise_exception=False)) if not self.is_new() else bool(
			self.secret_key
		)
		if self.secret_key and set(self.secret_key) != {"*"}:
			has_secret = True
		errors = su.validate_profile({**self.as_dict(), "has_secret": has_secret})
		if errors:
			frappe.throw("<br>".join(_(e) for e in errors))

	def on_update(self):
		"""Bucket or credentials changed: re-mount every site using the profile; if it holds the
		platform's database backups, push it to Nexaploy Settings too."""
		from nexaploy import storage

		values = {f: self.get(f) for f in su.profile_fingerprint_fields()}
		values["secret"] = self.get_password("secret_key", raise_exception=False)
		fingerprint = su_fingerprint(values)
		if fingerprint == (self.config_hash or ""):
			return
		first = not self.config_hash
		self.db_set("config_hash", fingerprint, update_modified=False)
		if first:
			return
		storage.reconfigure_profile_users(self.name)
		if frappe.db.get_single_value(storage.SETTINGS, "backup_storage_profile") == self.name:
			storage.apply_backup_profile(self.name)

	def on_trash(self):
		from nexaploy import storage

		used = frappe.get_all(
			storage.POLICY,
			or_filters=[["primary_profile", "=", self.name], ["replica_profile", "=", self.name],
			            ["trash_profile", "=", self.name]],
			pluck="name", limit=5,
		)
		if used:
			frappe.throw(_("Storage profile {0} is used by {1}").format(self.name, ", ".join(used)))
		settings = frappe.get_single(storage.SETTINGS)
		for field in ("default_primary_profile", "default_replica_profile", "default_trash_profile",
		              "backup_storage_profile"):
			if settings.get(field) == self.name:
				frappe.throw(_("Storage profile {0} is set in Storage Settings").format(self.name))


def su_fingerprint(values):
	from nexaploy.ops_utils import config_fingerprint

	return config_fingerprint(values)
