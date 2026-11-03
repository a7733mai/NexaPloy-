"""Scheduler jobs (see hooks.scheduler_events)."""

import frappe
from frappe.utils import add_to_date, now_datetime

OFFLINE_AFTER_MINUTES = 5


def mark_offline_servers():
	"""Active servers without a heartbeat for 5 minutes -> Offline."""
	cutoff = add_to_date(now_datetime(), minutes=-OFFLINE_AFTER_MINUTES)
	stale = frappe.get_all(
		"Server",
		filters={"status": "Active", "last_heartbeat": ["<", cutoff]},
		pluck="name",
	)
	never = frappe.get_all(
		"Server",
		filters={"status": "Active", "last_heartbeat": ["is", "not set"], "creation": ["<", cutoff]},
		pluck="name",
	)
	for name in set(stale) | set(never):
		frappe.db.set_value("Server", name, "status", "Offline", update_modified=False)
	if stale or never:
		frappe.db.commit()


def prune_registration_tokens():
	"""Delete unused registration tokens that expired (used ones are kept for audit)."""
	expired = frappe.get_all(
		"Server Registration Token",
		filters={"used": 0, "expires_on": ["<", now_datetime()]},
		pluck="name",
	)
	for name in expired:
		frappe.delete_doc("Server Registration Token", name, ignore_permissions=True, force=True)
	if expired:
		frappe.db.commit()
