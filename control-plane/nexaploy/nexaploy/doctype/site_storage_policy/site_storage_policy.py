# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy import storage_utils as su


class SiteStoragePolicy(Document):
	"""Where a site's attachments live (ARCHITECTURE.md §11). Changes go through
	nexaploy.api.storage (which enqueues the agent jobs); this only validates."""

	def validate(self):
		if not self.team and self.site:
			self.team = frappe.db.get_value("Site", self.site, "team")
		settings = frappe.get_cached_doc("Storage Settings")
		errors = su.validate_policy(
			self.attachments_mode, self.primary_profile, self.replica_profile, self.trash_profile,
			self.trash_retention_days, settings.max_trash_retention_days or 365,
		)
		if self.flags.skip_validate_profiles and self.attachments_mode == "Local":
			errors = []
		for field in ("primary_profile", "replica_profile", "trash_profile"):
			name = self.get(field)
			if not name or (self.flags.skip_validate_profiles and self.attachments_mode == "Local"):
				continue
			row = frappe.db.get_value("Storage Profile", name, ["name", "enabled", "team"], as_dict=True)
			if not su.profile_usable(row, self.team):
				errors.append(_("Storage profile {0} is not available").format(name))
		if errors:
			frappe.throw("<br>".join(_(e) for e in errors))
