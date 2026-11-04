"""Notification channels of the current team (/dashboard/notifications).

Owners and Admins only (team action `admin`, ARCHITECTURE.md §9): channels carry chat bot
tokens, WhatsApp access tokens and webhook URLs. Secrets are write-only: they are never
returned (only `has_<field>` flags and a masked hint), and leaving one blank on update keeps
the stored value. Every mutation is written to the activity log.

    options()                                   -> events (grouped), channel types, defaults
    list_channels()
    create_channel(channel)                     # dict: title, channel_type, enabled, events, <type fields>
    update_channel(name, channel)
    delete_channel(name)
    send_test(name)                             -> {ok, error} (synchronous)
    telegram_detect_chat(bot_token=None, name=None) -> [{id, type, title}]

The first non-email channel of a team also creates an "Email" channel subscribed to the
events that were emailed by default, so adding Telegram does not silently stop the emails
(it can be disabled or deleted). Dispatch: nexaploy/notify.py; pure rules: notify_utils.py.
"""

import frappe
from frappe import _

from nexaploy import notify_utils as nu
from nexaploy.api.dashboard import _ctx
from nexaploy.teams import check_team_access

CHANNEL = "Notification Channel"
PUBLIC_FIELDS = [
	"name", "title", "channel_type", "enabled", "email_recipients", "telegram_chat_id",
	"whatsapp_phone_number_id", "whatsapp_to", "whatsapp_template", "whatsapp_language",
	"webhook_host", "last_sent_at", "last_failure_at", "last_error", "failure_count", "creation",
]


def _throw(exc):
	frappe.throw(_(str(exc)))


def _log(team, action, name, details=None):
	try:
		from nexaploy.activity import log_activity

		log_activity(team, action, CHANNEL, name, details)
	except Exception:
		pass


def _parse(channel):
	if isinstance(channel, str):
		channel = frappe.parse_json(channel) if channel.strip() else {}
	if not isinstance(channel, dict):
		frappe.throw(_("Invalid channel data"))
	return channel


def _channel(name):
	"""Channel doc after checking the caller may administer its team."""
	c = _ctx("admin")
	if not name or not frappe.db.exists(CHANNEL, name):
		raise frappe.DoesNotExistError(_("Notification channel {0} not found").format(name))
	doc = frappe.get_doc(CHANNEL, name)
	if doc.team != c.team:
		check_team_access(doc.team, "admin")
	return doc


def _stored_secrets(doc):
	return {f for f in nu.SECRET_FIELDS if doc.get_password(f, raise_exception=False)}


def _channel_dict(doc):
	out = {f: doc.get(f) for f in PUBLIC_FIELDS}
	out["enabled"] = bool(doc.enabled)
	out["events"] = [row.event for row in doc.events]
	for f in nu.SECRET_FIELDS:
		value = doc.get_password(f, raise_exception=False) if f in nu.TYPE_FIELDS.get(doc.channel_type, ()) else None
		out["has_" + f] = bool(value)
		if f == "telegram_bot_token" and value:
			out["telegram_bot_hint"] = nu.mask(value)
	return out


def _apply(doc, values):
	for key, value in values.items():
		if key == "events":
			doc.set("events", [{"event": e} for e in value])
		else:
			doc.set(key, value)
	# fields of other channel types are cleared so a changed type leaves no stale data
	keep = set(nu.TYPE_FIELDS[doc.channel_type])
	for ctype, fields in nu.TYPE_FIELDS.items():
		for f in fields:
			if f not in keep and doc.get(f):
				doc.set(f, None)
	if doc.channel_type in nu.WEBHOOK_TYPES and values.get("webhook_url"):
		doc.webhook_host = nu.url_host(values["webhook_url"])
	elif doc.channel_type not in nu.WEBHOOK_TYPES:
		doc.webhook_host = None


@frappe.whitelist()
def options():
	c = _ctx("admin")
	return {
		"team": c.team,
		"types": list(nu.CHANNEL_TYPES),
		"events": [{"event": e, "label": nu.EVENT_LABELS[e]} for e in nu.EVENTS],
		"groups": [{"label": label, "events": list(events)} for label, events in nu.EVENT_GROUPS],
		"default_email_events": [e for e in nu.EVENTS if nu.DEFAULT_EMAIL.get(e)],
		"signature_header": nu.SIGNATURE_HEADER,
	}


