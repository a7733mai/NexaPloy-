"""/dashboard/account - the session user's profile, language, two-factor status and API key
(see nexaploy/api/account.py)."""

from frappe import _

from nexaploy.api.dashboard import prepare_page

no_cache = 1


def get_context(context):
	return prepare_page(context, "account", _("Account"))
