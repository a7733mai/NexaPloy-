# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy import email_utils as eu


class SiteEmail(Document):
	"""Managed e-mail state of one site (nexaploy.email_relay; portal API nexaploy.api.site_email).
	Named after the site; `team` follows the site."""

	def validate(self):
		self.team = frappe.db.get_value("Site", self.site, "team")
		if self.sender_domain:
			try:
				domain = eu.normalize_domain(self.sender_domain)
			except ValueError as exc:
				frappe.throw(_(str(exc)))
			if domain != self.sender_domain:
				self.sender_domain = domain
		if self.has_value_changed("sender_domain") and not self.is_new():
			self.domain_verified = 0
			self.dns_check_result = None
		if (self.daily_limit or 0) < 0:
			frappe.throw(_("Daily send limit cannot be negative"))
