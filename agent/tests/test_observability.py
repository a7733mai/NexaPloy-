"""Observability jobs: site.logs, site.analytics, site.slow_queries, site.sql, site.console."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from nexaploy_agent.api import create_app
from nexaploy_agent.jobs.registry import REGISTRY
from nexaploy_agent.runtime import console as rc
from nexaploy_agent.runtime import logs as rl
from nexaploy_agent.runtime import sql as rs
from nexaploy_agent.runtime.docker import Call, Result

from .conftest import TOKEN, bench_running, make_bench, make_site

SITE = "a.example.com"
SITE_PW = "site-db-pw-SECRET"
SITE_CFG = {"db_name": "_a1b2c3", "db_password": SITE_PW, "monitor": 1}


def run_job(ctx, worker, kind, params):
    job = ctx.enqueue(kind, params, precheck=True)
    assert worker.run_once()
    return ctx.store.get(job["id"])


def setup_site(ctx, second_site: bool = False):
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", SITE, extra=SITE_CFG)
    if second_site:
        make_site(ctx, "erp-v15", "b.example.com", extra={"db_name": "_b", "db_password": "other-pw"})
    return ctx.benches.path("erp-v15")


def test_kinds_registered():
    for kind in ("site.logs", "site.analytics", "site.slow_queries", "site.sql", "site.console"):
        assert kind in REGISTRY


# =========================================================================== SQL validator
@pytest.mark.parametrize("query", [
    "SELECT 1",
    "select name from `tabUser` where name = 'a;b'; show tables",
    "  -- comment\nSELECT 1 /* x; y */",
    "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
    "DESCRIBE `tabUser`",
    "desc `tabUser`",
    "EXPLAIN SELECT * FROM `tabUser`",
    "SHOW FULL PROCESSLIST",
    "(SELECT 1) UNION (SELECT 2)",
    "SELECT 'it''s \\'quoted\\'', \"dq\"",
    "SELECT 'system ls'",
    "SELECT\n  name\nFROM `tabUser`",
])
def test_validate_read_only_accepts(query):
    assert rs.validate_query(query, read_only=True)


@pytest.mark.parametrize("query,needle", [
    ("", "empty"),
    ("   ;  ; ", "empty"),
    ("DELETE FROM `tabUser`", "read-only"),
    ("SELECT 1; DROP TABLE `tabUser`", "DROP"),
    ("UPDATE `tabUser` SET enabled = 0", "UPDATE"),
    ("SELECT 1; COMMIT; DELETE FROM x", "COMMIT"),
    ("SET GLOBAL general_log = 1", "SET"),
    ("SELECT * FROM `tabUser` INTO OUTFILE '/tmp/x'", "OUTFILE"),
    ("SELECT * FROM `tabUser` FOR UPDATE", "locking"),
    ("SELECT 1 /*! ; DELETE FROM x */", "executable"),
    ("SELECT 1\nsystem ls", "client command"),
    ("source /etc/passwd", "client command"),
    ("SELECT 1 \\! ls", "backslash"),
    ("SELECT 'unterminated", "unterminated"),
    ("WITH x AS (SELECT 1) DELETE FROM y", "WITH"),
    ("CALL do_things()", "read-only"),
])
def test_validate_read_only_rejects(query, needle):
    with pytest.raises(rs.SqlValidationError) as exc:
        rs.validate_query(query, read_only=True)
    assert needle.lower() in str(exc.value).lower()


def test_validate_write_mode():
    assert rs.validate_query("UPDATE `tabUser` SET enabled = 1 WHERE name = 'x'", read_only=False)
    assert len(rs.validate_query("DELETE FROM a; INSERT INTO b VALUES (1)", read_only=False)) == 2
    # client commands are refused in every mode
    for q in ("delimiter //", "SELECT 1;\n  system id", "\\! id", "use mysql", "tee /tmp/x"):
        with pytest.raises(rs.SqlValidationError):
            rs.validate_query(q, read_only=False)
    with pytest.raises(rs.SqlValidationError):
        rs.validate_query("x" * (rs.MAX_QUERY_CHARS + 1), read_only=False)
    with pytest.raises(rs.SqlValidationError):
        rs.validate_query("SELECT 1;" * (rs.MAX_STATEMENTS + 1))


def test_split_statements():
    assert rs.split_statements("SELECT ';'; -- c;\nSELECT `a;b` # x;\n;") == ["SELECT ';'", "SELECT `a;b`"]


def test_build_script_read_only_wrapper():
    script = rs.build_script(["SELECT 1", "SHOW TABLES"], read_only=True, timeout=30, max_rows=100)
    lines = script.strip().splitlines()
    assert lines[0] == "SET SESSION max_statement_time = 30;"
    assert lines[1] == "SET SESSION sql_select_limit = 101;"
    assert lines[2:4] == ["SET SESSION TRANSACTION READ ONLY;", "START TRANSACTION;"]
    assert lines[4:6] == ["SELECT 1;", "SHOW TABLES;"]
    assert lines[-1] == "ROLLBACK;"
    write = rs.build_script(["UPDATE x SET a = 1"], read_only=False, timeout=5, max_rows=10)
    assert "READ ONLY" not in write and "ROLLBACK" not in write


def test_db_credentials():
    assert rs.db_credentials({"db_name": "_abc", "db_password": "p"}) == ("_abc", "p", "_abc")
    assert rs.db_credentials({"db_name": "_abc", "db_password": "p", "db_user": "u_1"})[0] == "u_1"
    for bad in ({}, {"db_name": "x; drop", "db_password": "p"}, {"db_name": "_a"}):
        with pytest.raises(rs.SqlValidationError):
            rs.db_credentials(bad)


XML_OUT = """<?xml version="1.0"?>

