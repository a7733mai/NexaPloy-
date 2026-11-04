# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, today

from nexaploy.agent_release_utils import validate_release


class AgentRelease(Document):
	"""A nexaploy-agent version servers can be upgraded to (nexaploy/agent_upgrades.py)."""

	def _clean(self):
		try:
			return validate_release(
				self.version, self.git_ref, self.channel or "stable", self.repo_url, self.wheel_url, self.wheel_sha256
			)
		except ValueError as exc:
			frappe.throw(_(str(exc)))

	def before_naming(self):
		self.version = self._clean()["version"]

	def validate(self):
		frappe.only_for("System Manager")
		clean = self._clean()
		for field in ("version", "git_ref", "channel", "repo_url", "wheel_url", "wheel_sha256"):
			self.set(field, clean[field])
		if self.published and not self.released_on:
			self.released_on = today()

	@frappe.whitelist()
	def upgrade_outdated_servers(self, dry_run=0):
		"""Upgrade every Active server whose agent is older than the latest release of its
		channel (or only list them with dry_run). Platform admins only."""
		from nexaploy.agent_upgrades import bulk_upgrade

		return bulk_upgrade(None, dry_run=cint(dry_run))
