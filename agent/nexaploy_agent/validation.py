"""Name validation (ARCHITECTURE.md §2). Run before any command is built."""

from __future__ import annotations

import re
from typing import Any

BENCH_RE = re.compile(r"^[a-z][a-z0-9-]{2,30}$")
SITE_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
APP_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
CONFIG_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")
ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
BACKUP_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")
# Docker image reference: [registry[:port]/]repo[/repo...]:tag  (tag required).
IMAGE_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?/)?"
    r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*"
    r":[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$"
)
# Path inside the rclone remote: safe characters only, no traversal.
REMOTE_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,512}$")

# site_config keys the agent refuses to change through site.set_config.
PROTECTED_CONFIG_KEYS = frozenset({"db_name", "db_password", "db_type"})


class ValidationError(ValueError):
    """Invalid user-supplied name or parameter."""


def _check(regex: re.Pattern[str], value: Any, what: str) -> str:
    if not isinstance(value, str) or not regex.match(value):
        raise ValidationError(f"invalid {what}: {value!r}")
    return value


def bench_name(value: Any) -> str:
    return _check(BENCH_RE, value, "bench name")


def site_name(value: Any) -> str:
    return _check(SITE_RE, value, "site name")


def domain_name(value: Any) -> str:
    return _check(SITE_RE, value, "domain")


def app_name(value: Any) -> str:
    return _check(APP_RE, value, "app name")


def image_ref(value: Any) -> str:
    return _check(IMAGE_RE, value, "image tag")


def config_key(value: Any) -> str:
    _check(CONFIG_KEY_RE, value, "config key")
    if value in PROTECTED_CONFIG_KEYS:
        raise ValidationError(f"config key {value!r} is managed by the agent")
    return value


def env_key(value: Any) -> str:
    return _check(ENV_KEY_RE, value, "env variable name")


def backup_id(value: Any) -> str:
    return _check(BACKUP_ID_RE, value, "backup id")


def remote_path(value: Any) -> str:
    _check(REMOTE_PATH_RE, value, "remote path")
    parts = value.split("/")
    if any(p in ("..", ".") for p in parts) or value.startswith("/"):
        raise ValidationError(f"invalid remote path: {value!r}")
    return value.strip("/")


def slug(site: str) -> str:
    return site.replace(".", "-")
