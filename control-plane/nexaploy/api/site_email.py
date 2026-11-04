"""Managed e-mail for the customer portal (/dashboard/site, "E-mail" tab).

Every method resolves the site's team and checks it with nexaploy.teams.check_team_access:
read -> status and DNS records, write -> enable/disable, sending domain, DNS check.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime

from nexaploy import email_utils as eu
from nexaploy import email_relay as er

DNS_CHECK_MIN_SECONDS = 20


def _site(site, action="read"):
	if not frappe.session.user or frappe.session.user == "Guest":
		raise frappe.PermissionError(_("Please log in"))
	if not site:
		frappe.throw(_("Site is required"))
	row = frappe.db.get_value("Site", site, ["name", "site_name", "team", "server", "status"], as_dict=True)
	if not row:
		raise frappe.DoesNotExistError(_("Site {0} not found").format(site))
	from nexaploy.teams import check_team_access

	check_team_access(row.team, action)
	return row


def _can_write(team):
	from nexaploy.team_utils import role_allows
	from nexaploy.teams import get_team_role, is_platform_admin

	return is_platform_admin() or role_allows(get_team_role(team), "write")


def _state(row, settings=None):
	settings = settings or er.get_settings()
	doc = er.get_site_email(row.name)
	available = er.relay_ready(settings)
	out = {
		"site": row.name,
		"available": available,
		"can_write": _can_write(row.team),
		"enabled": bool(doc and doc.enabled),
		"status": doc.status if doc else "Disabled",
		"email_id": doc.email_id if doc else None,
		"sender_domain": doc.sender_domain if doc else None,
		"domain_verified": bool(doc and doc.domain_verified),
		"last_dns_check": doc.last_dns_check if doc else None,
		"last_error": doc.last_error if doc else None,
		"daily_limit": er.daily_limit(doc, settings) if doc else int(settings.get("daily_send_limit") or 0),
		"default_sender": None,
		"records": [],
		"check": None,
	}
	try:
		probe = doc or frappe._dict(site=row.name, sender_domain=None, domain_verified=0)
		out["default_sender"] = eu.sender_address(row.name, None, False, settings.get("default_from_domain"))
		if doc and doc.sender_domain:
			out["records"] = er.expected_records(probe, settings)
			out["check"] = frappe.parse_json(doc.dns_check_result) if doc.dns_check_result else None
	except ValueError:
		pass
	if doc and doc.last_job:
		out["job_status"] = frappe.db.get_value("Agent Job", doc.last_job, "status")
	return out


@frappe.whitelist()
def get_site_email(site):
	row = _site(site, "read")
	return _state(row)


@frappe.whitelist(methods=["POST"])
def set_managed_email(site, enabled):
	"""Turn managed e-mail on/off for a site (queues site.configure_email)."""
	from nexaploy.activity import log_activity
	from nexaploy.portal_utils import to_bool

	row = _site(site, "write")
	enabled = to_bool(enabled)
	settings = er.get_settings()
	if enabled and not er.relay_ready(settings):
		frappe.throw(_("Managed e-mail is not available: the platform relay is not configured"))
	doc = er.get_site_email(row.name, create=enabled)
	if doc is None:
		return _state(row, settings)
	doc.enabled = 1 if enabled else 0
	doc.save(ignore_permissions=True)
	er.enqueue_configure(doc, enabled=enabled, settings=settings)
	log_activity(row.team, "site.email_enable" if enabled else "site.email_disable", "Site", row.name)
	return _state(row, settings)


@frappe.whitelist(methods=["POST"])
def set_sender_domain(site, domain=None):
	"""Set (or clear with an empty value) the custom sending domain; returns the DNS records."""
	from nexaploy.activity import log_activity

	row = _site(site, "write")
	settings = er.get_settings()
	value = None
	if domain and str(domain).strip():
		try:
			value = eu.normalize_domain(domain)
		except ValueError as exc:
			frappe.throw(_(str(exc)))
	doc = er.get_site_email(row.name, create=True)
	was_verified = bool(doc.domain_verified)
	doc.sender_domain = value
	doc.save(ignore_permissions=True)
	if was_verified and doc.enabled and er.relay_ready(settings):
		er.enqueue_configure(doc, settings=settings)  # back to the default sender until verified
	log_activity(row.team, "site.email_domain", "Site", row.name, {"domain": value})
	return _state(row, settings)


@frappe.whitelist(methods=["POST"])
def check_sender_domain(site):
	"""Query DNS for the SPF/DKIM/DMARC records now (at most every 20 s per site)."""
	row = _site(site, "write")
	doc = er.get_site_email(row.name)
	if not doc or not doc.sender_domain:
		frappe.throw(_("Set a sending domain first"))
	if doc.last_dns_check and (now_datetime() - doc.last_dns_check).total_seconds() < DNS_CHECK_MIN_SECONDS:
		frappe.throw(_("Please wait a few seconds before checking again"))
	er.check_domain(doc)
	return _state(row)
