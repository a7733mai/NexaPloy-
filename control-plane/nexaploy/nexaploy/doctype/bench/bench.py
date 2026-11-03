# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy.agent_job import create_agent_job
from nexaploy.utils import is_valid_bench_name


class Bench(Document):
	def before_insert(self):
		group = frappe.get_doc("Release Group", self.release_group)
		if not self.team:
			self.team = group.team
		if not self.deploy_candidate:
			self.deploy_candidate, _tag = group.latest_image()
		if not self.deploy_candidate:
			frappe.throw(_("Release Group {0} has no successful Deploy Candidate yet").format(group.name))
		dc = frappe.db.get_value(
			"Deploy Candidate", self.deploy_candidate, ["release_group", "status", "image_tag"], as_dict=True
		)
		if dc.release_group != self.release_group or dc.status != "Success":
			frappe.throw(_("Deploy Candidate {0} is not a successful build of {1}").format(
				self.deploy_candidate, self.release_group))
		self.image_tag = dc.image_tag
		self.status = "Pending"

	def validate(self):
		if not is_valid_bench_name(self.bench_name):
			frappe.throw(_("Bench name must match ^[a-z][a-z0-9-]{2,30}$, e.g. erp-v15-main"))
		if self.is_new() and frappe.db.get_value("Server", self.server, "status") not in ("Active", "Offline"):
			frappe.throw(_("Server {0} is not active").format(self.server))

	def after_insert(self):
		"""Enqueue bench.create {name, image, apps_json}."""
		apps_json = frappe.db.get_value("Deploy Candidate", self.deploy_candidate, "apps_json")
		create_agent_job(
			self.server,
			"bench.create",
			{"name": self.bench_name, "image": self.image_tag, "apps_json": frappe.parse_json(apps_json)},
			reference_doctype=self.doctype,
			reference_name=self.name,
		)

	def on_trash(self):
		if frappe.db.exists("Site", {"bench": self.name, "status": ["!=", "Archived"]}):
			frappe.throw(_("Bench {0} still has sites").format(self.name))

	def _job(self, kind, params=None):
		self.check_permission("write")
		return create_agent_job(
			self.server, kind, {"bench": self.bench_name, **(params or {})}, self.doctype, self.name
		).name

	@frappe.whitelist()
	def start(self):
		return self._job("bench.start")

	@frappe.whitelist()
	def stop(self):
		return self._job("bench.stop")

	@frappe.whitelist()
	def restart(self):
		return self._job("bench.restart")

	@frappe.whitelist()
	def update_image(self, deploy_candidate=None):
		"""Roll the bench to a newer build of the same release group (migrates all sites)."""
		group = frappe.get_doc("Release Group", self.release_group)
		dc_name = deploy_candidate or group.latest_image()[0]
		dc = frappe.db.get_value("Deploy Candidate", dc_name, ["release_group", "status", "image_tag"], as_dict=True)
		if not dc or dc.release_group != self.release_group or dc.status != "Success":
			frappe.throw(_("No successful Deploy Candidate for {0}").format(self.release_group))
		self.db_set("deploy_candidate", dc_name)
		return self._job("bench.update", {"image": dc.image_tag})

	@frappe.whitelist()
	def remove(self, force=False):
		return self._job("bench.remove", {"force": bool(int(force or 0))})
