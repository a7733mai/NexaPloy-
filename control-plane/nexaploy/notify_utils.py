"""Notification channels: pure logic (event catalogue, validation, per-channel message
formatting, HMAC signing, event filtering, alert transitions).

This module MUST NOT import frappe: it is unit-tested with plain pytest
(control-plane/tests/test_notifications.py). nexaploy/notify.py does the database and
HTTP work; nexaploy/api/notifications.py is the portal API.

A *notification* is a plain dict built by `build_notification`:
    {event, title, message, context, url, team, timestamp}
`build_request(channel, notification)` turns it into an HTTP request description
`{method, url, headers, body}` (body = bytes) for one channel; Email channels are sent with
frappe.sendmail instead (`format_email`).
"""

from __future__ import annotations

import hashlib
import hmac
import html
import ipaddress
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

# --------------------------------------------------------------------------- catalogue

SERVER_DOWN = "server_down"
SERVER_RECOVERED = "server_recovered"
DISK_HIGH = "disk_high"
CONTAINER_UNHEALTHY = "container_unhealthy"
SITE_DOWN = "site_down"
SITE_RECOVERED = "site_recovered"
BACKUP_FAILED = "backup_failed"
JOB_FAILED = "job_failed"
INVOICE_ISSUED = "invoice_issued"
PAYMENT_FAILED = "payment_failed"
SUSPENSION = "suspension"
SSL_EXPIRING = "ssl_expiring"  # no emitter yet (certificates are issued by Traefik)
SUPPORT_REPLY = "support_reply"
# uptime monitors (§16) other than a site's default monitor, which keeps site_down/site_recovered
MONITOR_DOWN = "monitor_down"
MONITOR_UP = "monitor_up"
SERVER_HEALTH_CRITICAL = "server_health_critical"
SERVER_HEALTH_OK = "server_health_ok"
TEST = "test"  # "Send test" from the portal; never subscribed, always delivered

EVENTS = (
	SERVER_DOWN,
	SERVER_RECOVERED,
	DISK_HIGH,
	CONTAINER_UNHEALTHY,
	SITE_DOWN,
	SITE_RECOVERED,
	BACKUP_FAILED,
	JOB_FAILED,
	INVOICE_ISSUED,
	PAYMENT_FAILED,
	SUSPENSION,
	SSL_EXPIRING,
	SUPPORT_REPLY,
	MONITOR_DOWN,
	MONITOR_UP,
	SERVER_HEALTH_CRITICAL,
	SERVER_HEALTH_OK,
)

EVENT_LABELS = {
	SERVER_DOWN: "Server offline",
	SERVER_RECOVERED: "Server back online",
	DISK_HIGH: "Disk almost full",
	CONTAINER_UNHEALTHY: "Unhealthy containers",
	SITE_DOWN: "Site down",
	SITE_RECOVERED: "Site back up",
	BACKUP_FAILED: "Backup failed",
	JOB_FAILED: "Job failed",
	INVOICE_ISSUED: "Invoice issued",
	PAYMENT_FAILED: "Payment failed",
	SUSPENSION: "Sites suspended",
	SSL_EXPIRING: "SSL certificate expiring",
	SUPPORT_REPLY: "Support reply",
	MONITOR_DOWN: "Monitor down",
	MONITOR_UP: "Monitor back up",
	SERVER_HEALTH_CRITICAL: "Server health critical",
	SERVER_HEALTH_OK: "Server health back to OK",
}

EVENT_GROUPS = (
	("Servers", (SERVER_DOWN, SERVER_RECOVERED, DISK_HIGH, CONTAINER_UNHEALTHY, SERVER_HEALTH_CRITICAL, SERVER_HEALTH_OK)),
	("Sites", (SITE_DOWN, SITE_RECOVERED, MONITOR_DOWN, MONITOR_UP, BACKUP_FAILED, JOB_FAILED, SSL_EXPIRING)),
	("Billing", (INVOICE_ISSUED, PAYMENT_FAILED, SUSPENSION)),
	("Support", (SUPPORT_REPLY,)),
)

