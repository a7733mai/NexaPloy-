"""Team membership (ARCHITECTURE.md §9) — the only place membership is checked.

A Team owns servers, release groups, benches, sites, backups, subscriptions and invoices.
Members have one role each (Owner/Admin/Developer/Billing); `team_utils.role_allows` maps
roles to the actions read/write/billing/admin. Platform admins (System Manager /
Administrator) pass every check.

    is_platform_admin(user=None) -> bool
    get_user_teams(user=None) -> [{name, title, role}]
    get_current_team(user=None) -> str
    check_team_access(team, action="read", user=None) -> role (raises frappe.PermissionError)
    get_team_role(team, user=None) -> str | None
    team_notification_emails(team, kind="ops"|"billing") -> [email]

Two-factor: a Team with `two_factor_required` makes all its members hold the Frappe role
"Nexaploy 2FA" (Role.two_factor_auth = 1), which Frappe's login enforces once System
Settings.enable_two_factor_auth is on. sync_two_factor_roles() keeps the role in step
(Team.on_update / after_delete call it for current and former members).
"""

import frappe
from frappe import _

from nexaploy.team_utils import (
	OWNER,
	TEAM_COOKIE,
	TEAM_HEADER,
	TWO_FACTOR_ROLE,
	normalize_title,
	notification_emails,
	personal_team_title,
	pick_team,
	role_allows,
	team_slug,
	two_factor_role_changes,
	unique_slug,
	users_requiring_two_factor,
)

PLATFORM_ROLES = ("System Manager",)
_CACHE_KEY = "nexaploy_team_cache"


def _user(user=None):
	return user or frappe.session.user


def _cache():
	"""Per-request memo (frappe.local is reset for every request / job)."""
	cache = getattr(frappe.local, _CACHE_KEY, None)
	if cache is None:
		cache = {}
		setattr(frappe.local, _CACHE_KEY, cache)
	return cache


def clear_cache():
	setattr(frappe.local, _CACHE_KEY, {})


# --------------------------------------------------------------------------- roles


def is_platform_admin(user=None):
	"""System Manager or Administrator."""
	user = _user(user)
	if user == "Administrator":
		return True
	if not user or user == "Guest":
		return False
	return any(r in PLATFORM_ROLES for r in frappe.get_roles(user))


def get_user_teams(user=None):
	"""Enabled teams the user is a member of: [{name, title, role}], oldest team first."""
	user = _user(user)
	if not user or user == "Guest":
		return []
	cache = _cache()
	key = ("teams", user)
	if key not in cache:
		cache[key] = frappe.db.sql(
			"""
			select t.name, t.title, tm.role
			from `tabTeam Member` tm
			join `tabTeam` t on t.name = tm.parent
			where tm.parenttype = 'Team' and tm.parentfield = 'members'
				and tm.user = %s and t.enabled = 1
			order by t.creation asc, t.name asc
			""",
			(user,),
			as_dict=True,
		)
	return [dict(r) for r in cache[key]]


def get_team_role(team, user=None):
	"""The user's role in `team` (None when not a member or the team is disabled)."""
	if not team:
		return None
	return next((t["role"] for t in get_user_teams(user) if t["name"] == team), None)


def check_team_access(team, action="read", user=None):
	"""Raise frappe.PermissionError unless the user may perform `action` on `team`.

	Returns the user's role ("Owner" etc.; platform admins get their role or "Owner").
	"""
	user = _user(user)
	role = get_team_role(team, user) if team else None
	if is_platform_admin(user):
		if team and not frappe.db.exists("Team", team):
			raise frappe.DoesNotExistError(_("Team {0} not found").format(team))
		return role or OWNER
	if not team:
		raise frappe.PermissionError(_("No team selected"))
	if not role:
		raise frappe.PermissionError(_("You are not a member of team {0}").format(team))
	if not role_allows(role, action):
		raise frappe.PermissionError(
			_("Your role {0} in team {1} does not allow this action").format(_(role), team)
		)
	return role


def has_team_action(team, action="read", user=None):
	"""Boolean form of check_team_access."""
	try:
		check_team_access(team, action, user)
		return True
	except (frappe.PermissionError, frappe.DoesNotExistError):
		return False


# --------------------------------------------------------------------------- current team


def _requested_teams():
	"""Team names the client asked for, most specific first: header, then cookie."""
	values = []
	try:
		values.append(frappe.get_request_header(TEAM_HEADER))
	except Exception:
		pass
	request = getattr(frappe.local, "request", None)
	if request is not None:
		try:
			values.append(request.cookies.get(TEAM_COOKIE))
		except Exception:
			pass
	return [v.strip() for v in values if v and v.strip()]


