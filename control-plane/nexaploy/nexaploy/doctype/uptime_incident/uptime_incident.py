# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class UptimeIncident(Document):
	"""Downtime of an Uptime Monitor (nexaploy.monitoring): opened when a quorum of probes saw
	N consecutive failures, resolved on recovery. `cause` is internal; the public status page
	only shows `public_title` and the Status Incident Update rows."""

	def on_update(self):
		from nexaploy.monitoring import clear_team_status_cache

		clear_team_status_cache(self.team)
