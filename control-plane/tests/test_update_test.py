"""Safe update (Site Update Test): pure logic in nexaploy.update_test_utils + static checks of the
frappe-side modules, portal page and wiring (no Frappe install needed)."""

import ast
import os
import re
import shutil
import subprocess

import jinja2
import pytest

from nexaploy import update_test_utils as ut

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "nexaploy")
API = os.path.join(APP, "api", "update_test.py")
CORE = os.path.join(APP, "update_test.py")
JS = os.path.join(APP, "public", "js", "safe_update.js")
PAGE = os.path.join(APP, "www", "dashboard", "safe_update.html")


def read(path):
	with open(path, encoding="utf-8") as fh:
		return fh.read()


# --------------------------------------------------------------------------- naming & targets


def test_copy_site_name():
	assert ut.copy_site_name("erp.example.com") == "uptest-erp.example.com"
	assert ut.copy_site_name(" ERP.Example.com ") == "uptest-erp.example.com"
	with pytest.raises(ValueError):
		ut.copy_site_name("not a site")
	with pytest.raises(ValueError):
		ut.copy_site_name("a" * 60 + ".example.com")


def test_new_bench_name():
	name = ut.new_bench_name("erp-v15", ["erp-v15-abc12"], "abc12")
	assert re.match(r"^[a-z][a-z0-9-]{2,30}$", name) and name != "erp-v15-abc12"
	assert ut.new_bench_name("erp-v15", [], "x1") == "erp-v15-x1"


BENCHES = [
	{"name": "erp-new", "status": "Active", "server": "s1", "release_group": "erp", "image_tag": "img:2"},
	{"name": "erp-old", "status": "Active", "server": "s1", "release_group": "erp", "image_tag": "img:1"},
	{"name": "erp-other-srv", "status": "Active", "server": "s2", "release_group": "erp", "image_tag": "img:2"},
	{"name": "erp-broken", "status": "Broken", "server": "s1", "release_group": "erp", "image_tag": "img:2"},
]


def test_pick_target_bench():
	assert ut.pick_target_bench(BENCHES, "s1", "erp", "img:2", exclude="erp-old") == "erp-new"
	assert ut.pick_target_bench(BENCHES, "s2", "erp", "img:2") == "erp-other-srv"
	assert ut.pick_target_bench(BENCHES, "s1", "erp", "img:3") is None
	assert ut.pick_target_bench(BENCHES, "s1", "erp", "img:2", exclude="erp-new") is None  # broken skipped
	assert ut.pick_target_bench(BENCHES, "s1", "crm", "img:2") is None


def test_update_available():
	assert ut.update_available("img:1", "img:2")
	assert not ut.update_available("img:2", "img:2")
	assert not ut.update_available("img:1", None)


def test_validate_target():
	ut.validate_target("s1", "erp-old", BENCHES[0])
	with pytest.raises(ValueError, match="not found"):
		ut.validate_target("s1", "erp-old", None)
	with pytest.raises(ValueError, match="already runs"):
		ut.validate_target("s1", "erp-new", BENCHES[0])
	with pytest.raises(ValueError, match="not active"):
		ut.validate_target("s1", "erp-old", BENCHES[3])
	with pytest.raises(ValueError, match="same server"):
		ut.validate_target("s1", "erp-old", BENCHES[2])


# --------------------------------------------------------------------------- job params & results


def test_clone_and_smoke_params():
	assert ut.clone_job_params("erp-old", "erp.example.com", "uptest-erp.example.com", "erp-new") == {
		"bench": "erp-old", "site": "erp.example.com", "target_site": "uptest-erp.example.com", "scrub": True,
		"target_bench": "erp-new",
	}
	assert "target_bench" not in ut.clone_job_params("b", "a.example.com", "uptest-a.example.com", "b")
	p = ut.smoke_params("uptest-erp.example.com", ["erpnext", "frappe", "", "erpnext"])
	assert p["paths"] == ["/api/method/ping", "/login"] and p["apps"] == ["erpnext", "frappe"]
	assert p["site"] == "uptest-erp.example.com" and p["timeout"] == ut.SMOKE_TIMEOUT


