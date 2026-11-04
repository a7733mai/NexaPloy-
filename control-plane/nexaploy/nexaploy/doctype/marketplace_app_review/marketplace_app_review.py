# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy import marketplace_utils as mu


class MarketplaceAppReview(Document):
	"""One rating (1-5) + review per team and Marketplace App. Written through
	nexaploy.api.marketplace (save_review / reply_review / hide_review)."""

	def validate(self):
		try:
			self.rating = mu.clean_rating(self.rating)
			self.review = mu.clean_review_text(self.review) or None
			self.publisher_reply = mu.clean_review_text(self.publisher_reply) or None
		except mu.RequirementError as exc:
			frappe.throw(_(str(exc)))
		dup = frappe.db.get_value(
			"Marketplace App Review", {"app": self.app, "team": self.team, "name": ["!=", self.name]}, "name"
		)
		if dup:
			frappe.throw(_("Team {0} already reviewed {1}").format(self.team, self.app))
