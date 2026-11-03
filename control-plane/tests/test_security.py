"""Activity log, prepaid credits, two-factor role sync and account API (pure parts + static checks)."""

import ast
import csv
import json
import os
import re
from decimal import Decimal

import pytest

from nexaploy import team_utils as tu
from nexaploy.billing import credits_utils as cu

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "nexaploy")
D = Decimal


def read(*parts):
	with open(os.path.join(APP, *parts), encoding="utf-8") as fh:
		return fh.read()


def functions(*parts):
	tree = ast.parse(read(*parts))
	return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}


def whitelisted(*parts):
	result = {}
	for name, fn in functions(*parts).items():
		for dec in fn.decorator_list:
			target = dec.func if isinstance(dec, ast.Call) else dec
			if isinstance(target, ast.Attribute) and target.attr == "whitelist":
				result[name] = fn
	return result


# --------------------------------------------------------------------------- credits: pure math


def test_credits_utils_has_no_frappe_import():
	assert not re.search(r"^\s*(import|from)\s+frappe", read("billing", "credits_utils.py"), re.M)


@pytest.mark.parametrize(
	"tx_type, amount, expected",
	[
		("TopUp", "10", D("10.00")),
		("Promo", 5.005, D("5.01")),
		("Refund", "1", D("1.00")),
		("InvoicePayment", "-3.333", D("-3.33")),
		("Adjustment", "-2", D("-2.00")),
		("Adjustment", "2", D("2.00")),
	],
)
def test_check_transaction(tx_type, amount, expected):
	assert cu.check_transaction(tx_type, amount) == expected


@pytest.mark.parametrize(
	"tx_type, amount",
	[("TopUp", "-1"), ("Promo", "-5"), ("Refund", "-1"), ("InvoicePayment", "1"), ("Adjustment", "0"), ("Bogus", "1"), ("TopUp", "0.001")],
)
def test_check_transaction_rejects(tx_type, amount):
	with pytest.raises(cu.CreditError):
		cu.check_transaction(tx_type, amount)


def test_ledger_balance_and_next_balance():
	rows = [
		{"amount": 10, "currency": "USD"},
		{"amount": -3.25, "currency": "usd"},
		{"amount": 100, "currency": "EGP"},
		{"amount": "0.10", "currency": "USD"},
	]
	assert cu.ledger_balance(rows, "USD") == D("6.85")
	assert cu.ledger_balance(rows, "EGP") == D("100.00")
	assert cu.ledger_balance(rows) == D("106.85")
	assert cu.ledger_balance([]) == D("0.00")
	assert cu.next_balance("6.85", "-6.85") == D("0.00")
	assert cu.next_balance(0, "5") == D("5.00")
	with pytest.raises(cu.CreditError):
		cu.next_balance("6.85", "-6.86")


def test_credits_to_apply_and_amount_due():
	assert cu.credits_to_apply(50, 20) == D("20.00")
	assert cu.credits_to_apply(5, 20) == D("5.00")
	assert cu.credits_to_apply(0, 20) == D("0.00")
	assert cu.credits_to_apply(-1, 20) == D("0.00")
	assert cu.credits_to_apply(10, 0) == D("0.00")
	assert cu.amount_due(20, 5) == D("15.00")
	assert cu.amount_due(20, 25) == D("0.00")


def test_apply_to_invoice_full():
	plan = cu.apply_to_invoice(total="12.50", credits_applied=0, balance="30")
	assert plan == {
		"apply": D("12.50"),
		"credits_applied": D("12.50"),
		"amount_due": D("0.00"),
		"paid": True,
		"balance_after": D("17.50"),
	}


def test_apply_to_invoice_partial_then_rest():
	plan = cu.apply_to_invoice(total="40", credits_applied=0, balance="15")
	assert plan["apply"] == D("15.00") and plan["amount_due"] == D("25.00") and not plan["paid"]
	assert plan["balance_after"] == D("0.00")
	# a later top-up of 30 pays the remaining 25 of the same invoice
	again = cu.apply_to_invoice(total="40", credits_applied=plan["credits_applied"], balance="30")
	assert again["apply"] == D("25.00") and again["credits_applied"] == D("40.00")
	assert again["paid"] and again["balance_after"] == D("5.00")


def test_apply_to_invoice_nothing_to_do():
	assert cu.apply_to_invoice(10, 0, 0)["apply"] == D("0.00")
	assert not cu.apply_to_invoice(10, 0, 0)["paid"]
	done = cu.apply_to_invoice(10, 10, 50)
	assert done["apply"] == D("0.00") and done["paid"] and done["balance_after"] == D("50.00")


