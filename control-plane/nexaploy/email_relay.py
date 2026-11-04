"""Managed e-mail: point a site's outgoing mail at the platform SMTP relay.

The operator configures the relay in **Email Relay Settings** (single). A team enables "Managed
e-mail" per site (**Site Email**, one per site); that enqueues the agent job
``site.configure_email`` which creates/updates the site's default outgoing Email Account
"Nexaploy Relay" inside the bench (agent/nexaploy_agent/runtime/mail_relay.py). The SMTP password
travels as ``__secret__:Email Relay Settings:Email Relay Settings:password`` and is resolved only
when the agent polls (api/agent.py _resolve_secret -> get_decrypted_password).

A custom sending domain is used once its SPF/DKIM/DMARC records verify (``check_domain``);
DNS is queried with dnspython when installed, else DNS-over-HTTPS (Cloudflare JSON API), else
the check reports "DNS lookup unavailable". Pure logic: nexaploy.email_utils.
"""

import hashlib

import frappe
from frappe import _
from frappe.utils import now_datetime

from nexaploy import email_utils as eu
from nexaploy.utils import TERMINAL_JOB_STATUSES, secret_ref

SETTINGS = "Email Relay Settings"
SITE_EMAIL = "Site Email"
DAILY_LIMIT_KEY = "nexaploy_email_daily_limit"
DOH_URL = "https://cloudflare-dns.com/dns-query"
DNS_TIMEOUT = 5


# --------------------------------------------------------------------------- settings


def get_settings():
	"""Email Relay Settings as a dict, normalized (provider presets filled)."""
	doc = frappe.get_cached_doc(SETTINGS)
	values = doc.as_dict()
	try:
		values.update(eu.validate_settings(values))
	except ValueError:
		pass
	values["enabled"] = int(values.get("enabled") or 0)
	return values


def relay_ready(settings=None):
	settings = settings or get_settings()
	return bool(settings.get("enabled") and settings.get("smtp_host"))


def settings_fingerprint(doc):
	password = doc.get_password("password", raise_exception=False) if doc.get("password") else ""
	pw_hash = hashlib.sha256(password.encode()).hexdigest() if password else ""
	values = doc.as_dict()
	try:
		values.update(eu.validate_settings(values))
	except ValueError:
		pass
	return eu.relay_fingerprint(values, pw_hash)


# --------------------------------------------------------------------------- per site


def get_site_email(site, create=False):
	# by the `site` link, not the name: a renamed Site keeps its Site Email (named after the old site)
	name = frappe.db.get_value(SITE_EMAIL, {"site": site}, "name")
	if name:
		return frappe.get_doc(SITE_EMAIL, name)
	if not create:
		return None
	doc = frappe.get_doc({"doctype": SITE_EMAIL, "site": site, "enabled": 0, "status": "Disabled"})
	doc.insert(ignore_permissions=True)
	return doc


def effective_dns_settings(settings, site_email=None):
	"""Global DNS guidance values with the per-site DKIM override applied."""
	values = dict(settings)
	if site_email is not None and site_email.get("dkim_selector") and site_email.get("dkim_value"):
		values["dkim_selector"] = site_email.dkim_selector
		values["dkim_value"] = site_email.dkim_value
		if site_email.get("dkim_record_type"):
			values["dkim_record_type"] = site_email.dkim_record_type
	return values


def sender_for(site_email, settings):
	return eu.sender_address(
		site_email.site, site_email.sender_domain, bool(site_email.domain_verified), settings.get("default_from_domain")
	)


def daily_limit(site_email, settings):
	return int(site_email.get("daily_limit") or 0) or int(settings.get("daily_send_limit") or 0)


def enqueue_configure(site_email, enabled=None, settings=None):
	"""Queue site.configure_email for the site (enable or disable). Returns the Agent Job name."""
	from nexaploy.agent_job import create_agent_job

	settings = settings or get_settings()
	enabled = bool(site_email.enabled if enabled is None else enabled)
	site = frappe.db.get_value("Site", site_email.site, ["name", "site_name", "server", "status"], as_dict=True)
	if not site:
		frappe.throw(_("Site {0} not found").format(site_email.site))
	if site.status not in ("Active", "Suspended"):
		frappe.throw(_("Site {0} is {1}").format(site.name, _(site.status)))
	if enabled and not relay_ready(settings):
		frappe.throw(_("Managed e-mail is not available: the platform relay is not configured"))
	email_id = sender_for(site_email, settings) if enabled else None
	params = eu.configure_params(
		site.site_name, settings, email_id, enabled, secret_ref(SETTINGS, SETTINGS, "password")
	)
	job = create_agent_job(site.server, "site.configure_email", params, SITE_EMAIL, site_email.name)
	values = {"last_job": job.name, "status": "Pending" if enabled else "Disabled", "last_error": None}
	if enabled:
		values["email_id"] = email_id
	frappe.db.set_value(SITE_EMAIL, site_email.name, values, update_modified=False)
	_sync_daily_limit(site.name, daily_limit(site_email, settings) if enabled else 0)
	return job.name