BILLING_EVENTS = frozenset({INVOICE_ISSUED, PAYMENT_FAILED, SUSPENSION})

# Who gets an email when the team has no Notification Channel at all (the behaviour before
# channels existed): "ops" -> nexaploy.ops.send_alert (team Owner/Admin/Developer plus
# Nexaploy Settings.alert_email), "billing" -> the team's billing recipients, None -> nobody
# (job failures were never emailed; support replies have their own email in nexaploy.support).
DEFAULT_EMAIL = {
	SERVER_DOWN: "ops",
	SERVER_RECOVERED: "ops",
	DISK_HIGH: "ops",
	CONTAINER_UNHEALTHY: "ops",
	SITE_DOWN: "ops",
	SITE_RECOVERED: "ops",
	BACKUP_FAILED: "ops",
	JOB_FAILED: None,
	INVOICE_ISSUED: "billing",
	PAYMENT_FAILED: "billing",
	SUSPENSION: "billing",
	SSL_EXPIRING: "ops",
	SUPPORT_REPLY: None,
	MONITOR_DOWN: "ops",
	MONITOR_UP: "ops",
	SERVER_HEALTH_CRITICAL: "ops",
	SERVER_HEALTH_OK: "ops",
}

# Platform operators (Nexaploy Settings.alert_email) keep a copy of infrastructure alerts
# even when the team routes its own alerts to chat channels.
PLATFORM_COPY_EVENTS = frozenset(
	{SERVER_DOWN, SERVER_RECOVERED, DISK_HIGH, CONTAINER_UNHEALTHY, SITE_DOWN, SITE_RECOVERED,
	 SERVER_HEALTH_CRITICAL, SERVER_HEALTH_OK}
)

# server alert kinds (ops_utils.server_alerts) -> events; recovery events by kind
ALERT_KIND_EVENTS = {"offline": SERVER_DOWN, "disk": DISK_HIGH, "containers": CONTAINER_UNHEALTHY}
ALERT_RECOVERY_EVENTS = {"offline": SERVER_RECOVERED}
ACTIVE_ALERTS_KEY = "_active"

# Agent job kinds whose failure a customer cares about (job_failed). Backups have their own
# event; read-only/diagnostic and housekeeping jobs (usage, config reads, logs, SQL,
# health, prune, configure_backups) never notify.
USER_FACING_JOB_KINDS = frozenset(
	{
		"image.build",
		"bench.create",
		"bench.update",
		"bench.start",
		"bench.stop",
		"bench.restart",
		"bench.remove",
		"site.create",
		"site.install_app",
		"site.uninstall_app",
		"site.add_domain",
		"site.remove_domain",
		"site.restore",
		"site.migrate",
		"site.drop",
		"site.clone",
		"site.rename",
		"server.os_update",
	}
)

EMAIL = "Email"
TELEGRAM = "Telegram"
WHATSAPP = "WhatsApp"
SLACK = "Slack"
DISCORD = "Discord"
WEBHOOK = "Webhook"
CHANNEL_TYPES = (EMAIL, TELEGRAM, WHATSAPP, SLACK, DISCORD, WEBHOOK)
WEBHOOK_TYPES = frozenset({SLACK, DISCORD, WEBHOOK})

# Password fields of Notification Channel (never returned by the API)
SECRET_FIELDS = ("telegram_bot_token", "whatsapp_access_token", "webhook_url", "webhook_secret")
# fields per channel type (besides title / enabled / events)
TYPE_FIELDS = {
	EMAIL: ("email_recipients",),
	TELEGRAM: ("telegram_bot_token", "telegram_chat_id"),
	WHATSAPP: (
		"whatsapp_phone_number_id",
		"whatsapp_access_token",
		"whatsapp_to",
		"whatsapp_template",
		"whatsapp_language",
	),
	SLACK: ("webhook_url", "webhook_secret"),
	DISCORD: ("webhook_url", "webhook_secret"),
	WEBHOOK: ("webhook_url", "webhook_secret"),
}
REQUIRED_SECRETS = {
	TELEGRAM: ("telegram_bot_token",),
	WHATSAPP: ("whatsapp_access_token",),
	SLACK: ("webhook_url",),
	DISCORD: ("webhook_url",),
	WEBHOOK: ("webhook_url",),
}