<resultset statement="SELECT name, full_name, last_login FROM `tabUser`
" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <row>
\t<field name="name">Administrator</field>
\t<field name="full_name">Admin &lt;root&gt;</field>
\t<field name="last_login" xsi:nil="true" />
  </row>
  <row>
\t<field name="name">a@b.c</field>
\t<field name="full_name">multi
line\ttab</field>
\t<field name="last_login">2026-11-03 10:00:00</field>
  </row>
  <row>
\t<field name="name">c@d.e</field>
\t<field name="full_name"></field>
\t<field name="last_login">2026-11-02 10:00:00</field>
  </row>
</resultset>
<?xml version="1.0"?>

<resultset statement="SELECT COUNT(*) AS n FROM `tabUser`" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <row>
\t<field name="n">3</field>
  </row>
</resultset>
<?xml version="1.0"?>

<resultset statement="SELECT 1 FROM dual WHERE 0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
</resultset>
"""


def test_parse_xml_output():
    sets = rs.parse_xml_output(XML_OUT, max_rows=2)
    assert len(sets) == 3
    first = sets[0]
    assert first["columns"] == ["name", "full_name", "last_login"]
    assert first["rows"] == [["Administrator", "Admin <root>", None], ["a@b.c", "multi\nline\ttab", "2026-11-03 10:00:00"]]
    assert first["truncated"] is True and first["row_count"] == 3
    assert sets[1]["rows"] == [["3"]] and sets[1]["columns"] == ["n"]
    assert sets[2]["rows"] == [] and sets[2]["columns"] == []
    long = rs.parse_xml_output(XML_OUT, max_cell_chars=3)
    assert long[0]["rows"][0][0] == "Adm" and long[0]["cells_truncated"]


def test_shape_result_uses_select_limit_extra_row():
    sets = rs.parse_xml_output(XML_OUT, max_rows=3)
    shaped = rs.shape_result(sets[:1], ["SELECT ..."], True, max_rows=2)
    assert shaped["row_count"] == 2 and shaped["truncated"] is True
    assert shaped["columns"] == ["name", "full_name", "last_login"]


def test_run_query_password_only_in_env(runner):
    runner.on("mariadb", stdout=XML_OUT)
    out = rs.run_query(runner, SITE_CFG, "SELECT 1", read_only=True, max_rows=10, timeout=20)
    assert out["error"] is None and out["result_sets"][0]["columns"] == ["name", "full_name", "last_login"]
    assert out["columns"] == []  # top level = last result set (empty here)
    call = runner.find("mariadb")[0]
    assert SITE_PW not in call.cmdline and "MYSQL_PWD" in call.args
    assert call.env == {"MYSQL_PWD": SITE_PW}
    assert call.args[:7] == ["docker", "exec", "-i", "-e", "MYSQL_PWD", "nexaploy-db", "mariadb"]
    assert "--sandbox" in call.args and "--xml" in call.args
    assert call.args[call.args.index("--user") + 1] == "_a1b2c3"
    assert call.args[call.args.index("--database") + 1] == "_a1b2c3"
    assert "root" not in call.args
    assert "SELECT 1;" in call.input and "START TRANSACTION;" in call.input
    assert call.timeout == 50


def test_run_query_error_is_masked(runner):
    runner.on("mariadb", returncode=1, stderr=f"ERROR 1064 (42000) at line 5: bad {SITE_PW}\n")
    out = rs.run_query(runner, SITE_CFG, "SELECT 1")
    assert out["error"].startswith("ERROR 1064") and SITE_PW not in out["error"]


# =========================================================================== site.sql job
def test_site_sql_job(ctx, worker, runner):
    setup_site(ctx)
    runner.on("mariadb", stdout=XML_OUT)
    job = run_job(ctx, worker, "site.sql", {"site": SITE, "query": "SELECT name FROM `tabUser`", "max_rows": 2})
    assert job["status"] == "success", job
    r = job["result"]
    assert r["columns"] == [] and r["result_sets"][1]["columns"] == ["n"]  # last result set wins
    assert r["read_only"] is True and r["statements"] == 1 and r["error"] is None
    assert len(r["result_sets"]) == 3
    dumped = json.dumps(job)
    assert SITE_PW not in dumped
    # bulk data is not duplicated into step output
    assert "Administrator" not in job["steps"][0]["output"]


def test_site_sql_rejects_writes_in_read_only(ctx):
    setup_site(ctx)
    with pytest.raises(ValidationError):
        ctx.enqueue("site.sql", {"site": SITE, "query": "DELETE FROM `tabUser`"})
    job = ctx.enqueue("site.sql", {"site": SITE, "query": "DELETE FROM `tabUser`", "read_only": False})
    assert job["params"]["read_only"] is False


def test_site_sql_error_returned_in_result(ctx, worker, runner):
    setup_site(ctx)
    runner.on("mariadb", returncode=1, stderr="ERROR 1146 (42S02) at line 5: Table '_a1b2c3.nope' doesn't exist\n")
    job = run_job(ctx, worker, "site.sql", {"site": SITE, "query": "SELECT * FROM nope"})
    assert job["status"] == "success"
    assert "doesn't exist" in job["result"]["error"]


def test_site_sql_write_not_rerun_after_crash(ctx, worker, runner):
    setup_site(ctx)
    job = ctx.enqueue("site.sql", {"site": SITE, "query": "UPDATE x SET a = 1", "read_only": False})
    ctx.store.set_result(job["id"], {"started": True})  # crashed mid-query
    assert worker.run_once()
    job = ctx.store.get(job["id"])
    assert job["status"] == "failure" and "not re-run" in job["error"]
    assert not runner.called("mariadb")


# =========================================================================== site.logs
def test_tail_and_filter(tmp_path):
    p = tmp_path / "x.log"
    p.write_text("".join(f"line {i}\n" for i in range(100)))
    assert rl.tail_file(p, 3) == ["line 97", "line 98", "line 99"]
    assert rl.tail_file(p, 3, max_bytes=20)[-1] == "line 99"
    assert rl.tail_file(tmp_path / "missing.log", 3) == []
    lines = ["a.example.com GET /", "b.example.com GET /", "x a.example.com", "y"]
    assert rl.filter_site_lines(lines, "a.example.com", 1) == ["x a.example.com"]
    kept, cut, used = rl.cap_text(["aaaa", "bbbb", "cccc"], 10)
    assert kept == ["bbbb", "cccc"] and cut and used == 10
    assert rl.valid_since("30m") == "30m" and rl.valid_since("2026-11-03T10:00Z")
    with pytest.raises(ValueError):
        rl.valid_since("$(id)")


def test_site_logs_single_site_bench(ctx, worker, runner):
    bench_dir = setup_site(ctx)
    (bench_dir / "logs").mkdir(exist_ok=True)
    (bench_dir / "logs" / "web.log").write_text("".join(f"web {i}\n" for i in range(50)))
    runner.on(["logs", "--no-color"], stdout="backend-1  | 2026-11-03T10:00:00Z GET /api ok\n")
    job = run_job(ctx, worker, "site.logs", {"site": SITE, "log": "web", "lines": 10})
    assert job["status"] == "success", job
    srcs = job["result"]["sources"]
    assert srcs[0]["source"] == "logs/web.log" and srcs[0]["lines"][-1] == "web 49" and len(srcs[0]["lines"]) == 10
    assert srcs[0]["filtered"] is False
    assert srcs[1]["source"] == "backend" and srcs[1]["kind"] == "container"
    call = runner.find("logs --no-color")[0]
    assert call.args[-1] == "backend" and "--tail" in call.args and call.args[call.args.index("--tail") + 1] == "10"
    assert call.args[:4] == ["docker", "compose", "-p", "erp-v15"]


def test_site_logs_shared_bench_is_filtered(ctx, worker, runner):
    bench_dir = setup_site(ctx, second_site=True)
    (bench_dir / "logs").mkdir(exist_ok=True)
    (bench_dir / "logs" / "worker.log").write_text(
        "job for a.example.com done\nsecret job for b.example.com\nanother a.example.com\n")
    runner.on(["logs", "--no-color"], stdout="queue | b.example.com private\nqueue | a.example.com ok\n")
    job = run_job(ctx, worker, "site.logs", {"site": SITE, "log": "worker", "lines": 100, "since": "1h"})
    assert job["status"] == "success", job
    text = json.dumps(job["result"])
    assert "b.example.com" not in text
    assert job["result"]["sources"][0]["lines"] == ["job for a.example.com done", "another a.example.com"]
    assert job["result"]["shared_bench"] is True
    services = [c.args[-1] for c in runner.find("logs --no-color")]
    assert services == ["queue-short", "queue-default", "queue-long"]
    assert "--since" in runner.find("logs --no-color")[0].args


def test_site_logs_site_level_files(ctx, worker, runner):
    setup_site(ctx, second_site=True)
    site_logs = ctx.sites.site_dir("erp-v15", SITE) / "logs"
    site_logs.mkdir()
    (site_logs / "frappe.log").write_text("no site name here\n")
    job = run_job(ctx, worker, "site.logs", {"site": SITE, "log": "frappe"})
    src = job["result"]["sources"][0]
    assert src["source"] == "sites/<site>/logs/frappe.log" and src["lines"] == ["no site name here"]
    assert src["filtered"] is False


def test_site_logs_error_log(ctx, worker, runner):
    setup_site(ctx)
    xml = """<?xml version="1.0"?>