def test_allocate_credits_oldest_first():
	invoices = [
		{"name": "INV-3", "amount_due": "30", "due_date": "2026-10-10", "status": "Unpaid"},
		{"name": "INV-1", "amount_due": "10", "due_date": "2026-08-10", "status": "Overdue"},
		{"name": "INV-2", "amount_due": "15", "due_date": "2026-09-10", "status": "Unpaid"},
		{"name": "INV-0", "amount_due": "99", "due_date": "2026-01-01", "status": "Paid"},
	]
	assert cu.allocate_credits(30, invoices) == [("INV-1", D("10.00")), ("INV-2", D("15.00")), ("INV-3", D("5.00"))]
	assert cu.allocate_credits(0, invoices) == []
	assert cu.allocate_credits(500, invoices)[-1] == ("INV-3", D("30.00"))


def test_ledger_replay_matches_running_balance():
	"""balance_after of each row = previous balance + amount (what add_credits stores)."""
	ops = [("Promo", 10), ("TopUp", 50), ("InvoicePayment", -35.5), ("Refund", 5), ("Adjustment", -29.5)]
	balance, after = D("0"), []
	for tx_type, amount in ops:
		balance = cu.next_balance(balance, cu.check_transaction(tx_type, amount))
		after.append(balance)
	assert after == [D("10.00"), D("60.00"), D("24.50"), D("29.50"), D("0.00")]
	assert cu.ledger_balance([{"amount": a} for _, a in ops]) == after[-1]


@pytest.mark.parametrize("amount, expected", [("5", D("5.00")), (10000, D("10000.00")), ("25.555", D("25.56"))])
def test_validate_topup_amount(amount, expected):
	assert cu.validate_topup_amount(amount) == expected


@pytest.mark.parametrize("amount", ["4.99", "10000.01", "abc", "-5", None])
def test_validate_topup_amount_rejects(amount):
	with pytest.raises(cu.CreditError):
		cu.validate_topup_amount(amount)


def test_validate_topup_custom_limits():
	assert cu.validate_topup_amount("1", minimum="1", maximum="2") == D("1.00")
	with pytest.raises(cu.CreditError):
		cu.validate_topup_amount("3", minimum="1", maximum="2")


def test_signup_credits_granted_once():
	assert cu.signup_credit_amount("10", False, False) == D("10.00")
	assert cu.signup_credit_amount("10", True, False) == D("0.00")  # this team already got them
	assert cu.signup_credit_amount("10", False, True) == D("0.00")  # owner's other team got them
	assert cu.signup_credit_amount(0, False, False) == D("0.00")
	assert cu.signup_credit_amount(None, False, False) == D("0.00")


def test_signup_credits_simulated_once_per_team():
	"""Re-running the grant for the same team (or a second team of the same owner) is a no-op."""
	ledger = []

	def grant(team, owner):
		team_granted = any(r["team"] == team for r in ledger)
		owner_granted = any(r["owner"] == owner and r["team"] != team for r in ledger)
		amount = cu.signup_credit_amount("10", team_granted, owner_granted)
		if amount:
			ledger.append({"team": team, "owner": owner, "amount": amount})

	grant("acme", "a@x.io")
	grant("acme", "a@x.io")
	grant("acme-2", "a@x.io")
	grant("beta", "b@x.io")
	assert [(r["team"], r["amount"]) for r in ledger] == [("acme", D("10.00")), ("beta", D("10.00"))]


# --------------------------------------------------------------------------- activity: scrubbing


def test_scrub_details_removes_secrets():
	details = {
		"site": "a.example.com",
		"admin_password": "hunter2",
		"Password": "x",
		"api_secret": "s",
		"api-key": "k",
		"token": "t",
		"registration_token": "t",
		"Authorization": "token a:b",
		"hmac": "h",
		"key": "maintenance_mode",
		"value": 1,
		"nested": {"db_password": "p", "keep": True, "list": [{"secret_key": "s", "name": "n"}, "Bearer abc"]},
		"header": "token abc:def",
		"ref": "__secret__:Site:x:admin_password",
	}
	clean = tu.scrub_details(details)
	assert clean == {
		"site": "a.example.com",
		"key": "maintenance_mode",
		"value": 1,
		"nested": {"keep": True, "list": [{"name": "n"}]},
	}
	text = json.dumps(clean)
	for secret in ("hunter2", "token a:b", "abc:def", "__secret__"):
		assert secret not in text
	assert details["admin_password"] == "hunter2"  # input untouched


