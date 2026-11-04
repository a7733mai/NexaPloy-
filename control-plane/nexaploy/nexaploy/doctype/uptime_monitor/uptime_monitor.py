# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy import monitor_utils as mu


class UptimeMonitor(Document):
	"""External uptime check of one address of a team's site (nexaploy.monitoring,
	ARCHITECTURE.md §16). The target host must be the site's primary host or one of its Active
	custom domains; plan limits (interval, monitors per site) are enforced here so the desk and
	the portal API agree."""

	def validate(self):
		from nexaploy.monitoring import advanced_monitoring, enabled_probe_names, site_hosts

		site_team = frappe.db.get_value("Site", self.site, "team")
		if not self.team:
			self.team = site_team
		if self.team != site_team:
			frappe.throw(_("The monitor and its site must belong to the same team"))
		# only nexaploy.monitoring.ensure_default_monitors creates default monitors; their target is fixed
		if self.is_new():
			self.is_default = 1 if self.flags.default_monitor else 0
		else:
			self.is_default = frappe.db.get_value("Uptime Monitor", self.name, "is_default") or 0
		if self.is_default:
			fixed = mu.default_monitor(self.site)
			for key in ("hostname", "path", "method", "expected_status", "keyword"):
				self.set(key, fixed[key])
		data = self.as_dict()
		try:
			clean = mu.validate_monitor(data, site_hosts(self.site), enabled_probe_names(include_disabled=True))
			count = frappe.db.count("Uptime Monitor", {"site": self.site, "name": ["!=", self.name or ""]}) + 1
			# the default monitor counts towards the per-site limit but is never refused for it
			mu.check_plan_limits(
				clean["interval_minutes"], 1 if self.is_default else count, advanced_monitoring(self.team, self.site)
			)
		except mu.MonitorError as exc:
			frappe.throw(_(str(exc)))
		for key, value in clean.items():
			if key != "enabled":
				self.set(key, value)
		if not self.enabled and self.status != mu.PAUSED:
			self.status = mu.PAUSED
			if self.open_incident:  # pausing is not downtime: close the incident silently
				from nexaploy.monitoring import resolve_incident

				resolve_incident(self.open_incident, frappe.utils.now_datetime(), "Monitor paused")
				self.open_incident = None
		elif self.enabled and self.status == mu.PAUSED:
			self.status = mu.PENDING
			self.probe_state = None

	def on_trash(self):
		for doctype in ("Uptime Check Result", "Uptime Rollup"):
			frappe.db.delete(doctype, {"monitor": self.name})
		for name in frappe.get_all("Uptime Incident", filters={"monitor": self.name}, pluck="name"):
			frappe.delete_doc("Uptime Incident", name, ignore_permissions=True, force=True)
		for row in frappe.get_all("Status Page Monitor", filters={"monitor": self.name}, fields=["name", "parent"]):
			frappe.db.delete("Status Page Monitor", {"name": row.name})
			from nexaploy.monitoring import clear_status_cache

			clear_status_cache(row.parent)
