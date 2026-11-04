"""Notification channels: pure logic (nexaploy.notify_utils) - formatting, HMAC signing,
event filtering, validation, alert transitions - plus static checks of the DocTypes, hooks,
emitters, API, portal page and translations."""

import ast
import csv
import glob
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta

import jinja2
import pytest

from nexaploy import notify_utils as nu

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "nexaploy")
DOCTYPES = os.path.join(APP, "nexaploy", "doctype")
PAGE = os.path.join(APP, "www", "dashboard", "notifications.html")
PAGE_PY = os.path.join(APP, "www", "dashboard", "notifications.py")
JS = os.path.join(APP, "public", "js", "notifications.js")
API = os.path.join(APP, "api", "notifications.py")
NOTIFY = os.path.join(APP, "notify.py")

T0 = datetime(2026, 11, 4, 9, 0, 0)
TOKEN = "123456789:AAH" + "x" * 32
SLACK_URL = "https://hooks.slack.com/services/T000/B000/XXXXXXXX"
DISCORD_URL = "https://discord.com/api/webhooks/123/abc-def"


def read(path):
	with open(path, encoding="utf-8") as fh:
		return fh.read()


def doctype(name):
	folder = name.lower().replace(" ", "_")
	return json.loads(read(os.path.join(DOCTYPES, folder, folder + ".json")))


def notif(**kw):
	base = dict(team="acme", event=nu.SITE_DOWN, title="shop.example.com is DOWN",
	            message="https://shop.example.com/api/method/ping stopped responding",
	            context={"site": "shop.example.com"}, url="https://cp.example.com/dashboard/site?name=shop.example.com", now=T0)
	base.update(kw)
	return nu.build_notification(base["team"], base["event"], base["title"], base["message"], base["context"], base["url"], base["now"])


# --------------------------------------------------------------------------- catalogue


def test_pure_module():
	assert not re.search(r"^\s*(import|from)\s+frappe", read(os.path.join(APP, "notify_utils.py")), re.M)


def test_event_catalogue():
	assert nu.EVENTS == (
		"server_down", "server_recovered", "disk_high", "container_unhealthy", "site_down", "site_recovered",
		"backup_failed", "job_failed", "invoice_issued", "payment_failed", "suspension", "ssl_expiring", "support_reply",
		"monitor_down", "monitor_up", "server_health_critical", "server_health_ok",
	)
	assert set(nu.EVENT_LABELS) == set(nu.EVENTS) == set(nu.DEFAULT_EMAIL)
	grouped = [e for _, events in nu.EVENT_GROUPS for e in events]
	assert sorted(grouped) == sorted(nu.EVENTS) and len(grouped) == len(set(grouped))
	assert set(nu.ALERT_KIND_EVENTS) == {"offline", "disk", "containers"}
	assert nu.BILLING_EVENTS <= set(nu.EVENTS) and nu.PLATFORM_COPY_EVENTS <= set(nu.EVENTS)
	assert set(nu.TYPE_FIELDS) == set(nu.CHANNEL_TYPES)


def test_user_facing_job_kinds_are_real_kinds():
	tree = ast.parse(read(os.path.join(APP, "agent_job.py")))
	kinds = next(ast.literal_eval(n.value) for n in tree.body
	             if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None) == "JOB_KINDS")
	assert nu.USER_FACING_JOB_KINDS <= set(kinds)
	# backups have their own event; diagnostics/housekeeping never notify
	for kind in ("site.backup", "site.usage", "site.get_config", "server.health", "backup.prune_remote",
	             "site.sql", "site.logs", "server.configure_backups"):
		assert kind not in nu.USER_FACING_JOB_KINDS


def test_normalize_events():
	assert nu.normalize_events(["site_down", "server_down", "site_down", ""]) == ["server_down", "site_down"]
	assert nu.normalize_events("suspension, disk_high") == ["disk_high", "suspension"]
	assert nu.normalize_events([{"event": "job_failed"}]) == ["job_failed"]
	assert nu.normalize_events(None) == []
	with pytest.raises(nu.ChannelError):
		nu.normalize_events(["nope"])


# --------------------------------------------------------------------------- event filtering


