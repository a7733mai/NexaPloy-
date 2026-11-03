# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy.github_utils import GitHubError, is_valid_branch, parse_repo, public_repo_url
from nexaploy.utils import is_valid_app_name, parse_frappe_majors


class CustomApp(Document):
	def validate(self):
		if not is_valid_app_name(self.app_name):
			frappe.throw(_("App name must be a python package name, e.g. acme_erp"))
		if self.app_name == "frappe":
			frappe.throw(_("frappe itself cannot be a custom app"))
		try:
			host, owner, repo = parse_repo(self.repo_url)
		except GitHubError as e:
			frappe.throw(str(e))
		if "@" in (self.repo_url or "").split("//", 1)[-1].split("/", 1)[0]:
			frappe.throw(_("Do not put credentials in the repository URL; use the access token field"))
		self.repo_url = public_repo_url(self.repo_url)
		self.repo_full_name = f"{owner}/{repo}"
		if not is_valid_branch(self.branch):
			frappe.throw(_("Invalid branch name {0}").format(self.branch))
		if self.provider == "GitHub":
			if host != "github.com":
				frappe.throw(_("GitHub apps need a github.com repository; use provider Other for {0}").format(host))
			if not self.installation:
				frappe.throw(_("Select the GitHub installation that can access {0}").format(self.repo_full_name))
			inst = frappe.db.get_value("GitHub Installation", self.installation, ["team", "status"], as_dict=True)
			if not inst or inst.team != self.team:
				frappe.throw(_("GitHub installation {0} belongs to another team").format(self.installation))
		majors = parse_frappe_majors(self.frappe_majors)
		if self.frappe_majors and not majors:
			frappe.throw(_("Frappe majors must be a comma separated list of 14, 15, 16"))
		self.frappe_majors = ",".join(majors)
		dup = frappe.db.get_value(
			"Custom App",
			{"team": self.team, "app_name": self.app_name, "name": ["!=", self.name], "status": "Active"},
			"name",
		)
		if dup and self.status == "Active":
			frappe.throw(_("Your team already has an active custom app named {0}").format(self.app_name))

	def on_trash(self):
		used = frappe.get_all(
			"Release Group App", filters={"custom_app": self.name, "parenttype": "Release Group"}, pluck="parent"
		)
		if used:
			frappe.throw(_("Custom app is used by release groups {0}").format(", ".join(sorted(set(used)))))

	def as_row(self, branch=None):
		"""Row dict for nexaploy.utils.check_group_apps / build_group_apps_json."""
		return custom_app_row(self.as_dict(), branch)


def custom_app_row(app, branch=None):
	return {
		"source_type": "Custom",
		"custom_app": app.get("name"),
		"app_name": app.get("app_name"),
		"team": app.get("team"),
		"status": app.get("status"),
		"frappe_majors": app.get("frappe_majors"),
		"repo_url": app.get("repo_url"),
		"repo": app.get("repo_full_name"),
		"branch": branch or app.get("branch"),
		"provider": app.get("provider"),
		"installation": app.get("installation"),
		"access_username": app.get("access_username"),
	}
