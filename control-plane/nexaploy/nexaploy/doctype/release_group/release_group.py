# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy.utils import (
	build_apps_json,
	check_compatibility,
	frappe_branch_for,
	is_valid_release_group_name,
	release_for_major,
)


class ReleaseGroup(Document):
	def before_insert(self):
		if not self.team and "Nexaploy Customer" in frappe.get_roles():
			self.team = frappe.session.user

	def validate(self):
		if not is_valid_release_group_name(self.title):
			frappe.throw(
				_("Title must match ^[a-z][a-z0-9-]{2,30}$ (it is used in image tags), e.g. erp-v15-main")
			)
		self.frappe_branch = frappe_branch_for(self.frappe_major, self.frappe_branch)
		self.validate_apps()
		if self.build_server:
			status = frappe.db.get_value("Server", self.build_server, "status")
			if status == "Archived":
				frappe.throw(_("Build server {0} is archived").format(self.build_server))

	def _compat_apps(self):
		apps = []
		for row in self.apps:
			app = frappe.get_cached_doc("Marketplace App", row.app)
			apps.append(app.as_compat_dict(branch=row.branch))
		return apps

	def validate_apps(self):
		"""Every app must be approved and have a Marketplace App Release for frappe_major."""
		apps = self._compat_apps()
		errors = check_compatibility(self.frappe_major, apps)
		if errors:
			frappe.throw("<br>".join(errors), title=_("Incompatible apps"))
		# default branch from the release for this major
		for row, app in zip(self.apps, apps):
			if not row.branch:
				row.branch = release_for_major(app["releases"], self.frappe_major)["branch"]

	def get_apps_json(self):
		"""apps.json for image-builder (§4): [{url, branch}, ...] (frappe itself excluded)."""
		return build_apps_json(self._compat_apps(), self.frappe_major)

	@frappe.whitelist()
	def build_apps_json(self):
		self.check_permission("read")
		return self.get_apps_json()

	@frappe.whitelist()
	def create_deploy_candidate(self):
		"""Start an image build (Deploy Candidate -> image.build job on build_server)."""
		self.check_permission("write")
		dc = frappe.get_doc({"doctype": "Deploy Candidate", "release_group": self.name})
		dc.insert(ignore_permissions=True)
		return dc.name

	def latest_image(self):
		"""(deploy_candidate, image_tag) of the newest successful build, or (None, None)."""
		row = frappe.db.get_value(
			"Deploy Candidate",
			{"release_group": self.name, "status": "Success"},
			["name", "image_tag"],
			order_by="creation desc",
			as_dict=True,
		)
		return (row.name, row.image_tag) if row else (None, None)

	def app_names(self):
		return [row.app for row in self.apps]