def test_channel_wants_and_select():
	channels = [
		{"name": "a", "enabled": 1, "events": ["site_down", "site_recovered"]},
		{"name": "b", "enabled": 0, "events": ["site_down"]},
		{"name": "c", "enabled": 1, "events": [{"event": "invoice_issued"}]},
		{"name": "d", "enabled": "1", "events": "site_down,disk_high"},
	]
	assert [c["name"] for c in nu.select_channels(channels, "site_down")] == ["a", "d"]
	assert [c["name"] for c in nu.select_channels(channels, "invoice_issued")] == ["c"]
	assert nu.select_channels(channels, "server_down") == []
	# the portal test reaches the channel whatever its subscriptions
	assert nu.channel_wants({"enabled": 0, "events": []}, nu.TEST)


def test_fallback_and_platform_copy():
	assert nu.fallback_email_kind("server_down", has_channels=False) == "ops"
	assert nu.fallback_email_kind("invoice_issued", has_channels=False) == "billing"
	assert nu.fallback_email_kind("job_failed", has_channels=False) is None  # never emailed before
	assert nu.fallback_email_kind("support_reply", has_channels=False) is None  # own email
	assert nu.fallback_email_kind("server_down", has_channels=True) is None
	assert nu.needs_platform_copy("server_down", True) and not nu.needs_platform_copy("server_down", False)
	assert not nu.needs_platform_copy("invoice_issued", True)
	assert nu.email_recipient_kind("payment_failed") == "billing" and nu.email_recipient_kind("site_down") == "ops"


# --------------------------------------------------------------------------- formatting


def test_build_notification():
	n = notif(context={"site": "s", "n": 3, "when": T0, "obj": {"a": 1}})
	assert n["event"] == "site_down" and n["team"] == "acme" and n["timestamp"] == "2026-11-04T09:00:00Z"
	assert n["context"] == {"site": "s", "n": 3, "when": str(T0), "obj": "{'a': 1}"}
	assert nu.build_notification(None, nu.TEST, "", "")["title"] == "Test notification"
	assert len(notif(message="x" * 10000)["message"]) == nu.MESSAGE_MAX
	with pytest.raises(ValueError):
		nu.build_notification("acme", "bogus", "t", "m")


def test_telegram_html_escaping():
	n = notif(title="<b>evil</b> & co", message="a < b > c & \"d\"", url="https://x.test/?a=1&b=\"2\"")
	text = nu.format_telegram(n)
	assert text.startswith("<b>&lt;b&gt;evil&lt;/b&gt; &amp; co</b>\n\n")
	assert "a &lt; b &gt; c &amp; &quot;d&quot;" in text
	assert '<a href="https://x.test/?a=1&amp;b=&quot;2&quot;">Open in Nexaploy</a>' in text
	# only our own tags remain
	assert set(re.findall(r"</?(\w+)", text)) == {"b", "a"}


def test_telegram_length_limit_never_cuts_entities():
	n = notif(message="&<>" * 2000)
	text = nu.format_telegram(n)
	assert len(text) <= nu.TELEGRAM_MAX
	body = text.split("\n\n")[1]
	assert not re.search(r"&[a-z]*$", body.rstrip("…"))
	assert text.endswith("Open in Nexaploy</a>")


def test_slack_discord_plain():
	n = notif(title="Site <down>", message="x & y")
	assert nu.format_slack(n) == {"text": f"*Site &lt;down&gt;*\nx &amp; y\n<{n['url']}|Open in Nexaploy>"}
	d = nu.format_discord(notif(message="@everyone look", url=None))
	assert d["content"] == "**shop.example.com is DOWN**\n@everyone look"
	assert d["allowed_mentions"] == {"parse": []}
	assert len(nu.format_discord(notif(message="z" * 5000))["content"]) == nu.DISCORD_MAX
	assert nu.format_plain(n).count("\n\n") == 2


def test_whatsapp_payloads():
	n = notif()
	text = nu.format_whatsapp(n, "97455551234")
	assert text["type"] == "text" and text["to"] == "97455551234" and text["messaging_product"] == "whatsapp"
	assert text["text"]["body"].startswith("shop.example.com is DOWN\n\n")
	tpl = nu.format_whatsapp(n, "97455551234", "nexaploy_alert", "ar")
	assert tpl["template"]["name"] == "nexaploy_alert" and tpl["template"]["language"] == {"code": "ar"}
	param = tpl["template"]["components"][0]["parameters"][0]["text"]
	assert "\n" not in param and " | " in param and len(param) <= nu.WHATSAPP_PARAM_MAX


