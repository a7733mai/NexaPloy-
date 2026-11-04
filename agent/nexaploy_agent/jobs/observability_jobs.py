"""Observability jobs (ARCHITECTURE.md §10 Phase 4): site.logs, site.analytics, site.slow_queries,
site.sql and site.console.

All of them are read-mostly and return their data in the job ``result`` (capped well below
~2 MB so ``job_update`` stays small). Commands that return bulk data are run on the *base*
runner so the data is not duplicated into step output; only a summary is logged.

``site.sql`` (write mode) and ``site.console`` are not idempotent: if the worker crashed while
they were running they are **not** re-run on resume (the step fails instead).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from ..config import FRAPPE_UID
from ..fsutil import chown, ensure_dir
from ..runtime import console as rc
from ..runtime import logs as rl
from ..runtime import sql as rs
from ..runtime.docker import Result
from .base import SiteParams
from .site_jobs import _SiteJob

ERROR_LOG_QUERY = (
    "SELECT creation, method, LEFT(error, {chars}) AS error FROM `tabError Log` "
    "ORDER BY creation DESC LIMIT {limit}"
)
MAX_ERROR_ROWS = 200


class _ObsJob(_SiteJob):
    def quiet(self, argv: list[str], **kw: Any) -> Result:
        """Run without teeing stdout into step output (bulk data); logs the command + stderr."""
        self.log("$ " + " ".join(str(a) for a in argv[:12]) + (" …" if len(argv) > 12 else ""))
        res = self.ctx.base_runner.run(argv, **kw)
        if res.stderr and not res.ok:
            self.log(res.stderr[-4000:])
        if not res.ok:
            self.log(f"[exit {res.returncode}]")
        return res

    def site_config(self, bench: str) -> dict[str, Any]:
        cfg = self.ctx.sites.site_config(bench, self.site)
        if not cfg:
            self.fail(f"cannot read site_config.json of {self.site}")
        self.secret(cfg.get("db_password"))
        return cfg

    def once(self, marker: str) -> None:
        """Guard for non-idempotent steps: refuse to run twice (resume after a crash)."""
        if self.result.get(marker) and not self.result.get(marker + "_done"):
            self.fail("interrupted while running; not re-run automatically — start a new job")
        self.result[marker] = True
        self.save()


# --------------------------------------------------------------------------- site.logs
class SiteLogsParams(SiteParams):
    log: Literal["web", "worker", "scheduler", "frappe", "error"] = "web"
    lines: int = Field(rl.DEFAULT_LINES, ge=1, le=rl.MAX_LINES)
    since: str | None = None

    @field_validator("since")
    @classmethod
    def _since(cls, value: str | None) -> str | None:
        return rl.valid_since(value)


class SiteLogs(_ObsJob):
    """Result: ``{site, bench, log, sources: [{source, kind, lines, truncated, filtered}],
    truncated}``; for ``log=error`` ``{rows: [{creation, method, error}]}`` instead of sources."""

    kind = "site.logs"
    steps = ("collect",)
    Params = SiteLogsParams

    def step_collect(self) -> None:
        bench = self.bench_for_site()
        p = self.p
        self.result.update({"site": self.site, "log": p.log, "lines": p.lines})
        if p.log == "error":
            return self.error_log(bench)
        bench_logs = self.ctx.benches.path(bench) / "logs"
        site_logs = self.ctx.sites.site_dir(bench, self.site) / "logs"
        shared = len(self.ctx.benches.sites(bench)) > 1
        sources: list[dict[str, Any]] = []
        budget = rl.MAX_LOG_BYTES
        for label, path, bench_level in rl.log_files(bench_logs, site_logs, p.log):
            filtered = bench_level and shared
            if filtered:
                lines = rl.filter_site_lines(rl.tail_file(path, 10 ** 9, rl.FILTER_WINDOW_BYTES), self.site, p.lines)
            else:
                lines = rl.tail_file(path, p.lines)
            lines, cut, used = rl.cap_text(lines, budget)
            budget -= used
            sources.append({"source": label, "kind": "file", "lines": lines, "truncated": cut, "filtered": filtered})
        for service in rl.LOG_SOURCES[p.log]["services"]:
            if budget <= 0:
                break
            args = ["logs", "--no-color", "--timestamps", "--tail", str(p.lines)]
            if p.since:
                args += ["--since", p.since]
            argv = [self.ctx.docker.bin, "compose", "-p", bench, "--project-directory",
                    str(self.ctx.benches.path(bench)), *args, service]
            res = self.quiet(argv, cwd=self.ctx.benches.path(bench), timeout=60)
            if not res.ok:
                sources.append({"source": service, "kind": "container", "lines": [], "truncated": False,
                                "filtered": False, "error": (res.stderr or "").strip()[-500:]})
                continue
            raw = [rl._clip(ln) for ln in (res.stdout + res.stderr).splitlines()]
            filtered = shared
            lines = rl.filter_site_lines(raw, self.site, p.lines) if filtered else raw[-p.lines:]
            lines, cut, used = rl.cap_text(lines, budget)
            budget -= used
            sources.append({"source": service, "kind": "container", "lines": lines, "truncated": cut,
                            "filtered": filtered})
        self.result["sources"] = sources
        self.result["truncated"] = budget <= 0 or any(s["truncated"] for s in sources)
        self.result["shared_bench"] = shared
        self.log(f"{sum(len(s['lines']) for s in sources)} line(s) from {len(sources)} source(s)")

    def error_log(self, bench: str) -> None:
        cfg = self.site_config(bench)
        limit = min(self.p.lines, MAX_ERROR_ROWS)
        query = ERROR_LOG_QUERY.format(chars=4000, limit=limit)
        try:
            data = rs.run_query(_Quiet(self), cfg, query, read_only=True, max_rows=limit, timeout=30)
        except rs.SqlValidationError as exc:
            self.fail(str(exc))
        if data["error"]:
            self.fail(f"reading Error Log failed: {data['error']}")
        cols = data["columns"]
        self.result["rows"] = [dict(zip(cols, r)) for r in data["rows"]]
        self.result["truncated"] = data["truncated"]
        self.log(f"{len(self.result['rows'])} Error Log row(s)")


class _Quiet:
    """Runner adapter: route a runtime helper's commands through ``job.quiet``."""

    def __init__(self, job: _ObsJob):
        self.job = job

    def run(self, args, cwd=None, env=None, input=None, timeout=None) -> Result:
        return self.job.quiet(list(args), cwd=cwd, env=env, input=input, timeout=timeout)


