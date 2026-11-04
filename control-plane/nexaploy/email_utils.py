"""Pure helpers for the managed e-mail relay (no frappe import; tests/test_email_branding.py).

* provider presets (SMTP host/port, SPF include) for Generic SMTP / Amazon SES / Postmark /
  Mailgun / Brevo;
* the sender address of a site and the ``site.configure_email`` job params;
* the SPF / DKIM / DMARC (and optional return-path) DNS records a customer adds for a custom
  sending domain, and checking them against what DNS returns.

Provider APIs (SES identities, Postmark sender signatures, Mailgun domains) are out of scope: the
operator registers the domain with the provider and enters the DKIM selector/value it shows;
``{domain}`` in those values is replaced by the customer's domain.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Mapping

PROVIDERS = ("Generic SMTP", "Amazon SES", "Postmark", "Mailgun", "Brevo")
TLS_MODES = ("STARTTLS", "SSL/TLS", "None")
JOB_TLS = {"STARTTLS": "starttls", "SSL/TLS": "ssl", "None": "none"}
DEFAULT_PORTS = {"STARTTLS": 587, "SSL/TLS": 465, "None": 25}
ACCOUNT_NAME = "Nexaploy Relay"
DEFAULT_LOCAL_PART = "noreply"

# host may use {region}; spf is the SPF mechanism to include; dkim_type the record type the
# provider asks for; return_path = (host label, CNAME target) when the provider uses one.
PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
	"Generic SMTP": {"host": "", "port": 587, "tls": "STARTTLS", "spf": "", "dkim_type": "TXT", "dkim_selector": "",
	                 "return_path": None},
	"Amazon SES": {"host": "email-smtp.{region}.amazonaws.com", "port": 587, "tls": "STARTTLS",
	               "spf": "include:amazonses.com", "dkim_type": "CNAME", "dkim_selector": "",
	               "return_path": None},
	"Postmark": {"host": "smtp.postmarkapp.com", "port": 587, "tls": "STARTTLS", "spf": "include:spf.mtasv.net",
	             "dkim_type": "TXT", "dkim_selector": "", "return_path": ("pm-bounces", "pm.mtasv.net")},
	"Mailgun": {"host": "smtp.mailgun.org", "port": 587, "tls": "STARTTLS", "spf": "include:mailgun.org",
	            "dkim_type": "TXT", "dkim_selector": "smtp", "return_path": ("email", "mailgun.org")},
	"Brevo": {"host": "smtp-relay.brevo.com", "port": 587, "tls": "STARTTLS", "spf": "include:spf.brevo.com",
	          "dkim_type": "TXT", "dkim_selector": "mail", "return_path": None},
}

DMARC_POLICIES = ("none", "quarantine", "reject")

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
DOMAIN_RE = re.compile(rf"^(?=.{{4,253}}$)(?:{_LABEL}\.)+[a-z]{{2,63}}$")
SELECTOR_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62})$", re.I)
EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@" + rf"(?:{_LABEL}\.)+[a-z]{{2,63}}$")
SPF_MECH_RE = re.compile(r"^(?:include|a|mx|ip4|ip6|exists|redirect)[:=]?[A-Za-z0-9.:/_%{}-]*$")


# --------------------------------------------------------------------------- settings


def normalize_provider(value: Any) -> str:
	text = str(value or "").strip()
	return text if text in PROVIDERS else "Generic SMTP"


def normalize_tls_mode(value: Any) -> str:
	text = str(value or "").strip()
	aliases = {"starttls": "STARTTLS", "tls": "STARTTLS", "ssl": "SSL/TLS", "ssl/tls": "SSL/TLS", "none": "None",
	           "": "STARTTLS"}
	if text in TLS_MODES:
		return text
	try:
		return aliases[text.lower()]
	except KeyError:
		raise ValueError("TLS mode must be STARTTLS, SSL/TLS or None") from None


def normalize_domain(value: Any) -> str:
	"""'  Mail.Example.COM. ' -> 'mail.example.com'; ValueError when not a domain."""
	text = str(value or "").strip().lower().strip(".")
	if text.startswith("http://") or text.startswith("https://"):
		text = text.split("://", 1)[1].split("/", 1)[0]
	if not DOMAIN_RE.match(text):
		raise ValueError("Enter a valid domain, e.g. example.com")
	return text


def is_valid_email(value: Any) -> bool:
	text = str(value or "")
	local, _, domain = text.partition("@")
	return bool(EMAIL_RE.match(f"{local}@{domain.lower()}"))


def provider_defaults(provider: Any, region: Any = None) -> dict[str, Any]:
	"""Preset for a provider with `{region}` filled (SES default us-east-1)."""
	preset = dict(PROVIDER_PRESETS[normalize_provider(provider)])
	region = str(region or "").strip().lower() or "us-east-1"
	if not re.match(r"^[a-z0-9-]{2,30}$", region):
		raise ValueError("Invalid region")
	preset["host"] = preset["host"].replace("{region}", region)
	return preset


def normalize_spf_include(value: Any) -> str:
	"""'amazonses.com' -> 'include:amazonses.com'; mechanisms are kept as typed."""
	parts = []
	for token in str(value or "").split():
		if ":" not in token and "=" not in token and "." in token:
			token = "include:" + token
		if not SPF_MECH_RE.match(token):
			raise ValueError(f"Invalid SPF mechanism: {token}")
		parts.append(token)
	return " ".join(parts)


def validate_settings(values: Mapping[str, Any]) -> dict[str, Any]:
	"""Normalize Email Relay Settings values; ValueError on the first problem.

	Returns the cleaned subset (`provider`, `smtp_host`, `smtp_port`, `tls_mode`,
	`default_from_domain`, `spf_include`, `dkim_selector`, `dmarc_policy`, `daily_send_limit`).
	Missing host/port/SPF are taken from the provider preset.
	"""
	provider = normalize_provider(values.get("provider"))
	preset = provider_defaults(provider, values.get("region"))
	out: dict[str, Any] = {"provider": provider}
	host = str(values.get("smtp_host") or "").strip().lower() or preset["host"]
	if values.get("enabled") and not host:
		raise ValueError("SMTP host is required")
	if host and not DOMAIN_RE.match(host) and not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host):
		raise ValueError("SMTP host must be a host name, e.g. smtp.example.com")
	out["smtp_host"] = host
	out["tls_mode"] = normalize_tls_mode(values.get("tls_mode") or preset["tls"])
	port = values.get("smtp_port")
	try:
		port = int(port) if port not in (None, "", 0, "0") else DEFAULT_PORTS[out["tls_mode"]]
	except (TypeError, ValueError):
		raise ValueError("SMTP port must be a number") from None
	if not 1 <= port <= 65535:
		raise ValueError("SMTP port must be between 1 and 65535")
	out["smtp_port"] = port
	frm = str(values.get("default_from_domain") or "").strip()
	out["default_from_domain"] = normalize_domain(frm) if frm else ""
	out["spf_include"] = normalize_spf_include(values.get("spf_include") or preset["spf"])
	selector = str(values.get("dkim_selector") or preset["dkim_selector"] or "").strip()
	if selector and not SELECTOR_RE.match(selector):
		raise ValueError("Invalid DKIM selector")
	out["dkim_selector"] = selector
	policy = str(values.get("dmarc_policy") or "none").strip().lower()
	if policy not in DMARC_POLICIES:
		raise ValueError("DMARC policy must be none, quarantine or reject")
	out["dmarc_policy"] = policy
	rua = str(values.get("dmarc_rua") or "").strip()
	if rua and not is_valid_email(rua):
		raise ValueError("DMARC report address must be an e-mail address")
	out["dmarc_rua"] = rua
	try:
		limit = int(values.get("daily_send_limit") or 0)
	except (TypeError, ValueError):
		raise ValueError("Daily send limit must be a number") from None
	if limit < 0:
		raise ValueError("Daily send limit cannot be negative")
	out["daily_send_limit"] = limit
	return out


# --------------------------------------------------------------------------- per site


def sender_address(site_name: str, sender_domain: Any = None, domain_verified: bool = False,
                   default_from_domain: Any = None, local_part: str = DEFAULT_LOCAL_PART) -> str:
	"""noreply@<verified custom domain> > noreply@<operator domain> > noreply@<site>.

	An unverified custom domain is never used: mail from it would fail SPF/DKIM.
	"""
	for domain in ((sender_domain if domain_verified else None), default_from_domain, site_name):
		if domain:
			try:
				return f"{local_part}@{normalize_domain(domain)}"
			except ValueError:
				continue
	raise ValueError("No valid sender domain")


def configure_params(site_name: str, settings: Mapping[str, Any], email_id: str, enabled: bool,
                     password_ref: str | None) -> dict[str, Any]:
	"""Params of the agent job `site.configure_email` (password as a __secret__ placeholder)."""
	if not enabled:
		return {"site": site_name, "enabled": False, "account_name": ACCOUNT_NAME}
	username = str(settings.get("username") or "").strip() or None
	params = {
		"site": site_name,
		"enabled": True,
		"account_name": ACCOUNT_NAME,
		"email_id": email_id,
		"smtp_server": settings.get("smtp_host"),
		"smtp_port": int(settings.get("smtp_port") or DEFAULT_PORTS[normalize_tls_mode(settings.get("tls_mode"))]),
		"tls_mode": JOB_TLS[normalize_tls_mode(settings.get("tls_mode"))],
		"username": username,
		"password": password_ref if username else None,
		"verify_smtp": bool(int(settings.get("verify_smtp", 1) or 0)),
	}
	return params


def relay_fingerprint(settings: Mapping[str, Any], password_hash: str = "") -> str:
	"""Stable fingerprint of the relay fields: sites are re-pushed when it changes."""
	import hashlib
	import json

	keys = ("enabled", "smtp_host", "smtp_port", "tls_mode", "username", "default_from_domain")
	data = {k: settings.get(k) for k in keys}
	data["password"] = password_hash
	return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:32]


# --------------------------------------------------------------------------- DNS records


def _fill(value: str, domain: str) -> str:
	return str(value or "").replace("{domain}", domain).replace("{domain_dashed}", domain.replace(".", "-"))


def dns_records(domain: str, settings: Mapping[str, Any]) -> list[dict[str, Any]]:
	"""Records a customer publishes for `domain` (SPF, DKIM, DMARC, provider return-path).

	Each record: `{purpose, type, host, value, required, note}`; `host` is the FQDN.
	DKIM is only listed when the operator configured a selector and value.
	"""
	domain = normalize_domain(domain)
	provider = normalize_provider(settings.get("provider"))
	preset = PROVIDER_PRESETS[provider]
	records: list[dict[str, Any]] = []
	spf = normalize_spf_include(settings.get("spf_include") or preset["spf"])
	if spf:
		records.append({"purpose": "SPF", "type": "TXT", "host": domain, "value": f"v=spf1 {spf} ~all",
		                "required": True, "include": spf,
		                "note": "Merge into your existing SPF record: a domain may only have one v=spf1 record."})
	selector = _fill(str(settings.get("dkim_selector") or preset["dkim_selector"] or "").strip(), domain)
	dkim_value = _fill(str(settings.get("dkim_value") or "").strip(), domain)
	if selector and dkim_value:
		dtype = str(settings.get("dkim_record_type") or preset["dkim_type"]).upper()
		dtype = dtype if dtype in ("TXT", "CNAME") else "TXT"
		records.append({"purpose": "DKIM", "type": dtype, "host": f"{selector}._domainkey.{domain}",
		                "value": dkim_value, "required": True, "note": ""})
	policy = str(settings.get("dmarc_policy") or "none").lower()
	dmarc = f"v=DMARC1; p={policy if policy in DMARC_POLICIES else 'none'}"
	if settings.get("dmarc_rua"):
		dmarc += f"; rua=mailto:{settings['dmarc_rua']}"
	records.append({"purpose": "DMARC", "type": "TXT", "host": f"_dmarc.{domain}", "value": dmarc,
	                "required": True, "note": "Keep your existing DMARC record if you already have one."})
	rp_host = str(settings.get("return_path_host") or "").strip()
	rp_value = str(settings.get("return_path_value") or "").strip()
	if not (rp_host and rp_value) and preset["return_path"]:
		rp_host, rp_value = preset["return_path"]
	if rp_host and rp_value:
		records.append({"purpose": "Return-Path", "type": "CNAME", "host": f"{_fill(rp_host, domain)}.{domain}",
		                "value": _fill(rp_value, domain), "required": False,
		                "note": "Recommended: aligns bounces with your domain."})
	return records


def _clean_txt(value: str) -> str:
	"""TXT data as dnspython / DoH print it: '"v=spf1 " "include:x ~all"' -> 'v=spf1 include:x ~all'."""
	text = str(value or "").strip()
	if '"' in text:
		text = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', text)) or text.strip('"')
	return text.replace('\\"', '"')


def _norm_host(value: str) -> str:
	return str(value or "").strip().lower().rstrip(".")


def _spf_ok(values: list[str], include: str) -> tuple[bool, str]:
	spf = [v for v in values if v.lower().startswith("v=spf1")]
	if not spf:
		return False, "No SPF record"
	if len(spf) > 1:
		return False, "More than one SPF record"
	tokens = spf[0].lower().split()
	missing = [t for t in include.lower().split() if t not in tokens and ("+" + t) not in tokens]
	if missing:
		return False, "SPF record does not include " + " ".join(missing)
	return True, "OK"


def _dmarc_ok(values: list[str]) -> tuple[bool, str]:
	dmarc = [v for v in values if v.replace(" ", "").lower().startswith("v=dmarc1")]
	if not dmarc:
		return False, "No DMARC record"
	if len(dmarc) > 1:
		return False, "More than one DMARC record"
	return True, "OK"


def _dkim_ok(values: list[str], record: Mapping[str, Any]) -> tuple[bool, str]:
	if not values:
		return False, "No DKIM record"
	if record["type"] == "CNAME":
		if any(_norm_host(v) == _norm_host(record["value"]) for v in values):
			return True, "OK"
		return False, "DKIM CNAME points elsewhere"
	want = re.sub(r"\s+", "", str(record["value"]))
	if any(re.sub(r"\s+", "", v) == want for v in values):
		return True, "OK"
	# operator may store only the public key (p=...) part
	if any(want and want in re.sub(r"\s+", "", v) for v in values):
		return True, "OK"
	return False, "DKIM record does not match"


def check_records(records: Iterable[Mapping[str, Any]], lookup: Callable[[str, str], list[str] | None]
                  ) -> dict[str, Any]:
	"""Compare expected records with DNS. `lookup(host, type)` returns the record strings, [] when
	there is none, or None when DNS could not be queried.

	Result: `{verified, checked, records: [record + {ok, found, message}]}`; `verified` needs every
	required record ok; `checked` is False when DNS lookups were unavailable.
	"""
	out = []
	checked = True
	for rec in records:
		rec = dict(rec)
		qtype = rec["type"]
		found = lookup(rec["host"], qtype)
		if found is None:
			checked = False
			rec.update({"ok": False, "found": [], "message": "DNS lookup unavailable"})
			out.append(rec)
			continue
		values = [_clean_txt(v) for v in found] if qtype == "TXT" else [_norm_host(v) for v in found]
		if rec["purpose"] == "SPF":
			ok, msg = _spf_ok(values, rec.get("include") or "")
		elif rec["purpose"] == "DMARC":
			ok, msg = _dmarc_ok(values)
		elif rec["purpose"] == "DKIM":
			ok, msg = _dkim_ok(values, rec)
		else:
			ok = any(_norm_host(v) == _norm_host(rec["value"]) for v in values)
			msg = "OK" if ok else ("No record" if not values else "Points elsewhere")
		rec.update({"ok": ok, "found": values[:5], "message": msg})
		out.append(rec)
	verified = checked and bool(out) and all(r["ok"] for r in out if r.get("required"))
	return {"verified": verified, "checked": checked, "records": out}


DNS_TYPES = {"TXT": 16, "CNAME": 5}


def parse_doh_answer(payload: Any, qtype: str) -> list[str] | None:
	"""Answers of a DNS-over-HTTPS JSON response (application/dns-json), or None on error.

	NXDOMAIN (Status 3) and empty answers are [] (record missing), other failures None.
	"""
	if not isinstance(payload, Mapping):
		return None
	status = payload.get("Status")
	if status == 3:
		return []
	if status != 0:
		return None
	want = DNS_TYPES.get(qtype.upper())
	result = []
	for ans in payload.get("Answer") or []:
		if isinstance(ans, Mapping) and ans.get("type") == want and isinstance(ans.get("data"), str):
			result.append(ans["data"])
	return result
