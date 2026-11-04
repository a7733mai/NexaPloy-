"""Structural tests for the Nexaploy bench template (ARCHITECTURE.md §3, §7).

Run: python -m pytest bench-template/tests
Requires: jinja2, pyyaml, pytest. `docker compose config` checks run only
when a docker binary with the compose plugin is available.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import jinja2
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
BENCH = "erp-v15-main"
FRAPPE_SERVICES = [
    "backend",
    "frontend",
    "websocket",
    "queue-short",
    "queue-long",
    "queue-default",
    "scheduler",
]
REDIS_SERVICES = ["redis-cache", "redis-queue"]
DB_SERVICES = ["backend", "queue-short", "queue-long", "queue-default", "scheduler", "configurator"]
SITES = "./sites:/home/frappe/frappe-bench/sites"
LOGS = "./logs:/home/frappe/frappe-bench/logs"


def _env() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(ROOT)),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


def render(path: str, **ctx) -> str:
    return _env().get_template(path).render(**ctx)


@pytest.fixture(scope="module")
def bench_text() -> str:
    return render("bench/docker-compose.yml.j2", bench=BENCH)


@pytest.fixture(scope="module")
def bench(bench_text) -> dict:
    return yaml.safe_load(bench_text)


@pytest.fixture(scope="module")
def proxy() -> dict:
    return yaml.safe_load((ROOT / "proxy/docker-compose.yml").read_text())


@pytest.fixture(scope="module")
def db() -> dict:
    return yaml.safe_load((ROOT / "db/docker-compose.yml").read_text())


def parse_env(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            k, _, v = line.partition("=")
            out[k] = v
    return out


# --------------------------------------------------------------------------
# Tree
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "rel",
    [
        "bench/docker-compose.yml.j2",
        "bench/env.example",
        "db/docker-compose.yml",
        "db/env.example",
        "proxy/docker-compose.yml",
        "proxy/env.example",
        "proxy/config/traefik/dynamic.yml",
        "site-route.yml.j2",
        "README.md",
    ],
)
def test_tree(rel):
    assert (ROOT / rel).is_file(), rel


# --------------------------------------------------------------------------
# Bench compose
# --------------------------------------------------------------------------
def test_bench_requires_bench_var():
    with pytest.raises(jinja2.UndefinedError):
        render("bench/docker-compose.yml.j2")


def test_bench_accepts_extra_context():
    text = render(
        "bench/docker-compose.yml.j2",
        bench=BENCH,
        image="x",
        frappe_uid=3113,
        frappe_gid=3113,
        redis_uid=3102,
        redis_gid=3102,
        tz="UTC",
        db_host="nexaploy-db",
        db_port=3306,
        backend_workers=2,
        backend_cpus=2,
        backend_mem="4G",
        worker_cpus=1,
        worker_mem="1G",
    )
    assert yaml.safe_load(text)["name"] == BENCH


def test_bench_services(bench):
    assert set(bench["services"]) == set(FRAPPE_SERVICES + REDIS_SERVICES + ["configurator"])
    assert bench["name"] == BENCH


def test_bench_container_names(bench):
    for svc in FRAPPE_SERVICES + REDIS_SERVICES:
        assert bench["services"][svc]["container_name"] == f"{BENCH}-{svc}"
    # configurator is a `run --rm` one-shot: no fixed name (would clash).
    assert "container_name" not in bench["services"]["configurator"]


def test_bench_no_db_and_no_traefik_labels(bench_text, bench):
    assert "mariadb" not in bench_text.lower().replace("nexaploy-db", "")
    assert "traefik" not in bench_text.lower()
    for svc in bench["services"].values():
        assert "labels" not in svc
        assert "ports" not in svc  # nothing published on the host


def test_bench_networks(bench):
    nets = bench["networks"]
    assert nets["bench-net"] == {"name": f"{BENCH}-net", "internal": True}
    assert nets["proxy-network"] == {"name": "proxy-network", "external": True}
    assert nets["nexaploy-db"] == {"name": "nexaploy-db", "external": True}
    svcs = bench["services"]
    assert set(svcs["frontend"]["networks"]) == {"bench-net", "proxy-network"}
    for name in DB_SERVICES:
        assert set(svcs[name]["networks"]) == {"bench-net", "nexaploy-db"}, name
    for name in ["websocket", *REDIS_SERVICES]:
        assert svcs[name]["networks"] == ["bench-net"], name
    # Only the frontend touches the proxy network.
    for name, svc in svcs.items():
        if name != "frontend":
            assert "proxy-network" not in svc["networks"], name


def test_bench_mounts(bench):
    svcs = bench["services"]
    for name in ["backend", "websocket", "queue-short", "queue-long", "queue-default", "scheduler", "configurator"]:
        vols = svcs[name]["volumes"]
        assert SITES in vols and LOGS in vols, name
    assert SITES + ":ro" in svcs["frontend"]["volumes"]
    assert "./redis-queue:/data" in svcs["redis-queue"]["volumes"]
    assert "volumes" not in svcs["redis-cache"]
    # No absolute host paths: everything is relative to the bench dir.
    for name, svc in svcs.items():
        for v in svc.get("volumes", []):
            assert not v.startswith("/"), (name, v)


def test_bench_uids(bench):
    for name in FRAPPE_SERVICES + ["configurator"]:
        assert bench["services"][name]["user"] == "${FRAPPE_UID:-3113}:${FRAPPE_GID:-3113}", name
    for name in REDIS_SERVICES:
        assert bench["services"][name]["user"] == "${REDIS_UID:-3102}:${REDIS_GID:-3102}", name
        assert bench["services"][name]["image"] == "${REDIS_IMAGE:-redis:7-alpine}"


def test_bench_hardening(bench):
    for name, svc in bench["services"].items():
        assert "no-new-privileges:true" in svc["security_opt"], name
        assert svc["cap_drop"] == ["ALL"], name
        assert svc["logging"]["driver"] == "local", name
        assert "limits" in svc["deploy"]["resources"], name
        assert svc["tmpfs"], name
        if name == "configurator":
            assert svc["restart"] == "no"
            assert svc["profiles"] == ["configurator"]
        else:
            assert svc["restart"] == "unless-stopped", name
            assert "healthcheck" in svc, name
        if name != "frontend":
            assert svc["read_only"] is True, name
    limits = bench["services"]["backend"]["deploy"]["resources"]["limits"]
    assert limits == {"cpus": "${BACKEND_CPUS:-2}", "memory": "${BACKEND_MEM:-4G}"}
    for w in ["queue-short", "queue-long", "queue-default"]:
        wl = bench["services"][w]["deploy"]["resources"]["limits"]
        assert wl == {"cpus": "${WORKER_CPUS:-1}", "memory": "${WORKER_MEM:-1G}"}


def test_bench_commands(bench):
    s = bench["services"]
    backend = s["backend"]["command"]
    assert "--bind=0.0.0.0:8000" in backend
    assert "--workers=${BACKEND_WORKERS:-2}" in backend
    assert backend[-1] == "frappe.app:application"
    assert s["frontend"]["command"] == ["nginx-entrypoint.sh"]
    assert s["websocket"]["command"][0] == "node"
    assert s["scheduler"]["command"] == ["bench", "schedule"]
    for q in ["short", "long", "default"]:
        assert s[f"queue-{q}"]["command"] == ["bench", "worker", "--queue", q]
    assert "--appendonly" in s["redis-queue"]["command"]
    i = s["redis-queue"]["command"].index("--appendonly")
    assert s["redis-queue"]["command"][i + 1] == "yes"
    for svc in s.values():
        assert svc.get("image") in ("${IMAGE:?IMAGE must be set in .env}", "${REDIS_IMAGE:-redis:7-alpine}")


def test_frontend_env(bench):
    env = bench["services"]["frontend"]["environment"]
    assert env["BACKEND"] == f"{BENCH}-backend:8000"
    assert env["SOCKETIO"] == f"{BENCH}-websocket:9000"
    # compose-escaped nginx var: the proxy sets X-Frappe-Site-Name per site route
    assert env["FRAPPE_SITE_NAME_HEADER"] == "$$http_x_frappe_site_name"
    for k in ["UPSTREAM_REAL_IP_ADDRESS", "UPSTREAM_REAL_IP_HEADER", "UPSTREAM_REAL_IP_RECURSIVE", "CLIENT_MAX_BODY_SIZE"]:
        assert k in env


def test_configurator(bench):
    c = bench["services"]["configurator"]
    script = c["command"][0]
    assert c["entrypoint"] == ["/usr/local/bin/nexaploy-entrypoint.sh", "bash", "-c"]
    assert c["working_dir"] == "/home/frappe/frappe-bench"  # bench must run from the bench root
    assert 'bench set-config -g db_host "$$DB_HOST"' in script
    assert 'bench set-config -gp db_port "$$DB_PORT"' in script
    assert f"redis_cache \"redis://{BENCH}-redis-cache:6379\"" in script
    assert f"redis_queue \"redis://{BENCH}-redis-queue:6379\"" in script
    assert f"redis_socketio \"redis://{BENCH}-redis-queue:6379\"" in script
    assert "socketio_port 9000" in script
    assert "apps.txt" in script
    assert c["environment"]["DB_HOST"] == "${DB_HOST:-nexaploy-db}"
    assert c["environment"]["DB_PORT"] == "${DB_PORT:-3306}"


def test_bench_env_example():
    env = parse_env(ROOT / "bench/env.example")
    required = {
        "BENCH_NAME", "IMAGE", "FRAPPE_UID", "FRAPPE_GID", "REDIS_UID", "REDIS_GID",
        "TZ", "DB_HOST", "DB_PORT", "BACKEND_WORKERS", "BACKEND_CPUS", "BACKEND_MEM",
    }
    assert required <= set(env)
    assert env["FRAPPE_UID"] == env["FRAPPE_GID"] == "3113"
    assert env["REDIS_UID"] == env["REDIS_GID"] == "3102"
    assert re.fullmatch(r"[a-z][a-z0-9-]{2,30}", env["BENCH_NAME"])


# --------------------------------------------------------------------------
# Shared DB
# --------------------------------------------------------------------------
def test_db(db):
    assert db["name"] == "nexaploy-db"
    assert list(db["services"]) == ["mariadb"]
    m = db["services"]["mariadb"]
    # 11.8 LTS: the Frappe v16 image ships the 11.8 client, which requires TLS
    assert m["image"] == "${MARIADB_IMAGE:-mariadb:11.8}"
    assert m["container_name"] == "nexaploy-db"
    assert m["user"] == "3101:3101"
    assert "ports" not in m
    assert m["env_file"] == ["${DB_ENV_FILE:-/opt/nexaploy/config/db.env}"]
    assert 'MARIADB_ROOT_PASSWORD="$$DB_ROOT_PASSWORD"' in " ".join(m["entrypoint"])
    cmd = m["command"]
    assert "--character-set-server=utf8mb4" in cmd
    assert "--skip-innodb-read-only-compressed" in cmd
    assert "--innodb-buffer-pool-size=${DB_BUFFER_POOL:-1G}" in cmd
    # slow query log (agent job site.slow_queries reads data/slow.log)
    assert "--slow-query-log=1" in cmd
    assert "--slow-query-log-file=/var/lib/mysql/slow.log" in cmd
    assert "--long-query-time=${DB_LONG_QUERY_TIME:-1}" in cmd
    assert m["volumes"] == ["./data:/var/lib/mysql"]
    assert m["networks"] == ["nexaploy-db"]
    assert db["networks"]["nexaploy-db"] == {"name": "nexaploy-db", "external": True}
    assert "healthcheck" in m
    assert m["logging"]["driver"] == "local"
    assert "no-new-privileges:true" in m["security_opt"]


# --------------------------------------------------------------------------
# Proxy
# --------------------------------------------------------------------------
def _flags(cmd: list[str]) -> dict[str, str]:
    return dict(f[2:].split("=", 1) for f in cmd)


def test_proxy_services(proxy):
    assert proxy["name"] == "nexaploy-proxy"
    s = proxy["services"]
    assert s["traefik"]["container_name"] == "nexaploy-traefik"
    # File provider only: no docker socket anywhere in the proxy stack.
    assert "socket-proxy" not in s
    assert not any("docker.sock" in v for svc in s.values() for v in svc.get("volumes", []))
    assert s["crowdsec"]["profiles"] == ["crowdsec"]
    assert s["traefik"]["user"] == "3100:3100"
    assert s["traefik"]["image"].endswith("traefik:v3.3}")
    for name in ["traefik"]:
        assert "profiles" not in s[name]
        assert "no-new-privileges:true" in s[name]["security_opt"]
        assert s[name]["logging"]["driver"] == "local"
        assert s[name]["restart"] == "unless-stopped"
    # Only Traefik publishes ports.
    assert [n for n, v in s.items() if "ports" in v] == ["traefik"]


def test_traefik_static_config(proxy):
    t = proxy["services"]["traefik"]
    f = _flags(t["command"])
    assert f["providers.file.directory"] == "/etc/traefik/dynamic"
    assert f["providers.file.watch"] == "true"
    # redirect to the PUBLIC https port, not the websecure entrypoint (container :8443)
    assert f["entrypoints.web.http.redirections.entrypoint.to"] == ":${HTTPS_PORT:-443}"
    assert not any(k.startswith("providers.docker") for k in f)
    # the file provider does not recurse: route files are mounted flat
    assert "./config/sites:/etc/traefik/dynamic:ro" in t["volumes"]
    assert f["entrypoints.web.http.redirections.entrypoint.scheme"] == "https"
    assert "entrypoints.websecure.address" in f
    assert f["certificatesresolvers.letsencrypt.acme.storage"] == "/data/certificates/acme.json"
    assert f["certificatesresolvers.letsencrypt.acme.httpchallenge.entrypoint"] == "web"
    assert f["accesslog.format"] == "json"
    assert f["accesslog.filepath"].startswith("/logs/")
    assert f["metrics.prometheus"] == "true"
    assert f["ping"] == "true"
    vols = t["volumes"]
    assert "./data:/data" in vols and "./logs:/logs" in vols
    assert "traefik" in t["healthcheck"]["test"] and "--ping" in t["healthcheck"]["test"]
    assert set(t["networks"]) == {"proxy-network"}
    assert proxy["networks"]["proxy-network"] == {"name": "proxy-network", "external": True}


def test_dynamic_middlewares():
    dyn = yaml.safe_load((ROOT / "proxy/config/traefik/dynamic.yml").read_text())
    mws = dyn["http"]["middlewares"]
    assert {"security-headers", "rate-limit", "compress"} <= set(mws)
    assert "headers" in mws["security-headers"]
    assert "rateLimit" in mws["rate-limit"]
    assert "compress" in mws["compress"]


def test_env_examples_parse():
    for rel in ["proxy/env.example", "db/env.example"]:
        assert parse_env(ROOT / rel)
    assert "ACME_EMAIL" in parse_env(ROOT / "proxy/env.example")
    assert "DB_BUFFER_POOL" in parse_env(ROOT / "db/env.example")


# --------------------------------------------------------------------------
# Site route
# --------------------------------------------------------------------------
def route(**ctx) -> dict:
    return yaml.safe_load(render("site-route.yml.j2", **ctx))


def test_route_single_domain():
    r = route(site="erp.example.com", domains=["erp.example.com"], bench=BENCH)
    routers, services = r["http"]["routers"], r["http"]["services"]
    assert list(routers) == ["site-erp-example-com"]
    rt = routers["site-erp-example-com"]
    assert rt["rule"] == "Host(`erp.example.com`)"
    assert rt["entryPoints"] == ["websecure"]
    assert rt["service"] == "svc-erp-example-com"
    assert rt["middlewares"] == ["site-erp-example-com-name@file", "security-headers@file", "compress@file"]
    mw = r["http"]["middlewares"]["site-erp-example-com-name"]
    assert mw["headers"]["customRequestHeaders"] == {"X-Frappe-Site-Name": "erp.example.com"}
    assert rt["tls"] == {"certResolver": "letsencrypt"}
    assert list(services) == ["svc-erp-example-com"]
    assert services["svc-erp-example-com"]["loadBalancer"]["servers"] == [
        {"url": f"http://{BENCH}-frontend:8080"}
    ]


def test_route_multi_domain_dedup_and_primary():
    r = route(site="a.example.com", domains=["www.shop.io", "a.example.com", "www.shop.io"], bench="b-v14")
    routers = r["http"]["routers"]
    # one router per host (own certificate each), primary first, duplicates dropped
    assert list(routers) == ["site-a-example-com", "site-a-example-com--www-shop-io"]
    assert routers["site-a-example-com"]["rule"] == "Host(`a.example.com`)"
    custom = routers["site-a-example-com--www-shop-io"]
    assert custom["rule"] == "Host(`www.shop.io`)"
    assert custom["service"] == "svc-a-example-com"
    assert custom["tls"] == {"certResolver": "letsencrypt"}
    assert custom["middlewares"] == ["site-a-example-com-name@file", "security-headers@file", "compress@file"]
    assert list(r["http"]["services"]) == ["svc-a-example-com"]
    assert list(r["http"]["middlewares"]) == ["site-a-example-com-name"]
    assert r["http"]["services"]["svc-a-example-com"]["loadBalancer"]["servers"][0]["url"] == "http://b-v14-frontend:8080"


def test_route_strict_undefined():
    with pytest.raises(jinja2.UndefinedError):
        render("site-route.yml.j2", site="a.example.com", bench=BENCH)


# --------------------------------------------------------------------------
# Optional: docker compose config
# --------------------------------------------------------------------------
def _have_compose() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=20).returncode == 0
    except Exception:
        return False


needs_compose = pytest.mark.skipif(not _have_compose(), reason="docker compose not available")


def _compose_config(workdir: Path, extra_env: dict | None = None, profiles: str = "") -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("COMPOSE_")}
    env.update(extra_env or {})
    if profiles:
        env["COMPOSE_PROFILES"] = profiles
    res = subprocess.run(
        ["docker", "compose", "config", "-q"], cwd=workdir, env=env, capture_output=True, text=True, timeout=60
    )
    assert res.returncode == 0, res.stderr


@needs_compose
def test_compose_config_bench(tmp_path, bench_text):
    (tmp_path / "docker-compose.yml").write_text(bench_text)
    shutil.copy(ROOT / "bench/env.example", tmp_path / ".env")
    _compose_config(tmp_path)
    _compose_config(tmp_path, profiles="configurator")


@needs_compose
def test_compose_config_db(tmp_path):
    shutil.copy(ROOT / "db/docker-compose.yml", tmp_path / "docker-compose.yml")
    shutil.copy(ROOT / "db/env.example", tmp_path / ".env")
    (tmp_path / "db.env").write_text("DB_ROOT_PASSWORD=test\n")
    _compose_config(tmp_path, {"DB_ENV_FILE": str(tmp_path / "db.env")})


@needs_compose
def test_compose_config_proxy(tmp_path):
    shutil.copytree(ROOT / "proxy", tmp_path / "proxy")
    shutil.copy(ROOT / "proxy/env.example", tmp_path / "proxy/.env")
    _compose_config(tmp_path / "proxy")
    _compose_config(tmp_path / "proxy", profiles="crowdsec")
