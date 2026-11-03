"""Customer portal (/dashboard): pure helpers, templates, translations, static checks.

Runs without a bench: templates are parsed and rendered with plain Jinja2 and a stubbed `_`.
"""

import ast
import csv
import glob
import os
import py_compile
import re
import shutil
import subprocess
from datetime import datetime, timedelta

import jinja2
import pytest

from nexaploy import portal_utils as pu

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "nexaploy")
WWW = os.path.join(APP, "www", "dashboard")
BASE = os.path.join(APP, "templates", "dashboard_base.html")
JS = os.path.join(APP, "public", "js", "dashboard.js")
CSS = os.path.join(APP, "public", "css", "dashboard.css")
API = os.path.join(APP, "api", "dashboard.py")
AR_CSV = os.path.join(APP, "translations", "ar.csv")
# Feature branches drop their strings in translations/ar.<feature>.part (same CSV format);
# the integrator merges them into ar.csv. The checks below accept both.
AR_PARTS = sorted(glob.glob(os.path.join(APP, "translations", "ar.*.part")))
TEAMS_API = os.path.join(APP, "api", "teams.py")
TEAMS = os.path.join(APP, "teams.py")

PAGES = ["index", "sites", "site", "servers", "server", "marketplace", "billing", "team"]
PAGE_HTML = sorted(glob.glob(os.path.join(WWW, "*.html")))

# _("...") / __('...') with a literal first argument
STRING_RE = re.compile(r"""(?<![A-Za-z0-9.])_{1,2}\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')\s*[,)]""")


def read(path):
	with open(path, encoding="utf-8") as fh:
		return fh.read()


# --------------------------------------------------------------------------- pure helpers


def test_portal_utils_has_no_frappe_import():
	tree = ast.parse(read(os.path.join(APP, "portal_utils.py")))
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			assert all(not a.name.startswith("frappe") for a in node.names)
		if isinstance(node, ast.ImportFrom):
			assert not (node.module or "").startswith("frappe")


@pytest.mark.parametrize(
	"raw,expected",
	[
		("shop", "shop"),
		("  My Shop_1 ", "my-shop-1"),
		("ACME--Corp", "acme-corp"),
		("-abc-", "abc"),
		("a.b c", "a-b-c"),
		("café", "caf"),
		("x" * 63, "x" * 63),
	],
)
def test_normalize_subdomain(raw, expected):
	assert pu.normalize_subdomain(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "ab", "--", "!!!", "x" * 64, "www", "admin", "API"])
def test_normalize_subdomain_rejects(raw):
	with pytest.raises(ValueError):
		pu.normalize_subdomain(raw)


def test_normalize_root_domain():
	assert pu.normalize_root_domain(None) == "nexaploy.cloud"
	assert pu.normalize_root_domain("") == pu.DEFAULT_ROOT_DOMAIN
	assert pu.normalize_root_domain(" Example.COM. ") == "example.com"
	assert pu.normalize_root_domain("not a domain") == pu.DEFAULT_ROOT_DOMAIN


def test_build_site_name():
	assert pu.build_site_name("Shop", None) == "shop.nexaploy.cloud"
	assert pu.build_site_name("shop", "example.com") == "shop.example.com"
	assert pu.build_site_name("shop.nexaploy.cloud", "nexaploy.cloud") == "shop.nexaploy.cloud"
	assert pu.build_site_name("My Shop", "nexaploy.cloud") == "my-shop.nexaploy.cloud"
	assert pu.build_site_name("ERP.Example.com", "nexaploy.cloud", allow_custom=True) == "erp.example.com"


@pytest.mark.parametrize(
	"value,allow_custom",
	[
		("erp.example.com", False),  # custom domains only on own servers
		("a.b.nexaploy.cloud", False),  # one level only
		("www.nexaploy.cloud", False),  # reserved
		("", False),
		("bad_domain..com", True),
		("-x.example.com", True),
	],
)
def test_build_site_name_rejects(value, allow_custom):
	with pytest.raises(ValueError):
		pu.build_site_name(value, "nexaploy.cloud", allow_custom=allow_custom)


