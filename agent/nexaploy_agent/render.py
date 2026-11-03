"""Jinja2 rendering of the packaged bench-template files (§3)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATES_ENV = "NEXAPLOY_TEMPLATES"
COMPOSE_TEMPLATE = "bench/docker-compose.yml.j2"
ROUTE_TEMPLATE = "site-route.yml.j2"

# Every key the bench compose template may reference (§3.1); all are always
# passed so StrictUndefined never trips on a template that uses any of them.
COMPOSE_CONTEXT_KEYS = (
    "bench", "image", "frappe_uid", "frappe_gid", "redis_uid", "redis_gid", "tz",
    "db_host", "db_port", "backend_workers", "backend_cpus", "backend_mem",
    "worker_cpus", "worker_mem",
)


def templates_dir() -> Path:
    env = os.environ.get(TEMPLATES_ENV)
    if env:
        return Path(env)
    return Path(__file__).resolve().parent / "templates"


@lru_cache(maxsize=8)
def _environment(path: str) -> Environment:
    return Environment(
        loader=FileSystemLoader(path),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,
        trim_blocks=False,
        lstrip_blocks=False,
    )


def render(template: str, context: dict[str, Any], directory: Path | None = None) -> str:
    env = _environment(str(directory or templates_dir()))
    return env.get_template(template).render(**context)


def render_compose(context: dict[str, Any], directory: Path | None = None) -> str:
    missing = [k for k in COMPOSE_CONTEXT_KEYS if k not in context]
    if missing:
        raise KeyError(f"compose context missing keys: {', '.join(missing)}")
    return render(COMPOSE_TEMPLATE, context, directory)


def route_hosts(site: str, domains: Iterable[str]) -> list[str]:
    hosts: list[str] = []
    for h in [site, *domains]:
        if h and h not in hosts:
            hosts.append(h)
    return hosts


def render_route(site: str, domains: Iterable[str], bench: str, directory: Path | None = None) -> str:
    return render(
        ROUTE_TEMPLATE,
        {"site": site, "domains": route_hosts(site, domains), "bench": bench},
        directory,
    )
