# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy.billing.pricing import SUBSCRIBABLE


class NexaploySubscription(Document):
	def validate(self):
		if self.document_type not in SUBSCRIBABLE:
			frappe.throw(_("Subscriptions are for {0} only").format(", ".join(SUBSCRIBABLE)))
		if self.document_type == "Marketplace App" and not self.site:
			frappe.throw(_("A Marketplace App subscription needs a site"))
		if (self.price_monthly or 0) < 0:
			frappe.throw(_("Price cannot be negative"))
		if self.end_date and self.start_date and str(self.end_date) < str(self.start_date):
			frappe.throw(_("End date must not be before start date"))
		if self.status == "Cancelled" and not self.end_date:
			self.end_date = frappe.utils.today()
