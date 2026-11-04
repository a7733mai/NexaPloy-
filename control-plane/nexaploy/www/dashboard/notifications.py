"""/dashboard/notifications - where the current team receives alerts: Email, Telegram,
WhatsApp, Slack, Discord and signed webhooks (Owners/Admins; see
nexaploy.api.notifications and nexaploy/notify.py)."""

from frappe import _

from nexaploy.api.dashboard import prepare_page

no_cache = 1


def get_context(context):
	return prepare_page(context, "notifications", _("Notifications"))
