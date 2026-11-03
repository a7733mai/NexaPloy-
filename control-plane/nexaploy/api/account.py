"""Account API for the customer portal (/dashboard/account): the session user's profile,
language, two-factor status and personal API key.

API keys are Frappe's own (User.api_key / User.api_secret, `Authorization: token
<key>:<secret>`). A key acts as its user with exactly the user's permissions, so every team
check still applies; add `X-Nexaploy-Team: <team>` to pick the team. The secret is shown
once (stored encrypted by Frappe). Key changes are logged to the current team's activity.
"""

import frappe
from frappe import _
from frappe.utils import get_url

from nexaploy.activity import log_activity
from nexaploy.portal_utils import has_portal_access
from nexaploy.team_utils import TEAM_HEADER
from nexaploy.teams import get_current_team, get_user_teams, two_factor_status

LANGUAGES = (("en", "English"), ("ar", "العربية"))


def _user():
	user = frappe.session.user
	if not user or user == "Guest":
		raise frappe.PermissionError(_("Please log in to use the dashboard"))
	if not has_portal_access(frappe.get_roles(user), user):
		raise frappe.PermissionError(_("You need the Nexaploy Customer role to use the dashboard"))
	return user


def _current_team(user):
	try:
		return get_current_team(user)
	except Exception:
		return None


def _log(user, action, details=None):
	"""Log an account action to every team the user belongs to (admins see who holds keys)."""
	teams = [t["name"] for t in get_user_teams(user)] or [_current_team(user)]
	for team in teams:
		if team:
			log_activity(team, action, "User", user, details)


@frappe.whitelist()
def get_account():
	"""{user, email, first_name, last_name, full_name, language, languages, two_factor,
	api_key, has_api_secret, api_base_url, team, team_header}"""
	user = _user()
	row = frappe.db.get_value(
		"User", user, ["name", "email", "first_name", "last_name", "full_name", "language", "api_key"], as_dict=True
	)
	return {
		"user": user,
		"email": row.email,
		"first_name": row.first_name,
		"last_name": row.last_name,
		"full_name": row.full_name,
		"language": row.language or "en",
		"languages": [{"code": c, "label": label} for c, label in LANGUAGES],
		"two_factor": two_factor_status(user),
		"api_key": row.api_key,
		"has_api_secret": bool(row.api_key) and _has_secret(user),
		"api_base_url": get_url(),
		"team": _current_team(user),
		"team_header": TEAM_HEADER,
	}


def _has_secret(user):
	try:
		from frappe.utils.password import get_decrypted_password

		return bool(get_decrypted_password("User", user, "api_secret", raise_exception=False))
	except Exception:
		return False


@frappe.whitelist(methods=["POST"])
def update_profile(first_name=None, last_name=None, language=None):
	"""Change the session user's name and/or portal language (en/ar; ar renders RTL)."""
	user = _user()
	doc = frappe.get_doc("User", user)
	if first_name is not None:
		first_name = str(first_name).strip()
		if not first_name:
			frappe.throw(_("First name is required"))
		doc.first_name = first_name[:140]
	if last_name is not None:
		doc.last_name = str(last_name).strip()[:140] or None
	if language is not None:
		if language not in {c for c, _label in LANGUAGES}:
			frappe.throw(_("Unsupported language {0}").format(language))
		doc.language = language
	doc.save(ignore_permissions=True)
	frappe.local.lang = doc.language or frappe.local.lang
	return {"full_name": doc.full_name, "language": doc.language}


@frappe.whitelist(methods=["POST"])
def generate_api_key(rotate=False):
	"""Create the user's API key/secret, or a new pair when `rotate` (the old pair stops
	working at once). Returns {api_key, api_secret}; the secret is never shown again."""
	user = _user()
	rotate = str(rotate).strip().lower() in ("1", "true", "yes")
	doc = frappe.get_doc("User", user)
	existed = bool(doc.api_key)
	if rotate or not doc.api_key:
		doc.api_key = frappe.generate_hash(length=15)
	secret = frappe.generate_hash(length=15)
	doc.api_secret = secret
	doc.save(ignore_permissions=True)
	_log(user, "api_key.rotate" if existed else "api_key.create", {"key_id": doc.api_key[:4] + "…"})
	return {"api_key": doc.api_key, "api_secret": secret}


@frappe.whitelist(methods=["POST"])
def revoke_api_key():
	"""Delete the user's API key and secret (requests using them fail immediately)."""
	user = _user()
	key = frappe.db.get_value("User", user, "api_key")
	if not key:
		return {"revoked": False}
	frappe.db.set_value("User", user, "api_key", None)
	try:
		from frappe.utils.password import remove_encrypted_password

		remove_encrypted_password("User", user, "api_secret")
	except Exception:
		frappe.db.delete("__Auth", {"doctype": "User", "name": user, "fieldname": "api_secret"})
	frappe.clear_cache(user=user)
	_log(user, "api_key.revoke", {"key_id": key[:4] + "…"})
	return {"revoked": True}
