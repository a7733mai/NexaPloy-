"""Public SaaS signup API (pages: /signup/<product> or /signup?product=<slug>).

    product_info(product)                       guest
    check_subdomain(product, subdomain)         guest, live availability check
    signup(product, email, full_name, subdomain, password, company=None)
                                                guest, rate limited; e-mails a verification link
                                                and returns {token, status} (progress token)
    verify_email(token)                         guest, rate limited; creates User + Team + Site
    signup_status(token)                        guest; {status, site_name, url, error}

An existing account must log in first; when the visitor is logged in as that e-mail the
verification step is skipped. The chosen password becomes the user's password and the
site's Administrator password. Tokens are stored as sha256 only (SaaS Signup).
"""

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import add_to_date, get_url, now_datetime

from nexaploy import signup_utils as su
from nexaploy.saas import SIGNUP, get_product, provision_signup, refresh_signup, site_name_taken
from nexaploy.utils import generate_token, hash_token


def _product(product):
	row = get_product(product)
	if not row:
		raise frappe.DoesNotExistError(_("This product is not available"))
	return row


def _throw(exc):
	frappe.throw(_(str(exc)))


def _request_ip():
	return getattr(frappe.local, "request_ip", None)


@frappe.whitelist(allow_guest=True)
def product_info(product):
	p = _product(product)
	return {
		"title": p.title,
		"slug": p.slug,
		"description": p.description,
		"domain": p.root_domain,
		"trial_days": int(p.trial_days or 0),
		"logged_in_as": None if frappe.session.user == "Guest" else frappe.session.user,
	}


@frappe.whitelist(allow_guest=True)
@rate_limit(limit=120, seconds=60)
def check_subdomain(product, subdomain):
	p = _product(product)
	result = su.subdomain_status(subdomain, p.root_domain, site_name_taken)
	if result.get("reason"):
		result["reason"] = _(result["reason"])
	return result


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=5, seconds=60 * 60)
def signup(product, email, full_name, subdomain, password, company=None):
	p = _product(product)
	try:
		email = su.normalize_email(email)
		password = su.check_password(password)
		label, site_name = su.site_name_for(subdomain, p.root_domain)
	except ValueError as exc:
		_throw(exc)
	full_name = su.clean_text(full_name)
	if not full_name:
		frappe.throw(_("Full name is required"))
	if site_name_taken(site_name, email):
		frappe.throw(_("This subdomain is already taken"))

	session_user = frappe.session.user
	existing = frappe.db.get_value("User", {"email": email}, "name")
	logged_in_as_owner = existing and session_user == existing
	if existing and not logged_in_as_owner:
		frappe.throw(
			_("An account with this e-mail already exists. Log in first, then sign up again."),
			frappe.PermissionError,
		)

	token, status_token = generate_token(), generate_token()
	doc = frappe.get_doc(
		{
			"doctype": SIGNUP,
			"product": p.name,
			"status": su.PENDING_VERIFICATION,
			"email": email,
			"full_name": full_name,
			"company": su.clean_text(company) or None,
			"subdomain": label,
			"site_name": site_name,
			"token_hash": hash_token(token),
			"status_token_hash": hash_token(status_token),
			"password": password,
			"expires_on": add_to_date(now_datetime(), hours=su.VERIFICATION_TTL_HOURS),
			"ip_address": _request_ip(),
		}
	)
	doc.insert(ignore_permissions=True)

	if logged_in_as_owner:
		# the logged-in user owns this e-mail: no verification needed
		doc.status = su.VERIFIED
		doc.verified_at = now_datetime()
		doc.save(ignore_permissions=True)
		provision_signup(doc)
	else:
		_send_verification(doc, p, token)
	return {"token": status_token, "status": doc.status, "site_name": site_name, "email": email}


def _send_verification(doc, product, token):
	link = get_url(f"/signup?product={product.slug}&verify={token}")
	frappe.sendmail(
		recipients=[doc.email],
		subject=_("Confirm your e-mail to create your {0} site").format(product.title),
		message=_(
			"Hello {0},<br><br>Confirm your e-mail address to create {1}:<br><br>"
			'<a href="{2}">{2}</a><br><br>The link is valid for 24 hours. If you did not sign up, ignore this e-mail.'
		).format(frappe.utils.escape_html(doc.full_name or ""), doc.site_name, link),
		now=True,
	)


def _by_token(token, fields=("token_hash",)):
	if not token or len(str(token)) > 256:
		return None
	digest = hash_token(str(token))
	for field in fields:
		name = frappe.db.get_value(SIGNUP, {field: digest}, "name")
		if name:
			return name
	return None


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=20, seconds=60 * 60)
def verify_email(token):
	"""Confirm the e-mail and start the site. Logs the new user in. Returns signup_status()."""
	name = _by_token(token, ("token_hash",))
	if not name:
		frappe.throw(_("This verification link is invalid"), frappe.DoesNotExistError)
	doc = frappe.get_doc(SIGNUP, name, for_update=True)
	if doc.status == su.PENDING_VERIFICATION:
		if su.verification_expired(doc.expires_on, now_datetime()):
			frappe.throw(_("This verification link has expired. Please sign up again."))
		if site_name_taken_by_other(doc):
			doc.status = su.FAILED
			doc.error = "This subdomain is already taken"
			doc.save(ignore_permissions=True)
			return _status(doc)
		doc.status = su.VERIFIED
		doc.verified_at = now_datetime()
		doc.save(ignore_permissions=True)
		previous_user = frappe.session.user
		frappe.set_user("Administrator")
		try:
			provision_signup(doc)
		finally:
			frappe.set_user(previous_user)
		if doc.user and previous_user in (None, "Guest"):
			try:
				frappe.local.login_manager.login_as(doc.user)
			except Exception:
				frappe.log_error(title=f"Nexaploy: login after signup failed for {doc.name}")
	return _status(doc)


def site_name_taken_by_other(doc):
	return bool(
		frappe.db.exists("Site", doc.site_name)
		or frappe.db.exists("Site Domain", {"domain": doc.site_name, "parenttype": "Site"})
		or frappe.db.exists(
			SIGNUP,
			{"site_name": doc.site_name, "name": ["!=", doc.name], "status": ["in", [su.PROVISIONING, su.ACTIVE]]},
		)
	)


@frappe.whitelist(allow_guest=True)
@rate_limit(limit=600, seconds=60 * 60)
def signup_status(token):
	"""Poll target of the signup page (progress token from signup() or the verification token)."""
	name = _by_token(token, ("status_token_hash", "token_hash"))
	if not name:
		raise frappe.DoesNotExistError(_("Unknown signup"))
	doc = frappe.get_doc(SIGNUP, name)
	if doc.status == su.PROVISIONING:
		doc = refresh_signup(doc)
	return _status(doc)


def _status(doc):
	out = {
		"status": doc.status,
		"site_name": doc.site_name,
		"email": doc.email,
		"url": None,
		"error": _(doc.error) if doc.error else None,
	}
	if doc.status == su.ACTIVE:
		out["url"] = f"https://{doc.site_name}"
		out["dashboard_url"] = f"/dashboard/site?name={doc.site}"
	return out
