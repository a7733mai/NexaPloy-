"""Server cleanup (``server.cleanup`` job): free disk space without touching anything that matters.

Every target returns ``{target, dry_run, freed_bytes, items, notes, errors}``; with
``dry_run`` nothing is changed and ``freed_bytes`` is what the run would free (an estimate for
docker images: shared layers are counted once per image).

Safety rules (enforced here, covered by tests):

* files are only ever deleted / truncated through :class:`PathGuard`, which accepts nothing
  but the exact shapes each target needs under the Nexaploy root (bench / site ``logs`` dirs,
  proxy and storage logs, staged backup dirs ``backups/<bench>/<site>/<ts>``, PITR work dirs
  ``backups/.pitr/<job>``, image build logs, agent upgrade leftovers) plus docker's
  ``containers/<id>/<id>-json.log`` - never ``sites/<site>/private|public``, ``site_config.json``,
  ``db/`` (MariaDB data), ``config/``, ``proxy/data`` (certificates) or a symlink;
* system space is only freed with fixed commands: ``docker image rm`` (never ``-f``: docker
  refuses images a container uses), ``docker rm`` (exited one-off containers only),
  ``docker builder prune -af``, ``journalctl --vacuum-*``, ``apt-get clean`` /
  ``autoremove --purge``;
* docker images: never one a container (any state) uses, never one named in a bench ``.env``
  (``IMAGE``) or in the proxy / db stacks, never mariadb / traefik / redis / socket-proxy
  images, never one younger than ``image_min_age_hours``, and the newest unused image of each
  repository + tag family is kept (rollback target);
* local backups: only dirs with the ``.uploaded`` marker (a copy exists in the remote), older
  than the retention, and never the newest backup of a site;
* ``/etc/docker/daemon.json`` gets ``log-opts`` (max-size 50m, max-file 3) only when it has
  none, is valid JSON and docker is not started with ``--log-*`` flags; docker is never
  restarted (the setting applies to containers created after the next docker restart / reboot).
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .. import validation as v
from ..fsutil import atomic_write
from .backup import UPLOADED_MARKER
from .docker import parse_json_lines
from .health import dir_size, parse_size
from .server import ONE_SHOT_SERVICES, _parse_labels

TARGETS = (
    "docker_images",
    "docker_build_cache",
    "docker_containers",
    "docker_logs",
    "bench_logs",
    "backups_local",
    "agent_data",
    "journal",
    "apt",
    "tmp",
)
# never removed, whatever their use (shared stacks re-pull them only when online)
PROTECTED_IMAGE_RE = re.compile(r"(^|/)(mariadb|mysql|traefik|redis|docker-socket-proxy|socket-proxy)(:|$)", re.I)
BUILD_SUFFIX_RE = re.compile(r"-\d{8}T\d{4,6}Z?$")
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
ROTATED_LOG_RE = re.compile(r"\.log(\.\d+|[.-]\d{4}-?\d{2}-?\d{2}.*|\.old)?(\.gz|\.xz|\.bz2|\.zst)?$|\.gz$")
LOG_ACTIVE_RE = re.compile(r"\.log$")
DAEMON_JSON = Path("/etc/docker/daemon.json")
DOCKER_LOG_OPTS = {"max-size": "50m", "max-file": "3"}
JOURNAL_MAX = "500M"
APT_ARCHIVES = Path("/var/cache/apt/archives")
DAY = 86400


class UnsafePath(ValueError):
    pass


def _ts_now() -> float:
    return time.time()


def fmt_bytes(n: int | float | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"  # pragma: no cover


def parse_systemd_size(text: str) -> int:
    """'1.2G', '512.0M', '8.0K', '120B' (binary units, as journalctl prints them) -> bytes."""
    m = re.match(r"^\s*([\d.]+)\s*([KMGT]?)i?B?\s*$", text or "", re.I)
    if not m:
        return 0
    mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}[m.group(2).upper()]
    return int(float(m.group(1)) * mult)


def file_size(path: Path) -> int:
    try:
        st = path.lstat()
    except OSError:
        return 0
    return st.st_size


def mtime(path: Path) -> float:
    try:
        return path.lstat().st_mtime
    except OSError:
        return 0.0


# --------------------------------------------------------------------------- path guard
class PathGuard:
    """Accepts only the path shapes the cleanup targets may delete or truncate."""

    def __init__(self, root: Path, docker_root: Path = Path("/var/lib/docker")):
        self.root = Path(root).resolve()
        self.docker_root = Path(docker_root)
        self.denied = [self.root / d for d in ("db", "config")] + [self.root / "proxy" / "data"]

    def _rel(self, path: Path, base: Path) -> tuple[str, ...] | None:
        try:
            return path.relative_to(base).parts
        except ValueError:
            return None

    def _resolved(self, path: Path) -> Path:
        """The real path (shapes are checked on it, so a symlinked dir pointing elsewhere is
        rejected by the shape check); the path itself must not be a symlink."""
        path = Path(path)
        if path.is_symlink():
            raise UnsafePath(f"refusing symlink {path}")
        resolved = path.resolve()
        for d in self.denied:
            if resolved == d or self._rel(resolved, d) is not None:
                raise UnsafePath(f"refusing protected path {path}")
        return resolved

    def log_file(self, path: Path) -> Path:
        """A regular file directly inside an allowed logs dir."""
        p = self._resolved(path)
        if not p.is_file():
            raise UnsafePath(f"not a regular file: {path}")
        parts = self._rel(p, self.root)
        ok = False
        if parts and len(parts) == 4 and parts[0] == "benches" and parts[2] == "logs":
            ok = v.BENCH_RE.match(parts[1]) is not None
        elif parts and len(parts) == 6 and parts[0] == "benches" and parts[2] == "sites" and parts[4] == "logs":
            ok = v.BENCH_RE.match(parts[1]) is not None and parts[3] not in ("assets",)
        elif parts and len(parts) == 3 and parts[0] in ("proxy", "storage") and parts[1] == "logs":
            ok = True
        if not ok or p.name == "site_config.json":
            raise UnsafePath(f"not a Nexaploy log file: {path}")
        return p

    def backup_dir(self, path: Path) -> Path:
        p = self._resolved(path)
        parts = self._rel(p, self.root)
        if not (parts and len(parts) == 4 and parts[0] == "backups" and v.BENCH_RE.match(parts[1])
                and v.BACKUP_ID_RE.match(parts[3]) and p.is_dir()):
            raise UnsafePath(f"not a staged backup dir: {path}")
        return p

    def pitr_dir(self, path: Path) -> Path:
        p = self._resolved(path)
        parts = self._rel(p, self.root)
        if not (parts and len(parts) == 3 and parts[:2] == ("backups", ".pitr")
                and re.match(r"^[0-9A-Za-z]{10,40}$", parts[2]) and p.is_dir()):
            raise UnsafePath(f"not a PITR work dir: {path}")
        return p

    def build_dir(self, path: Path) -> Path:
        p = self._resolved(path)
        parts = self._rel(p, self.root)
        if not (parts and len(parts) == 3 and parts[:2] == ("images", "builds") and p.is_dir()):
            raise UnsafePath(f"not an image build dir: {path}")
        return p

    def agent_leftover(self, path: Path) -> Path:
        p = self._resolved(path)
        parts = self._rel(p, self.root)
        if not (parts and len(parts) == 2 and parts[0] == "agent"
                and parts[1] in (".venv.prev", "src.prev", ".venv.failed", "src.failed",
                                 ".venv.new", "src.new", "dist.new")):
            raise UnsafePath(f"not an agent upgrade leftover: {path}")
        return p

    def docker_log(self, path: Path, container_id: str) -> Path:
        p = Path(path)
        if p.is_symlink() or not CONTAINER_ID_RE.match(container_id or ""):
            raise UnsafePath(f"refusing docker log {path}")
        expected = self.docker_root / "containers" / container_id / f"{container_id}-json.log"
        if p != expected or not p.is_file():
            raise UnsafePath(f"not a docker json-file log: {path}")
        return p


# --------------------------------------------------------------------------- options / results
@dataclass
class CleanupOptions:
    dry_run: bool = True
    log_retention_days: int = 14
    backup_local_keep_days: int | None = None
    docker_log_max_mb: int = 100
    bench_log_max_mb: int = 200
    image_min_age_hours: int = 72
    job_retention_days: int = 30
    build_logs_keep: int = 5


@dataclass
class TargetResult:
    target: str
    dry_run: bool
    freed_bytes: int = 0
    items: int = 0
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"target": self.target, "dry_run": self.dry_run, "freed_bytes": int(self.freed_bytes),
                "items": self.items, "notes": self.notes[:50], "errors": self.errors[:50]}


# --------------------------------------------------------------------------- docker helpers (pure)
def parse_created(text: str | None) -> float | None:
    """'2026-11-03 21:30:00 +0000 UTC' -> unix time."""
    m = re.match(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) ([+-]\d{4})", str(text or ""))
    if not m:
        return None
    try:
        return datetime.strptime(" ".join(m.groups()), "%Y-%m-%d %H:%M:%S %z").timestamp()
    except ValueError:
        return None


def image_ref(repo: str, tag: str) -> str | None:
    if not repo or repo == "<none>" or not tag or tag == "<none>":
        return None
    return f"{repo}:{tag}"


def normalize_ref(ref: str) -> str:
    ref = (ref or "").strip()
    last = ref.rsplit("/", 1)[-1]
    if ref and ":" not in last and "@" not in ref:
        ref += ":latest"
    return ref


def tag_family(repo: str, tag: str) -> str:
    return f"{repo}:{BUILD_SUFFIX_RE.sub('', tag or '')}"


def select_images(images: list[dict[str, Any]], used_ids: set[str], protected_refs: set[str],
                  now: float, min_age_hours: int) -> tuple[list[dict[str, Any]], list[str]]:
    """Images to remove and notes on what was kept.

    `images`: rows of ``docker image ls --no-trunc --format '{{json .}}'`` (ID, Repository,
    Tag, CreatedAt, Size). Returns rows with an extra ``ref`` (repo:tag, or the ID when
    dangling) - see the module doc for the rules.
    """
    protected = {normalize_ref(r) for r in protected_refs if r}
    min_age = max(0, int(min_age_hours)) * 3600
    candidates: list[dict[str, Any]] = []
    notes: list[str] = []
    families: dict[str, list[dict[str, Any]]] = {}
    for row in images:
        image_id = str(row.get("ID") or "")
        repo, tag = str(row.get("Repository") or ""), str(row.get("Tag") or "")
        ref = image_ref(repo, tag)
        created = parse_created(row.get("CreatedAt"))
        age = now - created if created else 0
        if image_id in used_ids:
            continue
        if ref and (normalize_ref(ref) in protected or PROTECTED_IMAGE_RE.search(repo + ":")):
            continue
        dangling = ref is None
        if dangling:
            if age < 3600:
                continue
            candidates.append({**row, "ref": image_id, "dangling": True})
            continue
        if age < min_age:
            notes.append(f"kept {ref}: younger than {min_age_hours} h")
            continue
        families.setdefault(tag_family(repo, tag), []).append({**row, "ref": ref, "dangling": False,
                                                                "_created": created or 0})
    for family, rows in sorted(families.items()):
        rows.sort(key=lambda r: r["_created"], reverse=True)
        notes.append(f"kept {rows[0]['ref']}: newest unused image of {family} (rollback target)")
        candidates.extend(rows[1:])
    for row in candidates:
        row.pop("_created", None)
    return candidates, notes


def select_oneoff_containers(rows: list[dict[str, Any]], projects: set[str], now: float,
                             min_age: float = 3600) -> list[dict[str, Any]]:
    """Exited/created/dead one-off containers (compose ``run --rm`` leftovers, configurator...)
    of Nexaploy projects."""
    out = []
    for row in rows:
        state = str(row.get("State") or "").lower()
        if state not in ("exited", "created", "dead"):
            continue
        labels = _parse_labels(row.get("Labels"))
        project = labels.get("com.docker.compose.project", "")
        name = str(row.get("Names") or "").split(",")[0].lstrip("/")
        ours = project in projects or project.startswith("nexaploy") or name.startswith("nexaploy-")
        oneoff = labels.get("com.docker.compose.oneoff", "").lower() == "true" or \
            labels.get("com.docker.compose.service", "") in ONE_SHOT_SERVICES
        created = parse_created(row.get("CreatedAt"))
        if ours and oneoff and (created is None or now - created >= min_age):
            out.append({"id": str(row.get("ID") or ""), "name": name,
                        "size": parse_size(str(row.get("Size") or "").split("(")[0])})
    return out


def merge_daemon_json(text: str | None) -> tuple[dict[str, Any] | None, str]:
    """New daemon.json content (or None) and why."""
    if text is None or not text.strip():
        return {"log-opts": dict(DOCKER_LOG_OPTS)}, "created with log rotation"
    try:
        data = json.loads(text)
    except ValueError:
        return None, "daemon.json is not valid JSON: left untouched"
    if not isinstance(data, dict):
        return None, "daemon.json is not a JSON object: left untouched"
    driver = data.get("log-driver")
    if driver not in (None, "json-file", "local"):
        return None, f"log driver {driver!r}: left untouched"
    opts = data.get("log-opts")
    if isinstance(opts, dict) and opts.get("max-size"):
        return None, "log rotation already configured"
    new = dict(data)
    new["log-opts"] = {**(opts if isinstance(opts, dict) else {}), **DOCKER_LOG_OPTS}
    return new, "log rotation added"


def select_backups(stamps: list[str], uploaded: set[str], keep_days: int, now: datetime) -> list[str]:
    """Uploaded local backups older than keep_days; never the newest one of the site."""
    from .backup import parse_ts

    stamps = sorted(stamps)
    if not stamps:
        return []
    newest = stamps[-1]
    out = []
    for ts in stamps:
        if ts == newest or ts not in uploaded:
            continue
        dt = parse_ts(ts)
        if dt is not None and (now - dt).total_seconds() >= keep_days * DAY:
            out.append(ts)
    return out


# --------------------------------------------------------------------------- cleaner
class Cleaner:
    def __init__(self, ctx, opts: CleanupOptions, log: Callable[[str], None] = lambda m: None,
                 clock: Callable[[], float] = _ts_now, daemon_json: Path = DAEMON_JSON,
                 docker_root: Path = Path("/var/lib/docker"), apt_archives: Path = APT_ARCHIVES,
                 which=shutil.which):
        self.ctx = ctx
        self.settings = ctx.settings
        self.runner = ctx.runner
        self.opts = opts
        self.log = log
        self.clock = clock
        self.daemon_json = daemon_json
        self.apt_archives = apt_archives
        self.which = which
        self.guard = PathGuard(self.settings.root, docker_root)

    @property
    def root(self) -> Path:
        return self.settings.root

    def run(self, target: str) -> dict[str, Any]:
        if target not in TARGETS:
            raise ValueError(f"unknown cleanup target {target!r}")
        res = TargetResult(target, self.opts.dry_run)
        try:
            getattr(self, f"_{target}")(res)
        except Exception as exc:  # one target never breaks the others
            res.errors.append(f"{type(exc).__name__}: {exc}"[:500])
        verb = "would free" if res.dry_run else "freed"
        self.log(f"{target}: {verb} {fmt_bytes(res.freed_bytes)} ({res.items} item(s))")
        for n in res.notes[:20]:
            self.log(f"  {n}")
        for e in res.errors[:20]:
            self.log(f"  error: {e}")
        return res.as_dict()

    # ---- file helpers -------------------------------------------------------------------------
    def _delete_file(self, res: TargetResult, path: Path, check: Callable[[Path], Path]) -> None:
        try:
            p = check(path)
        except UnsafePath as exc:
            res.errors.append(str(exc))
            return
        size = file_size(p)
        if not res.dry_run:
            try:
                p.unlink()
            except OSError as exc:
                res.errors.append(f"{p}: {exc}")
                return
        res.freed_bytes += size
        res.items += 1

    def _truncate_file(self, res: TargetResult, path: Path, check: Callable[[Path], Path]) -> None:
        try:
            p = check(path)
        except UnsafePath as exc:
            res.errors.append(str(exc))
            return
        size = file_size(p)
        if not res.dry_run:
            try:
                with open(p, "r+b") as fh:
                    fh.truncate(0)
            except OSError as exc:
                res.errors.append(f"{p}: {exc}")
                return
        res.freed_bytes += size
        res.items += 1
        res.notes.append(f"truncated {p.name} ({fmt_bytes(size)})")

    def _delete_dir(self, res: TargetResult, path: Path, check: Callable[[Path], Path], note: str = "") -> None:
        try:
            p = check(path)
        except UnsafePath as exc:
            res.errors.append(str(exc))
            return
        size = dir_size(p)
        if not res.dry_run:
            shutil.rmtree(p, ignore_errors=True)
            if p.exists():
                res.errors.append(f"could not remove {p}")
                return
        res.freed_bytes += size
        res.items += 1
        if note:
            res.notes.append(note)

    def _docker(self, *args: str, timeout: float = 300):
        return self.runner.run(["docker", *args], timeout=timeout)

    def _bench_names(self) -> list[str]:
        return self.ctx.benches.names()

    # ---- docker -------------------------------------------------------------------------------
    def _protected_refs(self) -> set[str]:
        from ..config import read_env_file

        refs: set[str] = set()
        for bench in self._bench_names():
            image = self.ctx.benches.image(bench)
            if image:
                refs.add(image)
        for env in (self.root / "proxy" / ".env", self.root / "db" / ".env"):
            for key, val in (read_env_file(env).items() if env.is_file() else []):
                if "IMAGE" in key.upper() and val:
                    refs.add(val)
        return refs

    def _used_image_ids(self) -> set[str] | None:
        res = self._docker("ps", "-aq", "--no-trunc", timeout=60)
        if not res.ok:
            return None
        ids = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        if not ids:
            return set()
        res = self._docker("inspect", "--format", "{{.Image}}", *ids, timeout=120)
        if not res.ok:
            return None
        return {line.strip() for line in res.stdout.splitlines() if line.strip()}

    def _docker_images(self, res: TargetResult) -> None:
        used = self._used_image_ids()
        if used is None:
            res.errors.append("cannot list containers: no image removed")
            return
        listing = self._docker("image", "ls", "--no-trunc", "--format", "{{json .}}", timeout=120)
        if not listing.ok:
            res.errors.append("docker image ls failed")
            return
        chosen, notes = select_images(parse_json_lines(listing.stdout), used, self._protected_refs(),
                                      self.clock(), self.opts.image_min_age_hours)
        res.notes.extend(notes)
        for row in chosen:
            size = parse_size(row.get("Size"))
            if not res.dry_run:
                r = self._docker("image", "rm", row["ref"], timeout=300)
                if not r.ok:
                    res.errors.append(f"{row['ref']}: {(r.stderr or r.stdout).strip()[-200:]}")
                    continue
            res.freed_bytes += size
            res.items += 1
            res.notes.append(f"{'would remove' if res.dry_run else 'removed'} {row['ref']} ({fmt_bytes(size)})")

    def _docker_build_cache(self, res: TargetResult) -> None:
        from .health import parse_system_df

        if res.dry_run:
            df = self._docker("system", "df", "--format", "{{json .}}", timeout=120)
            info = parse_system_df(df.stdout).get("build_cache") if df.ok else None
            res.freed_bytes = (info or {}).get("reclaimable", 0)
            res.items = (info or {}).get("count", 0)
            return
        out = self._docker("builder", "prune", "-af", timeout=1800)
        if not out.ok:
            res.errors.append((out.stderr or out.stdout).strip()[-300:])
            return
        m = re.search(r"Total(?: reclaimed space)?:\s*([\d.]+\s*[kKMGT]?i?B)", out.stdout)
        res.freed_bytes = parse_size(m.group(1)) if m else 0
        res.items = sum(1 for line in out.stdout.splitlines() if re.match(r"^[0-9a-z]{20,}", line.strip()))

    def _docker_containers(self, res: TargetResult) -> None:
        listing = self._docker("ps", "-a", "--no-trunc", "--size", "--filter", "status=exited",
                               "--filter", "status=created", "--filter", "status=dead",
                               "--format", "{{json .}}", timeout=120)
        if not listing.ok:
            res.errors.append("docker ps failed")
            return
        rows = select_oneoff_containers(parse_json_lines(listing.stdout), set(self._bench_names()), self.clock())
        for c in rows:
            if not res.dry_run:
                r = self._docker("rm", c["id"], timeout=120)
                if not r.ok:
                    res.errors.append(f"{c['name']}: {(r.stderr or '').strip()[-200:]}")
                    continue
            res.freed_bytes += c["size"]
            res.items += 1
            res.notes.append(f"{'would remove' if res.dry_run else 'removed'} {c['name']}")

    def _docker_logs(self, res: TargetResult) -> None:
        limit = max(1, int(self.opts.docker_log_max_mb)) * 1024 * 1024
        root = self._docker("info", "--format", "{{.DockerRootDir}}", timeout=30)
        if root.ok and root.stdout.strip().startswith("/"):
            self.guard.docker_root = Path(root.stdout.strip())
        ids = self._docker("ps", "-q", "--no-trunc", timeout=60)
        if ids.ok and ids.stdout.strip():
            info = self._docker("inspect", "--format", "{{.Id}}|{{.LogPath}}|{{.HostConfig.LogConfig.Type}}",
                                *ids.stdout.split(), timeout=120)
            for line in info.stdout.splitlines() if info.ok else []:
                parts = line.strip().split("|")
                if len(parts) != 3 or parts[2] != "json-file" or not parts[1]:
                    continue
                path = Path(parts[1])
                if file_size(path) > limit:
                    self._truncate_file(res, path, lambda p, cid=parts[0]: self.guard.docker_log(p, cid))
        self._daemon_log_rotation(res)

    def _daemon_log_rotation(self, res: TargetResult) -> None:
        unit = self.runner.run(["systemctl", "show", "docker.service", "-p", "ExecStart", "--value"], timeout=20)
        if unit.ok and "--log-" in unit.stdout:
            res.notes.append("docker is started with --log-* flags: daemon.json log rotation left untouched")
            return
        try:
            current = self.daemon_json.read_text() if self.daemon_json.is_file() else None
        except OSError as exc:
            res.notes.append(f"cannot read {self.daemon_json}: {exc}")
            return
        new, why = merge_daemon_json(current)
        if new is None:
            res.notes.append(why)
            return
        if res.dry_run:
            res.notes.append(f"would configure docker log rotation (max-size 50m, max-file 3) in {self.daemon_json}")
            return
        atomic_write(self.daemon_json, json.dumps(new, indent=2) + "\n", mode=0o644)
        res.notes.append(f"{self.daemon_json}: {why} (max-size 50m, max-file 3); takes effect for containers "
                         "created after the next docker restart / reboot - docker was not restarted")

    # ---- files ------------------------------------------------------------------------------
    def _log_dirs(self) -> list[Path]:
        dirs: list[Path] = []
        for bench in self._bench_names():
            base = self.ctx.benches.path(bench)
            dirs.append(base / "logs")
            sites = base / "sites"
            if sites.is_dir():
                for site in sorted(sites.iterdir()):
                    if site.is_dir() and not site.is_symlink() and (site / "site_config.json").is_file():
                        dirs.append(site / "logs")
        dirs += [self.root / "proxy" / "logs", self.root / "storage" / "logs"]
        return [d for d in dirs if d.is_dir() and not d.is_symlink()]

    def _bench_logs(self, res: TargetResult) -> None:
        cutoff = self.clock() - max(1, int(self.opts.log_retention_days)) * DAY
        limit = max(1, int(self.opts.bench_log_max_mb)) * 1024 * 1024
        for d in self._log_dirs():
            for f in sorted(d.iterdir()):
                if f.is_symlink() or not f.is_file():
                    continue
                active = bool(LOG_ACTIVE_RE.search(f.name))
                if active:
                    if mtime(f) < cutoff:
                        self._delete_file(res, f, self.guard.log_file)  # abandoned
                    elif file_size(f) > limit:
                        self._truncate_file(res, f, self.guard.log_file)
                elif ROTATED_LOG_RE.search(f.name) and mtime(f) < cutoff:
                    self._delete_file(res, f, self.guard.log_file)

    def _backups_local(self, res: TargetResult) -> None:
        keep_days = self.opts.backup_local_keep_days
        if keep_days is None:
            keep_days = self.settings.backup.retention_days
        now = datetime.fromtimestamp(self.clock(), timezone.utc)
        base = self.settings.backups_dir
        if not base.is_dir():
            return
        for bench_dir in sorted(base.iterdir()):
            if not v.BENCH_RE.match(bench_dir.name) or bench_dir.is_symlink() or not bench_dir.is_dir():
                continue
            for site_dir in sorted(bench_dir.iterdir()):
                if site_dir.is_symlink() or not site_dir.is_dir():
                    continue
                stamps = sorted(p.name for p in site_dir.iterdir() if p.is_dir() and v.BACKUP_ID_RE.match(p.name))
                uploaded = {ts for ts in stamps if (site_dir / ts / UPLOADED_MARKER).is_file()}
                for ts in select_backups(stamps, uploaded, keep_days, now):
                    self._delete_dir(res, site_dir / ts, self.guard.backup_dir, f"{site_dir.name}/{ts}")

    def _agent_data(self, res: TargetResult) -> None:
        cutoff = datetime.fromtimestamp(self.clock() - max(1, self.opts.job_retention_days) * DAY, timezone.utc)
        iso = cutoff.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        store = self.ctx.store
        db = self.settings.jobs_db
        before = sum(file_size(Path(str(db) + s)) for s in ("", "-wal"))
        if res.dry_run:
            n = store.count_finished_before(iso)
            res.items += n
            res.notes.append(f"would delete {n} finished job(s) older than {self.opts.job_retention_days} days")
        else:
            n = store.delete_finished_before(iso)
            store.vacuum()
            after = sum(file_size(Path(str(db) + s)) for s in ("", "-wal"))
            res.freed_bytes += max(0, before - after)
            res.items += n
            res.notes.append(f"deleted {n} finished job(s), database vacuumed")
        # image build logs: keep the newest N builds, drop older ones past the retention
        builds = self.settings.builds_dir
        if builds.is_dir():
            dirs = sorted((p for p in builds.iterdir() if p.is_dir() and not p.is_symlink()), key=mtime, reverse=True)
            log_cutoff = self.clock() - max(1, int(self.opts.log_retention_days)) * DAY
            for d in dirs[max(0, self.opts.build_logs_keep):]:
                if mtime(d) < log_cutoff:
                    self._delete_dir(res, d, self.guard.build_dir, f"build log {d.name}")
        self._agent_previous(res)

    def _agent_previous(self, res: TargetResult) -> None:
        from .upgrade import AgentInstall

        inst = AgentInstall(self.root / "agent")
        state = inst.read_state()
        healthy = state.get("status") in (None, "confirmed", "rolled_back")
        week = self.clock() - 7 * DAY
        if not healthy:
            res.notes.append("agent upgrade not confirmed yet: previous agent kept")
            return
        for path in (inst.venv_prev, inst.src_prev, inst.venv_failed, inst.src_failed):
            if path.exists() and mtime(path) < week:
                self._delete_dir(res, path, self.guard.agent_leftover, f"removed {path.name}")

    def _tmp(self, res: TargetResult) -> None:
        day_ago = self.clock() - DAY
        pitr = self.settings.backups_dir / ".pitr"
        if pitr.is_dir():
            for d in sorted(pitr.iterdir()):
                if not d.is_dir() or d.is_symlink() or mtime(d) >= day_ago:
                    continue
                try:
                    job = self.ctx.store.get(d.name, with_steps=False)
                except KeyError:
                    job = None
                if job and job.get("status") in ("queued", "running"):
                    continue
                self._delete_dir(res, d, self.guard.pitr_dir, f"PITR work dir {d.name}")
        from .upgrade import AgentInstall

        inst = AgentInstall(self.root / "agent")
        running = self.ctx.store.list(status="running", kind="agent.upgrade", limit=5)
        if not running:
            for path in (inst.venv_new, inst.src_new, inst.dist_new):
                if path.exists() and mtime(path) < day_ago:
                    self._delete_dir(res, path, self.guard.agent_leftover, f"upgrade leftover {path.name}")

    # ---- system -------------------------------------------------------------------------------
    def _journal(self, res: TargetResult) -> None:
        if not self.which("journalctl"):
            res.notes.append("journalctl not available")
            return
        usage = self.runner.run(["journalctl", "--disk-usage"], timeout=60)
        m = re.search(r"take up ([\d.]+\s*[KMGT]?)i?B?", usage.stdout or "")
        current = parse_systemd_size(m.group(1)) if m else 0
        if res.dry_run:
            res.freed_bytes = max(0, current - parse_systemd_size(JOURNAL_MAX))
            res.notes.append(f"journal uses {fmt_bytes(current)}; kept: last {self.opts.log_retention_days} days, "
                             f"at most {JOURNAL_MAX}")
            return
        out = self.runner.run(["journalctl", f"--vacuum-time={max(1, int(self.opts.log_retention_days))}d",
                               f"--vacuum-size={JOURNAL_MAX}"], timeout=600)
        text = (out.stdout or "") + (out.stderr or "")
        if not out.ok:
            res.errors.append(text.strip()[-300:])
            return
        freed = re.findall(r"freed ([\d.]+\s*[KMGT]?)i?B? of", text)
        res.freed_bytes = sum(parse_systemd_size(x) for x in freed)
        res.items = len(re.findall(r"^Deleted archived journal", text, re.M))

    def _apt(self, res: TargetResult) -> None:
        from .os_updates import APT_ENV, apt_get

        if not self.which("apt-get"):
            res.notes.append("not an apt system")
            return
        cache = sum(file_size(p) for d in (self.apt_archives, self.apt_archives / "partial") if d.is_dir()
                    for p in d.glob("*.deb"))
        if res.dry_run:
            sim = self.runner.run(apt_get("-s", "autoremove", "--purge"), env=APT_ENV, timeout=300)
            removable = len(re.findall(r"^Remv ", sim.stdout or "", re.M)) if sim.ok else 0
            res.freed_bytes = cache
            res.items = removable
            res.notes.append(f"package cache {fmt_bytes(cache)}; {removable} unused package(s)")
            return
        clean = self.runner.run(apt_get("clean"), env=APT_ENV, timeout=600)
        if not clean.ok:
            res.errors.append((clean.stderr or "apt-get clean failed").strip()[-300:])
        else:
            res.freed_bytes += cache
        auto = self.runner.run(apt_get("-y", "autoremove", "--purge"), env=APT_ENV, timeout=1800)
        if not auto.ok:
            res.errors.append((auto.stderr or "apt-get autoremove failed").strip()[-300:])
            return
        m = re.search(r"After this operation, ([\d.,]+\s*[kMG]?B) disk space will be freed", auto.stdout or "")
        if m:
            res.freed_bytes += parse_size(m.group(1).replace(",", ""))
        res.items += len(re.findall(r"^Removing ", auto.stdout or "", re.M))


def summarize(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(results)
    return {
        "freed_bytes": sum(int(r.get("freed_bytes") or 0) for r in rows),
        "items": sum(int(r.get("items") or 0) for r in rows),
        "errors": sum(len(r.get("errors") or []) for r in rows),
    }

