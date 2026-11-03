# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy.billing.pricing import compute_totals, money


class NexaployInvoice(Document):
	def validate(self):
		if self.period_start and self.period_end and str(self.period_end) < str(self.period_start):
			frappe.throw(_("Period end must not be before period start"))
		for row in self.items:
			if row.amount is None or (not row.amount and row.unit_price):
				row.amount = float(money(row.unit_price))
		totals = compute_totals([{"amount": row.amount} for row in self.items], self.tax_percent or 0)
		self.subtotal = float(totals["subtotal"])
		self.tax = float(totals["tax"])
		self.total = float(totals["total"])

	@frappe.whitelist()
	def mark_paid_manually(self, reference=None):
		"""Desk button: record an offline payment (bank transfer...)."""
		frappe.only_for("System Manager")
		from nexaploy.billing.invoicing import mark_paid

		frappe.get_doc(
			{
				"doctype": "Nexaploy Payment",
				"invoice": self.name,
				"team": self.team,
				"gateway": "Manual",
				"status": "Success",
				"amount": self.total,
				"currency": self.currency,
				"reference": reference,
			}
		).insert(ignore_permissions=True)
		mark_paid(self, "Manual", reference)

	@frappe.whitelist()
	def void(self):
		frappe.only_for("System Manager")
		if self.status == "Paid":
			frappe.throw(_("A paid invoice cannot be voided"))
		self.db_set("status", "Void")
