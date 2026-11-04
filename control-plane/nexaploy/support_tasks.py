"""Support scheduler tasks (hooks.py scheduler_events).

    auto_close_resolved_tickets   (hourly) Resolved tickets with no activity for
                                   support_utils.AUTO_CLOSE_DAYS (7) days -> Closed
"""

import frappe
from frappe.utils import add_days, now_datetime

from nexaploy import support_utils as su

TICKET = "Support Ticket"


def auto_close_resolved_tickets(now=None):
	now = now or now_datetime()
	cutoff = add_days(now, -su.AUTO_CLOSE_DAYS)
	closed = []
	for row in frappe.get_all(
		TICKET,
		filters={"status": su.RESOLVED, "resolved_at": ["<=", cutoff]},
		fields=["name", "team", "status", "resolved_at"],
		limit_page_length=500,
	):
		if not su.should_auto_close(row.status, row.resolved_at, now):
			continue
		try:
			frappe.db.set_value(TICKET, row.name, {"status": su.CLOSED, "closed_at": now})
			try:
				from nexaploy.activity import log_activity

				log_activity(row.team, "support.auto_close", TICKET, row.name, {"resolved_at": str(row.resolved_at)})
			except Exception:
				pass
			frappe.db.commit()
			closed.append(row.name)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Nexaploy: auto-closing {row.name} failed")
	return closed
