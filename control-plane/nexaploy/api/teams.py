"""Team management API for the customer portal (/dashboard/team), ARCHITECTURE.md §9.

Membership checks go through nexaploy.teams. Invitations store only sha256(token); the
plaintext token is in the emailed accept link /dashboard/team?invite=<token> (7 days).
Rules: only Owners grant, revoke or remove the Owner role, and a team always keeps at
least one Owner.
"""

import frappe
from frappe import _
from frappe.utils import get_url, now_datetime

from nexaploy.portal_utils import has_portal_access
from nexaploy.team_utils import (
	OWNER,
	can_invite_role,
	check_member_change,
	generate_invitation_token,
	hash_invitation_token,
	invitation_expired,
	invitation_expiry,
	invitation_url,
	normalize_email,
	normalize_role,
	normalize_title,
	role_allows,
)
from nexaploy.teams import (
	check_team_access,
	clear_cache,
	create_team as _create_team,
	get_current_team,
	get_members,
	get_team_role,
	get_user_teams,
	is_platform_admin,
	set_team_cookie,
)

CUSTOMER_ROLE = "Nexaploy Customer"
MAX_OWNED_TEAMS = 20
INVITATION = "Team Invitation"


def _login():
	user = frappe.session.user
	if not user or user == "Guest":
		raise frappe.PermissionError(_("Please log in to use the dashboard"))
	return user


def _portal_user():
	user = _login()
	if not has_portal_access(frappe.get_roles(user), user):
		raise frappe.PermissionError(_("You need the Nexaploy Customer role to use the dashboard"))
	return user


def _value_error(fn, *args):
	try:
		return fn(*args)
	except ValueError as exc:
		frappe.throw(_(str(exc)))


def _actor_role(team, user):
	"""Role used for Owner-only rules; platform admins act as Owners."""
	return OWNER if is_platform_admin(user) else get_team_role(team, user)


def _team_doc(team):
	return frappe.get_doc("Team", team)


# --------------------------------------------------------------------------- teams


@frappe.whitelist()
def my_teams():
	"""{current, teams: [{name, title, role}]}"""
	user = _portal_user()
	return {"current": get_current_team(user), "teams": get_user_teams(user)}


@frappe.whitelist(methods=["POST"])
def switch_team(team):
	"""Select `team` for this browser (cookie nexaploy_team). Returns {team, title, role}."""
	user = _portal_user()
	role = check_team_access(team, "read", user)
	set_team_cookie(team)
	return {"team": team, "title": frappe.db.get_value("Team", team, "title"), "role": role}


@frappe.whitelist(methods=["POST"])
def create_team(title):
	"""New team with the session user as Owner; also selects it. Returns {name, title}."""
	user = _portal_user()
	title = _value_error(normalize_title, title)
	owned = frappe.db.count("Team", {"owner_user": user})
	if owned >= MAX_OWNED_TEAMS and not is_platform_admin(user):
		frappe.throw(_("You can own at most {0} teams").format(MAX_OWNED_TEAMS))
	doc = _create_team(title, owner=user)
	set_team_cookie(doc.name)
	return {"name": doc.name, "title": doc.title, "role": OWNER}


@frappe.whitelist()
def get_team(team=None):
	"""Current (or given) team: details, members, and pending invitations for admins."""
	user = _portal_user()
	team = team or get_current_team(user)
	role = check_team_access(team, "read", user)
	doc = _team_doc(team)
	can_admin = role_allows(role, "admin")
	members = get_members(team)
	for m in members:
		m["is_you"] = m["user"] == user
		m.pop("enabled", None)
		if not can_admin:
			m.pop("email", None)
	invitations = []
	if can_admin:
		_expire_invitations(team)
		invitations = frappe.get_all(
			INVITATION,
			filters={"team": team, "status": "Pending"},
			fields=["name", "email", "role", "status", "expires_on", "invited_by", "creation"],
			order_by="creation desc",
		)
	return {
		"name": doc.name,
		"title": doc.title,
		"billing_email": doc.billing_email if role_allows(role, "billing") else None,
		"currency": doc.currency,
		"two_factor_required": bool(doc.two_factor_required),
		"owner_user": doc.owner_user,
		"role": role,
		"permissions": {a: role_allows(role, a) for a in ("read", "write", "billing", "admin")},
		"is_owner": role == OWNER,
		"members": members,
		"invitations": invitations,
		"roles": ["Owner", "Admin", "Developer", "Billing"],
	}


