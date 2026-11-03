"""site.clone (staging) and site.rename jobs (ARCHITECTURE.md §10)."""

import json

import pytest
import yaml
from pydantic import ValidationError as PydanticValidationError

from nexaploy_agent.jobs.base import Conflict, NotFound
from nexaploy_agent.runtime.lifecycle import (
    SCRUB_SQL, STAGING_CONFIG_KEY, db_exec_command, rename_config, rename_host_name, rename_state,
    restore_args, scrub_config,
)

from .conftest import DB_PW, make_bench, make_site, run_job, side_effect

SRC = "erp.example.com"
STG = "staging-erp.example.com"
KEY = "enc-key-SECRET-123"


def _script_backup(site_dir):
    def backup(call):
        d = site_dir / "private" / "backups"
        d.mkdir(parents=True, exist_ok=True)
        (d / "20261103_213000-erp_example_com-database.sql.gz").write_bytes(b"sql")
        (d / "20261103_213000-erp_example_com-files.tar").write_bytes(b"pub")
        (d / "20261103_213000-erp_example_com-private-files.tar").write_bytes(b"priv")
    return side_effect(backup)


def _setup_clone(ctx, runner):
    make_bench(ctx, "erp-v15")
    src = make_site(ctx, "erp-v15", SRC, domains=["www.example.com"], extra={"encryption_key": KEY})
    ctx.proxy.write_route(SRC, ["www.example.com"], "erp-v15")
    runner.on(["backup", "--with-files"], result=_script_backup(src))

    def new_site(call):
        make_site(ctx, "erp-v15", call.args[call.args.index("new-site") + 1], extra={"db_name": "_stg123"})

    runner.on("new-site", result=side_effect(new_site))
    return src


# ---- pure helpers -----------------------------------------------------------------------------
def test_pure_helpers():
    assert restore_args(STG, "/db.sql.gz", "/pub.tar", None, "pw") == [
        "--site", STG, "restore", "/db.sql.gz", "--with-public-files", "/pub.tar",
        "--db-root-username", "root", "--db-root-password", "pw", "--force",
    ]
    argv = db_exec_command("_stg123", SCRUB_SQL)
    assert argv[:5] == ["docker", "exec", "-e", "MYSQL_PWD", "nexaploy-db"] and argv[-1] == SCRUB_SQL
    assert "pw" not in " ".join(argv)
    with pytest.raises(Exception):
        db_exec_command("x; drop", SCRUB_SQL)
    cfg = scrub_config({"db_name": "x", "host_name": "https://erp.example.com", "domains": ["a.b.cc"]}, SRC)
    assert cfg == {"db_name": "x", "mute_emails": 1, "pause_scheduler": 1, "maintenance_mode": 0,
                   STAGING_CONFIG_KEY: SRC}
    assert rename_host_name("https://old.example.com", "old.example.com", "new.example.com") == "https://new.example.com"
    assert rename_host_name("old.example.com:8000", "old.example.com", "new.example.com") == "new.example.com:8000"
    assert rename_host_name("https://other.example.com", "old.example.com", "new.example.com") == "https://other.example.com"
    new = rename_config({"domains": ["new.example.com", {"domain": "x.example.com"}], "host_name": "old.example.com"},
                        "old.example.com", "new.example.com")
    assert new["domains"] == [{"domain": "x.example.com"}, "old.example.com"]
    assert new["host_name"] == "new.example.com"
    assert "domains" not in rename_config({}, "old.example.com", "new.example.com", keep_old_domain=False)


def test_rename_state(tmp_path):
    assert rename_state(tmp_path, "a.example.com", "b.example.com") == "missing"
    (tmp_path / "a.example.com").mkdir()
    (tmp_path / "a.example.com" / "site_config.json").write_text("{}")
    assert rename_state(tmp_path, "a.example.com", "b.example.com") == "pending"
    (tmp_path / "b.example.com").mkdir()
    (tmp_path / "b.example.com" / "site_config.json").write_text("{}")
    assert rename_state(tmp_path, "a.example.com", "b.example.com") == "conflict"
    (tmp_path / "a.example.com" / "site_config.json").unlink()
    assert rename_state(tmp_path, "a.example.com", "b.example.com") == "moved"


