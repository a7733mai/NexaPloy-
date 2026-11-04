import json

import pytest
import yaml
from pydantic import ValidationError as PydanticValidationError

from nexaploy_agent.jobs.base import Conflict, NotFound
from nexaploy_agent.redact import MASK
from nexaploy_agent.runtime.docker import Result

from .conftest import DB_PW, IMAGE, apps_listing, bench_running, make_bench, make_site, run_job, side_effect

NEW_IMAGE = "nexaploy/bench:erp-v15-main-20261201T1000"


def cmd_index(runner, needle):
    for i, line in enumerate(runner.cmdlines()):
        if needle in line:
            return i
    raise AssertionError(f"{needle!r} not called; calls:\n" + "\n".join(runner.cmdlines()))


# ---- bench -----------------------------------------------------------------------------------
def test_bench_create(ctx, worker, runner):
    runner.on(["image", "inspect"], returncode=1, times=1)  # not local -> pull
    runner.on(["network", "inspect", "--format", "{{.Name}}", "proxy-network"], returncode=1, times=1)
    job = run_job(ctx, worker, "bench.create", {
        "name": "erp-v15", "image": IMAGE, "env": {"TZ": "Asia/Qatar"},
        "apps_json": [{"url": "https://github.com/frappe/erpnext", "branch": "version-15"}],
    })
    assert job["status"] == "success", job
    path = ctx.benches.path("erp-v15")
    assert (path / "docker-compose.yml").exists()
    assert "TZ=Asia/Qatar" in (path / ".env").read_text()
    assert json.loads((path / "apps.json").read_text())[0]["branch"] == "version-15"
    pull = cmd_index(runner, f"docker pull {IMAGE}")
    conf = cmd_index(runner, "--profile configurator run --rm -T configurator")
    up = cmd_index(runner, "up -d --remove-orphans")
    assert pull < conf < up
    assert "docker compose -p erp-v15 --project-directory" in runner.cmdlines()[up]
    assert runner.called("docker network create --driver bridge proxy-network")
    assert not runner.called("network create --driver bridge nexaploy-db")  # already existed


def test_bench_create_conflict_and_validation(ctx):
    make_bench(ctx, "erp-v15")
    with pytest.raises(Conflict):
        ctx.enqueue("bench.create", {"name": "erp-v15", "image": IMAGE}, precheck=True)
    with pytest.raises(PydanticValidationError):
        ctx.enqueue("bench.create", {"name": "Bad_Name", "image": IMAGE})
    with pytest.raises(PydanticValidationError):
        ctx.enqueue("bench.create", {"name": "erp-v16", "image": "no-tag"})
    with pytest.raises(PydanticValidationError):
        ctx.enqueue("bench.create", {"name": "erp-v16", "image": IMAGE, "env": {"IMAGE": "x"}})


def test_bench_create_resume_is_idempotent(ctx, worker, runner):
    job = run_job(ctx, worker, "bench.create", {"name": "erp-v15", "image": IMAGE})
    assert job["status"] == "success"
    mtime = ctx.benches.compose_file("erp-v15").stat().st_mtime_ns
    # re-running the same create (e.g. control plane re-send) changes nothing on disk
    job2 = run_job(ctx, worker, "bench.create", {"name": "erp-v15", "image": IMAGE})
    assert job2["status"] == "success"
    assert ctx.benches.compose_file("erp-v15").stat().st_mtime_ns == mtime
    # but a different image is refused
    job3 = run_job(ctx, worker, "bench.create", {"name": "erp-v15", "image": NEW_IMAGE})
    assert job3["status"] == "failure" and "bench.update" in job3["error"]


def test_bench_update_migrates_all_sites(ctx, worker, runner):
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", "a.example.com")
    make_site(ctx, "erp-v15", "b.example.com")
    bench_running(runner)
    job = run_job(ctx, worker, "bench.update", {"bench": "erp-v15", "image": NEW_IMAGE})
    assert job["status"] == "success", job
    assert ctx.benches.image("erp-v15") == NEW_IMAGE
    assert job["result"]["previous_image"] == IMAGE
    assert job["result"]["migrated"] == ["a.example.com", "b.example.com"]
    conf = cmd_index(runner, "configurator")
    up = cmd_index(runner, "up -d")
    mig = cmd_index(runner, "exec -T backend bench --site a.example.com migrate")
    assert conf < up < mig
    assert runner.called("--site b.example.com migrate")
    assert not runner.called("volume rm")


