# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class NexaployPlan(Document):
	def validate(self):
		if (self.price_monthly or 0) < 0:
			frappe.throw(_("Price cannot be negative"))
		if (self.max_sites or 0) < 0:
			frappe.throw(_("Max Sites cannot be negative"))
		for field in ("cpu_seconds_per_day", "max_storage_mb", "max_database_mb"):
			if (self.get(field) or 0) < 0:
				frappe.throw(_("{0} cannot be negative (0 = unlimited)").format(self.meta.get_label(field)))
		if self.overage_action and self.overage_action not in ("Notify", "Throttle", "Suspend"):
			frappe.throw(_("Overage Action must be Notify, Throttle or Suspend"))
