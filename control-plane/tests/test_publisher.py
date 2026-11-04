"""Marketplace publishers and payouts: pure logic (publisher_utils, billing/payouts_utils) +
static checks of the DocTypes, API, portal page and translations."""

import ast
import csv
import glob
import json
import os
import re
import shutil
import subprocess
from datetime import date
from decimal import Decimal

import jinja2
import pytest

from nexaploy import publisher_utils as pu
from nexaploy.billing import payouts_utils as pou

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "nexaploy")
DOCTYPES = os.path.join(APP, "nexaploy", "doctype")
PAGE = os.path.join(APP, "www", "dashboard", "publisher.html")
PAGE_PY = os.path.join(APP, "www", "dashboard", "publisher.py")
JS = os.path.join(APP, "public", "js", "publisher.js")
API = os.path.join(APP, "api", "publisher.py")


def read(path):
	with open(path, encoding="utf-8") as fh:
		return fh.read()


def doctype(name):
	folder = name.lower().replace(" ", "_")
	return json.loads(read(os.path.join(DOCTYPES, folder, folder + ".json")))


# --------------------------------------------------------------------------- payout math


def test_publisher_share_rounding():
	assert pou.publisher_share("10.00", 80) == Decimal("8.00")
	assert pou.publisher_share("0.01", 80) == Decimal("0.01")  # 0.008 -> 0.01 (half up)
	assert pou.publisher_share("0.05", 70) == Decimal("0.04")  # 0.035 -> 0.04
	assert pou.publisher_share("3.33", "33.3") == Decimal("1.11")  # 1.10889
	assert pou.publisher_share(5, 0) == Decimal("0.00")
	assert pou.publisher_share(5, 100) == Decimal("5.00")
	with pytest.raises(pou.PayoutError):
		pou.publisher_share(5, 101)
	with pytest.raises(pou.PayoutError):
		pou.publisher_share(5, -1)


def test_build_payout_totals_add_up():
	lines = [
		{"invoice": "INV-2", "invoice_item": "b", "app": "acme", "site": "b.example.com", "amount": "0.05"},
		{"invoice": "INV-1", "invoice_item": "a", "app": "acme", "site": "a.example.com", "amount": 9.99},
		{"invoice": "INV-1", "invoice_item": "c", "app": "acme", "site": "c.example.com", "amount": 0},  # skipped
		{"invoice": "INV-3", "invoice_item": "d", "app": "zeta", "site": "d.example.com", "amount": "3.33"},
	]
	out = pou.build_payout(lines, 70)
	assert [i["invoice_item"] for i in out["items"]] == ["a", "b", "d"]  # sorted by invoice, app, site
	shares = [i["share"] for i in out["items"]]
	assert shares == [Decimal("6.99"), Decimal("0.04"), Decimal("2.33")]  # 6.993, 0.035, 2.331
	assert out["gross"] == Decimal("13.37")
	assert out["net"] == Decimal("9.36")  # sum of rounded shares, not round(13.37 * 0.7) = 9.36
	assert out["platform_fee"] == Decimal("4.01")
	assert out["net"] + out["platform_fee"] == out["gross"]


def test_build_payout_per_line_rounding_differs_from_total():
	# three lines of 0.05 at 70 %: each share 0.035 -> 0.04, net 0.12 (total-based would be 0.11)
	out = pou.build_payout([{"invoice_item": str(i), "amount": "0.05"} for i in range(3)], 70)
	assert out["net"] == Decimal("0.12") and out["gross"] == Decimal("0.15") and out["platform_fee"] == Decimal("0.03")
	assert pou.build_payout([], 80) == {"items": [], "gross": Decimal("0.00"), "net": Decimal("0.00"), "platform_fee": Decimal("0.00")}


