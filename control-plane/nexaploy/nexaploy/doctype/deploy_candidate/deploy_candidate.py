# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from nexaploy.agent_job import create_agent_job
from nexaploy.utils import image_tag, make_build_id


class DeployCandidate(Document):
	def before_insert(self):
		group = frappe.get_doc("Release Group", self.release_group)
		if not group.build_server:
			frappe.throw(_("Set a Build Server on Release Group {0} first").format(group.name))
		self.build_server = group.build_server
		self.build_id = make_build_id(now_datetime())
		self.image_tag = image_tag(group.name, self.build_id)
		self.apps_json = frappe.as_json(group.get_apps_json())
		self.status = "Pending"

	def after_insert(self):
		"""Enqueue image.build {major, apps_json, tag, frappe_branch} on the build server."""
		group = frappe.get_doc("Release Group", self.release_group)
		job = create_agent_job(
			self.build_server,
			"image.build",
			{
				"major": int(group.frappe_major),
				"apps_json": frappe.parse_json(self.apps_json),
				"tag": self.image_tag,
				"frappe_branch": group.frappe_branch,
			},
			reference_doctype=self.doctype,
			reference_name=self.name,
		)
		self.db_set("agent_job", job.name)
