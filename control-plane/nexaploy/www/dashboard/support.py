"""/dashboard/support - the current team's support tickets; ?name=<ticket> opens one
(see nexaploy.api.support)."""

from frappe import _

from nexaploy.api.dashboard import prepare_page

no_cache = 1


def get_context(context):
	return prepare_page(context, "support", _("Support"))
