"""Server resource charts (frappe-free): Server Metric rows -> downsampled percentage series.

Rows come every ~5 minutes (ops.record_heartbeat_metrics); a 30-day window is ~8600 rows, so
they are averaged into at most `max_points` buckets for the inline SVG charts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping

PERIODS = {"24h": 1, "7d": 7, "30d": 30}
DEFAULT_PERIOD = "24h"
MAX_POINTS = 180


def period_days(period: str | None) -> int:
	return PERIODS.get(period or DEFAULT_PERIOD, PERIODS[DEFAULT_PERIOD])


def _pct(used: Any, total: Any) -> float | None:
	try:
		used, total = float(used or 0), float(total or 0)
	except (TypeError, ValueError):
		return None
	if total <= 0:
		return None
	return max(0.0, min(100.0, round(used * 100.0 / total, 1)))


def point(row: Mapping[str, Any], cpu_count: Any) -> dict[str, Any]:
	"""One sample: CPU load as % of the cores (load_1 / cpus), memory % and disk %."""
	try:
		cpus = max(1, int(cpu_count or 0))
	except (TypeError, ValueError):
		cpus = 1
	try:
		cpu = round(min(100.0, max(0.0, float(row.get("load_1") or 0) * 100.0 / cpus)), 1)
	except (TypeError, ValueError):
		cpu = None
	return {
		"t": row.get("timestamp"),
		"cpu": cpu,
		"memory": _pct(row.get("memory_used_mb"), row.get("memory_total_mb")),
		"disk": _pct(row.get("disk_used_gb"), row.get("disk_total_gb")),
	}


def _avg(values: list[float]) -> float | None:
	values = [v for v in values if v is not None]
	return round(sum(values) / len(values), 1) if values else None


def downsample(points: list[dict[str, Any]], max_points: int = MAX_POINTS) -> list[dict[str, Any]]:
	"""Average consecutive points into at most `max_points` buckets (keeps the bucket's
	first timestamp; peaks are kept separately in the summary)."""
	if len(points) <= max_points:
		return points
	size = -(-len(points) // max_points)  # ceil
	out = []
	for i in range(0, len(points), size):
		chunk = points[i:i + size]
		out.append({
			"t": chunk[0]["t"],
			**{k: _avg([p[k] for p in chunk]) for k in ("cpu", "memory", "disk")},
		})
	return out


def summary(points: list[dict[str, Any]]) -> dict[str, dict[str, float | None]]:
	"""current / average / peak per metric over the raw (not downsampled) points."""
	out = {}
	for k in ("cpu", "memory", "disk"):
		values = [p[k] for p in points if p[k] is not None]
		out[k] = {
			"current": values[-1] if values else None,
			"avg": _avg(values),
			"max": max(values) if values else None,
		}
	return out


def build_series(rows: Iterable[Mapping[str, Any]], cpu_count: Any,
                 max_points: int = MAX_POINTS) -> dict[str, Any]:
	raw = [point(r, cpu_count) for r in rows]
	raw.sort(key=lambda p: p["t"] if isinstance(p["t"], (datetime, str)) else "")
	pts = downsample(raw, max_points)
	return {
		"points": [{**p, "t": p["t"].isoformat() if isinstance(p["t"], datetime) else p["t"]} for p in pts],
		"summary": summary(raw),
		"samples": len(raw),
	}
