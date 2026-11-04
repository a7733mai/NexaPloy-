"""Site observability: pure helpers (nexaploy.observability_utils), API module and portal page
static checks."""

import ast
import csv
import glob
import json
import os
import re
import shutil
import subprocess
from datetime import date

import jinja2
import pytest

from nexaploy import observability_utils as obs

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "nexaploy")
API = os.path.join(APP, "api", "observability.py")
PAGE = os.path.join(APP, "www", "dashboard", "insights.html")
PAGE_PY = os.path.join(APP, "www", "dashboard", "insights.py")
JS = os.path.join(APP, "public", "js", "insights.js")
PART = os.path.join(APP, "translations", "ar.csv")
AGENT_JOB = os.path.join(APP, "agent_job.py")
AGENT_JOB_JSON = os.path.join(APP, "nexaploy", "doctype", "agent_job", "agent_job.json")


def read(path):
	with open(path, encoding="utf-8") as fh:
		return fh.read()


def test_utils_has_no_frappe_import():
	tree = ast.parse(read(os.path.join(APP, "observability_utils.py")))
	for node in ast.walk(tree):
		if isinstance(node, (ast.Import, ast.ImportFrom)):
			names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
			assert not any(n and n.split(".")[0] == "frappe" for n in names)


# --------------------------------------------------------------------------- permissions


def test_required_action():
	assert obs.required_action("site.logs") == "read"
	assert obs.required_action("site.analytics") == "read"
	assert obs.required_action("site.slow_queries") == "read"
	assert obs.required_action("site.sql", True) == "write"
	assert obs.required_action("site.sql", False) == "admin"
	assert obs.required_action("site.console") == "admin"
	with pytest.raises(ValueError):
		obs.required_action("site.drop")


# --------------------------------------------------------------------------- SQL validator


READ_OK = [
	"SELECT 1",
	"select name from `tabUser` where name = 'a;b'; show tables",
	"-- c\nSELECT 1 /* x; y */",
	"WITH x AS (SELECT 1 AS a) SELECT a FROM x",
	"DESCRIBE `tabUser`",
	"EXPLAIN SELECT 1",
	"SELECT 'system ls'",
]
READ_BAD = [
	"",
	"DELETE FROM `tabUser`",
	"SELECT 1; DROP TABLE x",
	"SELECT 1; COMMIT; DELETE FROM x",
	"SELECT * FROM x INTO OUTFILE '/tmp/x'",
	"SELECT * FROM x FOR UPDATE",
	"SELECT 1 /*! ; DELETE FROM x */",
	"SELECT 1\nsystem ls",
	"SELECT 1 \\! ls",
	"SELECT 'unterminated",
	"WITH x AS (SELECT 1) DELETE FROM y",
]


@pytest.mark.parametrize("query", READ_OK)
def test_validate_read_only_accepts(query):
	assert obs.validate_query(query, True)


@pytest.mark.parametrize("query", READ_BAD)
def test_validate_read_only_rejects(query):
	with pytest.raises(obs.QueryError):
		obs.validate_query(query, True)


def test_validate_write_mode():
	assert obs.validate_query("UPDATE `tabUser` SET enabled = 1", False)
	for q in ("delimiter //", "SELECT 1;\n system id", "use mysql", "x" * (obs.MAX_QUERY_CHARS + 1)):
		with pytest.raises(obs.QueryError):
			obs.validate_query(q, False)


def test_validator_mirrors_agent():
	"""Same verdicts as the agent's validator (skipped when the agent package is not importable)."""
	rs = pytest.importorskip("nexaploy_agent.runtime.sql")
	corpus = READ_OK + READ_BAD + ["UPDATE x SET a = 1", "source x", "SELECT 1;" * 60, "show tables; select 2"]
	for ro in (True, False):
		for q in corpus:
			try:
				obs.validate_query(q, ro)
				ours = True
			except obs.QueryError:
				ours = False
			try:
				rs.validate_query(q, read_only=ro)
				theirs = True
			except rs.SqlValidationError:
				theirs = False
			assert ours == theirs, (q, ro)


# --------------------------------------------------------------------------- params


def test_logs_params():
	assert obs.logs_params("a.b.com", "worker", "50") == {"site": "a.b.com", "log": "worker", "lines": 50}
	assert obs.logs_params("a.b.com", None, None)["lines"] == obs.DEFAULT_LINES
	assert obs.logs_params("a.b.com", "web", 10 ** 9)["lines"] == obs.MAX_LINES
	assert obs.logs_params("a.b.com", "web", 1, "2h")["since"] == "2h"
	with pytest.raises(ValueError):
		obs.logs_params("a.b.com", "../etc", 1)
	with pytest.raises(ValueError):
		obs.logs_params("a.b.com", "web", 1, "1h; rm")


def test_analytics_and_slow_params():
	p = obs.analytics_params("a.b.com", "7", date(2026, 11, 3))
	assert p == {"site": "a.b.com", "date_from": "2026-10-28", "date_to": "2026-11-03"}
	assert obs.analytics_params("a.b.com", 400, date(2026, 11, 3))["date_from"] == "2026-10-04"
	assert obs.analytics_params("a.b.com", "x", date(2026, 11, 3))["date_from"] == "2026-10-28"
	assert obs.slow_query_params("a.b.com", 1000, -5) == {"site": "a.b.com", "limit": 200, "min_seconds": 0.0}


