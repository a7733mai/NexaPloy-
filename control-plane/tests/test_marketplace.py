"""Version-aware marketplace: dependencies, official catalog, release verification, reviews
(pure helpers of nexaploy.marketplace_utils; no frappe needed)."""

import csv
import glob
import os
import re
import shutil
import subprocess

import jinja2
import pytest

from nexaploy import marketplace_utils as mu
from nexaploy import publisher_utils as pu
from nexaploy.utils import check_compatibility, is_valid_app_name

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "nexaploy")


def rel(major, branch=None, status=None):
	r = {"frappe_major": str(major), "branch": branch or f"version-{major}"}
	if status:
		r["status"] = status
	return r


CATALOG = {
	"erpnext": {"app_name": "erpnext", "approved": True, "repo_url": "https://github.com/frappe/erpnext", "required_apps": [], "releases": [rel(14), rel(15), rel(16)]},
	"hrms": {"app_name": "hrms", "approved": True, "repo_url": "https://github.com/frappe/hrms", "required_apps": ["erpnext"], "releases": [rel(15), rel(16)]},
	"payments": {"app_name": "payments", "approved": True, "repo_url": "https://github.com/frappe/payments", "required_apps": [], "releases": [rel(15)]},
	"lms": {"app_name": "lms", "approved": True, "repo_url": "https://github.com/frappe/lms", "required_apps": "payments", "releases": [rel(15), rel(16)]},
	"payroll_x": {"app_name": "payroll_x", "approved": True, "repo_url": "https://x/payroll_x", "required_apps": "hrms", "releases": [rel(16)]},
	"draft": {"app_name": "draft", "approved": False, "required_apps": [], "releases": [rel(16)]},
	"needs_draft": {"app_name": "needs_draft", "approved": True, "required_apps": ["draft"], "releases": [rel(16)]},
}


# ------------------------------------------------------------------ parsing


def test_parse_required_apps():
	assert mu.parse_required_apps(None) == []
	assert mu.parse_required_apps("erpnext") == ["erpnext"]
	assert mu.parse_required_apps("erpnext, payments\nerpnext; frappe") == ["erpnext", "payments"]
	assert mu.parse_required_apps(["ERPNext", " hrms "]) == ["erpnext", "hrms"]
	with pytest.raises(mu.RequirementError):
		mu.parse_required_apps("erp-next")
	with pytest.raises(mu.RequirementError):
		mu.parse_required_apps("hrms, erpnext", app_name="hrms")
	assert mu.format_required_apps(["erpnext", "payments"]) == "erpnext\npayments"


def test_normalize_major_and_supported_majors():
	assert mu.normalize_major("v16") == "16"
	assert mu.normalize_major(15) == "15"
	assert mu.normalize_major("", "16") == "16"
	assert mu.normalize_major("all") is None
	with pytest.raises(ValueError):
		mu.normalize_major("13")
	assert mu.supported_majors([rel(16), rel(14), rel(13), rel(14)]) == ["14", "16"]


# ------------------------------------------------------------------ dependency graph


def test_dependency_order_requirements_first_and_transitive():
	assert mu.dependency_order(["hrms"], CATALOG) == ["erpnext", "hrms"]
	assert mu.dependency_order(["payroll_x", "lms"], CATALOG) == ["erpnext", "hrms", "payroll_x", "payments", "lms"]
	assert mu.dependency_order(["custom_thing"], CATALOG) == ["custom_thing"]
	assert mu.sort_by_dependencies(["hrms", "crm", "erpnext"], CATALOG) == ["erpnext", "hrms", "crm"]


def test_dependency_cycle_detected():
	cyc = {"app_a": {"required_apps": ["app_b"]}, "app_b": {"required_apps": ["app_a"]}}
	with pytest.raises(mu.RequirementError, match="Circular app requirements: app_a -> app_b -> app_a"):
		mu.dependency_order(["app_a"], cyc)


def test_apps_to_add_auto_includes_requirements():
	assert mu.apps_to_add(["hrms"], [], CATALOG, "16") == ["erpnext", "hrms"]
	assert mu.apps_to_add(["hrms"], ["erpnext"], CATALOG, 16) == ["hrms"]
	assert mu.apps_to_add(["payroll_x"], [], CATALOG, "16") == ["erpnext", "hrms", "payroll_x"]
	# a custom fork named erpnext satisfies the requirement
	assert mu.apps_to_add(["hrms"], {"erpnext"}, CATALOG, "15") == ["hrms"]


