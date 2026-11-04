# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy.portal_utils import parse_app_list
from nexaploy.signup_utils import normalize_slug


class SaaSProduct(Document):
	def validate(self):
		try:
			self.slug = normalize_slug(self.slug)
		except ValueError as exc:
			frappe.throw(_(str(exc)))
		if (self.trial_days or 0) < 0:
			frappe.throw(_("Trial days cannot be negative"))
		if self.default_plan and frappe.db.get_value("Nexaploy Plan", self.default_plan, "plan_type") != "Site":
			frappe.throw(_("Plan {0} is not a Site plan").format(self.default_plan))
		if self.release_group and frappe.db.get_value("Release Group", self.release_group, "team"):
			frappe.throw(_("Release group {0} belongs to a team; use a shared release group").format(self.release_group))
		apps = self.app_list()
		rows = frappe.get_all(
			"Release Group App",
			filters={"parent": self.release_group, "parenttype": "Release Group"},
			fields=["app", "app_name"],
		)
		allowed = {r.app_name or r.app for r in rows}
		missing = [a for a in apps if a not in allowed]
		if missing:
			frappe.throw(_("Apps {0} are not part of release group {1}").format(", ".join(missing), self.release_group))

	def app_list(self):
		try:
			return parse_app_list((self.apps or "").replace("\n", ","))
		except ValueError as exc:
			frappe.throw(_(str(exc)))
