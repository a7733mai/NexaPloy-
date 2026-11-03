"""Teams (ARCHITECTURE.md §9): pure helpers in nexaploy.team_utils + static API checks."""

import ast
import os
import re
from datetime import datetime, timedelta

import pytest

from nexaploy import team_utils as tu

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "nexaploy")


def read(*parts):
	with open(os.path.join(APP, *parts), encoding="utf-8") as fh:
		return fh.read()


def test_team_utils_has_no_frappe_import():
	tree = ast.parse(read("team_utils.py"))
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			assert all(not a.name.startswith("frappe") for a in node.names)
		if isinstance(node, ast.ImportFrom):
			assert not (node.module or "").startswith("frappe")


# --------------------------------------------------------------------------- role matrix

MATRIX = {
	# role: (read, write, billing, admin)
	"Owner": (True, True, True, True),
	"Admin": (True, True, True, True),
	"Developer": (True, True, False, False),
	"Billing": (True, False, True, False),
}


@pytest.mark.parametrize("role", sorted(MATRIX))
@pytest.mark.parametrize("idx,action", list(enumerate(tu.ACTIONS)))
def test_role_matrix(role, idx, action):
	assert tu.role_allows(role, action) is MATRIX[role][idx]


@pytest.mark.parametrize("role", [None, "", "Guest", "owner", "System Manager"])
def test_role_allows_unknown_roles(role):
	assert not any(tu.role_allows(role, a) for a in tu.ACTIONS)


def test_role_allows_unknown_action():
	with pytest.raises(ValueError):
		tu.role_allows("Owner", "delete")


def test_normalize_role():
	assert tu.normalize_role(" admin ") == "Admin"
	assert tu.normalize_role("BILLING") == "Billing"
	for bad in ("", None, "root", "System Manager"):
		with pytest.raises(ValueError):
			tu.normalize_role(bad)


@pytest.mark.parametrize(
	"doctype,ptype,action",
	[
		("Site", "read", "read"),
		("Site", "select", "read"),
		("Site", "print", "read"),
		("Site", "export", "read"),
		("Site", "write", "write"),
		("Site", "create", "write"),
		("Site", "delete", "write"),
		("Site", "submit", "write"),
		("Site", "cancel", "write"),
		("Bench", "write", "write"),
		("Site Backup", "create", "write"),
		("Release Group", "write", "write"),
		("Nexaploy Invoice", "read", "read"),
		("Nexaploy Invoice", "write", "billing"),
		("Nexaploy Payment", "create", "billing"),
		("Nexaploy Subscription", "delete", "billing"),
		("Server", "read", "read"),
		("Server", "write", "admin"),
		("Server Registration Token", "create", "admin"),
		("Team", "write", "admin"),
		("Team", "read", "read"),
		("Team Invitation", "read", "read"),
		("Team Invitation", "delete", "admin"),
		("Site", None, "read"),
	],
)
def test_required_action(doctype, ptype, action):
	assert tu.required_action(doctype, ptype) == action
	assert tu.ptype_action(ptype, doctype) == action


# --------------------------------------------------------------------------- member changes


def members(*pairs):
	return [{"user": u, "role": r} for u, r in pairs]


def test_cannot_remove_or_demote_last_owner():
	team = members(("o@x", "Owner"), ("a@x", "Admin"))
	with pytest.raises(ValueError, match="at least one Owner"):
		tu.check_member_change(team, "o@x", None, "Owner")
	with pytest.raises(ValueError, match="at least one Owner"):
		tu.check_member_change(team, "o@x", "Admin", "Owner")
	tu.check_member_change(team, "o@x", "Owner", "Owner")  # no-op is fine


def test_second_owner_can_be_demoted():
	team = members(("o@x", "Owner"), ("p@x", "Owner"))
	tu.check_member_change(team, "o@x", "Developer", "Owner")
	tu.check_member_change(team, "o@x", None, "Owner")


def test_only_owner_touches_owner_role():
	team = members(("o@x", "Owner"), ("p@x", "Owner"), ("a@x", "Admin"), ("d@x", "Developer"))
	with pytest.raises(ValueError, match="Only an Owner"):
		tu.check_member_change(team, "p@x", None, "Admin")
	with pytest.raises(ValueError, match="Only an Owner"):
		tu.check_member_change(team, "d@x", "Owner", "Admin")
	tu.check_member_change(team, "d@x", "Billing", "Admin")
	tu.check_member_change(team, "a@x", None, "Admin")
	tu.check_member_change(team, "d@x", "Owner", "Owner")


