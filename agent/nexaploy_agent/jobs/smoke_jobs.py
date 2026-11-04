"""``site.smoke_check`` ``{site, paths?, timeout?, apps?}``: is a site healthy on its bench?

Used by the control plane's "Safe update" test after a site was cloned onto a bench running a
newer image (and migrated there). Steps:

- ``http``: GET each path (default ``/api/method/ping`` and ``/login``) against gunicorn inside
  the backend container with ``Host: <site>``; each must answer 200 (ping must say ``pong``);
- ``database``: ``frappe.connect()`` + ``frappe.get_all("User", limit=1)`` and, when ``apps`` is
  given, every app must be installed on the site;
- ``verdict``: fails the job when any check failed (the error lists them).

Result: ``{site, bench, checks: [{name, ok, detail}], http: [...], ok, summary}``. Read only.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from .. import validation as v
from ..runtime import smoke as rs
from .base import SiteParams
from .site_jobs import _SiteJob


class SiteSmokeCheckParams(SiteParams):
    paths: list[str] = Field(default_factory=lambda: list(rs.DEFAULT_PATHS), max_length=rs.MAX_PATHS)
    timeout: int = Field(rs.DEFAULT_TIMEOUT, ge=1, le=rs.MAX_TIMEOUT)
    apps: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("paths")
    @classmethod
    def _paths(cls, value: list[str]) -> list[str]:
        out = [rs.valid_path(p) for p in value]
        return out or list(rs.DEFAULT_PATHS)

    @field_validator("apps")
    @classmethod
    def _apps(cls, value: list[str]) -> list[str]:
        return [v.app_name(a) for a in value]


class SiteSmokeCheck(_SiteJob):
    kind = "site.smoke_check"
    steps = ("http", "database", "verdict")
    Params = SiteSmokeCheckParams

    def _exec(self, bench: str, args: list[str], timeout: int):
        argv = [self.ctx.docker.bin, "compose", "-p", bench, "--project-directory",
                str(self.ctx.benches.path(bench)), "exec", "-T", "-w", rs.CONTAINER_SITES, "backend", *args]
        self.log("$ docker compose -p " + bench + " exec -T backend python -c <probe> " + " ".join(args[3:]))
        return self.ctx.base_runner.run(argv, cwd=self.ctx.benches.path(bench), timeout=timeout)

    def _checks(self) -> list[dict[str, Any]]:
        return list(self.result.get("checks") or [])

    def step_http(self) -> None:
        bench = self.bench_for_site()
        self.result["site"] = self.site
        if not self.ctx.docker.is_running(bench):
            self.fail(f"bench {bench} is not running")
        p = self.p
        res = self._exec(bench, rs.http_args(self.site, p.paths, p.timeout), timeout=p.timeout * len(p.paths) + 60)
        probes = rs.parse_output(res.stdout)
        if not isinstance(probes, list):
            tail = (res.stderr or res.stdout or "").strip()[-1000:]
            checks = [{"name": "http", "ok": False, "detail": f"probe failed (exit {res.returncode}): {tail}"}]
            probes = []
        else:
            checks = rs.evaluate_http(probes)
        self.result["http"] = probes
        self.result["checks"] = [c for c in self._checks() if not c["name"].startswith("http")] + checks
        for c in checks:
            self.log(f"{'ok  ' if c['ok'] else 'FAIL'} {c['name']}: {c['detail']}")

    def step_database(self) -> None:
        bench = self.bench_for_site()
        res = self._exec(bench, rs.db_args(self.site), timeout=180)
        data = rs.parse_output(res.stdout)
        if data is None and res.stderr:
            self.log(res.stderr.strip()[-1000:])
        checks = rs.evaluate_db(data, self.p.apps)
        keep = [c for c in self._checks() if c["name"] not in ("database", "installed apps")]
        self.result["checks"] = keep + checks
        for c in checks:
            self.log(f"{'ok  ' if c['ok'] else 'FAIL'} {c['name']}: {c['detail']}")

    def step_verdict(self) -> None:
        checks = self._checks()
        ok = bool(checks) and all(c.get("ok") for c in checks)
        self.result["ok"] = ok
        self.result["summary"] = rs.summary(checks)
        self.save()
        if not ok:
            self.fail("smoke check failed: " + self.result["summary"])
        self.log(self.result["summary"])
