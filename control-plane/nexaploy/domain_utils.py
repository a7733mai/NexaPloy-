"""Custom domain verification and TLS health - pure decision logic (no frappe import).

DNS: before a custom domain is sent to the agent (`site.add_domain`), it must resolve to the
server: A/AAAA records equal to the server's public IP(s), or a CNAME (anywhere in the chain)
to the site's primary hostname. `evaluate_dns()` turns the resolver answer into ok/message;
the frappe side (nexaploy/domains.py) does the lookups against public resolvers and stores
the outcome on the Site Domain row.

Site Domain status flow:

    Pending DNS --(DNS correct)--> Verifying --(site.add_domain ok)--> Active
         |                              \\--(job failed)--> Broken
         \\--(not correct within 72 h)--> Broken  ("Verify now" starts over)

TLS: the agent reports the certificate Traefik serves per host (`site.tls_status` / heartbeat
`tls`); `evaluate_tls()` classifies it (Valid / Expiring / Expired / Default Certificate /
Pending / Untrusted / Error) and `expiry_alert_threshold()` decides when to emit the
"ssl_expiring" notification.

Messages are English templates with "{0}" placeholders (translated in translations/ar.csv);
results carry `message` (template) and `args` so the portal can render them per language.
"""

from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

# --------------------------------------------------------------------------- statuses

PENDING_DNS = "Pending DNS"
VERIFYING = "Verifying"
ACTIVE = "Active"
BROKEN = "Broken"
REMOVING = "Removing"
LEGACY_PENDING = "Pending"  # rows created before DNS verification existed
DOMAIN_STATUSES = (PENDING_DNS, VERIFYING, LEGACY_PENDING, ACTIVE, REMOVING, BROKEN)
VERIFICATION_TYPES = ("", "A", "CNAME", "Skipped")

TLS_STATUSES = ("", "Valid", "Expiring", "Expired", "Default Certificate", "Pending", "Untrusted", "Error")

DNS_GIVE_UP_HOURS = 72
TLS_GRACE_MINUTES = 15  # ACME HTTP-01 normally takes seconds; flag the default cert after this
SSL_WARN_DAYS = 14
EXPIRY_THRESHOLDS = (14, 7, 3, 1, 0)  # notify once when crossing each (days left)

TRAEFIK_DEFAULT_CN = "TRAEFIK DEFAULT CERT"

# https://www.cloudflare.com/ips/ (stable for years; used only to explain proxied records)
CLOUDFLARE_RANGES = tuple(
	ipaddress.ip_network(n)
	for n in (
		"173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18",
		"108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17",
		"162.158.0.0/15", "104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
		"2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32", "2405:8100::/32",
		"2a06:98c0::/29", "2c0f:f248::/32",
	)
)

# Common two-label public suffixes, so `shop.com.qa` / `example.co.uk` count as apex domains
# (where a CNAME is not allowed). A heuristic for the instructions only.
TWO_LABEL_SUFFIXES = frozenset(
	f"{a}.{b}"
	for b in ("uk", "au", "nz", "za", "jp", "in", "br", "tr", "sa", "eg", "qa", "ae", "kw", "bh", "om",
	          "jo", "lb", "ma", "dz", "tn", "pk", "my", "sg", "id", "ng", "ke", "il", "mx", "ar", "cn", "hk", "tw")
	for a in ("co", "com", "net", "org", "gov", "edu", "ac", "ltd", "plc", "me", "info", "biz", "sch")
)

