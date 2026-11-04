"""Site lifecycle helpers: staging clones and renames (ARCHITECTURE.md §10, owner: lifecycle).

Pure functions (command builders, config rewrites) used by ``jobs/lifecycle_jobs.py`` and
unit-tested without Docker.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .. import validation as v
from .site import BENCH_DB_ROOT_USER

DB_CONTAINER = "nexaploy-db"
# Marker in a staging site's site_config.json: the production site it was cloned from.
STAGING_CONFIG_KEY = "nexaploy_staging_of"
# site_config changes applied to a staging clone (no mails, no scheduler, reachable).
SCRUB_CONFIG = {"mute_emails": 1, "pause_scheduler": 1, "maintenance_mode": 0}
# Never carried over to a staging clone (host/domain keys belong to production).
SCRUB_REMOVE_KEYS = ("host_name", "domains")
# Email accounts of the clone stop sending and fetching mail.
SCRUB_SQL = "UPDATE `tabEmail Account` SET enable_outgoing=0, enable_incoming=0"
DB_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
RESTORE_STAGING = ".nexaploy-restore"


def restore_args(site: str, database: str, public: str | None, private: str | None,
                 db_root_password: str) -> list[str]:
    """``bench --site <site> restore ... --force`` (container paths)."""
    args = ["--site", v.site_name(site), "restore", database]
    if public:
        args += ["--with-public-files", public]
    if private:
        args += ["--with-private-files", private]
    args += ["--db-root-username", BENCH_DB_ROOT_USER, "--db-root-password", db_root_password, "--force"]
    return args


def db_exec_command(db_name: str, sql: str, container: str = DB_CONTAINER) -> list[str]:
    """argv running ``sql`` against ``db_name`` in the shared MariaDB container.

    The root password reaches the docker CLI only through ``MYSQL_PWD`` in its environment.
    """
    if not DB_NAME_RE.match(db_name or ""):
        raise v.ValidationError(f"invalid database name {db_name!r}")
    return ["docker", "exec", "-e", "MYSQL_PWD", container, "mariadb", f"-u{BENCH_DB_ROOT_USER}", db_name,
            "-e", sql]


def scrub_config(config: dict[str, Any], source: str) -> dict[str, Any]:
    """site_config of a staging clone: scrub settings + the staging marker."""
    new = {k: val for k, val in config.items() if k not in SCRUB_REMOVE_KEYS}
    new.update(SCRUB_CONFIG)
    new[STAGING_CONFIG_KEY] = source
    return new


def _domain_name(entry: Any) -> str | None:
    return entry.get("domain") if isinstance(entry, dict) else entry if isinstance(entry, str) else None


def rename_host_name(value: Any, old: str, new: str) -> Any:
    """``host_name`` (``https://old``, ``old``, ``old:8000``) pointing at the old name -> new."""
    if not isinstance(value, str) or not value:
        return value
    if "://" in value:
        parts = urlsplit(value)
        host, sep, port = parts.netloc.partition(":")
        if host != old:
            return value
        return urlunsplit(parts._replace(netloc=new + sep + port))
    host, sep, port = value.partition(":")
    return new + sep + port if host == old else value


def rename_config(config: dict[str, Any], old: str, new: str, keep_old_domain: bool = True) -> dict[str, Any]:
    """site_config.json after renaming ``old`` -> ``new``.

    - ``host_name`` pointing at the old name is rewritten;
    - ``domains``: the new name is dropped (it is now the primary), the old name is added
      (keeps existing links working) when ``keep_old_domain``.
    """
    new_cfg = dict(config)
    if "host_name" in new_cfg:
        new_cfg["host_name"] = rename_host_name(new_cfg["host_name"], old, new)
    domains = [d for d in (config.get("domains") or []) if _domain_name(d) not in (new, old)]
    if keep_old_domain:
        domains.append(old)
    if domains or "domains" in config:
        new_cfg["domains"] = domains
    return new_cfg


def rename_state(sites_dir: Path, old: str, new: str) -> str:
    """Where a rename stands on disk: ``pending`` (old only), ``moved`` (new only),
    ``conflict`` (both), ``missing`` (neither)."""
    old_ok = (Path(sites_dir) / old / "site_config.json").exists()
    new_ok = (Path(sites_dir) / new / "site_config.json").exists()
    if old_ok and not new_ok:
        return "pending"
    if new_ok and not old_ok:
        return "moved"
    if old_ok and new_ok:
        return "conflict"
    return "missing"
