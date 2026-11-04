"""/dashboard/activity - the current team's activity log (Owners/Admins; see
nexaploy.api.teams.get_activity and nexaploy/activity.py)."""

from frappe import _

from nexaploy.api.dashboard import prepare_page

no_cache = 1


def get_context(context):
	return prepare_page(context, "activity", _("Activity"))
