"""Official app catalog seed (idempotent): run by after_install and patch v0_4.

Creates the "Nexaploy Official" team (no members) + Marketplace Publisher and the
well-known Frappe apps of marketplace_utils.OFFICIAL_APPS as approved, free, Published
listings with Verified releases per Frappe major. Re-running only fills what is missing:
- an app that does not exist is created;
- an app owned by the official publisher (or a platform app without publisher) gets the
  releases of majors it lacks and its required apps when empty - curated rows are kept;
- an app listed by another publisher is left alone.
"""

import frappe

from nexaploy import marketplace_utils as mu


def ensure_official_publisher():
	if not frappe.db.exists("Team", mu.OFFICIAL_TEAM):
		frappe.get_doc(
			{
				"doctype": "Team",
				"team_name": mu.OFFICIAL_TEAM,
				"title": mu.OFFICIAL_PUBLISHER_TITLE,
				"enabled": 1,
			}
		).insert(ignore_permissions=True)
	name = frappe.db.get_value("Marketplace Publisher", {"team": mu.OFFICIAL_TEAM}, "name")
	if name:
		return name
	doc = frappe.get_doc(
		{
			"doctype": "Marketplace Publisher",
			"title": mu.OFFICIAL_PUBLISHER_TITLE,
			"team": mu.OFFICIAL_TEAM,
			"verified": 1,
			"payout_method": "Credits",
			"revenue_share_percent": 0,
		}
	).insert(ignore_permissions=True)
	return doc.name


def seed_official_catalog():
	publisher = ensure_official_publisher()
	created, updated = [], []
	for seed in mu.OFFICIAL_APPS:
		name = seed["app_name"]
		if not frappe.db.exists("Marketplace App", name):
			values = mu.official_app_values(seed)
			values["releases"] = [dict(r, status=mu.VERIFIED) for r in values["releases"]]
			doc = frappe.get_doc({"doctype": "Marketplace App", "publisher_profile": publisher, **values})
			doc.flags.ignore_links = True
			doc.insert(ignore_permissions=True)
			created.append(name)
			continue
		doc = frappe.get_doc("Marketplace App", name)
		if doc.publisher_profile and doc.publisher_profile != publisher:
			continue
		changed = False
		for rel in mu.missing_seed_releases([r.as_dict() for r in doc.releases], seed):
			doc.append("releases", dict(rel, status=mu.VERIFIED))
			changed = True
		if not (doc.required_apps or "").strip() and seed.get("required_apps"):
			doc.required_apps = mu.format_required_apps(seed["required_apps"])
			changed = True
		if not doc.publisher_profile:
			doc.publisher_profile = publisher
			changed = True
		if changed:
			doc.save(ignore_permissions=True)
			updated.append(name)
	return {"created": created, "updated": updated}


def safe_seed():
	"""Never fail an install over the catalog."""
	try:
		return seed_official_catalog()
	except Exception:
		frappe.log_error(title="Nexaploy: seeding the official marketplace catalog failed")
		return None
