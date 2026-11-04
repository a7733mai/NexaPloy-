"""Portal API of "Console access" (/dashboard/console-access): per-member console capabilities,
the team's "require 2FA for console" flag and the Console Audit Log. Team Owners/Admins only
(the `admin` action); my_capabilities is open to every member.

Logic: nexaploy.console_access (frappe) and nexaploy.console_access_utils (pure). The capability
checks themselves run in nexaploy.api.observability (server-side, for every call).

    get_console_access()                              members + effective capabilities, 2FA flag
    set_member_capabilities(user, capabilities=None, reset=0)
    set_console_two_factor(required)
    get_console_audit(site=None, user=None, kind=None, status=None, date_from=None, date_to=None, page=1)
    my_capabilities(team=None)
"""

import frappe
from frappe import _
from frappe.utils import add_days, getdate

from nexaploy import console_access_utils as cu
from nexaploy.console_access import AUDIT, console_two_factor_required, get_capabilities, user_has_two_factor

AUDIT_FIELDS = [
	"name", "creation", "executed_at", "user", "site", "kind", "action", "status", "duration", "ip_address",
	"agent_job", "read_only_mode", "commit_changes", "command", "command_truncated", "error",
]


def _portal_user():
	user = frappe.session.user
	if not user or user == "Guest":
		raise frappe.PermissionError(_("Please log in"))
	return user


def _admin_team(user):
	from nexaploy.teams import check_team_access, get_current_team

	team = get_current_team(user)
	role = check_team_access(team, "admin", user)
	return team, role


def _bool(value):
	return str(value).strip().lower() in ("1", "true", "yes", "on")


@frappe.whitelist()
def my_capabilities(team=None):
	"""The current user's console capabilities in the current (or given) team."""
	from nexaploy.teams import check_team_access, get_current_team

	user = _portal_user()
	team = team or get_current_team(user)
	check_team_access(team, "read", user)
	required = console_two_factor_required(team)
	return {
		"team": team,
		"capabilities": get_capabilities(team, user),
		"two_factor_required": required,
		"two_factor_ok": (not required) or user_has_two_factor(user),
	}


@frappe.whitelist()
def get_console_access():
	"""Members with their effective capabilities and whether the caller may edit them."""
	from nexaploy.teams import get_members, is_platform_admin

	user = _portal_user()
	team, role = _admin_team(user)
	admin = is_platform_admin(user)
	rows = {
		r.user: r
		for r in frappe.get_all(
			"Team Member",
			filters={"parenttype": "Team", "parentfield": "members", "parent": team},
			fields=["user", "role", "custom_capabilities", *cu.CAPABILITIES],
		)
	}
	members = []
	for m in get_members(team):
		row = rows.get(m["user"]) or {}
		members.append(
			{
				"user": m["user"],
				"full_name": m["full_name"],
				"role": m["role"],
				"custom": bool(row.get("custom_capabilities")) and m["role"] != "Owner",
				"capabilities": cu.effective_capabilities(m["role"], row),
				"editable": cu.can_edit_capabilities(role, m["role"], admin),
				"is_you": m["user"] == user,
			}
		)
	return {
		"team": team,
		"role": role,
		"members": members,
		"capabilities": [{"key": c, "label": cu.CAPABILITY_LABELS[c]} for c in cu.CAPABILITIES],
		"role_defaults": cu.ROLE_DEFAULTS,
		"console_two_factor_required": console_two_factor_required(team),
		"system_two_factor": bool(frappe.db.get_single_value("System Settings", "enable_two_factor_auth")),
	}


