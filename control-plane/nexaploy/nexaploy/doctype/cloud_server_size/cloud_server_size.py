# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy.provisioning.utils import cloud_region_name


class CloudServerSize(Document):
	def autoname(self):
		self.name = cloud_region_name(self.provider, self.code)

	def validate(self):
		self.code = (self.code or "").strip()
		if not self.code:
			frappe.throw(_("Code is required"))
		if not self.title:
			self.title = self.code
		if self.plan and frappe.db.get_value("Nexaploy Plan", self.plan, "plan_type") != "Server Hosted":
			frappe.throw(_("Plan {0} is not a Server Hosted plan").format(self.plan))