def _steps(fail_at, output=""):
	names = ["check", "backup", "stage", "new_site", "restore", "restore_config", "scrub", "migrate", "write_route"]
	out = []
	for i, n in enumerate(names):
		status = "Success" if i < names.index(fail_at) else ("Failure" if n == fail_at else "Pending")
		out.append({"step_idx": i, "step_name": n, "status": status, "output": output if n == fail_at else "ok"})
	return out


def test_clone_failure_reports_migrate_error():
	f = ut.clone_failure(_steps("migrate", "Traceback ...\nfrappe.exceptions.ValidationError: bad patch"), "bench migrate failed")
	assert f["stage"] == "migrate" and f["message"] == "Migration failed on the new version"
	assert "bad patch" in f["detail"]
	f = ut.clone_failure(_steps("restore"), "bench restore failed: x")
	assert f["stage"] == "restore" and "Restoring" in f["message"] and "bench restore failed" in f["detail"]
	assert ut.clone_failure(_steps("backup"), "e")["message"].startswith("Backing up")
	assert ut.clone_failure([], "boom") == {"stage": "clone", "message": "Creating the test copy failed", "detail": "boom"}
	assert ut.failed_step([{"idx": 1, "name": "b", "status": "failure"}, {"idx": 0, "name": "a", "status": "success"}]) == "b"


def test_smoke_outcome():
	ok = ut.smoke_outcome({"ok": True, "checks": [{"name": "http /login", "ok": True, "detail": "HTTP 200"}], "summary": "1 check(s) passed"})
	assert ok["ok"] and ok["summary"] == "1 check(s) passed"
	bad = ut.smoke_outcome({"ok": False, "checks": [{"name": "database", "ok": False, "detail": "x"}], "summary": "database: x"})
	assert not bad["ok"] and bad["checks"][0]["name"] == "database"
	assert not ut.smoke_outcome({"ok": True, "checks": []})["ok"]  # no checks = no pass
	none = ut.smoke_outcome(None, "bench erp is not running")
	assert not none["ok"] and none["checks"][0]["detail"] == "bench erp is not running"


def test_logs_are_capped_and_keep_the_tail():
	log = ut.format_log([("clone", "a" * (ut.MAX_SECTION_CHARS + 10) + "END")])
	assert log.startswith("== clone ==") and log.endswith("END") and len(log) < ut.MAX_SECTION_CHARS + 100
	log = ut.append_log(None, "one", "1")
	log = ut.append_log(log, "two", None)
	assert log == "== one ==\n1\n== two ==\n(no output)"
	assert len(ut.append_log("x" * ut.MAX_LOG_CHARS, "y", "z")) <= ut.MAX_LOG_CHARS + 1


# --------------------------------------------------------------------------- apply


def test_apply_block_reason():
	site = {"status": "Active", "bench": "erp-old"}
	assert ut.apply_block_reason("Passed", site, "erp-old", "Active") is None
	assert ut.apply_block_reason("Failed", site, "erp-old", "Active") == "Only a passed test can be applied"
	assert ut.apply_block_reason("Passed", {**site, "status": "Suspended"}, "erp-old", "Active")
	assert "moved" in ut.apply_block_reason("Passed", {**site, "bench": "x"}, "erp-old", "Active")
	assert "no longer active" in ut.apply_block_reason("Passed", site, "erp-old", "Broken")


def test_apply_outcome():
	assert ut.apply_outcome({"status": "Moving", "bench": "erp-old"}, "erp-old", "erp-new") is None
	assert ut.apply_outcome({"status": "Active", "bench": "erp-new"}, "erp-old", "erp-new") == "Applied"
	assert ut.apply_outcome({"status": "Broken", "bench": "erp-old", "move_steps": "[]"}, "erp-old", "erp-new") == "Apply Failed"
	# aborted before the source was dropped: back to Active on the old bench, move state cleared
	assert ut.apply_outcome({"status": "Active", "bench": "erp-old", "move_steps": None}, "erp-old", "erp-new") == "Apply Failed"


def test_auto_gate():
	sites = ["a", "b", "c"]
	assert ut.auto_gate(sites, {"a": "Passed", "b": "Passed", "c": "Applied"})["action"] == "apply"
	d = ut.auto_gate(sites, {"a": "Passed", "b": None, "c": "Cancelled"})
	assert d["action"] == "wait" and d["start"] == ["b", "c"]
	assert ut.auto_gate(sites, {"a": "Testing", "b": "Passed", "c": "Passed"})["action"] == "wait"
	d = ut.auto_gate(sites, {"a": "Failed", "b": None, "c": "Passed"})
	assert d["action"] == "blocked" and d["failed"] == ["a"] and d["start"] == ["b"]
	assert ut.auto_gate([], {})["action"] == "apply"