HTTP_TIMEOUT = 10
SIGNATURE_HEADER = "X-Nexaploy-Signature"
EVENT_HEADER = "X-Nexaploy-Event"
TELEGRAM_API = "https://api.telegram.org"
WHATSAPP_API = "https://graph.facebook.com/v21.0"
TELEGRAM_MAX = 4096
DISCORD_MAX = 2000
SLACK_MAX = 3900
WHATSAPP_MAX = 4096
WHATSAPP_PARAM_MAX = 1024
TITLE_MAX = 140
MESSAGE_MAX = 3000

TELEGRAM_TOKEN_RE = re.compile(r"^\d{3,12}:[A-Za-z0-9_-]{30,64}$")
TELEGRAM_CHAT_RE = re.compile(r"^(-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31})$")
DIGITS_RE = re.compile(r"^\d{5,20}$")
PHONE_RE = re.compile(r"^\d{8,15}$")
TEMPLATE_RE = re.compile(r"^[a-z0-9_]{1,512}$")
LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(_[A-Z]{2})?$")
EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
SLACK_HOSTS = ("hooks.slack.com",)
DISCORD_HOSTS = ("discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com")


class ChannelError(ValueError):
	"""Invalid channel configuration (message is user-facing and translatable)."""


# --------------------------------------------------------------------------- helpers


def truncate(text: Any, limit: int) -> str:
	text = "" if text is None else str(text)
	if len(text) <= limit:
		return text
	return text[: max(0, limit - 1)].rstrip() + "…"


def iso_now(now: datetime | None = None) -> str:
	now = now or datetime.now(timezone.utc)
	if now.tzinfo is None:
		now = now.replace(tzinfo=timezone.utc)
	return now.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def event_label(event: str) -> str:
	return EVENT_LABELS.get(event, "Test notification" if event == TEST else str(event))


def is_event(event: Any) -> bool:
	return event in EVENTS


def normalize_events(events: Any) -> list[str]:
	"""List (or comma separated string) of events -> unique known events in catalogue order.
	Raises ChannelError for unknown names."""
	if events is None:
		return []
	if isinstance(events, str):
		events = [e for e in re.split(r"[\s,]+", events) if e]
	chosen = set()
	for e in events:
		if isinstance(e, Mapping):
			e = e.get("event")
		e = str(e or "").strip()
		if not e:
			continue
		if e not in EVENTS:
			raise ChannelError(f"Unknown event {e}")
		chosen.add(e)
	return [e for e in EVENTS if e in chosen]


def channel_events(channel: Mapping[str, Any]) -> set[str]:
	"""Subscribed events of a channel dict (`events` = list of names or child rows)."""
	raw = channel.get("events") or []
	if isinstance(raw, str):
		raw = re.split(r"[\s,]+", raw)
	out = set()
	for row in raw:
		name = row.get("event") if isinstance(row, Mapping) else row
		if name:
			out.add(str(name))
	return out


def channel_wants(channel: Mapping[str, Any], event: str) -> bool:
	"""Enabled and subscribed to `event` (the `test` event reaches every channel asked)."""
	if event == TEST:
		return True
	if not int(channel.get("enabled") or 0):
		return False
	return event in channel_events(channel)


def select_channels(channels: Iterable[Mapping[str, Any]], event: str) -> list[Mapping[str, Any]]:
	return [c for c in channels if channel_wants(c, event)]


def fallback_email_kind(event: str, has_channels: bool) -> str | None:
	"""Kind of default email ("ops"/"billing") when the team has no channel at all."""
	if has_channels:
		return None
	return DEFAULT_EMAIL.get(event)