def test_scrub_details_limits():
	long = "x" * 5000
	clean = tu.scrub_details({"query": long, "items": list(range(500)), "obj": object()})
	assert len(clean["query"]) == tu.ACTIVITY_MAX_STRING + 1
	assert len(clean["items"]) == tu.ACTIVITY_MAX_ITEMS
	assert isinstance(clean["obj"], str)
	deep = {}
	cur = deep
	for _ in range(20):
		cur["a"] = {}
		cur = cur["a"]
	json.dumps(tu.scrub_details(deep))
	assert tu.scrub_details(None) is None
	assert tu.scrub_details(("a", "b")) == ["a", "b"]


@pytest.mark.parametrize("key", ["password", "new_password", "api_secret", "API_KEY", "x-frappe-csrf-token", "sid", "otp", "private_key", "aws_access_key"])
def test_is_secret_key(key):
	assert tu.is_secret_key(key)


@pytest.mark.parametrize("key", ["key", "site", "domain", "monkey", "keys", "backup", "action", "amount"])
def test_is_not_secret_key(key):
	assert not tu.is_secret_key(key)


@pytest.mark.parametrize(
	"action, ok",
	[
		("site.create", True), ("api_key.create", True), ("credits.topup", True), ("team.two_factor", True),
		("site", False), ("Site.Create", False), ("site.", False), ("", False), (None, False), ("site create", False),
	],
)
def test_activity_action_format(action, ok):
	assert tu.is_valid_activity_action(action) is ok


# --------------------------------------------------------------------------- two-factor role sync decisions


def test_users_requiring_two_factor():
	memberships = [
		{"user": "a@x.io", "two_factor_required": 1, "enabled": 1},
		{"user": "a@x.io", "two_factor_required": 0, "enabled": 1},
		{"user": "b@x.io", "two_factor_required": 0, "enabled": 1},
		{"user": "c@x.io", "two_factor_required": 1, "enabled": 0},  # disabled team does not count
		{"user": "Administrator", "two_factor_required": 1, "enabled": 1},
	]
	assert tu.users_requiring_two_factor(memberships) == {"a@x.io"}


def test_two_factor_role_changes():
	users = ["a@x.io", "b@x.io", "c@x.io", "d@x.io", "Administrator"]
	required = {"a@x.io", "b@x.io", "Administrator"}
	holders = {"b@x.io", "c@x.io", "z@x.io"}  # z is not among the users looked at: untouched
	add, remove = tu.two_factor_role_changes(users, required, holders)
	assert add == ["a@x.io"]
	assert remove == ["c@x.io"]


def test_two_factor_sync_scenarios():
	def run(memberships, holders):
		users = {m["user"] for m in memberships}
		return tu.two_factor_role_changes(users, tu.users_requiring_two_factor(memberships), holders)

	team_on = lambda u: {"user": u, "two_factor_required": 1, "enabled": 1}  # noqa: E731
	team_off = lambda u: {"user": u, "two_factor_required": 0, "enabled": 1}  # noqa: E731
	# owner enables 2FA: every member gets the role
	assert run([team_on("o@x.io"), team_on("d@x.io")], set()) == (["d@x.io", "o@x.io"], [])
	# new member accepts an invitation into a 2FA team
	assert run([team_on("o@x.io"), team_on("n@x.io")], {"o@x.io"}) == (["n@x.io"], [])
	# disabled again: role removed
	assert run([team_off("o@x.io")], {"o@x.io"}) == ([], ["o@x.io"])
	# still required by another team: kept
	assert run([team_off("o@x.io"), team_on("o@x.io")], {"o@x.io"}) == ([], [])
	# removed member (no memberships left, but looked at): role removed
	assert tu.two_factor_role_changes(["gone@x.io"], set(), {"gone@x.io"}) == ([], ["gone@x.io"])


# --------------------------------------------------------------------------- static contracts


