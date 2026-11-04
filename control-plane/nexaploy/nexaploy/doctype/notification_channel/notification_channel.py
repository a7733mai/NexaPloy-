# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

"""Where a team receives notifications (nexaploy.notify). Customers manage channels in
/dashboard/notifications (nexaploy.api.notifications, Owners/Admins); staff in the desk."""

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy import notify_utils as nu


class NotificationChannel(Document):
	def validate(self):
		if not self.team:
			from nexaploy.teams import get_current_team

			self.team = get_current_team()
		if not self.flags.ignore_permissions:
			from nexaploy.teams import check_team_access

			check_team_access(self.team, "admin")
		self.title = (self.title or self.channel_type or "").strip()
		try:
			events = nu.normalize_events([row.event for row in self.events])
		except nu.ChannelError as e:
			frappe.throw(_(str(e)))
		self.set("events", [{"event": e} for e in events])
		self._validate_type_fields()

	def _validate_type_fields(self):
		"""Non-secret checks (secrets are validated by the portal API before they are
		stored; in the desk a Password field shows a mask unless it was just typed)."""
		try:
			if self.channel_type == nu.EMAIL:
				self.email_recipients = ", ".join(nu.parse_emails(self.email_recipients))
			elif self.channel_type == nu.TELEGRAM:
				if self.telegram_chat_id and not nu.TELEGRAM_CHAT_RE.match(self.telegram_chat_id.strip()):
					raise nu.ChannelError("Enter the chat id (a number, or @channelname)")
			elif self.channel_type == nu.WHATSAPP:
				if self.whatsapp_to:
					self.whatsapp_to = nu.normalize_phone(self.whatsapp_to)
				self.whatsapp_language = self.whatsapp_language or "en_US"
			elif self.channel_type in nu.WEBHOOK_TYPES:
				url = self.webhook_url or ""
				if url and not set(url) <= {"*"}:
					self.webhook_url = nu.validate_webhook_url(url, self.channel_type)
					self.webhook_host = nu.url_host(self.webhook_url)
		except nu.ChannelError as e:
			frappe.throw(_(str(e)))
