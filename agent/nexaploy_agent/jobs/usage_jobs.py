"""``site.usage``: per-site resource usage (CPU/request time, database size, file storage)."""

from __future__ import annotations

from typing import Any

from pydantic import field_validator

from .. import validation as v
from ..runtime.usage import compact_usage
from .base import BaseParams, Job, NotFound
from .site_jobs import enable_monitor


class SiteUsageParams(BaseParams):
    bench: str | None = None
    site: str | None = None
    # also turn the Frappe monitor on for sites that do not have it yet
    ensure_monitor: bool = False
    # bypass the file-size cache (du) for this run
    refresh_sizes: bool = False

    @field_validator("bench")
    @classmethod
    def _bench(cls, value: str | None) -> str | None:
        return v.bench_name(value) if value else None

    @field_validator("site")
    @classmethod
    def _site(cls, value: str | None) -> str | None:
        return v.site_name(value) if value else None


class SiteUsage(Job):
    """Result: ``{usage: <heartbeat block>, sites: {site: {bench, database_mb, storage_mb,
    monitor_enabled, days: {YYYY-MM-DD: {cpu_seconds, requests, jobs}}}}, collected_at, date}``."""

    kind = "site.usage"
    steps = ("collect",)
    Params = SiteUsageParams

    @classmethod
    def precheck(cls, ctx, params: dict[str, Any]) -> None:
        if params.get("bench") and not ctx.benches.exists(params["bench"]):
            raise NotFound(f"bench {params['bench']!r} not found")
        if params.get("site") and not ctx.sites.find_bench(params["site"]):
            raise NotFound(f"site {params['site']!r} not found")

    def step_collect(self) -> None:
        p = self.p
        if p.ensure_monitor:
            for bench, site in self.ctx.usage.targets(p.bench, p.site):
                self.site = site  # used by enable_monitor
                enable_monitor(self, bench)
        report = self.ctx.usage.collect(bench=p.bench, site=p.site, force_sizes=p.refresh_sizes)
        self.result.update(report)
        self.result["usage"] = compact_usage(report)
        self.log(f"usage of {len(report['sites'])} site(s); {report['lines_read']} new monitor lines")