# --------------------------------------------------------------------------- site.analytics
class SiteAnalyticsParams(SiteParams):
    date_from: str | None = None
    date_to: str | None = None
    top: int = Field(rl.TOP_N, ge=1, le=100)

    @field_validator("date_from", "date_to")
    @classmethod
    def _date(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        datetime.strptime(value, "%Y-%m-%d")
        return value

    @model_validator(mode="after")
    def _range(self) -> "SiteAnalyticsParams":
        rl.date_range(self.date_from, self.date_to, datetime.now(timezone.utc).date())
        return self


class SiteAnalytics(_ObsJob):
    """Aggregate ``logs/monitor.json.log`` for the site (see runtime.logs.aggregate_monitor)."""

    kind = "site.analytics"
    steps = ("aggregate",)
    Params = SiteAnalyticsParams

    def step_aggregate(self) -> None:
        bench = self.bench_for_site()
        start, end = rl.date_range(self.p.date_from, self.p.date_to, datetime.now(timezone.utc).date())
        files = rl.monitor_files(self.ctx.benches.path(bench) / "logs")

        def lines():
            for f in files:
                yield from rl.iter_monitor_lines(f, self.site)

        report = rl.aggregate_monitor(lines(), self.site, start, end, top=self.p.top)
        report["monitor_enabled"] = bool(self.ctx.sites.site_config(bench, self.site).get("monitor"))
        report["sources"] = [f.name for f in files]
        self.result.update(report)
        self.log(f"{report['transactions']} transaction(s) {start}..{end} from {len(files)} file(s)")


# --------------------------------------------------------------------------- site.slow_queries
class SiteSlowQueriesParams(SiteParams):
    limit: int = Field(50, ge=1, le=200)
    min_seconds: float = Field(0.0, ge=0, le=3600)


class SiteSlowQueries(_ObsJob):
    """Parse the shared MariaDB slow log (``<root>/db/data/slow.log``) for the site's schema."""

    kind = "site.slow_queries"
    steps = ("parse",)
    Params = SiteSlowQueriesParams

    def step_parse(self) -> None:
        bench = self.bench_for_site()
        cfg = self.site_config(bench)
        schema = cfg.get("db_name")
        if not schema:
            self.fail("site_config.json has no db_name")
        path = self.settings.root / "db" / "data" / "slow.log"
        self.result.update({"site": self.site, "enabled": path.is_file(), "log_size": 0, "queries": []})
        if not path.is_file():
            self.log(f"{path} not found: enable the slow query log in the nexaploy-db stack")
            return
        size = path.stat().st_size
        with path.open("rb") as fh:
            start = max(0, size - rl.MAX_SLOW_LOG_BYTES)
            fh.seek(start)
            text = fh.read().decode("utf-8", errors="replace")
        if start:
            # skip to the first complete entry
            cut = text.find("\n# Time:")
            cut = cut if cut != -1 else text.find("\n# User@Host:")
            text = text[cut + 1:] if cut != -1 else ""
        entries = rl.parse_slow_log(text)
        groups = rl.group_slow_queries(entries, schema, self.p.min_seconds, self.p.limit)
        self.result.update({"log_size": size, "scanned_bytes": size - start, "queries": groups,
                            "entries": sum(1 for e in entries if e.get("schema") == schema)})
        self.log(f"{len(groups)} slow query pattern(s) for {self.site}")


# --------------------------------------------------------------------------- site.sql
class SiteSqlParams(SiteParams):
    query: str = Field(min_length=1, max_length=rs.MAX_QUERY_CHARS)
    read_only: bool = True
    max_rows: int = Field(rs.DEFAULT_MAX_ROWS, ge=1, le=rs.MAX_ROWS)
    timeout: int = Field(rs.DEFAULT_TIMEOUT, ge=1, le=rs.MAX_TIMEOUT)

    @model_validator(mode="after")
    def _query(self) -> "SiteSqlParams":
        try:
            rs.validate_query(self.query, read_only=self.read_only)
        except rs.SqlValidationError as exc:
            raise ValueError(str(exc)) from None
        return self


class SiteSql(_ObsJob):
    """Result: ``{columns, rows, truncated, row_count, result_sets, statements, read_only}``."""

    kind = "site.sql"
    steps = ("query",)
    Params = SiteSqlParams

    def step_query(self) -> None:
        bench = self.bench_for_site()
        cfg = self.site_config(bench)
        if not self.p.read_only:
            self.once("started")
        try:
            data = rs.run_query(_Quiet(self), cfg, self.p.query, read_only=self.p.read_only,
                                max_rows=self.p.max_rows, timeout=self.p.timeout)
        except rs.SqlValidationError as exc:
            self.fail(str(exc))
        self.result["started_done"] = True
        # an SQL error is the user's answer, not a job failure: it is returned in result.error
        self.result.update(data)
        self.log(f"{data['statements']} statement(s); {data['row_count']} row(s)"
                 + (" (truncated)" if data["truncated"] else "")
                 + (f"; error: {data['error']}" if data["error"] else ""))


# --------------------------------------------------------------------------- site.console
class SiteConsoleParams(SiteParams):
    code: str = Field(min_length=1, max_length=rc.MAX_CODE_CHARS)
    commit: bool = False
    timeout: int = Field(rc.DEFAULT_TIMEOUT, ge=1, le=rc.MAX_TIMEOUT)
    # The backend container can read every site_config.json of the bench, so the console is
    # refused on a bench with other sites unless the caller (control plane: all of them belong to
    # the same team) says so.
    allow_shared_bench: bool = False

    @field_validator("code")
    @classmethod
    def _code(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("code contains a NUL byte")
        try:
            compile(value, "<console>", "exec")  # SyntaxError -> 422 before anything runs
        except SyntaxError as exc:
            raise ValueError(f"syntax error: {exc}") from None
        return value


class SiteConsole(_ObsJob):
    """Result: ``{ok, output, truncated, committed, error, duration}``."""

    kind = "site.console"
    steps = ("execute",)
    Params = SiteConsoleParams

    def step_execute(self) -> None:
        bench = self.bench_for_site()
        others = [s for s in self.ctx.benches.sites(bench) if s != self.site]
        if others and not self.p.allow_shared_bench:
            self.fail(f"the console is not available on a bench shared with other sites ({len(others)} other site(s))")
        if not self.ctx.docker.is_running(bench):
            self.fail(f"bench {bench} is not running")
        self.once("started")
        sites_dir = self.ctx.benches.sites_dir(bench)
        console_dir = ensure_dir(sites_dir / rc.CONSOLE_DIR, FRAPPE_UID, FRAPPE_UID, mode=0o700)
        path = console_dir / rc.console_file_name(self.id)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(self.p.code)
            os.chmod(path, 0o600)
            chown(path, FRAPPE_UID, FRAPPE_UID)
            container_path = f"{rc.CONTAINER_SITES}/{rc.CONSOLE_DIR}/{path.name}"
            argv = [self.ctx.docker.bin, "compose", "-p", bench, "--project-directory",
                    str(self.ctx.benches.path(bench)), "exec", "-T", "-w", rc.CONTAINER_SITES, "backend",
                    *rc.bootstrap_args(self.site, container_path, self.p.commit, self.p.timeout)]
            res = self.quiet(argv, cwd=self.ctx.benches.path(bench), timeout=self.p.timeout + 60)
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        self.result["started_done"] = True
        data = rc.parse_console_output(res.stdout)
        if data is None:
            tail = (res.stderr or res.stdout or "").strip()[-2000:]
            self.fail(f"console did not report a result (exit {res.returncode}): {tail}")
        pre = rc.preamble(res.stdout)
        output = (pre + "\n" if pre else "") + (data.get("output") or "")
        self.result.update({
            "ok": bool(data.get("ok")),
            "output": output[-rc.MAX_OUTPUT_CHARS:],
            "truncated": bool(data.get("truncated")) or len(output) > rc.MAX_OUTPUT_CHARS,
            "committed": bool(data.get("committed")),
            "error": data.get("error"),
            "duration": data.get("duration"),
        })
        # an exception in the user's code is reported in result.error (job still succeeds)
        self.log(f"console {'ok' if data.get('ok') else 'raised ' + str(data.get('error'))}"
                 f"{' (committed)' if data.get('committed') else ''} in {data.get('duration')}s")


OBSERVABILITY_JOBS = (SiteLogs, SiteAnalytics, SiteSlowQueries, SiteSql, SiteConsole)
