"""Teams (ARCHITECTURE.md §9): `team` fields used to link a User; they now link a Team.

For every distinct User found in a `team` field of a Nexaploy DocType, reuse a team the
user owns (owner_user / Owner member) or create the personal team "<user>'s team" with
that user as Owner, then rewrite the field to the Team name. Idempotent: values that are
already Team names are left alone; values that are neither are reported and kept.
"""

import frappe

from nexaploy.team_utils import OWNER, personal_team_title, team_slug, unique_slug


def team_doctypes():
	"""Non-child Nexaploy DocTypes with a `team` Link field."""
	rows = frappe.db.sql(
		"""
		select df.parent
		from `tabDocField` df
		join `tabDocType` dt on dt.name = df.parent
		where df.fieldname = 'team' and df.fieldtype = 'Link'
			and dt.module = 'Nexaploy' and dt.istable = 0 and dt.name not in ('Team', 'Team Invitation')
		order by df.parent
		""",
		pluck=True,
	)
	return [dt for dt in rows if frappe.db.table_exists(dt)]


def team_for_user(user):
	name = frappe.db.get_value("Team", {"owner_user": user}, "name", order_by="creation asc")
	if not name:
		name = frappe.db.get_value(
			"Team Member", {"parenttype": "Team", "user": user, "role": OWNER}, "parent", order_by="creation asc"
		)
	if name:
		return name
	full_name = frappe.db.get_value("User", user, "full_name")
	title = personal_team_title(user, full_name)
	doc = frappe.get_doc(
		{
			"doctype": "Team",
			"team_name": unique_slug(team_slug(title), lambda n: bool(frappe.db.exists("Team", n))),
			"title": title,
			"owner_user": user,
			"billing_email": frappe.db.get_value("User", user, "email") or None,
			"enabled": 1,
			"members": [{"user": user, "role": OWNER}],
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def execute():
	frappe.reload_doc("nexaploy", "doctype", "team_member")
	frappe.reload_doc("nexaploy", "doctype", "team")
	frappe.reload_doc("nexaploy", "doctype", "team_invitation")

	mapping = {}
	unknown = set()
	doctypes = team_doctypes()
	for dt in doctypes:
		values = frappe.db.sql(
			f"select distinct `team` from `tab{dt}` where ifnull(`team`, '') != ''", pluck=True
		)
		for value in values:
			if value in mapping or frappe.db.exists("Team", value):
				continue
			if frappe.db.exists("User", value):
				mapping[value] = team_for_user(value)
			else:
				unknown.add(value)

	for user, team in mapping.items():
		for dt in doctypes:
			frappe.db.sql(f"update `tab{dt}` set `team` = %s where `team` = %s", (team, user))

	if unknown:
		print(f"nexaploy user_team_to_team: kept unknown team values: {sorted(unknown)}")
	frappe.db.commit()
