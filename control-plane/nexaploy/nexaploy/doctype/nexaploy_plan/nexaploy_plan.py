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
