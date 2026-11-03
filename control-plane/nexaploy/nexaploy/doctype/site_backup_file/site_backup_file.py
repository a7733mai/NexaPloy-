# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class SiteBackupFile(Document):
	"""One file of a Site Backup (database / public / private / site_config) and its object
	key in the backup bucket (used for presigned downloads, nexaploy.api.site_tools)."""