@frappe.whitelist(methods=["POST"])
def update_team(title=None, billing_email=None):
	"""Rename the current team / set its billing email (admin)."""
	user = _portal_user()
	team = get_current_team(user)
	check_team_access(team, "admin", user)
	doc = _team_doc(team)
	if title is not None:
		doc.title = _value_error(normalize_title, title)
	if billing_email is not None:
		doc.billing_email = _value_error(normalize_email, billing_email) if billing_email else None
	doc.save(ignore_permissions=True)
	return {"name": doc.name, "title": doc.title, "billing_email": doc.billing_email}


# --------------------------------------------------------------------------- invitations


def _expire_invitations(team):
	now = now_datetime()
	for row in frappe.get_all(
		INVITATION, filters={"team": team, "status": "Pending", "expires_on": ["<=", now]}, pluck="name"
	):
		frappe.db.set_value(INVITATION, row, "status", "Expired", update_modified=False)


def _send_invitation(email, team_title, inviter, role, url):
	subject = _("You are invited to join {0} on Nexaploy").format(team_title)
	message = "<p>{}</p><p>{}</p><p><a href=\"{}\">{}</a></p><p>{}</p>".format(
		frappe.utils.escape_html(
			_("{0} invited you to the team {1} as {2}.").format(inviter, team_title, _(role))
		),
		frappe.utils.escape_html(_("Open the link below to accept the invitation. It expires in 7 days.")),
		url,
		frappe.utils.escape_html(_("Accept invitation")),
		frappe.utils.escape_html(_("If you did not expect this email, you can ignore it.")),
	)
	try:
		frappe.sendmail(recipients=[email], subject=subject, message=message, delayed=False)
		return True
	except Exception:
		frappe.log_error(title=f"Nexaploy: invitation email to {email} failed")
		return False


@frappe.whitelist(methods=["POST"])
def invite_member(email, role="Developer"):
	"""Invite `email` to the current team (admin). Emails the accept link and returns
	{name, email, role, expires_on, invite_url, email_sent}."""
	user = _portal_user()
	team = get_current_team(user)
	check_team_access(team, "admin", user)
	email = _value_error(normalize_email, email)
	role = _value_error(normalize_role, role)
	if not can_invite_role(_actor_role(team, user), role):
		raise frappe.PermissionError(_("Only an Owner can invite Owners"))

	members = get_members(team)
	if any((m.get("email") or "").lower() == email or m["user"].lower() == email for m in members):
		frappe.throw(_("{0} is already a member of this team").format(email))

	# one live invitation per (team, email): a new invite replaces the old one
	for old in frappe.get_all(INVITATION, filters={"team": team, "email": email, "status": "Pending"}, pluck="name"):
		frappe.db.set_value(INVITATION, old, "status", "Revoked")

	token = generate_invitation_token()
	doc = frappe.get_doc(
		{
			"doctype": INVITATION,
			"team": team,
			"email": email,
			"role": role,
			"token_hash": hash_invitation_token(token),
			"expires_on": invitation_expiry(now_datetime()),
			"status": "Pending",
			"invited_by": user,
		}
	)
	doc.insert(ignore_permissions=True)
	url = invitation_url(get_url(), token)
	title = frappe.db.get_value("Team", team, "title") or team
	sent = _send_invitation(email, title, frappe.utils.get_fullname(user) or user, role, url)
	return {
		"name": doc.name,
		"email": email,
		"role": role,
		"expires_on": str(doc.expires_on),
		"invite_url": url,
		"email_sent": sent,
	}


def _invitation_by_token(token):
	if not token:
		frappe.throw(_("Invitation token is required"))
	name = frappe.db.get_value(INVITATION, {"token_hash": hash_invitation_token(token)}, "name")
	if not name:
		raise frappe.DoesNotExistError(_("This invitation link is invalid"))
	return frappe.get_doc(INVITATION, name)


@frappe.whitelist()
def get_invitation(token):
	"""Preview an invitation for the accept screen: {team_title, role, email, status, matches}."""
	user = _login()
	inv = _invitation_by_token(token)
	status = inv.status
	if status == "Pending" and invitation_expired(inv.expires_on, now_datetime()):
		status = "Expired"
	email = (frappe.db.get_value("User", user, "email") or user).lower()
	return {
		"team": inv.team,
		"team_title": frappe.db.get_value("Team", inv.team, "title"),
		"role": inv.role,
		"email": inv.email,
		"status": status,
		"matches": email == inv.email or user.lower() == inv.email,
	}