# ---- site.clone -------------------------------------------------------------------------------
def test_site_clone(ctx, worker, runner):
    _setup_clone(ctx, runner)
    job = run_job(ctx, worker, "site.clone", {"bench": "erp-v15", "site": SRC, "target_site": STG})
    assert job["status"] == "success", job
    lines = runner.cmdlines()
    order = [next(i for i, line in enumerate(lines) if needle in line)
             for needle in ("backup --with-files", f"new-site {STG}", f"--site {STG} restore",
                            f"--site {STG} set-config encryption_key", "exec -e MYSQL_PWD", f"--site {STG} migrate")]
    assert order == sorted(order)
    restore = runner.find(f"--site {STG} restore")[0].args
    assert "--force" in restore and "--with-public-files" in restore and "--with-private-files" in restore
    assert restore[restore.index("--db-root-password") + 1] == DB_PW
    sql = runner.find("exec -e MYSQL_PWD")[0]
    assert sql.env == {"MYSQL_PWD": DB_PW} and DB_PW not in sql.cmdline
    assert sql.args[-1] == SCRUB_SQL and "_stg123" in sql.args
    cfg = ctx.sites.site_config("erp-v15", STG)
    assert cfg["mute_emails"] == 1 and cfg["pause_scheduler"] == 1 and cfg["maintenance_mode"] == 0
    assert cfg[STAGING_CONFIG_KEY] == SRC and "domains" not in cfg
    route = yaml.safe_load(ctx.proxy.route_path(STG).read_text())
    assert route["http"]["routers"]["site-staging-erp-example-com"]["rule"] == f"Host(`{STG}`)"
    # production untouched, secrets never stored
    assert ctx.sites.domains("erp-v15", SRC) == ["www.example.com"]
    blob = json.dumps(job)
    assert DB_PW not in blob and KEY not in blob
    # temporary restore copy removed
    assert not list((ctx.benches.sites_dir("erp-v15") / ".nexaploy-restore").glob("*"))


def test_site_clone_refresh_and_conflicts(ctx, worker, runner):
    _setup_clone(ctx, runner)
    assert run_job(ctx, worker, "site.clone", {"bench": "erp-v15", "site": SRC, "target_site": STG})["status"] == "success"
    runner.reset()
    # refresh: the staging site exists (with the marker) -> no new-site, restore again
    job = run_job(ctx, worker, "site.clone", {"bench": "erp-v15", "site": SRC, "target_site": STG})
    assert job["status"] == "success", job
    assert not runner.called("new-site") and runner.called(f"--site {STG} restore")
    assert job["result"]["refresh"] is True
    # a real site is never overwritten
    make_site(ctx, "erp-v15", "other.example.com")
    with pytest.raises(Conflict):
        ctx.enqueue("site.clone", {"bench": "erp-v15", "site": SRC, "target_site": "other.example.com"},
                    precheck=True)
    job = run_job(ctx, worker, "site.clone", {"bench": "erp-v15", "site": SRC, "target_site": "other.example.com"})
    assert job["status"] == "failure" and "not a staging copy" in job["error"]
    with pytest.raises(Conflict):  # a domain of the production site
        ctx.enqueue("site.clone", {"bench": "erp-v15", "site": SRC, "target_site": "www.example.com"},
                    precheck=True)
    with pytest.raises(NotFound):
        ctx.enqueue("site.clone", {"bench": "erp-v15", "site": "nope.example.com", "target_site": STG},
                    precheck=True)
    with pytest.raises(PydanticValidationError):
        ctx.enqueue("site.clone", {"bench": "erp-v15", "site": SRC, "target_site": SRC})


