# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy import brand_utils as bu
from nexaploy.team_utils import is_valid_slug


class Brand(Document):
	"""White-label brand (nexaploy.branding): portal look by request host and the sender of the
	e-mails of teams whose `reseller_brand` is this brand. Team-owned (reseller) or platform-owned
	(no team). Portal domains still need a route to the control plane (see `portal_domains`)."""

	def validate(self):
		self.brand_name = (self.brand_name or "").strip().lower()
		if not is_valid_slug(self.brand_name):
			frappe.throw(_("Brand ID must be lowercase letters, digits and '-'"))
		self.title = (self.title or "").strip()
		if not self.title or len(self.title) > 80:
			frappe.throw(_("Brand title is required (at most 80 characters)"))
		try:
			self.primary_color = bu.normalize_color(self.primary_color or bu.DEFAULT_PRIMARY)
			domains = bu.parse_domains(self.portal_domains)
			self.email_sender_name = bu.normalize_sender_name(self.email_sender_name) or None
		except ValueError as exc:
			frappe.throw(_(str(exc)))
		others = frappe.get_all("Brand", filters={"name": ["!=", self.name or ""]}, fields=["name", "portal_domains"])
		clash = bu.domain_conflicts(self.name, domains, others)
		if clash:
			frappe.throw(_("Portal domain {0} already belongs to another brand").format(", ".join(clash)))
		self.portal_domains = "\n".join(domains)
		for field in ("logo", "favicon"):
			if self.get(field) and not bu.safe_url(self.get(field)):
				frappe.throw(_("{0} must be an uploaded file or an https URL").format(_(self.meta.get_label(field))))
		if self.team and self.is_default:
			frappe.throw(_("Only a platform brand (without an owner team) can be the platform default"))
		if self.is_default and self.enabled:
			for other in frappe.get_all("Brand", filters={"is_default": 1, "name": ["!=", self.name or ""]}, pluck="name"):
				frappe.db.set_value("Brand", other, "is_default", 0, update_modified=False)

	def on_update(self):
		from nexaploy.branding import clear_brand_cache

		clear_brand_cache()

	def on_trash(self):
		from nexaploy.branding import clear_brand_cache

		clear_brand_cache()
