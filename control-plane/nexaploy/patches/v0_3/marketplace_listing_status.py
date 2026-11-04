"""Marketplace App gained `status` (Draft/In Review/Published/Rejected, Published <=> approved).
Existing approved apps become Published, the others Draft."""

import frappe


def execute():
	if not frappe.db.has_column("Marketplace App", "status"):
		return
	frappe.db.sql("update `tabMarketplace App` set status = 'Published' where approved = 1")
	frappe.db.sql(
		"update `tabMarketplace App` set status = 'Draft' "
		"where ifnull(approved, 0) = 0 and ifnull(status, '') in ('', 'Published')"
	)
