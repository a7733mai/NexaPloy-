"""Marketplace App Release gained a verification `status`. Releases that existed before were
curated by staff (or generated at approval) and stay visible: they become Verified."""

import frappe


def execute():
	if not frappe.db.has_column("Marketplace App Release", "status"):
		return
	frappe.db.sql(
		"update `tabMarketplace App Release` set status = 'Verified' where ifnull(status, '') = ''"
	)
