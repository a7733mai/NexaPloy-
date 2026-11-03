# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ServerMetric(Document):
	"""One heartbeat sample (at most every 5 minutes per server, pruned after 30 days)."""