def test_apps_to_add_refuses_clearly():
	with pytest.raises(mu.RequirementError, match="lms requires payments, which has no release for Frappe 16"):
		mu.apps_to_add(["lms"], [], CATALOG, "16")
	with pytest.raises(mu.RequirementError, match="not approved"):
		mu.apps_to_add(["needs_draft"], [], CATALOG, "16")
	with pytest.raises(mu.RequirementError, match="not in the marketplace"):
		mu.apps_to_add(["x_app"], [], {"x_app": {"required_apps": ["ghost"]}}, "16")


def test_unverified_requirement_does_not_count():
	cat = dict(CATALOG)
	cat["erpnext"] = dict(CATALOG["erpnext"], releases=[rel(16, status=mu.PENDING)])
	assert not mu.has_release({"releases": mu.visible_releases(cat["erpnext"]["releases"])}, 16)


def test_missing_requirements_for_group_validation():
	rows = [{"app_name": "hrms", "required_apps": ["erpnext"]}, {"app_name": "crm"}]
	assert mu.missing_requirements(rows) == ["hrms requires erpnext; add erpnext to the group"]
	rows.append({"app_name": "erpnext"})  # marketplace or custom app with that name
	assert mu.missing_requirements(rows) == []


def test_dependents():
	assert mu.dependents("erpnext", ["erpnext", "hrms", "crm"], CATALOG) == ["hrms"]
	assert mu.dependents("crm", ["erpnext", "hrms"], CATALOG) == []


def test_install_plan_orders_and_skips_installed():
	assert mu.install_plan(["hrms"], [], ["erpnext", "hrms"], CATALOG) == ["erpnext", "hrms"]
	assert mu.install_plan(["hrms"], ["erpnext"], ["erpnext", "hrms"], CATALOG) == ["hrms"]
	assert mu.install_plan(["hrms", "erpnext"], [], ["erpnext", "hrms"], CATALOG) == ["erpnext", "hrms"]


def test_install_plan_refuses_when_requirement_not_on_bench():
	with pytest.raises(mu.RequirementError) as exc:
		mu.install_plan(["hrms"], [], ["hrms"], CATALOG, group="acme-v16")
	assert "hrms requires erpnext, which is not part of bench group acme-v16" in str(exc.value)
	with pytest.raises(mu.RequirementError, match="crm is not available on this bench"):
		mu.install_plan(["crm"], [], ["erpnext"], CATALOG)


# ------------------------------------------------------------------ listing


def test_listing_item_version_filter_badges_and_requirements():
	app = {"app_name": "hrms", "approved": 1, "required_apps": "erpnext", "releases": [rel(15), rel(16), rel(14, status=mu.FAILED)]}
	item = mu.listing_item(app, "16")
	assert item["supported_majors"] == ["15", "16"]
	assert item["releases"] == [{"frappe_major": "16", "branch": "version-16", "commit": None}]
	assert item["required_apps"] == ["erpnext"]
	assert mu.listing_item(app, "14") is None  # failed release is not offered
	assert mu.listing_item(dict(app, approved=0)) is None
	assert mu.listing_item(dict(app, releases=[rel(16, status=mu.PENDING)])) is None
	# legacy rows without status count as verified
	assert mu.listing_item(dict(app, releases=[{"frappe_major": 16, "branch": "x"}]), "16")["supported_majors"] == ["16"]


def test_search_apps():
	apps = [
		{"app_name": "hrms", "title": "Frappe HR", "category": "HR", "description": "<p>Payroll</p>"},
		{"app_name": "crm", "title": "Frappe CRM", "category": "CRM", "description": "Sales"},
	]
	assert [a["app_name"] for a in mu.search_apps(apps, "payroll")] == ["hrms"]
	assert [a["app_name"] for a in mu.search_apps(apps, "frappe", "CRM")] == ["crm"]
	assert [a["app_name"] for a in mu.search_apps(apps, "p>")] == []  # tags are not searchable
	assert len(mu.search_apps(apps)) == 2


# ------------------------------------------------------------------ official catalog