<resultset statement="SELECT ..." xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <row><field name="creation">2026-11-03 10:00:00</field><field name="method">frappe.x</field><field name="error">Traceback ...</field></row>
</resultset>
"""
    runner.on("mariadb", stdout=xml)
    job = run_job(ctx, worker, "site.logs", {"site": SITE, "log": "error", "lines": 1000})
    assert job["status"] == "success", job
    assert job["result"]["rows"] == [{"creation": "2026-11-03 10:00:00", "method": "frappe.x", "error": "Traceback ..."}]
    call = runner.find("mariadb")[0]
    assert "`tabError Log`" in call.input and "LIMIT 200" in call.input and "READ ONLY" in call.input
    assert SITE_PW not in call.cmdline


def test_site_logs_params():
    cls = REGISTRY["site.logs"]
    with pytest.raises(ValidationError):
        cls.validate_params({"site": SITE, "log": "/etc/passwd"})
    with pytest.raises(ValidationError):
        cls.validate_params({"site": SITE, "lines": 10 ** 6})
    with pytest.raises(ValidationError):
        cls.validate_params({"site": SITE, "since": "1h; rm"})


# =========================================================================== analytics
def mline(site, ts, duration, ttype="request", path="/api/method/ping", status=200, method=None):
    data = {"site": site, "timestamp": ts, "transaction_type": ttype, "duration": duration, "uuid": "u"}
    if ttype == "request":
        data["request"] = {"ip": "1.2.3.4", "method": "GET", "path": path, "status_code": status}
    else:
        data["job"] = {"method": method or "frappe.jobs.x", "scheduled": False, "wait": 10}
    return json.dumps(data)


def test_aggregate_monitor():
    lines = [
        mline(SITE, "2026-11-03 10:05:00.1", 100_000, path="/app/sales-invoice?x=1"),
        mline(SITE, "2026-11-03 10:15:00", 300_000, path="/app/sales-invoice"),
        mline(SITE, "2026-11-03 11:00:00", 50_000, path="/api/method/ping", status=500),
        mline(SITE, "2026-11-03 11:30:00", 2_000_000, ttype="job", method="erpnext.stock.repost"),
        mline("b.example.com", "2026-11-03 11:30:00", 999_000_000),
        mline(SITE, "2026-11-01 11:30:00", 999_000_000),  # out of range
        "not json",
        mline(SITE, "garbage", 1),
    ]
    r = rl.aggregate_monitor(lines, SITE, date(2026, 11, 2), date(2026, 11, 3))
    assert r["transactions"] == 4
    assert r["requests"]["count"] == 3 and r["jobs"]["count"] == 1
    assert r["requests"]["p50_ms"] == 100.0 and r["requests"]["p95_ms"] == 300.0
    assert r["paths_by_count"][0] == {"path": "/app/sales-invoice", "count": 2, "total_ms": 400.0,
                                      "avg_ms": 200.0, "max_ms": 300.0}
    assert r["paths_by_duration"][0]["path"] == "/app/sales-invoice"
    assert r["jobs_by_method"][0]["method"] == "erpnext.stock.repost" and r["jobs_by_method"][0]["total_ms"] == 2000.0
    assert r["status_codes"] == [{"status": "200", "count": 2}, {"status": "500", "count": 1}]
    assert [h["hour"] for h in r["hourly"]] == ["2026-11-03T10:00Z", "2026-11-03T11:00Z"]
    assert r["hourly"][1] == {"hour": "2026-11-03T11:00Z", "requests": 1, "jobs": 1, "errors": 1, "duration_ms": 2050.0}
    assert r["daily"] == [{"date": "2026-11-03", "requests": 3, "jobs": 1, "errors": 1, "duration_ms": 2450.0}]


def test_percentile_and_range():
    assert rl.percentile([], 50) is None
    assert rl.percentile([1, 2, 3, 4], 50) == 2 and rl.percentile(list(range(1, 101)), 95) == 95
    today = date(2026, 11, 3)
    assert rl.date_range(None, None, today) == (date(2026, 10, 28), today)
    with pytest.raises(ValueError):
        rl.date_range("2026-01-01", "2026-11-03", today)
    with pytest.raises(ValueError):
        rl.date_range("2026-11-04", "2026-11-03", today)


def test_site_analytics_job(ctx, worker, runner):
    bench_dir = setup_site(ctx)
    now = datetime.now(timezone.utc)
    (bench_dir / "logs").mkdir(exist_ok=True)
    (bench_dir / "logs" / "monitor.json.log.1").write_text(mline(SITE, str(now - timedelta(hours=1)), 1000) + "\n")
    (bench_dir / "logs" / "monitor.json.log").write_text(
        mline(SITE, str(now), 3000) + "\n" + mline("b.example.com", str(now), 5) + "\n")
    job = run_job(ctx, worker, "site.analytics", {"site": SITE})
    assert job["status"] == "success", job
    r = job["result"]
    assert r["requests"]["count"] == 2 and r["monitor_enabled"] is True
    assert r["sources"] == ["monitor.json.log.1", "monitor.json.log"]
    with pytest.raises(ValidationError):
        ctx.enqueue("site.analytics", {"site": SITE, "date_from": "2020-01-01"})
    with pytest.raises(ValidationError):
        ctx.enqueue("site.analytics", {"site": SITE, "date_from": "yesterday"})


# =========================================================================== slow queries
SLOW_LOG = """/usr/sbin/mariadbd, Version: 10.6.20-MariaDB-ubu2004-log (mariadb.org binary distribution). started with:
Tcp port: 3306  Unix socket: /run/mysqld/mysqld.sock
Time		    Id Command	Argument
# Time: 261103 10:00:01
# User@Host: _a1b2c3[_a1b2c3] @  [172.18.0.5]
# Thread_id: 42  Schema: _a1b2c3  QC_hit: No
# Query_time: 2.500000  Lock_time: 0.000100  Rows_sent: 10  Rows_examined: 500000
# Rows_affected: 0  Bytes_sent: 1234
use _a1b2c3;
SET timestamp=1762164001;
SELECT name, grand_total FROM `tabSales Invoice` WHERE customer = 'ACME Corp' AND docstatus = 1 LIMIT 10;
# User@Host: _a1b2c3[_a1b2c3] @  [172.18.0.5]
# Thread_id: 42  Schema: _a1b2c3  QC_hit: No
# Query_time: 1.500000  Lock_time: 0.000100  Rows_sent: 3  Rows_examined: 300000
# Rows_affected: 0  Bytes_sent: 400
SET timestamp=1762164005;
SELECT name, grand_total
  FROM `tabSales Invoice`
  WHERE customer = 'Other, Inc.' AND docstatus = 2 LIMIT 20;
