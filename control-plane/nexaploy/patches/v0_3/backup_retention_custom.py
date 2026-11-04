"""Site gained `backup_retention_custom` (team default otherwise). Sites whose retention was
changed from the old default (14 days) keep it as their own policy; local copies keep the
previous agent default (14 days)."""

import frappe


def execute():
	if not frappe.db.has_column("Site", "backup_retention_custom"):
		return
	frappe.db.sql(
		"update `tabSite` set backup_retention_custom = 1 "
		"where ifnull(backup_retention_days, 14) != 14 and ifnull(backup_retention_custom, 0) = 0"
	)
