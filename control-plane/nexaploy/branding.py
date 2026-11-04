"""White-label brands: resolve the brand of a request / team, and brand transactional e-mails.

    from nexaploy.branding import request_brand, team_brand, mail_kwargs

    request_brand()            # brand of frappe.local.request.host (cached), else the default
    team_brand(team)           # Team.reseller_brand (enabled), else the default
    frappe.sendmail(recipients=..., **mail_kwargs(team, subject, message))
                               # -> {subject, message, sender?}: brand title in the subject
                               #    prefix, footer appended, "Sender Name <address>"

The host -> brand index is cached in Redis (``nexaploy:brand_index``) and cleared whenever a
Brand changes (Brand.on_update / on_trash). Pure logic lives in nexaploy.brand_utils.

A brand's portal domain is only reachable once the operator routes it to the control plane:
add the host to the control-plane site (``bench setup add-domain`` / Frappe domains) and to its
reverse proxy (Traefik router + certificate). Nexaploy does not automate this.
"""

import frappe

from nexaploy import brand_utils as bu

BRAND = "Brand"
CACHE_KEY = "nexaploy:brand_index"
CACHE_SECONDS = 600
ROW_FIELDS = ["name", "title", "enabled", "team", "is_default", "primary_color", "logo", "favicon",
              "portal_domains", "support_email", "footer_text", "email_sender_name", "email_sender_address"]


def _load_index():
	if not frappe.db.table_exists(BRAND):
		return {"hosts": {}, "brands": {}, "default": None}
	rows = frappe.get_all(BRAND, filters={"enabled": 1}, fields=ROW_FIELDS, order_by="creation asc")
	brands = {r.name: bu.public_brand(r) for r in rows}
	default = next((r.name for r in rows if r.is_default and not r.team), None)
	return {"hosts": bu.host_map(rows), "brands": brands, "default": default}


def brand_index():
	"""{hosts: {host: brand}, brands: {name: public brand}, default: name|None} (cached)."""
	cache = frappe.cache()
	data = None
	try:
		data = cache.get_value(CACHE_KEY)
	except Exception:
		data = None
	if not isinstance(data, dict):
		data = _load_index()
		try:
			cache.set_value(CACHE_KEY, data, expires_in_sec=CACHE_SECONDS)
		except Exception:
			pass
	return data


def clear_brand_cache(*args, **kwargs):
	try:
		frappe.cache().delete_value(CACHE_KEY)
	except Exception:
		pass
	frappe.local.nexaploy_request_brand = None


def default_brand():
	idx = _safe_index()
	if idx["default"] and idx["default"] in idx["brands"]:
		return dict(idx["brands"][idx["default"]], is_default=True)
	return bu.public_brand(None)


def _safe_index():
	try:
		return brand_index()
	except Exception:
		frappe.log_error(title="Nexaploy: brand index failed")
		return {"hosts": {}, "brands": {}, "default": None}


def brand_for_host(host):
	idx = _safe_index()
	name = bu.resolve_brand_name(host, idx["hosts"])
	if name and name in idx["brands"]:
		return dict(idx["brands"][name])
	return default_brand()


def request_host():
	request = getattr(frappe.local, "request", None)
	if request is None:
		return None
	try:
		return request.host
	except Exception:
		return None


def request_brand():
	"""Brand of the current request host (memoised per request)."""
	cached = getattr(frappe.local, "nexaploy_request_brand", None)
	if cached is not None:
		return cached
	brand = brand_for_host(request_host())
	frappe.local.nexaploy_request_brand = brand
	return brand


def request_brand_name():
	"""Name of the non-default brand of this request host, or None."""
	idx = _safe_index()
	return bu.resolve_brand_name(request_host(), idx["hosts"])


def team_brand(team):
	"""Brand of a team's customer-facing communication: its `reseller_brand` when enabled."""
	if not team:
		return default_brand()
	try:
		name = frappe.db.get_value("Team", team, "reseller_brand")
	except Exception:
		name = None  # column not migrated yet
	idx = _safe_index()
	if name and name in idx["brands"]:
		return dict(idx["brands"][name])
	return default_brand()


def team_url(team, path="/"):
	"""URL of `path` on the team's brand portal domain (else the control plane's own URL)."""
	from frappe.utils import get_url

	try:
		return bu.portal_url(team_brand(team), path, get_url())
	except Exception:
		return get_url(path)


def mail_kwargs(team, subject, message, brand=None):
	"""frappe.sendmail kwargs carrying the team's brand: subject prefix, footer and sender.
	Never raises: on any problem the unbranded subject/message are returned."""
	try:
		return _mail_kwargs(team, subject, message, brand)
	except Exception:
		frappe.log_error(title=f"Nexaploy: branding e-mail for {team} failed")
		return {"subject": subject, "message": message}


def _mail_kwargs(team, subject, message, brand=None):
	brand = brand or team_brand(team)
	out = {"subject": bu.brand_subject(subject, brand.get("title")), "message": message}
	if brand.get("is_default") and not brand.get("name"):
		return out
	out["message"] = bu.apply_footer(message, brand.get("footer_text"))
	sender = _sender(brand)
	if sender:
		out["sender"] = sender
	return out


def _sender(brand):
	name = brand.get("email_sender_name") or brand.get("title")
	address = brand.get("email_sender_address")
	if not address:
		try:
			from frappe.email.doctype.email_account.email_account import EmailAccount

			account = EmailAccount.find_outgoing()
			address = account.email_id if account else None
		except Exception:
			address = None
	return bu.format_sender(name, address)


def update_website_context(context):
	"""hooks.update_website_context: brand every web page of the control plane (login, signup,
	portal). Only a non-default brand overrides Frappe's own logo/app name/favicon."""
	brand = request_brand()
	values = {"nx_brand": brand}
	if not brand.get("is_default") or brand.get("name"):
		values["app_name"] = brand["title"]
		if brand.get("logo"):
			values["logo"] = brand["logo"]
			values["app_logo"] = brand["logo"]
		if brand.get("favicon"):
			values["favicon"] = brand["favicon"]
	return values
