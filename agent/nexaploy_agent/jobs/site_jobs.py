"""site.* jobs (§5.2, §5.4, §5.5)."""

from __future__ import annotations

import json
import secrets
import shutil
from pathlib import Path
from typing import Any

from pydantic import ConfigDict, Field, field_validator, model_validator

from .. import validation as v
from ..config import FRAPPE_UID
from ..fsutil import chown, ensure_dir
from ..runtime.backup import BackupError, classify, new_ts
from ..runtime.site import (
    CONTAINER_BENCH, PROTECTED_CONFIG_KEYS, SiteManager, apply_config_changes, redact_site_config,
)
from .base import BaseParams, Conflict, Job, NotFound, SiteParams

RESTORE_STAGING = ".nexaploy-restore"
ARCHIVED_SITES = f"{CONTAINER_BENCH}/sites/.archived_sites"


BACKUP_FILE_TYPES = {"database": "database", "public": "public", "private": "private", "config": "site_config"}


def backup_file_list(manifest: dict[str, Any], remote_path: str | None) -> list[dict[str, Any]]:
    """Manifest files -> [{file_type, name, size, sha256, remote_key}] for the control plane.

    ``remote_key`` is the object key inside the bucket: the rclone remote is an alias of
    ``<remote>-s3:<bucket>``, so ``<remote>:<prefix>/<bench>/<site>/<ts>`` + ``/<name>``
    is ``<prefix>/<bench>/<site>/<ts>/<name>`` in the bucket (None until uploaded).
    """
    rel = remote_path.partition(":")[2].strip("/") if remote_path else None
    out = []
    for f in manifest.get("files", []):
        kind = f.get("type") or classify(f.get("name", ""))
        out.append({
            "file_type": BACKUP_FILE_TYPES.get(kind or "", kind),
            "name": f.get("name"),
            "size": f.get("size"),
            "sha256": f.get("sha256"),
            "remote_key": f"{rel}/{f.get('name')}" if rel else None,
        })
    return out


class _SiteJob(Job):
    Params = SiteParams

    @property
    def site(self) -> str:
        return self.p.site

    @classmethod
    def precheck(cls, ctx, params: dict[str, Any]) -> None:
        if not ctx.sites.find_bench(params["site"]):
            raise NotFound(f"site {params['site']!r} not found")

    def bench_for_site(self) -> str:
        """Resolve (and remember) the bench hosting the site."""
        bench = self.result.get("bench") or self.ctx.sites.find_bench(self.site)
        if not bench:
            self.fail(f"site {self.site} not found on this server")
        self.result["bench"] = bench
        return bench

    def db_root_password(self) -> str:
        try:
            return self.secret(self.settings.db_root_password())  # type: ignore[return-value]
        except Exception as exc:  # ConfigError
            self.fail(str(exc))
            raise  # unreachable

    def write_route(self) -> None:
        bench = self.bench_for_site()
        domains = self.ctx.sites.domains(bench, self.site)
        changed = self.ctx.proxy.write_route(self.site, domains, bench)
        hosts = ", ".join([self.site, *domains])
        self.log(f"route {'written' if changed else 'unchanged'}: {hosts} -> {bench}-frontend")
        self.result["domains"] = domains

    # Shared backup sequence (site.backup and site.drop) -------------------------
    def take_backup(self, bench: str, with_files: bool) -> None:
        """``bench backup``: record the new files + timestamp in the result."""
        if self.result.get("files"):
            self.skip(f"backup files already produced: {', '.join(self.result['files'])}")
            return
        self.result.setdefault("backup_id", new_ts())
        self.save()
        src = self.ctx.backups.frappe_backup_dir(self.ctx.sites.site_dir(bench, self.site))
        before = self.ctx.backups.snapshot(src)
        args = ["--site", self.site, "backup"]
        if with_files:
            args.append("--with-files")
        self.check(self.ctx.sites.bench(bench, args, timeout=6 * 3600), "bench backup")
        new = sorted(n for n in self.ctx.backups.snapshot(src) - before if classify(n))
        if not any(classify(n) == "database" for n in new):
            self.fail(f"bench backup produced no database dump in {src}")
        self.result["files"] = new

    def stage_backup(self, bench: str) -> None:
        ts = self.result["backup_id"]
        src = self.ctx.backups.frappe_backup_dir(self.ctx.sites.site_dir(bench, self.site))
        try:
            manifest = self.ctx.backups.stage(
                bench, self.site, ts, src, self.result.get("files", []), self.ctx.benches.image(bench)
            )
        except BackupError as exc:
            self.fail(str(exc))
        dest = self.ctx.backups.backup_dir(bench, self.site, ts)
        self.result.update({
            "path": str(dest),
            "manifest": manifest,
            "created_at": manifest["created_at"],
            "size": sum(f["size"] for f in manifest["files"]),
            "backup_files": backup_file_list(manifest, self.result.get("remote_path")),
        })
        self.log(f"staged {len(manifest['files'])} files in {dest}")

    def upload_backup(self, bench: str, upload: bool) -> None:
        ts = self.result["backup_id"]
        if not upload:
            self.skip("upload not requested")
            self.result["uploaded"] = False
            return
        if not self.settings.rclone_remote_configured():
            self.log(
                f"warning: rclone remote {self.settings.backup.remote!r} not configured in "
                f"{self.settings.rclone_conf}; backup kept locally only"
            )
            self.result["uploaded"] = False
            return
        self.check(self.ctx.backups.upload(bench, self.site, ts), "rclone copy")
        remote = self.ctx.backups.remote_dir(bench, self.site, ts)
        self.result.update({"uploaded": True, "remote_path": remote})
        if isinstance(self.result.get("manifest"), dict):
            self.result["manifest"]["remote_path"] = remote
            self.result["backup_files"] = backup_file_list(self.result["manifest"], remote)