def test_email_format():
	subject, body = nu.format_email(notif(message="a<b\nc"))
	assert subject == "[Nexaploy] shop.example.com is DOWN"
	assert body.startswith("a&lt;b<br>c<br><br><a href=")


# --------------------------------------------------------------------------- signing & requests


def test_sign_and_verify():
	body = nu.json_body({"b": 1, "a": "é"})
	assert body == '{"a":"é","b":1}'.encode()
	sig = nu.sign("s" * 16, body)
	assert sig == "sha256=" + hmac.new(b"s" * 16, body, hashlib.sha256).hexdigest()
	assert nu.verify_signature("s" * 16, body, sig)
	assert not nu.verify_signature("s" * 16, body + b" ", sig)
	assert not nu.verify_signature("t" * 16, body, sig)
	assert not nu.verify_signature("s" * 16, body, None)


def test_generic_webhook_request_signed():
	n = notif()
	req = nu.build_request({"channel_type": "Webhook", "webhook_url": "https://hooks.example.com/x", "webhook_secret": "k" * 20}, n)
	assert req["method"] == "POST" and req["url"] == "https://hooks.example.com/x"
	payload = json.loads(req["body"])
	assert set(payload) == {"event", "title", "message", "context", "url", "team", "timestamp"}
	assert payload["event"] == "site_down" and payload["team"] == "acme"
	assert req["headers"]["X-Nexaploy-Signature"] == nu.sign("k" * 20, req["body"])
	assert req["headers"]["X-Nexaploy-Event"] == "site_down"
	unsigned = nu.build_request({"channel_type": "Webhook", "webhook_url": "https://hooks.example.com/x"}, n)
	assert "X-Nexaploy-Signature" not in unsigned["headers"]


def test_chat_requests():
	n = notif()
	tg = nu.build_request({"channel_type": "Telegram", "telegram_bot_token": TOKEN, "telegram_chat_id": "-100123"}, n)
	assert tg["url"] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
	body = json.loads(tg["body"])
	assert body["chat_id"] == "-100123" and body["parse_mode"] == "HTML" and body["disable_web_page_preview"]
	wa = nu.build_request({"channel_type": "WhatsApp", "whatsapp_access_token": "EAAB", "whatsapp_phone_number_id": "1055",
	                       "whatsapp_to": "97455551234"}, n)
	assert wa["url"] == f"{nu.WHATSAPP_API}/1055/messages" and wa["headers"]["Authorization"] == "Bearer EAAB"
	slack = nu.build_request({"channel_type": "Slack", "webhook_url": SLACK_URL}, n)
	assert set(json.loads(slack["body"])) == {"text"}
	discord = nu.build_request({"channel_type": "Discord", "webhook_url": DISCORD_URL}, n)
	assert "content" in json.loads(discord["body"])
	for bad in ({"channel_type": "Telegram", "telegram_chat_id": "1"}, {"channel_type": "Slack"},
	            {"channel_type": "WhatsApp", "whatsapp_to": "1"}, {"channel_type": "Email"}):
		with pytest.raises(nu.ChannelError):
			nu.build_request(bad, n)


def test_response_error_and_redact():
	assert nu.response_error(200, '{"ok":true}', "Telegram") is None
	assert nu.response_error(200, '{"ok":false,"description":"chat not found"}', "Telegram") == "Telegram: chat not found"
	assert nu.response_error(204, "", "Discord") is None
	assert nu.response_error(400, '{"error":{"message":"Invalid OAuth access token"}}', "WhatsApp") == "HTTP 400: Invalid OAuth access token"
	assert nu.response_error(404, "no_service", "Slack") == "HTTP 404: no_service"
	assert nu.response_error(500, "") == "HTTP 500"
	err = f"HTTPSConnectionPool(host='api.telegram.org'): Max retries exceeded with url: /bot{TOKEN}/sendMessage"
	assert TOKEN not in nu.redact(err, [TOKEN, None, ""]) and "/bot***/sendMessage" in nu.redact(err, [TOKEN])
	assert nu.mask(TOKEN) == "••••" + TOKEN[-4:] and nu.mask("") == "" and nu.mask("short") == "••••"


# --------------------------------------------------------------------------- validation


