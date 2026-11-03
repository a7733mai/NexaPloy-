# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class TeamActivity(Document):
	"""Audit log row (ARCHITECTURE.md §10). Written only through
	nexaploy.activity.log_activity; never edited or deleted from the UI."""

	def validate(self):
		if not self.is_new():
			frappe.throw(_("Team Activity is read-only"))

	def on_trash(self):
		if not frappe.flags.in_test and not frappe.flags.nexaploy_prune_activity:
			frappe.throw(_("Team Activity cannot be deleted"))