def needs_platform_copy(event: str, has_channels: bool) -> bool:
	"""Copy to Nexaploy Settings.alert_email when the team's own alerts go to channels (in
	fallback mode send_alert already includes it)."""
	return has_channels and event in PLATFORM_COPY_EVENTS


def email_recipient_kind(event: str) -> str:
	return "billing" if event in BILLING_EVENTS else "ops"


def parse_emails(value: Any) -> list[str]:
	"""Comma / semicolon / newline separated emails -> unique list; ChannelError if invalid."""
	if not value:
		return []
	items = value if isinstance(value, (list, tuple)) else re.split(r"[\s,;]+", str(value))
	out, seen = [], set()
	for item in items:
		item = str(item or "").strip()
		if not item:
			continue
		if not EMAIL_RE.match(item):
			raise ChannelError(f"Invalid email address {item}")
		if item.lower() not in seen:
			seen.add(item.lower())
			out.append(item)
	return out


def normalize_phone(value: Any) -> str:
	"""'+974 5555-1234' -> '97455551234' (E.164 digits, no plus); ChannelError if invalid."""
	digits = re.sub(r"[\s().+-]", "", str(value or ""))
	if digits.startswith("00"):
		digits = digits[2:]
	if not PHONE_RE.match(digits):
		raise ChannelError("Enter the WhatsApp number in international format, e.g. +974 5555 1234")
	return digits


def mask(value: Any, keep: int = 4) -> str:
	"""Display form of a secret: '••••abcd' (empty stays empty)."""
	value = str(value or "")
	if not value:
		return ""
	return "••••" + (value[-keep:] if len(value) > keep * 3 else "")


def redact(text: Any, secrets: Iterable[Any]) -> str:
	"""Replace every secret (token, webhook URL...) in an error message with '***'."""
	text = "" if text is None else str(text)
	for secret in sorted({str(s) for s in secrets if s and len(str(s)) >= 6}, key=len, reverse=True):
		text = text.replace(secret, "***")
	return text


# --------------------------------------------------------------------------- URLs (SSRF guard)


def is_public_ip(value: str) -> bool:
	try:
		ip = ipaddress.ip_address(value)
	except ValueError:
		return False
	if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
		ip = ip.ipv4_mapped
	return ip.is_global and not ip.is_multicast


def url_host(url: Any) -> str:
	try:
		return (urlsplit(str(url or "")).hostname or "").lower()
	except ValueError:
		return ""


def validate_webhook_url(url: Any, channel_type: str = WEBHOOK) -> str:
	"""https URL that does not point at the control plane's own network. Slack and Discord
	URLs must be the vendors' incoming-webhook URLs. Returns the stripped URL."""
	url = str(url or "").strip()
	try:
		parts = urlsplit(url)
		port = parts.port
	except ValueError:
		raise ChannelError("Enter a valid https:// webhook URL") from None
	host = (parts.hostname or "").lower().rstrip(".")
	if parts.scheme != "https" or not host or parts.username or parts.password or len(url) > 1000:
		raise ChannelError("Enter a valid https:// webhook URL")
	if port not in (None, 443) and channel_type != WEBHOOK:
		raise ChannelError("Enter a valid https:// webhook URL")
	if channel_type == SLACK and not (host in SLACK_HOSTS and parts.path.startswith("/services/")):
		raise ChannelError("Slack webhook URLs start with https://hooks.slack.com/services/")
	if channel_type == DISCORD and not (host in DISCORD_HOSTS and parts.path.startswith("/api/webhooks/")):
		raise ChannelError("Discord webhook URLs start with https://discord.com/api/webhooks/")
	bare = host.strip("[]")
	try:
		ipaddress.ip_address(bare)
		literal = True
	except ValueError:
		literal = False
	if literal and not is_public_ip(bare):
		raise ChannelError("Webhook URLs must point to a public address")
	if not literal and (
		"." not in host or host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa"))
	):
		raise ChannelError("Webhook URLs must point to a public address")
	return url


# --------------------------------------------------------------------------- validation


