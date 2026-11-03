# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy.utils import SUPPORTED_MAJORS, is_valid_app_name


class MarketplaceApp(Document):
	def validate(self):
		if not is_valid_app_name(self.app_name):
			frappe.throw(_("App name must be a python package name, e.g. erpnext"))
		if self.pricing == "Paid" and not self.price_monthly:
			frappe.throw(_("Paid apps need a monthly price"))
		seen = set()
		for rel in self.releases:
			if str(rel.frappe_major) not in SUPPORTED_MAJORS:
				frappe.throw(_("Row {0}: unsupported Frappe major {1}").format(rel.idx, rel.frappe_major))
			if rel.frappe_major in seen:
				frappe.throw(_("Row {0}: duplicate release for Frappe {1}").format(rel.idx, rel.frappe_major))
			seen.add(rel.frappe_major)

	def as_compat_dict(self, branch=None):
		"""Shape consumed by nexaploy.utils.check_compatibility / build_apps_json."""
		return {
			"app_name": self.app_name,
			"approved": bool(self.approved),
			"repo_url": self.repo_url,
			"branch": branch,
			"releases": [
				{"frappe_major": r.frappe_major, "branch": r.branch, "commit": r.commit} for r in self.releases
			],
		}