def test_group_lines():
	rows = [
		{"invoice": "I1", "invoice_item": "x1", "document_type": "Marketplace App", "app": "acme", "site": "s1", "amount": 10, "currency": "usd"},
		{"invoice": "I1", "invoice_item": "x2", "document_type": "Site", "app": "s1", "amount": 5, "currency": "USD"},
		{"invoice": "I2", "invoice_item": "x3", "document_type": "Marketplace App", "app": "erpnext", "amount": 10, "currency": "USD"},
		{"invoice": "I3", "invoice_item": "x4", "document_type": "Marketplace App", "app": "acme", "amount": 10, "currency": "EGP"},
		{"invoice": "I4", "invoice_item": "x5", "document_type": "Marketplace App", "app": "acme", "amount": 10, "currency": "USD"},
		{"invoice": "I5", "invoice_item": "x6", "document_type": "Marketplace App", "app": "beta", "amount": -1, "currency": "USD"},
	]
	groups = pou.group_lines(rows, {"acme": "pub-a", "beta": "pub-b", "erpnext": None}, already_paid={"x5"})
	assert set(groups) == {("pub-a", "USD"), ("pub-a", "EGP")}
	assert [l["invoice_item"] for l in groups[("pub-a", "USD")]] == ["x1"]
	assert groups[("pub-a", "EGP")][0]["app"] == "acme"


def test_payout_period():
	# invoice day 1 -> payouts on the 6th for the month before
	assert pou.payout_period(date(2026, 3, 6), 1) == (date(2026, 2, 1), date(2026, 2, 28))
	assert pou.payout_period(date(2026, 3, 5), 1) is None
	assert pou.payout_period(date(2026, 3, 7), 1) is None
	assert pou.is_payout_day("2026-01-06", 1)
	assert pou.payout_period("2026-01-06", 1) == (date(2025, 12, 1), date(2025, 12, 31))
	# invoice day past the month end + 5 spills into the next month
	assert pou.payout_period(date(2026, 3, 5), 31) == (date(2026, 1, 1), date(2026, 1, 31))  # Feb 28 + 5
	assert pou.payout_period(date(2026, 5, 5), 31) == (date(2026, 3, 1), date(2026, 3, 31))  # Apr 30 + 5
	assert pou.payout_period(date(2026, 4, 5), 31) == (date(2026, 2, 1), date(2026, 2, 28))  # Mar 31 + 5
	assert pou.payout_period(date(2026, 3, 6), None) == (date(2026, 2, 1), date(2026, 2, 28))


def test_payout_transitions_and_credits():
	assert pou.check_payout_transition("Draft", "Approved") == "Approved"
	assert pou.check_payout_transition("Approved", "Paid") == "Paid"
	assert pou.check_payout_transition("Approved", "Draft") == "Draft"
	assert pou.check_payout_transition("Paid", "Paid") == "Paid"
	assert pou.check_payout_transition(None, "Draft") == "Draft"
	for old, new in (("Draft", "Paid"), ("Paid", "Approved"), ("Paid", "Draft"), ("Draft", "Bogus")):
		with pytest.raises(pou.PayoutError):
			pou.check_payout_transition(old, new)
	assert pou.credits_due("Credits", "Approved", "Paid")
	assert not pou.credits_due("Credits", "Approved", "Paid", already_credited=True)
	assert not pou.credits_due("Bank", "Approved", "Paid")
	assert not pou.credits_due("Credits", "Draft", "Approved")


def test_revenue_by_month():
	rows = [
		{"paid_on": "2026-02-03 10:00:00", "amount": 10, "currency": "USD"},
		{"paid_on": date(2026, 2, 20), "amount": "0.05", "currency": "USD"},
		{"paid_on": "2026-03-01", "amount": 20, "currency": "USD"},
		{"paid_on": None, "amount": 99, "currency": "USD"},
	]
	months = pou.revenue_by_month(rows, 80)
	assert [m["month"] for m in months] == ["2026-03", "2026-02"]
	feb = months[1]
	assert feb["gross"] == Decimal("10.05") and feb["share"] == Decimal("8.04") and feb["sales"] == 2


