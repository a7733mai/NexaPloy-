"""Server-level and remote-backup jobs pushed by the control plane.

* ``server.configure_backups`` – write the rclone remote (S3, Google Drive, OneDrive,
  Dropbox) and ``[backup]`` in agent.toml
* ``server.health``            – return host facts (same as the heartbeat)
* ``backup.prune_remote``      – remote retention on dated backup dirs
* ``backup.test_remote``       – check the backup storage is reachable and writable
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .. import validation as v
from ..config import update_config_file
from ..fsutil import write_if_changed
from ..runtime.backup import (
    BackupError, merge_rclone_conf, oauth_sections, plan_backup_remote, rclone_sections,
    select_prunable_gfs,
)
from ..runtime.server import server_info
from .base import BaseParams, Job, SiteParams

REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
PROVIDER_RE = re.compile(r"^[A-Za-z0-9]{1,32}$")
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,62}$")
REGION_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
ENDPOINT_RE = re.compile(r"^(https?://)?[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$")


CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,255}$")
DRIVE_FOLDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
ONEDRIVE_ID_RE = re.compile(r"^[A-Za-z0-9!._-]{1,256}$")
TENANT_RE = re.compile(r"^[A-Za-z0-9.-]{1,128}$")
EXPIRY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$")
TOKEN_MAX = 16384
TOKEN_KEYS = ("access_token", "token_type", "refresh_token", "expiry")


def read_rclone_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _no_ctl(value: str, what: str) -> str:
    if not value or any(c in value for c in "\r\n\0"):
        raise ValueError(f"invalid {what}")
    return value


def oauth_token(value: Any) -> str:
    """Validate an rclone OAuth token and return it as compact one-line JSON.

    Must be a JSON object with non-empty ``access_token`` and ``refresh_token`` strings
    (rclone refreshes with the latter); ``token_type`` and an RFC 3339 ``expiry`` are
    optional. Other keys are dropped.
    """
    if isinstance(value, str):
        if len(value) > TOKEN_MAX:
            raise ValueError("token too long")
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            raise ValueError("token must be a JSON object") from None
    if not isinstance(value, dict):
        raise ValueError("token must be a JSON object")
    out: dict[str, str] = {}
    for key in TOKEN_KEYS:
        val = value.get(key)
        if val in (None, "") and key in ("token_type", "expiry"):
            continue
        if not isinstance(val, str) or not val or len(val) > 8192 or any(
            ord(c) < 32 or ord(c) == 127 for c in val
        ):
            raise ValueError(f"token.{key} missing or invalid")
        out[key] = val
    if "expiry" in out and not EXPIRY_RE.match(out["expiry"]):
        raise ValueError("token.expiry must be an RFC 3339 timestamp")
    out.setdefault("token_type", "Bearer")
    return json.dumps(out, separators=(",", ":"))


# --------------------------------------------------------------------------- configure_backups
class ConfigureBackupsParams(BaseParams):
    """``type`` selects the backend: ``s3`` (static keys) or the OAuth backends ``drive``
    (Google Drive), ``onedrive`` and ``dropbox`` (``token`` = rclone token JSON)."""

    remote: str = "nexaploy-backups"
    type: Literal["s3", "drive", "onedrive", "dropbox"] = "s3"
    # s3
    provider: str = "Other"
    endpoint: str | None = None
    region: str | None = None
    bucket: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    # OAuth backends
    token: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    root_folder_id: str | None = None   # drive
    drive_id: str | None = None         # onedrive
    drive_type: Literal["personal", "business", "documentLibrary"] | None = None  # onedrive
    tenant: str | None = None           # onedrive (Microsoft Entra tenant; default "common")
    prefix: str | None = None
    retention_days: int | None = None

    @field_validator("token", mode="before")
    @classmethod
    def _token(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        return oauth_token(value)

    @field_validator("client_id")
    @classmethod
    def _client_id(cls, value: str | None) -> str | None:
        if value and not CLIENT_ID_RE.match(value):
            raise ValueError("invalid client_id")
        return value or None

    @field_validator("client_secret")
    @classmethod
    def _client_secret(cls, value: str | None) -> str | None:
        if value and (len(value) > 1024 or any(ord(c) < 32 for c in value)):
            raise ValueError("invalid client_secret")
        return value or None

    @field_validator("root_folder_id")
    @classmethod
    def _root_folder(cls, value: str | None) -> str | None:
        if value and not DRIVE_FOLDER_ID_RE.match(value):
            raise ValueError("invalid root_folder_id")
        return value or None

    @field_validator("drive_id")
    @classmethod
    def _drive_id(cls, value: str | None) -> str | None:
        if value and not ONEDRIVE_ID_RE.match(value):
            raise ValueError("invalid drive_id")
        return value or None

    @field_validator("tenant")
    @classmethod
    def _tenant(cls, value: str | None) -> str | None:
        if value and not TENANT_RE.match(value):
            raise ValueError("invalid tenant")
        return value or None

    @model_validator(mode="after")
    def _by_type(self) -> "ConfigureBackupsParams":
        if self.type == "s3":
            missing = [k for k in ("bucket", "access_key_id", "secret_access_key") if not getattr(self, k)]
            if missing:
                raise ValueError(f"s3 backups need {', '.join(missing)}")
            if self.token:
                raise ValueError("token is only for drive/onedrive/dropbox")
            return self
        if not self.token:
            raise ValueError(f"{self.type} backups need an OAuth token")
        if self.bucket or self.access_key_id or self.secret_access_key:
            raise ValueError(f"bucket/access keys are only for s3, not {self.type}")
        if bool(self.client_id) != bool(self.client_secret):
            raise ValueError("client_id and client_secret go together")
        if self.type == "onedrive" and not (self.drive_id and self.drive_type):
            raise ValueError("onedrive backups need drive_id and drive_type")
        if self.type != "drive" and self.root_folder_id:
            raise ValueError("root_folder_id is only for drive")
        if self.type != "onedrive" and (self.drive_id or self.drive_type or self.tenant):
            raise ValueError("drive_id/drive_type/tenant are only for onedrive")
        return self

    def sections(self) -> dict[str, dict[str, str]]:
        if self.type == "s3":
            return rclone_sections(
                self.remote, provider=self.provider, endpoint=self.endpoint, region=self.region,
                bucket=self.bucket, access_key_id=self.access_key_id,
                secret_access_key=self.secret_access_key,
            )
        return oauth_sections(
            self.remote, self.type, token=self.token, client_id=self.client_id,
            client_secret=self.client_secret, root_folder_id=self.root_folder_id,
            drive_id=self.drive_id, drive_type=self.drive_type, tenant=self.tenant,
        )

    def describe(self) -> str:
        return f"s3 bucket {self.bucket}" if self.type == "s3" else self.type

    def token_secrets(self) -> list[str]:
        if not self.token:
            return []
        data = json.loads(self.token)
        return [data.get("access_token"), data.get("refresh_token")]

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
    def _bucket(cls, value: str | None) -> str | None:
        if value is None:
            return None
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
    def _creds(cls, value: str | None) -> str | None:
        return None if value is None else _no_ctl(value, "credential")

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
        for s in p.token_secrets():
            self.secret(s)
        path = self.settings.rclone_conf
        try:
            existing = path.read_text()
        except OSError:
            existing = ""
        state = read_rclone_state(self.settings.rclone_state)
        text, new_state, kept = plan_backup_remote(existing, p.remote, p.type, p.sections(), state)
        changed = write_if_changed(path, text, mode=0o600)
        if new_state:
            write_if_changed(self.settings.rclone_state, json.dumps(new_state, indent=2, sort_keys=True) + "\n",
                             mode=0o600)
        elif self.settings.rclone_state.exists():
            self.settings.rclone_state.unlink()
        if kept:
            self.log("keeping the OAuth token rclone refreshed on this server (same connection)")
        if changed:
            self.log(f"rclone remote {p.remote!r} -> {p.describe()} written to {path}")
        else:
            self.skip(f"rclone remote {p.remote!r} already configured")
        self.result.update({"remote": p.remote, "type": p.type, "rclone_conf": str(path)})
        if p.type == "s3":
            self.result["bucket"] = p.bucket

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
        facts["os_updates"] = self.ctx.os_updates.status()
        facts["health"] = self.ctx.health.light(force=True)
        self.result.update(facts)
        self.log(
            f"load {facts.get('load_avg')}, unhealthy containers: {facts.get('unhealthy_containers', 0)}"
        )


# --------------------------------------------------------------------------- prune_remote
class PruneRemoteParams(SiteParams):
    bench: str
    keep_days: int = 14
    keep_min: int = 3
    # Grandfather-father-son: also keep the newest backup of the last N days/weeks/months.
    keep_daily: int = Field(default=0, ge=0, le=366)
    keep_weekly: int = Field(default=0, ge=0, le=520)
    keep_monthly: int = Field(default=0, ge=0, le=240)
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
        doomed = select_prunable_gfs(ids, p.keep_days, p.keep_min, p.keep_daily, p.keep_weekly, p.keep_monthly)
        self.result.update({"remote_ids": ids, "doomed": doomed, "configured": True})
        gfs = (f", {p.keep_daily} daily/{p.keep_weekly} weekly/{p.keep_monthly} monthly"
               if p.keep_daily or p.keep_weekly or p.keep_monthly else "")
        self.log(f"{len(ids)} remote backups, {len(doomed)} older than {p.keep_days} days "
                 f"(keeping at least {p.keep_min}{gfs})")

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


# --------------------------------------------------------------------------- test_remote
class TestRemoteParams(BaseParams):
    """Without ``destination``: test the configured backup remote. With ``destination``
    (same shape as ``server.configure_backups`` params): test that storage from a throw-away
    rclone config, without touching the server's active backup remote."""

    destination: ConfigureBackupsParams | None = None
    prefix: str | None = None

    @field_validator("prefix")
    @classmethod
    def _prefix(cls, value: str | None) -> str | None:
        return v.remote_path(value) if value else None