def test_site_clone_without_scrub(ctx, worker, runner):
    _setup_clone(ctx, runner)
    job = run_job(ctx, worker, "site.clone", {"bench": "erp-v15", "site": SRC, "target_site": STG, "scrub": False})
    assert job["status"] == "success"
    assert not runner.called("exec -e MYSQL_PWD")
    assert "mute_emails" not in ctx.sites.site_config("erp-v15", STG)


def test_site_clone_resume_skips_new_site(ctx, worker, runner):
    """Crash after new-site created the target: the resumed job does not run new-site again."""
    src = _setup_clone(ctx, runner)
    job = ctx.enqueue("site.clone", {"bench": "erp-v15", "site": SRC, "target_site": STG})
    jid = job["id"]
    # run check/backup/stage for real through a first worker pass that fails at restore
    runner.on(f"--site {STG} restore", returncode=1, stderr="boom", times=1)
    worker.run_once()
    first = ctx.store.get(jid)
    assert first["status"] == "failure" and runner.called(f"new-site {STG}")
    assert src.exists()
    # simulate a crash instead: steps up to new_site done, restore running
    rec = ctx.store.create("site.clone", {"bench": "erp-v15", "site": SRC, "target_site": STG},
                           [s for s in ("check", "backup", "stage", "new_site", "restore", "restore_config",
                                        "scrub", "migrate", "write_route")])
    ctx.store.claim_next()
    for idx in range(4):
        ctx.store.start_step(rec["id"], idx)
        ctx.store.end_step(rec["id"], idx, "success")
    ctx.store.start_step(rec["id"], 4)
    ctx.store.set_result(rec["id"], {**first["result"], "target_created": True})
    runner.reset()
    assert worker.recover() == [rec["id"]]
    assert worker.run_once()
    got = ctx.store.get(rec["id"])
    assert got["status"] == "success", got
    assert not runner.called("new-site") and not runner.called("backup --with-files")
    assert runner.called(f"--site {STG} restore")


# ---- site.rename ------------------------------------------------------------------------------
def _setup_rename(ctx):
    make_bench(ctx, "erp-v15")
    d = make_site(ctx, "erp-v15", "old.example.com", domains=["www.example.com", "new.example.com"],
                  extra={"host_name": "https://old.example.com", "maintenance_mode": 0})
    ctx.proxy.write_route("old.example.com", ["www.example.com", "new.example.com"], "erp-v15")
    return d


def test_site_rename(ctx, worker, runner):
    _setup_rename(ctx)
    seen = {}

    def clear(call):
        seen["maintenance"] = ctx.sites.site_config("erp-v15", "new.example.com").get("maintenance_mode")

    runner.on("clear-cache", result=side_effect(clear))
    job = run_job(ctx, worker, "site.rename",
                  {"bench": "erp-v15", "site": "old.example.com", "new_name": "new.example.com"})
    assert job["status"] == "success", job
    sites = ctx.benches.sites_dir("erp-v15")
    assert not (sites / "old.example.com").exists() and (sites / "new.example.com" / "site_config.json").exists()
    cfg = ctx.sites.site_config("erp-v15", "new.example.com")
    assert cfg["host_name"] == "https://new.example.com"
    assert cfg["domains"] == ["www.example.com", "old.example.com"]
    assert cfg["db_name"] == "_abc"  # same database
    assert cfg["maintenance_mode"] == 0 and seen["maintenance"] == 1
    assert not ctx.proxy.has_route("old.example.com")
    rule = yaml.safe_load(ctx.proxy.route_path("new.example.com").read_text())["http"]["routers"][
        "site-new-example-com"]["rule"]
    assert rule == "Host(`new.example.com`) || Host(`www.example.com`) || Host(`old.example.com`)"
    assert runner.called("--site new.example.com clear-cache")
    assert ctx.sites.find_bench("new.example.com") == "erp-v15"