# --------------------------------------------------------------------------- review state machine


def test_review_state_machine():
	t = pu.listing_transition
	assert t("Draft", "submit") == "In Review"
	assert t("Rejected", "submit") == "In Review"
	assert t(None, "submit") == "In Review"
	assert t("In Review", "withdraw") == "Draft"
	assert t("In Review", "approve", is_admin=True) == "Published"
	assert t("In Review", "reject", is_admin=True, notes="Add screenshots") == "Rejected"
	assert t("Published", "unpublish", is_admin=True) == "Draft"
	with pytest.raises(pu.ListingError, match="administrators"):
		t("In Review", "approve")  # publishers cannot approve their own listing
	with pytest.raises(pu.ListingError, match="review notes"):
		t("In Review", "reject", is_admin=True, notes="  ")
	for status, action in (("Published", "submit"), ("Draft", "approve"), ("Rejected", "approve"), ("Draft", "withdraw"), ("Draft", "unpublish")):
		with pytest.raises(pu.ListingError):
			t(status, action, is_admin=True)
	with pytest.raises(pu.ListingError):
		t("Draft", "launch")


def test_sync_approval():
	s = pu.sync_approval
	# new documents: curated apps created approved are Published
	assert s("Draft", 1, is_new=True) == ("Published", True)
	assert s("Draft", 0, is_new=True) == ("Draft", False)
	assert s("Published", 0, is_new=True) == ("Published", True)
	# status change wins
	assert s("Published", 0, "In Review", 0) == ("Published", True)
	assert s("Draft", 1, "Published", 1) == ("Draft", False)
	assert s("Rejected", 1, "In Review", 1) == ("Rejected", False)
	# approved flag toggled in the desk
	assert s("Draft", 1, "Draft", 0) == ("Published", True)
	assert s("Published", 0, "Published", 1) == ("Draft", False)
	assert s("In Review", 0, "In Review", 1) == ("In Review", False)
	# nothing changed: normalised
	assert s("Published", 1, "Published", 1) == ("Published", True)
	assert s(None, None, None, None) == ("Draft", False)


def test_editable_fields_and_clean_values():
	assert "pricing" in pu.editable_listing_fields("Draft")
	assert pu.editable_listing_fields("In Review") == ()
	assert "pricing" not in pu.editable_listing_fields("Published")
	assert "description" in pu.editable_listing_fields("Published")
	out = pu.clean_listing_values({"title": "  Acme  ERP ", "pricing": "Paid", "price_monthly": "12.5", "bogus": 1}, "Draft")
	assert out == {"title": "Acme ERP", "pricing": "Paid", "price_monthly": Decimal("12.50")}
	assert pu.clean_listing_values({"pricing": "Free", "price_monthly": 9}, "Draft")["price_monthly"] == Decimal("0.00")
	with pytest.raises(pu.ListingError, match="monthly price"):
		pu.clean_listing_values({"pricing": "Paid", "price_monthly": 0}, "Draft")
	with pytest.raises(pu.ListingError):
		pu.clean_listing_values({"price_monthly": 5}, "Published")
	with pytest.raises(pu.ListingError):
		pu.clean_listing_values({"title": "x"}, "In Review")
	with pytest.raises(pu.ListingError):
		pu.clean_listing_values({"price_monthly": "-1"}, "Draft")
	with pytest.raises(pu.ListingError):
		pu.clean_listing_values({"documentation_url": "javascript:alert(1)"}, "Draft")
	with pytest.raises(pu.ListingError):
		pu.clean_listing_values({"category": "Games"}, "Draft")
	assert pu.clean_listing_values({"logo": "/files/acme.png"}, "Published") == {"logo": "/files/acme.png"}
	with pytest.raises(pu.ListingError):
		pu.clean_listing_values({"logo": "/private/files/acme.png"}, "Draft")