@frappe.whitelist(methods=["POST"])
def set_member_capabilities(user, capabilities=None, reset=0):
	"""Customise (or reset to the role defaults) one member's console capabilities."""
	from nexaploy.activity import log_activity
	from nexaploy.teams import clear_cache, is_platform_admin

	actor = _portal_user()
	team, role = _admin_team(actor)
	doc = frappe.get_doc("Team", team)
	member = next((m for m in doc.members if m.user == user), None)
	if not member:
		frappe.throw(_("{0} is not a member of this team").format(user))
	if not cu.can_edit_capabilities(role, member.role, is_platform_admin(actor)):
		raise frappe.PermissionError(_("You cannot change the console permissions of a team {0}").format(_(member.role)))
	caps = frappe.parse_json(capabilities) if isinstance(capabilities, str) else (capabilities or {})
	if not isinstance(caps, dict):
		frappe.throw(_("capabilities must be an object"))
	unknown = set(caps) - set(cu.CAPABILITIES)
	if unknown:
		frappe.throw(_("Unknown capability {0}").format(", ".join(sorted(unknown))))
	before = cu.effective_capabilities(member.role, member.as_dict())
	values = cu.capability_changes(member.role, member.as_dict(), caps, reset=_bool(reset))
	member.update(values)
	doc.save(ignore_permissions=True)
	clear_cache()
	after = cu.effective_capabilities(member.role, values)
	log_activity(team, "team.console_permissions", "Team", team, {
		"user": user, "reset": _bool(reset),
		"changed": {k: after[k] for k in cu.CAPABILITIES if after[k] != before[k]},
	})
	return {"user": user, "custom": bool(values["custom_capabilities"]), "capabilities": after}


@frappe.whitelist(methods=["POST"])
def set_console_two_factor(required):
	"""Require two-factor login for SQL and the console in the current team."""
	from nexaploy.activity import log_activity

	actor = _portal_user()
	team, _role = _admin_team(actor)
	value = 1 if _bool(required) else 0
	if int(frappe.db.get_value("Team", team, "console_two_factor_required") or 0) != value:
		frappe.db.set_value("Team", team, "console_two_factor_required", value)
		log_activity(team, "team.console_two_factor", "Team", team, {"required": bool(value)})
	return {
		"console_two_factor_required": bool(value),
		"you_have_two_factor": user_has_two_factor(actor),
		"system_two_factor": bool(frappe.db.get_single_value("System Settings", "enable_two_factor_auth")),
	}


@frappe.whitelist()
def get_console_audit(site=None, user=None, kind=None, status=None, date_from=None, date_to=None, page=1, page_length=50):
	"""Console Audit Log of the current team, newest first (Owners/Admins). Read only."""
	actor = _portal_user()
	team, _role = _admin_team(actor)
	page = max(int(page or 1), 1)
	page_length = min(max(int(page_length or 50), 1), 200)
	filters = [["team", "=", team]]
	if site:
		filters.append(["site", "=", site])
	if user:
		filters.append(["user", "=", user])
	if kind:
		if kind not in ("sql", "console", "logs"):
			frappe.throw(_("Unknown kind {0}").format(kind))
		filters.append(["kind", "=", kind])
	if status:
		if status not in cu.AUDIT_STATUSES:
			frappe.throw(_("Unknown status {0}").format(status))
		filters.append(["status", "=", status])
	if date_from:
		filters.append(["creation", ">=", getdate(date_from)])
	if date_to:
		filters.append(["creation", "<", add_days(getdate(date_to), 1)])
	rows = frappe.get_all(
		AUDIT, filters=filters, fields=AUDIT_FIELDS, order_by="creation desc",
		start=(page - 1) * page_length, limit_page_length=page_length + 1,
	)
	has_more = len(rows) > page_length
	rows = rows[:page_length]
	names = {r.user for r in rows if r.user}
	fullnames = {}
	if names:
		fullnames = {u.name: u.full_name for u in frappe.get_all("User", filters={"name": ["in", list(names)]}, fields=["name", "full_name"])}
	for r in rows:
		r["full_name"] = fullnames.get(r.user) or r.user
	sites = frappe.get_all(AUDIT, filters={"team": team}, pluck="site", distinct=True, order_by="site asc")
	from nexaploy.teams import get_members

	return {
		"rows": rows,
		"page": page,
		"page_length": page_length,
		"has_more": has_more,
		"sites": sorted({s for s in sites if s}),
		"users": [{"user": m["user"], "full_name": m["full_name"]} for m in get_members(team)],
		"statuses": list(cu.AUDIT_STATUSES),
	}
