# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from nexaploy import ops
from nexaploy.agent_job import create_agent_job
from nexaploy.github_secrets import apps_json_secret_ref, needs_clone_tokens
from nexaploy.github_utils import redact_apps_json
from nexaploy.utils import make_build_id


class DeployCandidate(Document):
	def before_insert(self):
		group = frappe.get_doc("Release Group", self.release_group)
		build_server = group.build_server or ops.default_build_server()
		if not build_server:
			frappe.throw(
				_("Set a Build Server on Release Group {0} (or a Default Build Server in Nexaploy Settings)").format(
					group.name
				)
			)
		self.build_server = build_server
		self.build_id = make_build_id(now_datetime())
		# <registry_url>/nexaploy/bench:<group>-<build_id> when a registry is configured
		self.image_tag = ops.image_tag_for(group.name, self.build_id)
		apps_json, sources = group.get_app_sources()
		# apps_json is credential-free; custom repos get a token only at poll time (see
		# after_insert and nexaploy.github_secrets)
		self.apps_json = frappe.as_json(redact_apps_json(apps_json))
		self.app_sources = frappe.as_json(sources)
		self.commits = frappe.as_json(group.custom_commits())
		self.status = "Pending"

	def after_insert(self):
		"""Enqueue image.build {major, apps_json, tag, frappe_branch[, push, registry]} on the
		build server. With a registry the image is pushed so benches on other servers can pull it.
		"""
		group = frappe.get_doc("Release Group", self.release_group)
		apps_json = frappe.parse_json(self.apps_json)
		if needs_clone_tokens(frappe.parse_json(self.app_sources or "[]")):
			# resolved by nexaploy.github_secrets.resolve in nexaploy.api.agent.poll
			apps_json = apps_json_secret_ref(self.name)
		params = {
			"major": int(group.frappe_major),
			"apps_json": apps_json,
			"tag": self.image_tag,
			"frappe_branch": group.frappe_branch,
		}
		registry = ops.registry_params(self.image_tag)
		if registry:
			params.update({"push": True, "registry": registry})
		job = create_agent_job(
			self.build_server,
			"image.build",
			params,
			reference_doctype=self.doctype,
			reference_name=self.name,
		)
		self.db_set("agent_job", job.name)

	def on_update(self):
		"""After a successful build, clear Release Group.update_available unless a custom app
		received a newer commit after this build was created."""
		if self.status != "Success" or not self.has_value_changed("status"):
			return
		group = frappe.get_doc("Release Group", self.release_group)
		if not group.update_available:
			return
		built = frappe.parse_json(self.commits or "{}") or {}
		current = group.custom_commits()
		if all(built.get(app) == commit for app, commit in current.items()):
			frappe.db.set_value("Release Group", group.name, "update_available", 0)