# --------------------------------------------------------------------------- create
class SiteCreateParams(BaseParams):
    bench: str
    name: str
    admin_password: str
    apps: list[str] = []

    @field_validator("bench")
    @classmethod
    def _bench(cls, value: str) -> str:
        return v.bench_name(value)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return v.site_name(value)

    @field_validator("admin_password")
    @classmethod
    def _pw(cls, value: str) -> str:
        if len(value) < 6:
            raise ValueError("admin_password must be at least 6 characters")
        return value

    @field_validator("apps")
    @classmethod
    def _apps(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for app in value:
            v.app_name(app)
            if app != "frappe" and app not in out:
                out.append(app)
        return out


class SiteCreate(_SiteJob):
    """bench new-site, install apps, write the Traefik route."""

    kind = "site.create"
    steps = ("check", "new_site", "install_apps", "enable_monitor", "write_route")
    Params = SiteCreateParams

    @property
    def site(self) -> str:
        return self.p.name

    @classmethod
    def precheck(cls, ctx, params: dict[str, Any]) -> None:
        if not ctx.benches.exists(params["bench"]):
            raise NotFound(f"bench {params['bench']!r} not found")
        owner = ctx.sites.find_bench(params["name"])
        if owner:
            raise Conflict(f"site {params['name']} already exists on bench {owner}")
        if params["name"] in ctx.sites.all_hosts():
            raise Conflict(f"{params['name']} is already a domain of another site")

    def step_check(self) -> None:
        bench = self.p.bench
        if not self.ctx.benches.exists(bench):
            self.fail(f"bench {bench} does not exist")
        owner = self.ctx.sites.find_bench(self.site)
        if owner and owner != bench:
            self.fail(f"site {self.site} already exists on bench {owner}")
        hosts = self.ctx.sites.all_hosts()
        if hosts.get(self.site, self.site) != self.site:
            self.fail(f"{self.site} is already a domain of site {hosts[self.site]}")
        self.result["bench"] = bench
        self.secret(self.p.admin_password)

    def step_new_site(self) -> None:
        bench = self.p.bench
        self.secret(self.p.admin_password)
        force = False
        if self.ctx.sites.exists(bench, self.site):
            apps = self.ctx.sites.installed_apps(bench, self.site)
            if apps and "frappe" in apps:
                self.skip(f"site {self.site} exists with apps {', '.join(apps)}")
                return
            self.log("site directory exists but the site is not usable; recreating with --force")
            force = True
        db_host = self.ctx.benches.read_env(bench).get("DB_HOST") or self.settings.bench.db_host
        args = SiteManager.new_site_args(
            self.site, self.p.admin_password, self.db_root_password(), db_host, force=force
        )
        self.check(self.ctx.sites.bench(bench, args), "bench new-site")

    def step_install_apps(self) -> None:
        bench = self.p.bench
        if not self.p.apps:
            self.skip("no apps requested")
            return
        installed = self.ctx.sites.installed_apps(bench, self.site) or []
        for app in self.p.apps:
            if app in installed:
                self.skip(f"{app} installed")
                continue
            self.check(self.ctx.sites.install_app(bench, self.site, app), f"install-app {app}")
        self.result["apps"] = self.ctx.sites.installed_apps(bench, self.site) or []

    def step_enable_monitor(self) -> None:
        """Frappe monitor -> logs/monitor.json.log, the source of per-site CPU usage."""
        enable_monitor(self, self.p.bench)

    def step_write_route(self) -> None:
        self.write_route()


def enable_monitor(job: Job, bench: str) -> bool:
    """``bench --site s set-config -p monitor 1`` unless already on. True when changed."""
    site = job.site  # type: ignore[attr-defined]
    if job.ctx.sites.site_config(bench, site).get("monitor"):
        job.skip("monitor already enabled")
        return False
    job.check(job.ctx.sites.set_config(bench, site, "monitor", 1), "enable monitor")
    return True


# --------------------------------------------------------------------------- apps
class SiteAppParams(SiteParams):
    app: str

    @field_validator("app")
    @classmethod
    def _app(cls, value: str) -> str:
        return v.app_name(value)


class SiteInstallApp(_SiteJob):
    kind = "site.install_app"
    steps = ("install",)
    Params = SiteAppParams

    def step_install(self) -> None:
        bench = self.bench_for_site()
        installed = self.ctx.sites.installed_apps(bench, self.site)
        if installed is None:
            self.fail(f"cannot list apps of {self.site}")
        if self.p.app in installed:
            self.skip(f"{self.p.app} already installed")
            return
        self.check(self.ctx.sites.install_app(bench, self.site, self.p.app), f"install-app {self.p.app}")


class SiteUninstallApp(_SiteJob):
    kind = "site.uninstall_app"
    steps = ("uninstall",)
    Params = SiteAppParams

    @classmethod
    def validate_params(cls, raw: dict[str, Any]) -> dict[str, Any]:
        params = super().validate_params(raw)
        if params["app"] == "frappe":
            raise v.ValidationError("frappe cannot be uninstalled")
        return params

    def step_uninstall(self) -> None:
        bench = self.bench_for_site()
        installed = self.ctx.sites.installed_apps(bench, self.site)
        if installed is None:
            self.fail(f"cannot list apps of {self.site}")
        if self.p.app not in installed:
            self.skip(f"{self.p.app} not installed")
            return
        self.check(self.ctx.sites.uninstall_app(bench, self.site, self.p.app), f"uninstall-app {self.p.app}")


# --------------------------------------------------------------------------- domains
class SiteDomainParams(SiteParams):
    domain: str

    @field_validator("domain")
    @classmethod
    def _domain(cls, value: str) -> str:
        return v.domain_name(value.lower())


class SiteAddDomain(_SiteJob):
    kind = "site.add_domain"
    steps = ("add_domain", "write_route")
    Params = SiteDomainParams

    @classmethod
    def precheck(cls, ctx, params: dict[str, Any]) -> None:
        super().precheck(ctx, params)
        owner = ctx.sites.all_hosts().get(params["domain"])
        if owner and owner != params["site"]:
            raise Conflict(f"{params['domain']} is already used by site {owner}")

    def step_add_domain(self) -> None:
        bench = self.bench_for_site()
        if self.p.domain == self.site:
            self.skip("domain is the site name")
            return
        owner = self.ctx.sites.all_hosts().get(self.p.domain)
        if owner and owner != self.site:
            self.fail(f"{self.p.domain} is already used by site {owner}")
        if self.p.domain in self.ctx.sites.domains(bench, self.site):
            self.skip(f"{self.p.domain} already in site_config domains")
            return
        self.check(self.ctx.sites.add_domain(bench, self.site, self.p.domain), "bench setup add-domain")

    def step_write_route(self) -> None:
        self.write_route()


class SiteRemoveDomain(_SiteJob):
    kind = "site.remove_domain"
    steps = ("remove_domain", "write_route")
    Params = SiteDomainParams

    @classmethod
    def validate_params(cls, raw: dict[str, Any]) -> dict[str, Any]:
        params = super().validate_params(raw)
        if params["domain"] == params["site"]:
            raise v.ValidationError("the primary domain (site name) cannot be removed")
        return params

    def step_remove_domain(self) -> None:
        bench = self.bench_for_site()
        if self.p.domain not in self.ctx.sites.domains(bench, self.site):
            self.skip(f"{self.p.domain} not in site_config domains")
            return
        self.check(self.ctx.sites.remove_domain(bench, self.site, self.p.domain), "bench setup remove-domain")

    def step_write_route(self) -> None:
        self.write_route()


# --------------------------------------------------------------------------- backup
class SiteBackupParams(SiteParams):
    with_files: bool = True
    upload: bool = True


class SiteBackup(_SiteJob):
    kind = "site.backup"
    steps = ("backup", "stage", "upload", "prune")
    Params = SiteBackupParams

    def step_backup(self) -> None:
        self.take_backup(self.bench_for_site(), self.p.with_files)

    def step_stage(self) -> None:
        self.stage_backup(self.bench_for_site())

    def step_upload(self) -> None:
        self.upload_backup(self.bench_for_site(), self.p.upload)

    def step_prune(self) -> None:
        pruned = self.ctx.backups.prune(self.bench_for_site(), self.site)
        self.result["pruned"] = pruned
        self.log(f"pruned {len(pruned)} local backups" if pruned else "nothing to prune")


# --------------------------------------------------------------------------- restore
class SiteRestoreParams(SiteParams):
    backup_id: str | None = None
    remote_path: str | None = None
    bench: str | None = None
    with_files: bool = True

    @field_validator("backup_id")
    @classmethod
    def _bid(cls, value: str | None) -> str | None:
        return v.backup_id(value) if value else None

    @field_validator("bench")
    @classmethod
    def _bench(cls, value: str | None) -> str | None:
        return v.bench_name(value) if value else None

    @field_validator("remote_path")
    @classmethod
    def _remote(cls, value: str | None) -> str | None:
        if not value:
            return None
        v.remote_path(value.partition(":")[2] if ":" in value else value)
        return value

    @model_validator(mode="after")
    def _one_source(self) -> "SiteRestoreParams":
        if bool(self.backup_id) == bool(self.remote_path):
            raise ValueError("exactly one of backup_id or remote_path is required")
        return self


class SiteRestore(_SiteJob):
    """Fetch, verify sha256, bench restore, restore encryption key, migrate, route."""

    kind = "site.restore"
    steps = ("locate", "fetch", "verify", "ensure_site", "restore", "restore_config", "migrate", "write_route")
    Params = SiteRestoreParams

    @classmethod
    def precheck(cls, ctx, params: dict[str, Any]) -> None:
        owner = ctx.sites.find_bench(params["site"])
        bench = params.get("bench")
        if not owner and not bench:
            raise NotFound(f"site {params['site']!r} not found; pass bench to restore as a new site")
        if bench and not ctx.benches.exists(bench):
            raise NotFound(f"bench {bench!r} not found")
        if owner and bench and owner != bench:
            raise Conflict(f"site {params['site']} lives on bench {owner}, not {bench}")
        if params.get("backup_id") and not ctx.backups.find_local(params["site"], params["backup_id"]):
            raise NotFound(f"backup {params['backup_id']} not found locally")

    def step_locate(self) -> None:
        owner = self.ctx.sites.find_bench(self.site)
        bench = self.p.bench or owner
        if not bench:
            self.fail(f"site {self.site} not found; pass bench to restore as a new site")
        if owner and owner != bench:
            self.fail(f"site {self.site} lives on bench {owner}, not {bench}")
        if not self.ctx.benches.exists(bench):
            self.fail(f"bench {bench} does not exist")
        self.result["bench"] = bench
        if self.p.backup_id:
            loc = self.ctx.backups.find_local(self.site, self.p.backup_id)
            if not loc:
                self.fail(f"backup {self.p.backup_id} of {self.site} not found locally")
            self.result["dir"] = str(loc.path)
        else:
            d = self.settings.backups_dir / ".restore" / self.site / self.id
            self.result["dir"] = str(d)
            self.result["remote"] = self.ctx.backups.remote_spec(self.p.remote_path)
        self.log(f"restoring {self.site} on bench {bench} from {self.result.get('remote') or self.result['dir']}")

    def step_fetch(self) -> None:
        d = Path(self.result["dir"])
        if self.p.backup_id:
            self.skip("local backup")
            return
        if (d / "manifest.json").exists():
            self.skip("already fetched")
            return
        self.check(self.ctx.backups.fetch(self.result["remote"], d), "rclone copy")

    def step_verify(self) -> None:
        try:
            manifest = self.ctx.backups.verify(Path(self.result["dir"]))
        except BackupError as exc:
            self.fail(str(exc))
        files: dict[str, str] = {}
        for f in manifest.get("files", []):
            kind = f.get("type") or classify(f["name"])
            if kind:
                files[kind] = f["name"]
        if "database" not in files:
            self.fail("backup has no database dump")
        self.result["files"] = files
        self.log(f"sha256 verified for {len(manifest.get('files', []))} files")

    def step_ensure_site(self) -> None:
        """`bench restore` only restores INTO an existing site (verified on a real bench), so a
        restore as a new site (moves between benches/servers) first creates an empty one; its
        database and admin password are then replaced by the backup."""
        bench = self.result["bench"]
        if self.ctx.sites.exists(bench, self.site):
            self.skip(f"{self.site} exists on {bench}")
            return
        password = self.secret(secrets.token_urlsafe(18))
        db_host = self.ctx.benches.read_env(bench).get("DB_HOST") or self.settings.bench.db_host
        args = SiteManager.new_site_args(self.site, password, self.db_root_password(), db_host)
        self.check(self.ctx.sites.bench(bench, args, timeout=3600), "bench new-site")
        self.result["created_site"] = True

    def step_restore(self) -> None:
        bench = self.result["bench"]
        files: dict[str, str] = self.result["files"]
        src = Path(self.result["dir"])
        staging = self.ctx.benches.sites_dir(bench) / RESTORE_STAGING / self.id
        ensure_dir(staging, FRAPPE_UID, FRAPPE_UID, 0o750)
        for name in files.values():
            shutil.copy2(src / name, staging / name)
            chown(staging / name, FRAPPE_UID, FRAPPE_UID)
        cpath = lambda key: self.ctx.sites.container_path(bench, staging / files[key])  # noqa: E731
        args = ["--site", self.site, "restore", cpath("database")]
        if self.p.with_files and "public" in files:
            args += ["--with-public-files", cpath("public")]
        if self.p.with_files and "private" in files:
            args += ["--with-private-files", cpath("private")]
        args += ["--db-root-username", "root", "--db-root-password", self.db_root_password(), "--force"]
        try:
            self.check(self.ctx.sites.bench(bench, args, timeout=6 * 3600), "bench restore")
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def step_restore_config(self) -> None:
        """Carry the encryption_key over so encrypted fields stay readable."""
        bench = self.result["bench"]
        name = self.result["files"].get("config")
        if not name:
            self.skip("backup has no site_config backup")
            return
        try:
            backup_cfg = json.loads((Path(self.result["dir"]) / name).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            self.log(f"warning: cannot read {name}: {exc}")
            return
        key = backup_cfg.get("encryption_key")
        if not key:
            self.skip("no encryption_key in backup")
            return
        self.secret(key)
        if self.ctx.sites.site_config(bench, self.site).get("encryption_key") == key:
            self.skip("encryption_key already matches")
            return
        res = self.ctx.sites.bench(
            bench, ["--site", self.site, "set-config", "encryption_key", key], timeout=300
        )
        self.check(res, "set encryption_key")

    def step_migrate(self) -> None:
        self.check(self.ctx.sites.migrate(self.result["bench"], self.site), "bench migrate")

    def step_write_route(self) -> None:
        self.write_route()
        if self.p.remote_path:
            shutil.rmtree(Path(self.result["dir"]), ignore_errors=True)


# --------------------------------------------------------------------------- migrate / config
class SiteMigrate(_SiteJob):
    kind = "site.migrate"
    steps = ("migrate",)

    def step_migrate(self) -> None:
        bench = self.bench_for_site()
        self.check(self.ctx.sites.migrate(bench, self.site), "bench migrate")


class SiteSetConfigParams(SiteParams):
    key: str
    value: Any = None

    @field_validator("key")
    @classmethod
    def _key(cls, value: str) -> str:
        return v.config_key(value)


class SiteSetConfig(_SiteJob):
    kind = "site.set_config"
    steps = ("set_config",)
    Params = SiteSetConfigParams

    def step_set_config(self) -> None:
        bench = self.bench_for_site()
        from ..redact import is_sensitive

        if is_sensitive(self.p.key) and isinstance(self.p.value, str):
            self.secret(self.p.value)
        current = self.ctx.sites.site_config(bench, self.site)
        if self.p.key in current and current[self.p.key] == self.p.value:
            self.skip(f"{self.p.key} already set")
            return
        self.check(self.ctx.sites.set_config(bench, self.site, self.p.key, self.p.value), "bench set-config")


class SiteGetConfig(_SiteJob):
    """Return site_config.json with secrets redacted (db_password, encryption_key, *key*, ...)."""

    kind = "site.get_config"
    steps = ("read",)

    def step_read(self) -> None:
        bench = self.bench_for_site()
        path = self.ctx.sites.config_path(bench, self.site)
        try:
            config = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            self.fail(f"cannot read {path.name}: {exc}")
        if not isinstance(config, dict):
            self.fail("site_config.json is not an object")
        redacted, keys = redact_site_config(config)
        self.result.update({"config": redacted, "redacted": keys})
        self.log(f"{len(config)} keys ({len(keys)} redacted)")


class SiteUpdateConfigParams(SiteParams):
    set_values: dict[str, Any] = Field(default_factory=dict, alias="set")
    remove: list[str] = []

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True, populate_by_name=True)

    @field_validator("set_values")
    @classmethod
    def _set(cls, value: dict[str, Any]) -> dict[str, Any]:
        for key in value:
            v.config_key(key)
            if key in PROTECTED_CONFIG_KEYS:
                raise ValueError(f"{key} cannot be changed")
        if len(json.dumps(value, default=str)) > 256 * 1024:
            raise ValueError("config changes too large")
        return value

    @field_validator("remove")
    @classmethod
    def _remove(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for key in value:
            v.config_key(key)
            if key in PROTECTED_CONFIG_KEYS:
                raise ValueError(f"{key} cannot be removed")
            if key not in out:
                out.append(key)
        return out

    @model_validator(mode="after")
    def _disjoint(self) -> "SiteUpdateConfigParams":
        both = set(self.set_values) & set(self.remove)
        if both:
            raise ValueError(f"keys both set and removed: {', '.join(sorted(both))}")
        return self


class SiteUpdateConfig(_SiteJob):
    """Batch site_config.json change ``{site, set: {k: v}, remove: [k]}`` in one atomic write.

    Values are JSON (str/number/bool/list/object). Check-then-act: unchanged keys are not
    rewritten; a no-op job does not touch the file. Protected keys (db_*, encryption_key)
    are refused.
    """

    kind = "site.update_config"
    steps = ("update",)
    Params = SiteUpdateConfigParams

    @classmethod
    def validate_params(cls, raw: dict[str, Any]) -> dict[str, Any]:
        return cls.Params.model_validate(raw).model_dump(mode="json", by_alias=True)

    def step_update(self) -> None:
        from ..redact import is_sensitive

        bench = self.bench_for_site()
        for key, value in self.p.set_values.items():
            if is_sensitive(key) and isinstance(value, str):
                self.secret(value)
        path = self.ctx.sites.config_path(bench, self.site)
        try:
            current = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            self.fail(f"cannot read {path.name}: {exc}")
        if not isinstance(current, dict):
            self.fail("site_config.json is not an object")
        new, changed, removed = apply_config_changes(current, self.p.set_values, self.p.remove)
        self.result.update({"changed": changed, "removed": removed})
        if not changed and not removed:
            self.skip("site_config already up to date")
            return
        self.ctx.sites.write_site_config(bench, self.site, new)
        self.log(f"site_config.json: set {', '.join(changed) or '-'}; removed {', '.join(removed) or '-'}")


# --------------------------------------------------------------------------- drop
class SiteDropParams(SiteParams):
    skip_backup: bool = False


class SiteDrop(_SiteJob):
    """Backup (unless skip_backup), bench drop-site, remove the route."""

    kind = "site.drop"
    steps = ("backup", "drop", "remove_route")
    Params = SiteDropParams

    def _bench_or_none(self) -> str | None:
        bench = self.result.get("bench") or self.ctx.sites.find_bench(self.site)
        if bench:
            self.result["bench"] = bench
        return bench

    def step_backup(self) -> None:
        bench = self._bench_or_none()
        if not bench:
            self.skip("site does not exist")
            return
        if self.p.skip_backup:
            self.skip("skip_backup requested")
            return
        self.take_backup(bench, with_files=True)
        if "path" not in self.result:
            self.stage_backup(bench)
        if "uploaded" not in self.result:
            self.upload_backup(bench, upload=True)

    def step_drop(self) -> None:
        bench = self._bench_or_none()
        if not bench or not self.ctx.sites.exists(bench, self.site):
            self.skip("site already dropped")
            return
        args = SiteManager.drop_site_args(self.site, self.db_root_password())
        args += ["--archived-sites-path", ARCHIVED_SITES]
        self.check(self.ctx.sites.bench(bench, args), "bench drop-site")

    def step_remove_route(self) -> None:
        if self.ctx.proxy.remove_route(self.site):
            self.log("route removed")
        else:
            self.skip("no route")
