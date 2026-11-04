"""/dashboard/backups - backup destinations (S3, Google Drive, OneDrive, Dropbox), which servers
use them, retention (team default and per site) and restore (see
nexaploy.api.backup_destinations)."""

from frappe import _

from nexaploy.api.dashboard import prepare_page

no_cache = 1


def get_context(context):
	return prepare_page(context, "backups", _("Backups"))