def test_bench_start_stop_restart(ctx, worker, runner):
    make_bench(ctx, "erp-v15")
    assert run_job(ctx, worker, "bench.stop", {"bench": "erp-v15"})["status"] == "success"
    assert runner.called("compose -p erp-v15") and runner.called(" stop")
    assert run_job(ctx, worker, "bench.start", {"bench": "erp-v15"})["status"] == "success"
    bench_running(runner)
    assert run_job(ctx, worker, "bench.restart", {"bench": "erp-v15"})["status"] == "success"
    assert runner.called(" restart")
    with pytest.raises(NotFound):
        ctx.enqueue("bench.start", {"bench": "nope-bench"}, precheck=True)


def test_bench_remove(ctx, worker, runner):
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", "a.example.com")
    ctx.proxy.write_route("a.example.com", [], "erp-v15")
    with pytest.raises(Conflict):
        ctx.enqueue("bench.remove", {"bench": "erp-v15"}, precheck=True)
    failed = run_job(ctx, worker, "bench.remove", {"bench": "erp-v15"})
    assert failed["status"] == "failure" and "still has sites" in failed["error"]
    assert [s["status"] for s in failed["steps"]] == ["failure", "skipped", "skipped", "skipped"]
    job = run_job(ctx, worker, "bench.remove", {"bench": "erp-v15", "force": True})
    assert job["status"] == "success", job
    assert not ctx.benches.path("erp-v15").exists()
    assert "archived_to" in job["result"]
    assert not ctx.proxy.has_route("a.example.com")
    assert runner.called("down --remove-orphans")

    make_bench(ctx, "erp-v14")
    job = run_job(ctx, worker, "bench.remove", {"bench": "erp-v14"})
    assert job["status"] == "success" and not ctx.benches.path("erp-v14").exists()


# ---- site ------------------------------------------------------------------------------------
def _script_new_site(ctx, runner, bench="erp-v15"):
    def create(call):
        site = call.args[call.args.index("new-site") + 1]
        make_site(ctx, bench, site)
        apps_listing(runner, "frappe")

    runner.on("new-site", result=side_effect(create))


def test_site_create(ctx, worker, runner):
    make_bench(ctx, "erp-v15")
    bench_running(runner)
    runner.on("list-apps", returncode=1, stderr="no site")
    _script_new_site(ctx, runner)
    installed = []

    def install(call):
        installed.append(call.args[-1])
        apps_listing(runner, "frappe", *installed)

    runner.on("install-app", result=side_effect(install))
    job = run_job(ctx, worker, "site.create", {
        "bench": "erp-v15", "name": "erp.example.com", "admin_password": "Adm1nPass!", "apps": ["erpnext", "hrms"],
    })
    assert job["status"] == "success", job
    new_site = [c for c in runner.find("new-site") if "--help" not in c.args][0].args
    i = new_site.index("bench")
    assert new_site[:i] == ["docker", "compose", "-p", "erp-v15", "--project-directory",
                            str(ctx.benches.path("erp-v15")), "exec", "-T", "backend"]
    tail = new_site[i:]
    assert tail[:3] == ["bench", "new-site", "erp.example.com"]
    for flag, val in (("--db-host", "nexaploy-db"), ("--db-root-username", "root"),
                      ("--db-root-password", DB_PW), ("--admin-password", "Adm1nPass!")):
        assert tail[tail.index(flag) + 1] == val
    # the fake `bench new-site --help` lists no login-scope flag -> treated as v14 (legacy flag)
    assert "--no-mariadb-socket" in tail
    assert installed == ["erpnext", "hrms"]
    assert job["result"]["apps"] == ["frappe", "erpnext", "hrms"]
    # secrets never reach step output, error or stored params
    blob = json.dumps(job)
    assert DB_PW not in blob and "Adm1nPass!" not in blob
    assert job["params"]["admin_password"] == MASK
    route = yaml.safe_load(ctx.proxy.route_path("erp.example.com").read_text())
    assert "site-erp-example-com" in route["http"]["routers"]