def test_validate_telegram():
	out = nu.validate_channel({"channel_type": "Telegram", "telegram_bot_token": TOKEN, "telegram_chat_id": " -1001 ",
	                           "events": ["site_down"], "title": " Ops "})
	assert out == {"title": "Ops", "channel_type": "Telegram", "enabled": 1, "events": ["site_down"],
	               "telegram_bot_token": TOKEN, "telegram_chat_id": "-1001"}
	assert nu.validate_channel({"channel_type": "Telegram", "telegram_chat_id": "@my_channel", "events": "site_down"},
	                           existing_secrets={"telegram_bot_token"})["title"] == "Telegram"
	for bad in (
		{"telegram_bot_token": "nope", "telegram_chat_id": "1"},
		{"telegram_bot_token": TOKEN, "telegram_chat_id": "abc"},
		{"telegram_chat_id": "1"},  # token required on create
		{"telegram_bot_token": "*****", "telegram_chat_id": "1"},  # a mask is not a token
	):
		with pytest.raises(nu.ChannelError):
			nu.validate_channel({"channel_type": "Telegram", "events": ["site_down"], **bad})


def test_validate_whatsapp_email_and_common():
	out = nu.validate_channel({"channel_type": "WhatsApp", "whatsapp_phone_number_id": "105512345678", "whatsapp_access_token": "EAAB",
	                           "whatsapp_to": "+974 5555-1234", "events": ["server_down"], "enabled": "0"})
	assert out["whatsapp_to"] == "97455551234" and out["whatsapp_language"] == "en_US" and out["enabled"] == 0
	assert nu.normalize_phone("00974 5555 1234") == "97455551234"
	with pytest.raises(nu.ChannelError):
		nu.normalize_phone("12")
	with pytest.raises(nu.ChannelError):
		nu.validate_channel({"channel_type": "WhatsApp", "whatsapp_phone_number_id": "105512345678", "whatsapp_access_token": "x",
		                     "whatsapp_to": "97455551234", "whatsapp_template": "Bad Name", "events": ["server_down"]})
	email = nu.validate_channel({"channel_type": "Email", "email_recipients": "a@x.io; b@y.io\nA@x.io", "events": ["suspension"]})
	assert email["email_recipients"] == "a@x.io, b@y.io"
	assert nu.validate_channel({"channel_type": "Email", "events": ["suspension"]})["email_recipients"] == ""
	with pytest.raises(nu.ChannelError):
		nu.validate_channel({"channel_type": "Email", "email_recipients": "not-an-email", "events": ["suspension"]})
	with pytest.raises(nu.ChannelError):
		nu.validate_channel({"channel_type": "Email", "events": []})
	with pytest.raises(nu.ChannelError):
		nu.validate_channel({"channel_type": "Pigeon", "events": ["suspension"]})
	# fields of other types are ignored
	assert "webhook_url" not in nu.validate_channel({"channel_type": "Email", "events": ["suspension"], "webhook_url": "http://x"})


def test_validate_webhooks_ssrf():
	ok = nu.validate_channel({"channel_type": "Webhook", "webhook_url": " https://hooks.example.com:8443/n ", "events": ["job_failed"],
	                          "webhook_secret": "s" * 16})
	assert ok["webhook_url"] == "https://hooks.example.com:8443/n"
	assert nu.validate_channel({"channel_type": "Slack", "webhook_url": SLACK_URL, "events": ["job_failed"]})["webhook_url"] == SLACK_URL
	assert nu.validate_channel({"channel_type": "Discord", "webhook_url": DISCORD_URL, "events": ["job_failed"]})
	bad_urls = [
		("Webhook", "http://hooks.example.com/x"),
		("Webhook", "https://localhost/x"),
		("Webhook", "https://127.0.0.1/x"),
		("Webhook", "https://10.0.0.5/x"),
		("Webhook", "https://169.254.169.254/latest/meta-data"),
		("Webhook", "https://[::1]/x"),
		("Webhook", "https://[::ffff:127.0.0.1]/x"),
		("Webhook", "https://metadata.internal/x"),
		("Webhook", "https://intranet/x"),
		("Webhook", "https://user:pw@hooks.example.com/x"),
		("Webhook", "ftp://hooks.example.com/x"),
		("Slack", "https://hooks.example.com/services/x"),
		("Discord", "https://discord.com/channels/1"),
	]
	for ctype, url in bad_urls:
		with pytest.raises(nu.ChannelError):
			nu.validate_channel({"channel_type": ctype, "webhook_url": url, "events": ["job_failed"]})
	with pytest.raises(nu.ChannelError):
		nu.validate_channel({"channel_type": "Webhook", "webhook_url": "https://h.example.com", "webhook_secret": "short",
		                     "events": ["job_failed"]})
	# blank URL on update keeps the stored one
	kept = nu.validate_channel({"channel_type": "Webhook", "events": ["job_failed"]}, existing_secrets={"webhook_url"})
	assert "webhook_url" not in kept
	assert nu.is_public_ip("1.1.1.1") and not nu.is_public_ip("192.168.1.1") and not nu.is_public_ip("::1")
	assert not nu.is_public_ip("not-an-ip")