# --------------------------------------------------------------------------- messages
# Every template a result can carry (kept here so tests can check the translations).
MSG = {
	"ok_cname": "{0} is a CNAME to {1}. DNS is correct.",
	"ok_a": "{0} points to this server ({1}). DNS is correct.",
	"skipped": "DNS verification was skipped by an administrator.",
	"lookup_failed": "DNS lookup for {0} failed: {1}. We will retry automatically.",
	"no_record": "No DNS record found for {0}. Create the record shown below.",
	"dangling_cname": "{0} is a CNAME to {1}, which does not resolve. Point it to {2} instead.",
	"cloudflare": (
		"{0} resolves to Cloudflare ({1}): the Cloudflare proxy (orange cloud) is on, so we cannot verify "
		"the record or issue a certificate. Set the record to \"DNS only\" (grey cloud) until the domain is "
		"active, or ask support to enable it in proxied mode (TLS terminated by Cloudflare, SSL mode \"Full\")."
	),
	"no_server_ip": "The server's public IP address is unknown. Create a CNAME record for {0} pointing to {1}.",
	"wrong_cname": "{0} is a CNAME to {1}, not to {2}. Change the record as shown below.",
	"extra_aaaa": (
		"{0} has an AAAA (IPv6) record {1} that does not point to this server. Let's Encrypt prefers "
		"IPv6, so remove the AAAA record (or point it to this server)."
	),
	"extra_records": "{0} also points to {1}, which is not this server. Remove those records so the certificate can be issued.",
	"wrong_ip": "{0} points to {1}, but this server is {2}. Change the record as shown below.",
	"agent_failed": "The server could not add {0}: {1}",
	"gave_up": (
		"DNS for {0} was not set up correctly within 72 hours, so we stopped checking. Fix the record "
		"and press \"Verify now\" to try again."
	),
	# TLS
	"tls_valid": "Certificate from {0}, valid until {1}.",
	"tls_pending": "The certificate for {0} is being issued.",
	"tls_default": (
		"{0} still serves the default certificate: Let's Encrypt could not issue one. Check that the DNS "
		"record points to this server, that port 80 is open to the internet (HTTP-01 challenge) and that "
		"the Let's Encrypt rate limit (5 failed validations per hour) was not hit; issuance is retried "
		"automatically."
	),
	"tls_expiring": (
		"The certificate for {0} expires in {1} days ({2}). Certificates renew 30 days before expiry, so "
		"renewal is failing: check the DNS record and that port 80 is open."
	),
	"tls_expired": "The certificate for {0} expired on {1}. Check the DNS record and that port 80 is open.",
	"tls_untrusted": "The certificate for {0} is not trusted by browsers ({1}).",
	"tls_error": "Could not check the certificate of {0}: {1}",
}


def render(message: str, args: Iterable[Any] | None = None) -> str:
	text = message
	for i, arg in enumerate(args or ()):
		text = text.replace("{" + str(i) + "}", str(arg))
	return text


def _result(ok: bool, key: str, args: list[Any], **extra) -> dict[str, Any]:
	out = {"ok": ok, "message": MSG[key], "args": [str(a) for a in args], "code": key}
	out["text"] = render(out["message"], out["args"])
	out.setdefault("verification_type", "")
	out.setdefault("proxied", False)
	out.setdefault("retry", True)
	out.update(extra)
	return out


# --------------------------------------------------------------------------- hosts & IPs


def normalize_host(name: Any) -> str:
	return str(name or "").strip().lower().rstrip(".")


def _ip(value: Any):
	try:
		return ipaddress.ip_address(str(value).strip().split("%", 1)[0])
	except ValueError:
		return None


def parse_ip_list(text: Any) -> list[str]:
	"""'1.2.3.4, 2a01::1' (commas/whitespace/newlines; or a list) -> valid unique IPs, in order."""
	if not text:
		return []
	items = text if isinstance(text, (list, tuple)) else str(text).replace(",", " ").split()
	out: list[str] = []
	for item in items:
		ip = _ip(item)
		if ip is not None and str(ip) not in out:
			out.append(str(ip))
	return out


def server_dns_targets(override=None, public_ipv4=None, public_ipv6=None, facts_public_ips=None,
                       fallback_ip=None) -> list[str]:
	"""IPs a custom domain's A/AAAA records must point to.

	The operator's override (Server.dns_target_ips: floating IP, load balancer, NAT) wins.
	Otherwise: provisioning fields (public_ipv4/public_ipv6) + the agent's `public_ips` fact
	(global source addresses), else `fallback_ip` (the address the agent connects from) when
	it is a public address.
	"""
	manual = parse_ip_list(override)
	if manual:
		return manual
	out = parse_ip_list([x for x in (public_ipv4, public_ipv6) if x])
	for ip in parse_ip_list(facts_public_ips or []):
		if _ip(ip).is_global and ip not in out:
			out.append(ip)
	if not out and fallback_ip:
		ip = _ip(fallback_ip)
		if ip is not None and ip.is_global:
			out.append(str(ip))
	return out


def is_cloudflare_ip(value: Any) -> bool:
	ip = _ip(value)
	return ip is not None and any(ip.version == n.version and ip in n for n in CLOUDFLARE_RANGES)


def is_apex(domain: str) -> bool:
	"""True for a registrable domain (example.com, shop.com.qa), where CNAME records are not allowed."""
	labels = normalize_host(domain).split(".")
	if len(labels) <= 2:
		return True
	return len(labels) == 3 and ".".join(labels[-2:]) in TWO_LABEL_SUFFIXES


