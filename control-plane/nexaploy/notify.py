"""Notification dispatch: one entry point for every team-facing event.

    from nexaploy.notify import notify
    notify(team, "site_down", title, message, context={"site": name}, url=get_url(...))

`notify` never blocks and never raises: it enqueues `deliver` on the `short` queue (after
the caller's transaction commits). `deliver` sends to every enabled Notification Channel of
the team subscribed to the event (Email via frappe.sendmail; Telegram, WhatsApp, Slack,
Discord and generic webhooks over HTTPS with a 10 s timeout). Each channel's failure is
logged (Error Log) and recorded on the channel (last_error, last_failure_at,
failure_count) without affecting the others; a success records last_sent_at.

A team with no channel at all keeps the behaviour from before channels existed
(notify_utils.DEFAULT_EMAIL): infrastructure alerts email the team's ops recipients plus
Nexaploy Settings.alert_email (nexaploy.ops.send_alert), billing events email the billing
recipients. Platform operators (alert_email) keep a copy of infrastructure alerts even when
the team uses chat channels.

Pure formatting/signing/filtering: nexaploy/notify_utils.py.
"""

import socket

import frappe
from frappe.utils import now_datetime

from nexaploy import notify_utils as nu

CHANNEL = "Notification Channel"
CHANNEL_FIELDS = [
	"name", "title", "team", "channel_type", "enabled", "failure_count", "email_recipients",
	"telegram_chat_id", "whatsapp_phone_number_id", "whatsapp_to", "whatsapp_template",
	"whatsapp_language",
]


def notify(team, event, title, message, context=None, url=None, email_fallback=True):
	"""Queue `event` for `team`'s channels. Never raises, never blocks the caller.

	email_fallback=False skips the default email of a team without channels (for callers
	that send their own email, e.g. support replies)."""
	try:
		notification = nu.build_notification(team, event, title, message, context, url, now_datetime())
		frappe.enqueue(
			"nexaploy.notify.deliver",
			queue="short",
			enqueue_after_commit=True,
			now=bool(frappe.flags.in_test),
			notification=notification,
			email_fallback=bool(email_fallback),
		)
	except Exception:
		frappe.log_error(title=f"Nexaploy: notify {event} for {team} failed")


def deliver(notification, email_fallback=True):
	"""Background job: send one notification to the team's channels (see module doc)."""
	team, event = notification.get("team"), notification.get("event")
	channels = team_channels(team)
	has_channels = bool(channels)
	kind = nu.fallback_email_kind(event, has_channels) if email_fallback else None
	if kind:
		_safe(_default_email, team, kind, notification)
	if nu.needs_platform_copy(event, has_channels):
		_safe(_platform_copy, notification)
	sent = 0
	for row in nu.select_channels(channels, event):
		if send_to_channel(row, notification)[0]:
			sent += 1
	return sent


def team_channels(team):
	"""All channels of `team` (enabled or not) with their subscribed events."""
	if not team:
		return []
	rows = frappe.get_all(CHANNEL, filters={"team": team}, fields=CHANNEL_FIELDS, order_by="creation asc")
	if not rows:
		return []
	events = frappe.get_all(
		"Notification Channel Event",
		filters={"parenttype": CHANNEL, "parent": ["in", [r.name for r in rows]]},
		fields=["parent", "event"],
	)
	by_parent = {}
	for e in events:
		by_parent.setdefault(e.parent, []).append(e.event)
	for r in rows:
		r["events"] = by_parent.get(r.name, [])
	return rows


def send_to_channel(row, notification, record=True, immediate=False):
	"""Send to one channel. Returns (ok, error). Never raises; failures are logged and,
	with `record`, stored on the channel. `immediate` sends email synchronously (tests)
	instead of through the Email Queue."""
	channel = dict(row)
	secrets = []
	try:
		_load_secrets(channel)
		secrets = nu.channel_secrets(channel)
		if channel.get("channel_type") == nu.EMAIL:
			_send_email(channel, notification, immediate)
		else:
			_send_http(nu.build_request(channel, notification), channel.get("channel_type"))
		ok, error = True, None
	except Exception as e:
		ok, error = False, nu.redact(_error_text(e), secrets)
		frappe.log_error(
			title=f"Nexaploy: notification channel {channel.get('name')} failed",
			message=nu.redact(frappe.get_traceback(), secrets) + f"\n\nEvent: {notification.get('event')}\nError: {error}",
		)
	if record and channel.get("name"):
		try:
			values = nu.record_result(channel, ok, error, now_datetime())
			frappe.db.set_value(CHANNEL, channel["name"], values, update_modified=False)
		except Exception:
			frappe.log_error(title=f"Nexaploy: recording channel {channel.get('name')} status failed")
	return ok, error