# --------------------------------------------------------------------------- delivery status & alerts


def test_record_result():
	assert nu.record_result({"failure_count": 3}, True, None, T0) == {"last_sent_at": T0, "last_error": "", "failure_count": 0}
	failed = nu.record_result({"failure_count": 3}, False, "boom" * 500, T0)
	assert failed["failure_count"] == 4 and failed["last_failure_at"] == T0 and len(failed["last_error"]) == nu.MAX_ERROR
	assert nu.record_result({}, False, None, T0)["last_error"] == "Unknown error"


def test_alert_transitions():
	state = {}
	alert, recovered, state = nu.alert_transitions(state, ["offline"], T0)
	assert alert == ["offline"] and recovered == [] and state[nu.ACTIVE_ALERTS_KEY] == "offline"
	# still offline 5 minutes later: throttled
	alert, recovered, state2 = nu.alert_transitions(state, ["offline"], T0 + timedelta(minutes=5))
	assert alert == [] and recovered == [] and state2 == state
	# back online -> recovered once
	alert, recovered, state3 = nu.alert_transitions(state, [], T0 + timedelta(minutes=10))
	assert recovered == ["offline"] and nu.ACTIVE_ALERTS_KEY not in state3 and state3["offline"] == state["offline"]
	alert, recovered, _ = nu.alert_transitions(state3, [], T0 + timedelta(minutes=15))
	assert alert == recovered == []
	# offline again within 6 h: throttled, and therefore no recovery message later either
	alert, recovered, state4 = nu.alert_transitions(state3, ["offline"], T0 + timedelta(hours=1))
	assert alert == [] and nu.ACTIVE_ALERTS_KEY not in state4
	assert nu.alert_transitions(state4, [], T0 + timedelta(hours=2))[1] == []
	# after 6 h it alerts again
	assert nu.alert_transitions(state4, ["offline", "disk"], T0 + timedelta(hours=6))[0] == ["offline", "disk"]
	# legacy state (no _active key, bad timestamps) still works
	assert nu.alert_transitions({"disk": "garbage"}, ["disk"], T0)[0] == ["disk"]


def test_telegram_chats():
	updates = {"ok": True, "result": [
		{"update_id": 1, "message": {"chat": {"id": 42, "type": "private", "first_name": "Ann", "last_name": "Lee"}}},
		{"update_id": 2, "my_chat_member": {"chat": {"id": -1001, "type": "supergroup", "title": "Ops"}}},
		{"update_id": 3, "channel_post": {"chat": {"id": -1002, "type": "channel", "username": "alerts"}}},
		{"update_id": 4, "message": {"chat": {"id": 42, "type": "private", "first_name": "Ann"}}},
		{"update_id": 5, "callback_query": {"id": "x"}},
		"junk",
	]}
	chats = nu.telegram_chats(updates)
	assert chats == [
		{"id": "42", "type": "private", "title": "Ann"},
		{"id": "-1002", "type": "channel", "title": "@alerts"},
		{"id": "-1001", "type": "supergroup", "title": "Ops"},
	]
	assert nu.telegram_chats({"ok": True, "result": []}) == []
	assert nu.telegram_updates_url(TOKEN).startswith(f"https://api.telegram.org/bot{TOKEN}/getUpdates")
	with pytest.raises(nu.ChannelError):
		nu.telegram_updates_url("bad")


# --------------------------------------------------------------------------- static checks


