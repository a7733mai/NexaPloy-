"""/dashboard/publisher - marketplace publisher profile, listings, revenue and payouts
(see nexaploy.api.publisher). Listing review happens in the desk (Marketplace App)."""

from frappe import _

from nexaploy.api.dashboard import prepare_page

no_cache = 1


def get_context(context):
	return prepare_page(context, "publisher", _("Publisher"))