def test_generate_password():
	seen = set()
	for _ in range(50):
		pwd = pu.generate_password()
		assert len(pwd) == 16
		assert any(c.islower() for c in pwd) and any(c.isupper() for c in pwd) and any(c.isdigit() for c in pwd)
		assert not set(pwd) & set("lIO0o1")
		seen.add(pwd)
	assert len(seen) == 50
	assert len(pu.generate_password(8)) == 12  # minimum length
	assert len(pu.generate_password(24)) == 24


def test_parse_app_list():
	assert pu.parse_app_list(None) == []
	assert pu.parse_app_list("") == []
	assert pu.parse_app_list(["erpnext", "hrms", "erpnext", " "]) == ["erpnext", "hrms"]
	assert pu.parse_app_list('["erpnext", "crm"]') == ["erpnext", "crm"]
	assert pu.parse_app_list("erpnext, hrms") == ["erpnext", "hrms"]
	with pytest.raises(ValueError):
		pu.parse_app_list("[broken")
	with pytest.raises(ValueError):
		pu.parse_app_list({"a": 1})


def test_to_bool():
	assert pu.to_bool("1") and pu.to_bool("true") and pu.to_bool(1) and pu.to_bool(True)
	assert not pu.to_bool("0") and not pu.to_bool("false") and not pu.to_bool(0)
	assert pu.to_bool(None, True) and pu.to_bool("", True)


def test_filter_action_args():
	assert pu.filter_action_args("migrate", {"app": "x", "cmd": "y"}) == {}
	assert pu.filter_action_args("install_app", {"app": " erpnext ", "evil": 1}) == {"app": "erpnext"}
	assert pu.filter_action_args("add_domain", {"domain": "ERP.Example.com"}) == {"domain": "erp.example.com"}
	assert pu.filter_action_args("backup", {"with_files": 0}) == {"with_files": 0}
	assert pu.filter_action_args("backup", None) == {}
	assert pu.filter_action_args("restore", {"backup": "abc"}) == {"backup": "abc"}
	with pytest.raises(ValueError):
		pu.filter_action_args("install_app", {})
	with pytest.raises(ValueError):
		pu.filter_action_args("restore", {"backup": ""})
	with pytest.raises(ValueError):
		pu.filter_action_args("move_to_bench", {})  # not exposed to the portal
	with pytest.raises(ValueError):
		pu.filter_action_args("__init__", {})


def test_site_actions_match_site_controller():
	"""Every portal action except restore is a whitelisted Site method."""
	tree = ast.parse(read(os.path.join(APP, "nexaploy", "doctype", "site", "site.py")))
	methods = {
		fn.name
		for cls in tree.body
		if isinstance(cls, ast.ClassDef)
		for fn in cls.body
		if isinstance(fn, ast.FunctionDef)
	}
	for action in pu.SITE_ACTIONS:
		if action != "restore":
			assert action in methods, action


def test_text_direction():
	assert pu.text_direction("ar") == "rtl"
	assert pu.text_direction("ar-EG") == "rtl"
	assert pu.text_direction("ar_SA") == "rtl"
	assert pu.text_direction("en") == "ltr"
	assert pu.text_direction(None) == "ltr"
	assert pu.text_direction("fr") == "ltr"


def test_redirects():
	assert pu.safe_redirect_path("/dashboard/sites") == "/dashboard/sites"
	assert pu.safe_redirect_path("//evil.com") == "/dashboard"
	assert pu.safe_redirect_path("https://evil.com") == "/dashboard"
	assert pu.safe_redirect_path("/x\r\nSet-Cookie: a") == "/dashboard"
	assert pu.login_redirect_url("/dashboard/site", "name=a.b.c") == "/login?redirect-to=/dashboard/site%3Fname%3Da.b.c"
	assert pu.login_redirect_url("/dashboard", b"") == "/login?redirect-to=/dashboard"
	assert pu.login_redirect_url("//evil.com") == "/login?redirect-to=/dashboard"


def test_roles():
	assert pu.has_portal_access(["Nexaploy Customer"])
	assert pu.has_portal_access(["System Manager"])
	assert pu.has_portal_access([], user="Administrator")
	assert not pu.has_portal_access(["Website User"])
	assert pu.is_portal_admin(["System Manager"])
	assert not pu.is_portal_admin(["Nexaploy Customer"])
	assert pu.is_portal_admin([], user="Administrator")


