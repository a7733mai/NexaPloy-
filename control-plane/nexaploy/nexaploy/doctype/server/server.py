# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy.utils import generate_server_id, generate_token, hash_token


class Server(Document):
	def before_insert(self):
		if not self.server_id:
			self.server_id = generate_server_id()
			while frappe.db.exists("Server", {"server_id": self.server_id}):
				self.server_id = generate_server_id()
		if not self.team and "Nexaploy Customer" in frappe.get_roles():
			self.team = frappe.session.user

	def autoname(self):
		if not self.server_id:
			self.before_insert()
		self.name = self.server_id

	def validate(self):
		if self.plan:
			plan_type = frappe.db.get_value("Nexaploy Plan", self.plan, "plan_type")
			expected = "Server BYOS" if self.ownership == "BYOS" else "Server Hosted"
			if plan_type != expected:
				frappe.throw(_("Plan {0} is not a {1} plan").format(self.plan, expected))

	def on_trash(self):
		if frappe.db.exists("Bench", {"server": self.name, "status": ["!=", "Archived"]}):
			frappe.throw(_("Archive the benches on this server first"))

	@frappe.whitelist()
	def rotate_token(self):
		"""Issue a new server token (e.g. for a Hosted server installed with
		`install.sh --server-id <id> --token <T>`). Returned once, stored as sha256 only.
		"""
		frappe.only_for("System Manager")
		token = generate_token()
		self.db_set("token_hash", hash_token(token))
		return {"server_id": self.server_id, "server_token": token}

	@frappe.whitelist()
	def archive(self):
		self.check_permission("write")
		self.on_trash()
		self.db_set("status", "Archived")
		self.db_set("token_hash", None)
