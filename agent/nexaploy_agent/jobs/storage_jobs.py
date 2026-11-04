"""Attachments offload, replication and trash (ARCHITECTURE.md §11).

* ``site.storage_configure``     – remotes + bench propagation; re-mount when the policy changed
* ``site.storage_migrate``       – move a site's files to the bucket and mount it (``to_bucket``)
                                   or copy them back and unmount (``to_local``)
* ``site.storage_status``        – mounts, cache, dirty uploads, last replication, bucket size
* ``site.storage_failover``      – mount the replica (primary region down) or go back to the primary
* ``site.storage_trash_list``    – list a site's trash
* ``site.storage_trash_restore`` – copy a trash item back into the live bucket
* ``server.storage_replicate``   – ``rclone sync primary -> replica`` (deletions -> trash) per site
* ``server.storage_prune_trash`` – delete trash older than each site's retention

Every job carries the Storage Profiles it needs (secrets resolved by the control plane at
poll time) and rewrites their remotes in ``config/storage-rclone.conf`` first, so credential
rotation is just "send the next job".
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .. import validation as v
from ..config import FRAPPE_UID
from ..fsutil import chown
from ..runtime.storage import (
    KINDS, SITES_PROPAGATION_ENV, Profile, StorageError, check_argv, copy_argv, move_argv, new_ts,
    parse_lsjson, parse_trash_item, propagation_ok, prune_trash_argv, sync_argv, trash_plan, unit_name,
    validate_policy,
)
from .base import BaseParams, Conflict, Job, NotFound

MOUNT_WAIT_ATTEMPTS = 15
CLEAN_WAIT_ATTEMPTS = 120  # x mount_wait_seconds: wait for pending uploads
ASIDE_PREFIX = "files.pre-offload-"
RESTORING = "files.restoring"


def count_dirty(meta_dir: Path) -> int:
    """Files rclone still has to upload: ``"Dirty": true`` in its VFS metadata cache."""
    if not meta_dir.is_dir():
        return 0
    n = 0
    for dirpath, _dirs, files in os.walk(meta_dir):
        for name in files:
            try:
                with open(os.path.join(dirpath, name), encoding="utf-8") as fh:
                    if json.load(fh).get("Dirty"):
                        n += 1
            except (OSError, ValueError, AttributeError):
                continue
    return n


# --------------------------------------------------------------------------- params
class ProfileParams(BaseParams):
    name: str
    preset: str = "Other"
    bucket: str
    access_key_id: str
    secret_access_key: str
    endpoint: str | None = None
    region: str | None = None
    path_style: bool | None = None
    prefix: str | None = None

    def profile(self) -> Profile:
        return Profile.from_params(self.model_dump())

    @model_validator(mode="after")
    def _valid(self) -> "ProfileParams":
        try:
            self.profile()
        except v.ValidationError as exc:
            raise ValueError(str(exc)) from None
        return self


class _ProfilesParams(BaseParams):
    profiles: list[ProfileParams] = Field(default_factory=list)

    def get_profile(self, name: str | None) -> Profile | None:
        if not name:
            return None
        for p in self.profiles:
            if p.name == name:
                return p.profile()
        raise ValueError(f"storage profile {name!r} not in params.profiles")


class PolicyParams(_ProfilesParams):
    site: str
    primary: str
    replica: str | None = None
    trash: str | None = None
    trash_retention_days: int | None = None
    active: Literal["primary", "replica"] = "primary"
    cache_max_size: str | None = None

    @field_validator("site")
    @classmethod
    def _site(cls, value: str) -> str:
        return v.site_name(value)

    @model_validator(mode="after")
    def _policy(self) -> "PolicyParams":
        try:
            validate_policy(self.primary, self.replica, self.trash, self.trash_retention_days)
        except v.ValidationError as exc:
            raise ValueError(str(exc)) from None
        for name in (self.primary, self.replica, self.trash):
            self.get_profile(name)
        if self.active == "replica" and not self.replica:
            raise ValueError("cannot mount the replica: no replica profile")
        return self

    def policy_state(self) -> dict[str, Any]:
        return {"primary": self.primary, "replica": self.replica, "trash": self.trash,
                "trash_retention_days": self.trash_retention_days, "cache_max_size": self.cache_max_size}


# --------------------------------------------------------------------------- shared
class _StorageJob(Job):
    """Helpers shared by the site.storage_* jobs."""

    Params = PolicyParams
    mount_wait_seconds: float = 1.0

    @property
    def site(self) -> str:
        return self.p.site

    @property
    def st(self):
        return self.ctx.storage

    @classmethod
    def precheck(cls, ctx, params: dict[str, Any]) -> None:
        if not ctx.sites.find_bench(params["site"]):
            raise NotFound(f"site {params['site']!r} not found")

    def bench_for_site(self) -> str:
        bench = self.result.get("bench") or self.ctx.sites.find_bench(self.site)
        if not bench:
            self.fail(f"site {self.site} not found on this server")
        self.result["bench"] = bench
        return bench

    def site_dir(self) -> Path:
        return self.ctx.sites.site_dir(self.bench_for_site(), self.site)

    def files_dir(self, kind: str) -> Path:
        return self.st.files_dir(self.site_dir(), kind)

    def profiles_used(self) -> list[Profile]:
        return [p.profile() for p in self.p.profiles]

    def target_profile(self, active: str | None = None) -> Profile:
        active = active or self.p.active
        name = self.p.replica if active == "replica" else self.p.primary
        prof = self.p.get_profile(name)
        assert prof is not None
        return prof

    def rclone(self, argv: list[str], what: str, timeout: float = 6 * 3600):
        return self.check(self.st.run(argv, timeout=timeout), what)

    @property
    def rc(self) -> str:
        return self.settings.storage.rclone

    @property
    def conf(self) -> Path:
        return self.st.config_path

    # ---- steps building blocks -------------------------------------------------------------
    def ensure_remotes(self) -> None:
        for p in self.p.profiles:
            self.secret(p.secret_access_key)
        if self.st.ensure_remotes(self.profiles_used()):
            self.log(f"rclone remotes written: {', '.join(p.remote for p in self.profiles_used())}")
        else:
            self.skip("rclone remotes up to date")

    def prepare_bench(self) -> None:
        """``SITES_PROPAGATION=rslave`` in the bench .env (containers follow host mounts)."""
        bench = self.bench_for_site()
        b = self.ctx.benches
        env = b.read_env(bench)
        if env.get(SITES_PROPAGATION_ENV) == "rslave":
            self.skip("bench sites mount already uses rslave propagation")
            return
        prop = self.st.host_propagation(b.sites_dir(bench))
        if not propagation_ok(prop):
            self.fail(
                f"{b.sites_dir(bench)} is on a mount with propagation {prop or 'unknown'!r}; Docker needs "
                "a shared mount for rslave (systemd hosts are shared by default; otherwise run "
                "`mount --make-rshared /` at boot)"
            )
        env[SITES_PROPAGATION_ENV] = "rslave"
        b.write_env(bench, env)
        b.render(bench)
        self.log(f"bench {bench}: {SITES_PROPAGATION_ENV}=rslave; recreating containers")
        self.check(b.up(bench), "docker compose up")

    def wait_mounted(self, path: Path) -> bool:
        for i in range(MOUNT_WAIT_ATTEMPTS):
            if self.st.is_mounted(path):
                return True
            if self.mount_wait_seconds:
                time.sleep(self.mount_wait_seconds)
        return False

    def start_mount(self, kind: str, profile: Profile, restart: bool = False) -> None:
        mp = self.files_dir(kind)
        unit = unit_name(self.site, kind)
        changed = self.st.write_unit(profile, self.site, kind, mp, self.p.cache_max_size)
        if changed:
            self.check(self.st.systemctl("daemon-reload"), "systemctl daemon-reload")
        if changed or restart or not self.st.is_mounted(mp):
            self.check(self.st.systemctl("enable", unit), f"systemctl enable {unit}")
            self.check(self.st.systemctl("restart", unit), f"systemctl restart {unit}")
            if not self.wait_mounted(mp):
                self.fail(f"{unit}: {mp} did not come up (see {self.st.log_file(self.site, kind)} "
                          f"and `journalctl -u {unit}`)")
            self.log(f"{kind}: {profile.site_root(self.site, kind)} mounted on {mp}")
        else:
            self.skip(f"{kind}: mounted, unit unchanged")

    def wait_clean(self, kind: str) -> int:
        """Wait until the mount has uploaded every dirty file; return what is still dirty."""
        meta = self.st.cache_dir(self.site, kind) / "vfsMeta"
        dirty = count_dirty(meta)
        for _ in range(CLEAN_WAIT_ATTEMPTS):
            if not dirty:
                break
            if self.mount_wait_seconds:
                time.sleep(self.mount_wait_seconds)
            dirty = count_dirty(meta)
        return dirty

    def stop_mount(self, kind: str, remove: bool = False) -> int:
        """Stop (and with ``remove`` disable + delete) the unit once pending uploads are done.

        Returns the number of files still pending; then the unit is left running so rclone
        keeps uploading, and nothing is removed.
        """
        unit = unit_name(self.site, kind)
        meta = self.st.cache_dir(self.site, kind) / "vfsMeta"
        for attempt in range(3):
            if self.st.unit_active(self.site, kind):
                self.wait_clean(kind)
            self.st.systemctl("stop", unit)
            dirty = count_dirty(meta)
            if not dirty:
                break
            # written between the wait and the stop: start again so rclone uploads them
            self.log(f"{kind}: {dirty} files still pending upload; restarting the mount to flush")
            self.st.systemctl("start", unit)
        else:
            return dirty
        mp = self.files_dir(kind)
        if self.st.is_mounted(mp):
            self.st.run([self.settings.storage.fusermount, "-uz", str(mp)], timeout=60)
        if remove:
            self.st.systemctl("disable", unit)
            if self.st.remove_unit(self.site, kind):
                self.st.systemctl("daemon-reload")
        return 0


def teardown_site_storage(job: Job, site: str, site_dir: Path) -> None:
    """Unmount and remove a site's units before the site directory is dropped/moved.

    Bucket data is kept (the control plane decides about it); pending uploads are flushed.
    """
    st = job.ctx.storage
    if not st.read_state(site):
        return
    for kind in KINDS:
        unit = unit_name(site, kind)
        meta = st.cache_dir(site, kind) / "vfsMeta"
        if count_dirty(meta):
            job.log(f"warning: {unit} has files pending upload; stopping anyway")
        st.systemctl("disable", "--now", unit)
        mp = st.files_dir(site_dir, kind)
        if st.is_mounted(mp):
            st.run([job.settings.storage.fusermount, "-uz", str(mp)], timeout=60)
        if st.remove_unit(site, kind):
            job.log(f"removed {unit}")
        if mp.is_dir() and st.dir_empty(mp):
            mp.rmdir()
    st.systemctl("daemon-reload")
    st.remove_tree(st.settings.storage_dir / "cache" / site)
    st.remove_state(site)


# --------------------------------------------------------------------------- configure
class SiteStorageConfigure(_StorageJob):
    """Refresh remotes, bench propagation and (if offloaded) the mount units."""

    kind = "site.storage_configure"
    steps = ("ensure_remotes", "prepare_bench", "apply")

    def step_ensure_remotes(self) -> None:
        self.ensure_remotes()

    def step_prepare_bench(self) -> None:
        self.prepare_bench()

    def step_apply(self) -> None:
        state = self.st.read_state(self.site)
        if state.get("mode") != "Offloaded":
            self.st.update_state(self.site, bench=self.bench_for_site(), policy=self.p.policy_state(),
                                 mode=state.get("mode") or "Local")
            self.log("files are still local; run site.storage_migrate (to_bucket) to offload them")
            self.result.update({"mode": "Local", "offloaded": False})
            return
        profile = self.target_profile()
        restart = state.get("active") != self.p.active
        for kind in KINDS:
            self.start_mount(kind, profile, restart=restart)
        self.st.update_state(self.site, policy=self.p.policy_state(), active=self.p.active)
        self.result.update({"mode": "Offloaded", "offloaded": True, "active": self.p.active})


# --------------------------------------------------------------------------- migrate
class MigrateParams(PolicyParams):
    direction: Literal["to_bucket", "to_local"] = "to_bucket"
    keep_local_copy: bool = False


class SiteStorageMigrate(_StorageJob):
    kind = "site.storage_migrate"
    Params = MigrateParams
    TO_BUCKET = ("ensure_remotes", "prepare_bench", "copy", "switch", "verify", "cleanup")
    TO_LOCAL = ("ensure_remotes", "copy_back", "switch_back")

    @classmethod
    def step_names(cls, params: dict[str, Any]) -> list[str]:
        return list(cls.TO_LOCAL if params.get("direction") == "to_local" else cls.TO_BUCKET)

    def step_ensure_remotes(self) -> None:
        self.ensure_remotes()

    def step_prepare_bench(self) -> None:
        self.prepare_bench()

    # ---- to_bucket ---------------------------------------------------------------------
    def step_copy(self) -> None:
        """Bulk copy while the site keeps running (the switch copies the delta)."""
        prof = self.target_profile()
        for kind in KINDS:
            local = self.files_dir(kind)
            if self.st.is_mounted(local):
                self.skip(f"{kind}: already mounted")
                continue
            if not local.is_dir():
                self.log(f"{kind}: no local files")
                continue
            self.rclone(copy_argv(self.rc, self.conf, str(local), prof.site_root(self.site, kind)),
                        f"rclone copy {kind} files")
            self.log(f"{kind}: copied to {prof.site_root(self.site, kind)}")

    def step_switch(self) -> None:
        prof = self.target_profile()
        ts = self.result.setdefault("ts", new_ts())
        self.save()
        self.st.update_state(self.site, bench=self.bench_for_site(), policy=self.p.policy_state(),
                             mode="Migrating", active=self.p.active)
        for kind in KINDS:
            local = self.files_dir(kind)
            if self.st.is_mounted(local):
                self.skip(f"{kind}: already mounted")
                continue
            aside = local.parent / f"{ASIDE_PREFIX}{ts}"
            remote = prof.site_root(self.site, kind)
            if local.is_dir() and not self.st.dir_empty(local):
                # final delta, then verify every local file is in the bucket before moving it aside
                self.rclone(copy_argv(self.rc, self.conf, str(local), remote), f"rclone copy {kind} (delta)")
                self.rclone(check_argv(self.rc, self.conf, str(local), remote), f"rclone check {kind}")
                local.rename(aside)
                self.log(f"{kind}: verified; local copy moved aside to {aside.name}")
            self.st.make_mountpoint(local)
            try:
                self.start_mount(kind, prof)
            except Exception:
                self.rollback(ts)
                raise

    def rollback(self, ts: str) -> None:
        """A mount failed: undo every kind so the site serves its local files again."""
        for kind in KINDS:
            local = self.files_dir(kind)
            aside = local.parent / f"{ASIDE_PREFIX}{ts}"
            self.st.systemctl("disable", "--now", unit_name(self.site, kind))
            if self.st.is_mounted(local):
                self.st.run([self.settings.storage.fusermount, "-uz", str(local)], timeout=60)
            self.st.remove_unit(self.site, kind)
            if aside.is_dir() and self.st.dir_empty(local):
                if local.exists():
                    local.rmdir()
                aside.rename(local)
                self.log(f"{kind}: local files restored")
        self.st.systemctl("daemon-reload")
        self.st.update_state(self.site, mode="Local")

    def step_verify(self) -> None:
        missing = [k for k in KINDS if not self.st.is_mounted(self.files_dir(k))]
        if missing:
            self.fail(f"not mounted: {', '.join(missing)}")
        self.st.update_state(self.site, mode="Offloaded", active=self.p.active,
                             offloaded_at=self.result.get("ts"))
        self.result.update({"mode": "Offloaded", "offloaded": True, "active": self.p.active})
        self.log("files are served from the bucket")

    def step_cleanup(self) -> None:
        removed = []
        for kind in KINDS:
            parent = self.site_dir() / kind
            for aside in sorted(parent.glob(f"{ASIDE_PREFIX}*")) if parent.is_dir() else []:
                if self.p.keep_local_copy:
                    self.log(f"{kind}: keeping {aside} (keep_local_copy)")
                    continue
                self.st.remove_tree(aside)
                removed.append(str(aside))
        self.result["removed_local"] = removed
        if not removed and not self.p.keep_local_copy:
            self.skip("no local copies left")

    # ---- to_local ----------------------------------------------------------------------
    def _active_profile(self) -> Profile:
        return self.target_profile(self.st.read_state(self.site).get("active") or self.p.active)

    def step_copy_back(self) -> None:
        prof = self._active_profile()
        for kind in KINDS:
            local = self.files_dir(kind)
            if not self.st.is_mounted(local) and not self.st.unit_path(self.site, kind).exists():
                self.skip(f"{kind}: not offloaded")
                continue
            dest = local.parent / RESTORING
            self.rclone(copy_argv(self.rc, self.conf, prof.site_root(self.site, kind), str(dest)),
                        f"rclone copy {kind} back")
            self.log(f"{kind}: copied to {dest}")

    def step_switch_back(self) -> None:
        prof = self._active_profile()
        self.st.update_state(self.site, mode="Migrating")
        for kind in KINDS:
            local = self.files_dir(kind)
            dest = local.parent / RESTORING
            if not self.st.is_mounted(local) and not self.st.unit_path(self.site, kind).exists():
                if dest.is_dir() and self.st.dir_empty(local):
                    if local.exists():
                        local.rmdir()
                    dest.rename(local)
                self.skip(f"{kind}: local")
                continue
            dirty = self.stop_mount(kind, remove=True)
            if dirty:
                self.fail(f"{kind}: {dirty} files were not uploaded; start the mount and retry")
            self.rclone(copy_argv(self.rc, self.conf, prof.site_root(self.site, kind), str(dest)),
                        f"rclone copy {kind} back (delta)")
            if self.st.is_mounted(local) or not self.st.dir_empty(local):
                self.fail(f"{local} is still mounted or not empty")
            if local.exists():
                local.rmdir()
            dest.rename(local)
            chown(local, FRAPPE_UID, FRAPPE_UID, recursive=True)
            self.log(f"{kind}: files are local again")
        self.st.remove_tree(self.settings.storage_dir / "cache" / self.site)
        self.st.update_state(self.site, mode="Local")
        self.result.update({"mode": "Local", "offloaded": False})


# --------------------------------------------------------------------------- status
class StatusParams(BaseParams):
    site: str
    with_size: bool = False
    profiles: list[ProfileParams] = Field(default_factory=list)

    @field_validator("site")
    @classmethod
    def _site(cls, value: str) -> str:
        return v.site_name(value)


class SiteStorageStatus(_StorageJob):
    kind = "site.storage_status"
    steps = ("collect",)
    Params = StatusParams

    def step_collect(self) -> None:
        state = self.st.read_state(self.site)
        kinds: dict[str, Any] = {}
        for kind in KINDS:
            cache = self.st.cache_dir(self.site, kind)
            kinds[kind] = {
                "unit": unit_name(self.site, kind),
                "unit_active": self.st.unit_active(self.site, kind),
                "mounted": self.st.is_mounted(self.files_dir(kind)),
                "dirty": count_dirty(cache / "vfsMeta"),
                "cache_bytes": _dir_bytes(cache / "vfs"),
            }
        out: dict[str, Any] = {
            "site": self.site,
            "mode": state.get("mode") or "Local",
            "active": state.get("active"),
            "policy": state.get("policy"),
            "offloaded_at": state.get("offloaded_at"),
            "last_replication": state.get("last_replication"),
            "kinds": kinds,
            "healthy": state.get("mode") != "Offloaded" or all(k["mounted"] for k in kinds.values()),
        }
        if self.p.with_size and state.get("mode") == "Offloaded" and self.p.profiles:
            for p in self.p.profiles:
                self.secret(p.secret_access_key)
            self.st.ensure_remotes([p.profile() for p in self.p.profiles])
            name = (state.get("policy") or {}).get("replica" if state.get("active") == "replica" else "primary")
            prof = next((p.profile() for p in self.p.profiles if p.name == name), None)
            if prof:
                res = self.st.rclone("size", "--json", "--fast-list", prof.site_root(self.site), timeout=1800)
                if res.ok:
                    try:
                        data = json.loads(res.stdout)
                        out["bucket"] = {"objects": data.get("count"), "bytes": data.get("bytes")}
                    except ValueError:
                        pass
        self.result.update(out)
        self.log(f"mode {out['mode']}, healthy {out['healthy']}")


def _dir_bytes(path: Path) -> int:
    total = 0
    if not path.is_dir():
        return 0
    for dirpath, _d, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_blocks * 512
            except OSError:
                pass
    return total


# --------------------------------------------------------------------------- failover
class FailoverParams(PolicyParams):
    # mount this side; going back to the primary first syncs replica -> primary
    target: Literal["primary", "replica"]
    resync: bool = True
    max_delete: int = 1000


class SiteStorageFailover(_StorageJob):
    kind = "site.storage_failover"
    steps = ("ensure_remotes", "resync", "switch")
    Params = FailoverParams

    def step_ensure_remotes(self) -> None:
        if not self.p.replica:
            self.fail("failover needs a replica profile")
        self.ensure_remotes()

    def step_resync(self) -> None:
        state = self.st.read_state(self.site)
        if state.get("mode") != "Offloaded":
            self.fail("site files are not offloaded")
        if self.p.target == "replica" or not self.p.resync:
            self.skip("no resync needed")
            return
        if state.get("active") != "replica":
            self.skip("primary already active")
            return
        if self.result.get("resynced"):
            self.skip("replica already synced back")
            return
        # files written during the failover go back to the primary; files deleted during the
        # failover are removed from the primary (to the trash when one is configured)
        replica, primary = self.target_profile("replica"), self.target_profile("primary")
        trash = self.p.get_profile(self.p.trash)
        ts = self.result.setdefault("ts", new_ts())
        plan = trash_plan(primary, trash, self.site, ts) if trash else None
        for kind in KINDS:
            self.rclone(sync_argv(self.rc, self.conf, replica.site_root(self.site, kind),
                                  primary.site_root(self.site, kind),
                                  backup_dir=f"{plan.backup_dir}/{kind}" if plan else None,
                                  max_delete=self.p.max_delete), f"rclone sync replica -> primary ({kind})")
        if plan and plan.staging_root:
            res = self.st.run(move_argv(self.rc, self.conf, plan.staging_root, plan.trash_root))
            if not res.ok and "directory not found" not in (res.stderr or "").lower():
                self.check(res, "rclone move to trash")
        self.result["resynced"] = True
        self.log("replica synced back to the primary")

    def step_switch(self) -> None:
        state = self.st.read_state(self.site)
        restart = state.get("active") != self.p.target
        prof = self.target_profile(self.p.target)
        for kind in KINDS:
            # pending uploads to the old side cannot complete if it is down; they stay in the
            # cache and are uploaded when that side is mounted again
            dirty = count_dirty(self.st.cache_dir(self.site, kind) / "vfsMeta")
            if dirty and restart:
                self.log(f"warning: {kind}: {dirty} files pending upload to the {state.get('active')} side")
            self.start_mount(kind, prof, restart=restart)
        self.st.update_state(self.site, active=self.p.target, policy=self.p.policy_state(),
                             failed_over_at=new_ts() if self.p.target == "replica" else None)
        self.result.update({"active": self.p.target})
        self.log(f"site files now served from the {self.p.target} ({prof.name})")


# --------------------------------------------------------------------------- trash
class TrashParams(_ProfilesParams):
    site: str
    trash: str
    primary: str | None = None
    replica: str | None = None
    item: str | None = None
    limit: int = Field(default=1000, ge=1, le=10000)

    @field_validator("site")
    @classmethod
    def _site(cls, value: str) -> str:
        return v.site_name(value)

    @field_validator("item")
    @classmethod
    def _item(cls, value: str | None) -> str | None:
        if value:
            try:
                parse_trash_item(value)
            except v.ValidationError as exc:
                raise ValueError(str(exc)) from None
        return value or None


class SiteStorageTrashList(_StorageJob):
    kind = "site.storage_trash_list"
    steps = ("list",)
    Params = TrashParams

    @classmethod
    def precheck(cls, ctx, params: dict[str, Any]) -> None:
        """The trash can be listed even after the site left this server."""

    def step_list(self) -> None:
        self.ensure_remotes()
        trash = self.p.get_profile(self.p.trash)
        root = trash.site_root(self.site) + (f"/{self.p.item}" if self.p.item else "")
        res = self.st.rclone("lsjson", "-R", "--files-only", "--fast-list", root, timeout=1800)
        if not res.ok:
            if "directory not found" in (res.stderr or "").lower():
                self.result["entries"] = []
                return
            self.check(res, "rclone lsjson")
        entries = parse_lsjson(res.stdout, limit=self.p.limit)
        if self.p.item:
            for e in entries:
                e["path"] = f"{self.p.item.strip('/')}/{e['path']}"
        self.result.update({"entries": entries, "truncated": len(entries) >= self.p.limit})
        self.log(f"{len(entries)} files in the trash")


class SiteStorageTrashRestore(_StorageJob):
    """Copy ``<ts>[/<kind>[/<path>]]`` from the trash into the live bucket (kept in the trash)."""

    kind = "site.storage_trash_restore"
    steps = ("ensure_remotes", "restore", "refresh")
    Params = TrashParams

    def step_ensure_remotes(self) -> None:
        if not self.p.item:
            self.fail("item is required")
        if not self.p.primary:
            self.fail("primary is required")
        self.ensure_remotes()

    def step_restore(self) -> None:
        ts, kind, rest = parse_trash_item(self.p.item or "")
        state = self.st.read_state(self.site)
        live_name = self.p.replica if state.get("active") == "replica" and self.p.replica else self.p.primary
        live = self.p.get_profile(live_name)
        trash = self.p.get_profile(self.p.trash)
        src_root = trash.site_root(self.site)
        restored = []
        for k in ([kind] if kind else list(KINDS)):
            src = f"{src_root}/{ts}/{k}" + (f"/{rest}" if rest else "")
            dst = live.site_root(self.site, k) + (f"/{rest}" if rest else "")
            # a path may be a file (copyto) or a folder (copy): try the file first
            if rest:
                res = self.st.run(copy_argv(self.rc, self.conf, src, dst, single_file=True))
                if not res.ok:
                    self.rclone(copy_argv(self.rc, self.conf, src, dst), f"rclone copy {src}")
            else:
                res = self.st.run(copy_argv(self.rc, self.conf, src, dst))
                if not res.ok and "directory not found" not in (res.stderr or "").lower():
                    self.check(res, f"rclone copy {src}")
            restored.append(dst)
        self.result.update({"restored": restored, "into": live_name})
        self.log(f"restored {self.p.item} into {live_name}")

    def step_refresh(self) -> None:
        if self.st.is_offloaded(self.site):
            self.st.flush_dir_cache(self.site)
            self.log("mount directory cache flushed")
        else:
            self.skip("site not mounted here")


# --------------------------------------------------------------------------- server-level
class ReplicateSite(BaseParams):
    site: str
    primary: str
    replica: str
    trash: str | None = None
    max_delete: int = 1000
    compare: Literal["size", "modtime", "checksum"] = "size"

    @field_validator("site")
    @classmethod
    def _site(cls, value: str) -> str:
        return v.site_name(value)


class ReplicateParams(_ProfilesParams):
    sites: list[ReplicateSite] = Field(default_factory=list)

    @model_validator(mode="after")
    def _refs(self) -> "ReplicateParams":
        for s in self.sites:
            try:
                validate_policy(s.primary, s.replica, s.trash, 1)
            except v.ValidationError as exc:
                raise ValueError(f"{s.site}: {exc}") from None
            for name in (s.primary, s.replica, s.trash):
                self.get_profile(name)
        return self


class ServerStorageReplicate(Job):
    """``rclone sync primary -> replica`` per site; replica deletions/overwrites -> trash.

    A file deleted from the primary (by Frappe through the mount) still exists in the replica
    at the next sync; ``--backup-dir`` moves it out of the replica instead of deleting it, and
    it lands in the trash bucket. Sites mounted on their replica (failover) are skipped: a
    sync from a stale primary would delete new files. One site failing does not stop others;
    the job fails at the end if any site failed.
    """

    kind = "server.storage_replicate"
    steps = ("ensure_remotes", "replicate")
    Params = ReplicateParams

    def step_ensure_remotes(self) -> None:
        for p in self.p.profiles:
            self.secret(p.secret_access_key)
        changed = self.ctx.storage.ensure_remotes([p.profile() for p in self.p.profiles])
        self.log("rclone remotes written" if changed else "already done: rclone remotes up to date")

    def step_replicate(self) -> None:
        st = self.ctx.storage
        rc, conf = self.settings.storage.rclone, st.config_path
        done: dict[str, Any] = self.result.setdefault("sites", {})
        for s in self.p.sites:
            if s.site in done:
                self.skip(f"{s.site}: {done[s.site]['status']}")
                continue
            started = new_ts()
            entry: dict[str, Any] = {"started_at": started}
            try:
                state = st.read_state(s.site)
                if state.get("active") == "replica":
                    entry.update(status="skipped", reason="failed over to the replica")
                else:
                    entry.update(self._sync_site(st, rc, conf, s, started))
            except Exception as exc:  # StorageError, validation, ...
                entry.update(status="failure", error=self.mask(str(exc))[-2000:])
            entry["ended_at"] = new_ts()
            done[s.site] = entry
            if st.read_state(s.site):
                st.update_state(s.site, last_replication=entry)
            self.save()
            self.log(f"{s.site}: {entry['status']}" + (f" ({entry.get('trashed', 0)} to trash)"
                                                        if entry["status"] == "success" else ""))
        failed = [k for k, e in done.items() if e.get("status") == "failure"]
        if failed:
            self.fail(f"replication failed for {', '.join(sorted(failed))}")

    def _sync_site(self, st, rc: str, conf: Path, s: ReplicateSite, ts: str) -> dict[str, Any]:
        primary, replica = self.p.get_profile(s.primary), self.p.get_profile(s.replica)
        trash = self.p.get_profile(s.trash)
        plan = trash_plan(replica, trash, s.site, ts) if trash else None
        synced = []
        for kind in KINDS:
            src, dst = primary.site_root(s.site, kind), replica.site_root(s.site, kind)
            res = st.run(sync_argv(rc, conf, src, dst,
                                   backup_dir=f"{plan.backup_dir}/{kind}" if plan else None,
                                   max_delete=s.max_delete, compare=s.compare))
            if res.ok:
                synced.append(kind)
                continue
            if "directory not found" in (res.stderr or "").lower():
                # S3 has no empty dirs: no files of this kind in the primary yet. Only fine when
                # the replica is empty too - otherwise refuse (a wrong primary must not wipe it).
                lst = st.run([rc, "--config", str(conf), "lsf", "--max-depth", "1", dst], timeout=300)
                if not (lst.ok and lst.stdout.strip()):
                    continue
                raise StorageError(f"{kind}: the primary has no files but the replica has; refusing to sync")
            raise StorageError(f"rclone sync {kind} failed (exit {res.returncode}): "
                               f"{(res.stderr or '').strip()[-600:]}")
        trashed = 0
        if plan:
            lst = st.run([rc, "--config", str(conf), "lsf", "-R", "--files-only", plan.backup_dir], timeout=1800)
            if lst.ok:
                trashed = len([x for x in lst.stdout.splitlines() if x.strip()])
            if plan.staging_root:
                mv = st.run(move_argv(rc, conf, plan.staging_root, plan.trash_root))
                if not mv.ok and "directory not found" not in (mv.stderr or "").lower():
                    raise StorageError(f"rclone move to trash failed: {(mv.stderr or '').strip()[-600:]}")
        return {"status": "success", "trashed": trashed, "synced": synced}


class PruneSite(BaseParams):
    site: str
    trash: str
    retention_days: int = Field(ge=1)

    @field_validator("site")
    @classmethod
    def _site(cls, value: str) -> str:
        return v.site_name(value)


class PruneTrashParams(_ProfilesParams):
    sites: list[PruneSite] = Field(default_factory=list)

    @model_validator(mode="after")
    def _refs(self) -> "PruneTrashParams":
        for s in self.sites:
            self.get_profile(s.trash)
        return self


class ServerStoragePruneTrash(Job):
    """``rclone delete <trash>/<site> --min-age <days>d --rmdirs`` per site."""

    kind = "server.storage_prune_trash"
    steps = ("ensure_remotes", "prune")
    Params = PruneTrashParams

    def step_ensure_remotes(self) -> None:
        for p in self.p.profiles:
            self.secret(p.secret_access_key)
        self.ctx.storage.ensure_remotes([p.profile() for p in self.p.profiles])

    def step_prune(self) -> None:
        st = self.ctx.storage
        done: dict[str, str] = self.result.setdefault("sites", {})
        for s in self.p.sites:
            if s.site in done:
                continue
            trash = self.p.get_profile(s.trash)
            res = st.run(prune_trash_argv(self.settings.storage.rclone, st.config_path,
                                          trash.site_root(s.site), s.retention_days), timeout=6 * 3600)
            if res.ok or "directory not found" in (res.stderr or "").lower():
                done[s.site] = "success"
            else:
                done[s.site] = "failure"
                self.log(self.mask(f"{s.site}: {(res.stderr or '').strip()[-400:]}"))
            self.save()
        failed = sorted(k for k, val in done.items() if val == "failure")
        if failed:
            self.fail(f"trash pruning failed for {', '.join(failed)}")
        self.log(f"pruned trash of {len(done)} sites")


STORAGE_JOB_CLASSES: tuple[type[Job], ...] = (
    SiteStorageConfigure, SiteStorageMigrate, SiteStorageStatus, SiteStorageFailover,
    SiteStorageTrashList, SiteStorageTrashRestore, ServerStorageReplicate, ServerStoragePruneTrash,
)


def ensure_not_offloaded(ctx, site: str, action: str) -> None:
    """Precheck for jobs that move a site directory (rename): the mounts must go first."""
    if ctx.storage.is_offloaded(site):
        raise Conflict(f"{site}: files are offloaded to object storage; move them back "
                       f"(site.storage_migrate to_local) before {action}")