def get_current_team(user=None):
	"""Team for this request: header X-Nexaploy-Team, else cookie nexaploy_team, else the
	user's first team. A personal team ("<user>'s team", Owner) is created when the user
	has none. Platform admins may select any existing team."""
	user = _user(user)
	if not user or user == "Guest":
		raise frappe.PermissionError(_("Please log in"))
	cache = _cache()
	key = ("current", user)
	if key in cache:
		return cache[key]

	requested = _requested_teams()
	teams = [t["name"] for t in get_user_teams(user)]
	team = pick_team(requested, teams)
	if is_platform_admin(user):
		for value in requested:
			if value not in teams and frappe.db.exists("Team", value):
				team = value
				break
	if not team:
		team = ensure_personal_team(user)
	cache[key] = team
	return team


def set_team_cookie(team):
	"""Remember the selected team in the nexaploy_team cookie (30 days)."""
	manager = getattr(frappe.local, "cookie_manager", None)
	if manager is None:
		return
	if team:
		from frappe.utils import add_days, now_datetime

		manager.set_cookie(TEAM_COOKIE, team, expires=add_days(now_datetime(), 30))
	else:
		manager.delete_cookie(TEAM_COOKIE)
	clear_cache()


# --------------------------------------------------------------------------- creation


def new_team_name(title):
	return unique_slug(team_slug(title), lambda name: bool(frappe.db.exists("Team", name)))


def create_team(title, owner=None, billing_email=None, currency=None):
	"""Insert a Team with `owner` as its Owner. Returns the Team doc."""
	owner = _user(owner)
	title = normalize_title(title)
	doc = frappe.get_doc(
		{
			"doctype": "Team",
			"team_name": new_team_name(title),
			"title": title,
			"owner_user": owner,
			"billing_email": billing_email or frappe.db.get_value("User", owner, "email") or None,
			"currency": currency or None,
			"enabled": 1,
			"members": [{"user": owner, "role": OWNER}],
		}
	)
	doc.insert(ignore_permissions=True)
	clear_cache()
	from nexaploy.activity import log_activity
	from nexaploy.billing.credits import grant_signup_credits

	log_activity(doc.name, "team.create", "Team", doc.name, {"title": title})
	grant_signup_credits(doc.name)  # Billing Settings.signup_credits, never raises
	return doc


def ensure_personal_team(user=None):
	"""The user's first team; creates "<user>'s team" (user = Owner) if they have none."""
	user = _user(user)
	teams = get_user_teams(user)
	if teams:
		return teams[0]["name"]
	# serialize concurrent first requests of the same user
	frappe.db.get_value("User", user, "name", for_update=True)
	clear_cache()
	teams = get_user_teams(user)
	if teams:
		return teams[0]["name"]
	full_name = frappe.db.get_value("User", user, "full_name")
	doc = create_team(personal_team_title(user, full_name), owner=user)
	request = getattr(frappe.local, "request", None)
	if request is not None and getattr(request, "method", "GET") in ("GET", "HEAD"):
		# GET page renders are not committed by Frappe; keep the team
		frappe.db.commit()
	return doc.name


def personal_team_for(user):
	"""Team owned by `user` (owner_user) — used to map legacy user-valued `team` fields."""
	if not user:
		return None
	if frappe.db.exists("Team", user):
		return user
	name = frappe.db.get_value("Team", {"owner_user": user}, "name", order_by="creation asc")
	if name:
		return name
	if frappe.db.exists("User", user):
		return ensure_personal_team(user)
	return None


# --------------------------------------------------------------------------- members & notifications


def get_members(team):
	"""[{user, role, email, full_name, enabled}] in row order."""
	rows = frappe.get_all(
		"Team Member",
		filters={"parenttype": "Team", "parentfield": "members", "parent": team},
		fields=["user", "role"],
		order_by="idx asc",
	)
	if not rows:
		return []
	users = {
		u.name: u
		for u in frappe.get_all(
			"User",
			filters={"name": ["in", [r.user for r in rows]]},
			fields=["name", "email", "full_name", "enabled"],
		)
	}
	result = []
	for r in rows:
		u = users.get(r.user) or frappe._dict()
		result.append(
			{
				"user": r.user,
				"role": r.role,
				"email": u.get("email") or (r.user if "@" in (r.user or "") else None),
				"full_name": u.get("full_name") or r.user,
				"enabled": bool(u.get("enabled", 1)),
			}
		)
	return result


def team_notification_emails(team, kind="ops"):
	"""Who to email about `team`: ops (alerts) → Owner/Admin/Developer; billing →
	billing_email + Owner/Billing members. Disabled users are skipped."""
	if not team or not frappe.db.exists("Team", team):
		return []
	billing_email = frappe.db.get_value("Team", team, "billing_email") if kind == "billing" else None
	members = [m for m in get_members(team) if m["enabled"]]
	return notification_emails(members, kind, billing_email)


# --------------------------------------------------------------------------- two-factor authentication


