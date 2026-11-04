# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy import marketplace_utils as mu
from nexaploy.github_utils import is_valid_branch
from nexaploy.ops_utils import parse_update_window
from nexaploy.utils import (
	CUSTOM_SOURCE,
	MARKETPLACE_SOURCE,
	build_group_apps_json,
	check_group_apps,
	frappe_branch_for,
	is_valid_release_group_name,
	release_for_major,
)


class ReleaseGroup(Document):
	def before_insert(self):
		if not self.team and "Nexaploy Customer" in frappe.get_roles():
			from nexaploy.teams import get_current_team, is_platform_admin

			if not is_platform_admin():
				self.team = get_current_team()

	def validate(self):
		if not is_valid_release_group_name(self.title):
			frappe.throw(
				_("Title must match ^[a-z][a-z0-9-]{2,30}$ (it is used in image tags), e.g. erp-v15-main")
			)
		self.frappe_branch = frappe_branch_for(self.frappe_major, self.frappe_branch)
		self.validate_apps()
		if self.update_window:
			try:
				parse_update_window(self.update_window)
			except ValueError as e:
				frappe.throw(str(e))
			self.update_window = self.update_window.replace(" ", "")
		if self.build_server:
			status = frappe.db.get_value("Server", self.build_server, "status")
			if status == "Archived":
				frappe.throw(_("Build server {0} is archived").format(self.build_server))

	def _app_rows(self):
		"""Pure row dicts (see nexaploy.utils.check_group_apps) for every Release Group App."""
		from nexaploy.nexaploy.doctype.custom_app.custom_app import custom_app_row

		rows = []
		existing = self._apps_before_save()
		for row in self.apps:
			if row.source_type == CUSTOM_SOURCE:
				if not row.custom_app or not frappe.db.exists("Custom App", row.custom_app):
					rows.append({"source_type": CUSTOM_SOURCE, "custom_app": row.custom_app, "app_name": row.app_name})
					continue
				app = frappe.get_cached_doc("Custom App", row.custom_app)
				rows.append(custom_app_row(app.as_dict(), row.branch))
			else:
				row.source_type = MARKETPLACE_SOURCE
				app = frappe.get_cached_doc("Marketplace App", row.app)
				# apps already in the group keep working while a new release is being verified;
				# newly added apps need a Verified release
				item = app.as_compat_dict(branch=row.branch, include_unverified=row.app in existing)
				item["source_type"] = MARKETPLACE_SOURCE
				rows.append(item)
		return rows

	def _apps_before_save(self):
		before = None if self.is_new() else self.get_doc_before_save()
		return {r.app for r in before.apps if r.app} if before else set()

	def validate_apps(self):
		"""Marketplace apps: approved + a Marketplace App Release for frappe_major.
		Custom apps: same team as this group, Active, compatible major (if declared)."""
		for row in self.apps:
			if row.source_type == CUSTOM_SOURCE:
				row.app = None
			else:
				row.custom_app = None
				if not row.app:
					frappe.throw(_("Row {0}: select a marketplace app").format(row.idx))
		rows = self._app_rows()
		errors = check_group_apps(self.frappe_major, self.team, rows)
		errors += mu.missing_requirements(rows)
		if errors:
			frappe.throw("<br>".join(errors), title=_("Incompatible apps"))
		for row, item in zip(self.apps, rows):
			row.app_name = item.get("app_name")
			row.repo_url = item.get("repo_url")
			if not row.branch:
				if row.source_type == CUSTOM_SOURCE:
					row.branch = item.get("branch")
				else:
					row.branch = release_for_major(item["releases"], self.frappe_major)["branch"]
			if not is_valid_branch(row.branch):
				frappe.throw(_("Row {0}: invalid branch {1}").format(row.idx, row.branch))
		self.sort_apps_by_requirements(rows)

	def sort_apps_by_requirements(self, rows):
		"""Required apps before the apps needing them (apps.json / bench get-app order)."""
		catalog = {r.get("app_name"): r for r in rows if r.get("app_name")}
		names = [row.app_name for row in self.apps]
		try:
			order = mu.sort_by_dependencies(names, catalog)
		except mu.RequirementError as exc:
			frappe.throw(_(str(exc)))
		if order != names:
			by_name = {row.app_name: row for row in self.apps}
			self.apps = [by_name[n] for n in order]
			for i, row in enumerate(self.apps, start=1):
				row.idx = i

	def get_apps_json(self):
		"""apps.json for image-builder (§4): [{url, branch}, ...] (frappe itself excluded).

		Never contains credentials: private custom repos are listed by their plain URL and get
		a short-lived token only in the image.build job handed to the agent at poll time.
		"""
		return build_group_apps_json(self._app_rows(), self.frappe_major)[0]

	def get_app_sources(self):
		"""(apps_json, app_sources) - see nexaploy.utils.build_group_apps_json."""
		return build_group_apps_json(self._app_rows(), self.frappe_major)

	def custom_commits(self):
		"""{custom_app: last_commit} of the custom apps in this group."""
		names = [row.custom_app for row in self.apps if row.source_type == CUSTOM_SOURCE and row.custom_app]
		if not names:
			return {}
		return {
			r.name: r.last_commit
			for r in frappe.get_all("Custom App", filters={"name": ["in", names]}, fields=["name", "last_commit"])
		}

	def has_custom_apps(self):
		return any(row.source_type == CUSTOM_SOURCE for row in self.apps)

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

	@frappe.whitelist()
	def deploy_latest(self):
		"""Build a new image from the current app branches (Deploy Candidate -> image.build).

		When the build succeeds and `auto_update` is set, every Active bench of this group is
		rolled to it (bench.update) inside `update_window` (see nexaploy.ops.apply_auto_updates).
		"""
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
		"""Python app names (what sites install), marketplace and custom."""
		return [row.app_name or row.app for row in self.apps]
