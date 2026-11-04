# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

DEFAULT_IMAGES = {"Hetzner": "ubuntu-24.04", "DigitalOcean": "ubuntu-24-04-x64"}


class CloudProvider(Document):
	def validate(self):
		if not self.default_image:
			self.default_image = DEFAULT_IMAGES.get(self.provider)

	@frappe.whitelist()
	def sync_catalog(self):
		"""Import the provider's regions and sizes as *disabled* Cloud Region / Cloud Server
		Size rows (existing rows are left alone). Enable the ones to sell and link a plan."""
		frappe.only_for("System Manager")
		from nexaploy.provisioning.jobs import provider_client
		from nexaploy.provisioning.utils import cloud_region_name

		client = provider_client(self.name)
		added = {"regions": 0, "sizes": 0}
		for r in client.list_regions():
			if not r.get("code") or frappe.db.exists("Cloud Region", cloud_region_name(self.name, r["code"])):
				continue
			frappe.get_doc(
				{
					"doctype": "Cloud Region",
					"provider": self.name,
					"code": r["code"],
					"title": r.get("title") or r["code"],
					"country": r.get("country"),
					"enabled": 0,
				}
			).insert(ignore_permissions=True)
			added["regions"] += 1
		for s in client.list_sizes():
			if not s.get("code") or frappe.db.exists("Cloud Server Size", cloud_region_name(self.name, s["code"])):
				continue
			frappe.get_doc(
				{
					"doctype": "Cloud Server Size",
					"provider": self.name,
					"code": s["code"],
					"title": s["code"],
					"vcpu": s.get("vcpu"),
					"memory_gb": s.get("memory_gb"),
					"disk_gb": s.get("disk_gb"),
					"monthly_cost": s.get("monthly_cost"),
					"enabled": 0,
				}
			).insert(ignore_permissions=True)
			added["sizes"] += 1
		frappe.msgprint(_("Imported {0} regions and {1} sizes (disabled)").format(added["regions"], added["sizes"]))
		return added
