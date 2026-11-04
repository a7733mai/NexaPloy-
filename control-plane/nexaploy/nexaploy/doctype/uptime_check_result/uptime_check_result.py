# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class UptimeCheckResult(Document):
	"""One raw check result of one probe (nexaploy.monitoring; pruned after 48 hours)."""
