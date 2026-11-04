"""Site logs, request analytics and slow queries (ARCHITECTURE.md §10, "observability" kinds).

* :func:`tail_file` / :func:`filter_site_lines` – bench log files (``benches/<b>/logs/*.log``,
  ``sites/<s>/logs/*.log``). On a bench shared by several sites, bench-level lines are filtered to
  those mentioning the site so one customer never sees another's log lines.
* :func:`aggregate_monitor` – Frappe monitor lines (``logs/monitor.json.log``, also read by
  ``runtime/usage.py``) aggregated for a date range: top paths by count / total time, status codes,
  p50/p95, background jobs by method, per-hour histogram.
* :func:`parse_slow_log` / :func:`group_slow_queries` – the shared MariaDB slow query log
  (``/opt/nexaploy/db/data/slow.log``, enabled in ``bench-template/db/docker-compose.yml``),
  filtered by the site's schema, normalised (literals -> ``?``) and grouped.

All functions are pure or only read files; command execution lives in the jobs.
"""

from __future__ import annotations

import json
import math
import re
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .usage import MONITOR_LOG, parse_monitor_timestamp

MB = 1024 * 1024
MAX_LOG_BYTES = 2 * MB          # text returned by site.logs (all sources together)
MAX_LINE_CHARS = 4000
DEFAULT_LINES = 200
MAX_LINES = 5000
FILTER_WINDOW_BYTES = 16 * MB   # how far back to look when filtering a shared log by site
MAX_MONITOR_BYTES = 512 * MB    # analytics reads at most this much of each monitor log
MAX_SLOW_LOG_BYTES = 64 * MB    # slow.log tail that is parsed
MAX_DURATION_SAMPLES = 200_000
TOP_N = 20

# log name -> bench-level files, site-level files, compose services (docker compose logs)
LOG_SOURCES: dict[str, dict[str, tuple[str, ...]]] = {
    "web": {"bench": ("web.log", "web.error.log"), "site": (), "services": ("backend",)},
    "worker": {
        "bench": ("worker.log", "worker.error.log"), "site": (),
        "services": ("queue-short", "queue-default", "queue-long"),
    },
    "scheduler": {
        "bench": ("scheduler.log", "scheduler.error.log"), "site": ("scheduler.log",),
        "services": ("scheduler",),
    },
    "frappe": {"bench": ("frappe.log",), "site": ("frappe.log", "database.log"), "services": ()},
}
LOG_NAMES = (*LOG_SOURCES, "error")


# --------------------------------------------------------------------------- tails
def _clip(line: str) -> str:
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + " …"


def tail_file(path: Path, lines: int, max_bytes: int = MAX_LOG_BYTES) -> list[str]:
    """The last ``lines`` lines of ``path`` reading at most ``max_bytes`` from the end."""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size == 0 or lines <= 0:
        return []
    start = max(0, size - max_bytes)
    with path.open("rb") as fh:
        fh.seek(start)
        data = fh.read(max_bytes)
    text = data.decode("utf-8", errors="replace")
    out = text.splitlines()
    if start > 0 and out:
        out = out[1:]  # first line is partial
    return [_clip(ln) for ln in out[-lines:]]


def mentions_site(line: str, site: str) -> bool:
    return site in line


def filter_site_lines(lines: Iterable[str], site: str, limit: int) -> list[str]:
    """Last ``limit`` lines that mention ``site``."""
    return list(deque((ln for ln in lines if mentions_site(ln, site)), maxlen=max(0, limit)))


def cap_text(lines: list[str], budget: int) -> tuple[list[str], bool, int]:
    """Keep the newest lines within ``budget`` chars. Returns (lines, truncated, used)."""
    kept: deque[str] = deque()
    used = 0
    for ln in reversed(lines):
        size = len(ln) + 1
        if used + size > budget:
            return list(kept), True, used
        kept.appendleft(ln)
        used += size
    return list(kept), False, used


def log_files(bench_logs: Path, site_logs: Path, log: str) -> list[tuple[str, Path, bool]]:
    """Existing files for ``log``: ``[(label, path, shared)]``; shared = bench-level file."""
    spec = LOG_SOURCES.get(log) or {}
    out: list[tuple[str, Path, bool]] = []
    for name in spec.get("bench", ()):
        p = bench_logs / name
        if p.is_file():
            out.append((f"logs/{name}", p, True))
    for name in spec.get("site", ()):
        p = site_logs / name
        if p.is_file():
            out.append((f"sites/<site>/logs/{name}", p, False))
    return out