def test_site_create_skips_existing(ctx, worker, runner):
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", "erp.example.com")
    apps_listing(runner, "frappe", "erpnext")
    job = run_job(ctx, worker, "site.create", {
        "bench": "erp-v15", "name": "erp.example.com", "admin_password": "Adm1nPass!", "apps": ["erpnext"],
    })
    assert job["status"] == "success"
    assert not runner.called("new-site") and not runner.called("install-app")
    assert "already done" in job["steps"][1]["output"]
    # uses `run --rm` when the bench is down
    assert runner.called("run --rm -T backend bench --site erp.example.com list-apps")


def test_site_create_conflicts(ctx):
    make_bench(ctx, "erp-v15")
    make_bench(ctx, "erp-v14")
    make_site(ctx, "erp-v14", "a.example.com", domains=["www.example.com"])
    with pytest.raises(Conflict):
        ctx.enqueue("site.create", {"bench": "erp-v15", "name": "a.example.com", "admin_password": "secret1"},
                    precheck=True)
    with pytest.raises(Conflict):
        ctx.enqueue("site.create", {"bench": "erp-v15", "name": "www.example.com", "admin_password": "secret1"},
                    precheck=True)
    with pytest.raises(NotFound):
        ctx.enqueue("site.create", {"bench": "nope-x", "name": "b.example.com", "admin_password": "secret1"},
                    precheck=True)


def test_domains(ctx, worker, runner):
    make_bench(ctx, "erp-v15")
    site_dir = make_site(ctx, "erp-v15", "a.example.com", domains=[])

    def add(call):
        cfg = json.loads((site_dir / "site_config.json").read_text())
        cfg["domains"].append({"domain": call.args[call.args.index("add-domain") + 1]})
        (site_dir / "site_config.json").write_text(json.dumps(cfg))

    runner.on("add-domain", result=side_effect(add))
    job = run_job(ctx, worker, "site.add_domain", {"site": "a.example.com", "domain": "WWW.Example.com"})
    assert job["status"] == "success", job
    assert runner.called("bench setup add-domain www.example.com --site a.example.com")
    rule = yaml.safe_load(ctx.proxy.route_path("a.example.com").read_text())["http"]["routers"][
        "site-a-example-com"]["rule"]
    assert rule == "Host(`a.example.com`) || Host(`www.example.com`)"
    # idempotent
    runner.reset()
    job = run_job(ctx, worker, "site.add_domain", {"site": "a.example.com", "domain": "www.example.com"})
    assert job["status"] == "success" and not runner.called("add-domain")

    def remove(call):
        cfg = json.loads((site_dir / "site_config.json").read_text())
        cfg["domains"] = []
        (site_dir / "site_config.json").write_text(json.dumps(cfg))

    runner.on("remove-domain", result=side_effect(remove))
    job = run_job(ctx, worker, "site.remove_domain", {"site": "a.example.com", "domain": "www.example.com"})
    assert job["status"] == "success"
    assert "www.example.com" not in ctx.proxy.route_path("a.example.com").read_text()
    with pytest.raises(Exception):
        ctx.enqueue("site.remove_domain", {"site": "a.example.com", "domain": "a.example.com"})


def test_domain_owned_by_other_site(ctx):
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", "a.example.com", domains=["shop.example.com"])
    make_site(ctx, "erp-v15", "b.example.com")
    with pytest.raises(Conflict):
        ctx.enqueue("site.add_domain", {"site": "b.example.com", "domain": "shop.example.com"}, precheck=True)


