"""The control plane as a bench of this server (all-in-one, ARCHITECTURE.md §5.7.1, §5.9).

Pure helpers for ``controlplane.update`` (step planning and rollback decisions), the
``config/control-plane.env`` file written by ``install.sh``, and the single-file control plane
backup (``nexaploy controlplane backup --to DIR`` / ``install.sh --restore-control-plane``):

* :func:`export_tarball` packs one staged backup dir (``manifest.json`` + database dump +
  public/private files + ``site_config_backup.json`` with the encryption key) into one ``.tar``;
* :func:`import_backup` takes a tarball, a staged dir, a dir of staged dirs (newest wins) or a
  plain Frappe backup dir (manifest generated) and stages it under
  ``backups/<bench>/<site>/<ts>/`` so ``site.restore {backup_id}`` can restore it.
"""

from __future__ import annotations

import json
import re
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..config import read_env_file
from ..fsutil import atomic_write, sha256_file
from .backup import MANIFEST, BackupError, classify, new_ts, parse_ts

CP_ENV_NAME = "control-plane.env"
DEFAULT_PORT = 8090
DEFAULT_IMAGE_REPO = "ghcr.io/a7733mai/nexaploy-control-plane"
ROLLBACK_REPO = "nexaploy-cp-rollback"
TARBALL_PREFIX = "nexaploy-control-plane"

# Steps of controlplane.update, in order. Only these can trigger a rollback.
UPDATE_STEPS = ("prepare", "backup", "pull", "switch", "migrate", "health", "finalize")
ROLLBACK_STEPS = frozenset({"switch", "migrate", "health"})


class ControlPlaneError(RuntimeError):
    pass


# --------------------------------------------------------------------------- control-plane.env
def cp_env_path(config_dir: Path) -> Path:
    return Path(config_dir) / CP_ENV_NAME


def read_cp_env(config_dir: Path) -> dict[str, str]:
    """``DOMAIN``, ``BENCH``, ``IMAGE``, ``PORT``, ``ADMIN_PASSWORD`` (install.sh); {} when absent."""
    return read_env_file(cp_env_path(config_dir))


def set_cp_env(config_dir: Path, **values: str) -> bool:
    """Rewrite keys of control-plane.env in place (mode 600). False when the file is absent."""
    path = cp_env_path(config_dir)
    if not path.is_file():
        return False
    lines = path.read_text().splitlines()
    seen: set[str] = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in values:
            out.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in values.items() if k not in seen]
    atomic_write(path, "\n".join(out) + "\n", mode=0o600)
    return True


