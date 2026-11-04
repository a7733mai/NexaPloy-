"""Custom domains for the customer portal: DNS instructions and verification, TLS health.

Every method resolves the site's team and checks it with nexaploy.teams.check_team_access
(ARCHITECTURE.md §9): read -> get_domains, write -> add / verify / remove / check TLS.
Skipping DNS verification is for platform admins (System Manager) only.
Logic: nexaploy.domains (frappe) and nexaploy.domain_utils (pure).
"""

import frappe
from frappe import _

from nexaploy import domain_utils as du

SITE_FIELDS = (
	"name", "site_name", "team", "server", "status",
	"tls_status", "tls_issuer", "tls_expires_on", "tls_checked_at", "tls_message", "tls_detail",
)
DOMAIN_FIELDS = (
	"domain", "status", "verification_type", "skip_dns_check", "proxied", "dns_requested_at",
	"last_checked_at", "dns_check_attempts", "activated_at", "dns_check_message", "dns_check_detail",
	"tls_status", "tls_issuer", "tls_expires_on", "tls_checked_at", "tls_message", "tls_detail",
)


def _site(site, action="read"):
	from nexaploy.teams import check_team_access

	if not frappe.session.user or frappe.session.user == "Guest":
		raise frappe.PermissionError(_("Please log in"))
	if not site:
		frappe.throw(_("Site is required"))
	row = frappe.db.get_value("Site", site, list(SITE_FIELDS), as_dict=True)
	if not row:
		raise frappe.DoesNotExistError(_("Site {0} not found").format(site))
	check_team_access(row.team, action)
	return row


def _is_admin():
	from nexaploy.teams import is_platform_admin

	return is_platform_admin()


def _log(team, action, site, details):
	from nexaploy.activity import log_activity

	log_activity(team, action, "Site", site, details)


def _translated(detail, fallback):
	"""Stored {message, args} -> text in the user's language (fallback: stored English text)."""
	data = frappe.parse_json(detail) if detail else None
	if isinstance(data, dict) and data.get("message"):
		return du.render(_(data["message"]), data.get("args") or [])
	return fallback or None


def _tls(row):
	return {
		"status": row.tls_status or None,
		"issuer": row.tls_issuer,
		"expires_on": row.tls_expires_on,
		"checked_at": row.tls_checked_at,
		"message": _translated(row.tls_detail, row.tls_message),
	}


@frappe.whitelist()
def get_domains(site):
	"""Primary hostname + custom domains with DNS state, the records to create and TLS health."""
	from nexaploy.domains import server_targets
	from nexaploy.teams import get_team_role
	from nexaploy.team_utils import role_allows

	row = _site(site, "read")
	targets = server_targets(row.server)
	domains = frappe.get_all(
		"Site Domain",
		filters={"parenttype": "Site", "parent": row.name},
		fields=list(DOMAIN_FIELDS),
		order_by="idx asc",
	)
	admin = _is_admin()
	out = []
	for d in domains:
		out.append({
			"domain": d.domain,
			"status": d.status,
			"verification_type": d.verification_type or None,
			"skip_dns_check": bool(d.skip_dns_check),
			"proxied": bool(d.proxied),
			"requested_at": d.dns_requested_at,
			"last_checked_at": d.last_checked_at,
			"attempts": d.dns_check_attempts or 0,
			"activated_at": d.activated_at,
			"message": _translated(d.dns_check_detail, d.dns_check_message),
			"records": du.dns_instructions(d.domain, row.site_name, targets),
			"apex": du.is_apex(d.domain),
			"tls": _tls(d),
		})
	role = get_team_role(row.team) if row.team else None
	return {
		"site": row.name,
		"site_name": row.site_name,
		"status": row.status,
		"targets": targets,
		"primary": {"domain": row.site_name, "tls": _tls(row)},
		"domains": out,
		"can_write": admin or role_allows(role, "write"),
		"is_admin": admin,
		"give_up_hours": du.DNS_GIVE_UP_HOURS,
	}


@frappe.whitelist(methods=["POST"])
def preview_records(site, domain):
	"""Records to create for a domain before adding it."""
	row = _site(site, "read")
	from nexaploy.domains import server_targets
	from nexaploy.utils import is_valid_domain

	domain = du.normalize_host(domain)
	if not is_valid_domain(domain):
		frappe.throw(_("Invalid domain {0}").format(domain))
	return {"domain": domain, "records": du.dns_instructions(domain, row.site_name, server_targets(row.server))}


@frappe.whitelist(methods=["POST"])
def add_domain(site, domain, skip_dns_check=0):
	"""Attach a domain; DNS is checked right away (job sent only when it is correct)."""
	row = _site(site, "write")
	skip = bool(frappe.parse_json(skip_dns_check)) if isinstance(skip_dns_check, str) else bool(skip_dns_check)
	if skip and not _is_admin():
		raise frappe.PermissionError(_("Only administrators can skip DNS verification"))
	doc = frappe.get_doc("Site", row.name)
	job = doc.add_domain(domain, skip_dns_check=skip)
	domain = du.normalize_host(domain)
	_log(row.team, "site.add_domain", row.name, {"domain": domain, "job": job, "skip_dns_check": skip})
	return {"domain": domain, "job": job, "state": get_domains(row.name)}


@frappe.whitelist(methods=["POST"])
def verify_domain(site, domain):
	"""'Verify now': check DNS immediately (a Broken domain starts a new 72 h window)."""
	from nexaploy.domains import verify_domain as _verify

	row = _site(site, "write")
	result = _verify(row.name, du.normalize_host(domain), manual=True)
	_log(row.team, "site.domain_verify", row.name, {"domain": du.normalize_host(domain), "ok": result["ok"], "job": result["job"]})
	result["message"] = du.render(_(result["message"]), result["args"])
	return result


@frappe.whitelist(methods=["POST"])
def set_skip_dns_check(site, domain, skip=1):
	"""Operator override (System Manager): skip / require DNS verification for a domain."""
	from nexaploy.domains import verify_domain as _verify

	row = _site(site, "write")
	if not _is_admin():
		raise frappe.PermissionError(_("Only administrators can skip DNS verification"))
	skip = bool(frappe.parse_json(skip)) if isinstance(skip, str) else bool(skip)
	name = frappe.db.get_value("Site Domain", {"parenttype": "Site", "parent": row.name, "domain": du.normalize_host(domain)}, "name")
	if not name:
		frappe.throw(_("Domain {0} is not attached to this site").format(domain))
	frappe.db.set_value("Site Domain", name, "skip_dns_check", int(skip), update_modified=False)
	_log(row.team, "site.domain_skip_dns", row.name, {"domain": du.normalize_host(domain), "skip": skip})
	result = _verify(row.name, du.normalize_host(domain), manual=True) if skip else None
	return {"skip": skip, "job": result and result["job"]}


@frappe.whitelist(methods=["POST"])
def remove_domain(site, domain):
	row = _site(site, "write")
	doc = frappe.get_doc("Site", row.name)
	job = doc.remove_domain(du.normalize_host(domain))
	_log(row.team, "site.remove_domain", row.name, {"domain": du.normalize_host(domain), "job": job})
	return {"job": job}


@frappe.whitelist(methods=["POST"])
def check_tls(site):
	"""Ask the agent which certificate it serves for the site's hosts (site.tls_status)."""
	from nexaploy.domains import enqueue_tls_check

	row = _site(site, "write")
	if row.status not in ("Active", "Suspended", "Broken"):
		frappe.throw(_("Site {0} is {1}").format(row.name, _(row.status)))
	return {"job": enqueue_tls_check(row.name)}