def test_official_catalog_is_consistent():
	names = [s["app_name"] for s in mu.OFFICIAL_APPS]
	assert len(names) == len(set(names))
	assert set(names) >= {
		"erpnext", "hrms", "crm", "helpdesk", "insights", "payments", "lms", "print_designer", "wiki",
		"builder", "drive", "ecommerce_integrations", "india_compliance",
	}
	by_name = {s["app_name"]: s for s in mu.OFFICIAL_APPS}
	seen = set()
	for seed in mu.OFFICIAL_APPS:
		assert is_valid_app_name(seed["app_name"])
		assert seed["repo_url"].startswith("https://github.com/")
		assert seed["category"] in pu.CATEGORIES
		assert seed["releases"] and set(seed["releases"]) <= set(mu.SUPPORTED_MAJORS)
		for req in seed["required_apps"]:
			# requirements are seeded first, and support every major the app does
			assert req in seen, (seed["app_name"], req)
			assert set(seed["releases"]) <= set(by_name[req]["releases"]), (seed["app_name"], req)
		seen.add(seed["app_name"])
		values = mu.official_app_values(seed)
		assert values["approved"] == 1 and values["status"] == "Published" and values["pricing"] == "Free"
	# required by the brief
	for app in ("hrms", "ecommerce_integrations", "india_compliance"):
		assert by_name[app]["required_apps"] == ["erpnext"]
	for app in ("erpnext", "hrms"):
		assert by_name[app]["releases"] == {"14": "version-14", "15": "version-15", "16": "version-16"}
	# every official app supports Frappe 15 (the release groups' common denominator)
	assert all("15" in s["releases"] for s in mu.OFFICIAL_APPS)


def test_official_catalog_passes_group_compatibility():
	apps = [dict(mu.official_app_values(s), releases=mu.seed_releases(s)) for s in mu.OFFICIAL_APPS]
	v16 = [a for a in apps if mu.has_release(a, "16")]
	assert check_compatibility("16", v16) == []
	assert {"erpnext", "hrms", "crm", "helpdesk"} <= {a["app_name"] for a in v16}
	assert "print_designer" not in {a["app_name"] for a in v16}


def test_missing_seed_releases_keeps_curated_rows():
	seed = next(s for s in mu.OFFICIAL_APPS if s["app_name"] == "erpnext")
	existing = [{"frappe_major": "16", "branch": "my-fork"}]
	assert mu.missing_seed_releases(existing, seed) == [rel(14), rel(15)]
	assert mu.missing_seed_releases(mu.seed_releases(seed), seed) == []


# ------------------------------------------------------------------ release verification


def test_release_visibility():
	assert mu.release_visible({"status": "Verified"})
	assert mu.release_visible({})  # created before verification existed
	assert not mu.release_visible({"status": mu.PENDING})
	assert not mu.release_visible({"status": mu.FAILED})


def test_verification_apps_json_minimal_and_ordered():
	app = {"app_name": "payroll_x", "repo_url": "https://x/payroll_x", "required_apps": ["hrms"]}
	assert mu.verification_apps_json(app, "main", CATALOG, "16") == [
		{"url": "https://github.com/frappe/erpnext", "branch": "version-16"},
		{"url": "https://github.com/frappe/hrms", "branch": "version-16"},
		{"url": "https://x/payroll_x", "branch": "main"},
	]
	assert mu.verification_apps_json({"app_name": "crm", "repo_url": "https://github.com/frappe/crm"}, "version-16", CATALOG, 16) == [
		{"url": "https://github.com/frappe/crm", "branch": "version-16"}
	]


def test_verification_apps_json_needs_verified_requirements():
	cat = dict(CATALOG)
	cat["erpnext"] = dict(CATALOG["erpnext"], releases=[rel(16, status=mu.PENDING)])
	app = {"app_name": "hrms", "repo_url": "https://github.com/frappe/hrms", "required_apps": ["erpnext"]}
	with pytest.raises(mu.RequirementError, match="no verified release for Frappe 16"):
		mu.verification_apps_json(app, "version-16", cat, "16")
	with pytest.raises(mu.RequirementError):
		mu.verification_apps_json({"app_name": "crm", "repo_url": ""}, "main", CATALOG, "16")


def test_verification_tag_is_a_valid_image_ref():
	tag = mu.verification_tag("india_compliance", 16, "20261104T101500")
	assert tag == "nexaploy/verify:india_compliance-v16-20261104t101500"
	image_re = re.compile(
		r"^(?:[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?/)?"
		r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*"
		r":[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$"
	)
	assert image_re.match(tag)


def test_release_changed():
	assert mu.release_changed(None, {"branch": "main"})
	assert not mu.release_changed({"branch": "main", "commit": None}, {"branch": "main", "commit": ""})
	assert mu.release_changed({"branch": "main"}, {"branch": "develop"})
	assert mu.release_changed({"branch": "main", "commit": "abc1234"}, {"branch": "main", "commit": "def5678"})


# ------------------------------------------------------------------ reviews


def test_rating_and_review_cleaning():
	assert mu.clean_rating("5") == 5
	for bad in (0, 6, "x", None, "4.5"):
		with pytest.raises(mu.RequirementError):
			mu.clean_rating(bad)
	assert mu.clean_review_text("  <b>Great</b> app\r\n\r\n\r\n\r\nThanks ") == "Great app\n\nThanks"
	with pytest.raises(mu.RequirementError):
		mu.clean_review_text("x" * (mu.MAX_REVIEW_LENGTH + 1))


