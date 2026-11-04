"""``site.tls_status``: which certificate Traefik serves for each routed host (runtime/tls.py).

Params ``{site?, domains?}``: no site -> every routed host of the server; ``site`` -> the
primary name and custom domains of that site; ``domains`` narrows it further (hosts that are
not routed here are reported with ``error: "not routed on this server"``).
Result ``{collected_at, port, hosts: {host: {site, trusted, not_after, issuer, default_cert, ...}}}``
- the same block the heartbeat carries as ``tls``.
"""

from __future__ import annotations

from typing import Any

from pydantic import field_validator

from .. import validation as v
from .base import BaseParams, Job, NotFound


class TlsStatusParams(BaseParams):
    site: str | None = None
    domains: list[str] = []

    @field_validator("site")
    @classmethod
    def _site(cls, value: str | None) -> str | None:
        return v.site_name(value.lower()) if value else None

    @field_validator("domains")
    @classmethod
    def _domains(cls, value: list[str]) -> list[str]:
        if len(value) > 200:
            raise ValueError("at most 200 domains")
        return [v.domain_name(str(d).strip().lower()) for d in value]


class SiteTlsStatus(Job):
    kind = "site.tls_status"
    steps = ("check",)
    Params = TlsStatusParams

    @classmethod
    def precheck(cls, ctx, params: dict[str, Any]) -> None:
        if params.get("site") and not ctx.sites.find_bench(params["site"]):
            raise NotFound(f"site {params['site']!r} not found")

    def step_check(self) -> None:
        p = self.p
        routed = self.ctx.tls.hosts(p.site)
        hosts = {h: s for h, s in routed.items() if h in p.domains} if p.domains else routed
        report = self.ctx.tls.check(hosts)
        for host in p.domains:
            if host not in hosts:
                report["hosts"][host] = {"host": host, "site": p.site, "trusted": False,
                                         "error": "not routed on this server"}
        self.result.update(report)
        for host, info in sorted(report["hosts"].items()):
            state = ("default certificate" if info.get("default_cert") else
                     "trusted" if info.get("trusted") else
                     info.get("error") or info.get("verify_error") or "untrusted")
            self.log(f"{host}: {state}; issuer {info.get('issuer') or '-'}; expires {info.get('not_after') or '-'}")
