# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy import backup_destination_utils as bdu


class BackupDestination(Document):
	def before_insert(self):
		if not self.team and "Nexaploy Customer" in frappe.get_roles():
			from nexaploy.teams import get_current_team

			self.team = get_current_team()

	def validate(self):
		self.title = (self.title or "").strip() or self.destination_type
		if self.destination_type not in bdu.DESTINATION_TYPES:
			frappe.throw(_("Unknown backup destination type {0}").format(self.destination_type))
		try:
			self.folder = bdu.normalize_folder(self.folder)
			if self.destination_type == bdu.S3:
				self.s3_provider = (self.s3_provider or "Other").strip()
				bdu.validate_s3(self.as_dict())
			if self.root_folder_id and not bdu.DRIVE_FOLDER_ID_RE.match(self.root_folder_id):
				raise bdu.DestinationError("Invalid Google Drive folder id")
		except bdu.DestinationError as e:
			frappe.throw(_(str(e)))
		if not self.is_new() and self.has_value_changed("destination_type"):
			# another provider: the old connection means nothing any more
			self.oauth_token = None
			self.account_email = self.account_name = self.drive_id = self.drive_type = None
			self.status = bdu.NOT_CONNECTED
		if self.destination_type == bdu.S3:
			# a Password field holds the new value, or asterisks when a stored one is unchanged
			has_secret = bool(self.s3_secret_key)
			if self.status != bdu.ERROR or self.has_value_changed("s3_secret_key"):
				self.status = bdu.CONNECTED if (self.s3_bucket and self.s3_access_key and has_secret) else bdu.NOT_CONNECTED

	def on_update(self):
		if self.is_default:
			# one default per team
			for other in frappe.get_all(
				"Backup Destination", filters={"team": self.team, "is_default": 1, "name": ["!=", self.name]}, pluck="name"
			):
				frappe.db.set_value("Backup Destination", other, "is_default", 0)
		from nexaploy.ops import push_destination_if_changed

		push_destination_if_changed(self)

	def on_trash(self):
		if frappe.db.exists("Server", {"backup_destination": self.name, "status": ["!=", "Archived"]}):
			frappe.throw(_("Servers still use this backup destination; choose another one for them first"))
