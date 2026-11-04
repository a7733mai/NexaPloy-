"""Support tickets: staff role, notifications and row permissions (database side).

Pure rules (SLA, status machine, HTML sanitiser) live in nexaploy/support_utils.py, the
portal API in nexaploy/api/support.py, the scheduler task in nexaploy/support_tasks.py.

* Staff = users with the role **Nexaploy Support** (desk access) or System Managers. They see
  every ticket in the desk and reply with the *Reply* button on the Support Ticket form
  (nexaploy.api.support.staff_reply).
* New tickets and customer replies email the assigned staff member (else every support
  user); staff replies email the team's ops recipients (Owner/Admin/Developer) plus the
  person who raised the ticket.
"""

import frappe
from frappe import _
from frappe.utils import get_url

from nexaploy import support_utils as su

SUPPORT_ROLE = "Nexaploy Support"
TICKET = "Support Ticket"


def ensure_support_role():
	"""Create the Nexaploy Support role (desk access) if missing."""
	if frappe.db.exists("Role", SUPPORT_ROLE):
		return
	frappe.get_doc(
		{"doctype": "Role", "role_name": SUPPORT_ROLE, "desk_access": 1, "is_custom": 0}
	).insert(ignore_permissions=True)


def is_support_staff(user=None):
	user = user or frappe.session.user
	if not user or user == "Guest":
		return False
	if user == "Administrator":
		return True
	roles = set(frappe.get_roles(user))
	return bool(roles & {SUPPORT_ROLE, "System Manager"})


def support_staff_emails():
	"""Enabled users with the Nexaploy Support role."""
	users = frappe.get_all(
		"Has Role", filters={"role": SUPPORT_ROLE, "parenttype": "User"}, pluck="parent", distinct=True
	)
	if not users:
		return []
	rows = frappe.get_all(
		"User", filters={"name": ["in", users], "enabled": 1}, fields=["name", "email"]
	)
	return sorted({(r.email or r.name) for r in rows if r.name not in ("Administrator", "Guest") and "@" in (r.email or r.name)})


def _dedupe(emails):
	out, seen = [], set()
	for email in emails:
		email = (email or "").strip()
		if email and "@" in email and email.lower() not in seen:
			seen.add(email.lower())
			out.append(email)
	return out


def _user_email(user):
	if not user:
		return None
	return frappe.db.get_value("User", user, "email") or (user if "@" in user else None)


# --------------------------------------------------------------------------- notifications


def notify_staff(ticket, reply=None, new_ticket=False):
	"""Email support staff about a new ticket / customer reply (never raises)."""
	try:
		recipients = [_user_email(ticket.assigned_to)] if ticket.assigned_to else support_staff_emails()
		alert = None
		if not recipients:
			alert = frappe.db.get_single_value("Nexaploy Settings", "alert_email")
			recipients = [alert] if alert else []
		recipients = _dedupe(recipients)
		if not recipients:
			return
		subject = (
			_("[{0}] New support ticket: {1}") if new_ticket else _("[{0}] Customer replied: {1}")
		).format(ticket.name, ticket.subject)
		body = su.html_to_text(reply.message if reply else "", 4000)
		frappe.sendmail(
			recipients=recipients,
			subject=subject,
			message=_message_html(
				[
					_("Team: {0}").format(ticket.team),
					_("Priority: {0}").format(_(ticket.priority or su.DEFAULT_PRIORITY)),
					_("Site: {0}").format(ticket.site) if ticket.site else None,
					_("Server: {0}").format(ticket.server) if ticket.server else None,
				],
				body,
				get_url(f"/app/support-ticket/{ticket.name}"),
			),
			reference_doctype=TICKET,
			reference_name=ticket.name,
		)
	except Exception:
		frappe.log_error(title=f"Nexaploy: support email for {ticket.name} failed")


def notify_customer(ticket, reply):
	"""Email the team (ops recipients + the ticket's author) about a staff reply, and post it
	to the team's Notification Channels subscribed to support_reply."""
	_notify_channels(ticket, reply)
	try:
		from nexaploy.teams import team_notification_emails

		recipients = _dedupe(team_notification_emails(ticket.team, "ops") + [_user_email(ticket.raised_by)])
		if not recipients:
			return
		frappe.sendmail(
			recipients=recipients,
			subject=_("[{0}] Support replied: {1}").format(ticket.name, ticket.subject),
			message=_message_html(
				[_("Status: {0}").format(_(ticket.status))],
				su.html_to_text(reply.message, 4000),
				get_url(f"/dashboard/support?name={ticket.name}"),
			),
			reference_doctype=TICKET,
			reference_name=ticket.name,
		)
	except Exception:
		frappe.log_error(title=f"Nexaploy: support email for {ticket.name} failed")


def _notify_channels(ticket, reply):
	"""support_reply to the team's Notification Channels (the email above is always sent,
	so no fallback email here)."""
	try:
		from nexaploy.notify import notify

		notify(
			ticket.team,
			"support_reply",
			f"[{ticket.name}] Support replied: {ticket.subject}",
			su.html_to_text(reply.message, 1500),
			{"ticket": ticket.name, "status": ticket.status},
			get_url(f"/dashboard/support?name={ticket.name}"),
			email_fallback=False,
		)
	except Exception:
		frappe.log_error(title=f"Nexaploy: support notification for {ticket.name} failed")


def _message_html(meta_lines, body_text, link):
	esc = frappe.utils.escape_html
	parts = [esc(line) for line in meta_lines if line]
	html = "<br>".join(parts)
	if body_text:
		html += "<br><br>" + esc(body_text).replace("\n", "<br>")
	html += f'<br><br><a href="{esc(link)}">{esc(link)}</a>'
	return html


# --------------------------------------------------------------------------- permissions (hooks.py)


def support_ticket_query(user=None, doctype=None):
	"""Support staff see every ticket; customers only their teams' tickets."""
	user = user or frappe.session.user
	if is_support_staff(user):
		return ""
	from nexaploy.permissions import team_query

	return team_query(user, TICKET)


def has_support_ticket_permission(doc, ptype=None, user=None):
	"""Staff: everything their DocPerms allow. Customers: read (any member of the team);
	they write only through nexaploy.api.support."""
	user = user or frappe.session.user
	if is_support_staff(user):
		return True
	if (ptype or "read") not in ("read", "select", "print", "report", "email"):
		return False
	team = doc.get("team")
	if not team:
		return False
	from nexaploy.team_utils import role_allows
	from nexaploy.teams import get_team_role

	return role_allows(get_team_role(team, user), "read")