# Time: 261103 10:05:00
# User@Host: _b[_b] @  [172.18.0.6]
# Thread_id: 43  Schema: _b  QC_hit: No
# Query_time: 9.000000  Lock_time: 0.000000  Rows_sent: 1  Rows_examined: 9
SET timestamp=1762164300;
SELECT secret FROM `tabOther` WHERE id IN (1, 2, 3);
# User@Host: _a1b2c3[_a1b2c3] @  [172.18.0.5]
# Thread_id: 44  Schema: _a1b2c3  QC_hit: No
# Query_time: 3.000000  Lock_time: 0.000000  Rows_sent: 0  Rows_examined: 70
SET timestamp=1762164310;
DELETE FROM `tabVersion` WHERE name IN ('a', 'b', 'c') /* frappe */;
"""


def test_parse_slow_log():
    entries = rl.parse_slow_log(SLOW_LOG)
    assert len(entries) == 4
    e = entries[0]
    assert e["schema"] == "_a1b2c3" and e["query_time"] == 2.5 and e["rows_examined"] == 500000
    assert e["rows_sent"] == 10 and e["query"].startswith("SELECT name, grand_total")
    assert e["timestamp"] == "2025-11-03T10:00:01+00:00"
    assert entries[1]["query"].count("\n") == 2
    assert entries[2]["schema"] == "_b"


def test_normalize_query():
    assert rl.normalize_query("SELECT * FROM `tab1` WHERE a = 'x''y' AND b=12.5 AND c IN (1, 2,3) -- c") \
        == "SELECT * FROM `tab1` WHERE a = ? AND b=? AND c IN (?, ...)"
    assert rl.normalize_query("INSERT INTO t VALUES (1, 'a'), (2, 'b'), (3, 'c');") == "INSERT INTO t VALUES (?, ...), ..."
    assert rl.normalize_query("select /* x */  1") == "select ?"


def test_group_slow_queries():
    groups = rl.group_slow_queries(rl.parse_slow_log(SLOW_LOG), "_a1b2c3")
    assert len(groups) == 2
    top = groups[0]
    assert top["count"] == 2 and top["total_time"] == 4.0 and top["avg_time"] == 2.0 and top["max_time"] == 2.5
    assert top["rows_examined_max"] == 500000 and top["rows_examined_avg"] == 400000
    assert "ACME" not in top["query"] and "ACME Corp" in top["example"]
    assert groups[1]["query"].startswith("DELETE FROM `tabVersion` WHERE name IN (?, ...)")
    assert all("secret" not in g["example"] for g in groups)
    assert rl.group_slow_queries(rl.parse_slow_log(SLOW_LOG), "_a1b2c3", min_seconds=2.6)[0]["count"] == 1


def test_site_slow_queries_job(ctx, worker, settings):
    setup_site(ctx)
    job = run_job(ctx, worker, "site.slow_queries", {"site": SITE})
    assert job["status"] == "success" and job["result"]["enabled"] is False
    data = settings.root / "db" / "data"
    data.mkdir(parents=True)
    (data / "slow.log").write_text(SLOW_LOG)
    job = run_job(ctx, worker, "site.slow_queries", {"site": SITE, "limit": 1})
    assert job["status"] == "success", job
    r = job["result"]
    assert r["enabled"] is True and len(r["queries"]) == 1 and r["entries"] == 3
    assert "tabOther" not in json.dumps(r)


# =========================================================================== console
def test_console_bootstrap_compiles_and_args():
    compile(rc.BOOTSTRAP, "<bootstrap>", "exec")
    for needle in ('frappe.init(site=site, sites_path=".")', "frappe.connect()", "frappe.db.commit()",
                   "frappe.db.rollback()", "frappe.destroy()", "signal.alarm(timeout)", rc.RESULT_MARKER):
        assert needle in rc.BOOTSTRAP
    args = rc.bootstrap_args(SITE, "/home/frappe/frappe-bench/sites/.nexaploy-console/j1.py", True, 30)
    assert args[0] == "/home/frappe/frappe-bench/env/bin/python" and args[1] == "-c"
    assert args[3:] == [SITE, "/home/frappe/frappe-bench/sites/.nexaploy-console/j1.py", "1", "30", str(rc.MAX_OUTPUT_CHARS)]
    assert rc.console_file_name("01J/../x") == "01Jx.py"


def test_console_bootstrap_runs_with_fake_frappe(tmp_path):
    """Execute the bootstrap against a stub `frappe` module to check capture/commit semantics."""
    import subprocess
    import sys

    stub = tmp_path / "frappe.py"
    stub.write_text(
        "calls = []\n"
        "class _DB:\n"
        "    def commit(self): print('COMMIT-CALLED')\n"
        "    def rollback(self): print('ROLLBACK-CALLED')\n"
        "class _Local: db = None\n"
        "local = _Local()\n"
        "db = _DB()\n"
        "def init(site, sites_path): pass\n"
        "def connect(): local.db = db\n"
        "def destroy(): pass\n"
    )

    def run(code, commit, timeout=10):
        f = tmp_path / "code.py"
        f.write_text(code)
        proc = subprocess.run([sys.executable, "-c", rc.BOOTSTRAP, SITE, str(f), "1" if commit else "0", str(timeout), "50"],
                              cwd=tmp_path, capture_output=True, text=True, env={"PYTHONPATH": str(tmp_path)})
        return proc.stdout, rc.parse_console_output(proc.stdout)

    out, data = run("print('hello', frappe.__name__)", commit=True)
    assert data["ok"] and data["committed"] and data["output"] == "hello frappe\n"
    assert "COMMIT-CALLED" in out
    out, data = run("print('x' * 100)", commit=False)
    assert data["ok"] and not data["committed"] and data["truncated"] and len(data["output"]) == 50
    assert "ROLLBACK-CALLED" in out
    out, data = run("raise ValueError('boom')", commit=True)
    assert not data["ok"] and not data["committed"] and data["error"] == "ValueError: boom"
    assert "Traceback" in data["output"]
    out, data = run("import time\nwhile True: time.sleep(0.05)", commit=True, timeout=1)
    assert not data["ok"] and "timed out" in data["error"]


def test_parse_console_output():
    assert rc.parse_console_output("noise") is None
    good = 'warn\n' + rc.RESULT_MARKER + json.dumps({"ok": True, "output": "x"}) + "\n"
    assert rc.parse_console_output(good) == {"ok": True, "output": "x"}
    assert rc.preamble(good) == "warn"


def _console_result(call: Call) -> Result:
    path = call.args[call.args.index("-c") + 3]
    host = path.replace("/home/frappe/frappe-bench/sites", _console_result.sites_dir)
    p = Path(host)
    assert p.read_text() == "print(frappe.db.count('User'))"
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    assert oct(p.parent.stat().st_mode & 0o777) == "0o700"
    payload = {"ok": True, "committed": False, "error": None, "output": "3\n", "truncated": False, "duration": 0.1}
    return Result(0, rc.RESULT_MARKER + json.dumps(payload) + "\n", "")


def test_site_console_job(ctx, worker, runner):
    setup_site(ctx)
    bench_running(runner)
    _console_result.sites_dir = str(ctx.benches.sites_dir("erp-v15"))
    runner.on(["exec", "-T", "-w"], result=_console_result)
    job = run_job(ctx, worker, "site.console", {"site": SITE, "code": "print(frappe.db.count('User'))", "timeout": 20})
    assert job["status"] == "success", job
    assert job["result"]["output"] == "3\n" and job["result"]["ok"] is True and job["result"]["committed"] is False
    call = runner.find("exec -T -w")[0]
    assert call.args[:4] == ["docker", "compose", "-p", "erp-v15"]
    assert call.args[call.args.index("-w") + 1] == "/home/frappe/frappe-bench/sites"
    assert call.args[call.args.index("backend") + 1] == "/home/frappe/frappe-bench/env/bin/python"
    assert call.args[-3:] == ["0", "20", str(rc.MAX_OUTPUT_CHARS)]
    assert call.timeout == 80
    # the code file is removed afterwards and the code never appears in argv
    assert not list((ctx.benches.sites_dir("erp-v15") / rc.CONSOLE_DIR).iterdir())
    assert "count('User')" not in call.cmdline


def test_site_console_requires_running_bench_and_valid_code(ctx, worker, runner):
    setup_site(ctx)
    with pytest.raises(ValidationError):
        ctx.enqueue("site.console", {"site": SITE, "code": "def ("})
    with pytest.raises(ValidationError):
        ctx.enqueue("site.console", {"site": SITE, "code": "x", "timeout": 100000})
    job = run_job(ctx, worker, "site.console", {"site": SITE, "code": "print(1)"})
    assert job["status"] == "failure" and "not running" in job["error"]


def test_site_console_user_exception_is_a_result(ctx, worker, runner):
    setup_site(ctx)
    bench_running(runner)
    payload = {"ok": False, "committed": False, "error": "ZeroDivisionError: division by zero",
               "output": "Traceback ...", "truncated": False, "duration": 0.01}
    runner.on(["exec", "-T", "-w"], stdout=rc.RESULT_MARKER + json.dumps(payload) + "\n")
    job = run_job(ctx, worker, "site.console", {"site": SITE, "code": "1/0", "commit": True})
    assert job["status"] == "success"
    assert job["result"]["ok"] is False and job["result"]["error"].startswith("ZeroDivisionError")
    assert runner.find("exec -T -w")[0].args[-3] == "1"


def test_site_console_refused_on_shared_bench(ctx, worker, runner):
    setup_site(ctx, second_site=True)
    bench_running(runner)
    job = run_job(ctx, worker, "site.console", {"site": SITE, "code": "print(1)"})
    assert job["status"] == "failure" and "shared" in job["error"]
    assert not runner.called("exec -T -w")
    runner.on(["exec", "-T", "-w"], stdout=rc.RESULT_MARKER + json.dumps({"ok": True, "output": ""}) + "\n")
    job = run_job(ctx, worker, "site.console", {"site": SITE, "code": "print(1)", "allow_shared_bench": True})
    assert job["status"] == "success", job


def test_site_console_missing_result_fails(ctx, worker, runner):
    setup_site(ctx)
    bench_running(runner)
    runner.on(["exec", "-T", "-w"], returncode=1, stderr="ModuleNotFoundError: frappe")
    job = run_job(ctx, worker, "site.console", {"site": SITE, "code": "print(1)"})
    assert job["status"] == "failure" and "ModuleNotFoundError" in job["error"]


# =========================================================================== API
@pytest.fixture
def client(ctx):
    c = TestClient(create_app(ctx))
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    return c


def test_api_routes(client, ctx):
    setup_site(ctx)
    r = client.get(f"/v1/sites/{SITE}/logs?log=worker&lines=50&since=2h")
    assert r.status_code == 202 and r.json()["kind"] == "site.logs"
    params = ctx.store.get(r.json()["job_id"])["params"]
    assert params["log"] == "worker" and params["lines"] == 50 and params["since"] == "2h"
    assert client.get(f"/v1/sites/{SITE}/logs?log=nope").status_code == 422
    assert client.get(f"/v1/sites/{SITE}/analytics?date_from=2026-11-01&date_to=2026-11-03").status_code == 202
    assert client.get(f"/v1/sites/{SITE}/slow-queries?limit=5&min_seconds=2").status_code == 202
    r = client.post(f"/v1/sites/{SITE}/sql", json={"query": "SELECT 1"})
    assert r.status_code == 202 and r.json()["kind"] == "site.sql"
    r = client.post(f"/v1/sites/{SITE}/sql", json={"query": "DROP TABLE x"})
    assert r.status_code == 422 and "read-only" in r.text
    assert client.post(f"/v1/sites/{SITE}/sql", json={"query": "DROP TABLE x", "read_only": False}).status_code == 202
    assert client.post(f"/v1/sites/{SITE}/console", json={"code": "print(1)", "commit": True}).status_code == 202
    assert client.post("/v1/sites/zz.example.com/console", json={"code": "print(1)"}).status_code == 404
    assert client.get("/v1/sites/zz.example.com/logs").status_code == 404
