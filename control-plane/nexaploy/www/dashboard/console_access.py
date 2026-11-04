"""/dashboard/console_access - per-member console permissions, "require 2FA for console" and the
Console Audit Log (team Owners/Admins; see nexaploy/api/console_access.py)."""

from frappe import _

from nexaploy.api.dashboard import prepare_page

no_cache = 1


def get_context(context):
	return prepare_page(context, "activity", _("Console access"))
