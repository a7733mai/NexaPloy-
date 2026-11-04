"""Attachments offload to S3-compatible storage (ARCHITECTURE.md §11).

Design: one ``rclone mount`` per site and kind (``public`` / ``private``), run by a systemd
unit on the host, mounted directly on ``sites/<site>/{public,private}/files``. The bench's
``./sites`` bind mount uses ``rslave`` propagation (``SITES_PROPAGATION`` in the bench .env)
so containers see the FUSE mounts appear, restart and fail over without being recreated.

Bucket layout (every profile)::

    <bucket>/<prefix>/<site>/public/...                    live files (primary / replica)
    <bucket>/<prefix>/<site>/private/...
    <bucket>/<prefix>/.nexaploy-trash-staging/<site>/<ts>/  replica-side --backup-dir (transient)
    <trash bucket>/<prefix>/<site>/<ts>/{public,private}/... trash, pruned by age

The first half of this module is pure (argv / config / unit builders, validation) and is
unit-tested without rclone; :class:`StorageManager` does the side effects through the
:class:`~nexaploy_agent.runtime.docker.Runner`.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .. import validation as v
from ..config import FRAPPE_UID, Settings
from ..fsutil import atomic_write, ensure_dir, write_if_changed
from .backup import merge_rclone_conf
from .docker import Result, Runner

KINDS = ("public", "private")
TRASH_STAGING = ".nexaploy-trash-staging"
REMOTE_PREFIX = "nxs-"
PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,62}$")
REGION_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
ENDPOINT_RE = re.compile(r"^(https?://)?[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$")
SIZE_RE = re.compile(r"^[0-9]{1,6}[KMGT]?$")
DURATION_RE = re.compile(r"^[0-9]{1,6}[smhd]$")
TS_FORMAT = "%Y%m%dT%H%M%SZ"
TS_RE = re.compile(r"^\d{8}T\d{6}Z$")
SITES_PROPAGATION_ENV = "SITES_PROPAGATION"
# rclone S3 comparison modes for replication (cost vs precision, see §11).
COMPARE_MODES = ("size", "modtime", "checksum")

# Provider presets -> rclone S3 settings. ``endpoint`` may use ``{region}``.
# ``needs`` lists the profile fields the preset requires.
PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "AWS": {"provider": "AWS", "endpoint": None, "path_style": False, "needs": ("region",)},
    "IDrive": {"provider": "IDrive", "endpoint": None, "path_style": True, "needs": ("endpoint",)},
    "Wasabi": {"provider": "Wasabi", "endpoint": "s3.{region}.wasabisys.com", "path_style": True,
               "needs": ("region",)},
    "B2": {"provider": "Other", "endpoint": "s3.{region}.backblazeb2.com", "path_style": True,
           "needs": ("region",)},
    "R2": {"provider": "Cloudflare", "endpoint": None, "path_style": True, "region": "auto",
           "needs": ("endpoint",)},
    "MinIO": {"provider": "Minio", "endpoint": None, "path_style": True, "needs": ("endpoint",)},
    "Hetzner": {"provider": "Other", "endpoint": "{region}.your-objectstorage.com", "path_style": True,
                "needs": ("region",)},
    "Other": {"provider": "Other", "endpoint": None, "path_style": True, "needs": ("endpoint",)},
}


class StorageError(RuntimeError):
    pass


# --------------------------------------------------------------------------- profiles
@dataclass(frozen=True)
class Profile:
    """One S3-compatible bucket (a control-plane Storage Profile)."""

    name: str
    preset: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    endpoint: str | None = None
    region: str | None = None
    path_style: bool | None = None
    prefix: str = ""

    @classmethod
    def from_params(cls, data: Mapping[str, Any]) -> "Profile":
        def opt(key: str) -> str | None:
            val = data.get(key)
            return str(val).strip() if val not in (None, "") else None

        p = cls(
            name=str(data.get("name") or ""),
            preset=str(data.get("preset") or data.get("provider") or "Other"),
            bucket=str(data.get("bucket") or ""),
            access_key_id=str(data.get("access_key_id") or ""),
            secret_access_key=str(data.get("secret_access_key") or ""),
            endpoint=opt("endpoint"),
            region=opt("region"),
            path_style=data.get("path_style") if data.get("path_style") in (True, False) else None,
            prefix=(opt("prefix") or "").strip("/"),
        )
        validate_profile(p)
        return p

    @property
    def remote(self) -> str:
        return remote_name(self.name)

    def root(self, *parts: str) -> str:
        """``nxs-<name>:<bucket>/<prefix>/<parts...>``."""
        segs = [self.bucket, self.prefix, *parts]
        return f"{self.remote}:{'/'.join(s.strip('/') for s in segs if s)}"

    def site_root(self, site: str, kind: str | None = None) -> str:
        parts = [v.site_name(site)]
        if kind:
            parts.append(check_kind(kind))
        return self.root(*parts)

    def account_key(self) -> tuple[Any, ...]:
        """Profiles with the same key live in the same account (server-side copy possible)."""
        s = rclone_profile_section(self)
        return (s.get("provider"), s.get("endpoint"), s.get("region"), self.access_key_id)


def remote_name(profile_name: str) -> str:
    if not PROFILE_NAME_RE.match(profile_name or ""):
        raise v.ValidationError(f"invalid storage profile name: {profile_name!r}")
    return f"{REMOTE_PREFIX}{profile_name}"


def check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise v.ValidationError(f"invalid files kind: {kind!r}")
    return kind


def _no_ctl(value: str, what: str) -> str:
    if not value or any(c in value for c in "\r\n\0"):
        raise v.ValidationError(f"invalid {what}")
    return value


def validate_profile(p: Profile) -> Profile:
    remote_name(p.name)
    preset = PROVIDER_PRESETS.get(p.preset)
    if preset is None:
        raise v.ValidationError(f"unknown storage provider preset {p.preset!r}")
    if not BUCKET_RE.match(p.bucket):
        raise v.ValidationError(f"invalid bucket name: {p.bucket!r}")
    _no_ctl(p.access_key_id, "access key")
    _no_ctl(p.secret_access_key, "secret key")
    if p.region and not REGION_RE.match(p.region):
        raise v.ValidationError(f"invalid region: {p.region!r}")
    if p.endpoint and not ENDPOINT_RE.match(p.endpoint):
        raise v.ValidationError(f"invalid endpoint: {p.endpoint!r}")
    if p.prefix:
        v.remote_path(p.prefix)
    for need in preset["needs"]:
        if not getattr(p, need):
            raise v.ValidationError(f"provider {p.preset} needs a {need}")
    return p


def rclone_profile_section(p: Profile) -> dict[str, str]:
    """The rclone.conf section (S3 backend) for a profile."""
    preset = PROVIDER_PRESETS[p.preset]
    region = p.region or preset.get("region")
    endpoint = p.endpoint
    if not endpoint and preset.get("endpoint"):
        endpoint = preset["endpoint"].format(region=region or "")
    path_style = preset["path_style"] if p.path_style is None else p.path_style
    s: dict[str, str] = {
        "type": "s3",
        "provider": preset["provider"],
        "env_auth": "false",
        "access_key_id": p.access_key_id,
        "secret_access_key": p.secret_access_key,
    }
    if region:
        s["region"] = region
    if endpoint:
        s["endpoint"] = endpoint
    s["force_path_style"] = "true" if path_style else "false"
    s["acl"] = "private"
    s["no_check_bucket"] = "true"
    return s


def rclone_sections(profiles: Iterable[Profile]) -> dict[str, dict[str, str]]:
    return {p.remote: rclone_profile_section(p) for p in profiles}


# --------------------------------------------------------------------------- policy
def validate_policy(primary: str | None, replica: str | None, trash: str | None,
                    trash_retention_days: int | None = None) -> None:
    """Rules shared with the control plane (storage_utils.validate_policy)."""
    if not primary:
        raise v.ValidationError("offloaded storage needs a primary profile")
    if replica and replica == primary:
        raise v.ValidationError("the replica must be a different profile than the primary")
    if trash and not replica:
        raise v.ValidationError("the trash bin needs a replica (deletions are captured while replicating)")
    if trash and trash in (primary, replica):
        raise v.ValidationError("the trash must be a different profile than the primary and the replica")
    if trash and (trash_retention_days is None or trash_retention_days < 1):
        raise v.ValidationError("trash_retention_days must be >= 1")


# --------------------------------------------------------------------------- argv builders
def rclone_base(rclone: str, config: Path | str) -> list[str]:
    return [rclone, "--config", str(config)]


def mount_argv(rclone: str, config: Path | str, remote: str, mountpoint: Path | str,
               cache_dir: Path | str, log_file: Path | str, cache_max_size: str = "2G",
               cache_max_age: str = "72h", uid: int = FRAPPE_UID, gid: int = FRAPPE_UID) -> list[str]:
    """``rclone mount`` for one site/kind.

    * ``--vfs-cache-mode full``: Frappe needs seekable reads (images, PDFs, zip) and
      writes that are acknowledged locally, then uploaded (``--vfs-write-back``). Dirty
      files survive a crash in ``cache_dir`` and are uploaded on the next start.
    * bounded cache (``--vfs-cache-max-size``/``-age``) keeps the server (almost) empty.
    * ``--allow-other`` + uid/gid 3113: the bench containers (uid 3113) use the mount.
    * no ``--allow-non-empty``: never hide local files under the mount.
    """
    if not SIZE_RE.match(cache_max_size):
        raise v.ValidationError(f"invalid cache size {cache_max_size!r}")
    if not DURATION_RE.match(cache_max_age):
        raise v.ValidationError(f"invalid cache age {cache_max_age!r}")
    return [
        *rclone_base(rclone, config), "mount", remote, str(mountpoint),
        "--allow-other", "--uid", str(uid), "--gid", str(gid), "--umask", "0027",
        "--vfs-cache-mode", "full",
        "--vfs-cache-max-size", cache_max_size,
        "--vfs-cache-max-age", cache_max_age,
        "--vfs-write-back", "5s",
        "--cache-dir", str(cache_dir),
        "--dir-cache-time", "5m",
        "--poll-interval", "0",
        "--attr-timeout", "1s",
        "--transfers", "4",
        "--log-file", str(log_file),
        "--log-level", "NOTICE",
    ]


def systemd_unit(description: str, argv: list[str], mountpoint: Path | str, fusermount: str) -> str:
    """systemd unit running one ``rclone mount`` (Type=notify: rclone signals readiness)."""
    def q(arg: str) -> str:
        if re.match(r"^[A-Za-z0-9_@%+=:,./-]+$", arg):
            return arg
        return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'

    exec_start = " ".join(q(a) for a in argv)
    mp = q(str(mountpoint))
    return (
        "# Generated by the Nexaploy agent (ARCHITECTURE.md §11). Do not edit.\n"
        "[Unit]\n"
        f"Description={description}\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "# mount before the bench containers start on boot\n"
        "Before=docker.service\n"
        "StartLimitIntervalSec=0\n"
        "\n"
        "[Service]\n"
        "Type=notify\n"
        f"ExecStartPre=-{fusermount} -uz {mp}\n"
        f"ExecStart={exec_start}\n"
        f"ExecStopPost=-{fusermount} -uz {mp}\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "TimeoutStopSec=120\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def unit_name(site: str, kind: str) -> str:
    return f"nexaploy-files-{v.slug(v.site_name(site))}-{check_kind(kind)}.service"


def sync_argv(rclone: str, config: Path | str, src: str, dst: str, backup_dir: str | None = None,
              max_delete: int | None = None, compare: str = "size") -> list[str]:
    """``rclone sync src dst`` - deletions and overwrites in ``dst`` go to ``backup_dir``.

    ``--backup-dir`` must be on the same remote as ``dst`` (rclone rule), see
    :func:`trash_plan`. ``--max-delete`` stops a runaway sync (e.g. an empty primary).
    """
    if compare not in COMPARE_MODES:
        raise v.ValidationError(f"invalid compare mode {compare!r}")
    argv = [*rclone_base(rclone, config), "sync", src, dst, "--fast-list",
            "--retries", "3", "--low-level-retries", "10", "--stats-log-level", "NOTICE", "--stats", "0"]
    if compare == "size":
        argv.append("--size-only")
    elif compare == "checksum":
        argv.append("--checksum")
    if backup_dir:
        argv += ["--backup-dir", backup_dir]
    if max_delete is not None and max_delete >= 0:
        argv += ["--max-delete", str(max_delete)]
    return argv


def move_argv(rclone: str, config: Path | str, src: str, dst: str) -> list[str]:
    return [*rclone_base(rclone, config), "move", src, dst, "--delete-empty-src-dirs", "--retries", "3"]


def copy_argv(rclone: str, config: Path | str, src: str, dst: str, single_file: bool = False) -> list[str]:
    return [*rclone_base(rclone, config), "copyto" if single_file else "copy", src, dst, "--retries", "3"]


def check_argv(rclone: str, config: Path | str, src: str, dst: str) -> list[str]:
    """Every file of ``src`` exists in ``dst`` with the same size (extra files in dst are fine)."""
    return [*rclone_base(rclone, config), "check", src, dst, "--one-way", "--size-only"]


def prune_trash_argv(rclone: str, config: Path | str, trash_site_root: str, retention_days: int) -> list[str]:
    if retention_days < 1:
        raise v.ValidationError("retention_days must be >= 1")
    return [*rclone_base(rclone, config), "delete", trash_site_root, "--min-age", f"{int(retention_days)}d",
            "--rmdirs"]


def new_ts(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime(TS_FORMAT)


@dataclass(frozen=True)
class TrashPlan:
    """Where a sync's --backup-dir points and where its content ends up."""

    backup_dir: str           # --backup-dir for this run (same remote as the sync destination)
    staging_root: str | None  # moved to ``trash_root`` after the sync (None: backup_dir is the trash)
    trash_root: str           # <trash>:<bucket>/<prefix>/<site>