TEST_MARKER = ".nexaploy-connection-test"


class BackupTestRemote(Job):
    """Check that backup storage is reachable and writable: mkdir the prefix, write and
    delete a marker file, list the prefix and (where the backend supports it) read the quota."""

    kind = "backup.test_remote"
    steps = ("check",)
    Params = TestRemoteParams

    def step_check(self) -> None:
        p = self.p
        dest = p.destination
        conf: Path | None = None
        if dest is not None:
            for s in dest.token_secrets():
                self.secret(s)
            conf = self.settings.config_dir / f".rclone-test-{self.id}.conf"
            write_if_changed(conf, merge_rclone_conf("", dest.sections()), mode=0o600)
            remote, backend = dest.remote, dest.type
            prefix = p.prefix or dest.prefix or self.settings.backup_prefix
        else:
            if not self.settings.rclone_remote_configured():
                self.fail(f"rclone remote {self.settings.backup.remote!r} is not configured on this server")
            remote, backend = self.settings.backup.remote, None
            prefix = p.prefix or self.settings.backup_prefix
        base = f"{remote}:{prefix}"
        rc = self.ctx.backups.rclone
        try:
            self.check(rc("mkdir", base, timeout=180, config=conf), f"rclone mkdir {base}")
            marker = f"{base}/{TEST_MARKER}"
            self.check(rc("touch", marker, timeout=180, config=conf), "writing a test file")
            self.check(rc("deletefile", marker, timeout=180, config=conf), "deleting the test file")
            res = self.check(rc("lsf", "--max-depth", "1", base, timeout=180, config=conf), f"rclone lsf {base}")
            entries = [line for line in res.stdout.splitlines() if line.strip()]
            about = None
            res = rc("about", "--json", f"{remote}:", timeout=120, config=conf)
            if res.ok:  # S3 has no quota: about fails there, which is fine
                try:
                    data = json.loads(res.stdout or "{}")
                    about = {k: data[k] for k in ("total", "used", "free", "trashed") if isinstance(data.get(k), int)}
                except (json.JSONDecodeError, AttributeError):
                    about = None
        finally:
            if conf is not None:
                try:
                    os.unlink(conf)
                except OSError:
                    pass
        self.log(f"{base} is writable ({len(entries)} entries)")
        self.result.update({
            "ok": True, "remote": remote, "type": backend, "prefix": prefix,
            "entries": len(entries), "about": about,
        })