def test_urls_and_screenshots():
	assert pu.is_valid_url("https://acme.dev/docs")
	assert not pu.is_valid_url("https://user:pw@acme.dev")
	assert not pu.is_valid_url('https://acme.dev/"><script>')
	assert not pu.is_valid_url("data:image/png;base64,xx")
	assert pu.parse_screenshots("https://a.dev/1.png\n\n/files/2.png\r\nhttps://a.dev/1.png") == ["https://a.dev/1.png", "/files/2.png"]
	assert pu.parse_screenshots(None) == []
	with pytest.raises(pu.ListingError):
		pu.parse_screenshots("ftp://a.dev/1.png")
	with pytest.raises(pu.ListingError):
		pu.parse_screenshots("\n".join(f"https://a.dev/{i}.png" for i in range(9)))


def test_check_submittable():
	ok = {"title": "Acme", "description": "<p>Does things</p>", "source_custom_app": "abc", "pricing": "Free"}
	assert pu.check_submittable(ok) == []
	assert "Add a description" in pu.check_submittable(dict(ok, description="<p> </p>"))
	assert "Paid apps need a monthly price" in pu.check_submittable(dict(ok, pricing="Paid", price_monthly=0))
	assert "The listing has no source custom app" in pu.check_submittable(dict(ok, source_custom_app=None))


def test_publisher_values():
	assert pu.validate_share_percent(None) == Decimal("80")
	assert pu.validate_share_percent("70.5") == Decimal("70.5")
	for bad in ("101", "-1", "abc", "nan"):
		with pytest.raises(pu.ListingError):
			pu.validate_share_percent(bad)
	out = pu.clean_publisher_values({"title": " Acme ", "website": "https://acme.dev", "support_email": "help@acme.dev", "payout_method": "PayPal"})
	assert out == {"title": "Acme", "website": "https://acme.dev", "support_email": "help@acme.dev", "payout_method": "PayPal"}
	assert pu.clean_publisher_values({"payout_method": None})["payout_method"] == "Credits"
	for bad in ({"title": "x"}, {"website": "acme.dev"}, {"support_email": "nope"}, {"payout_method": "Cash"}):
		with pytest.raises(pu.ListingError):
			pu.clean_publisher_values(bad)
	assert pu.payout_details_required("Credits", "", False) is None
	assert pu.payout_details_required("PayPal", "pay@acme.dev", False) is None
	assert pu.payout_details_required("PayPal", "", False)
	assert pu.payout_details_required("Bank", "Acme Bank", False)
	assert pu.payout_details_required("Bank", "Acme Bank", True) is None


# --------------------------------------------------------------------------- releases from custom app majors


def test_releases_from_version_branches():
	branches = ["main", "develop", "version-14", "version-15", "v15", "version-15-hotfix", "v16", "version-13"]
	assert pu.releases_from_branches(branches, "main") == [
		{"frappe_major": "14", "branch": "version-14"},
		{"frappe_major": "15", "branch": "version-15"},  # version-NN beats vNN and suffixes
		{"frappe_major": "16", "branch": "v16"},
	]


def test_releases_declared_majors_use_tracked_branch():
	# app on `main` declaring 15 and 16; no version branches -> both from main
	assert pu.releases_from_branches(["main"], "main", ["15", "16"]) == [
		{"frappe_major": "15", "branch": "main"},
		{"frappe_major": "16", "branch": "main"},
	]
	# declared majors restrict version branches; a version branch beats the tracked branch
	assert pu.releases_from_branches(["main", "version-14", "version-15"], "main", ["15", "16"]) == [
		{"frappe_major": "15", "branch": "version-15"},
		{"frappe_major": "16", "branch": "main"},
	]
	# unsupported declared majors are ignored
	assert pu.releases_from_branches([], "main", ["13", "15"]) == [{"frappe_major": "15", "branch": "main"}]