def test_sql_and_console_params():
	p = obs.sql_params("a.b.com", "SELECT 1", "1", "100000", "9999")
	assert p == {"site": "a.b.com", "query": "SELECT 1", "read_only": True, "max_rows": obs.MAX_ROWS,
	             "timeout": obs.MAX_SQL_TIMEOUT}
	assert obs.sql_params("a.b.com", "DELETE FROM x", "0")["read_only"] is False
	assert obs.sql_params("a.b.com", "SELECT 1", None)["read_only"] is True  # default read-only
	with pytest.raises(obs.QueryError):
		obs.sql_params("a.b.com", "DELETE FROM x", "true")
	c = obs.console_params("a.b.com", "print(1)", "true", None)
	assert c == {"site": "a.b.com", "code": "print(1)", "commit": True, "timeout": obs.CONSOLE_TIMEOUT}
	assert obs.console_params("a.b.com", "print(1)")["commit"] is False
	for bad in ("", "   ", None, "x" * (obs.MAX_CODE_CHARS + 1), "a\x00b"):
		with pytest.raises(ValueError):
			obs.console_params("a.b.com", bad)


def test_same_request():
	assert obs.same_request({"site": "a", "log": "web"}, {"log": "web", "site": "a"})
	assert not obs.same_request({"site": "a", "log": "web"}, {"site": "a", "log": "worker"})
	assert not obs.same_request({"site": "a"}, {"site": "a", "since": "1h"})


# --------------------------------------------------------------------------- activity / results


def test_activity_details_truncates_text():
	d = obs.activity_details("site.sql", {"site": "a", "query": "SELECT " + "x" * 5000, "read_only": True})
	assert d["read_only"] is True and "site" not in d
	assert len(d["query"]) < obs.ACTIVITY_TEXT_CHARS + 40 and d["query"].endswith("chars]")
	c = obs.activity_details("site.console", {"site": "a", "code": "print(1)", "commit": False})
	assert c == {"code": "print(1)", "commit": False}
	assert obs.activity_details("site.logs", {"site": "a", "log": "web", "lines": 5}) == {"log": "web", "lines": 5}
	assert obs.activity_action("site.sql") == "site.sql"


def test_shape_result():
	pending = obs.shape_result("site.sql", "Queued", None)
	assert pending == {"kind": "site.sql", "status": "Queued", "done": False, "error": None, "data": None}
	ok = obs.shape_result("site.sql", "Success", {"columns": ["a"], "rows": [[1]], "bench": "b", "started": True,
	                                              "started_done": True, "error": None})
	assert ok["done"] and ok["data"] == {"columns": ["a"], "rows": [[1]], "truncated": False, "error": None}
	fail = obs.shape_result("site.console", "Failure", {}, "bench is not running")
	assert fail["error"] == "bench is not running" and fail["data"] is None
	con = obs.shape_result("site.console", "Success", {"output": "3\n", "ok": True})
	assert con["data"]["output"] == "3\n"
	logs = obs.shape_result("site.logs", "Success", {})
	assert logs["data"] == {"sources": []}
	assert obs.shape_result("site.logs", "Success", "garbage")["data"] == {"sources": []}


def test_rows_to_csv_and_chart_bars():
	csv_text = obs.rows_to_csv(["a", "b"], [[1, None], ['x,"y"', "line\nbreak"]])
	assert list(csv.reader(csv_text.splitlines(True))) == [["a", "b"], ["1", ""], ['x,"y"', "line\nbreak"]]
	bars = obs.chart_bars([{"v": 1}, {"v": 4}, {"v": 0}], "v")
	assert [b["pct"] for b in bars] == [25.0, 100.0, 0.0]
	assert obs.chart_bars([], "v") == []


# --------------------------------------------------------------------------- agent job kinds


def test_kinds_registered_in_agent_job():
	tree = ast.parse(read(AGENT_JOB))
	kinds = None
	for node in tree.body:
		if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "JOB_KINDS":
			kinds = ast.literal_eval(node.value)
	assert set(obs.OBS_KINDS) <= set(kinds)
	options = next(f for f in json.loads(read(AGENT_JOB_JSON))["fields"] if f["fieldname"] == "kind")["options"]
	assert set(obs.OBS_KINDS) <= set(options.split("\n"))


# --------------------------------------------------------------------------- API module


def _whitelisted():
	tree = ast.parse(read(API))
	out = {}
	for fn in tree.body:
		if not isinstance(fn, ast.FunctionDef):
			continue
		for dec in fn.decorator_list:
			target = dec.func if isinstance(dec, ast.Call) else dec
			if isinstance(target, ast.Attribute) and target.attr == "whitelist":
				out[fn.name] = (fn, dec)
	return out


