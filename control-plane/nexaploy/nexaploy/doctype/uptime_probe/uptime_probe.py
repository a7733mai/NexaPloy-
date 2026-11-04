# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy import monitor_utils as mu


class UptimeProbe(Document):
	"""A place uptime checks run from (ARCHITECTURE.md §16). The built-in probe
	(`control-plane`) is the control plane's own scheduler; every other probe is an external
	`nexaploy-probe` worker authenticated by a token stored as sha256 only. A new external probe
	gets a token on insert, shown once."""

	def validate(self):
		try:
			self.probe_name = mu.validate_probe_name(self.probe_name)
		except mu.MonitorError as exc:
			frappe.throw(_(str(exc)))
		if not self.title:
			self.title = self.probe_name
		if self.is_new():
			self.is_builtin = 1 if self.probe_name == mu.BUILTIN_PROBE else 0

	def after_insert(self):
		if self.is_builtin or self.token_hash:
			return
		from nexaploy.api.monitoring_probe import set_new_token

		token = set_new_token(self.name)
		frappe.msgprint(
			_("Probe token (shown only once): {0}").format(f"<code>{frappe.utils.escape_html(token)}</code>"),
			title=_("Uptime probe created"),
		)

	def on_trash(self):
		if self.is_builtin:
			frappe.throw(_("The built-in probe cannot be deleted; disable it instead"))
