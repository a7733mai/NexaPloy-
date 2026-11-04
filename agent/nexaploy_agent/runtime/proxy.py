"""Traefik file-provider routes: one ``<site>.yml`` per site (§3.2).

Traefik watches the directory, so writing/removing a file is enough; no
reload is needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .. import validation as v
from ..fsutil import write_if_changed
from ..render import render_route


class ProxyManager:
    def __init__(self, sites_dir: Path, templates: Path | None = None):
        self.sites_dir = Path(sites_dir)
        self.templates = templates

    def route_path(self, site: str) -> Path:
        v.site_name(site)
        return self.sites_dir / f"{site}.yml"

    def has_route(self, site: str) -> bool:
        return self.route_path(site).exists()

    def write_route(self, site: str, domains: Iterable[str], bench: str) -> bool:
        """Render and write the route; returns True when the file changed."""
        v.site_name(site)
        v.bench_name(bench)
        domains = [v.domain_name(d) for d in domains]
        content = render_route(site, domains, bench, self.templates)
        self.sites_dir.mkdir(parents=True, exist_ok=True)
        # Traefik runs as uid 3100 and only needs to read: 0644.
        return write_if_changed(self.route_path(site), content, mode=0o644)

    def remove_route(self, site: str) -> bool:
        path = self.route_path(site)
        if path.exists():
            path.unlink()
            return True
        return False

    def routes(self) -> list[str]:
        if not self.sites_dir.is_dir():
            return []
        return sorted(
            p.stem for p in self.sites_dir.glob("*.yml") if v.SITE_RE.match(p.stem)
        )