def test_site_rename_resume_after_move(ctx, worker, runner):
    """The directory was already renamed (crash after mv): the job skips the move."""
    d = _setup_rename(ctx)
    cfg = json.loads((d / "site_config.json").read_text())
    cfg["maintenance_mode"] = 1
    (d / "site_config.json").write_text(json.dumps(cfg))
    d.rename(d.parent / "new.example.com")
    job = run_job(ctx, worker, "site.rename",
                  {"bench": "erp-v15", "site": "old.example.com", "new_name": "new.example.com"})
    assert job["status"] == "success", job
    move = next(s for s in job["steps"] if s["name"] == "move")
    assert "already done" in move["output"]
    assert ctx.proxy.has_route("new.example.com") and not ctx.proxy.has_route("old.example.com")
    assert "old.example.com" in ctx.sites.domains("erp-v15", "new.example.com")


def test_site_rename_keeps_prior_maintenance_and_conflicts(ctx, worker, runner):
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", "a.example.com", extra={"maintenance_mode": 1})
    make_site(ctx, "erp-v15", "b.example.com", domains=["shop.example.com"])
    with pytest.raises(Conflict):
        ctx.enqueue("site.rename", {"bench": "erp-v15", "site": "a.example.com", "new_name": "b.example.com"},
                    precheck=True)
    with pytest.raises(Conflict):
        ctx.enqueue("site.rename", {"bench": "erp-v15", "site": "a.example.com", "new_name": "shop.example.com"},
                    precheck=True)
    with pytest.raises(NotFound):
        ctx.enqueue("site.rename", {"bench": "erp-v15", "site": "zz.example.com", "new_name": "y.example.com"},
                    precheck=True)
    with pytest.raises(PydanticValidationError):
        ctx.enqueue("site.rename", {"bench": "erp-v15", "site": "a.example.com", "new_name": "a.example.com"})
    job = run_job(ctx, worker, "site.rename",
                  {"bench": "erp-v15", "site": "a.example.com", "new_name": "c.example.com", "keep_old_domain": False})
    assert job["status"] == "success", job
    cfg = ctx.sites.site_config("erp-v15", "c.example.com")
    assert cfg["maintenance_mode"] == 1 and "a.example.com" not in json.dumps(cfg.get("domains", []))
    # both directories present -> refuse
    make_site(ctx, "erp-v15", "d.example.com")
    make_site(ctx, "erp-v15", "e.example.com")
    job = run_job(ctx, worker, "site.rename",
                  {"bench": "erp-v15", "site": "d.example.com", "new_name": "e.example.com"})
    assert job["status"] == "failure" and "both" in job["error"]


def test_lifecycle_api_and_param_shapes(ctx):
    from fastapi.testclient import TestClient

    from nexaploy_agent.api import create_app

    from .conftest import TOKEN

    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", SRC)
    client = TestClient(create_app(ctx))
    h = {"Authorization": f"Bearer {TOKEN}"}
    r = client.post(f"/v1/sites/{SRC}/clone", json={"target_site": STG}, headers=h)
    assert r.status_code == 202 and r.json()["kind"] == "site.clone"
    assert ctx.store.get(r.json()["job_id"])["params"]["bench"] == "erp-v15"
    r = client.post(f"/v1/sites/{SRC}/rename", json={"new_name": "renamed.example.com"}, headers=h)
    assert r.status_code == 202 and r.json()["kind"] == "site.rename"
    r = client.post("/v1/sites/nope.example.com/rename", json={"new_name": "x.example.com"}, headers=h)
    assert r.status_code == 404
    # control-plane shaped params
    job = ctx.enqueue("site.clone", {"bench": "erp-v15", "site": SRC, "target_site": STG, "scrub": True},
                      origin="control_plane", remote_id="AJ-1")
    assert job["status"] == "queued"
    job = ctx.enqueue("site.rename", {"bench": "erp-v15", "site": SRC, "new_name": "n.example.com",
                                      "keep_old_domain": True}, origin="control_plane", remote_id="AJ-2")
    assert job["status"] == "queued"
