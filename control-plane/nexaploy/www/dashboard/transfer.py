"""/dashboard/transfer?token=<token> - accept or decline a site transfer
(see nexaploy/api/site_lifecycle.py)."""

from frappe import _

from nexaploy.api.dashboard import prepare_page

no_cache = 1


def get_context(context):
	return prepare_page(context, "transfer", _("Site transfer"))
