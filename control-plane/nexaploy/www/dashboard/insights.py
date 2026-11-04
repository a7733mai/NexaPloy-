"""/dashboard/insights?site=<site> - site analytics, logs, slow queries, SQL playground and
console (see nexaploy/api/observability.py)."""

from frappe import _

from nexaploy.api.dashboard import prepare_page

no_cache = 1


def get_context(context):
	return prepare_page(context, "site", _("Insights"))
