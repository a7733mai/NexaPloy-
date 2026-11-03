"""Backups: bench backup -> stage + manifest -> rclone upload -> prune (§5.5)."""

from __future__ import annotations

import configparser
import io
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .. import validation as v
from ..config import FRAPPE_UID, Settings
from ..fsutil import atomic_write, chown, ensure_dir, sha256_file
from .docker import Runner

TS_FORMAT = "%Y%m%dT%H%M%SZ"
MANIFEST = "manifest.json"

# Frappe backup file suffixes.
SUFFIX_DB = "-database.sql.gz"
SUFFIX_PUBLIC = "-files.tar"
SUFFIX_PRIVATE = "-private-files.tar"
SUFFIX_CONFIG = "-site_config_backup.json"


class BackupError(RuntimeError):
    pass


def new_ts(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime(TS_FORMAT)


def parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.strptime(ts, TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def classify(name: str) -> str | None:
    if name.endswith(SUFFIX_DB) or name.endswith("-database.sql"):
        return "database"
    if name.endswith(SUFFIX_PRIVATE) or name.endswith("-private-files.tgz"):
        return "private"
    if name.endswith(SUFFIX_PUBLIC) or name.endswith("-files.tgz"):
        return "public"
    if name.endswith(SUFFIX_CONFIG):
        return "config"
    return None


def select_prunable(timestamps: Iterable[str], retention_days: int, keep_min: int,
                    now: datetime | None = None) -> list[str]:
    """Backups to delete: older than retention, but always keep the newest ``keep_min``.

    Unparseable names are never pruned.
    """
    now = now or datetime.now(timezone.utc)
    dated = sorted(((parse_ts(t), t) for t in timestamps if parse_ts(t)), reverse=True)
    cutoff = now - timedelta(days=retention_days)
    return sorted(t for i, (dt, t) in enumerate(dated) if i >= keep_min and dt < cutoff)


def rclone_sections(remote: str, *, provider: str, endpoint: str | None, region: str | None,
                    bucket: str, access_key_id: str, secret_access_key: str) -> dict[str, dict[str, str]]:
    """rclone.conf sections for an S3 backup target.

    ``<remote>-s3`` holds the S3 credentials; ``<remote>`` is an ``alias`` of
    ``<remote>-s3:<bucket>`` so every path stays ``<remote>:<prefix>/<bench>/<site>/<ts>``
    (§5.5) regardless of the bucket.
    """
    s3: dict[str, str] = {
        "type": "s3",
        "provider": provider,
        "env_auth": "false",
        "access_key_id": access_key_id,
        "secret_access_key": secret_access_key,
    }
    if region:
        s3["region"] = region
    if endpoint:
        s3["endpoint"] = endpoint
    s3["acl"] = "private"
    s3["no_check_bucket"] = "true"
    return {
        f"{remote}-s3": s3,
        remote: {"type": "alias", "remote": f"{remote}-s3:{bucket}"},
    }


def merge_rclone_conf(existing: str, sections: dict[str, dict[str, str]]) -> str:
    """Replace ``sections`` in an rclone.conf text, keeping every other remote."""
    cp = configparser.RawConfigParser(interpolation=None, strict=False, default_section="\0none")
    cp.optionxform = str  # keep key case
    try:
        cp.read_string(existing or "")
    except configparser.Error:
        cp = configparser.RawConfigParser(interpolation=None, default_section="\0none")
        cp.optionxform = str
    for name, values in sections.items():
        if cp.has_section(name):
            cp.remove_section(name)
        cp.add_section(name)
        for k, val in values.items():
            cp.set(name, k, val)
    buf = io.StringIO()
    cp.write(buf)
    return buf.getvalue().rstrip("\n") + "\n"


@dataclass
class BackupLocation:
    bench: str
    site: str
    ts: str
    path: Path


class BackupManager:
    def __init__(self, settings: Settings, runner: Runner):
        self.settings = settings
        self.runner = runner

    # ---- paths ---------------------------------------------------------------------
    def site_root(self, bench: str, site: str) -> Path:
        return self.settings.backups_dir / v.bench_name(bench) / v.site_name(site)

    def backup_dir(self, bench: str, site: str, ts: str) -> Path:
        return self.site_root(bench, site) / v.backup_id(ts)

    def remote_dir(self, bench: str, site: str, ts: str | None = None, prefix: str | None = None) -> str:
        base = v.remote_path(prefix) if prefix else self.settings.backup_prefix
        parts = [base, v.bench_name(bench), v.site_name(site)]
        if ts:
            parts.append(v.backup_id(ts))
        return f"{self.settings.backup.remote}:{'/'.join(p for p in parts if p)}"

    def frappe_backup_dir(self, site_dir: Path) -> Path:
        return site_dir / "private" / "backups"

    # ---- create ----------------------------------------------------------------------
    @staticmethod
    def snapshot(directory: Path) -> set[str]:
        if not directory.is_dir():
            return set()
        return {p.name for p in directory.iterdir() if p.is_file()}

    def stage(self, bench: str, site: str, ts: str, source_dir: Path, names: Iterable[str],
              image: str | None) -> dict[str, Any]:
        """Move ``names`` from ``source_dir`` into the staging dir and write the manifest.

        Idempotent: files already moved are kept; the manifest is rewritten
        from what is present in the staging dir.
        """
        dest = self.backup_dir(bench, site, ts)
        ensure_dir(dest, FRAPPE_UID, FRAPPE_UID, 0o750)
        for name in names:
            src = source_dir / name
            if src.is_file() and not (dest / name).exists():
                shutil.move(str(src), str(dest / name))
                chown(dest / name, FRAPPE_UID, FRAPPE_UID)
        files = [p for p in sorted(dest.iterdir()) if p.is_file() and p.name != MANIFEST]
        if not any(classify(p.name) == "database" for p in files):
            raise BackupError(f"no database dump found in {dest}")
        manifest = {
            "site": site,
            "bench": bench,
            "image": image,
            "created_at": (parse_ts(ts) or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z"),
            "files": [
                {"name": p.name, "size": p.stat().st_size, "sha256": sha256_file(p), "type": classify(p.name)}
                for p in files
            ],
        }
        atomic_write(dest / MANIFEST, json.dumps(manifest, indent=2) + "\n", mode=0o640)
        return manifest

    def read_manifest(self, path: Path) -> dict[str, Any]:
        try:
            return json.loads((path / MANIFEST).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise BackupError(f"cannot read manifest in {path}: {exc}") from exc

    def verify(self, path: Path) -> dict[str, Any]:
        manifest = self.read_manifest(path)
        for entry in manifest.get("files", []):
            name = entry.get("name", "")
            if "/" in name or name.startswith("."):
                raise BackupError(f"unsafe file name in manifest: {name!r}")
            f = path / name
            if not f.is_file():
                raise BackupError(f"missing backup file {name}")
            digest = sha256_file(f)
            if digest != entry.get("sha256"):
                raise BackupError(f"sha256 mismatch for {name}: {digest} != {entry.get('sha256')}")
        return manifest

    # ---- rclone ----------------------------------------------------------------------
    def rclone(self, *args: str, timeout: float | None = 6 * 3600):
        return self.runner.run(
            ["rclone", "--config", str(self.settings.rclone_conf), *args], timeout=timeout
        )

    def upload(self, bench: str, site: str, ts: str):
        return self.rclone("copy", str(self.backup_dir(bench, site, ts)), self.remote_dir(bench, site, ts))

    def fetch(self, remote: str, dest: Path):
        ensure_dir(dest, mode=0o750)
        return self.rclone("copy", remote, str(dest))

    def remote_spec(self, remote_path: str) -> str:
        """``prefix/bench/site/ts`` (or ``remote:...``) -> full rclone spec."""
        if ":" in remote_path:
            remote, _, rel = remote_path.partition(":")
            if remote != self.settings.backup.remote:
                raise v.ValidationError(f"unknown rclone remote {remote!r}")
            remote_path = rel
        return f"{self.settings.backup.remote}:{v.remote_path(remote_path)}"

    def list_remote(self, bench: str, site: str) -> list[dict[str, Any]]:
        if not self.settings.rclone_remote_configured():
            return []
        res = self.rclone("lsf", "--dirs-only", self.remote_dir(bench, site), timeout=120)
        if not res.ok:
            return []
        out = []
        for line in res.stdout.splitlines():
            ts = line.strip().rstrip("/")
            if v.BACKUP_ID_RE.match(ts):
                out.append({
                    "id": ts, "bench": bench, "site": site, "location": "remote",
                    "remote_path": self.remote_dir(bench, site, ts),
                })
        return out

    def remote_ids(self, bench: str, site: str, prefix: str | None = None) -> list[str]:
        """Dated backup dirs (``YYYYmmddTHHMMSSZ``) under the site's remote dir.

        Raises :class:`BackupError` when rclone fails (a missing dir lists as empty).
        """
        res = self.rclone("lsf", "--dirs-only", self.remote_dir(bench, site, prefix=prefix), timeout=300)
        if not res.ok:
            err = (res.stderr or res.stdout).lower()
            if "directory not found" in err or "not found" in err:
                return []
            raise BackupError(f"rclone lsf failed (exit {res.returncode}): {(res.stderr or '').strip()[-300:]}")
        ids = (line.strip().rstrip("/") for line in res.stdout.splitlines())
        return sorted(i for i in ids if v.BACKUP_ID_RE.match(i))

    def purge_remote(self, bench: str, site: str, ts: str, prefix: str | None = None):
        return self.rclone("purge", self.remote_dir(bench, site, ts, prefix=prefix))

    # ---- local listing / retention ---------------------------------------------------
    def local_timestamps(self, bench: str, site: str) -> list[str]:
        root = self.site_root(bench, site)
        if not root.is_dir():
            return []
        return sorted(p.name for p in root.iterdir() if p.is_dir() and v.BACKUP_ID_RE.match(p.name))

    def find_local(self, site: str, ts: str) -> BackupLocation | None:
        v.site_name(site)
        v.backup_id(ts)
        root = self.settings.backups_dir
        if not root.is_dir():
            return None
        for bench_dir in sorted(root.iterdir()):
            p = bench_dir / site / ts
            if v.BENCH_RE.match(bench_dir.name) and (p / MANIFEST).is_file():
                return BackupLocation(bench_dir.name, site, ts, p)
        return None

    def list_local(self, site: str) -> list[dict[str, Any]]:
        v.site_name(site)
        out = []
        root = self.settings.backups_dir
        if not root.is_dir():
            return out
        for bench_dir in sorted(root.iterdir()):
            if not v.BENCH_RE.match(bench_dir.name):
                continue
            for ts in self.local_timestamps(bench_dir.name, site):
                path = bench_dir / site / ts
                try:
                    manifest = self.read_manifest(path)
                except BackupError:
                    continue
                out.append({
                    "id": ts, "bench": bench_dir.name, "site": site, "location": "local",
                    "path": str(path), "created_at": manifest.get("created_at"),
                    "image": manifest.get("image"),
                    "size": sum(int(f.get("size", 0)) for f in manifest.get("files", [])),
                    "files": manifest.get("files", []),
                })
        return sorted(out, key=lambda b: b["id"], reverse=True)

    def prune(self, bench: str, site: str, now: datetime | None = None) -> list[str]:
        doomed = select_prunable(
            self.local_timestamps(bench, site),
            self.settings.backup.retention_days,
            self.settings.backup.keep_min,
            now,
        )
        for ts in doomed:
            shutil.rmtree(self.backup_dir(bench, site, ts), ignore_errors=True)
        return doomed