def test_releases_fallbacks():
	# nothing declared, the tracked branch names the major
	assert pu.releases_from_branches([], "version-15") == [{"frappe_major": "15", "branch": "version-15"}]
	# nothing known at all
	assert pu.releases_from_branches(["main", "develop"], "main") == []
	# invalid branch names are ignored
	assert pu.releases_from_branches(["version-15..x", "version 16"], None, ["15"]) == []


def test_merge_releases_keeps_existing():
	existing = [{"frappe_major": "15", "branch": "stable", "commit": "abc"}]
	generated = [{"frappe_major": "15", "branch": "version-15"}, {"frappe_major": "14", "branch": "version-14"}]
	assert pu.merge_releases(existing, generated) == [
		{"frappe_major": "14", "branch": "version-14", "commit": None},
		{"frappe_major": "15", "branch": "stable", "commit": "abc"},
	]
	assert pu.merge_releases([], []) == []


def test_installs_by_app():
	rows = [
		{"app": "acme", "site": "a.example.com", "status": "Installed"},
		{"app": "acme", "site": "a.example.com", "status": "Installed"},
		{"app": "acme", "parent": "b.example.com", "status": "Pending"},
		{"app": "acme", "site": "c.example.com", "status": "Failed"},
		{"app": "zeta", "site": "a.example.com", "status": "Installed"},
	]
	assert pu.installs_by_app(rows) == {"acme": 2, "zeta": 1}


# --------------------------------------------------------------------------- static checks


@pytest.mark.parametrize("rel", ["publisher_utils.py", os.path.join("billing", "payouts_utils.py")])
def test_pure_modules(rel):
	assert not re.search(r"^\s*(import|from)\s+frappe", read(os.path.join(APP, rel)), re.M)


def test_doctypes():
	pub = {f["fieldname"]: f for f in doctype("Marketplace Publisher")["fields"]}
	for name in ("team", "title", "website", "support_email", "payout_method", "payout_details", "payout_account", "revenue_share_percent", "verified"):
		assert name in pub, name
	assert pub["payout_method"]["options"].split("\n") == list(pu.PAYOUT_METHODS)
	assert pub["payout_account"]["fieldtype"] == "Password"
	assert pub["revenue_share_percent"]["default"] == "80"
	assert pub["team"].get("unique") == 1

	app = {f["fieldname"]: f for f in doctype("Marketplace App")["fields"]}
	assert app["publisher"]["fieldtype"] == "Data"  # kept; the profile link is separate
	assert app["publisher_profile"]["options"] == "Marketplace Publisher"
	assert app["status"]["options"].split("\n") == list(pu.LISTING_STATUSES)
	assert app["source_custom_app"]["options"] == "Custom App"
	assert app["logo"]["fieldtype"] == "Attach Image"
	for name in ("review_notes", "screenshots", "documentation_url"):
		assert name in app

	payout = {f["fieldname"]: f for f in doctype("Publisher Payout")["fields"]}
	assert payout["status"]["options"].split("\n") == list(pou.PAYOUT_STATUSES)
	for name in ("publisher", "period_start", "period_end", "gross", "platform_fee", "net", "currency", "items"):
		assert name in payout, name
	assert payout["items"]["options"] == "Publisher Payout Item"
	item = {f["fieldname"] for f in doctype("Publisher Payout Item")["fields"]}
	assert {"invoice", "invoice_item", "app", "site", "amount", "share"} <= item


def test_api_whitelisted_checked_and_logged():
	tree = ast.parse(read(API))
	funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
	public = ("get_publisher", "save_publisher", "list_listings", "listable_custom_apps", "create_listing",
	          "update_listing", "submit_listing", "withdraw_listing", "approve_listing", "reject_listing",
	          "unpublish_listing", "get_revenue")
	for name in public:
		assert any("whitelist" in ast.unparse(d) for d in funcs[name].decorator_list), name
	for name in ("save_publisher", "create_listing", "update_listing", "submit_listing", "withdraw_listing"):
		body = ast.unparse(funcs[name])
		assert "_log(" in body, name
		assert "_ctx('admin')" in body or "_listing(" in body, name
	assert "frappe.only_for('System Manager')" in ast.unparse(funcs["_review"])
	assert "_ctx('billing')" in ast.unparse(funcs["get_revenue"])
	assert "payout_account" not in "".join(re.findall(r"PUBLISHER_FIELDS = \[[^\]]*\]", read(API), re.S))


