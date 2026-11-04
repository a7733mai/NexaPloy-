"""Site lifecycle jobs (ARCHITECTURE.md §10, owner: lifecycle).

- ``site.clone`` ``{bench, site, target_site, scrub}``: staging copy of a site on the same
  bench (local backup -> new-site -> restore --force -> encryption key -> scrub -> migrate
  -> route). Re-running it on an existing staging site refreshes the copy.
- ``site.rename`` ``{bench, site, new_name, keep_old_domain}``: maintenance on -> move
  ``sites/<old>`` to ``sites/<new>`` -> site_config (host_name, domains) -> route -> off.

Cross-server moves are a chain of the existing ``site.backup`` / ``site.restore`` (with
``bench``) / ``site.drop`` jobs driven by the control plane.
"""

from __future__ import annotations

import secrets
import shutil
from pathlib import Path
from typing import Any

from pydantic import field_validator, model_validator

from .. import validation as v
from ..config import FRAPPE_UID
from ..fsutil import chown, ensure_dir
from ..runtime.backup import classify
from ..runtime.lifecycle import (
    DB_CONTAINER, RESTORE_STAGING, SCRUB_SQL, STAGING_CONFIG_KEY, db_exec_command, rename_config,
    rename_state, restore_args, scrub_config,
)
from ..runtime.site import SiteManager
from .base import BaseParams, Conflict, NotFound
from .site_jobs import _SiteJob


# --------------------------------------------------------------------------- clone
class SiteCloneParams(BaseParams):
    bench: str
    site: str
    target_site: str
    scrub: bool = True

    @field_validator("bench")
    @classmethod
    def _bench(cls, value: str) -> str:
        return v.bench_name(value)

    @field_validator("site", "target_site")
    @classmethod
    def _site(cls, value: str) -> str:
        return v.site_name(value.lower())

    @model_validator(mode="after")
    def _distinct(self) -> "SiteCloneParams":
        if self.site == self.target_site:
            raise ValueError("target_site must differ from site")
        return self