def _is_masked(value: Any) -> bool:
	text = str(value or "")
	return bool(text) and set(text) <= {"*", "•"}


def validate_channel(data: Mapping[str, Any], existing_secrets: Iterable[str] = ()) -> dict[str, Any]:
	"""Validate/normalise portal input for a channel. `existing_secrets` = Password fields
	already stored (blank input keeps them). Returns {title, channel_type, enabled, events,
	<type fields>}; secrets left blank are omitted (= keep). Raises ChannelError."""
	existing = set(existing_secrets or ())
	ctype = str(data.get("channel_type") or "").strip()
	if ctype not in CHANNEL_TYPES:
		raise ChannelError("Choose a channel type")
	title = truncate(str(data.get("title") or "").strip() or ctype, TITLE_MAX)
	out: dict[str, Any] = {
		"title": title,
		"channel_type": ctype,
		"enabled": 1 if _truthy(data.get("enabled", 1)) else 0,
		"events": normalize_events(data.get("events")),
	}
	if not out["events"]:
		raise ChannelError("Choose at least one event")
	value = {f: data.get(f) for f in TYPE_FIELDS[ctype]}
	for f in TYPE_FIELDS[ctype]:
		v = value[f]
		if f in SECRET_FIELDS:
			v = "" if v is None or _is_masked(v) else str(v).strip()
			if v:
				out[f] = v
			elif f in REQUIRED_SECRETS.get(ctype, ()) and f not in existing:
				raise ChannelError(f"{FIELD_LABELS[f]} is required")
		else:
			out[f] = "" if v is None else str(v).strip()

	if ctype == EMAIL:
		out["email_recipients"] = ", ".join(parse_emails(out["email_recipients"]))
	elif ctype == TELEGRAM:
		token = out.get("telegram_bot_token")
		if token and not TELEGRAM_TOKEN_RE.match(token):
			raise ChannelError("The bot token looks like 123456789:AA... (from @BotFather)")
		if not TELEGRAM_CHAT_RE.match(out["telegram_chat_id"]):
			raise ChannelError("Enter the chat id (a number, or @channelname)")
	elif ctype == WHATSAPP:
		if not DIGITS_RE.match(out["whatsapp_phone_number_id"]):
			raise ChannelError("Enter the Phone number ID from the WhatsApp Business dashboard")
		out["whatsapp_to"] = normalize_phone(out["whatsapp_to"])
		if out["whatsapp_template"] and not TEMPLATE_RE.match(out["whatsapp_template"]):
			raise ChannelError("Template names use lowercase letters, digits and underscores")
		out["whatsapp_language"] = out["whatsapp_language"] or "en_US"
		if not LANGUAGE_RE.match(out["whatsapp_language"]):
			raise ChannelError("Template language looks like en_US or ar")
	else:
		if out.get("webhook_url"):
			out["webhook_url"] = validate_webhook_url(out["webhook_url"], ctype)
		secret = out.get("webhook_secret")
		if secret is not None and secret and len(secret) < 16:
			raise ChannelError("The signing secret must be at least 16 characters")
	return out


FIELD_LABELS = {
	"telegram_bot_token": "Bot token",
	"whatsapp_access_token": "Access token",
	"webhook_url": "Webhook URL",
	"webhook_secret": "Signing secret",
}


def _truthy(value: Any) -> bool:
	if isinstance(value, str):
		return value.strip().lower() in ("1", "true", "yes", "on")
	return bool(value)


# --------------------------------------------------------------------------- notifications


def build_notification(
	team: Any,
	event: str,
	title: Any,
	message: Any,
	context: Mapping[str, Any] | None = None,
	url: Any = None,
	now: datetime | None = None,
) -> dict[str, Any]:
	"""The canonical payload (also the generic webhook body)."""
	if event != TEST and event not in EVENTS:
		raise ValueError(f"Unknown notification event {event!r}")
	ctx = {}
	for k, v in dict(context or {}).items():
		ctx[str(k)] = v if isinstance(v, (str, int, float, bool)) or v is None else str(v)
	return {
		"event": event,
		"title": truncate(title or event_label(event), TITLE_MAX * 2),
		"message": truncate(message or "", MESSAGE_MAX),
		"context": ctx,
		"url": str(url) if url else None,
		"team": team or None,
		"timestamp": iso_now(now),
	}


