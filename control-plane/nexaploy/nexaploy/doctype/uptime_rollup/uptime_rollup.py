# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class UptimeRollup(Document):
	"""Hourly aggregate of an Uptime Monitor's check results (upserted on ingest, kept 90 days)."""
