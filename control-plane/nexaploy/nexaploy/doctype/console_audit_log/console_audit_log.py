# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

# set by the control plane when a job finishes (status / duration / error); nothing else changes
MUTABLE_FIELDS = ("status", "duration", "error", "agent_job")


class ConsoleAuditLog(Document):
	"""One SQL / console / logs execution (nexaploy.console_access). Written by the control
	plane only; customers (team Owners/Admins) can read it but never edit or delete it."""

	def validate(self):
		if self.is_new():
			return
		if not self.flags.nexaploy_audit_update:
			frappe.throw(_("Console Audit Log is read-only"))
		before = self.get_doc_before_save()
		if before is None:
			return
		for field in self.meta.get_valid_columns():
			if field in MUTABLE_FIELDS or field in ("modified", "modified_by"):
				continue
			if self.get(field) != before.get(field):
				frappe.throw(_("Console Audit Log is read-only"))

	def on_trash(self):
		if not frappe.flags.in_test and not frappe.flags.nexaploy_prune_console_audit:
			frappe.throw(_("Console Audit Log cannot be deleted"))