def telegram_escape(text: Any) -> str:
	"""Escape for Telegram parse_mode=HTML (only &, <, > are special; quotes for attributes)."""
	return html.escape("" if text is None else str(text), quote=True)


def format_telegram(n: Mapping[str, Any]) -> str:
	"""HTML message (parse_mode=HTML), at most TELEGRAM_MAX characters; only the message
	is shortened, before escaping, so no entity or tag is ever cut."""
	head = f"<b>{telegram_escape(n.get('title'))}</b>"
	tail = f'<a href="{telegram_escape(n["url"])}">Open in Nexaploy</a>' if n.get("url") else ""
	room = max(0, TELEGRAM_MAX - len(head) - len(tail) - 4)
	msg = str(n.get("message") or "")
	body = telegram_escape(msg)
	while len(body) > room and msg:
		keep = min(len(msg) - 1, int(len(msg) * room / len(body)))
		msg = truncate(msg, keep) if keep > 1 else ""
		body = telegram_escape(msg)
	return "\n\n".join(p for p in (head, body, tail) if p)


def slack_escape(text: Any) -> str:
	"""Slack mrkdwn control characters: &, <, >."""
	return ("" if text is None else str(text)).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_slack(n: Mapping[str, Any]) -> dict[str, Any]:
	text = f"*{slack_escape(n.get('title'))}*"
	if n.get("message"):
		text += "\n" + slack_escape(n["message"])
	if n.get("url"):
		text += f"\n<{slack_escape(n['url'])}|Open in Nexaploy>"
	return {"text": truncate(text, SLACK_MAX)}


def format_discord(n: Mapping[str, Any]) -> dict[str, Any]:
	text = f"**{n.get('title') or ''}**"
	if n.get("message"):
		text += "\n" + str(n["message"])
	if n.get("url"):
		text += f"\n<{n['url']}>"
	# no @everyone / role / user pings from customer-controlled text
	return {"content": truncate(text, DISCORD_MAX), "allowed_mentions": {"parse": []}}


def format_plain(n: Mapping[str, Any], limit: int = WHATSAPP_MAX) -> str:
	text = str(n.get("title") or "")
	if n.get("message"):
		text += "\n\n" + str(n["message"])
	if n.get("url"):
		text += "\n\n" + str(n["url"])
	return truncate(text, limit)


def format_whatsapp(n: Mapping[str, Any], to: str, template: str | None = None, language: str | None = None) -> dict[str, Any]:
	"""Meta Cloud API message. Free-form text is only delivered inside the 24 h customer
	service window; with an approved `template` (one body variable {{1}}) it always is."""
	if template:
		param = re.sub(r"\s*\n\s*", " | ", format_plain(n, WHATSAPP_PARAM_MAX))  # no newlines in params
		return {
			"messaging_product": "whatsapp",
			"to": to,
			"type": "template",
			"template": {
				"name": template,
				"language": {"code": language or "en_US"},
				"components": [{"type": "body", "parameters": [{"type": "text", "text": param}]}],
			},
		}
	return {
		"messaging_product": "whatsapp",
		"recipient_type": "individual",
		"to": to,
		"type": "text",
		"text": {"preview_url": False, "body": format_plain(n)},
	}


def format_email(n: Mapping[str, Any]) -> tuple[str, str]:
	"""(subject, html body) for Email channels."""
	subject = f"[Nexaploy] {n.get('title') or ''}"
	body = html.escape(str(n.get("message") or "")).replace("\n", "<br>")
	if n.get("url"):
		link = html.escape(str(n["url"]), quote=True)
		body += f'<br><br><a href="{link}">{link}</a>'
	return subject, body


