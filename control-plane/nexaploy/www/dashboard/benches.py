"""/dashboard/benches - customer portal page (see nexaploy/api/benches.py, public/js/benches.js)."""

from frappe import _

no_cache = 1


def get_context(context):
	from nexaploy.api.dashboard import prepare_page

	return prepare_page(context, "benches", _("Bench Groups"))