def test_install_uninstall_app(ctx, worker, runner):
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", "a.example.com")
    apps_listing(runner, "frappe")
    assert run_job(ctx, worker, "site.install_app", {"site": "a.example.com", "app": "erpnext"})["status"] == "success"
    assert runner.called("--site a.example.com install-app erpnext")
    job = run_job(ctx, worker, "site.uninstall_app", {"site": "a.example.com", "app": "erpnext"})
    assert job["status"] == "success" and not runner.called("uninstall-app")  # not installed
    apps_listing(runner, "frappe", "erpnext")
    run_job(ctx, worker, "site.uninstall_app", {"site": "a.example.com", "app": "erpnext"})
    assert runner.called("--site a.example.com uninstall-app erpnext --yes")
    with pytest.raises(Exception):
        ctx.enqueue("site.uninstall_app", {"site": "a.example.com", "app": "frappe"})


def test_set_config_and_migrate(ctx, worker, runner):
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", "a.example.com", extra={"maintenance_mode": 0})
    job = run_job(ctx, worker, "site.set_config", {"site": "a.example.com", "key": "maintenance_mode", "value": 0})
    assert job["status"] == "success" and not runner.called("set-config")
    run_job(ctx, worker, "site.set_config", {"site": "a.example.com", "key": "maintenance_mode", "value": 1})
    assert runner.called("--site a.example.com set-config -p maintenance_mode 1")
    run_job(ctx, worker, "site.set_config", {"site": "a.example.com", "key": "api_secret", "value": "s3cr3tvalue"})
    job = ctx.store.list(kind="site.set_config", limit=1)[0]
    assert "s3cr3tvalue" not in json.dumps(ctx.store.get(job["id"]))
    with pytest.raises(PydanticValidationError):
        ctx.enqueue("site.set_config", {"site": "a.example.com", "key": "db_password", "value": "x"})
    assert run_job(ctx, worker, "site.migrate", {"site": "a.example.com"})["status"] == "success"
    assert runner.called("--site a.example.com migrate")


def test_site_job_on_missing_site(ctx, worker):
    with pytest.raises(NotFound):
        ctx.enqueue("site.migrate", {"site": "nope.example.com"}, precheck=True)
    job = run_job(ctx, worker, "site.migrate", {"site": "nope.example.com"})
    assert job["status"] == "failure" and "not found" in job["error"]


def test_site_drop(ctx, worker, runner):
    make_bench(ctx, "erp-v15")
    site_dir = make_site(ctx, "erp-v15", "a.example.com")
    ctx.proxy.write_route("a.example.com", [], "erp-v15")

    def backup(call):
        d = site_dir / "private" / "backups"
        d.mkdir(parents=True, exist_ok=True)
        (d / "20261103_213000-a_example_com-database.sql.gz").write_bytes(b"sql")
        (d / "20261103_213000-a_example_com-files.tar").write_bytes(b"pub")

    def drop(call):
        import shutil
        shutil.rmtree(site_dir)

    runner.on(["backup", "--with-files"], result=side_effect(backup))
    runner.on("drop-site", result=side_effect(drop))
    job = run_job(ctx, worker, "site.drop", {"site": "a.example.com"})
    assert job["status"] == "success", job
    args = runner.find("drop-site")[0].args
    assert "--force" in args and args[args.index("--db-root-password") + 1] == DB_PW
    assert cmd_index(runner, "backup --with-files") < cmd_index(runner, "drop-site")
    assert job["result"]["uploaded"] is False  # no rclone remote configured
    assert (ctx.settings.backups_dir / "erp-v15" / "a.example.com").is_dir()
    assert not ctx.proxy.has_route("a.example.com")
    assert DB_PW not in json.dumps(job)
    # idempotent: dropping again succeeds without commands
    runner.reset()
    job = run_job(ctx, worker, "site.drop", {"site": "a.example.com", "skip_backup": True})
    assert job["status"] == "success" and not runner.called("drop-site")


