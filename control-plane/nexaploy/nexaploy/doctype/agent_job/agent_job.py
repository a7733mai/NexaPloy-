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


# Composite indexes (Frappe calls this on every migrate; add_index is idempotent):
# - poll / heartbeat look up Pending jobs of one server every few seconds per server; without
#   it each call scanned the whole table (found in the load test, e2e/loadtest).
# - callbacks and the portal find a document's jobs by (reference_doctype, reference_name).
AGENT_JOB_INDEXES = (
	("server", "status"),
	("reference_doctype", "reference_name"),
	("status", "modified"),  # retention pruning (nexaploy.tasks.prune_agent_jobs)
)


def on_doctype_update():
	for columns in AGENT_JOB_INDEXES:
		frappe.db.add_index("Agent Job", list(columns))