def trash_plan(dst: Profile, trash: Profile, site: str, ts: str) -> TrashPlan:
    """Plan --backup-dir for a sync into ``dst`` whose deletions belong in ``trash``.

    rclone requires --backup-dir on the same remote as the destination. When both profiles
    are in the same account (same provider, endpoint, region and key) the trash bucket is
    addressed through the destination's remote and rclone moves server-side; otherwise the
    deleted files are staged in the destination bucket under ``.nexaploy-trash-staging``
    (outside ``<prefix>/<site>``, so it never overlaps the sync) and moved to the trash
    afterwards.
    """
    if not TS_RE.match(ts):
        raise v.ValidationError(f"invalid timestamp {ts!r}")
    site = v.site_name(site)
    trash_root = trash.site_root(site)
    if dst.account_key() == trash.account_key():
        segs = [trash.bucket, trash.prefix, site, ts]
        return TrashPlan(f"{dst.remote}:{'/'.join(s for s in segs if s)}", None, trash_root)
    staging = dst.root(TRASH_STAGING, site)
    return TrashPlan(f"{staging}/{ts}", staging, trash_root)


def parse_trash_item(item: str) -> tuple[str, str | None, str | None]:
    """``<ts>[/<kind>[/<path>]]`` (relative to the site's trash root) -> (ts, kind, path)."""
    item = (item or "").strip().strip("/")
    parts = item.split("/") if item else []
    if not parts or not TS_RE.match(parts[0]):
        raise v.ValidationError(f"invalid trash item {item!r}: expected <timestamp>/<public|private>/<path>")
    kind = check_kind(parts[1]) if len(parts) > 1 else None
    rest = "/".join(parts[2:]) or None
    if rest is not None:
        if any(p in ("", ".", "..") for p in parts[2:]) or any(c in rest for c in "\r\n\0\\"):
            raise v.ValidationError(f"invalid path in trash item {item!r}")
    return parts[0], kind, rest