def test_job_and_time_helpers():
	assert pu.is_job_active("Pending") and pu.is_job_active("Running")
	assert not pu.is_job_active("Success") and not pu.is_job_active("Failure") and not pu.is_job_active(None)
	now = datetime(2026, 11, 3, 12, 0, 0)
	assert pu.seconds_since(None, now) is None
	assert pu.seconds_since(now - timedelta(minutes=2), now) == 120
	assert pu.seconds_since(now + timedelta(minutes=2), now) == 0


def test_summarize_counts_and_default_plan():
	rows = [{"status": "Active"}, {"status": "Active"}, {"status": "Broken"}, {}]
	assert pu.summarize_counts(rows) == {"Active": 2, "Broken": 1, "Unknown": 1}
	plans = [
		{"name": "Site Pro", "price_monthly": 25},
		{"name": "Site Basic", "price_monthly": 5},
		{"name": "Off", "price_monthly": 1, "enabled": 0},
	]
	assert pu.pick_default_plan(plans) == "Site Basic"
	assert pu.pick_default_plan([]) is None


# --------------------------------------------------------------------------- pages & templates


def test_pages_present():
	for page in PAGES:
		assert os.path.isfile(os.path.join(WWW, page + ".html")), page
		assert os.path.isfile(os.path.join(WWW, page + ".py")), page
	assert os.path.isfile(os.path.join(APP, "www", "__init__.py"))
	assert os.path.isfile(os.path.join(WWW, "__init__.py"))
	assert os.path.isfile(BASE) and os.path.isfile(JS) and os.path.isfile(CSS) and os.path.isfile(AR_CSV)


