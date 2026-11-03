# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class BillingSettings(Document):
	def validate(self):
		if not 1 <= (self.invoice_day or 1) <= 31:
			frappe.throw(_("Invoice Day must be between 1 and 31"))
		for field in ("grace_days", "suspend_after_days", "tax_percent"):
			if (self.get(field) or 0) < 0:
				frappe.throw(_("{0} cannot be negative").format(self.meta.get_label(field)))
		if self.enable_paymob and (self.paymob_exchange_rate or 0) <= 0:
			frappe.throw(_("Paymob exchange rate must be positive"))