@frappe.whitelist()
def list_channels():
	c = _ctx("admin")
	names = frappe.get_all(CHANNEL, filters={"team": c.team}, pluck="name", order_by="creation asc")
	return {"team": c.team, "channels": [_channel_dict(frappe.get_doc(CHANNEL, n)) for n in names]}


@frappe.whitelist()
def create_channel(channel):
	c = _ctx("admin")
	data = _parse(channel)
	try:
		values = nu.validate_channel(data)
	except nu.ChannelError as e:
		_throw(e)
	first = not frappe.db.exists(CHANNEL, {"team": c.team})
	doc = frappe.new_doc(CHANNEL)
	doc.team = c.team
	_apply(doc, values)
	doc.flags.ignore_permissions = True
	doc.insert()
	_log(c.team, "notification_channel.create", doc.name, {"title": doc.title, "type": doc.channel_type, "events": values["events"]})
	result = {"channel": _channel_dict(doc), "email_channel": None}
	if first and doc.channel_type != nu.EMAIL:
		email = _create_default_email_channel(c.team)
		result["email_channel"] = _channel_dict(email)
	return result


def _create_default_email_channel(team):
	"""Keep the emails a team without channels received (notify_utils.DEFAULT_EMAIL)."""
	doc = frappe.new_doc(CHANNEL)
	doc.team = team
	doc.title = _("Email")
	doc.channel_type = nu.EMAIL
	doc.enabled = 1
	doc.set("events", [{"event": e} for e in nu.EVENTS if nu.DEFAULT_EMAIL.get(e)])
	doc.flags.ignore_permissions = True
	doc.insert()
	_log(team, "notification_channel.create", doc.name, {"title": doc.title, "type": nu.EMAIL, "default": True})
	return doc


@frappe.whitelist()
def update_channel(name, channel):
	doc = _channel(name)
	data = _parse(channel)
	data.setdefault("channel_type", doc.channel_type)
	for key in ("title", "enabled"):
		data.setdefault(key, doc.get(key))
	data.setdefault("events", [row.event for row in doc.events])
	for f in nu.TYPE_FIELDS.get(data.get("channel_type"), ()):
		if f not in nu.SECRET_FIELDS:
			data.setdefault(f, doc.get(f))
	existing = _stored_secrets(doc) if data["channel_type"] == doc.channel_type else set()
	try:
		values = nu.validate_channel(data, existing)
	except nu.ChannelError as e:
		_throw(e)
	_apply(doc, values)
	doc.flags.ignore_permissions = True
	doc.save()
	_drop_stale_secrets(doc)
	_log(doc.team, "notification_channel.update", doc.name, {
		"title": doc.title, "type": doc.channel_type, "enabled": doc.enabled, "events": values["events"],
		"updated_fields": sorted(f for f in nu.SECRET_FIELDS if values.get(f)),
	})
	return _channel_dict(doc)


def _drop_stale_secrets(doc):
	"""Remove stored secrets of other channel types (after a type change)."""
	from frappe.utils.password import remove_encrypted_password

	keep = set(nu.TYPE_FIELDS[doc.channel_type])
	for f in nu.SECRET_FIELDS:
		if f not in keep:
			try:
				remove_encrypted_password(CHANNEL, doc.name, f)
			except Exception:
				pass


@frappe.whitelist()
def delete_channel(name):
	doc = _channel(name)
	team, title = doc.team, doc.title
	frappe.delete_doc(CHANNEL, doc.name, ignore_permissions=True)
	_log(team, "notification_channel.delete", name, {"title": title})
	return {"deleted": name}


@frappe.whitelist()
def send_test(name):
	doc = _channel(name)
	from nexaploy.notify import send_test as send

	result = send(doc.name)
	_log(doc.team, "notification_channel.test", doc.name, {"ok": result["ok"]})
	doc.reload()
	result["channel"] = _channel_dict(doc)
	return result


@frappe.whitelist()
def telegram_detect_chat(bot_token=None, name=None):
	"""After the user sent any message to the bot (or added it to a group/channel), list the
	chats Telegram reports for it so the portal can fill the chat id."""
	_ctx("admin")
	token = (bot_token or "").strip()
	if not token and name:
		doc = _channel(name)
		token = doc.get_password("telegram_bot_token", raise_exception=False) or ""
	if not token:
		frappe.throw(_("Enter the bot token first"))
	from nexaploy.notify import telegram_chats

	try:
		chats = telegram_chats(token)
	except nu.ChannelError as e:
		_throw(e)
	return {"chats": chats}