def test_member_change_errors():
	team = members(("o@x", "Owner"))
	with pytest.raises(ValueError, match="not a member"):
		tu.check_member_change(team, "z@x", None, "Owner")
	with pytest.raises(ValueError):
		tu.check_member_change(team + members(("d@x", "Developer")), "d@x", "Root", "Owner")
	assert tu.count_owners(members(("a", "Owner"), ("b", "Owner"), ("c", "Admin"))) == 2


def test_can_invite_role():
	assert tu.can_invite_role("Owner", "Owner")
	assert tu.can_invite_role("Owner", "Billing")
	assert tu.can_invite_role("Admin", "Developer")
	assert tu.can_invite_role("Admin", "Admin")
	assert not tu.can_invite_role("Admin", "Owner")
	assert not tu.can_invite_role("Developer", "Developer")
	assert not tu.can_invite_role("Billing", "Billing")
	assert not tu.can_invite_role(None, "Developer")


# --------------------------------------------------------------------------- slugs & titles


@pytest.mark.parametrize(
	"title,slug",
	[
		("Acme Corp. (EU)", "acme-corp-eu"),
		("  My   Team  ", "my-team"),
		("ahmed@example.com's team", "ahmed-at-example-com-s-team"),
		("Café Österreich", "cafe-osterreich"),
		("---x---y---", "x-y"),
	],
)
def test_slugify(title, slug):
	assert tu.slugify(title) == slug
	assert tu.is_valid_slug(slug)


def test_slugify_limits():
	assert len(tu.slugify("a" * 200)) == tu.SLUG_MAX
	assert not tu.slugify("ab-" * 30).endswith("-")
	assert tu.slugify("a" * 39 + "-b") == "a" * 39
	assert tu.slugify("شركة") == ""


@pytest.mark.parametrize("title", ["", None, "شركة", "x", "Admin", "teams", "ab"])
def test_team_slug_always_valid(title):
	slug = tu.team_slug(title)
	assert tu.is_valid_slug(slug), slug
	assert len(slug) >= 3
	assert slug not in tu.RESERVED_SLUGS


def test_team_slug_examples():
	assert tu.team_slug("Acme") == "acme"
	assert tu.team_slug("") == "my-team"
	assert tu.team_slug("ab") == "ab-team"
	assert tu.team_slug("Admin") == "admin-team"


def test_unique_slug():
	taken = {"acme", "acme-2", "acme-3"}
	assert tu.unique_slug("acme", taken.__contains__) == "acme-4"
	assert tu.unique_slug("free", taken.__contains__) == "free"
	everything = lambda name: not re.match(r"^acme-[0-9a-f]{6}$", name)  # noqa: E731
	assert re.match(r"^acme-[0-9a-f]{6}$", tu.unique_slug("acme", everything, max_tries=3))
	long_base = "x" * tu.SLUG_MAX
	assert tu.is_valid_slug(tu.unique_slug(long_base, lambda n: n == long_base[: tu.SLUG_MAX - 7]))


def test_is_valid_slug():
	assert tu.is_valid_slug("acme-1")
	for bad in ("", None, "-acme", "acme-", "Acme", "a b", "a_b", "x" * 41):
		assert not tu.is_valid_slug(bad)


def test_personal_team_title_and_normalize_title():
	assert tu.personal_team_title("a@b.com") == "a@b.com's team"
	assert tu.personal_team_title("a@b.com", "Ahmed Ali") == "Ahmed Ali's team"
	assert tu.personal_team_title("a@b.com", "  ") == "a@b.com's team"
	assert tu.normalize_title("  Acme \n Corp ") == "Acme Corp"
	with pytest.raises(ValueError):
		tu.normalize_title("   ")
	with pytest.raises(ValueError):
		tu.normalize_title("x" * 141)


# --------------------------------------------------------------------------- invitations


