# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy.team_utils import OWNER, count_owners, is_valid_slug, normalize_title


class Team(Document):
	"""A Team owns servers, release groups, benches, sites, backups, subscriptions and
	invoices (ARCHITECTURE.md §9). Named by `team_name`, a slug of the title."""

	def before_insert(self):
		self._set_team_name()
		if self.owner_user and not any(m.user == self.owner_user for m in self.members):
			self.append("members", {"user": self.owner_user, "role": OWNER})

	def before_naming(self):
		self._set_team_name()

	def autoname(self):
		self._set_team_name()
		self.name = self.team_name

	def _set_team_name(self):
		if not self.team_name:
			from nexaploy.teams import new_team_name

			self.team_name = new_team_name(self.title or self.owner_user or "team")

	def validate(self):
		try:
			self.title = normalize_title(self.title)
		except ValueError as exc:
			frappe.throw(_(str(exc)))
		if not is_valid_slug(self.team_name):
			frappe.throw(_("Team name {0} must be lowercase letters, digits and '-'").format(self.team_name))
		seen = set()
		for row in self.members:
			if row.user in seen:
				frappe.throw(_("{0} is listed twice in the team").format(row.user))
			seen.add(row.user)
		if self.members and not count_owners(m.as_dict() for m in self.members):
			frappe.throw(_("A team must keep at least one Owner"))
		if self.owner_user and self.owner_user not in seen and self.members:
			# owner_user follows the first Owner when the original owner left
			self.owner_user = next(m.user for m in self.members if m.role == OWNER)

	def on_update(self):
		from nexaploy.teams import clear_cache

		clear_cache()
