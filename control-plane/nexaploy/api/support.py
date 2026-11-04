"""Support tickets for the customer portal (/dashboard/support) and the staff Reply button.

Every member of a team may open, read, answer, close and reopen the team's tickets
(team action `read`, ARCHITECTURE.md §9). Staff (role Nexaploy Support / System Manager)
answer from the desk through `staff_reply`. Every mutation is written to the activity log.

    options()                                   -> sites/servers of the team, priorities
    list_tickets(status=None, start=0, limit=50)
    get_ticket(name)
    create_ticket(subject, message, priority="Normal", site=None, server=None)
    reply(name, message)                        # customer -> Waiting on Support
    close_ticket(name) / reopen_ticket(name)
    staff_reply(name, message, status=None)     # desk: -> Waiting on Customer (or Resolved)

Pure rules: nexaploy/support_utils.py (SLA, status machine, sanitiser).
"""

import frappe
from frappe import _
from frappe.utils import now_datetime

from nexaploy import support_utils as su
from nexaploy.api.dashboard import _ctx
from nexaploy.teams import check_team_access

TICKET = "Support Ticket"
LIST_FIELDS = [
	"name", "subject", "status", "priority", "site", "server", "raised_by", "creation", "modified",
	"first_response_due", "first_response_at", "last_reply_at", "resolved_at", "closed_at",
]


def _throw(exc):
	frappe.throw(_(str(exc)))


def _log(team, action, name, details=None):
	try:
		from nexaploy.activity import log_activity

		log_activity(team, action, TICKET, name, details)
	except Exception:
		pass


def _ticket(name):
	"""Ticket doc after checking the caller's team access (`read`)."""
	_ctx("read")
	if not name or not frappe.db.exists(TICKET, name):
		raise frappe.DoesNotExistError(_("Ticket {0} not found").format(name))
	doc = frappe.get_doc(TICKET, name)
	check_team_access(doc.team, "read")
	return doc


def _sla(row, now=None):
	state = su.sla_state(row.get("creation"), row.get("priority"), row.get("first_response_at"), now or now_datetime())
	return {"due": state["due"], "state": state["state"], "remaining_seconds": state["remaining_seconds"]}


def _ticket_dict(doc):
	out = {f: doc.get(f) for f in LIST_FIELDS}
	out["team"] = doc.team
	out["sla"] = _sla(out)
	out["can_reply"] = doc.status != su.CLOSED
	out["can_close"] = doc.status != su.CLOSED
	out["can_reopen"] = doc.status in (su.RESOLVED, su.CLOSED)
	names = {}
	replies = []
	for row in doc.replies:
		if row.author not in names:
			names[row.author] = frappe.utils.get_fullname(row.author) if row.author else ""
		replies.append(
			{
				"name": row.name,
				"author": None if row.is_staff else row.author,  # staff addresses stay private
				"author_name": _("Nexaploy Support") if row.is_staff else names[row.author],
				"is_staff": bool(row.is_staff),
				"created": row.created,
				"message": su.sanitize_html(row.message),
			}
		)
	out["replies"] = replies
	return out


# --------------------------------------------------------------------------- portal


@frappe.whitelist()
def options():
	c = _ctx("read")
	return {
		"priorities": list(su.PRIORITIES),
		"sites": frappe.get_all("Site", filters={"team": c.team, "status": ["!=", "Archived"]}, pluck="name", order_by="name asc"),
		"servers": frappe.get_all(
			"Server", filters={"team": c.team, "status": ["!=", "Archived"]}, fields=["name", "hostname"], order_by="hostname asc"
		),
		"first_response_hours": su.FIRST_RESPONSE_HOURS,
	}


@frappe.whitelist()
def list_tickets(status=None, start=0, limit=50):
	c = _ctx("read")
	filters = {"team": c.team}
	if status == "active":
		filters["status"] = ["in", list(su.ACTIVE_STATUSES)]
	elif status:
		if status not in su.STATUSES:
			frappe.throw(_("Unknown status {0}").format(status))
		filters["status"] = status
	rows = frappe.get_all(
		TICKET,
		filters=filters,
		fields=LIST_FIELDS,
		order_by="modified desc",
		start=max(int(start or 0), 0),
		page_length=min(max(int(limit or 50), 1), 200),
	)
	now = now_datetime()
	for row in rows:
		row["sla"] = _sla(row, now)
	return {"tickets": rows, "statuses": list(su.STATUSES)}