@pytest.mark.parametrize("page", PAGES)
def test_page_controller(page):
	src = read(os.path.join(WWW, page + ".py"))
	tree = ast.parse(src)
	names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
	assert "get_context" in names
	assigns = {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
	assert "no_cache" in assigns
	assert "prepare_page(" in src


def _env():
	env = jinja2.Environment(loader=jinja2.FileSystemLoader(APP), undefined=jinja2.StrictUndefined)
	env.globals["_"] = lambda s, *a: s
	return env


def _context(page, lang="en"):
	return {
		"title": page.title(),
		"page": page,
		"lang": lang,
		"dir": pu.text_direction(lang),
		"csrf_token": "tok</script>",
		"boot": {"user": "a@b.c", "root_domain": "nexaploy.cloud", "messages": {"Sites": "المواقع"}},
		"user": "a@b.c",
		"user_fullname": "A B",
		"is_admin": False,
		"team": "acme",
		"team_role": "Developer",
		"teams": [{"name": "acme", "title": "Acme", "role": "Developer"}],
	}


@pytest.mark.parametrize("path", PAGE_HTML + [BASE], ids=os.path.basename)
def test_template_parses(path):
	jinja2.Environment().parse(read(path))


@pytest.mark.parametrize("page", PAGES)
@pytest.mark.parametrize("lang", ["en", "ar"])
def test_template_renders(page, lang):
	html = _env().get_template(f"www/dashboard/{page}.html").render(**_context(page, lang))
	assert html.lstrip().startswith("<!doctype html>")
	assert f'dir="{pu.text_direction(lang)}"' in html
	assert f'lang="{lang}"' in html
	assert "/assets/nexaploy/js/dashboard.js" in html
	assert "alpinejs@3.14.1" in html
	assert "</script>\";" not in html  # tojson escapes "</script>" in the CSRF token
	assert html.count("<template") == html.count("</template>")
	assert "{{" not in html and "{%" not in html


@pytest.mark.parametrize("path", PAGE_HTML, ids=os.path.basename)
def test_pages_extend_base(path):
	src = read(path)
	assert src.startswith('{% extends "templates/dashboard_base.html" %}')


def test_alpine_components_registered():
	import glob
	js = "\n".join(read(f) for f in glob.glob(os.path.join(APP, "public", "js", "*.js")))
	registered = set(re.findall(r'Alpine\.data\("(\w+)"', js))
	used = set()
	for path in PAGE_HTML:
		used |= set(re.findall(r'x-data="(nx\w+)"', read(path)))
	assert used and used <= registered, used - registered


def test_js_calls_known_api_methods():
	"""Every nexaploy.api.dashboard.* method used in JS exists and is whitelisted."""
	js = read(JS)
	used = set(re.findall(r'\bapi\("(\w+)"', js)) | set(re.findall(r"nexaploy\.api\.dashboard\.(\w+)", js))
	assert used <= set(_whitelisted()), used - set(_whitelisted())


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_js_syntax():
	subprocess.run(["node", "--check", JS], check=True, capture_output=True)


def test_css_supports_dark_and_rtl():
	css = read(CSS)
	assert "prefers-color-scheme: dark" in css
	assert "inline-start" in css and "inline-end" in css
	assert "margin-left" not in css and "padding-left" not in css


# --------------------------------------------------------------------------- API module


def _whitelisted():
	tree = ast.parse(read(API))
	result = {}
	for fn in tree.body:
		if not isinstance(fn, ast.FunctionDef):
			continue
		for dec in fn.decorator_list:
			target = dec.func if isinstance(dec, ast.Call) else dec
			if isinstance(target, ast.Attribute) and target.attr == "whitelist":
				result[fn.name] = fn
	return result


def test_api_methods_present():
	expected = {
		"overview", "list_sites", "get_site", "list_servers", "get_server", "list_benches",
		"create_site", "site_action", "get_job", "create_registration_token", "marketplace",
	}
	assert expected <= set(_whitelisted())


def test_api_methods_enforce_portal_access():
	"""Every whitelisted method starts by resolving the session user's team via _ctx()."""
	for name, fn in _whitelisted().items():
		first = fn.body[0]
		if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
			first = fn.body[1]  # docstring
		calls = [n for n in ast.walk(first) if isinstance(n, ast.Call)]
		assert any(isinstance(c.func, ast.Name) and c.func.id == "_ctx" for c in calls), name
		assert not any(
			isinstance(d, ast.Call) and any(k.arg == "allow_guest" for k in d.keywords) for d in fn.decorator_list
		), name


def test_create_site_signature():
	fn = _whitelisted()["create_site"]
	assert [a.arg for a in fn.args.args] == ["bench", "subdomain_or_name", "apps", "plan"]


def test_py_compile_all():
	for path in glob.glob(os.path.join(APP, "**", "*.py"), recursive=True):
		py_compile.compile(path, doraise=True)


# --------------------------------------------------------------------------- translations


def _source_strings():
	files = PAGE_HTML + [BASE, JS, API, TEAMS_API, TEAMS] + glob.glob(os.path.join(WWW, "*.py"))
	strings = set()
	for path in files:
		for m in STRING_RE.finditer(read(path)):
			strings.add(m.group(1) if m.group(1) is not None else m.group(2))
	return strings


def _read_csv(path):
	with open(path, encoding="utf-8", newline="") as fh:
		return [r for r in csv.reader(fh) if r]


def _ar():
	"""ar.csv plus every translations/ar.*.part file."""
	rows = _read_csv(AR_CSV)
	for part in AR_PARTS:
		rows += _read_csv(part)
	return rows


@pytest.mark.parametrize("path", [AR_CSV] + AR_PARTS, ids=os.path.basename)
def test_ar_csv_no_duplicates(path):
	sources = [r[0] for r in _read_csv(path)]
	assert len(sources) == len(set(sources)), "duplicate source strings"


def test_ar_csv_well_formed():
	rows = _ar()
	assert rows
	for row in rows:
		assert len(row) in (2, 3), row
		assert row[0].strip() and row[1].strip(), row
		# placeholders survive translation
		assert sorted(re.findall(r"\{\d\}", row[0])) == sorted(re.findall(r"\{\d\}", row[1])), row


def test_ar_csv_covers_portal_strings():
	translated = {r[0] for r in _ar()}
	strings = _source_strings()
	assert len(strings) > 100
	missing = sorted(strings - translated)
	assert not missing, missing


def test_ar_csv_covers_helper_errors_and_statuses():
	translated = {r[0] for r in _ar()}
	src = read(os.path.join(APP, "portal_utils.py"))
	errors = {m for m in re.findall(r'raise ValueError\("([^"{]+)"\)', src)}
	assert errors and errors <= translated, errors - translated
	for status in ("Active", "Pending", "Broken", "Offline", "Success", "Failure", "Running", "Installed", "Unpaid", "Paid"):
		assert status in translated
	js = read(JS)
	labels = set(re.findall(r'^\s+\w+: "([^"]+)",?$', js[js.index("ACTION_LABELS") : js.index("};", js.index("ACTION_LABELS"))], re.M))
	assert labels and labels <= translated, labels - translated
