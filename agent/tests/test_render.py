import pytest
import yaml
from jinja2 import UndefinedError

from nexaploy_agent.render import COMPOSE_CONTEXT_KEYS, render, render_compose, render_route, templates_dir

from .conftest import IMAGE, make_bench

SERVICES = {
    "configurator", "backend", "frontend", "websocket", "scheduler",
    "queue-short", "queue-long", "queue-default", "redis-cache", "redis-queue",
}


def test_packaged_templates_present():
    d = templates_dir()
    for rel in ("bench/docker-compose.yml.j2", "site-route.yml.j2"):
        assert (d / rel).is_file(), rel


def test_compose_render_structure(ctx):
    path = make_bench(ctx, "erp-v15")
    doc = yaml.safe_load((path / "docker-compose.yml").read_text())
    assert doc["name"] == "erp-v15"
    assert SERVICES <= set(doc["services"])
    assert "db" not in doc["services"] and "mariadb" not in doc["services"]
    svc = doc["services"]
    for name in SERVICES - {"configurator"}:
        assert svc[name]["container_name"] == f"erp-v15-{name}"
    # frontend on proxy-network, backend/workers on nexaploy-db
    assert "proxy-network" in svc["frontend"]["networks"]
    for name in ("backend", "scheduler", "queue-short", "queue-long", "queue-default"):
        assert "nexaploy-db" in svc[name]["networks"]
        assert any(m.startswith("./sites:") for m in svc[name]["volumes"])
        assert any(m.startswith("./logs:") for m in svc[name]["volumes"])
        assert "no-new-privileges:true" in svc[name]["security_opt"]
    assert doc["networks"]["proxy-network"]["external"] is True
    assert doc["networks"]["nexaploy-db"]["external"] is True
    assert doc["networks"]["bench-net"]["name"] == "erp-v15-net"
    # no Traefik labels on the frontend (file provider routing)
    assert not any("traefik" in str(k) for k in (svc["frontend"].get("labels") or {}))
    # configurator is a one-shot behind a profile
    assert "configurator" in svc["configurator"].get("profiles", ["configurator"])
    assert "redis-queue" in str(svc["redis-queue"]["volumes"])


def test_compose_env_file(ctx):
    path = make_bench(ctx, "erp-v15")
    env = dict(line.split("=", 1) for line in (path / ".env").read_text().splitlines()
               if line and not line.startswith("#"))
    assert env["BENCH_NAME"] == "erp-v15" and env["COMPOSE_PROJECT_NAME"] == "erp-v15"
    assert env["IMAGE"] == IMAGE
    assert env["FRAPPE_UID"] == env["FRAPPE_GID"] == "3113"
    assert env["REDIS_UID"] == env["REDIS_GID"] == "3102"
    for key in ("TZ", "DB_HOST", "DB_PORT", "BACKEND_WORKERS", "BACKEND_CPUS", "BACKEND_MEM",
                "WORKER_CPUS", "WORKER_MEM"):
        assert key in env
    assert env["DB_HOST"] == "nexaploy-db" and env["DB_PORT"] == "3306"
    assert (path / ".env").stat().st_mode & 0o777 == 0o600
    for d in ("sites", "logs", "redis-queue"):
        assert (path / d).is_dir()


def test_compose_context_has_all_keys(ctx):
    c = ctx.benches.context("erp-v15", ctx.benches.default_env("erp-v15", IMAGE))
    assert set(COMPOSE_CONTEXT_KEYS) <= set(c)
    with pytest.raises(KeyError):
        render_compose({"bench": "x"})


def test_strict_undefined(tmp_path):
    (tmp_path / "t.j2").write_text("{{ nope }}")
    with pytest.raises(UndefinedError):
        render("t.j2", {}, tmp_path)


def test_route_render():
    doc = yaml.safe_load(render_route("erp.example.com", ["www.example.com", "erp.example.com"], "erp-v15"))
    router = doc["http"]["routers"]["site-erp-example-com"]
    assert router["service"] == "svc-erp-example-com"
    assert router["entryPoints"] == ["websecure"]
    assert router["tls"]["certResolver"] == "letsencrypt"
    assert router["middlewares"] == ["site-erp-example-com-name@file", "security-headers@file", "compress@file"]
    assert router["rule"] == "Host(`erp.example.com`) || Host(`www.example.com`)"
    servers = doc["http"]["services"]["svc-erp-example-com"]["loadBalancer"]["servers"]
    assert servers == [{"url": "http://erp-v15-frontend:8080"}]


def test_proxy_write_remove(ctx):
    assert ctx.proxy.write_route("a.example.com", [], "erp-v15")
    assert not ctx.proxy.write_route("a.example.com", [], "erp-v15")  # unchanged
    p = ctx.settings.proxy_sites_dir / "a.example.com.yml"
    assert p.exists() and ctx.proxy.routes() == ["a.example.com"]
    assert ctx.proxy.remove_route("a.example.com") and not p.exists()
    assert not ctx.proxy.remove_route("a.example.com")
