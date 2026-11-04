"""Public marketplace API."""

import frappe

from nexaploy.utils import compatible_apps

LISTED_FIELDS = (
	"app_name",
	"title",
	"repo_url",
	"publisher",
	"category",
	"description",
	"pricing",
	"price_monthly",
	"approved",
	"logo",
	"documentation_url",
)


@frappe.whitelist(allow_guest=True)
def list_apps(frappe_major=None):
	"""Approved Marketplace Apps; with `frappe_major`, only apps that have a release for it
	(and only those releases). Returns
	[{app_name, title, repo_url, publisher, category, description, pricing, price_monthly,
	  logo, documentation_url, releases: [{frappe_major, branch, commit}]}].
	"""
	apps = frappe.get_all(
		"Marketplace App", filters={"approved": 1}, fields=list(LISTED_FIELDS), order_by="title asc"
	)
	releases = frappe.get_all(
		"Marketplace App Release",
		filters={"parenttype": "Marketplace App", "parent": ["in", [a.app_name for a in apps] or [""]]},
		fields=["parent", "frappe_major", "branch", "commit"],
		order_by="frappe_major asc",
	)
	by_app = {}
	for rel in releases:
		by_app.setdefault(rel.parent, []).append(
			{"frappe_major": rel.frappe_major, "branch": rel.branch, "commit": rel.commit}
		)
	items = []
	for app in apps:
		item = dict(app)
		item["releases"] = by_app.get(app.app_name, [])
		items.append(item)
	result = compatible_apps(items, frappe_major or None)
	for item in result:
		item.pop("approved", None)
	return result