@frappe.whitelist()
def get_ticket(name):
	return _ticket_dict(_ticket(name))


@frappe.whitelist(methods=["POST"])
def create_ticket(subject, message, priority="Normal", site=None, server=None):
	c = _ctx("read")
	try:
		subject = su.validate_subject(subject)
		priority = su.normalize_priority(priority)
	except su.TicketError as exc:
		_throw(exc)
	if su.is_blank_message(message):
		frappe.throw(_("Please describe the problem"))
	for doctype, value in (("Site", site), ("Server", server)):
		if value and frappe.db.get_value(doctype, value, "team") != c.team:
			raise frappe.PermissionError(_("{0} {1} belongs to another team").format(_(doctype), value))
	open_count = frappe.db.count(TICKET, {"team": c.team, "status": ["in", list(su.ACTIVE_STATUSES)]})
	if not su.can_open_ticket(open_count):
		frappe.throw(_("Your team has too many open tickets. Please reply to or close an existing one."))
	doc = frappe.get_doc(
		{
			"doctype": TICKET,
			"team": c.team,
			"subject": subject,
			"priority": priority,
			"site": site or None,
			"server": server or None,
			"status": su.OPEN,
			"raised_by": c.user,
			"replies": [{"author": c.user, "is_staff": 0, "message": message}],
		}
	)
	doc.flags.portal_reply = True
	doc.insert(ignore_permissions=True)
	_log(c.team, "support.ticket_create", doc.name, {"subject": subject, "priority": priority, "site": site, "server": server})
	return _ticket_dict(doc)


@frappe.whitelist(methods=["POST"])
def reply(name, message):
	doc = _ticket(name)
	if su.is_blank_message(message):
		frappe.throw(_("Please write a message"))
	if doc.status == su.CLOSED:
		frappe.throw(_("This ticket is closed. Reopen it to reply"))
	doc.append("replies", {"author": frappe.session.user, "is_staff": 0, "message": message})
	doc.flags.portal_reply = True
	doc.save(ignore_permissions=True)
	_log(doc.team, "support.reply", doc.name, {"status": doc.status})
	return _ticket_dict(doc)


def _set_status(doc, event, action):
	try:
		doc.status = su.next_status(doc.status, event)
	except su.TicketError as exc:
		_throw(exc)
	doc.save(ignore_permissions=True)
	_log(doc.team, action, doc.name, {"status": doc.status})
	return _ticket_dict(doc)


@frappe.whitelist(methods=["POST"])
def close_ticket(name):
	return _set_status(_ticket(name), "close", "support.close")


@frappe.whitelist(methods=["POST"])
def reopen_ticket(name):
	doc = _ticket(name)
	result = _set_status(doc, "reopen", "support.reopen")
	from nexaploy.support import notify_staff

	notify_staff(doc)
	return result


# --------------------------------------------------------------------------- staff (desk)


@frappe.whitelist(methods=["POST"])
def staff_reply(name, message, status=None):
	"""Desk Reply button: add a staff reply (-> Waiting on Customer, emails the team);
	`status` "Resolved" also resolves the ticket."""
	from nexaploy.support import is_support_staff

	if not is_support_staff():
		raise frappe.PermissionError(_("Only support staff can reply here"))
	doc = frappe.get_doc(TICKET, name)
	if su.is_blank_message(message):
		frappe.throw(_("Please write a message"))
	doc.append("replies", {"author": frappe.session.user, "is_staff": 1, "message": message})
	if not doc.assigned_to:
		doc.assigned_to = frappe.session.user
	doc.save(ignore_permissions=True)
	if status == su.RESOLVED and doc.status != su.RESOLVED:
		doc.reload()
		try:
			doc.status = su.next_status(doc.status, "resolve")
		except su.TicketError as exc:
			_throw(exc)
		doc.save(ignore_permissions=True)
		_log(doc.team, "support.resolve", doc.name)
	return {"status": doc.status}