def send_test(name):
	"""Synchronous test message to one channel: {ok, error}."""
	row = frappe.db.get_value(CHANNEL, name, CHANNEL_FIELDS, as_dict=True)
	if not row:
		return {"ok": False, "error": "Channel not found"}
	from frappe.utils import get_url

	notification = nu.sample_notification(row.team, now_datetime(), get_url("/dashboard/notifications"))
	ok, error = send_to_channel(row, notification, immediate=True)
	return {"ok": ok, "error": error}


def telegram_chats(bot_token):
	"""Chats that recently messaged the bot (getUpdates), newest first: [{id, type, title}].
	Raises nu.ChannelError with a readable message."""
	import requests

	url = nu.telegram_updates_url(bot_token)
	try:
		resp = requests.get(url, timeout=nu.HTTP_TIMEOUT, allow_redirects=False)
	except Exception as e:
		raise nu.ChannelError(nu.redact(f"Could not reach Telegram: {_error_text(e)}", [bot_token])) from None
	error = nu.response_error(resp.status_code, resp.text, nu.TELEGRAM)
	if error:
		raise nu.ChannelError(nu.redact(error, [bot_token]))
	try:
		return nu.telegram_chats(resp.json())
	except ValueError:
		raise nu.ChannelError("Telegram returned an invalid response") from None


# --------------------------------------------------------------------------- internals


def _safe(fn, *args):
	try:
		fn(*args)
	except Exception:
		frappe.log_error(title=f"Nexaploy: notification {fn.__name__} failed")


def _error_text(e):
	text = str(e) or e.__class__.__name__
	return nu.truncate(text, nu.MAX_ERROR)


def _load_secrets(channel):
	if not channel.get("name"):
		return
	from frappe.utils.password import get_decrypted_password

	for field in nu.TYPE_FIELDS.get(channel.get("channel_type"), ()):
		if field in nu.SECRET_FIELDS and not channel.get(field):
			channel[field] = get_decrypted_password(CHANNEL, channel["name"], field, raise_exception=False)


def _send_http(request, channel_type=None):
	import requests

	_check_public(request["url"])
	resp = requests.request(
		request["method"],
		request["url"],
		data=request["body"],
		headers=request["headers"],
		timeout=nu.HTTP_TIMEOUT,
		allow_redirects=False,
	)
	error = nu.response_error(resp.status_code, resp.text[:2000], channel_type)
	if error:
		raise nu.ChannelError(error)


def _check_public(url):
	"""Refuse URLs resolving to private/loopback/link-local addresses (customer-supplied
	webhooks must not reach the control plane's own network)."""
	host = nu.url_host(url)
	if not host:
		raise nu.ChannelError("Invalid URL")
	try:
		infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
	except socket.gaierror:
		raise nu.ChannelError(f"Could not resolve {host}") from None
	for info in infos:
		if not nu.is_public_ip(info[4][0]):
			raise nu.ChannelError(f"{host} resolves to a private address")


def _recipients(team, kind):
	from nexaploy.teams import team_notification_emails

	return team_notification_emails(team, kind) if team else []


def _send_email(channel, notification, immediate=False):
	recipients = nu.parse_emails(channel.get("email_recipients")) or _recipients(
		channel.get("team"), nu.email_recipient_kind(notification.get("event"))
	)
	if not recipients:
		raise nu.ChannelError("No recipients")
	subject, body = nu.format_email(notification)
	frappe.sendmail(recipients=recipients, subject=subject, message=body, delayed=not immediate)


def _default_email(team, kind, notification):
	"""Behaviour before channels: ops -> send_alert (team + alert_email), billing -> billing
	recipients."""
	subject, body = nu.format_email(notification)
	if kind == "ops":
		from nexaploy.ops import send_alert

		send_alert(team, notification.get("title"), body)
		return
	recipients = _recipients(team, kind)
	if recipients:
		frappe.sendmail(recipients=recipients, subject=subject, message=body, delayed=True)


def _platform_copy(notification):
	alert = frappe.db.get_single_value("Nexaploy Settings", "alert_email")
	if not alert:
		return
	subject, body = nu.format_email(notification)
	team = notification.get("team")
	if team:
		body = f"Team: {frappe.utils.escape_html(team)}<br><br>" + body
	frappe.sendmail(recipients=[alert], subject=subject, message=body, delayed=True)