def dns_instructions(domain: str, primary_host: str, targets: list[str]) -> list[dict[str, Any]]:
	"""The records the customer can create (one of them is enough), recommended first.

	Subdomain -> CNAME to the primary hostname (follows server moves); apex -> A/AAAA.
	"""
	domain, primary = normalize_host(domain), normalize_host(primary_host)
	apex = is_apex(domain)
	records: list[dict[str, Any]] = []
	if not apex:
		records.append({"type": "CNAME", "name": domain, "value": primary, "recommended": True})
	v4 = [ip for ip in targets if _ip(ip).version == 4]
	v6 = [ip for ip in targets if _ip(ip).version == 6]
	for ip in v4:
		records.append({"type": "A", "name": domain, "value": ip, "recommended": apex})
	for ip in v6:
		records.append({"type": "AAAA", "name": domain, "value": ip, "recommended": apex and not v4})
	return records


# --------------------------------------------------------------------------- DNS decision


def evaluate_dns(domain: str, primary_host: str, targets: list[str], answer: dict[str, Any]) -> dict[str, Any]:
	"""Decide whether `domain` points to the site.

	answer: {"cname": [chain of CNAME targets], "a": [...], "aaaa": [...], "error": str|None}
	Returns {ok, verification_type (A/CNAME/""), proxied, retry, code, message, args, text}.
	"""
	domain, primary = normalize_host(domain), normalize_host(primary_host)
	chain = [normalize_host(c) for c in (answer.get("cname") or []) if c]
	a = parse_ip_list(answer.get("a") or [])
	aaaa = parse_ip_list(answer.get("aaaa") or [])
	addrs = a + aaaa
	targets = parse_ip_list(targets or [])

	if primary and primary in chain:
		return _result(True, "ok_cname", [domain, primary], verification_type="CNAME")
	if answer.get("error") and not addrs:
		return _result(False, "lookup_failed", [domain, answer["error"]])
	if not addrs:
		if chain:
			return _result(False, "dangling_cname", [domain, chain[-1], primary])
		return _result(False, "no_record", [domain])

	cloudflare = [ip for ip in addrs if is_cloudflare_ip(ip) and ip not in targets]
	if cloudflare:
		return _result(False, "cloudflare", [domain, ", ".join(cloudflare)], proxied=True)
	if not targets:
		if chain:
			return _result(False, "wrong_cname", [domain, chain[0], primary])
		return _result(False, "no_server_ip", [domain, primary])

	right = [ip for ip in addrs if ip in targets]
	wrong = [ip for ip in addrs if ip not in targets]
	if not wrong:
		return _result(True, "ok_a", [domain, ", ".join(right)], verification_type="A")
	if right:
		wrong_v6 = [ip for ip in wrong if ip in aaaa]
		if wrong_v6 and len(wrong_v6) == len(wrong):
			return _result(False, "extra_aaaa", [domain, ", ".join(wrong_v6)])
		return _result(False, "extra_records", [domain, ", ".join(wrong)])
	if chain:
		return _result(False, "wrong_cname", [domain, chain[0], primary])
	return _result(False, "wrong_ip", [domain, ", ".join(wrong), ", ".join(targets)])


def skipped_result() -> dict[str, Any]:
	return _result(True, "skipped", [], verification_type="Skipped")


def gave_up_result(domain: str) -> dict[str, Any]:
	return _result(False, "gave_up", [normalize_host(domain)], retry=False)


# --------------------------------------------------------------------------- scheduling


def check_interval(age: timedelta) -> timedelta:
	"""How often a Pending DNS domain is re-checked, by time since it was requested."""
	if age < timedelta(hours=1):
		return timedelta(minutes=5)
	if age < timedelta(hours=6):
		return timedelta(minutes=15)
	if age < timedelta(hours=24):
		return timedelta(minutes=30)
	return timedelta(hours=1)


def dns_check_due(requested_at: datetime | None, last_checked_at: datetime | None, now: datetime) -> bool:
	"""Scheduler (every 5 min): is this Pending DNS domain due for another lookup?"""
	if last_checked_at is None or requested_at is None:
		return True
	interval = check_interval(now - requested_at)
	# 30 s slack: the cron fires every 5 minutes, not exactly 300 s after the last check
	return now - last_checked_at >= interval - timedelta(seconds=30)


