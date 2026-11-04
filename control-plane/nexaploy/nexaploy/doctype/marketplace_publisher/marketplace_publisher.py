# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy import publisher_utils as pu


class MarketplacePublisher(Document):
	"""A team's marketplace publisher profile (one per team). Team admins edit it from
	/dashboard/publisher (nexaploy.api.publisher); `verified` and `revenue_share_percent`
	are set by platform staff in the desk."""

	def validate(self):
		try:
			self.revenue_share_percent = float(pu.validate_share_percent(self.revenue_share_percent))
			values = pu.clean_publisher_values(
				{
					"title": self.title,
					"website": self.website,
					"support_email": self.support_email,
					"payout_method": self.payout_method,
					"payout_details": self.payout_details,
				}
			)
		except pu.ListingError as exc:
			frappe.throw(_(str(exc)))
		self.update(values)
		dup = frappe.db.get_value("Marketplace Publisher", {"team": self.team, "name": ["!=", self.name]}, "name")
		if dup:
			frappe.throw(_("Team {0} already has a publisher profile").format(self.team))

	def on_update(self):
		before = self.get_doc_before_save()
		if before and before.title != self.title:
			for app in frappe.get_all("Marketplace App", filters={"publisher_profile": self.name}, pluck="name"):
				frappe.db.set_value("Marketplace App", app, "publisher", self.title, update_modified=False)

	def on_trash(self):
		if frappe.db.exists("Marketplace App", {"publisher_profile": self.name}):
			frappe.throw(_("Remove or reassign the publisher's listings first"))
		if frappe.db.exists("Publisher Payout", {"publisher": self.name}):
			frappe.throw(_("A publisher with payouts cannot be deleted"))