def _sync_daily_limit(site, limit):
	"""Record the limit in site_config (informational: Frappe core does not enforce it)."""
	try:
		from nexaploy.ops import enqueue_config_update

		if limit:
			enqueue_config_update(site, {DAILY_LIMIT_KEY: int(limit)})
		else:
			enqueue_config_update(site, remove=[DAILY_LIMIT_KEY])
	except Exception:
		frappe.log_error(title=f"Nexaploy: e-mail limit config failed for {site}")


def reconfigure_all():
	"""Re-push the relay to every site with managed e-mail (settings changed)."""
	settings = get_settings()
	count = 0
	for name in frappe.get_all(SITE_EMAIL, filters={"enabled": 1}, pluck="name"):
		doc = frappe.get_doc(SITE_EMAIL, name)
		try:
			enqueue_configure(doc, enabled=relay_ready(settings), settings=settings)
			count += 1
		except Exception:
			frappe.log_error(title=f"Nexaploy: e-mail reconfigure failed for {name}")
	return count


def on_configure_email(job):
	"""Callback of site.configure_email (agent_callbacks)."""
	if job.status not in TERMINAL_JOB_STATUSES or job.reference_doctype != SITE_EMAIL:
		return
	name = job.reference_name
	if not name or not frappe.db.exists(SITE_EMAIL, name):
		return
	if frappe.db.get_value(SITE_EMAIL, name, "last_job") not in (None, "", job.name):
		return  # a newer job superseded this one
	params = frappe.parse_json(job.params) if job.params else {}
	enabled = bool((params or {}).get("enabled"))
	if job.status == "Success":
		values = {"status": "Active" if enabled else "Disabled", "last_error": None}
	else:
		values = {"status": "Failed", "last_error": (job.error or "")[-2000:]}
	frappe.db.set_value(SITE_EMAIL, name, values, update_modified=False)


# --------------------------------------------------------------------------- DNS


def _dnspython_lookup(host, qtype):
	import dns.exception
	import dns.resolver

	try:
		answers = dns.resolver.resolve(host, qtype, lifetime=DNS_TIMEOUT)
	except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
		return []
	except dns.exception.DNSException:
		return None
	return [a.to_text() for a in answers]


def _doh_lookup(host, qtype):
	import requests

	try:
		resp = requests.get(
			DOH_URL, params={"name": host, "type": qtype}, headers={"Accept": "application/dns-json"},
			timeout=DNS_TIMEOUT,
		)
		if resp.status_code != 200:
			return None
		return eu.parse_doh_answer(resp.json(), qtype)
	except Exception:
		return None


def dns_lookup(host, qtype):
	"""Record strings for host/type: [] when absent, None when DNS cannot be queried."""
	try:
		import dns.resolver  # noqa: F401  (dnspython: optional, not a Frappe dependency)
	except ImportError:
		return _doh_lookup(host, qtype)
	return _dnspython_lookup(host, qtype)


def expected_records(site_email, settings=None):
	settings = settings or get_settings()
	if not site_email.sender_domain:
		return []
	return eu.dns_records(site_email.sender_domain, effective_dns_settings(settings, site_email))


def check_domain(site_email, settings=None, lookup=None):
	"""Verify the custom domain's records; switch the sender when the verified state changes."""
	settings = settings or get_settings()
	records = expected_records(site_email, settings)
	if not records:
		return {"verified": False, "checked": False, "records": []}
	result = eu.check_records(records, lookup or dns_lookup)
	was = bool(site_email.domain_verified)
	values = {"last_dns_check": now_datetime(), "dns_check_result": frappe.as_json(result)}
	if result["checked"]:
		values["domain_verified"] = 1 if result["verified"] else 0
	frappe.db.set_value(SITE_EMAIL, site_email.name, values, update_modified=False)
	site_email.reload()
	if result["checked"] and was != bool(result["verified"]) and site_email.enabled and relay_ready(settings):
		enqueue_configure(site_email, settings=settings)
	return result


def recheck_domains():
	"""Daily: re-verify custom sending domains of sites with managed e-mail."""
	settings = get_settings()
	for name in frappe.get_all(SITE_EMAIL, filters={"enabled": 1, "sender_domain": ["is", "set"]}, pluck="name"):
		try:
			check_domain(frappe.get_doc(SITE_EMAIL, name), settings)
			frappe.db.commit()
		except Exception:
			frappe.log_error(title=f"Nexaploy: e-mail domain check failed for {name}")