class SiteClone(_SiteJob):
    """Staging copy of ``site`` as ``target_site`` on the same bench."""

    kind = "site.clone"
    steps = ("check", "backup", "stage", "new_site", "restore", "restore_config", "scrub", "migrate",
             "write_route")
    Params = SiteCloneParams

    @property
    def target(self) -> str:
        return self.p.target_site

    @classmethod
    def precheck(cls, ctx, params: dict[str, Any]) -> None:
        if not ctx.benches.exists(params["bench"]):
            raise NotFound(f"bench {params['bench']!r} not found")
        if ctx.sites.find_bench(params["site"]) != params["bench"]:
            raise NotFound(f"site {params['site']!r} not found on bench {params['bench']}")
        owner = ctx.sites.find_bench(params["target_site"])
        if owner and owner != params["bench"]:
            raise Conflict(f"{params['target_site']} already exists on bench {owner}")
        if owner and ctx.sites.site_config(owner, params["target_site"]).get(STAGING_CONFIG_KEY) != params["site"]:
            raise Conflict(f"{params['target_site']} exists and is not a staging copy of {params['site']}")
        host_owner = ctx.sites.all_hosts().get(params["target_site"])
        if host_owner and host_owner != params["target_site"]:
            raise Conflict(f"{params['target_site']} is already a domain of site {host_owner}")

    def step_check(self) -> None:
        bench = self.p.bench
        if not self.ctx.benches.exists(bench):
            self.fail(f"bench {bench} does not exist")
        if not self.ctx.sites.exists(bench, self.site):
            self.fail(f"site {self.site} not found on bench {bench}")
        owner = self.ctx.sites.find_bench(self.target)
        if owner and owner != bench:
            self.fail(f"{self.target} already exists on bench {owner}")
        if owner and not self.result.get("target_created"):
            marker = self.ctx.sites.site_config(bench, self.target).get(STAGING_CONFIG_KEY)
            if marker != self.site:
                self.fail(f"{self.target} exists and is not a staging copy of {self.site}; refusing to overwrite it")
            self.result["refresh"] = True
        host_owner = self.ctx.sites.all_hosts().get(self.target)
        if host_owner and host_owner != self.target:
            self.fail(f"{self.target} is already a domain of site {host_owner}")
        self.result.update({"bench": bench, "target_site": self.target})
        self.log(f"{'refreshing' if self.result.get('refresh') else 'creating'} {self.target} from {self.site} on {bench}")

    def step_backup(self) -> None:
        self.take_backup(self.p.bench, with_files=True)

    def step_stage(self) -> None:
        self.stage_backup(self.p.bench)

    def step_new_site(self) -> None:
        bench = self.p.bench
        if self.ctx.sites.exists(bench, self.target):
            self.skip(f"{self.target} exists")
        else:
            password = self.secret(secrets.token_urlsafe(18))  # replaced by the restored database
            db_host = self.ctx.benches.read_env(bench).get("DB_HOST") or self.settings.bench.db_host
            args = SiteManager.new_site_args(self.target, password, self.db_root_password(), db_host,
                                             legacy_host_flag=self.ctx.sites.legacy_host_flag(bench))
            self.check(self.ctx.sites.bench(bench, args), "bench new-site")
        self.result["target_created"] = True
        self._write_target_config(lambda cfg: {**cfg, STAGING_CONFIG_KEY: self.site})

    def _write_target_config(self, change) -> bool:
        bench = self.p.bench
        current = self.ctx.sites.site_config(bench, self.target)
        new = change(dict(current))
        if new == current:
            return False
        self.ctx.sites.write_site_config(bench, self.target, new)
        return True

    def _backup_files(self) -> dict[str, str]:
        files: dict[str, str] = {}
        for f in (self.result.get("manifest") or {}).get("files", []):
            kind = f.get("type") or classify(f.get("name", ""))
            if kind:
                files[kind] = f["name"]
        if "database" not in files:
            self.fail("staged backup has no database dump")
        return files

    def step_restore(self) -> None:
        bench = self.p.bench
        files = self._backup_files()
        src = Path(self.result["path"])
        staging = self.ctx.benches.sites_dir(bench) / RESTORE_STAGING / self.id
        ensure_dir(staging, FRAPPE_UID, FRAPPE_UID, 0o750)
        try:
            for name in files.values():
                shutil.copy2(src / name, staging / name)
                chown(staging / name, FRAPPE_UID, FRAPPE_UID)
            cpath = lambda key: (  # noqa: E731
                self.ctx.sites.container_path(bench, staging / files[key]) if key in files else None
            )
            args = restore_args(self.target, cpath("database"), cpath("public"), cpath("private"),
                                self.db_root_password())
            self.check(self.ctx.sites.bench(bench, args, timeout=6 * 3600), "bench restore")
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        self.result["restored"] = True

    def step_restore_config(self) -> None:
        """Same encryption_key as the source, so encrypted fields (passwords) stay readable."""
        bench = self.p.bench
        key = self.ctx.sites.site_config(bench, self.site).get("encryption_key")
        if not key:
            self.skip("source has no encryption_key")
            return
        self.secret(key)
        if self.ctx.sites.site_config(bench, self.target).get("encryption_key") == key:
            self.skip("encryption_key already matches")
            return
        res = self.ctx.sites.bench(bench, ["--site", self.target, "set-config", "encryption_key", key], timeout=300)
        self.check(res, "set encryption_key")

    def step_scrub(self) -> None:
        if not self.p.scrub:
            self.skip("scrub not requested")
            return
        changed = self._write_target_config(lambda cfg: scrub_config(cfg, self.site))
        self.log("site_config: mute_emails=1, pause_scheduler=1, maintenance_mode=0"
                 if changed else "already done: site_config scrubbed")
        db_name = self.ctx.sites.site_config(self.p.bench, self.target).get("db_name")
        if not db_name:
            self.fail(f"{self.target} has no db_name in site_config.json")
        try:
            argv = db_exec_command(db_name, SCRUB_SQL, DB_CONTAINER)
        except v.ValidationError as exc:
            self.fail(str(exc))
        res = self.ctx.runner.run(argv, env={"MYSQL_PWD": self.db_root_password()}, timeout=300)
        self.check(res, "disable email accounts")
        self.log("email accounts: outgoing and incoming disabled")

    def step_migrate(self) -> None:
        self.check(self.ctx.sites.migrate(self.p.bench, self.target), "bench migrate")

    def step_write_route(self) -> None:
        bench = self.p.bench
        domains = self.ctx.sites.domains(bench, self.target)
        changed = self.ctx.proxy.write_route(self.target, domains, bench)
        self.log(f"route {'written' if changed else 'unchanged'}: {self.target} -> {bench}-frontend")
        self.result["domains"] = domains


# --------------------------------------------------------------------------- rename
class SiteRenameParams(BaseParams):
    bench: str
    site: str
    new_name: str
    keep_old_domain: bool = True

    @field_validator("bench")
    @classmethod
    def _bench(cls, value: str) -> str:
        return v.bench_name(value)

    @field_validator("site", "new_name")
    @classmethod
    def _site(cls, value: str) -> str:
        return v.site_name(value.lower())

    @model_validator(mode="after")
    def _distinct(self) -> "SiteRenameParams":
        if self.site == self.new_name:
            raise ValueError("new_name must differ from site")
        return self


