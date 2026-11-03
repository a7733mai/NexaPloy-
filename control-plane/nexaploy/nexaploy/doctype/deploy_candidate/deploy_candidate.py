# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from nexaploy import ops
from nexaploy.agent_job import create_agent_job
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
		self.apps_json = frappe.as_json(group.get_apps_json())
		self.status = "Pending"

	def after_insert(self):
		"""Enqueue image.build {major, apps_json, tag, frappe_branch[, push, registry]} on the
		build server. With a registry the image is pushed so benches on other servers can pull it.
		"""
		group = frappe.get_doc("Release Group", self.release_group)
		params = {
			"major": int(group.frappe_major),
			"apps_json": frappe.parse_json(self.apps_json),
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