def test_doctypes():
	d = doctype("Notification Channel")
	fields = {f["fieldname"]: f for f in d["fields"]}
	assert fields["team"]["fieldtype"] == "Link" and fields["team"]["options"] == "Team" and fields["team"]["reqd"] == 1
	assert fields["channel_type"]["options"].split("\n") == list(nu.CHANNEL_TYPES)
	assert fields["enabled"]["fieldtype"] == "Check" and fields["enabled"]["default"] == "1"
	for f in nu.SECRET_FIELDS:
		assert fields[f]["fieldtype"] == "Password", f
	for fs in nu.TYPE_FIELDS.values():
		assert set(fs) <= set(fields)
	for f in ("last_sent_at", "last_error", "last_failure_at", "failure_count"):
		assert fields[f].get("read_only") == 1, f
	assert fields["events"]["fieldtype"] == "Table" and fields["events"]["options"] == "Notification Channel Event"
	child = doctype("Notification Channel Event")
	assert child["istable"] == 1
	assert child["fields"][0]["fieldname"] == "event" and child["fields"][0]["options"].split("\n") == list(nu.EVENTS)
	roles = {p["role"] for p in d["permissions"]}
	assert roles == {"System Manager", "Nexaploy Customer"}
	ctrl = read(os.path.join(DOCTYPES, "notification_channel", "notification_channel.py"))
	assert 'check_team_access(self.team, "admin")' in ctrl


def test_hooks_and_permissions():
	hooks = read(os.path.join(APP, "hooks.py"))
	assert '"Notification Channel": "nexaploy.permissions.notification_channel_query"' in hooks
	assert '"Notification Channel": "nexaploy.permissions.has_notification_channel_permission"' in hooks
	perms = read(os.path.join(APP, "permissions.py"))
	assert "ROLE_MATRIX['admin']" in perms and 'role_allows(get_team_role(team, user), "admin")' in perms


def test_dispatch_module():
	src = read(NOTIFY)
	tree = ast.parse(src)
	funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
	for name in ("notify", "deliver", "send_to_channel", "send_test", "telegram_chats", "team_channels"):
		assert name in funcs, name
	notify_src = ast.unparse(funcs["notify"])
	assert "frappe.enqueue" in notify_src and "queue='short'" in notify_src and "enqueue_after_commit=True" in notify_src
	# `event` is a frappe.enqueue parameter: the payload travels as `notification`
	assert "notification=notification" in notify_src
	send_src = ast.unparse(funcs["send_to_channel"])
	assert "frappe.log_error" in send_src and "nu.record_result" in send_src and "nu.redact" in send_src
	http = ast.unparse(funcs["_send_http"])
	assert "timeout=nu.HTTP_TIMEOUT" in http and "allow_redirects=False" in http and "_check_public" in http
	assert nu.HTTP_TIMEOUT == 10


def test_emitters_wired():
	tasks = read(os.path.join(APP, "tasks.py"))
	assert "send_alert" not in tasks
	assert "nu.alert_transitions(" in tasks and "nu.ALERT_RECOVERY_EVENTS" in tasks
	# site_down / site_recovered come from the default uptime monitor of each site (§16)
	monitoring = read(os.path.join(APP, "monitoring.py"))
	assert "check_sites_uptime" in tasks and "monitoring" in tasks
	assert "nu.SITE_DOWN" in monitoring and "nu.SITE_RECOVERED" in monitoring
	cb = read(os.path.join(APP, "agent_callbacks.py"))
	assert "_notify_job_failed(job)" in cb and "nu.USER_FACING_JOB_KINDS" in cb and "nu.BACKUP_FAILED" in cb
	billing = read(os.path.join(APP, "billing", "invoicing.py"))
	for event in ("invoice_issued", "payment_failed", "suspension"):
		assert f'"{event}"' in billing, event
	support = read(os.path.join(APP, "support.py"))
	assert '"support_reply"' in support and "email_fallback=False" in support
	assert 'team_notification_emails(ticket.team, "ops")' in support  # the support email still goes out
	assert "nu.ALERT_KIND_EVENTS[kind]" in tasks  # server_down / disk_high / container_unhealthy
	mapped = set(nu.ALERT_KIND_EVENTS.values()) | set(nu.ALERT_RECOVERY_EVENTS.values())
	for event in nu.EVENTS:
		if event == nu.SSL_EXPIRING or event in mapped:
			continue  # no emitter yet / emitted through the alert kind maps
		src = tasks + cb + billing + support + read(os.path.join(APP, "monitoring.py")) + read(os.path.join(APP, "server_maintenance.py"))
		const = "nu." + event.upper()
		assert f'"{event}"' in src or const in src, event


