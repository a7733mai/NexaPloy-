"""Team activity log (ARCHITECTURE.md §10).

    from nexaploy.activity import log_activity
    log_activity(team, action, reference_doctype=None, reference_name=None, details=None)

Records a customer-visible mutation in `Team Activity` with the session user and
frappe.local.request_ip. It never raises: a failure to log is written to the Error Log
and the caller carries on. `details` is scrubbed with nexaploy.team_utils.scrub_details
(password/secret/token/key-like keys and credential-looking values are dropped, long
strings truncated) before it is stored. `action` is a dotted verb ("site.create",
"team.invite", "credits.topup", ...).

The row is inserted in the caller's transaction, so a request that fails and rolls back
also drops its activity row (nothing is logged for a mutation that did not happen).
"""

import json

import frappe

from nexaploy.team_utils import is_valid_activity_action, scrub_details

ACTIVITY = "Team Activity"


def log_activity(team, action, reference_doctype=None, reference_name=None, details=None):
	"""Append a Team Activity row; returns its name, or None when it could not be logged."""
	try:
		if not team or not is_valid_activity_action(action):
			raise ValueError(f"invalid activity {team!r} {action!r}")
		if not frappe.db.exists("Team", team):
			raise ValueError(f"unknown team {team!r}")
		user = getattr(getattr(frappe, "session", None), "user", None) or "Administrator"
		clean = scrub_details(details) if details is not None else None
		doc = frappe.get_doc(
			{
				"doctype": ACTIVITY,
				"team": team,
				"user": user if user != "Guest" else None,
				"action": str(action),
				"reference_doctype": reference_doctype or None,
				"reference_name": str(reference_name) if reference_name else None,
				"details": json.dumps(clean, default=str, ensure_ascii=False) if clean is not None else None,
				"ip_address": _request_ip(),
			}
		)
		doc.flags.ignore_links = True  # the referenced document may already be gone (drop)
		doc.insert(ignore_permissions=True)
		return doc.name
	except Exception:
		try:
			frappe.log_error(title=f"Nexaploy: activity {action} for {team} not logged")
		except Exception:
			pass
		return None


def _request_ip():
	try:
		ip = getattr(frappe.local, "request_ip", None)
		return str(ip)[:140] if ip else None
	except Exception:
		return None