def test_rating_summaries_ignore_hidden():
	rows = [
		{"app": "hrms", "rating": 5}, {"app": "hrms", "rating": 4}, {"app": "hrms", "rating": 1, "hidden": 1},
		{"app": "crm", "rating": 3},
	]
	assert mu.rating_summaries(rows) == {"hrms": {"average": 4.5, "count": 2}, "crm": {"average": 3.0, "count": 1}}
	assert mu.rating_summary([]) == {"average": None, "count": 0}


def test_publisher_listing_required_apps():
	out = pu.clean_listing_values({"required_apps": "erpnext, payments"}, "Draft")
	assert out["required_apps"] == "erpnext\npayments"
	with pytest.raises(pu.ListingError):
		pu.clean_listing_values({"required_apps": "Bad-Name"}, "Draft")
	with pytest.raises(pu.ListingError):
		pu.clean_listing_values({"required_apps": "erpnext"}, "Published")
	assert "Support" in pu.CATEGORIES


# ------------------------------------------------------------------ portal, JS, translations


def read(path):
	with open(path, encoding="utf-8") as fh:
		return fh.read()


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
@pytest.mark.parametrize("js", ["dashboard.js", "benches.js", "publisher.js"])
def test_js_syntax(js):
	subprocess.run(["node", "--check", os.path.join(APP, "public", "js", js)], check=True, capture_output=True)


def test_marketplace_page_renders_and_defaults_to_16():
	env = jinja2.Environment(loader=jinja2.FileSystemLoader(APP), undefined=jinja2.Undefined)
	env.globals["_"] = lambda s, *a: s
	src = read(os.path.join(APP, "www", "dashboard", "marketplace.html"))
	body = src.split("{% block content %}", 1)[1].split("{% endblock %}", 1)[0]
	html = env.from_string(body).render()
	assert html.count("<template") == html.count("</template>")
	assert 'x-model="q"' in html and 'x-model="category"' in html and 'x-model="major"' in html
	assert "a.required_apps" in html and "majorsOf(a)" in html and "stars(" in html
	js = read(os.path.join(APP, "public", "js", "dashboard.js"))
	assert 'query("major") || "16"' in js
	assert "supported_majors" in js
	assert "cdn" not in src.lower()


STRING_RE = re.compile(r"""(?<![A-Za-z0-9.])_{1,2}\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')\s*[,)]""")
T_RE = re.compile(r'\bt\(\s*"((?:[^"\\]|\\.)*)"\s*[,)]')


def arabic_messages():
	ar = set()
	for path in [os.path.join(APP, "translations", "ar.csv")] + sorted(glob.glob(os.path.join(APP, "translations", "ar.*.part"))):
		with open(path, encoding="utf-8", newline="") as fh:
			ar |= {r[0] for r in csv.reader(fh) if r}
	return ar


def test_translations_cover_marketplace_strings():
	strings = set()
	for rel_path in (
		"www/dashboard/marketplace.html", "www/dashboard/site.html", "www/dashboard/publisher.html",
		"www/dashboard/benches.html", "api/marketplace.py", "api/benches.py", "api/publisher.py",
		"nexaploy/doctype/marketplace_app/marketplace_app.py",
		"nexaploy/doctype/marketplace_app_review/marketplace_app_review.py",
	):
		strings |= {m.group(1) or m.group(2) for m in STRING_RE.finditer(read(os.path.join(APP, rel_path)))}
	# Site: only the app install messages (the rest of site.py belongs to other features)
	site_py = read(os.path.join(APP, "nexaploy", "doctype", "site", "site.py"))
	for msg in (
		"Apps {0} are not available on bench {1} (release group {2}); add them to the group and deploy first",
		"Missing required apps", "{0} is already being installed", "{0} is required by {1}; uninstall those first",
	):
		assert msg in site_py
		strings.add(msg)
	for js in ("benches.js", "publisher.js"):
		strings |= set(T_RE.findall(read(os.path.join(APP, "public", "js", js))))
	dashboard = read(os.path.join(APP, "public", "js", "dashboard.js"))
	block = dashboard[dashboard.index('Alpine.data("nxMarketplace"') : dashboard.index('Alpine.data("nxBilling"')]
	strings |= {m.group(1) or m.group(2) for m in STRING_RE.finditer(block)}
	strings |= set(mu.RELEASE_STATUSES) | {s["category"] for s in mu.OFFICIAL_APPS}
	missing = sorted(strings - arabic_messages())
	assert not missing, missing
