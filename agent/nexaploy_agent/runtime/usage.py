"""Per-site resource usage (plan limits, ARCHITECTURE.md §5.6 "usage").

Three sources, all collected on the host:

* **CPU / request time** – Frappe's monitor (``bench --site <s> set-config -p monitor 1``,
  enabled by ``site.create``) appends one JSON line per request / background job to
  ``benches/<b>/logs/monitor.json.log`` (``{site, timestamp, transaction_type, duration}``,
  ``duration`` in microseconds). The log is read *incrementally*: the byte offset (and inode,
  to detect rotation) per bench is kept in ``<data_dir>/usage_state.json`` and durations are
  summed per site per UTC day.
* **Database size** – ``information_schema.tables`` of the shared MariaDB via
  ``docker exec -e MYSQL_PWD nexaploy-db mariadb -uroot ...``. The root password reaches the
  ``docker`` CLI only through its environment (``MYSQL_PWD``), never argv.
* **File storage** – ``du -sb`` of ``sites/<s>/public/files`` and ``sites/<s>/private/files``
  (backups excluded), cached for ``SIZE_CACHE_SECONDS`` and bounded by a timeout.

``UsageCollector.collect()`` returns the detailed per-site report used by the ``site.usage``
job; :func:`compact_usage` turns it into the small block sent with the heartbeat.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .. import validation as v
from ..fsutil import atomic_write

MONITOR_LOG = "monitor.json.log"
STATE_FILE = "usage_state.json"
DB_CONTAINER = "nexaploy-db"
MB = 1024 * 1024
# transaction types that consume bench CPU (Frappe monitor)
COUNTED_TYPES = ("request", "job")
KEEP_DAYS = 7
SIZE_CACHE_SECONDS = 3600
DB_SIZE_CACHE_SECONDS = 600
DU_TIMEOUT = 120
DB_TIMEOUT = 60
MAX_READ_BYTES = 64 * MB  # per bench per collection; the rest is read next time
# Heartbeats carry usage at most this often (agent-side throttle).
HEARTBEAT_USAGE_SECONDS = 15 * 60

DB_SIZE_QUERY = (
    "SELECT table_schema, COALESCE(SUM(data_length + index_length), 0) "
    "FROM information_schema.tables GROUP BY table_schema"
)

_LOCK = threading.Lock()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- monitor log parsing
def parse_monitor_timestamp(value: Any) -> datetime | None:
    """Frappe writes ``str(datetime)``: '2026-11-03 10:00:00.123456[+00:00]' (UTC)."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_monitor_line(line: str) -> tuple[str, str, str, int] | None:
    """One monitor line -> (site, utc_day, transaction_type, duration_us) or None."""
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    site = data.get("site")
    ttype = data.get("transaction_type")
    if not isinstance(site, str) or ttype not in COUNTED_TYPES:
        return None
    ts = parse_monitor_timestamp(data.get("timestamp"))
    duration = data.get("duration")
    if ts is None or isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration < 0:
        return None
    return site, ts.date().isoformat(), ttype, int(duration)


def accumulate(days: dict[str, dict[str, dict[str, int]]], lines: Iterable[str]) -> int:
    """Add monitor lines into ``days[site][day] = {cpu_us, requests, jobs}``; returns lines used."""
    used = 0
    for line in lines:
        parsed = parse_monitor_line(line)
        if parsed is None:
            continue
        site, day, ttype, duration = parsed
        bucket = days.setdefault(site, {}).setdefault(day, {"cpu_us": 0, "requests": 0, "jobs": 0})
        bucket["cpu_us"] += duration
        bucket["requests" if ttype == "request" else "jobs"] += 1
        used += 1
    return used


def read_new_lines(path: Path, offset: int, inode: int | None,
                   max_bytes: int = MAX_READ_BYTES) -> tuple[list[str], int, int | None]:
    """Complete lines appended to ``path`` since ``offset``.

    Returns ``(lines, new_offset, inode)``. A changed inode or a file shorter than the offset
    (rotation / truncation) restarts at 0. A trailing partial line is left for the next read.
    """
    try:
        st = path.stat()
    except OSError:
        return [], 0, None
    if inode is not None and st.st_ino != inode:
        offset = 0
    if st.st_size < offset:
        offset = 0
    if st.st_size == offset:
        return [], offset, st.st_ino
    with path.open("rb") as fh:
        fh.seek(offset)
        chunk = fh.read(max_bytes)
    end = chunk.rfind(b"\n")
    if end == -1:
        return [], offset, st.st_ino
    data = chunk[: end + 1]
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines, offset + len(data), st.st_ino


