"""/dashboard/safe_update?site=<site> - test an update on a copy of the site before applying it
(see nexaploy/api/update_test.py and nexaploy/update_test.py)."""

from frappe import _

from nexaploy.api.dashboard import prepare_page

no_cache = 1


def get_context(context):
	return prepare_page(context, "site", _("Safe update"))
