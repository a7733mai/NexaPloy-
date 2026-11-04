"""Pure logic of the platform Administrator dashboard (/dashboard/admin, nexaploy.api.admin).

No frappe import: aggregations for the overview, whitelists + coercion of every settings form,
secret masking and the parameters of a Storage Profile connection test. The glue in
nexaploy/api/admin.py loads rows, calls these and saves documents.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from nexaploy import agent_release_utils as aru
from nexaploy.storage_utils import backup_settings_from_profile

MASK = "••••"
CLEAR = "__clear__"  # a secret sent as this value is removed
DISK_ALERT_PERCENT = 85
HEARTBEAT_STALE_MINUTES = 10
OPEN_TICKET_STATUSES = ("Open", "Waiting on Customer", "Waiting on Support")
PLAN_TYPES = ("Server Hosted", "Server BYOS", "Site", "Add-on")
OVERAGE_ACTIONS = ("Notify", "Throttle", "Suspend")
CREDIT_TYPES = ("Promo", "Refund", "Adjustment")
MAX_CREDIT_GRANT = Decimal("100000")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
PLAN_TITLE_RE = re.compile(r"^[^<>\r\n]{2,80}$")
EMAIL_RE = re.compile(r"^[^\s<>@,;\"]+@[^\s<>@,;\"]+\.[^\s<>@,;\"]+$")
HTTPS_URL_RE = re.compile(r"^https://[^\s\"'<>]+$")

# --------------------------------------------------------------------------- form specs
# field -> type: bool | int | float | str | "email" | "url" | "currency" | tuple(choices)

PLATFORM_FIELDS: dict[str, Any] = {
	"alert_email": "email",
	"registry_url": str,  # normalised by Nexaploy Settings.validate
	"registry_username": str,
	"allow_customer_agent_upgrade": bool,
	"agent_upgrade_timeout_minutes": int,
	"agent_repo_url": "url",
	"backup_retention_min_days": int,
	"backup_retention_max_days": int,
	"backup_retention_included_days": int,
	"backup_local_retention_max_days": int,
	"backup_keep_monthly_max": int,
}
PLATFORM_SECRETS = ("registry_password",)

OAUTH_FIELDS: dict[str, Any] = {
	"google_client_id": str,
	"microsoft_client_id": str,
	"microsoft_tenant": str,
	"dropbox_app_key": str,
}
OAUTH_SECRETS = ("google_client_secret", "microsoft_client_secret", "dropbox_app_secret")

BILLING_FIELDS: dict[str, Any] = {
	"enabled": bool,
	"currency": "currency",
	"tax_percent": float,
	"invoice_day": int,
	"grace_days": int,
	"suspend_after_days": int,
	"default_byos_plan": str,
	"default_site_plan": str,
	"enable_stripe": bool,
	"enable_paymob": bool,
	"return_url": "url",
	"stripe_publishable_key": str,
	"paymob_integration_id": str,
	"paymob_iframe_id": str,
	"paymob_currency": "currency",
	"paymob_exchange_rate": float,
	"paymob_base_url": "url",
	"signup_credits": float,
	"min_topup": float,
	"max_topup": float,
}
BILLING_SECRETS = ("stripe_secret_key", "stripe_webhook_secret", "paymob_api_key", "paymob_hmac_secret")

EMAIL_FIELDS: dict[str, Any] = {
	"enabled": bool,
	"provider": str,
	"region": str,
	"smtp_host": str,
	"smtp_port": int,
	"tls_mode": str,
	"username": str,
	"verify_smtp": bool,
	"default_from_domain": str,
	"daily_send_limit": int,
	"spf_include": str,
	"dkim_selector": str,
	"dkim_record_type": ("TXT", "CNAME"),
	"dkim_value": str,
	"dmarc_policy": ("none", "quarantine", "reject"),
	"dmarc_rua": str,
	"return_path_host": str,
	"return_path_value": str,
}
EMAIL_SECRETS = ("password",)

PLAN_FIELDS: dict[str, Any] = {
	"title": str,
	"plan_type": PLAN_TYPES,
	"enabled": bool,
	"price_monthly": float,
	"currency": "currency",
	"max_sites": int,
	"plan_order": int,
	"cpu_seconds_per_day": int,
	"overage_action": OVERAGE_ACTIONS,
	"max_storage_mb": int,
	"max_database_mb": int,
	"description": str,
}

BRAND_FIELDS: dict[str, Any] = {
	"brand_name": str,
	"title": str,
	"enabled": bool,
	"team": str,
	"is_default": bool,
	"primary_color": str,
	"logo": str,
	"favicon": str,
	"portal_domains": str,
	"support_email": "email",
	"footer_text": str,
	"email_sender_name": str,
	"email_sender_address": "email",
}

TEAM_FLAGS = ("billing_exempt", "extended_backup_retention", "enabled")

RELEASE_FIELDS: dict[str, Any] = {
	"version": str,
	"channel": aru.CHANNELS,
	"git_ref": str,
	"repo_url": "url",
	"wheel_url": "url",
	"wheel_sha256": str,
	"published": bool,
	"release_notes": str,
}


class AdminError(ValueError):
	"""A rejected admin form value: `message` is a translatable source string with {0}
	placeholders filled from `params` (the glue translates, then formats)."""

	def __init__(self, message: str, *params: Any):
		super().__init__(message.format(*params) if params else message)
		self.message = message
		self.params = params


def _bool(value: Any) -> int:
	return 1 if value in (True, 1, "1", "true", "True", "on", "yes") else 0


def clean_fields(values: Mapping[str, Any] | None, spec: Mapping[str, Any]) -> dict[str, Any]:
	"""Whitelist + coerce `values` against `spec`. Unknown keys raise AdminError (the browser
	can never set a field the form does not offer, e.g. a hash or a secret)."""
	out: dict[str, Any] = {}
	for key, value in (values or {}).items():
		if key not in spec:
			raise AdminError("Unknown setting {0}", key)
		kind = spec[key]
		if kind is bool:
			out[key] = _bool(value)
		elif kind is int:
			try:
				out[key] = int(value) if value not in (None, "") else 0
			except (TypeError, ValueError):
				raise AdminError("{0} must be a number", key) from None
			if out[key] < 0:
				raise AdminError("{0} cannot be negative", key)
		elif kind is float:
			try:
				out[key] = float(value) if value not in (None, "") else 0.0
			except (TypeError, ValueError):
				raise AdminError("{0} must be a number", key) from None
			if out[key] < 0:
				raise AdminError("{0} cannot be negative", key)
		elif isinstance(kind, tuple):
			text = str(value or "").strip()
			if text not in kind:
				raise AdminError("{0} must be one of {1}", key, ", ".join(kind))
			out[key] = text
		else:
			text = str(value).strip() if value not in (None, "") else ""
			if text and kind == "email" and not EMAIL_RE.match(text):
				raise AdminError("{0} must be an e-mail address", key)
			if text and kind == "url" and not HTTPS_URL_RE.match(text):
				raise AdminError("{0} must be an https URL", key)
			if kind == "currency":
				text = text.upper()
				if text and not CURRENCY_RE.match(text):
					raise AdminError("{0} must be a 3-letter currency code", key)
			if len(text) > 5000:
				raise AdminError("{0} is too long", key)
			out[key] = text or None
	return out


def split_secrets(secrets: Mapping[str, Any] | None, allowed: Iterable[str]) -> tuple[dict[str, str], list[str]]:
	"""Write-only secrets: ({field: new value}, [fields to clear]). Empty = keep the stored
	value; CLEAR removes it. Unknown fields raise AdminError."""
	allowed = tuple(allowed)
	set_: dict[str, str] = {}
	clear: list[str] = []
	for key, value in (secrets or {}).items():
		if key not in allowed:
			raise AdminError("Unknown secret {0}", key)
		if value in (None, ""):
			continue
		if value == CLEAR:
			clear.append(key)
			continue
		text = str(value).strip()
		if not text or len(text) > 4000 or "\n" in text:
			raise AdminError("Invalid value for {0}", key)
		set_[key] = text
	return set_, clear


def mask_secret(value: Any) -> str:
	"""Hint for a stored secret: '' when unset, '••••' + the last 4 characters for long
	values, else just '••••'. The full value never leaves the server."""
	text = str(value or "")
	if not text:
		return ""
	return MASK + text[-4:] if len(text) >= 16 else MASK


def secret_states(values: Mapping[str, Any], fields: Iterable[str]) -> dict[str, dict[str, Any]]:
	"""{field: {set, hint}} for the (decrypted) secret values in `values`."""
	return {f: {"set": bool(values.get(f)), "hint": mask_secret(values.get(f))} for f in fields}


def check_retention_limits(values: Mapping[str, Any]) -> None:
	lo = int(values.get("backup_retention_min_days") or 0)
	hi = int(values.get("backup_retention_max_days") or 0)
	inc = int(values.get("backup_retention_included_days") or 0)
	if hi and lo > hi:
		raise AdminError("Minimum retention cannot exceed the maximum")
	if hi and inc > hi:
		raise AdminError("Included retention cannot exceed the maximum")


def check_billing(values: Mapping[str, Any]) -> None:
	if "tax_percent" in values and values["tax_percent"] > 100:
		raise AdminError("Tax must be between 0 and 100 %")
	if "invoice_day" in values and not 1 <= values["invoice_day"] <= 31:
		raise AdminError("Invoice day must be between 1 and 31")
	lo, hi = values.get("min_topup"), values.get("max_topup")
	if lo and hi and lo > hi:
		raise AdminError("Minimum top-up cannot exceed the maximum")


def clean_plan(values: Mapping[str, Any] | None, new: bool = False) -> dict[str, Any]:
	out = clean_fields(values, PLAN_FIELDS)
	if new:
		if not out.get("title"):
			raise AdminError("Plan title is required")
		if not out.get("plan_type"):
			raise AdminError("Plan type is required")
	if "title" in out and (not out["title"] or not PLAN_TITLE_RE.match(out["title"])):
		raise AdminError("Plan title must be 2-80 characters")
	return out


def clean_credit_grant(amount: Any, tx_type: Any = "Promo") -> tuple[Decimal, str]:
	tx_type = str(tx_type or "Promo")
	if tx_type not in CREDIT_TYPES:
		raise AdminError("Credit type must be Promo, Refund or Adjustment")
	try:
		value = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
	except (InvalidOperation, ValueError):
		raise AdminError("Amount must be a number") from None
	if value == 0:
		raise AdminError("Amount must not be zero")
	if tx_type != "Adjustment" and value < 0:
		raise AdminError("Only an adjustment can remove credits")
	if abs(value) > MAX_CREDIT_GRANT:
		raise AdminError("Amount is too large")
	return value, tx_type


# --------------------------------------------------------------------------- overview


def _dt(value: Any) -> datetime | None:
	if isinstance(value, datetime):
		return value
	if not value:
		return None
	try:
		return datetime.fromisoformat(str(value).replace("Z", "").split("+")[0])
	except ValueError:
		return None


def latest_by_channel(releases: Iterable[Mapping[str, Any]]) -> dict[str, str | None]:
	releases = list(releases)
	out = {}
	for ch in aru.CHANNELS:
		r = aru.latest_release(releases, ch)
		out[ch] = r["version"] if r else None
	return out


def agent_rows(servers: Iterable[Mapping[str, Any]], releases: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
	"""Per server: version, channel, latest of its channel, outdated (non-archived only)."""
	latest = latest_by_channel(releases)
	rows = []
	for s in servers:
		if s.get("status") == "Archived":
			continue
		ch = s.get("agent_channel") or "stable"
		target = latest.get(ch)
		rows.append({
			"name": s.get("name"),
			"hostname": s.get("hostname"),
			"team": s.get("team"),
			"status": s.get("status"),
			"agent_version": s.get("agent_version"),
			"channel": ch,
			"latest": target,
			"outdated": bool(target) and aru.is_older(s.get("agent_version"), target),
			"upgrade_status": s.get("agent_upgrade_status") or "",
			"upgrade_error": s.get("agent_upgrade_error"),
		})
	return rows


def server_summary(servers: Iterable[Mapping[str, Any]], releases: Iterable[Mapping[str, Any]], now: datetime,
                   disk_alert: float = DISK_ALERT_PERCENT, stale_minutes: int = HEARTBEAT_STALE_MINUTES) -> dict[str, Any]:
	"""Counts and alerts over Server rows (archived servers ignored)."""
	servers = [s for s in servers if s.get("status") != "Archived"]
	by_status: dict[str, int] = {}
	online = offline = 0
	disk = []
	stale_before = now - timedelta(minutes=stale_minutes)
	for s in servers:
		st = s.get("status") or "Unknown"
		by_status[st] = by_status.get(st, 0) + 1
		hb = _dt(s.get("last_heartbeat"))
		if st == "Active" and hb is not None and hb >= stale_before:
			online += 1
		elif st in ("Active", "Offline"):
			offline += 1
		pct = s.get("disk_used_percent")
		if pct not in (None, "") and float(pct) >= disk_alert:
			disk.append({"name": s.get("name"), "hostname": s.get("hostname"), "disk_used_percent": round(float(pct), 1)})
	agents = agent_rows(servers, releases)
	versions: dict[str, int] = {}
	for a in agents:
		key = a["agent_version"] or "?"
		versions[key] = versions.get(key, 0) + 1
	disk.sort(key=lambda r: -r["disk_used_percent"])
	return {
		"total": len(servers),
		"by_status": by_status,
		"online": online,
		"offline": offline,
		"disk_alerts": disk,
		"outdated_agents": sum(1 for a in agents if a["outdated"]),
		"agent_versions": versions,
		"latest": latest_by_channel(releases),
	}


def site_summary(sites: Iterable[Mapping[str, Any]], bench_major: Mapping[str, Any]) -> dict[str, Any]:
	"""Sites by status and by Frappe major (via the bench's release group). Archived sites
	count by status only."""
	by_status: dict[str, int] = {}
	by_major: dict[str, int] = {}
	total = 0
	for s in sites:
		st = s.get("status") or "Unknown"
		by_status[st] = by_status.get(st, 0) + 1
		if st == "Archived":
			continue
		total += 1
		major = bench_major.get(s.get("bench")) if s.get("bench") else None
		key = f"v{major}" if major else "?"
		by_major[key] = by_major.get(key, 0) + 1
	return {"total": total, "by_status": by_status, "by_major": dict(sorted(by_major.items()))}


def failed_jobs(jobs: Iterable[Mapping[str, Any]], limit: int = 25) -> dict[str, Any]:
	"""Failed Agent Jobs (already filtered to the window): count, per kind, newest first."""
	jobs = sorted(jobs, key=lambda j: _dt(j.get("creation")) or datetime.min, reverse=True)
	by_kind: dict[str, int] = {}
	for j in jobs:
		by_kind[j.get("kind") or "?"] = by_kind.get(j.get("kind") or "?", 0) + 1
	items = [
		{
			"name": j.get("name"),
			"kind": j.get("kind"),
			"server": j.get("server"),
			"reference_doctype": j.get("reference_doctype"),
			"reference_name": j.get("reference_name"),
			"error": (str(j.get("error") or "").strip().splitlines() or [""])[-1][:300],
			"creation": j.get("creation"),
			"url": "/app/agent-job/" + str(j.get("name")),
		}
		for j in jobs[:limit]
	]
	return {"count": len(jobs), "by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1])), "items": items}


def _money(value: Any) -> Decimal:
	try:
		return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
	except InvalidOperation:
		return Decimal("0.00")


def mrr(subscriptions: Iterable[Mapping[str, Any]], exempt_teams: Iterable[str] = (), default_currency: str = "USD"
        ) -> dict[str, Any]:
	"""Monthly recurring revenue from Active subscriptions, per currency (billing-exempt
	teams excluded). Returns {by_currency: {USD: 120.0}, subscriptions: n}."""
	exempt = set(exempt_teams or ())
	totals: dict[str, Decimal] = {}
	count = 0
	for s in subscriptions:
		if s.get("status") != "Active" or s.get("team") in exempt:
			continue
		cur = (s.get("currency") or default_currency or "USD").upper()
		totals[cur] = totals.get(cur, Decimal("0")) + _money(s.get("price_monthly"))
		count += 1
	return {"by_currency": {k: float(v) for k, v in sorted(totals.items())}, "subscriptions": count}


def overdue_summary(invoices: Iterable[Mapping[str, Any]], default_currency: str = "USD") -> dict[str, Any]:
	totals: dict[str, Decimal] = {}
	teams = set()
	count = 0
	for inv in invoices:
		if inv.get("status") != "Overdue":
			continue
		count += 1
		teams.add(inv.get("team"))
		cur = (inv.get("currency") or default_currency or "USD").upper()
		totals[cur] = totals.get(cur, Decimal("0")) + _money(inv.get("amount_due"))
	return {"count": count, "teams": len(teams), "by_currency": {k: float(v) for k, v in sorted(totals.items())}}


def ticket_summary(tickets: Iterable[Mapping[str, Any]], now: datetime) -> dict[str, Any]:
	by_status: dict[str, int] = {}
	by_priority: dict[str, int] = {}
	overdue = 0
	for t in tickets:
		st = t.get("status")
		if st not in OPEN_TICKET_STATUSES:
			continue
		by_status[st] = by_status.get(st, 0) + 1
		pr = t.get("priority") or "Normal"
		by_priority[pr] = by_priority.get(pr, 0) + 1
		due = _dt(t.get("first_response_due"))
		if due and not t.get("first_response_at") and due < now:
			overdue += 1
	return {"open": sum(by_status.values()), "by_status": by_status, "by_priority": by_priority,
	        "first_response_overdue": overdue}


# --------------------------------------------------------------------------- storage profile test


def profile_test_params(profile: Mapping[str, Any], secret: str, remote: str = "nexaploy-backups") -> dict[str, Any]:
	"""`backup.test_remote` params that check a Storage Profile's bucket is reachable and
	writable from a throw-away rclone config (the server's backup remote is not touched)."""
	b = backup_settings_from_profile(profile)
	prefix = (b.get("backup_prefix") or "") + "/.nexaploy-test" if b.get("backup_prefix") else ".nexaploy-test"
	dest = {
		"remote": remote,
		"type": "s3",
		"prefix": prefix,
		"provider": b["backup_provider"],
		"endpoint": b["backup_endpoint"],
		"region": b["backup_region"],
		"bucket": b["backup_bucket"],
		"access_key_id": b["backup_access_key"],
		"secret_access_key": secret,
	}
	return {"destination": dest, "prefix": prefix}


def may_test_on(server: Mapping[str, Any], team: str | None) -> bool:
	"""A profile's credentials only go to servers allowed to hold them: a team bucket to an
	Active server of that team, a platform bucket to an Active platform (Hosted) server -
	never to a customer's BYOS machine."""
	if server.get("status") != "Active":
		return False
	if team:
		return server.get("team") == team
	return bool(server.get("hosts_control_plane")) or server.get("ownership") == "Hosted"


def pick_test_server(servers: Iterable[Mapping[str, Any]], team: str | None) -> str | None:
	"""Server to run a profile test on: the control-plane host first, else the first allowed."""
	allowed = [s for s in servers if may_test_on(s, team)]
	allowed.sort(key=lambda s: (not s.get("hosts_control_plane"), str(s.get("name") or "")))
	return allowed[0]["name"] if allowed else None


def change_summary(before: Mapping[str, Any], after: Mapping[str, Any], secrets: Iterable[str] = ()) -> dict[str, Any]:
	"""{field: {from, to}} for changed fields; secrets are reported as changed without values."""
	secret = set(secrets or ())
	out: dict[str, Any] = {}
	for key, new in after.items():
		old = before.get(key)
		if key in secret:
			out[key] = "changed"
			continue
		if (old in (None, "") and new in (None, "")) or old == new:
			continue
		try:
			if old is not None and new is not None and float(old) == float(new):
				continue
		except (TypeError, ValueError):
			pass
		out[key] = {"from": old, "to": new}
	return out