def test_payouts_module():
	src = read(os.path.join(APP, "billing", "payouts.py"))
	assert "def generate_publisher_payouts(" in src
	assert "inv.status = 'Paid'" in src and "item.document_type = 'Marketplace App'" in src
	ctrl = read(os.path.join(DOCTYPES, "publisher_payout", "publisher_payout.py"))
	assert "add_credits(" in ctrl and "credits_due(" in ctrl


def test_js_component():
	js = read(JS)
	assert 'Alpine.data("nxPublisher"' in js
	tree = ast.parse(read(API))
	whitelisted = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
	used = set(re.findall(r'call\("(\w+)"', js)) | set(re.findall(r'_act\("(\w+)"', js))
	assert used and used <= whitelisted, used - whitelisted


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_js_syntax():
	subprocess.run(["node", "--check", JS], check=True, capture_output=True)


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_page_renders(lang):
	env = jinja2.Environment(loader=jinja2.FileSystemLoader(APP), undefined=jinja2.StrictUndefined)
	env.globals["_"] = lambda s, *a: s
	html = env.get_template("www/dashboard/publisher.html").render(
		title="Publisher", page="publisher", lang=lang, dir="rtl" if lang == "ar" else "ltr", csrf_token="tok",
		boot={"user": "a@b.c", "messages": {}}, user="a@b.c", user_fullname="A", is_admin=False,
		team="acme", team_role="Admin", teams=[{"name": "acme", "title": "Acme", "role": "Admin"}],
	)
	assert '<script src="/assets/nexaploy/js/publisher.js"></script>' in html
	assert 'x-data="nxPublisher"' in html
	assert html.count("<template") == html.count("</template>")
	page_py = read(PAGE_PY)
	assert "no_cache = 1" in page_py and "prepare_page(" in page_py


STRING_RE = re.compile(r"""(?<![A-Za-z0-9.])_{1,2}\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')\s*[,)]""")


def arabic_messages():
	ar = set()
	for path in [os.path.join(APP, "translations", "ar.csv")] + sorted(glob.glob(os.path.join(APP, "translations", "ar.*.part"))):
		with open(path, encoding="utf-8", newline="") as fh:
			ar |= {r[0] for r in csv.reader(fh) if r}
	return ar


def test_translations_cover_page_api_and_js():
	strings = set()
	for path in (PAGE, PAGE_PY, API, os.path.join(APP, "billing", "payouts.py"), os.path.join(APP, "www", "dashboard", "marketplace.html")):
		strings |= {m.group(1) or m.group(2) for m in STRING_RE.finditer(read(path))}
	strings |= set(re.findall(r'\bt\(\s*"((?:[^"\\]|\\.)*)"\s*[,)]', read(JS)))
	strings |= set(pu.LISTING_STATUSES) | set(pou.PAYOUT_STATUSES) | set(pu.PAYOUT_METHODS)
	missing = sorted(strings - arabic_messages())
	assert not missing, missing


def test_part_well_formed():
	path = os.path.join(APP, "translations", "ar.publisher.part")
	if not os.path.exists(path):
		pytest.skip("ar.publisher.part merged into ar.csv")
	with open(path, encoding="utf-8", newline="") as fh:
		rows = [r for r in csv.reader(fh) if r]
	assert rows
	sources = [r[0] for r in rows]
	assert len(sources) == len(set(sources))
	for row in rows:
		assert len(row) == 2 and row[0].strip() and row[1].strip(), row
		assert sorted(re.findall(r"\{\d\}", row[0])) == sorted(re.findall(r"\{\d\}", row[1])), row