def json_body(payload: Any) -> bytes:
	"""Canonical JSON bytes (what is signed is exactly what is sent)."""
	return json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")


def sign(secret: str | bytes, body: bytes) -> str:
	"""X-Nexaploy-Signature value: 'sha256=' + hex HMAC-SHA256(secret, raw body)."""
	key = secret.encode("utf-8") if isinstance(secret, str) else secret
	return "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()


def verify_signature(secret: str | bytes, body: bytes, header: str | None) -> bool:
	"""Receiver side helper (documented for customers; used in tests)."""
	if not header:
		return False
	return hmac.compare_digest(sign(secret, body), header.strip())


def webhook_payload(n: Mapping[str, Any]) -> dict[str, Any]:
	return {k: n.get(k) for k in ("event", "title", "message", "context", "url", "team", "timestamp")}


def build_request(channel: Mapping[str, Any], n: Mapping[str, Any]) -> dict[str, Any]:
	"""HTTP request for a non-email channel. `channel` carries decrypted secrets.
	Returns {method, url, headers, body (bytes)}; raises ChannelError if misconfigured."""
	ctype = channel.get("channel_type")
	headers = {"Content-Type": "application/json", "User-Agent": "Nexaploy-Notifications/1.0"}
	if ctype == TELEGRAM:
		token = channel.get("telegram_bot_token")
		if not token or not channel.get("telegram_chat_id"):
			raise ChannelError("Telegram bot token and chat id are required")
		payload = {
			"chat_id": channel["telegram_chat_id"],
			"text": format_telegram(n),
			"parse_mode": "HTML",
			"disable_web_page_preview": True,
		}
		return {"method": "POST", "url": f"{TELEGRAM_API}/bot{token}/sendMessage", "headers": headers, "body": json_body(payload)}
	if ctype == WHATSAPP:
		token, phone_id, to = (channel.get(k) for k in ("whatsapp_access_token", "whatsapp_phone_number_id", "whatsapp_to"))
		if not (token and phone_id and to):
			raise ChannelError("WhatsApp phone number ID, access token and recipient are required")
		payload = format_whatsapp(n, to, channel.get("whatsapp_template"), channel.get("whatsapp_language"))
		headers["Authorization"] = f"Bearer {token}"
		return {"method": "POST", "url": f"{WHATSAPP_API}/{phone_id}/messages", "headers": headers, "body": json_body(payload)}
	if ctype in WEBHOOK_TYPES:
		url = channel.get("webhook_url")
		if not url:
			raise ChannelError("Webhook URL is required")
		if ctype == SLACK:
			payload = format_slack(n)
		elif ctype == DISCORD:
			payload = format_discord(n)
		else:
			payload = webhook_payload(n)
		body = json_body(payload)
		headers[EVENT_HEADER] = str(n.get("event") or "")
		if channel.get("webhook_secret"):
			headers[SIGNATURE_HEADER] = sign(channel["webhook_secret"], body)
		return {"method": "POST", "url": url, "headers": headers, "body": body}
	raise ChannelError(f"Channel type {ctype} is not sent over HTTP")


def channel_secrets(channel: Mapping[str, Any]) -> list[str]:
	return [str(channel.get(f)) for f in SECRET_FIELDS if channel.get(f)]


def response_error(status: int, text: Any, channel_type: str | None = None) -> str | None:
	"""None when the HTTP response means 'delivered', else a short error message."""
	body = None
	try:
		body = json.loads(text) if text else None
	except (TypeError, ValueError):
		body = None
	if channel_type == TELEGRAM and isinstance(body, Mapping) and body.get("ok") is False:
		return truncate(f"Telegram: {body.get('description') or 'error'}", 300)
	if 200 <= int(status) < 300:
		return None
	detail = ""
	if isinstance(body, Mapping):
		err = body.get("error")
		if isinstance(err, Mapping):
			detail = err.get("message") or err.get("type") or ""
		else:
			detail = body.get("description") or body.get("message") or err or ""
	elif text:
		detail = str(text)
	detail = re.sub(r"\s+", " ", str(detail)).strip()
	return truncate(f"HTTP {status}" + (f": {detail}" if detail else ""), 300)