def test_log_activity_signature_and_never_raises():
	fns = functions("activity.py")
	fn = fns["log_activity"]
	assert [a.arg for a in fn.args.args] == ["team", "action", "reference_doctype", "reference_name", "details"]
	assert [getattr(d, "value", "x") for d in fn.args.defaults] == [None, None, None]
	# the whole body is one try/except Exception
	body = [n for n in fn.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
	assert len(body) == 1 and isinstance(body[0], ast.Try)
	assert any(isinstance(h.type, ast.Name) and h.type.id == "Exception" for h in body[0].handlers)
	src = read("activity.py")
	assert "scrub_details" in src and "request_ip" in src


def test_billing_credit_api():
	fns = functions("billing", "credits.py")
	for name, args in {
		"get_balance": ["team", "currency"],
		"add_credits": ["team", "amount", "type", "ref", "description", "currency"],
		"apply_credits_to_invoice": ["invoice"],
		"grant_signup_credits": ["team"],
		"credit_topup": ["invoice"],
	}.items():
		assert [a.arg for a in fns[name].args.args] == args, name
	wl = whitelisted("api", "billing.py")
	assert [a.arg for a in wl["topup"].args.args] == ["amount", "gateway"]
	assert "get_credits" in wl
	assert "grant_signup_credits" in read("teams.py")
	assert "_apply_credits(invoice)" in read("billing", "invoicing.py")


def test_teams_and_account_api():
	wl = whitelisted("api", "teams.py")
	assert [a.arg for a in wl["set_two_factor"].args.args] == ["required"]
	assert "get_activity" in wl
	acc = whitelisted("api", "account.py")
	assert {"get_account", "update_profile", "generate_api_key", "revoke_api_key"} <= set(acc)
	for fn in list(acc.values()) + [wl["set_two_factor"], wl["get_activity"]]:
		assert not any(
			isinstance(d, ast.Call) and any(k.arg == "allow_guest" for k in d.keywords) for d in fn.decorator_list
		), fn.name
	src = read("api", "teams.py")
	for action in ("team.invite", "team.change_role", "team.remove_member", "team.two_factor"):
		assert f'"{action}"' in src, action
	assert '"api_key.create"' in read("api", "account.py") and '"api_key.revoke"' in read("api", "account.py")
	assert '"credits.topup"' in read("api", "billing.py")
	dash = read("api", "dashboard.py")
	assert '"site.create"' in dash and 'f"site.{action}"' in dash and '"server.registration_token"' in dash


def _doctype(folder):
	with open(os.path.join(APP, "nexaploy", "doctype", folder, folder + ".json")) as fh:
		return json.load(fh)


def test_new_doctypes_spec():
	activity = {f["fieldname"]: f for f in _doctype("team_activity")["fields"]}
	for name in ("team", "user", "action", "reference_doctype", "reference_name", "details", "ip_address"):
		assert name in activity, name
	assert activity["details"]["fieldtype"] == "JSON"
	ledger = {f["fieldname"]: f for f in _doctype("team_credit_transaction")["fields"]}
	for name in ("team", "amount", "currency", "type", "reference_doctype", "reference_name", "balance_after", "description"):
		assert name in ledger, name
	assert tuple(ledger["type"]["options"].split("\n")) == cu.TRANSACTION_TYPES
	for folder in ("team_activity", "team_credit_transaction"):
		perms = {p["role"]: p for p in _doctype(folder)["permissions"]}
		assert perms["Nexaploy Customer"]["read"] == 1
		assert not any(perms["Nexaploy Customer"].get(k) for k in ("write", "create", "delete"))
		assert not perms["System Manager"].get("delete") and not perms["System Manager"].get("write")
	team = {f["fieldname"]: f for f in _doctype("team")["fields"]}
	assert team["credit_balance"]["read_only"] == 1
	invoice = {f["fieldname"]: f for f in _doctype("nexaploy_invoice")["fields"]}
	assert {"credits_applied", "amount_due", "invoice_type"} <= set(invoice)
	assert tuple(invoice["invoice_type"]["options"].split("\n")) == cu.INVOICE_TYPES
	assert "Credits" in invoice["gateway"]["options"].split("\n")
	settings = {f["fieldname"] for f in _doctype("billing_settings")["fields"]}
	assert {"signup_credits", "min_topup", "max_topup"} <= settings


def test_pages_and_sidebar():
	for page in ("account", "activity"):
		assert os.path.isfile(os.path.join(APP, "www", "dashboard", page + ".html"))
		assert "prepare_page(" in read("www", "dashboard", page + ".py")
	base = read("templates", "dashboard_base.html")
	assert "/dashboard/activity" in base and "/dashboard/account" in base
	assert "/assets/nexaploy/js/security.js" in base
	js = read("public", "js", "security.js")
	for comp in ("nxCredits", "nxActivity", "nxAccount", "nxTeamSecurity"):
		assert f'Alpine.data("{comp}"' in js


def test_security_translations_part():
	path = os.path.join(APP, "translations", "ar.csv")
	with open(path, encoding="utf-8", newline="") as fh:
		rows = [r for r in csv.reader(fh) if r]
	sources = {r[0] for r in rows}
	for s in ("Activity", "Account", "Credit balance", "Two-factor authentication", "API key"):
		assert s in sources, s