def prune_days(days: dict[str, dict[str, Any]], today: date, keep: int = KEEP_DAYS) -> None:
    cutoff = (today - timedelta(days=keep - 1)).isoformat()
    for site in list(days):
        for day in list(days[site]):
            if day < cutoff:
                del days[site][day]
        if not days[site]:
            del days[site]


# --------------------------------------------------------------------------- db / files sizes
def parse_db_sizes(text: str) -> dict[str, int]:
    """``mariadb -N -B`` output 'schema<TAB>bytes' -> {schema: bytes}."""
    out: dict[str, int] = {}
    for line in (text or "").splitlines():
        parts = line.strip().split("\t")
        if len(parts) != 2:
            continue
        try:
            out[parts[0]] = int(float(parts[1]))
        except ValueError:
            continue
    return out


def parse_du(text: str) -> int:
    """Sum of ``du -sb`` output lines ('<bytes>\\t<path>')."""
    total = 0
    for line in (text or "").splitlines():
        first = line.strip().split("\t", 1)[0].split(" ", 1)[0]
        if first.isdigit():
            total += int(first)
    return total


def db_size_command(container: str = DB_CONTAINER) -> list[str]:
    """argv for the DB size query; the password comes from the env var MYSQL_PWD."""
    return [
        "docker", "exec", "-e", "MYSQL_PWD", container,
        "mariadb", "-uroot", "-N", "-B", "-e", DB_SIZE_QUERY,
    ]


def to_mb(n: int | float | None) -> float:
    return round((n or 0) / MB, 2)