def cp_port(env: dict[str, str], explicit: int | None = None) -> int:
    if explicit:
        return int(explicit)
    try:
        return int(env.get("PORT") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


# --------------------------------------------------------------------------- update planning
def rollback_tag(job_id: str) -> str:
    """Local tag pinning the image the control plane ran before the update (a floating tag
    such as ``:16`` is moved by the pull, so the old name alone cannot bring it back)."""
    return f"{ROLLBACK_REPO}:{re.sub(r'[^a-z0-9_.-]', '', job_id.lower())[:120] or 'previous'}"


def up_to_date(previous_image: str | None, image: str, previous_id: str | None, new_id: str | None) -> bool:
    """Same image name and the pull did not change its content: nothing to switch or migrate."""
    return bool(previous_image == image and previous_id and new_id and previous_id == new_id)


def rollback_plan(failed_step: str, *, switched: bool, migrate_started: bool,
                  backup_id: str | None) -> list[str]:
    """What to undo after ``failed_step`` failed.

    * before ``switch`` nothing changed on the running control plane -> nothing to do;
    * once the image was switched -> ``switch_back`` (previous image, compose up);
    * once ``migrate`` started the schema may be half-migrated -> ``restore_database`` from the
      pre-update backup (only when one was taken), run with the previous image, i.e. the code the
      backup was taken with;
    * always finish with ``health`` (is the old control plane answering again?).
    """
    if failed_step not in ROLLBACK_STEPS or not switched:
        return []
    actions = ["switch_back"]
    if migrate_started and backup_id:
        actions.append("restore_database")
    actions.append("health")
    return actions


def is_pong(status: int | None, body: str | None) -> bool:
    if status != 200:
        return False
    try:
        return json.loads(body or "{}").get("message") == "pong"
    except (ValueError, AttributeError):
        return False


def ping_url(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}/api/method/ping"


def image_for_version(version: str, repo: str = DEFAULT_IMAGE_REPO) -> str:
    return f"{repo.rstrip('/')}:{version.lstrip('v')}"


# --------------------------------------------------------------------------- backup tarball
def export_tarball(backup_dir: Path, dest_dir: Path, site: str) -> Path:
    """Pack a staged backup dir (manifest + files) into ``<dest>/nexaploy-control-plane-<site>-<ts>.tar``.

    The files are already compressed (sql.gz, tar), so the outer archive is a plain tar. Mode 600:
    it holds the whole database and the encryption key.
    """
    backup_dir = Path(backup_dir)
    if not (backup_dir / MANIFEST).is_file():
        raise BackupError(f"no {MANIFEST} in {backup_dir}")
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"{TARBALL_PREFIX}-{site}-{backup_dir.name}.tar"
    tmp = out.with_suffix(".tar.part")
    with tarfile.open(tmp, "w") as tar:
        for p in sorted(backup_dir.iterdir()):
            if p.is_file() and not p.name.startswith("."):
                tar.add(p, arcname=f"{backup_dir.name}/{p.name}")
    tmp.chmod(0o600)
    tmp.replace(out)
    return out


def _safe_extract(tar_path: Path, dest: Path) -> None:
    dest = Path(dest).resolve()
    with tarfile.open(tar_path) as tar:
        for m in tar.getmembers():
            target = (dest / m.name).resolve()
            inside = target == dest or str(target).startswith(str(dest) + "/")
            if not (m.isfile() or m.isdir()) or not inside:
                raise BackupError(f"unsafe entry in {tar_path.name}: {m.name!r}")
        for m in tar.getmembers():
            if m.isdir():
                (dest / m.name).mkdir(parents=True, exist_ok=True)
                continue
            (dest / m.name).parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(m)
            assert src is not None
            with src, open(dest / m.name, "wb") as fh:
                shutil.copyfileobj(src, fh)


def _backup_files(d: Path) -> list[Path]:
    return [p for p in sorted(d.iterdir()) if p.is_file() and classify(p.name)]


def find_backup_dir(root: Path) -> Path:
    """The dir holding the backup: ``root`` itself (manifest or Frappe files), else the newest
    sub dir (by name: ``YYYYmmddTHHMMSSZ`` sorts by time) that has one."""
    root = Path(root)
    if (root / MANIFEST).is_file() or any(classify(p.name) == "database" for p in _backup_files(root)):
        return root
    candidates = []
    for p in root.rglob(MANIFEST):
        candidates.append(p.parent)
    if not candidates:
        for p in root.rglob("*"):
            if p.is_file() and classify(p.name) == "database":
                candidates.append(p.parent)
    if not candidates:
        raise BackupError(f"no control plane backup found in {root} (expected manifest.json or *-database.sql.gz)")
    return sorted(set(candidates), key=lambda c: (c.name, str(c)))[-1]


def _choose_latest(files: Iterable[Path]) -> dict[str, Path]:
    """One file per kind (database/public/private/config): the newest name wins (Frappe names
    start with ``YYYYmmdd_HHMMSS``)."""
    out: dict[str, Path] = {}
    for p in sorted(files, key=lambda f: f.name):
        kind = classify(p.name)
        if kind:
            out[kind] = p
    return out


def import_backup(source: Path, dest_root: Path, *, site: str, bench: str, image: str | None = None,
                  now: datetime | None = None) -> tuple[str, Path]:
    """Stage ``source`` (tarball or dir) as ``<dest_root>/<ts>/`` (dest_root =
    ``backups/<bench>/<site>``) with a verified manifest. Returns ``(ts, dir)``."""
    source = Path(source)
    dest_root = Path(dest_root)
    if not source.exists():
        raise BackupError(f"{source} does not exist")
    work: Path | None = None
    try:
        if source.is_file():
            if not tarfile.is_tarfile(source):
                raise BackupError(f"{source} is not a tar archive")
            work = dest_root.parent / f".import-{new_ts(now)}"
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True)
            _safe_extract(source, work)
            source = work
        src = find_backup_dir(source)
        manifest: dict[str, Any] | None = None
        if (src / MANIFEST).is_file():
            try:
                manifest = json.loads((src / MANIFEST).read_text())
            except (OSError, ValueError) as exc:
                raise BackupError(f"cannot read {src / MANIFEST}: {exc}") from exc
            names = [f.get("name", "") for f in manifest.get("files", [])]
            for n in names:
                if "/" in n or n.startswith(".") or not (src / n).is_file():
                    raise BackupError(f"backup file {n!r} listed in the manifest is missing")
            files = [src / n for n in names]
        else:
            files = list(_choose_latest(_backup_files(src)).values())
        if not any(classify(p.name) == "database" for p in files):
            raise BackupError(f"no database dump in {src}")
        ts = src.name if parse_ts(src.name) else new_ts(now)
        dest = dest_root / ts
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        for p in files:
            shutil.copy2(p, dest / p.name)
            (dest / p.name).chmod(0o640)
        created = (manifest or {}).get("created_at") or (parse_ts(ts) or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
        out = {
            "site": site, "bench": bench, "image": (manifest or {}).get("image") or image,
            "created_at": created, "imported_from": (manifest or {}).get("site") or None,
            "files": [{"name": p.name, "size": (dest / p.name).stat().st_size,
                       "sha256": sha256_file(dest / p.name), "type": classify(p.name)} for p in files],
        }
        if manifest:
            for f in manifest.get("files", []):
                mine = next((x for x in out["files"] if x["name"] == f.get("name")), None)
                if mine and f.get("sha256") and f["sha256"] != mine["sha256"]:
                    shutil.rmtree(dest, ignore_errors=True)
                    raise BackupError(f"sha256 mismatch for {f['name']} (corrupted download?)")
        atomic_write(dest / MANIFEST, json.dumps(out, indent=2) + "\n", mode=0o640)
        return ts, dest
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)