def test_api_methods_and_signatures():
	wl = _whitelisted()
	expected = {
		"get_logs": ["site", "log", "lines", "since", "refresh"],
		"get_analytics": ["site", "days", "refresh"],
		"get_slow_queries": ["site", "limit", "min_seconds", "refresh"],
		"run_sql": ["site", "query", "read_only", "max_rows", "timeout"],
		"run_console": ["site", "code", "commit", "timeout"],
		"get_result": ["job"],
		"get_permissions": ["site"],
	}
	for name, args in expected.items():
		fn, _ = wl[name]
		assert [a.arg for a in fn.args.args] == args, name
	# run_* mutate: POST only; nothing allows guests
	for name in ("run_sql", "run_console"):
		dec = wl[name][1]
		assert isinstance(dec, ast.Call) and any(k.arg == "methods" for k in dec.keywords), name
	for _, dec in wl.values():
		assert not (isinstance(dec, ast.Call) and any(k.arg == "allow_guest" for k in dec.keywords))


def test_api_checks_access_and_logs_activity():
	src = read(API)
	assert "check_team_access" in src and "from nexaploy.activity import log_activity" in src
	assert "except ImportError" in src
	# each entry point resolves the site through the capability check of its job kind
	kinds = {"get_logs": "site.logs", "get_analytics": "site.analytics", "get_slow_queries": "site.slow_queries",
	         "run_sql": "site.sql", "run_console": "site.console"}
	for name, kind in kinds.items():
		body = src[src.index(f"def {name}("):]
		body = body[: body.index("\n\n\n")] if "\n\n\n" in body else body
		assert f'_site(site, "{kind}"' in body, name
		assert "_enqueue(" in body, name
	assert "check_capability(row.team, kind" in src
	assert "capability_allows(get_capabilities(team), row.kind" in src  # get_result re-checks per kind
	assert "start_audit(" in src  # every execution is audited


def test_js_calls_known_methods():
	js = read(JS)
	used = set(re.findall(r'call\("(\w+)"', js)) | set(re.findall(r'_run\(this\.\w+, "(\w+)"', js))
	assert used and used <= set(_whitelisted()), used - set(_whitelisted())


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_js_syntax():
	subprocess.run(["node", "--check", JS], check=True, capture_output=True)


# --------------------------------------------------------------------------- portal page


def _render(lang):
	env = jinja2.Environment(loader=jinja2.FileSystemLoader(APP), undefined=jinja2.StrictUndefined)
	env.globals["_"] = lambda s, *a: s
	return env.get_template("www/dashboard/insights.html").render(
		title="Insights", page="site", lang=lang, dir="rtl" if lang == "ar" else "ltr", csrf_token="tok",
		boot={"user": "a@b.c", "messages": {}}, user="a@b.c", user_fullname="A", is_admin=False,
		team="acme", team_role="Developer", teams=[{"name": "acme", "title": "Acme", "role": "Developer"}],
	)


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_page_renders(lang):
	html = _render(lang)
	assert html.lstrip().startswith("<!doctype html>")
	assert '<script src="/assets/nexaploy/js/insights.js"></script>' in html
	assert 'x-data="nxInsights"' in html
	assert html.count("<template") == html.count("</template>")
	assert "{{" not in html and "{%" not in html
	for tab in ("analytics", "logs", "slow", "sql", "console"):
		assert f"tab === '{tab}'" in html
	# SQL write toggle and console are role-gated
	assert 'x-show="perms.sql_write"' in html and "perms.console" in html


def test_page_controller():
	src = read(PAGE_PY)
	assert "no_cache = 1" in src and "prepare_page(" in src and "def get_context" in src


def test_component_registered():
	assert 'Alpine.data("nxInsights"' in read(JS)


def _part_rows():
	with open(PART, encoding="utf-8", newline="") as fh:
		return [r for r in csv.reader(fh) if r]


def test_translations_cover_page_and_js():
	ar = set()
	for path in [os.path.join(APP, "translations", "ar.csv")] + sorted(glob.glob(os.path.join(APP, "translations", "ar.*.part"))):
		with open(path, encoding="utf-8", newline="") as fh:
			ar |= {r[0] for r in csv.reader(fh) if r}
	string_re = re.compile(r"""(?<![A-Za-z0-9.])_{1,2}\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')\s*[,)]""")
	strings = set()
	for path in (PAGE, PAGE_PY, API):
		strings |= {m.group(1) or m.group(2) for m in string_re.finditer(read(path))}
	strings |= set(re.findall(r'\bt\(\s*"((?:[^"\\]|\\.)*)"\s*[,)]', read(JS)))
	strings |= set(re.findall(r'raise (?:obs\.)?QueryError\(\s*"([^"{]+)"', read(os.path.join(APP, "observability_utils.py"))))
	missing = sorted(strings - ar)
	assert not missing, missing


def test_part_well_formed():
	rows = _part_rows()
	assert rows
	sources = [r[0] for r in rows]
	assert len(sources) == len(set(sources))
	for row in rows:
		assert len(row) == 2 and row[0].strip() and row[1].strip(), row
		assert sorted(re.findall(r"\{\d\}", row[0])) == sorted(re.findall(r"\{\d\}", row[1])), row
