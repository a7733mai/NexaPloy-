"""Server-level and remote-backup jobs pushed by the control plane.

* ``server.configure_backups`` – write the rclone remote and ``[backup]`` in agent.toml
* ``server.health``            – return host facts (same as the heartbeat)
* ``backup.prune_remote``      – remote retention on dated backup dirs
"""

from __future__ import annotations

import re
import tomllib
from typing import Any, Literal

from pydantic import field_validator

from .. import validation as v
from ..config import update_config_file
from ..fsutil import write_if_changed
from ..runtime.backup import BackupError, merge_rclone_conf, rclone_sections, select_prunable
from ..runtime.server import server_info
from .base import BaseParams, Job, SiteParams

REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
PROVIDER_RE = re.compile(r"^[A-Za-z0-9]{1,32}$")
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,62}$")
REGION_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
ENDPOINT_RE = re.compile(r"^(https?://)?[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$")


def _no_ctl(value: str, what: str) -> str:
    if not value or any(c in value for c in "\r\n\0"):
        raise ValueError(f"invalid {what}")
    return value


# --------------------------------------------------------------------------- configure_backups
class ConfigureBackupsParams(BaseParams):
    remote: str = "nexaploy-backups"
    type: Literal["s3"] = "s3"
    provider: str = "Other"
    endpoint: str | None = None
    region: str | None = None
    bucket: str
    access_key_id: str
    secret_access_key: str
    prefix: str | None = None
    retention_days: int | None = None

    @field_validator("remote")
    @classmethod
    def _remote(cls, value: str) -> str:
        if not REMOTE_NAME_RE.match(value):
            raise ValueError("invalid rclone remote name")
        return value

    @field_validator("provider")
    @classmethod
    def _provider(cls, value: str) -> str:
        if not PROVIDER_RE.match(value or ""):
            raise ValueError("invalid provider")
        return value

    @field_validator("bucket")
    @classmethod
    def _bucket(cls, value: str) -> str:
        if not BUCKET_RE.match(value):
            raise ValueError("invalid bucket name")
        return value

    @field_validator("region")
    @classmethod
    def _region(cls, value: str | None) -> str | None:
        if value and not REGION_RE.match(value):
            raise ValueError("invalid region")
        return value or None

    @field_validator("endpoint")
    @classmethod
    def _endpoint(cls, value: str | None) -> str | None:
        if value and not ENDPOINT_RE.match(value):
            raise ValueError("invalid endpoint")
        return value or None

    @field_validator("access_key_id", "secret_access_key")
    @classmethod
    def _creds(cls, value: str) -> str:
        return _no_ctl(value, "credential")

    @field_validator("prefix")
    @classmethod
    def _prefix(cls, value: str | None) -> str | None:
        return v.remote_path(value) if value else None

    @field_validator("retention_days")
    @classmethod
    def _retention(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("retention_days must be >= 0")
        return value


class ServerConfigureBackups(Job):
    """Write ``config/rclone.conf`` (mode 600) and ``[backup]`` in agent.toml. Idempotent."""

    kind = "server.configure_backups"
    steps = ("write_rclone_conf", "update_agent_config")
    Params = ConfigureBackupsParams

    def step_write_rclone_conf(self) -> None:
        p = self.p
        path = self.settings.rclone_conf
        try:
            existing = path.read_text()
        except OSError:
            existing = ""
        sections = rclone_sections(
            p.remote, provider=p.provider, endpoint=p.endpoint, region=p.region, bucket=p.bucket,
            access_key_id=p.access_key_id, secret_access_key=p.secret_access_key,
        )
        changed = write_if_changed(path, merge_rclone_conf(existing, sections), mode=0o600)
        if changed:
            self.log(f"rclone remote {p.remote!r} -> s3 bucket {p.bucket} written to {path}")
        else:
            self.skip(f"rclone remote {p.remote!r} already configured")
        self.result.update({"remote": p.remote, "bucket": p.bucket, "rclone_conf": str(path)})

    def step_update_agent_config(self) -> None:
        p = self.p
        b = self.settings.backup
        updates: dict[str, Any] = {"remote": p.remote}
        if p.prefix:
            updates["prefix"] = p.prefix
        if p.retention_days is not None:
            updates["retention_days"] = p.retention_days
        path = self.settings.source or (self.settings.config_dir / "agent.toml")
        try:
            with path.open("rb") as fh:
                on_disk = tomllib.load(fh).get("backup", {})
        except (OSError, tomllib.TOMLDecodeError):
            on_disk = {}
        if all(on_disk.get(k) == val for k, val in updates.items()):
            self.skip(f"agent.toml [backup] already {updates}")
        else:
            update_config_file(path, {"backup": updates})
            self.log(f"agent.toml [backup] updated: {updates}")
        # Apply to the running worker too (the API process picks it up on restart).
        b.remote = p.remote
        if p.prefix:
            b.prefix = p.prefix
        if p.retention_days is not None:
            b.retention_days = p.retention_days
        self.result.update({"prefix": self.settings.backup_prefix, "agent_config": str(path)})


# --------------------------------------------------------------------------- health
class ServerHealth(Job):
    """Collect host facts on demand (result = the same facts as the heartbeat)."""

    kind = "server.health"
    steps = ("collect",)
    Params = BaseParams

    def step_collect(self) -> None:
        facts = server_info(self.ctx.docker, self.ctx.benches, self.settings.agent.server_id)
        self.result.update(facts)
        self.log(
            f"load {facts.get('load_avg')}, unhealthy containers: {facts.get('unhealthy_containers', 0)}"
        )


# --------------------------------------------------------------------------- prune_remote
class PruneRemoteParams(SiteParams):
    bench: str
    keep_days: int = 14
    keep_min: int = 3
    # Override the server's backup prefix (prune backups another server uploaded).
    prefix: str | None = None

    @field_validator("bench")
    @classmethod
    def _bench(cls, value: str) -> str:
        return v.bench_name(value)

    @field_validator("keep_days")
    @classmethod
    def _days(cls, value: int) -> int:
        if value < 1:
            raise ValueError("keep_days must be >= 1")
        return value

    @field_validator("keep_min")
    @classmethod
    def _min(cls, value: int) -> int:
        if value < 1:
            raise ValueError("keep_min must be >= 1")
        return value

    @field_validator("prefix")
    @classmethod
    def _prefix(cls, value: str | None) -> str | None:
        return v.remote_path(value) if value else None


class BackupPruneRemote(Job):
    """Delete remote backup dirs older than ``keep_days`` (always keep the newest ``keep_min``)."""

    kind = "backup.prune_remote"
    steps = ("list", "prune")
    Params = PruneRemoteParams

    def _remote_path(self, ts: str) -> str:
        return self.ctx.backups.remote_dir(self.p.bench, self.p.site, ts, prefix=self.p.prefix)

    def step_list(self) -> None:
        p = self.p
        self.result.update({"site": p.site, "bench": p.bench, "pruned": self.result.get("pruned", [])})
        if not self.settings.rclone_remote_configured():
            self.log(f"rclone remote {self.settings.backup.remote!r} not configured; nothing to prune")
            self.result.update({"remote_ids": [], "doomed": [], "configured": False})
            return
        if "doomed" in self.result:
            self.skip(f"{len(self.result['doomed'])} backups selected")
            return
        try:
            ids = self.ctx.backups.remote_ids(p.bench, p.site, prefix=p.prefix)
        except BackupError as exc:
            self.fail(str(exc))
        doomed = select_prunable(ids, p.keep_days, p.keep_min)
        self.result.update({"remote_ids": ids, "doomed": doomed, "configured": True})
        self.log(f"{len(ids)} remote backups, {len(doomed)} older than {p.keep_days} days "
                 f"(keeping at least {p.keep_min})")

    def step_prune(self) -> None:
        pruned: list[dict[str, str]] = self.result.setdefault("pruned", [])
        done = {x["ts"] for x in pruned}
        for ts in self.result.get("doomed", []):
            if ts in done:
                self.skip(f"{ts} purged")
                continue
            path = self._remote_path(ts)
            self.check(self.ctx.backups.purge_remote(self.p.bench, self.p.site, ts, prefix=self.p.prefix),
                       f"rclone purge {path}")
            pruned.append({"ts": ts, "remote_path": path})
            self.save()
        kept = [t for t in self.result.get("remote_ids", []) if t not in {x["ts"] for x in pruned}]
        self.result["kept"] = kept
        if not pruned:
            self.log("nothing to prune")