# --------------------------------------------------------------------------- collector
class UsageCollector:
    def __init__(self, settings, runner, benches, sites):
        self.settings = settings
        self.runner = runner
        self.benches = benches
        self.sites = sites

    @property
    def state_path(self) -> Path:
        return self.settings.data_dir / STATE_FILE

    # ---- state -----------------------------------------------------------------------
    def load_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("monitor", {})
        data.setdefault("days", {})
        data.setdefault("sizes", {})
        return data

    def save_state(self, state: dict[str, Any]) -> None:
        atomic_write(self.state_path, json.dumps(state, sort_keys=True), mode=0o600)

    # ---- sources ----------------------------------------------------------------------
    def monitor_log(self, bench: str) -> Path:
        return self.benches.path(bench) / "logs" / MONITOR_LOG

    def read_monitor(self, state: dict[str, Any], bench: str) -> int:
        entry = state["monitor"].get(bench) or {}
        lines, offset, inode = read_new_lines(
            self.monitor_log(bench), int(entry.get("offset") or 0), entry.get("inode")
        )
        used = accumulate(state["days"], lines)
        state["monitor"][bench] = {"offset": offset, "inode": inode}
        return used

    def db_sizes(self, state: dict[str, Any], now: datetime) -> dict[str, int] | None:
        cached = state.get("db_sizes") or {}
        if cached.get("at") and now.timestamp() - float(cached["at"]) < DB_SIZE_CACHE_SECONDS:
            return cached.get("sizes")
        try:
            password = self.settings.db_root_password()
        except Exception:
            return cached.get("sizes")
        res = self.runner.run(db_size_command(), env={"MYSQL_PWD": password}, timeout=DB_TIMEOUT)
        if not res.ok:
            return cached.get("sizes")
        sizes = parse_db_sizes(res.stdout)
        state["db_sizes"] = {"at": now.timestamp(), "sizes": sizes}
        return sizes

    def files_size(self, state: dict[str, Any], bench: str, site: str, now: datetime,
                   force: bool = False) -> int | None:
        cached = state["sizes"].get(site) or {}
        if not force and cached.get("at") and now.timestamp() - float(cached["at"]) < SIZE_CACHE_SECONDS:
            return cached.get("bytes")
        site_dir = self.sites.site_dir(bench, site)
        # offloaded files (bucket mounts, §11) are not on this disk: du would list the bucket
        paths = [p for p in (site_dir / "public" / "files", site_dir / "private" / "files")
                 if p.is_dir() and not os.path.ismount(p)]
        if not paths:
            total = 0
        else:
            res = self.runner.run(["du", "-sb", "--", *map(str, paths)], timeout=DU_TIMEOUT)
            if not res.ok and not res.stdout:
                return cached.get("bytes")
            total = parse_du(res.stdout)
        state["sizes"][site] = {"at": now.timestamp(), "bytes": total}
        return total

    # ---- collect ----------------------------------------------------------------------
    def targets(self, bench: str | None = None, site: str | None = None) -> list[tuple[str, str]]:
        if site:
            owner = self.sites.find_bench(v.site_name(site))
            return [(owner, site)] if owner and (not bench or owner == bench) else []
        benches = [v.bench_name(bench)] if bench else self.benches.names()
        return [(b, s) for b in benches if self.benches.exists(b) for s in self.benches.sites(b)]

    def collect(self, bench: str | None = None, site: str | None = None, now: datetime | None = None,
                force_sizes: bool = False) -> dict[str, Any]:
        now = now or utcnow()
        today = now.date()
        with _LOCK:
            state = self.load_state()
            targets = self.targets(bench, site)
            lines = 0
            for b in sorted({b for b, _ in targets}):
                lines += self.read_monitor(state, b)
            prune_days(state["days"], today)
            db_sizes = self.db_sizes(state, now) if targets else {}
            out_sites: dict[str, Any] = {}
            for b, s in targets:
                cfg = self.sites.site_config(b, s)
                db_bytes = (db_sizes or {}).get(cfg.get("db_name") or "") if db_sizes is not None else None
                files = self.files_size(state, b, s, now, force=force_sizes)
                days = {
                    d: {
                        "cpu_seconds": round(c["cpu_us"] / 1_000_000, 3),
                        "requests": c.get("requests", 0),
                        "jobs": c.get("jobs", 0),
                    }
                    for d, c in sorted((state["days"].get(s) or {}).items())
                }
                out_sites[s] = {
                    "bench": b,
                    "database_mb": to_mb(db_bytes) if db_bytes is not None else None,
                    "storage_mb": to_mb(files) if files is not None else None,
                    "monitor_enabled": bool(cfg.get("monitor")),
                    "days": days,
                }
            self.save_state(state)
        return {"collected_at": iso(now), "date": today.isoformat(), "lines_read": lines, "sites": out_sites}


def compact_usage(report: dict[str, Any], days: int = 2) -> dict[str, Any]:
    """Heartbeat block: per site db/storage and the last ``days`` UTC days of cpu/requests."""
    today = report.get("date") or utcnow().date().isoformat()
    first = (date.fromisoformat(today) - timedelta(days=days - 1)).isoformat()
    sites: dict[str, Any] = {}
    for name, info in (report.get("sites") or {}).items():
        entry: dict[str, Any] = {
            "days": {
                d: {"cpu_seconds": c["cpu_seconds"], "requests": c["requests"]}
                for d, c in (info.get("days") or {}).items()
                if first <= d <= today
            },
        }
        for key in ("database_mb", "storage_mb"):
            if info.get(key) is not None:
                entry[key] = info[key]
        sites[name] = entry
    return {"collected_at": report.get("collected_at"), "date": today, "sites": sites}


class HeartbeatUsage:
    """Agent-side throttle: usage rides along with a heartbeat at most every 15 minutes."""

    def __init__(self, collector_factory, interval: float = HEARTBEAT_USAGE_SECONDS,
                 clock=lambda: utcnow().timestamp()):
        self.collector_factory = collector_factory
        self.interval = interval
        self.clock = clock
        self.last: float | None = None

    def due(self) -> bool:
        return self.last is None or self.clock() - self.last >= self.interval

    def maybe_collect(self) -> dict[str, Any] | None:
        if not self.due():
            return None
        now = self.clock()
        try:
            report = self.collector_factory().collect()
        except Exception:
            # never break the heartbeat; retry next interval
            self.last = now
            return None
        self.last = now
        return compact_usage(report)
