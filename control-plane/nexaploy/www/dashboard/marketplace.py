"""/dashboard/marketplace - customer portal page (see nexaploy/api/dashboard.py)."""

from frappe import _

from nexaploy.api.dashboard import prepare_page

no_cache = 1


def get_context(context):
	return prepare_page(context, "marketplace", _("Marketplace"))
