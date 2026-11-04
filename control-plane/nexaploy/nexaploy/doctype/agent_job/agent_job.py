# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from nexaploy.utils import redact_params


class AgentJob(Document):
	def get_redacted_params(self):
		return redact_params(frappe.parse_json(self.params or "{}"))

	@frappe.whitelist()
	def retry(self):
		"""Re-enqueue a failed job as a new Pending job with the same params/reference."""
		frappe.only_for("System Manager")
		from nexaploy.agent_job import create_agent_job

		return create_agent_job(
			self.server, self.kind, frappe.parse_json(self.params or "{}"),
			self.reference_doctype, self.reference_name,
		).name