SINCE_RE = re.compile(r"^(\d{1,5}[smhd]|\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?Z?)?)$")


def valid_since(value: str | None) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not SINCE_RE.match(value):
        raise ValueError("since must look like 30m, 2h, 1d or 2026-11-03[T10:00[:00]Z]")
    return value


# --------------------------------------------------------------------------- monitor analytics
def parse_monitor_record(line: str) -> dict[str, Any] | None:
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def percentile(sorted_values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile of an ascending list."""
    if not sorted_values:
        return None
    k = max(0, min(len(sorted_values) - 1, math.ceil(pct / 100.0 * len(sorted_values)) - 1))
    return sorted_values[k]


def _ms(us: float) -> float:
    return round(us / 1000.0, 2)


def iter_monitor_lines(path: Path, site: str, max_bytes: int = MAX_MONITOR_BYTES) -> Iterator[str]:
    """Lines of a monitor log that may belong to ``site`` (cheap substring prefilter)."""
    try:
        size = path.stat().st_size
    except OSError:
        return
    needle = f'"{site}"'.encode()
    start = max(0, size - max_bytes)
    with path.open("rb") as fh:
        fh.seek(start)
        if start:
            fh.readline()  # partial line
        for raw in fh:
            if needle in raw:
                yield raw.decode("utf-8", errors="replace")


def monitor_files(bench_logs: Path) -> list[Path]:
    """monitor.json.log plus rotated copies (monitor.json.log.1, ...), oldest first."""
    files = [p for p in bench_logs.glob(MONITOR_LOG + "*") if p.is_file()]

    def order(p: Path) -> int:
        suffix = p.name[len(MONITOR_LOG):].lstrip(".")
        return -int(suffix) if suffix.isdigit() else 0

    return sorted(files, key=order)


def aggregate_monitor(lines: Iterable[str], site: str, date_from: date, date_to: date,
                      top: int = TOP_N) -> dict[str, Any]:
    """Aggregate monitor lines of ``site`` whose UTC date is within [date_from, date_to]."""
    paths: dict[str, dict[str, float]] = {}
    methods: dict[str, dict[str, float]] = {}
    statuses: dict[str, int] = {}
    hours: dict[str, dict[str, float]] = {}
    days: dict[str, dict[str, float]] = {}
    durations: list[float] = []
    req = {"count": 0, "duration_us": 0.0}
    jobs = {"count": 0, "duration_us": 0.0}
    seen = 0
    first = last = None
    for line in lines:
        data = parse_monitor_record(line)
        if not data or data.get("site") != site:
            continue
        ttype = data.get("transaction_type")
        if ttype not in ("request", "job"):
            continue
        ts = parse_monitor_timestamp(data.get("timestamp"))
        duration = data.get("duration")
        if ts is None or isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration < 0:
            continue
        if not (date_from <= ts.date() <= date_to):
            continue
        seen += 1
        first = ts if first is None or ts < first else first
        last = ts if last is None or ts > last else last
        hour = ts.strftime("%Y-%m-%dT%H:00Z")
        day = ts.date().isoformat()
        hb = hours.setdefault(hour, {"requests": 0, "jobs": 0, "duration_us": 0.0, "errors": 0})
        db = days.setdefault(day, {"requests": 0, "jobs": 0, "duration_us": 0.0, "errors": 0})
        hb["duration_us"] += duration
        db["duration_us"] += duration
        if ttype == "request":
            info = data.get("request") if isinstance(data.get("request"), dict) else {}
            path = str(info.get("path") or "?").split("?", 1)[0][:300]
            status = str(info.get("status_code") or "?")
            req["count"] += 1
            req["duration_us"] += duration
            hb["requests"] += 1
            db["requests"] += 1
            if status.startswith("5"):
                hb["errors"] += 1
                db["errors"] += 1
            statuses[status] = statuses.get(status, 0) + 1
            p = paths.setdefault(path, {"count": 0, "duration_us": 0.0, "max_us": 0.0})
            p["count"] += 1
            p["duration_us"] += duration
            p["max_us"] = max(p["max_us"], duration)
            if len(durations) < MAX_DURATION_SAMPLES:
                durations.append(duration)
            else:  # deterministic thinning keeps the distribution roughly intact
                durations[req["count"] % MAX_DURATION_SAMPLES] = duration
        else:
            info = data.get("job") if isinstance(data.get("job"), dict) else {}
            method = str(info.get("method") or "?")[:300]
            jobs["count"] += 1
            jobs["duration_us"] += duration
            hb["jobs"] += 1
            db["jobs"] += 1
            m = methods.setdefault(method, {"count": 0, "duration_us": 0.0, "max_us": 0.0})
            m["count"] += 1
            m["duration_us"] += duration
            m["max_us"] = max(m["max_us"], duration)

    def rows(table: dict[str, dict[str, float]], key: str, sort: str) -> list[dict[str, Any]]:
        items = sorted(table.items(), key=lambda kv: (-kv[1][sort], kv[0]))[:top]
        return [{
            key: name,
            "count": int(v["count"]),
            "total_ms": _ms(v["duration_us"]),
            "avg_ms": _ms(v["duration_us"] / v["count"]) if v["count"] else 0,
            "max_ms": _ms(v["max_us"]),
        } for name, v in items]

    durations.sort()

    def bucket_rows(table: dict[str, dict[str, float]], key: str) -> list[dict[str, Any]]:
        return [{key: k, "requests": int(v["requests"]), "jobs": int(v["jobs"]),
                 "errors": int(v["errors"]), "duration_ms": _ms(v["duration_us"])}
                for k, v in sorted(table.items())]

    p50 = percentile(durations, 50)
    p95 = percentile(durations, 95)
    return {
        "site": site,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "transactions": seen,
        "first_at": first.isoformat() if first else None,
        "last_at": last.isoformat() if last else None,
        "requests": {
            "count": req["count"],
            "total_ms": _ms(req["duration_us"]),
            "avg_ms": _ms(req["duration_us"] / req["count"]) if req["count"] else None,
            "p50_ms": _ms(p50) if p50 is not None else None,
            "p95_ms": _ms(p95) if p95 is not None else None,
        },
        "jobs": {"count": jobs["count"], "total_ms": _ms(jobs["duration_us"])},
        "paths_by_count": rows(paths, "path", "count"),
        "paths_by_duration": rows(paths, "path", "duration_us"),
        "jobs_by_method": rows(methods, "method", "duration_us"),
        # list (not a dict keyed by code) so key-based redaction never touches it
        "status_codes": [{"status": k, "count": v} for k, v in sorted(statuses.items())],
        "hourly": bucket_rows(hours, "hour"),
        "daily": bucket_rows(days, "date"),
    }


def date_range(date_from: str | None, date_to: str | None, today: date,
               default_days: int = 7, max_days: int = 31) -> tuple[date, date]:
    """Parse/clamp an analytics range (inclusive, UTC)."""
    end = date.fromisoformat(date_to) if date_to else today
    start = date.fromisoformat(date_from) if date_from else end - timedelta(days=default_days - 1)
    if start > end:
        raise ValueError("date_from is after date_to")
    if (end - start).days + 1 > max_days:
        raise ValueError(f"at most {max_days} days per request")
    return start, end


# --------------------------------------------------------------------------- slow query log
_HEADER_RE = re.compile(r"^# (Time|User@Host):")
_FIELD_RE = re.compile(r"([A-Za-z_]+):\s+(\S+)")
_USE_RE = re.compile(r"^use\s+`?([A-Za-z0-9_$]+)`?;?\s*$", re.IGNORECASE)
_SET_TS_RE = re.compile(r"^SET\s+timestamp\s*=\s*(\d+)\s*;?\s*$", re.IGNORECASE)
_TIME_RE = re.compile(r"^# Time:\s+(.+)$")


def _parse_time(value: str) -> str | None:
    value = value.strip()
    for fmt in ("%y%m%d %H:%M:%S", "%y%m%d  %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()
    except ValueError:
        return None


def parse_slow_log(text: str) -> list[dict[str, Any]]:
    """MariaDB/MySQL slow log text -> ``[{schema, query_time, lock_time, rows_sent,
    rows_examined, timestamp, query}]``. Server start-up banners are ignored."""
    entries: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    last_time: str | None = None

    def flush() -> None:
        nonlocal cur
        if cur is not None and cur["sql"]:
            query = "\n".join(cur.pop("sql")).strip()
            if query:
                cur["query"] = query
                entries.append(cur)
        cur = None

    for raw in (text or "").splitlines():
        line = raw.rstrip("\r")
        m = _TIME_RE.match(line)
        if m:
            flush()
            last_time = _parse_time(m.group(1)) or last_time
            continue
        if line.startswith("# User@Host:"):
            flush()
            cur = {"schema": None, "query_time": 0.0, "lock_time": 0.0, "rows_sent": 0,
                   "rows_examined": 0, "timestamp": last_time, "user": line[12:].strip()[:80], "sql": []}
            continue
        if cur is None:
            continue
        if line.startswith("#"):
            for key, value in _FIELD_RE.findall(line):
                k = key.lower()
                if k == "schema":
                    cur["schema"] = value
                elif k in ("query_time", "lock_time"):
                    try:
                        cur[k] = float(value)
                    except ValueError:
                        pass
                elif k in ("rows_sent", "rows_examined"):
                    try:
                        cur[k] = int(float(value))
                    except ValueError:
                        pass
            continue
        mu = _USE_RE.match(line.strip())
        if mu and not cur["sql"]:
            cur["schema"] = cur["schema"] or mu.group(1)
            continue
        mt = _SET_TS_RE.match(line.strip())
        if mt and not cur["sql"]:
            cur["timestamp"] = datetime.fromtimestamp(int(mt.group(1)), tz=timezone.utc).isoformat()
            continue
        if re.match(r"^\S+, Version: .* started with:$", line) or line.startswith(("Tcp port:", "Time  ")):
            flush()
            continue
        cur["sql"].append(line)
    flush()
    return entries


_STR_RE = re.compile(r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.|\"\")*\"")
_NUM_RE = re.compile(r"(?<![A-Za-z0-9_`$.])-?\b\d+(?:\.\d+)?(?:e[+-]?\d+)?\b", re.IGNORECASE)
_HEX_RE = re.compile(r"\b0x[0-9a-f]+\b", re.IGNORECASE)
_IN_RE = re.compile(r"\(\s*\?(?:\s*,\s*\?)+\s*\)")
_VALUES_RE = re.compile(r"(\(\s*\?(?:\s*,\s*\?)*\s*\))(?:\s*,\s*\(\s*\?(?:\s*,\s*\?)*\s*\))+")
_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_WS_RE = re.compile(r"\s+")
_LINE_COMMENT_RE = re.compile(r"--\s[^\n]*")


def normalize_query(query: str) -> str:
    """Fingerprint: comments removed, literals -> ?, IN lists / VALUES tuples collapsed."""
    q = _COMMENT_RE.sub(" ", query or "")
    q = _STR_RE.sub("?", q)
    q = _LINE_COMMENT_RE.sub(" ", q)
    q = _HEX_RE.sub("?", q)
    q = _NUM_RE.sub("?", q)
    q = _VALUES_RE.sub(r"\1, ...", q)
    q = _IN_RE.sub("(?, ...)", q)
    q = _WS_RE.sub(" ", q).strip().rstrip(";").strip()
    return q


def group_slow_queries(entries: Iterable[dict[str, Any]], schema: str, min_seconds: float = 0.0,
                       limit: int = 50, example_chars: int = 2000) -> list[dict[str, Any]]:
    """Entries of ``schema`` grouped by fingerprint, sorted by total time (desc)."""
    groups: dict[str, dict[str, Any]] = {}
    for e in entries:
        if e.get("schema") != schema or e.get("query_time", 0) < min_seconds:
            continue
        fp = normalize_query(e["query"])
        if not fp:
            continue
        g = groups.get(fp)
        if g is None:
            g = groups[fp] = {
                "query": fp[:example_chars], "count": 0, "total_time": 0.0, "max_time": 0.0,
                "rows_examined_total": 0, "rows_examined_max": 0, "rows_sent_total": 0,
                "example": e["query"][:example_chars], "last_seen": None,
            }
        g["count"] += 1
        g["total_time"] += e.get("query_time", 0.0)
        if e.get("query_time", 0.0) >= g["max_time"]:
            g["max_time"] = e.get("query_time", 0.0)
            g["example"] = e["query"][:example_chars]
        g["rows_examined_total"] += e.get("rows_examined", 0)
        g["rows_examined_max"] = max(g["rows_examined_max"], e.get("rows_examined", 0))
        g["rows_sent_total"] += e.get("rows_sent", 0)
        ts = e.get("timestamp")
        if ts and (g["last_seen"] is None or ts > g["last_seen"]):
            g["last_seen"] = ts
    out = []
    for g in sorted(groups.values(), key=lambda g: (-g["total_time"], -g["count"])):
        n = g["count"]
        out.append({
            "query": g["query"],
            "count": n,
            "avg_time": round(g["total_time"] / n, 4),
            "max_time": round(g["max_time"], 4),
            "total_time": round(g["total_time"], 4),
            "rows_examined_avg": int(g["rows_examined_total"] / n),
            "rows_examined_max": g["rows_examined_max"],
            "rows_sent_avg": int(g["rows_sent_total"] / n),
            "example": g["example"],
            "last_seen": g["last_seen"],
        })
    return out[:limit]