def test_invitation_token_hashing():
	token = tu.generate_invitation_token()
	assert len(token) >= 40 and re.match(r"^[A-Za-z0-9_-]+$", token)
	assert token != tu.generate_invitation_token()
	digest = tu.hash_invitation_token(token)
	assert re.match(r"^[0-9a-f]{64}$", digest) and token not in digest
	assert tu.hash_invitation_token(token) == digest
	assert tu.verify_invitation_token(token, digest)
	assert not tu.verify_invitation_token(token + "x", digest)
	assert not tu.verify_invitation_token(None, digest)
	assert not tu.verify_invitation_token(token, None)
	# known vector: sha256("abc")
	assert tu.hash_invitation_token("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_invitation_expiry():
	now = datetime(2026, 11, 3, 12, 0)
	exp = tu.invitation_expiry(now)
	assert exp - now == timedelta(days=7)
	assert not tu.invitation_expired(exp, now)
	assert tu.invitation_expired(exp, exp)
	assert tu.invitation_expired(None, now)
	assert tu.invitation_url("https://cp.example.com/", "TOK") == "https://cp.example.com/dashboard/team?invite=TOK"


def test_normalize_email():
	assert tu.normalize_email("  Ahmed@Example.COM ") == "ahmed@example.com"
	for bad in ("", None, "ahmed", "a@b", "a b@c.d", "@x.y"):
		with pytest.raises(ValueError):
			tu.normalize_email(bad)


# --------------------------------------------------------------------------- selection & recipients


def test_pick_team():
	assert tu.pick_team(["b", "a"], ["a", "b"]) == "b"
	assert tu.pick_team([None, "a"], ["a", "b"]) == "a"
	assert tu.pick_team(["zzz"], ["a", "b"]) == "a"  # not a member: fall back to the first team
	assert tu.pick_team([], ["a"]) == "a"
	assert tu.pick_team(["a"], []) is None


def test_notification_emails():
	team = [
		{"email": "o@x.com", "role": "Owner"},
		{"email": "a@x.com", "role": "Admin"},
		{"email": "d@x.com", "role": "Developer"},
		{"email": "b@x.com", "role": "Billing"},
		{"email": None, "role": "Owner"},
		{"email": "O@X.com", "role": "Developer"},
	]
	assert tu.notification_emails(team, "ops") == ["o@x.com", "a@x.com", "d@x.com"]
	assert tu.notification_emails(team, "billing") == ["o@x.com", "b@x.com"]
	assert tu.notification_emails(team, "billing", "acct@x.com") == ["acct@x.com", "o@x.com", "b@x.com"]
	assert tu.notification_emails(team, "billing", "B@x.com") == ["B@x.com", "o@x.com"]
	assert tu.notification_emails([], "ops") == []
	with pytest.raises(ValueError):
		tu.notification_emails(team, "spam")


def test_header_and_cookie_names():
	assert tu.TEAM_HEADER == "X-Nexaploy-Team"
	assert tu.TEAM_COOKIE == "nexaploy_team"
	js = read("public", "js", "dashboard.js")
	assert '"X-Nexaploy-Team"' in js and '"nexaploy_team"' in js


# --------------------------------------------------------------------------- static API checks


def _functions(path):
	return {n.name: n for n in ast.parse(read(*path.split("/"))).body if isinstance(n, ast.FunctionDef)}


def _whitelisted(path):
	result = {}
	for name, fn in _functions(path).items():
		for dec in fn.decorator_list:
			target = dec.func if isinstance(dec, ast.Call) else dec
			if isinstance(target, ast.Attribute) and target.attr == "whitelist":
				result[name] = fn
	return result


def test_teams_module_contract():
	"""nexaploy/teams.py exposes the §9 API with the documented signatures."""
	fns = _functions("teams.py")
	expected = {
		"is_platform_admin": ["user"],
		"get_user_teams": ["user"],
		"get_current_team": ["user"],
		"check_team_access": ["team", "action", "user"],
		"get_team_role": ["team", "user"],
		"team_notification_emails": ["team", "kind"],
	}
	for name, args in expected.items():
		assert name in fns, name
		assert [a.arg for a in fns[name].args.args] == args, name
	check = fns["check_team_access"]
	assert [d.value for d in check.args.defaults] == ["read", None]


def test_teams_api_methods():
	wl = _whitelisted("api/teams.py")
	expected = {
		"my_teams": [],
		"switch_team": ["team"],
		"create_team": ["title"],
		"get_team": ["team"],
		"invite_member": ["email", "role"],
		"accept_invitation": ["token"],
		"remove_member": ["user"],
		"change_role": ["user", "role"],
		"leave_team": ["team"],
		"revoke_invitation": ["name"],
	}
	for name, args in expected.items():
		assert name in wl, name
		assert [a.arg for a in wl[name].args.args] == args, name
	for fn in wl.values():
		assert not any(
			isinstance(d, ast.Call) and any(k.arg == "allow_guest" for k in d.keywords) for d in fn.decorator_list
		), fn.name


def test_js_calls_known_teams_methods():
	js = read("public", "js", "dashboard.js")
	used = set(re.findall(r'TEAMS_API \+ "(\w+)"', js))
	assert used and used <= set(_whitelisted("api/teams.py")), used - set(_whitelisted("api/teams.py"))


def test_permissions_hook_targets_kept():
	fns = _functions("permissions.py")
	for name in ("team_query", "server_query", "deploy_candidate_query", "agent_job_query", "has_team_permission"):
		assert name in fns, name
	src = read("permissions.py")
	assert "`tabTeam Member`" in src and "parenttype" in src


def test_patch_registered():
	assert "nexaploy.patches.v0_2.user_team_to_team" in read("patches.txt")
	assert "def execute(" in read("patches", "v0_2", "user_team_to_team.py")