def test_api():
	tree = ast.parse(read(API))
	funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
	public = ("options", "list_channels", "create_channel", "update_channel", "delete_channel", "send_test", "telegram_detect_chat")
	for name in public:
		fn = funcs[name]
		assert any("whitelist" in ast.unparse(d) for d in fn.decorator_list), name
		assert not any("allow_guest" in ast.unparse(d) for d in fn.decorator_list), name
		body = ast.unparse(fn)
		assert "_ctx('admin')" in body or "_channel(" in body, name
	assert "check_team_access(doc.team, 'admin')" in ast.unparse(funcs["_channel"])
	for name in ("update_channel", "delete_channel", "send_test"):
		assert "_channel(name)" in ast.unparse(funcs[name]), name
	for name in ("create_channel", "update_channel", "delete_channel"):
		assert "_log(" in ast.unparse(funcs[name]), name
	# secrets never leave the server
	cd = ast.unparse(funcs["_channel_dict"])
	assert "PUBLIC_FIELDS" in cd
	src = read(API)
	public_fields = ast.literal_eval(next(n.value for n in tree.body if isinstance(n, ast.Assign)
	                                      and getattr(n.targets[0], "id", None) == "PUBLIC_FIELDS"))
	assert not set(public_fields) & set(nu.SECRET_FIELDS)
	assert "nu.validate_channel(" in src


def test_js_component():
	js = read(JS)
	assert 'Alpine.data("nxNotifications"' in js
	tree = ast.parse(read(API))
	whitelisted = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
	used = set(re.findall(r'call\("(\w+)"', js))
	assert used and used <= whitelisted, used - whitelisted


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_js_syntax():
	subprocess.run(["node", "--check", JS], check=True, capture_output=True)


@pytest.mark.parametrize("lang", ["en", "ar"])
@pytest.mark.parametrize("role", ["Owner", "Developer"])
def test_page_renders(lang, role):
	env = jinja2.Environment(loader=jinja2.FileSystemLoader(APP), undefined=jinja2.StrictUndefined)
	env.globals["_"] = lambda s, *a: s
	html = env.get_template("www/dashboard/notifications.html").render(
		title="Notifications", page="notifications", lang=lang, dir="rtl" if lang == "ar" else "ltr", csrf_token="tok",
		boot={"user": "a@b.c", "messages": {}}, user="a@b.c", user_fullname="A", is_admin=False,
		team="acme", team_role=role, teams=[{"name": "acme", "title": "Acme", "role": role}],
	)
	assert '<script src="/assets/nexaploy/js/notifications.js"></script>' in html
	assert 'x-data="nxNotifications"' in html
	assert html.count("<template") == html.count("</template>")
	assert "x-html" not in html
	# sidebar link for Owners/Admins only (like Activity)
	assert ('href="/dashboard/notifications"' in html) == (role == "Owner")
	page_py = read(PAGE_PY)
	assert "no_cache = 1" in page_py and "prepare_page(" in page_py


STRING_RE = re.compile(r"""(?<![A-Za-z0-9.])_{1,2}\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')\s*[,)]""")


def test_translations_cover_page_api_js_and_labels():
	ar = set()
	for path in [os.path.join(APP, "translations", "ar.csv")] + sorted(glob.glob(os.path.join(APP, "translations", "ar.*.part"))):
		with open(path, encoding="utf-8", newline="") as fh:
			ar |= {r[0] for r in csv.reader(fh) if r}
	strings = set()
	for path in (PAGE, PAGE_PY, API, os.path.join(DOCTYPES, "notification_channel", "notification_channel.py")):
		strings |= {m.group(1) or m.group(2) for m in STRING_RE.finditer(read(path))}
	js = read(JS)
	strings |= set(re.findall(r'\bt\(\s*"((?:[^"\\]|\\.)*)"\s*[,)]', js))
	hints = js[js.index("TYPE_HINTS"):js.index("};", js.index("TYPE_HINTS"))]
	strings |= set(re.findall(r'^\s+\w+: "((?:[^"\\]|\\.)*)",?$', hints, re.M))
	strings |= set(nu.EVENT_LABELS.values()) | {g for g, _ in nu.EVENT_GROUPS} | set(nu.CHANNEL_TYPES)
	strings |= set(re.findall(r'ChannelError\(\s*"([^"{]+)"\s*\)', read(os.path.join(APP, "notify_utils.py"))))
	strings |= {f"{label} is required" for label in nu.FIELD_LABELS.values() if label != "Signing secret"}
	missing = sorted(strings - ar)
	assert not missing, missing