# ---- control-plane shaped params (exactly what nexaploy.api.agent.poll hands out) -----------
def test_control_plane_param_shapes(ctx):
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", "a.example.com")
    shapes = [
        ("site.create", {"bench": "erp-v15", "name": "new.example.com", "admin_password": "pw123456",
                         "apps": ["erpnext"]}),
        ("site.backup", {"site": "a.example.com", "with_files": True, "upload": True}),
        ("site.restore", {"site": "a.example.com",
                          "remote_path": "nexaploy-backups:srv-test/erp-v15/a.example.com/20261103T213000Z"}),
        ("site.drop", {"site": "a.example.com", "skip_backup": True}),
        ("site.install_app", {"site": "a.example.com", "app": "hrms"}),
        ("site.add_domain", {"site": "a.example.com", "domain": "www.example.com"}),
        ("bench.create", {"name": "erp-v16", "image": IMAGE,
                          "apps_json": [{"url": "https://github.com/frappe/erpnext", "branch": "develop"}]}),
        ("bench.update", {"bench": "erp-v15", "image": NEW_IMAGE}),
        ("image.build", {"major": 15, "tag": IMAGE, "frappe_branch": "version-15",
                         "apps_json": [{"url": "https://github.com/frappe/erpnext", "branch": "version-15"}]}),
    ]
    for kind, params in shapes:
        job = ctx.enqueue(kind, params, origin="control_plane", remote_id=f"AJ-{kind}")
        assert job["status"] == "queued" and job["origin"] == "control_plane", kind
        assert job["remote_id"] == f"AJ-{kind}"


def test_image_build(ctx, worker, runner):
    script = ctx.settings.build_script
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/bash\n")
    runner.on(["image", "inspect"], returncode=1)
    runner.on("build.sh", stdout=f"step 1\nstep 2\n{IMAGE}\n")
    job = run_job(ctx, worker, "image.build", {
        "major": 15, "tag": IMAGE, "frappe_branch": "version-15",
        "apps_json": json.dumps([{"url": "https://github.com/frappe/erpnext", "branch": "version-15"}]),
    })
    assert job["status"] == "success", job
    assert job["result"]["tag"] == IMAGE and job["result"]["built"] is True
    args = runner.find("build.sh")[0].args
    build_dir = ctx.settings.builds_dir / "erp-v15-main-20261103T2130"
    assert args[args.index("--major") + 1] == "15"
    assert args[args.index("--apps-json") + 1] == str(build_dir / "apps.json")
    assert args[args.index("--context") + 1] == str(ctx.settings.images_dir / "frappe_docker")
    assert args[args.index("--log") + 1] == str(build_dir / "build.log")
    assert args[args.index("--frappe-branch") + 1] == "version-15"
    # failure maps exit codes
    runner.on("build.sh", returncode=2)
    job = run_job(ctx, worker, "image.build", {"major": 15, "tag": NEW_IMAGE, "apps_json": []})
    assert job["status"] == "failure" and "invalid input" in job["error"]
    with pytest.raises(PydanticValidationError):
        ctx.enqueue("image.build", {"major": 13, "tag": IMAGE, "apps_json": []})
    with pytest.raises(PydanticValidationError):
        ctx.enqueue("image.build", {"major": 15, "tag": IMAGE, "apps_json": [{"url": "file:///etc"}]})


def test_runner_result_helpers():
    r = Result(1, "", "bad", ["x"])
    assert not r.ok
    with pytest.raises(Exception):
        r.check()



def test_new_site_flag_follows_frappe_version(ctx, worker, runner):
    """v15+/v16 get --mariadb-user-host-login-scope=% (v16 deprecates --no-mariadb-socket);
    v14 (no such option in `bench new-site --help`) keeps --no-mariadb-socket."""
    make_bench(ctx, "erp-v16")
    bench_running(runner)
    runner.on("list-apps", returncode=1, stderr="no site")
    _script_new_site(ctx, runner, bench="erp-v16")
    runner.on("--help", stdout="Options:\n  --mariadb-user-host-login-scope TEXT\n")
    job = run_job(ctx, worker, "site.create", {
        "bench": "erp-v16", "name": "v16.example.com", "admin_password": "Adm1nPass!", "apps": [],
    })
    assert job["status"] == "success", (job["error"], [s["output"][-300:] for s in job["steps"] if s["status"]=="failure"])
    args = [c for c in runner.find("new-site") if "--help" not in c.args][0].args
    assert "--mariadb-user-host-login-scope=%" in args and "--no-mariadb-socket" not in args
