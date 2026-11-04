# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy import monitor_utils as mu


class StatusPage(Document):
	"""Public status page of a team (one per team): /status/<slug>, or `status_domain` once the
	operator routes that host to the control plane (ARCHITECTURE.md §16). Only monitors of the
	same team can be listed; they are shown under their public name, never their URL."""

	def validate(self):
		try:
			self.slug = mu.validate_slug(self.slug)
			self.status_domain = mu.validate_domain(self.status_domain)
		except mu.MonitorError as exc:
			frappe.throw(_(str(exc)))
		if self.status_domain and frappe.db.exists(
			"Status Page", {"status_domain": self.status_domain, "name": ["!=", self.name or ""]}
		):
			frappe.throw(_("This domain is already used by another status page"))
		for row in self.monitors:
			if frappe.db.get_value("Uptime Monitor", row.monitor, "team") != self.team:
				frappe.throw(_("Only monitors of the same team can be shown"))

	def on_update(self):
		from nexaploy.monitoring import clear_status_cache

		clear_status_cache(self.name)

	def on_trash(self):
		from nexaploy.monitoring import clear_status_cache

		clear_status_cache(self.name)
