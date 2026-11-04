# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
import frappe.utils
from frappe import _
from frappe.model.document import Document

from nexaploy import marketplace_utils as mu
from nexaploy import publisher_utils as pu
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
			if not rel.status:
				rel.status = mu.VERIFIED
			if rel.status == mu.VERIFIED and not rel.verified_on:
				rel.verified_on = frappe.utils.now_datetime()
		self.validate_required_apps()
		self.validate_listing()

	def validate_required_apps(self):
		try:
			apps = mu.parse_required_apps(self.required_apps, self.app_name)
		except mu.RequirementError as exc:
			frappe.throw(_(str(exc)))
		self.required_apps = mu.format_required_apps(apps) or None
		unknown = [a for a in apps if not frappe.db.exists("Marketplace App", a)]
		if unknown:
			frappe.msgprint(
				_("Required apps {0} are not in the marketplace yet; bench groups cannot add {1} until they are.").format(
					", ".join(unknown), self.app_name
				),
				indicator="orange",
				alert=True,
			)
		if apps:
			from nexaploy.api.marketplace import load_catalog

			catalog = load_catalog()
			catalog[self.app_name] = {"app_name": self.app_name, "required_apps": apps}
			try:
				mu.dependency_order([self.app_name], catalog)
			except mu.RequirementError as exc:
				frappe.throw(_(str(exc)))

	def required_app_list(self):
		try:
			return mu.parse_required_apps(self.required_apps, self.app_name)
		except mu.RequirementError:
			return []

	def validate_listing(self):
		"""Publisher listing fields (see nexaploy.publisher_utils): approved <=> Published,
		publisher display name, media URLs."""
		before = None if self.is_new() else self.get_doc_before_save()
		self.status, approved = pu.sync_approval(
			self.status,
			self.approved,
			before.status if before else None,
			before.approved if before else None,
			is_new=before is None,
		)
		self.approved = 1 if approved else 0
		if self.publisher_profile:
			title = frappe.db.get_value("Marketplace Publisher", self.publisher_profile, "title")
			if title:
				self.publisher = title
		try:
			self.screenshots = "\n".join(pu.parse_screenshots(self.screenshots)) or None
		except pu.ListingError as exc:
			frappe.throw(_(str(exc)))
		if self.documentation_url and not pu.is_valid_url(self.documentation_url):
			frappe.throw(_("Invalid documentation URL"))
		if self.status == pu.REJECTED and not (self.review_notes or "").strip():
			frappe.throw(_("Explain why the listing is rejected in the review notes"))

	def as_compat_dict(self, branch=None, include_unverified=False):
		"""Shape consumed by nexaploy.utils.check_compatibility / build_apps_json.

		Only Verified releases count, unless `include_unverified` (an app that is already in a
		Release Group keeps working while a new release of it is being verified)."""
		return {
			"app_name": self.app_name,
			"approved": bool(self.approved),
			"repo_url": self.repo_url,
			"branch": branch,
			"required_apps": self.required_app_list(),
			"releases": [
				{"frappe_major": r.frappe_major, "branch": r.branch, "commit": r.commit, "status": r.status}
				for r in self.releases
				if include_unverified or mu.release_visible({"status": r.status})
			],
		}
