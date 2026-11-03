"""Site operations, always executed inside the bench container (§5.4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import validation as v
from ..config import FRAPPE_UID
from ..fsutil import atomic_write, chown
from ..redact import MASK, is_sensitive
from .bench import BenchManager
from .docker import Docker, Result, parse_app_list

BENCH_DB_ROOT_USER = "root"
CONTAINER_BENCH = "/home/frappe/frappe-bench"


# site_config.json keys the agent never changes through site.update_config (they are owned by
# bench new-site / restore). The control plane applies a stricter user-facing deny-list.
PROTECTED_CONFIG_KEYS = frozenset({
    "db_name", "db_password", "db_type", "db_host", "db_port", "db_user", "db_socket",
    "encryption_key", "admin_password", "root_password",
})
# Always redacted in site.get_config results (in addition to password|secret|token|key).
REDACTED_CONFIG_KEYS = frozenset({"db_password", "encryption_key", "admin_password", "root_password"})


class SiteError(RuntimeError):
    pass


def redact_site_config(config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Mask secret values of a site_config (top level). Returns (config, redacted keys)."""
    out: dict[str, Any] = {}
    redacted: list[str] = []
    for key, value in config.items():
        if key in REDACTED_CONFIG_KEYS or is_sensitive(str(key)):
            out[key] = MASK if value not in (None, "") else value
            redacted.append(key)
        else:
            out[key] = value
    return out, sorted(redacted)


def apply_config_changes(config: dict[str, Any], set_values: dict[str, Any],
                         remove: list[str]) -> tuple[dict[str, Any], list[str], list[str]]:
    """Pure: new config + (keys set/changed, keys removed). Unchanged keys are not reported."""
    new = dict(config)
    changed: list[str] = []
    removed: list[str] = []
    for key, value in set_values.items():
        if key not in new or new[key] != value:
            new[key] = value
            changed.append(key)
    for key in remove:
        if key in new:
            del new[key]
            removed.append(key)
    return new, changed, removed


class SiteManager:
    def __init__(self, benches: BenchManager, docker: Docker):
        self.benches = benches
        self.docker = docker

    # ---- discovery ---------------------------------------------------------------
    def find_bench(self, site: str) -> str | None:
        return self.benches.find_site(site)

    def require_bench(self, site: str) -> str:
        bench = self.find_bench(site)
        if not bench:
            raise SiteError(f"site {site!r} not found on this server")
        return bench

    def site_dir(self, bench: str, site: str) -> Path:
        return self.benches.sites_dir(bench) / v.site_name(site)

    def exists(self, bench: str, site: str) -> bool:
        return (self.site_dir(bench, site) / "site_config.json").exists()

    def site_config(self, bench: str, site: str) -> dict[str, Any]:
        try:
            return json.loads((self.site_dir(bench, site) / "site_config.json").read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def config_path(self, bench: str, site: str) -> Path:
        return self.site_dir(bench, site) / "site_config.json"

    def write_site_config(self, bench: str, site: str, config: dict[str, Any]) -> None:
        """Atomic rewrite in Frappe's format (indent=1, sorted), owned by the frappe user."""
        path = self.config_path(bench, site)
        mode = path.stat().st_mode & 0o7777 if path.exists() else 0o644
        atomic_write(path, json.dumps(config, indent=1, sort_keys=True), mode=mode)
        chown(path, FRAPPE_UID, FRAPPE_UID)

    def domains(self, bench: str, site: str) -> list[str]:
        """Extra domains from site_config ``domains`` (strings or dicts)."""
        out: list[str] = []
        for entry in self.site_config(bench, site).get("domains") or []:
            name = entry.get("domain") if isinstance(entry, dict) else entry
            if isinstance(name, str) and v.SITE_RE.match(name) and name != site and name not in out:
                out.append(name)
        return out

    def all_hosts(self) -> dict[str, str]:
        """Every routed host on this server -> owning site."""
        hosts: dict[str, str] = {}
        for bench in self.benches.names():
            for site in self.benches.sites(bench):
                hosts[site] = site
                for d in self.domains(bench, site):
                    hosts.setdefault(d, site)
        return hosts

    def info(self, bench: str, site: str) -> dict[str, Any]:
        return {"name": site, "bench": bench, "domains": self.domains(bench, site)}

    def list(self, bench: str | None = None) -> list[dict[str, Any]]:
        benches = [bench] if bench else self.benches.names()
        return [self.info(b, s) for b in benches for s in self.benches.sites(b)]

    # ---- command builders (pure: used by jobs, unit-tested) ------------------------
    @staticmethod
    def new_site_args(site: str, admin_password: str, db_root_password: str, db_host: str,
                      force: bool = False) -> list[str]:
        args = [
            "new-site", v.site_name(site),
            "--db-host", db_host,
            "--db-root-username", BENCH_DB_ROOT_USER,
            "--db-root-password", db_root_password,
            "--admin-password", admin_password,
            "--no-mariadb-socket",
        ]
        if force:
            args.append("--force")
        return args

    @staticmethod
    def drop_site_args(site: str, db_root_password: str) -> list[str]:
        return [
            "drop-site", v.site_name(site), "--force", "--no-backup",
            "--db-root-username", BENCH_DB_ROOT_USER,
            "--db-root-password", db_root_password,
        ]

    # ---- operations --------------------------------------------------------------
    def bench(self, bench: str, args: list[str], timeout: float | None = 3600) -> Result:
        return self.docker.bench_cmd(bench, args, timeout=timeout)

    def installed_apps(self, bench: str, site: str) -> list[str] | None:
        """Apps installed on the site, or None when the site is not usable."""
        res = self.bench(bench, ["--site", v.site_name(site), "list-apps"], timeout=300)
        if not res.ok:
            return None
        return parse_app_list(res.stdout)

    def install_app(self, bench: str, site: str, app: str) -> Result:
        return self.bench(bench, ["--site", v.site_name(site), "install-app", v.app_name(app)])

    def uninstall_app(self, bench: str, site: str, app: str) -> Result:
        return self.bench(bench, ["--site", v.site_name(site), "uninstall-app", v.app_name(app), "--yes"])

    def migrate(self, bench: str, site: str) -> Result:
        return self.bench(bench, ["--site", v.site_name(site), "migrate"], timeout=7200)

    def add_domain(self, bench: str, site: str, domain: str) -> Result:
        return self.bench(bench, ["setup", "add-domain", v.domain_name(domain), "--site", v.site_name(site)])

    def remove_domain(self, bench: str, site: str, domain: str) -> Result:
        return self.bench(bench, ["setup", "remove-domain", v.domain_name(domain), "--site", v.site_name(site)])

    def set_config(self, bench: str, site: str, key: str, value: Any) -> Result:
        v.config_key(key)
        if isinstance(value, str):
            args = ["--site", v.site_name(site), "set-config", key, value]
        else:
            args = ["--site", v.site_name(site), "set-config", "-p", key, json.dumps(value)]
        return self.bench(bench, args, timeout=300)

    def container_path(self, bench: str, host_path: Path) -> str:
        """Map a host path under ``benches/<b>/sites`` to the container path."""
        rel = Path(host_path).resolve().relative_to(self.benches.sites_dir(bench).resolve())
        return f"{CONTAINER_BENCH}/sites/{rel.as_posix()}"