# --------------------------------------------------------------------------- delivery status


MAX_ERROR = 500


def record_result(
	state: Mapping[str, Any], ok: bool, error: Any = None, now: datetime | None = None
) -> dict[str, Any]:
	"""New status values for a channel after a send attempt."""
	now = now or datetime.now()
	if ok:
		return {"last_sent_at": now, "last_error": "", "failure_count": 0}
	return {
		"last_failure_at": now,
		"last_error": truncate(error or "Unknown error", MAX_ERROR),
		"failure_count": int(state.get("failure_count") or 0) + 1,
	}


# --------------------------------------------------------------------------- server alert transitions


def alert_transitions(
	state: Mapping[str, str],
	current_kinds: Iterable[str],
	now: datetime,
	interval: timedelta = timedelta(hours=6),
) -> tuple[list[str], list[str], dict[str, str]]:
	"""Throttled alerts and recoveries for one server.

	`state` is Server.alert_state ({kind: last alert iso}, plus ACTIVE_ALERTS_KEY = kinds
	alerted and not yet recovered, comma separated). Returns (kinds to alert now, kinds
	that recovered, new state). A kind is alerted at most once per `interval`; a recovery
	is only reported for a kind that was alerted (so throttled flaps stay quiet).
	"""
	new = {k: str(v) for k, v in dict(state or {}).items()}
	active = {k for k in str(new.get(ACTIVE_ALERTS_KEY) or "").split(",") if k}
	current = list(dict.fromkeys(current_kinds))
	alert = []
	for kind in current:
		last = new.get(kind)
		due = True
		if last:
			try:
				due = now - datetime.fromisoformat(last) >= interval
			except ValueError:
				due = True
		if due:
			alert.append(kind)
			new[kind] = now.isoformat(timespec="seconds")
			active.add(kind)
	recovered = sorted(active - set(current))
	active -= set(recovered)
	if active:
		new[ACTIVE_ALERTS_KEY] = ",".join(sorted(active))
	else:
		new.pop(ACTIVE_ALERTS_KEY, None)
	return alert, recovered, new


# --------------------------------------------------------------------------- Telegram chat detection


def telegram_chats(updates: Any) -> list[dict[str, Any]]:
	"""getUpdates result -> unique chats [{id, type, title}] (newest first) the bot has seen,
	so the portal can fill the chat id after the user messages the bot."""
	result = updates.get("result") if isinstance(updates, Mapping) else updates
	seen: dict[str, dict[str, Any]] = {}
	for upd in reversed(result or []):
		if not isinstance(upd, Mapping):
			continue
		for key in ("message", "edited_message", "channel_post", "edited_channel_post", "my_chat_member", "chat_member"):
			chat = (upd.get(key) or {}).get("chat") if isinstance(upd.get(key), Mapping) else None
			if not isinstance(chat, Mapping) or chat.get("id") is None:
				continue
			cid = str(chat["id"])
			if cid in seen:
				continue
			name = chat.get("title") or " ".join(
				p for p in (chat.get("first_name"), chat.get("last_name")) if p
			) or (f"@{chat['username']}" if chat.get("username") else cid)
			seen[cid] = {"id": cid, "type": chat.get("type") or "", "title": str(name)}
	return list(seen.values())


def telegram_updates_url(token: str) -> str:
	if not TELEGRAM_TOKEN_RE.match(str(token or "")):
		raise ChannelError("The bot token looks like 123456789:AA... (from @BotFather)")
	return f"{TELEGRAM_API}/bot{token}/getUpdates?timeout=0"


def sample_notification(team: Any, now: datetime | None = None, url: Any = None) -> dict[str, Any]:
	return build_notification(
		team,
		TEST,
		"Nexaploy test notification",
		"This channel is set up correctly. You will receive the events you subscribed to here.",
		{"team": team},
		url,
		now,
	)