def parse_lsjson(text: str, limit: int = 2000) -> list[dict[str, Any]]:
    try:
        rows = json.loads(text or "[]")
    except json.JSONDecodeError:
        return []
    out = []
    for r in rows:
        if r.get("IsDir"):
            continue
        out.append({"path": r.get("Path"), "size": r.get("Size"), "mod_time": r.get("ModTime")})
        if len(out) >= limit:
            break
    return out


def propagation_ok(findmnt_output: str) -> bool:
    """``findmnt -o PROPAGATION`` of the bench dir: Docker's rslave needs shared or slave."""
    value = (findmnt_output or "").strip().lower()
    return "shared" in value or "slave" in value


# --------------------------------------------------------------------------- manager
class StorageManager:
    """Side effects: rclone config, systemd units, local state, file moves."""

    def __init__(self, settings: Settings, runner: Runner):
        self.settings = settings
        self.runner = runner

    # ---- paths / state --------------------------------------------------------------
    @property
    def config_path(self) -> Path:
        return self.settings.storage_rclone_conf

    def state_path(self, site: str) -> Path:
        return self.settings.storage_dir / "state" / f"{v.site_name(site)}.json"

    def cache_dir(self, site: str, kind: str) -> Path:
        return self.settings.storage_dir / "cache" / v.site_name(site) / check_kind(kind)

    def log_file(self, site: str, kind: str) -> Path:
        return self.settings.storage_dir / "logs" / f"{v.site_name(site)}-{check_kind(kind)}.log"

    def unit_path(self, site: str, kind: str) -> Path:
        return Path(self.settings.storage.systemd_dir) / unit_name(site, kind)

    @staticmethod
    def files_dir(site_dir: Path, kind: str) -> Path:
        return site_dir / check_kind(kind) / "files"

    def read_state(self, site: str) -> dict[str, Any]:
        try:
            return json.loads(self.state_path(site).read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def write_state(self, site: str, state: Mapping[str, Any]) -> None:
        ensure_dir(self.state_path(site).parent, mode=0o700)
        atomic_write(self.state_path(site), json.dumps(dict(state), indent=2, sort_keys=True) + "\n", mode=0o600)

    def update_state(self, site: str, **changes: Any) -> dict[str, Any]:
        state = self.read_state(site)
        state.update(changes)
        state["site"] = site
        self.write_state(site, state)
        return state

    def remove_state(self, site: str) -> None:
        try:
            self.state_path(site).unlink()
        except OSError:
            pass

    def is_offloaded(self, site: str) -> bool:
        """Files are (being) served from a bucket mount: backups skip them, renames refuse."""
        return self.read_state(site).get("mode") in ("Offloaded", "Migrating")

    # ---- rclone ---------------------------------------------------------------------
    def rclone(self, *args: str, timeout: float | None = 6 * 3600) -> Result:
        return self.runner.run([*rclone_base(self.settings.storage.rclone, self.config_path), *args],
                               timeout=timeout)

    def run(self, argv: list[str], timeout: float | None = 6 * 3600) -> Result:
        return self.runner.run(argv, timeout=timeout)

    def ensure_remotes(self, profiles: Iterable[Profile]) -> bool:
        """Write/refresh the profiles' remotes in ``storage-rclone.conf`` (mode 600)."""
        try:
            existing = self.config_path.read_text()
        except OSError:
            existing = ""
        return write_if_changed(self.config_path, merge_rclone_conf(existing, rclone_sections(profiles)),
                                mode=0o600)

    # ---- mounts -----------------------------------------------------------------------
    def mount_argv(self, profile: Profile, site: str, kind: str, mountpoint: Path,
                   cache_max_size: str | None = None) -> list[str]:
        st = self.settings.storage
        return mount_argv(
            st.rclone, self.config_path, profile.site_root(site, kind), mountpoint,
            self.cache_dir(site, kind), self.log_file(site, kind),
            cache_max_size or st.cache_max_size, st.cache_max_age,
        )

    def write_unit(self, profile: Profile, site: str, kind: str, mountpoint: Path,
                   cache_max_size: str | None = None) -> bool:
        ensure_dir(self.cache_dir(site, kind), mode=0o700)
        ensure_dir(self.log_file(site, kind).parent, mode=0o750)
        text = systemd_unit(
            f"Nexaploy files mount: {site} ({kind}) -> {profile.name}",
            self.mount_argv(profile, site, kind, mountpoint, cache_max_size),
            mountpoint, self.settings.storage.fusermount,
        )
        return write_if_changed(self.unit_path(site, kind), text, mode=0o644)

    def remove_unit(self, site: str, kind: str) -> bool:
        path = self.unit_path(site, kind)
        if not path.exists():
            return False
        path.unlink()
        return True

    def systemctl(self, *args: str, timeout: float = 180) -> Result:
        return self.runner.run(["systemctl", *args], timeout=timeout)

    def is_mounted(self, path: Path) -> bool:
        res = self.runner.run(["findmnt", "-n", "-o", "FSTYPE", "--mountpoint", str(path)], timeout=30)
        return res.ok and "fuse" in res.stdout

    def unit_active(self, site: str, kind: str) -> bool:
        res = self.systemctl("is-active", unit_name(site, kind), timeout=30)
        return res.ok and res.stdout.strip() == "active"

    def host_propagation(self, path: Path) -> str:
        res = self.runner.run(["findmnt", "-n", "-o", "PROPAGATION", "--target", str(path)], timeout=30)
        return res.stdout.strip() if res.ok else ""

    def flush_dir_cache(self, site: str) -> None:
        """SIGHUP makes rclone mount drop its directory cache (files restored server-side)."""
        for kind in KINDS:
            self.systemctl("kill", "--signal=HUP", unit_name(site, kind), timeout=30)

    # ---- local file moves -----------------------------------------------------------------
    @staticmethod
    def make_mountpoint(path: Path) -> None:
        """Empty, root-owned 0755 dir: if the mount is down, Frappe (uid 3113) cannot write
        into it, so uploads fail loudly instead of silently landing on the server's disk."""
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o755)

    @staticmethod
    def dir_empty(path: Path) -> bool:
        return not path.exists() or (path.is_dir() and not any(path.iterdir()))

    @staticmethod
    def remove_tree(path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)
