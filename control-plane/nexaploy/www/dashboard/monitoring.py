"""/dashboard/monitoring - external uptime monitors of the current team's sites, incidents and the
public status page (nexaploy.api.monitoring, nexaploy/monitoring.py; ARCHITECTURE.md §16)."""

from frappe import _

from nexaploy.api.dashboard import prepare_page

no_cache = 1


def get_context(context):
	return prepare_page(context, "monitoring", _("Monitoring"))
