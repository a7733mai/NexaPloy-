# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from nexaploy import support_utils as su


class SupportTicket(Document):
	"""A team's support request. Customers use nexaploy.api.support (portal /dashboard/support);
	staff (role Nexaploy Support) answer in the desk with the Reply button. Every new reply row
	is stamped (author, created, is_staff), its message sanitised, the status moved with
	support_utils.next_status and the other side emailed (nexaploy.support)."""

	def validate(self):
		try:
			self.subject = su.validate_subject(self.subject)
			self.priority = su.normalize_priority(self.priority)
		except su.TicketError as exc:
			frappe.throw(_(str(exc)))
		if self.status not in su.STATUSES:
			self.status = su.OPEN
		before = None if self.is_new() else self.get_doc_before_save()
		now = now_datetime()
		if self.is_new():
			self.raised_by = self.raised_by or frappe.session.user
			self.first_response_due = su.first_response_due(now, self.priority)
		elif before and before.priority != self.priority:
			self.first_response_due = su.first_response_due(self.creation, self.priority)
		self._check_links()
		self._stamp_replies(before, now)
		self._track_status(before, now)

	def _check_links(self):
		for field in ("site", "server"):
			value = self.get(field)
			if value and frappe.db.get_value(field.title(), value, "team") != self.team:
				frappe.throw(_("{0} {1} does not belong to team {2}").format(_(field.title()), value, self.team))

	def _stamp_replies(self, before, now):
		from nexaploy.support import is_support_staff

		self.flags.new_replies = []
		portal = bool(self.flags.portal_reply)
		for row in self.replies:
			row.message = su.sanitize_html(row.message)
			if row.created:
				continue
			if su.is_blank_message(row.message):
				frappe.throw(_("Reply {0} is empty").format(row.idx))
			row.author = row.author or frappe.session.user
			row.created = now
			row.is_staff = 0 if portal else (1 if is_support_staff(row.author) else 0)
			self.flags.new_replies.append(row)
		for row in self.flags.new_replies:
			if self.is_new() and row.idx == 1 and not row.is_staff:
				continue  # the opening message
			event = "staff_reply" if row.is_staff else "customer_reply"
			try:
				self.status = su.next_status(self.status, event)
			except su.TicketError as exc:
				frappe.throw(_(str(exc)))
			if row.is_staff and not self.first_response_at:
				self.first_response_at = now
		if self.flags.new_replies:
			self.last_reply_at = now

	def _track_status(self, before, now):
		old = before.status if before else None
		if old == self.status:
			return
		if self.status == su.RESOLVED:
			self.resolved_at = now
		elif self.status == su.CLOSED:
			self.closed_at = now
		if self.status in su.ACTIVE_STATUSES:
			self.resolved_at = None
			self.closed_at = None

	def after_insert(self):
		from nexaploy.support import notify_staff

		first = self.replies[0] if self.replies else None
		notify_staff(self, first, new_ticket=True)

	def on_update(self):
		if self.flags.in_insert:
			return
		from nexaploy.support import notify_customer, notify_staff

		for row in self.flags.new_replies or []:
			if row.is_staff:
				notify_customer(self, row)
				self._log("support.staff_reply", {"status": self.status})
			else:
				notify_staff(self, row)

	def _log(self, action, details=None):
		try:
			from nexaploy.activity import log_activity

			log_activity(self.team, action, self.doctype, self.name, details)
		except Exception:
			pass
