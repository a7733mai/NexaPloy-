"""/status/<slug> (website_route_rules) or /status on a Status Page's `status_domain`: the
public uptime status page of a team (nexaploy.monitoring, ARCHITECTURE.md §16).

Guest-accessible. The page data comes from monitoring.status_page_data (frappe.cache, 60 s) and
never contains internal data: monitors appear under their public name only (no URL, host,
path, probe or error text); incidents show their public title and the written updates."""

import frappe
from frappe import _

from nexaploy.portal_utils import text_direction

no_cache = 1


def _request_host():
	request = getattr(frappe.local, "request", None)
	try:
		return request.host if request is not None else None
	except Exception:
		return None


def get_context(context):
	from nexaploy.monitoring import resolve_status_page, status_page_data

	slug = frappe.form_dict.get("slug")
	name = resolve_status_page(slug=slug) if slug else resolve_status_page(host=_request_host())
	if not name:
		raise frappe.DoesNotExistError(_("This status page does not exist"))
	lang = getattr(frappe.local, "lang", None) or "en"
	data = status_page_data(name, lang)
	context.no_cache = 1
	context.show_sidebar = 0
	context.lang = lang
	context.dir = text_direction(lang)
	context.title = data["title"]
	context.status = data
	context.status_brand = data["brand"]
	return context