def ensure_two_factor_role():
	"""Role "Nexaploy 2FA" with two_factor_auth=1 and no desk access (idempotent)."""
	if frappe.db.exists("Role", TWO_FACTOR_ROLE):
		if not frappe.db.get_value("Role", TWO_FACTOR_ROLE, "two_factor_auth"):
			frappe.db.set_value("Role", TWO_FACTOR_ROLE, "two_factor_auth", 1)
		return TWO_FACTOR_ROLE
	frappe.get_doc(
		{
			"doctype": "Role",
			"role_name": TWO_FACTOR_ROLE,
			"desk_access": 0,
			"two_factor_auth": 1,
			"is_custom": 1,
		}
	).insert(ignore_permissions=True)
	return TWO_FACTOR_ROLE


def ensure_system_two_factor():
	"""Turn on System Settings.enable_two_factor_auth (OTP App by default). Without it Frappe
	ignores Role.two_factor_auth. Returns True when the setting was changed.

	Site-wide but harmless for everyone else: Frappe only asks for a second factor when one
	of the user's roles has two_factor_auth (or the "All" role has it)."""
	changed = False
	if not frappe.db.get_single_value("System Settings", "enable_two_factor_auth"):
		frappe.db.set_single_value("System Settings", "enable_two_factor_auth", 1)
		changed = True
	if not frappe.db.get_single_value("System Settings", "two_factor_method"):
		frappe.db.set_single_value("System Settings", "two_factor_method", "OTP App")
		changed = True
	return changed


def _two_factor_memberships(users):
	if not users:
		return []
	return frappe.db.sql(
		"""
		select tm.user, t.two_factor_required, t.enabled
		from `tabTeam Member` tm
		join `tabTeam` t on t.name = tm.parent
		where tm.parenttype = 'Team' and tm.parentfield = 'members' and tm.user in %(users)s
		""",
		{"users": tuple(users)},
		as_dict=True,
	)


def _role_holders(users):
	if not users:
		return []
	return frappe.get_all(
		"Has Role",
		filters={"parenttype": "User", "role": TWO_FACTOR_ROLE, "parent": ["in", list(users)]},
		pluck="parent",
	)


def sync_two_factor_roles(users):
	"""Give the 2FA role to `users` that belong to an enabled team requiring 2FA and take it
	from those that no longer do. Returns {added, removed, system_enabled}."""
	users = sorted({u for u in users or [] if u})
	result = {"added": [], "removed": [], "system_enabled": False}
	if not users:
		return result
	required = users_requiring_two_factor(_two_factor_memberships(users))
	add, remove = two_factor_role_changes(users, required, _role_holders(users))
	if add:
		ensure_two_factor_role()
		result["system_enabled"] = ensure_system_two_factor()
	for user in add:
		if not frappe.db.exists("User", user):
			continue
		row = frappe.get_doc(
			{
				"doctype": "Has Role",
				"parent": user,
				"parenttype": "User",
				"parentfield": "roles",
				"role": TWO_FACTOR_ROLE,
			}
		)
		row.db_insert()
		result["added"].append(user)
	for user in remove:
		frappe.db.delete("Has Role", {"parent": user, "parenttype": "User", "role": TWO_FACTOR_ROLE})
		result["removed"].append(user)
	for user in result["added"] + result["removed"]:
		frappe.clear_cache(user=user)
	return result


def sync_team_two_factor(doc):
	"""Team.on_update: re-sync current and former members of `doc`."""
	users = {m.user for m in doc.get("members") or []}
	before = doc.get_doc_before_save() if hasattr(doc, "get_doc_before_save") else None
	if before is not None:
		users |= {m.user for m in before.get("members") or []}
	if not users:
		return None
	try:
		return sync_two_factor_roles(users)
	except Exception:
		frappe.log_error(title=f"Nexaploy: 2FA role sync for team {doc.name} failed")
		return None


def two_factor_status(user=None):
	"""{system_enabled, has_role, enforced, method, required_by: [{name, title}]} for `user`."""
	user = _user(user)
	system_enabled = bool(frappe.db.get_single_value("System Settings", "enable_two_factor_auth"))
	has_role = TWO_FACTOR_ROLE in frappe.get_roles(user)
	enforced = False
	try:
		from frappe.twofactor import two_factor_is_enabled

		enforced = bool(two_factor_is_enabled(user=user))
	except Exception:
		enforced = system_enabled and has_role
	required_by = frappe.db.sql(
		"""
		select t.name, t.title from `tabTeam Member` tm join `tabTeam` t on t.name = tm.parent
		where tm.parenttype = 'Team' and tm.parentfield = 'members' and tm.user = %s
			and t.enabled = 1 and t.two_factor_required = 1
		order by t.title
		""",
		(user,),
		as_dict=True,
	)
	return {
		"system_enabled": system_enabled,
		"has_role": has_role,
		"enforced": enforced,
		"method": frappe.db.get_single_value("System Settings", "two_factor_method") or None,
		"required_by": [dict(r) for r in required_by],
	}