@frappe.whitelist(methods=["POST"])
def accept_invitation(token):
	"""Join the invitation's team with its role. The invitation must be Pending, not expired
	and addressed to the session user's email. Selects the team; returns {team, title, role}."""
	user = _login()
	inv = _invitation_by_token(token)
	frappe.db.get_value(INVITATION, inv.name, "name", for_update=True)
	inv.reload()
	if inv.status != "Pending":
		frappe.throw(_("This invitation is {0}").format(_(inv.status)))
	if invitation_expired(inv.expires_on, now_datetime()):
		inv.db_set("status", "Expired")
		frappe.throw(_("This invitation has expired"))
	email = (frappe.db.get_value("User", user, "email") or user).lower()
	if inv.email not in (email, user.lower()):
		raise frappe.PermissionError(_("This invitation was sent to {0}").format(inv.email))

	team = _team_doc(inv.team)
	if not team.enabled:
		frappe.throw(_("Team {0} is disabled").format(team.title))
	existing = next((m for m in team.members if m.user == user), None)
	if existing is None:
		team.append("members", {"user": user, "role": inv.role})
		team.save(ignore_permissions=True)

	if CUSTOMER_ROLE not in frappe.get_roles(user):
		frappe.get_doc("User", user).add_roles(CUSTOMER_ROLE)

	inv.db_set({"status": "Accepted", "accepted_by": user, "accepted_on": now_datetime()})
	clear_cache()
	set_team_cookie(team.name)
	return {"team": team.name, "title": team.title, "role": existing.role if existing else inv.role}


@frappe.whitelist(methods=["POST"])
def revoke_invitation(name):
	"""Revoke a Pending invitation of a team you administer."""
	user = _portal_user()
	team = frappe.db.get_value(INVITATION, name, "team")
	if not team:
		raise frappe.DoesNotExistError(_("Invitation {0} not found").format(name))
	check_team_access(team, "admin", user)
	inv = frappe.get_doc(INVITATION, name)
	if inv.status != "Pending":
		frappe.throw(_("This invitation is {0}").format(_(inv.status)))
	inv.db_set("status", "Revoked")
	return {"name": name, "status": "Revoked"}


# --------------------------------------------------------------------------- members


def _change_member(team, target, new_role, user):
	doc = _team_doc(team)
	members = [{"user": m.user, "role": m.role} for m in doc.members]
	try:
		check_member_change(members, target, new_role, _actor_role(team, user))
	except ValueError as exc:
		frappe.throw(_(str(exc)))
	if new_role is None:
		doc.members = [m for m in doc.members if m.user != target]
		for i, m in enumerate(doc.members, start=1):
			m.idx = i
	else:
		for m in doc.members:
			if m.user == target:
				m.role = normalize_role(new_role)
	doc.save(ignore_permissions=True)
	clear_cache()
	return doc


@frappe.whitelist(methods=["POST"])
def remove_member(user):
	"""Remove `user` from the current team (admin; Owners only by Owners; never the last Owner)."""
	actor = _portal_user()
	team = get_current_team(actor)
	check_team_access(team, "admin", actor)
	_change_member(team, user, None, actor)
	return {"team": team, "removed": user}


@frappe.whitelist(methods=["POST"])
def change_role(user, role):
	"""Give `user` a new role in the current team (admin; Owner role only by Owners;
	the last Owner cannot be demoted)."""
	actor = _portal_user()
	team = get_current_team(actor)
	check_team_access(team, "admin", actor)
	role = _value_error(normalize_role, role)
	_change_member(team, user, role, actor)
	return {"team": team, "user": user, "role": role}


@frappe.whitelist(methods=["POST"])
def leave_team(team=None):
	"""Leave the current (or given) team. The last Owner cannot leave."""
	user = _portal_user()
	team = team or get_current_team(user)
	role = get_team_role(team, user)
	if not role:
		raise frappe.PermissionError(_("You are not a member of team {0}").format(team))
	doc = _team_doc(team)
	members = [{"user": m.user, "role": m.role} for m in doc.members]
	try:
		# leaving yourself is allowed for every role; only the last-Owner rule applies
		check_member_change(members, user, None, OWNER)
	except ValueError as exc:
		frappe.throw(_(str(exc)))
	doc.members = [m for m in doc.members if m.user != user]
	for i, m in enumerate(doc.members, start=1):
		m.idx = i
	doc.save(ignore_permissions=True)
	clear_cache()
	set_team_cookie(None)
	return {"left": team, "current": get_current_team(user)}