def dns_gave_up(requested_at: datetime | None, now: datetime, hours: int = DNS_GIVE_UP_HOURS) -> bool:
	return requested_at is not None and now - requested_at >= timedelta(hours=hours)


# --------------------------------------------------------------------------- TLS


def parse_utc(value: Any) -> datetime | None:
	"""ISO-8601 (Z/offset/naive=UTC) -> naive UTC datetime."""
	if not value:
		return None
	if isinstance(value, datetime):
		dt = value
	else:
		text = str(value).strip()
		if text.endswith("Z"):
			text = text[:-1] + "+00:00"
		try:
			dt = datetime.fromisoformat(text)
		except ValueError:
			return None
	if dt.tzinfo is not None:
		dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
	return dt


def is_default_cert(info: dict[str, Any]) -> bool:
	if info.get("default_cert"):
		return True
	return TRAEFIK_DEFAULT_CN in (info.get("subject_cn"), info.get("issuer_cn"))


def evaluate_tls(host: str, info: dict[str, Any] | None, now_utc: datetime,
                 active_since: datetime | None = None, now_local: datetime | None = None,
                 warn_days: int = SSL_WARN_DAYS) -> dict[str, Any] | None:
	"""Classify one host's certificate report (agent `tls.hosts[host]`).

	`active_since` / `now_local`: when the route for the host went live (system time, e.g.
	Site Domain.activated_at) - the default certificate is only flagged after TLS_GRACE_MINUTES.
	Returns {status, message, args, text, expires_on (naive UTC), issuer, days_left}.
	"""
	if not isinstance(info, dict):
		return None
	host = normalize_host(host)
	expires = parse_utc(info.get("not_after"))
	issuer = info.get("issuer") or info.get("issuer_cn") or None
	base = {"expires_on": expires, "issuer": issuer, "days_left": None}

	def out(status, key, args):
		args = [str(a) for a in args]
		return dict(base, status=status, message=MSG[key], args=args, text=render(MSG[key], args), code=key)

	if info.get("error") and not expires:
		return out("Error", "tls_error", [host, info["error"]])
	if is_default_cert(info):
		base["issuer"] = None
		base["expires_on"] = None
		recent = (
			active_since is not None and now_local is not None
			and now_local - active_since < timedelta(minutes=TLS_GRACE_MINUTES)
		)
		return out("Pending" if recent else "Default Certificate", "tls_pending" if recent else "tls_default", [host])
	if expires is None:
		return out("Error", "tls_error", [host, info.get("verify_error") or "no certificate"])
	days_left = int((expires - now_utc).total_seconds() // 86400)
	base["days_left"] = days_left
	date = expires.strftime("%Y-%m-%d")
	if expires <= now_utc:
		return out("Expired", "tls_expired", [host, date])
	if not info.get("trusted"):
		return out("Untrusted", "tls_untrusted", [host, info.get("verify_error") or issuer or "unknown issuer"])
	if days_left < warn_days:
		return out("Expiring", "tls_expiring", [host, days_left, date])
	return out("Valid", "tls_valid", [issuer or "?", date])


def agent_failed_result(domain: str, error: Any) -> dict[str, Any]:
	return _result(False, "agent_failed", [normalize_host(domain), str(error or "unknown error")[:300]], retry=False)


def expiry_alert_threshold(days_left: int | None, last_threshold: int | None) -> int | None:
	"""Threshold (days) to notify "ssl_expiring" for now, or None.

	One notification per threshold crossed (14, 7, 3, 1, 0 days left); `last_threshold` is the
	lowest one already notified for the current certificate (None/negative after a renewal).
	"""
	if days_left is None:
		return None
	crossed = [t for t in EXPIRY_THRESHOLDS if days_left <= t]
	if not crossed:
		return None
	threshold = min(crossed)
	if last_threshold is not None and last_threshold >= 0 and threshold >= last_threshold:
		return None
	return threshold


def marker_to_threshold(marker: Any) -> int | None:
	"""Stored Int field (0 = nothing notified, else threshold + 1) -> threshold or None."""
	try:
		value = int(marker or 0)
	except (TypeError, ValueError):
		return None
	return value - 1 if value > 0 else None


def threshold_to_marker(threshold: int | None) -> int:
	return 0 if threshold is None else int(threshold) + 1


def reset_expiry_alert(days_left: int | None) -> bool:
	"""A renewed certificate (more than SSL_WARN_DAYS left) re-arms the expiry alerts."""
	return days_left is not None and days_left >= SSL_WARN_DAYS
