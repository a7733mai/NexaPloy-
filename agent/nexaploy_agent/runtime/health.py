"""Server health facts.

* ``light_health(...)`` rides along with every heartbeat as ``facts["health"]`` (cheap: statvfs,
  /proc reads, one ``systemctl`` and one ``timedatectl`` call - those two at most every 5 minutes)
* ``full_health(...)`` is the ``server.diagnose`` job: the light facts plus docker disk usage,
  OOM kills since boot, MariaDB connections / datadir / binlog size and served certificates.

The control plane turns them into issues (nexaploy/server_health_utils.py ``evaluate_health``);
the agent only measures.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .docker import parse_json_lines

DOCKER_ROOT = Path("/var/lib/docker")
UNIT_INTERVAL_SECONDS = 300
# failed units worth reporting (others are the operator's business)
UNIT_PATTERNS = (
    re.compile(r"^nexaploy[-.@]"),
    re.compile(r"^docker\.(service|socket)$"),
    re.compile(r"^containerd\.service$"),
    re.compile(r"rclone", re.I),
    re.compile(r"\.mount$"),
)
OOM_RE = re.compile(r"(Out of memory|oom-kill|oom_reaper|Killed process)", re.I)
UNITS = {"B": 1, "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4,
         "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3, "TIB": 1024 ** 4}


def utc_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_size(text: str | None) -> int:
    """Docker human sizes ('1.23GB', '512kB', '0B', '1.2 GiB') -> bytes (decimal units as docker)."""
    m = re.match(r"^\s*([\d.]+)\s*([kKMGT]?i?B)\s*$", str(text or ""))
    if not m:
        return 0
    try:
        return int(float(m.group(1)) * UNITS.get(m.group(2).upper(), 1))
    except ValueError:
        return 0


# --------------------------------------------------------------------------- filesystems
def filesystem_usage(paths: Iterable[Path]) -> list[dict[str, Any]]:
    """Space and inode usage per distinct filesystem of `paths` (first path wins)."""
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for p in paths:
        p = Path(p)
        try:
            if not p.exists():
                continue
            dev = p.stat().st_dev
            if dev in seen:
                continue
            st = os.statvfs(p)
        except OSError:
            continue
        seen.add(dev)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = max(0, total - st.f_bfree * st.f_frsize)
        inodes = st.f_files
        inodes_free = st.f_ffree
        out.append({
            "path": str(p),
            "total": total,
            "free": free,
            "used_percent": round(100.0 * used / (used + free), 1) if (used + free) else 0.0,
            "inodes_total": inodes,
            "inodes_used_percent": round(100.0 * (inodes - inodes_free) / inodes, 1) if inodes else 0.0,
        })
    return out


def meminfo(path: Path = Path("/proc/meminfo")) -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        for line in path.read_text().splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                out[key] = int(parts[0]) * 1024
    except OSError:
        pass
    return out


def memory_facts(mem: dict[str, int]) -> dict[str, Any]:
    total, avail = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
    swap_total, swap_free = mem.get("SwapTotal", 0), mem.get("SwapFree", 0)
    return {
        "mem_total": total,
        "mem_available": avail,
        "mem_used_percent": round(100.0 * (total - avail) / total, 1) if total else 0.0,
        "swap_total": swap_total,
        "swap_used_percent": round(100.0 * (swap_total - swap_free) / swap_total, 1) if swap_total else 0.0,
    }


# --------------------------------------------------------------------------- systemd / time
def parse_failed_units(text: str) -> list[str]:
    """``systemctl list-units --state=failed --no-legend --plain`` -> unit names we care about."""
    units = []
    for line in (text or "").splitlines():
        parts = line.strip().lstrip("●* ").split()
        if not parts:
            continue
        name = parts[0]
        if any(p.search(name) for p in UNIT_PATTERNS):
            units.append(name)
    return sorted(dict.fromkeys(units))


def parse_ntp(text: str) -> bool | None:
    value = (text or "").strip().lower()
    if value in ("yes", "true", "1"):
        return True
    if value in ("no", "false", "0"):
        return False
    return None


def systemd_facts(runner) -> dict[str, Any]:
    out: dict[str, Any] = {"failed_units": None, "ntp_synchronized": None}
    res = runner.run(["systemctl", "list-units", "--state=failed", "--no-legend", "--plain", "--all"], timeout=20)
    if res.ok:
        out["failed_units"] = parse_failed_units(res.stdout)
    res = runner.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], timeout=20)
    if res.ok:
        out["ntp_synchronized"] = parse_ntp(res.stdout)
    return out


# --------------------------------------------------------------------------- light (heartbeat)
class HealthCollector:
    def __init__(self, settings, runner, clock=time.time, unit_interval: float = UNIT_INTERVAL_SECONDS,
                 meminfo_path: Path = Path("/proc/meminfo")):
        self.settings = settings
        self.runner = runner
        self.clock = clock
        self.unit_interval = unit_interval
        self.meminfo_path = meminfo_path
        self._units: dict[str, Any] | None = None
        self._units_at: float | None = None

    def paths(self) -> list[Path]:
        root = self.settings.root
        return [Path("/"), root, DOCKER_ROOT, root / "db" / "data", root / "backups"]

    def _systemd(self, force: bool = False) -> dict[str, Any]:
        now = self.clock()
        if force or self._units is None or self._units_at is None or now - self._units_at >= self.unit_interval:
            try:
                self._units = systemd_facts(self.runner)
            except Exception:
                self._units = {"failed_units": None, "ntp_synchronized": None}
            self._units_at = now
        return dict(self._units)

    def light(self, force: bool = False) -> dict[str, Any]:
        """Never raises."""
        out: dict[str, Any] = {"agent_time": utc_iso(self.clock())}
        try:
            out["filesystems"] = filesystem_usage(self.paths())
            out.update(memory_facts(meminfo(self.meminfo_path)))
            out.update(self._systemd(force))
        except Exception as exc:  # pragma: no cover - defensive
            out["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return out


# --------------------------------------------------------------------------- full (server.diagnose)
def parse_system_df(text: str) -> dict[str, Any]:
    """``docker system df --format '{{json .}}'`` -> {images: {size, reclaimable}, ...}."""
    out: dict[str, Any] = {}
    keys = {"images": "images", "containers": "containers", "local volumes": "volumes", "build cache": "build_cache"}
    for row in parse_json_lines(text):
        key = keys.get(str(row.get("Type") or "").strip().lower())
        if not key:
            continue
        reclaim = str(row.get("Reclaimable") or "").split("(")[0].strip()
        out[key] = {
            "count": int(row.get("TotalCount") or 0) if str(row.get("TotalCount") or "0").isdigit() else 0,
            "size": parse_size(row.get("Size")),
            "reclaimable": parse_size(reclaim),
        }
    out["total"] = sum(v["size"] for v in out.values() if isinstance(v, dict))
    out["reclaimable"] = sum(v["reclaimable"] for v in out.values() if isinstance(v, dict))
    return out


def count_oom(text: str) -> tuple[int, list[str]]:
    lines = [line.strip() for line in (text or "").splitlines() if OOM_RE.search(line)]
    kills = [line for line in lines if re.search(r"Killed process|oom-kill", line, re.I)]
    return len(kills) or len(lines), lines[-5:]


def oom_facts(runner) -> dict[str, Any]:
    res = runner.run(["journalctl", "-k", "-b", "--no-pager", "-q", "-o", "cat",
                      "--grep", "Out of memory|oom-kill|Killed process"], timeout=60)
    if not res.ok and res.returncode != 1:  # 1 = no match
        res = runner.run(["dmesg", "--ctime"], timeout=30)
        if not res.ok:
            return {"oom_kills": None}
    count, recent = count_oom(res.stdout)
    return {"oom_kills": count, "oom_recent": recent}


def dir_size(path: Path, limit_entries: int = 2_000_000) -> int:
    total = 0
    seen = 0
    stack = [Path(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    seen += 1
                    if seen > limit_entries:
                        return total
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_blocks * 512
                    except OSError:
                        continue
        except OSError:
            continue
    return total


MARIADB_SQL = (
    "SELECT 'threads_connected', VARIABLE_VALUE FROM information_schema.GLOBAL_STATUS "
    "WHERE VARIABLE_NAME='THREADS_CONNECTED'; "
    "SELECT 'max_used_connections', VARIABLE_VALUE FROM information_schema.GLOBAL_STATUS "
    "WHERE VARIABLE_NAME='MAX_USED_CONNECTIONS'; "
    "SELECT 'max_connections', @@max_connections; "
    "SELECT 'log_bin', @@log_bin"
)


def parse_key_values(text: str) -> dict[str, str]:
    out = {}
    for line in (text or "").splitlines():
        parts = line.strip().split("\t")
        if len(parts) >= 2:
            out[parts[0].strip()] = parts[1].strip()
    return out


def mariadb_facts(runner, password: str | None, data_dir: Path) -> dict[str, Any]:
    from .pitr import db_query_command

    out: dict[str, Any] = {"reachable": None}
    if password:
        res = runner.run(db_query_command(MARIADB_SQL), env={"MYSQL_PWD": password}, timeout=60)
        out["reachable"] = res.ok
        if res.ok:
            kv = parse_key_values(res.stdout)
            for key in ("threads_connected", "max_used_connections", "max_connections"):
                if kv.get(key, "").isdigit():
                    out[key] = int(kv[key])
            out["log_bin"] = kv.get("log_bin") in ("1", "ON")
        else:
            out["error"] = (res.stderr or res.stdout or "").strip()[-300:]
    if data_dir.is_dir():
        out["datadir_bytes"] = dir_size(data_dir)
        try:
            out["binlog_bytes"] = sum(p.stat().st_size for p in data_dir.glob("mysql-bin.[0-9]*"))
        except OSError:
            pass
    return out


def tls_summary(report: dict[str, Any] | None, now: datetime | None = None, warn_days: int = 14) -> dict[str, Any]:
    hosts = (report or {}).get("hosts") or {}
    now = now or datetime.now(timezone.utc)
    failing, expiring = [], []
    for host, info in sorted(hosts.items()):
        if not isinstance(info, dict):
            continue
        if info.get("error") or info.get("trusted") is False:
            failing.append(host)
            continue
        try:
            not_after = datetime.fromisoformat(str(info.get("not_after")).replace("Z", "+00:00"))
            if (not_after - now).days < warn_days:
                expiring.append(host)
        except (TypeError, ValueError):
            pass
    return {"hosts": len(hosts), "failing": failing[:20], "expiring": expiring[:20]}


def full_health(ctx) -> dict[str, Any]:
    """Everything server.diagnose reports (each part best effort)."""
    runner = ctx.runner
    out = HealthCollector(ctx.settings, runner).light(force=True)
    res = runner.run(["docker", "system", "df", "--format", "{{json .}}"], timeout=120)
    out["docker_df"] = parse_system_df(res.stdout) if res.ok else None
    res = runner.run(["docker", "info", "--format", "{{.DockerRootDir}}"], timeout=30)
    root_dir = Path(res.stdout.strip()) if res.ok and res.stdout.strip().startswith("/") else DOCKER_ROOT
    out["docker_root"] = str(root_dir)
    out["docker_root_fs"] = (filesystem_usage([root_dir]) or [None])[0]
    try:
        out.update(oom_facts(runner))
    except Exception:
        out["oom_kills"] = None
    try:
        password = ctx.settings.db_root_password()
    except Exception:
        password = None
    try:
        out["mariadb"] = mariadb_facts(runner, password, ctx.settings.root / "db" / "data")
    except Exception as exc:
        out["mariadb"] = {"reachable": None, "error": str(exc)[:300]}
    try:
        out["tls"] = tls_summary(ctx.tls.check())
    except Exception:
        out["tls"] = None
    out["full"] = True
    return out

