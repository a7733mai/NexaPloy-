"""Pure team logic (ARCHITECTURE.md §9): role matrix, slugs, invitation tokens.

This module MUST NOT import frappe: it is unit-tested with plain pytest
(control-plane/tests/test_teams.py). nexaploy/teams.py does the database work.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import unicodedata
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Mapping

ROLES = ("Owner", "Admin", "Developer", "Billing")
ACTIONS = ("read", "write", "billing", "admin")
OWNER = "Owner"

# §9 role matrix: action -> roles allowed
ROLE_MATRIX: dict[str, frozenset[str]] = {
	"read": frozenset({"Owner", "Admin", "Developer", "Billing"}),
	"write": frozenset({"Owner", "Admin", "Developer"}),
	"billing": frozenset({"Owner", "Admin", "Billing"}),
	"admin": frozenset({"Owner", "Admin"}),
}

TEAM_HEADER = "X-Nexaploy-Team"
TEAM_COOKIE = "nexaploy_team"

INVITATION_TTL_DAYS = 7
INVITATION_STATUSES = ("Pending", "Accepted", "Expired", "Revoked")
INVITATION_TOKEN_BYTES = 32

SLUG_MAX = 40
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
RESERVED_SLUGS = frozenset({"admin", "administrator", "guest", "new", "nexaploy", "system", "teams"})

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Frappe permission types -> §9 actions
READ_PTYPES = frozenset({"read", "select", "print", "export", "report", "email", "share"})
WRITE_PTYPES = frozenset({"write", "create", "submit", "cancel", "delete", "amend", "import"})
BILLING_DOCTYPES = frozenset({"Nexaploy Invoice", "Nexaploy Payment", "Nexaploy Subscription"})
ADMIN_DOCTYPES = frozenset({"Server", "Server Registration Token", "Team", "Team Invitation"})


# --------------------------------------------------------------------------- roles


def normalize_role(role: Any) -> str:
	"""'admin ' -> 'Admin'. ValueError for unknown roles."""
	text = str(role or "").strip().title()
	if text not in ROLES:
		raise ValueError("Unknown team role")
	return text


def role_allows(role: str | None, action: str = "read") -> bool:
	"""True when `role` may perform `action` (read/write/billing/admin) per the §9 matrix."""
	if action not in ROLE_MATRIX:
		raise ValueError(f"Unknown team action {action!r}")
	return bool(role) and role in ROLE_MATRIX[action]


def ptype_action(ptype: str | None, doctype: str | None = None) -> str:
	"""Map a Frappe permission type on `doctype` to the §9 action it needs.

	Reads are open to every member. Mutations need `write`, except billing documents
	(`billing`) and servers, registration tokens, teams and invitations (`admin`).
	"""
	ptype = (ptype or "read").lower()
	if ptype in READ_PTYPES:
		return "read"
	if doctype in BILLING_DOCTYPES:
		return "billing"
	if doctype in ADMIN_DOCTYPES:
		return "admin"
	return "write"


def required_action(doctype: str | None, ptype: str | None) -> str:
	"""Alias of ptype_action(ptype, doctype) with the doctype first."""
	return ptype_action(ptype, doctype)


def count_owners(members: Iterable[Mapping[str, Any]]) -> int:
	return sum(1 for m in members if m.get("role") == OWNER)


def check_member_change(
	members: Iterable[Mapping[str, Any]], target_user: str, new_role: str | None, actor_role: str | None
) -> None:
	"""Validate changing `target_user` to `new_role` (None = remove from the team).

	Raises ValueError when:
	- the target is not a member;
	- the change would leave the team without an Owner;
	- a non-Owner grants, revokes or removes the Owner role.
	"""
	members = list(members)
	current = next((m for m in members if m.get("user") == target_user), None)
	if current is None:
		raise ValueError("User is not a member of this team")
	if new_role is not None:
		new_role = normalize_role(new_role)
	touches_owner = current.get("role") == OWNER or new_role == OWNER
	if touches_owner and actor_role != OWNER:
		raise ValueError("Only an Owner can change the Owner role")
	if current.get("role") == OWNER and new_role != OWNER and count_owners(members) <= 1:
		raise ValueError("A team must keep at least one Owner")


def can_invite_role(actor_role: str | None, role: str) -> bool:
	"""Admins invite Admin/Developer/Billing; only Owners invite Owners."""
	role = normalize_role(role)
	if not role_allows(actor_role, "admin"):
		return False
	return role != OWNER or actor_role == OWNER


# --------------------------------------------------------------------------- slugs & titles


def slugify(text: Any, max_len: int = SLUG_MAX) -> str:
	"""'Acme Corp. (EU)' -> 'acme-corp-eu'. Non-ASCII letters are transliterated when
	possible and dropped otherwise; may return '' when nothing usable is left."""
	value = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
	value = value.lower().replace("@", "-at-")
	value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
	value = re.sub(r"-{2,}", "-", value)
	return value[:max_len].strip("-")


def team_slug(title: Any, fallback: str = "my-team") -> str:
	"""Base team_name for a title: the slug, padded/defaulted so it is always valid
	(empty -> `fallback`; too short or reserved -> '<slug>-team')."""
	slug = slugify(title)
	if not slug:
		return fallback
	if len(slug) < 3 or slug in RESERVED_SLUGS:
		slug = f"{slug}-team"
	return slug[:SLUG_MAX].strip("-")


def unique_slug(base: str, exists: Callable[[str], bool], max_tries: int = 50) -> str:
	"""`base`, else `base-2`, `base-3`, ... (then a random suffix) until `exists` is False."""
	base = base[: SLUG_MAX - 7].strip("-") if len(base) > SLUG_MAX - 7 else base
	if not exists(base):
		return base
	for n in range(2, max_tries + 2):
		candidate = f"{base}-{n}"
		if not exists(candidate):
			return candidate
	while True:
		candidate = f"{base}-{secrets.token_hex(3)}"
		if not exists(candidate):
			return candidate


def is_valid_slug(value: Any) -> bool:
	return bool(value) and bool(SLUG_RE.match(str(value)))


def personal_team_title(user: str, full_name: str | None = None) -> str:
	""""<user>'s team" (the full name when known, else the user id / email)."""
	who = (full_name or "").strip() or str(user or "").strip() or "My"
	return f"{who}'s team"[:140]


def normalize_title(title: Any) -> str:
	text = re.sub(r"\s+", " ", str(title or "")).strip()
	if not text:
		raise ValueError("Team name is required")
	if len(text) > 140:
		raise ValueError("Team name must have at most 140 characters")
	return text


# --------------------------------------------------------------------------- invitations


def normalize_email(email: Any) -> str:
	text = str(email or "").strip().lower()
	if not EMAIL_RE.match(text) or len(text) > 254:
		raise ValueError("Enter a valid email address")
	return text


def generate_invitation_token(nbytes: int = INVITATION_TOKEN_BYTES) -> str:
	"""URL-safe random token (shown once, in the invitation link)."""
	return secrets.token_urlsafe(nbytes)


def hash_invitation_token(token: str) -> str:
	"""sha256 hex of the token; only the hash is stored (Team Invitation.token_hash)."""
	return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def verify_invitation_token(token: str | None, token_hash: str | None) -> bool:
	if not token or not token_hash:
		return False
	return hmac.compare_digest(hash_invitation_token(token), str(token_hash))


def invitation_expiry(now: datetime, days: int = INVITATION_TTL_DAYS) -> datetime:
	return now + timedelta(days=days)


def invitation_expired(expires_on: datetime | None, now: datetime) -> bool:
	return expires_on is None or expires_on <= now


def invitation_url(base_url: str, token: str) -> str:
	return f"{str(base_url or '').rstrip('/')}/dashboard/team?invite={token}"


# --------------------------------------------------------------------------- selection & recipients


def pick_team(requested: Iterable[Any], team_names: Iterable[str]) -> str | None:
	"""First of `requested` (header, cookie, ...) that is in `team_names`, else the first team."""
	names = [t for t in team_names if t]
	allowed = set(names)
	for value in requested:
		if value and str(value) in allowed:
			return str(value)
	return names[0] if names else None


def notification_emails(
	members: Iterable[Mapping[str, Any]], kind: str = "ops", billing_email: str | None = None
) -> list[str]:
	"""Recipients for team notifications.

	members: [{email, role}] of enabled users. `ops` (alerts, uptime): Owner, Admin,
	Developer. `billing` (invoices, payments): Team.billing_email, Owners and Billing members.
	Deduplicated case-insensitively, order kept.
	"""
	if kind not in ("ops", "billing"):
		raise ValueError(f"Unknown notification kind {kind!r}")
	roles = {"Owner", "Admin", "Developer"} if kind == "ops" else {"Owner", "Billing"}
	candidates = [billing_email] if kind == "billing" else []
	candidates += [m.get("email") for m in members if m.get("role") in roles]
	result, seen = [], set()
	for email in candidates:
		email = (email or "").strip()
		if not email or "@" not in email or email.lower() in seen:
			continue
		seen.add(email.lower())
		result.append(email)
	return result


# --------------------------------------------------------------------------- activity log (§10)

ACTIVITY_ACTION_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
# keys whose values never reach Team Activity.details (matched case-insensitively on the
# key with '-' -> '_'): anything password/secret/token/credential-like
SECRET_KEY_PARTS = (
	"password", "passwd", "secret", "token", "credential", "authorization", "cookie",
	"hmac", "signature", "private_key", "api_key", "apikey", "access_key", "session",
)
SECRET_KEY_EXACT = frozenset({"pwd", "sid", "otp", "pin", "auth", "csrf", "key_secret"})
SECRET_VALUE_RE = re.compile(
	r"^(?:token\s+\S+:\S+|bearer\s+\S+|basic\s+\S+|__secret__:.*|sk_(?:live|test)_\S+|-----BEGIN [A-Z ]*PRIVATE KEY-----.*)$",
	re.IGNORECASE | re.DOTALL,
)
ACTIVITY_MAX_STRING = 2000
ACTIVITY_MAX_ITEMS = 100
ACTIVITY_MAX_DEPTH = 6


def is_valid_activity_action(action: Any) -> bool:
	"""Dotted lowercase verb: 'site.create', 'credits.topup', 'api_key.create'."""
	return bool(action) and len(str(action)) <= 140 and bool(ACTIVITY_ACTION_RE.match(str(action)))


def is_secret_key(key: Any) -> bool:
	k = str(key or "").strip().lower().replace("-", "_")
	if not k:
		return False
	return k in SECRET_KEY_EXACT or any(part in k for part in SECRET_KEY_PARTS)


def looks_like_secret(value: Any) -> bool:
	"""String values that are credentials whatever their key ("token k:s", "Bearer x", ...)."""
	return isinstance(value, str) and bool(SECRET_VALUE_RE.match(value.strip()))


def scrub_details(details: Any, _depth: int = 0) -> Any:
	"""Copy of `details` safe to store in Team Activity: secret-looking keys and values are
	dropped, long strings truncated, collections capped. Non-JSON values become str()."""
	if _depth > ACTIVITY_MAX_DEPTH:
		return "…"
	if isinstance(details, Mapping):
		out = {}
		for key, value in list(details.items())[:ACTIVITY_MAX_ITEMS]:
			if is_secret_key(key) or looks_like_secret(value):
				continue
			out[str(key)] = scrub_details(value, _depth + 1)
		return out
	if isinstance(details, (list, tuple, set, frozenset)):
		items = list(details)[:ACTIVITY_MAX_ITEMS]
		return [scrub_details(v, _depth + 1) for v in items if not looks_like_secret(v)]
	if details is None or isinstance(details, (bool, int, float)):
		return details
	text = details if isinstance(details, str) else str(details)
	if len(text) > ACTIVITY_MAX_STRING:
		text = text[:ACTIVITY_MAX_STRING] + "…"
	return text


# --------------------------------------------------------------------------- two-factor role sync

TWO_FACTOR_ROLE = "Nexaploy 2FA"
TWO_FACTOR_EXEMPT_USERS = frozenset({"Administrator", "Guest"})


def users_requiring_two_factor(memberships: Iterable[Mapping[str, Any]]) -> set[str]:
	"""Users that must hold the 2FA role: members of at least one *enabled* team with
	`two_factor_required`. memberships: [{user, two_factor_required, enabled}]."""
	result = set()
	for m in memberships:
		user = m.get("user")
		if not user or user in TWO_FACTOR_EXEMPT_USERS:
			continue
		if m.get("enabled", 1) and m.get("two_factor_required"):
			result.add(user)
	return result


def two_factor_role_changes(
	users: Iterable[str], required: Iterable[str], holders: Iterable[str]
) -> tuple[list[str], list[str]]:
	"""(users to give the 2FA role, users to take it from), restricted to `users` (the ones
	whose memberships were looked at). `required` = users_requiring_two_factor(...),
	`holders` = users that currently hold the role."""
	users = {u for u in users if u and u not in TWO_FACTOR_EXEMPT_USERS}
	required, holders = set(required), set(holders)
	add = sorted(u for u in users if u in required and u not in holders)
	remove = sorted(u for u in users if u not in required and u in holders)
	return add, remove
