"""/signup/<product> (website_route_rules) or /signup?product=<slug>: public SaaS signup
form (nexaploy/api/signup.py). ?verify=<token> confirms the e-mail and shows progress."""

import frappe
from frappe import _

from nexaploy.portal_utils import text_direction

no_cache = 1


def get_context(context):
	from nexaploy.saas import get_product

	slug = frappe.form_dict.get("product") or frappe.form_dict.get("slug")
	product = get_product(slug) if slug else None
	if not product:
		raise frappe.DoesNotExistError(_("This product is not available"))

	user = frappe.session.user
	lang = getattr(frappe.local, "lang", None) or "en"
	context.no_cache = 1
	context.show_sidebar = 0
	context.title = _("Sign up for {0}").format(product.title)
	context.lang = lang
	context.dir = text_direction(lang)
	context.product = product
	from nexaploy.branding import request_brand

	context.nx_brand = request_brand()
	context.csrf_token = ""
	if user and user != "Guest":
		try:
			from frappe.sessions import get_csrf_token

			context.csrf_token = get_csrf_token()
		except Exception:
			pass
	from nexaploy.api.dashboard import _portal_messages

	context.signup_boot = {
		"product": product.slug,
		"title": product.title,
		"domain": product.root_domain,
		"trial_days": int(product.trial_days or 0),
		"user": None if user == "Guest" else user,
		"verify": frappe.form_dict.get("verify") or None,
		"messages": _portal_messages(lang),
	}
	return context
