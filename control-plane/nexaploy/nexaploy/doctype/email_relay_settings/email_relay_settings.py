# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy import email_utils as eu


class EmailRelaySettings(Document):
	"""Platform SMTP relay for "Managed e-mail" (nexaploy.email_relay). Saving a change of the
	relay fields (incl. the password) re-pushes the Email Account to every enabled site."""

	def validate(self):
		try:
			clean = eu.validate_settings(self.as_dict())
		except ValueError as exc:
			frappe.throw(_(str(exc)))
		for key in ("provider", "smtp_host", "smtp_port", "tls_mode", "default_from_domain", "spf_include",
		            "dkim_selector", "dmarc_policy", "daily_send_limit"):
			self.set(key, clean[key])
		if self.username and not (self.password or self.get_password("password", raise_exception=False)):
			frappe.throw(_("Set the SMTP password for {0}").format(self.username))

	def on_update(self):
		from nexaploy.email_relay import reconfigure_all, settings_fingerprint

		fingerprint = settings_fingerprint(self)
		if fingerprint == (self.relay_config_hash or ""):
			return
		self.db_set("relay_config_hash", fingerprint, update_modified=False)
		frappe.clear_document_cache(self.doctype, self.name)
		count = reconfigure_all()
		if count:
			frappe.msgprint(_("E-mail relay configuration queued for {0} site(s)").format(count), alert=True)