def test_status_sets():
	assert set(ut.IN_PROGRESS) | set(ut.FINAL) | {"Passed"} == set(ut.STATUSES)
	assert "Applying" not in ut.CANCELLABLE and "Applied" not in ut.CANCELLABLE


def test_no_frappe_import():
	assert "import frappe" not in read(os.path.join(APP, "update_test_utils.py"))


# --------------------------------------------------------------------------- wiring (static)


def _whitelisted(path):
	tree = ast.parse(read(path))
	out = {}
	for fn in tree.body:
		if isinstance(fn, ast.FunctionDef):
			for dec in fn.decorator_list:
				target = dec.func if isinstance(dec, ast.Call) else dec
				if isinstance(target, ast.Attribute) and target.attr == "whitelist":
					out[fn.name] = (fn, dec)
	return out


def test_api_checks_team_access():
	wl = _whitelisted(API)
	assert {"get_overview", "get_test", "start_test", "apply_update", "delete_test_site", "cancel_test",
	        "set_test_before_auto_update"} <= set(wl)
	src = read(API)
	for name, (fn, dec) in wl.items():
		body = ast.get_source_segment(src, fn)
		assert re.search(r"_site\(|_test\(|_check\(", body), name
		assert not (isinstance(dec, ast.Call) and any(k.arg == "allow_guest" for k in dec.keywords)), name
		if name not in ("get_overview", "get_test"):
			assert isinstance(dec, ast.Call) and any(k.arg == "methods" for k in dec.keywords), name
	for name in ("start_test", "apply_update", "delete_test_site", "cancel_test"):
		assert '"write")' in ast.get_source_segment(src, wl[name][0]), name


def test_chain_wiring():
	core = read(CORE)
	callbacks = read(os.path.join(APP, "agent_callbacks.py"))
	assert '@handles("site.smoke_check")' in callbacks
	assert callbacks.count("_update_tests(job)") >= 4  # bench jobs, drop, clone, smoke
	for kind in ("bench.create", "site.clone", "site.smoke_check", "bench.update", "site.drop"):
		assert f'"{kind}"' in core, kind
	assert "move_to_bench(doc.target_bench, allow_upgrade=True)" in core  # apply = move (fresh backup first)
	assert "nexaploy.update_test.sync_update_tests" in read(os.path.join(APP, "hooks.py"))
	assert "auto_update_gate(release_group, name, dc_name)" in read(os.path.join(APP, "ops.py"))
	site = read(os.path.join(APP, "nexaploy", "doctype", "site", "site.py"))
	assert 'params["target_bench"]' in site
	assert '"site.smoke_check"' in read(os.path.join(APP, "agent_job.py"))


def test_js_calls_known_methods():
	used = set(re.findall(r'(?:call|_mutate)\("(\w+)"', read(JS)))
	assert used and used <= set(_whitelisted(API)), used - set(_whitelisted(API))


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_js_syntax():
	subprocess.run(["node", "--check", JS], check=True, capture_output=True)


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_page_renders(lang):
	env = jinja2.Environment(loader=jinja2.FileSystemLoader(APP), undefined=jinja2.StrictUndefined)
	env.globals["_"] = lambda s, *a: s
	html = env.get_template("www/dashboard/safe_update.html").render(
		title="Safe update", page="site", lang=lang, dir="rtl" if lang == "ar" else "ltr", csrf_token="tok",
		boot={"user": "a@b.c", "messages": {}}, user="a@b.c", user_fullname="A", is_admin=False,
		team="acme", team_role="Developer", teams=[{"name": "acme", "title": "Acme", "role": "Developer"}],
	)
	assert '<script src="/assets/nexaploy/js/safe_update.js"></script>' in html
	assert 'x-data="nxSafeUpdate"' in html and "Alpine.data(\"nxSafeUpdate\"" in read(JS)
	assert html.count("<template") == html.count("</template>")
	assert "/dashboard/safe_update?site=" in read(os.path.join(APP, "www", "dashboard", "site.html"))