class SiteRename(_SiteJob):
    """Rename a site in place: same database, same files, new directory and route."""

    kind = "site.rename"
    steps = ("check", "maintenance_on", "move", "update_config", "write_route", "clear_cache", "maintenance_off")
    Params = SiteRenameParams

    @property
    def new(self) -> str:
        return self.p.new_name

    @classmethod
    def precheck(cls, ctx, params: dict[str, Any]) -> None:
        bench, old, new = params["bench"], params["site"], params["new_name"]
        if not ctx.benches.exists(bench):
            raise NotFound(f"bench {bench!r} not found")
        if ctx.sites.find_bench(old) != bench:
            raise NotFound(f"site {old!r} not found on bench {bench}")
        if ctx.sites.find_bench(new):
            raise Conflict(f"site {new} already exists")
        owner = ctx.sites.all_hosts().get(new)
        if owner and owner != old:
            raise Conflict(f"{new} is already a domain of site {owner}")

    def _state(self) -> str:
        return rename_state(self.ctx.benches.sites_dir(self.p.bench), self.site, self.new)

    def _current(self) -> str:
        return self.new if self._state() == "moved" else self.site

    def _set_maintenance(self, value: int) -> bool:
        name = self._current()
        current = self.ctx.sites.site_config(self.p.bench, name)
        if current.get("maintenance_mode", 0) == value:
            return False
        self.ctx.sites.write_site_config(self.p.bench, name, {**current, "maintenance_mode": value})
        return True

    def step_check(self) -> None:
        bench = self.p.bench
        if not self.ctx.benches.exists(bench):
            self.fail(f"bench {bench} does not exist")
        state = self._state()
        if state == "conflict":
            self.fail(f"both sites/{self.site} and sites/{self.new} exist on {bench}; resolve manually")
        if state == "missing":
            self.fail(f"site {self.site} not found on bench {bench}")
        if state != "moved" and self.ctx.storage.is_offloaded(self.site):
            self.fail(f"{self.site}: files are offloaded to object storage (bucket mounts); "
                      "move them back (site.storage_migrate to_local) before renaming")
        other = self.ctx.sites.find_bench(self.new)
        if other and other != bench:
            self.fail(f"{self.new} already exists on bench {other}")
        owner = self.ctx.sites.all_hosts().get(self.new)
        if owner and owner not in (self.site, self.new):
            self.fail(f"{self.new} is already a domain of site {owner}")
        if state == "moved":
            self.log(f"sites/{self.new} already exists and sites/{self.site} is gone: resuming a rename")
        elif "maintenance_before" not in self.result:
            self.result["maintenance_before"] = self.ctx.sites.site_config(bench, self.site).get("maintenance_mode", 0)
        self.result.update({"bench": bench, "old_name": self.site, "new_name": self.new})

    def step_maintenance_on(self) -> None:
        if self._set_maintenance(1):
            self.log(f"maintenance_mode on for {self._current()}")
        else:
            self.skip("maintenance_mode already on")

    def step_move(self) -> None:
        sites_dir = self.ctx.benches.sites_dir(self.p.bench)
        state = self._state()
        if state == "moved":
            self.skip(f"sites/{self.new} exists")
            return
        if state != "pending":
            self.fail(f"cannot move sites/{self.site} (state: {state})")
        (sites_dir / self.site).rename(sites_dir / self.new)
        self.log(f"moved sites/{self.site} -> sites/{self.new}")

    def step_update_config(self) -> None:
        bench = self.p.bench
        current = self.ctx.sites.site_config(bench, self.new)
        new = rename_config(current, self.site, self.new, self.p.keep_old_domain)
        if new == current:
            self.skip("site_config already up to date")
            return
        self.ctx.sites.write_site_config(bench, self.new, new)
        self.log(f"site_config.json: host_name/domains updated (keep old name: {self.p.keep_old_domain})")

    def step_write_route(self) -> None:
        bench = self.p.bench
        if self.ctx.proxy.remove_route(self.site):
            self.log(f"route of {self.site} removed")
        domains = self.ctx.sites.domains(bench, self.new)
        changed = self.ctx.proxy.write_route(self.new, domains, bench)
        self.log(f"route {'written' if changed else 'unchanged'}: {', '.join([self.new, *domains])} -> {bench}-frontend")
        self.result["domains"] = domains

    def step_clear_cache(self) -> None:
        self.check(self.ctx.sites.bench(self.p.bench, ["--site", self.new, "clear-cache"], timeout=600), "clear-cache")

    def step_maintenance_off(self) -> None:
        before = self.result.get("maintenance_before", 0) or 0
        if self._set_maintenance(int(before)):
            self.log(f"maintenance_mode restored to {before}")
        else:
            self.skip(f"maintenance_mode already {before}")