SOURCE_KINDS = ("url", "rclone", "path")


def source_kind(source: str) -> str:
    """``https://…`` -> url, ``remote:path`` / ``:backend,opt=…:path`` -> rclone, else a local path."""
    if re.match(r"^https?://", source):
        return "url"
    if source.startswith(("/", "./", "../", "~")):
        return "path"
    try:
        exists = Path(source).exists()
    except OSError:  # e.g. PermissionError on an unreadable parent: not decidable, keep going
        exists = False
    if exists:
        return "path"
    if re.match(r"^:?[A-Za-z0-9_][A-Za-z0-9_.,=@+-]*:", source):
        return "rclone"
    return "path"


# --------------------------------------------------------------------------- releases
RELEASES_API = "https://api.github.com/repos/{repo}/releases"
DEFAULT_RELEASE_REPO = "a7733mai/NexaPloy-"


def version_key(tag: str) -> tuple:
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$", tag or "")
    if not m:
        return (-1,)
    major, minor, patch, pre = m.groups()
    # a final release sorts after its pre-releases
    return (int(major), int(minor), int(patch), 1 if pre is None else 0, pre or "")


def pick_release(releases: list[dict[str, Any]], channel: str = "stable") -> dict[str, Any] | None:
    """Newest release of a GitHub releases API list; ``stable`` skips pre-releases, ``beta``
    includes them; drafts and tags that are not ``vX.Y.Z[-pre]`` are ignored."""
    best = None
    for r in releases or []:
        if r.get("draft") or (r.get("prerelease") and channel != "beta"):
            continue
        tag = r.get("tag_name") or ""
        if version_key(tag) == (-1,):
            continue
        if best is None or version_key(tag) > version_key(best["tag_name"]):
            best = r
    return best
