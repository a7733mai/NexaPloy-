"""FastAPI app: the local REST API (§5.2).

Every mutating route validates its params with the job class, runs a cheap
state precheck (404/409) and returns ``202 {"job_id": ...}``; the worker
process executes the job.
"""

from __future__ import annotations

from typing import Any

from fastapi import Body, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError

from . import __version__
from . import validation as v
from .auth import require_token
from .config import Settings, load_settings
from .context import AgentContext
from .jobs.base import Conflict, NotFound
from .jobs.registry import UnknownJobKind
from .jobs.store import JOB_STATUSES, JobNotFound
from .redact import redact_params
from .runtime.server import server_info


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    out = dict(job)
    out["params"] = redact_params(job.get("params") or {})
    return out


def _clean_errors(exc: PydanticValidationError) -> list[dict[str, Any]]:
    # Drop "input"/"ctx" so secrets in rejected bodies are never echoed.
    return [
        {"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")}
        for e in exc.errors()
    ]


def create_app(ctx: AgentContext | None = None, settings: Settings | None = None) -> FastAPI:
    if ctx is None:
        ctx = AgentContext(settings or load_settings())
    app = FastAPI(title="Nexaploy Agent", version=__version__, docs_url=None, redoc_url=None)
    app.state.ctx = ctx

    # ---- error mapping --------------------------------------------------------------
    @app.exception_handler(PydanticValidationError)
    async def _pyd(_: Request, exc: PydanticValidationError):
        return JSONResponse({"detail": _clean_errors(exc)}, status_code=422)

    @app.exception_handler(v.ValidationError)
    async def _val(_: Request, exc: v.ValidationError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(NotFound)
    @app.exception_handler(JobNotFound)
    @app.exception_handler(UnknownJobKind)
    async def _nf(_: Request, exc: Exception):
        msg = exc.args[0] if exc.args else "not found"
        return JSONResponse({"detail": str(msg)}, status_code=404)

    @app.exception_handler(Conflict)
    async def _conflict(_: Request, exc: Conflict):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    auth = [Depends(require_token)]

    def accepted(kind: str, params: dict[str, Any]) -> JSONResponse:
        job = ctx.enqueue(kind, params, origin="local", precheck=True)
        return JSONResponse({"job_id": job["id"], "kind": kind, "status": job["status"]}, status_code=202)

    def body_with(body: dict[str, Any] | None, **path: Any) -> dict[str, Any]:
        merged = dict(body or {})
        merged.update(path)
        return merged

    def require_bench(bench: str) -> str:
        v.bench_name(bench)
        if not ctx.benches.exists(bench):
            raise NotFound(f"bench {bench!r} not found")
        return bench

    def require_site(site: str) -> str:
        v.site_name(site)
        bench = ctx.sites.find_bench(site)
        if not bench:
            raise NotFound(f"site {site!r} not found")
        return bench

    # ---- server / jobs ----------------------------------------------------------------
    @app.get("/v1/ping", dependencies=auth)
    def ping():
        return {"ok": True, "version": __version__, "server_id": ctx.settings.agent.server_id}

    @app.get("/v1/server", dependencies=auth)
    def server():
        return server_info(ctx.docker, ctx.benches, ctx.settings.agent.server_id)

    @app.post("/v1/server/health", dependencies=auth, status_code=202)
    def server_health():
        return accepted("server.health", {})

    @app.post("/v1/server/backup-config", dependencies=auth, status_code=202)
    def configure_backups(body: dict[str, Any] = Body(...)):
        return accepted("server.configure_backups", body)

    @app.get("/v1/jobs", dependencies=auth)
    def list_jobs(status: str | None = Query(None), kind: str | None = Query(None),
                  limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
        if status and status not in JOB_STATUSES:
            raise v.ValidationError(f"invalid status {status!r}")
        return [public_job(j) for j in ctx.store.list(status=status, kind=kind, limit=limit, offset=offset)]

    @app.get("/v1/jobs/{job_id}", dependencies=auth)
    def get_job(job_id: str):
        return public_job(ctx.store.get(job_id))

    @app.post("/v1/jobs/{job_id}/cancel", dependencies=auth)
    def cancel_job(job_id: str):
        job = ctx.store.get(job_id, with_steps=False)
        if not ctx.store.cancel(job_id):
            raise Conflict(f"job {job_id} is {job['status']}; only queued jobs can be cancelled")
        return public_job(ctx.store.get(job_id))

    @app.post("/v1/jobs/{job_id}/retry", dependencies=auth)
    def retry_job(job_id: str):
        job = ctx.store.get(job_id, with_steps=False)
        if job.get("origin") != "local":
            raise Conflict("control plane jobs are retried by the control plane")
        if _contains_mask(job.get("params")):
            raise Conflict("job secrets were scrubbed after it finished; submit a new job")
        if not ctx.store.retry(job_id):
            raise Conflict(f"job {job_id} is {job['status']}; only failed jobs can be retried")
        return public_job(ctx.store.get(job_id))

    # ---- images -----------------------------------------------------------------------
    @app.post("/v1/images/build", dependencies=auth, status_code=202)
    def build_image(body: dict[str, Any] = Body(...)):
        return accepted("image.build", body)

    # ---- benches ----------------------------------------------------------------------
    @app.get("/v1/benches", dependencies=auth)
    def list_benches():
        return ctx.benches.list(with_status=True)

    @app.post("/v1/benches", dependencies=auth, status_code=202)
    def create_bench(body: dict[str, Any] = Body(...)):
        return accepted("bench.create", body)

    @app.get("/v1/benches/{bench}", dependencies=auth)
    def get_bench(bench: str):
        require_bench(bench)
        return ctx.benches.info(bench)

    @app.post("/v1/benches/{bench}/start", dependencies=auth, status_code=202)
    def start_bench(bench: str):
        return accepted("bench.start", {"bench": bench})

    @app.post("/v1/benches/{bench}/stop", dependencies=auth, status_code=202)
    def stop_bench(bench: str):
        return accepted("bench.stop", {"bench": bench})

    @app.post("/v1/benches/{bench}/restart", dependencies=auth, status_code=202)
    def restart_bench(bench: str):
        return accepted("bench.restart", {"bench": bench})

    @app.post("/v1/benches/{bench}/update", dependencies=auth, status_code=202)
    def update_bench(bench: str, body: dict[str, Any] = Body(...)):
        return accepted("bench.update", body_with(body, bench=bench))

    @app.delete("/v1/benches/{bench}", dependencies=auth, status_code=202)
    def remove_bench(bench: str, force: bool = Query(False), body: dict[str, Any] | None = Body(None)):
        params = body_with(body, bench=bench)
        params["force"] = bool(params.get("force") or force)
        return accepted("bench.remove", params)

    @app.get("/v1/benches/{bench}/sites", dependencies=auth)
    def bench_sites(bench: str):
        require_bench(bench)
        return ctx.sites.list(bench)

    @app.post("/v1/benches/{bench}/sites", dependencies=auth, status_code=202)
    def create_site(bench: str, body: dict[str, Any] = Body(...)):
        return accepted("site.create", body_with(body, bench=bench))

    # ---- sites ------------------------------------------------------------------------
    @app.get("/v1/sites", dependencies=auth)
    def list_sites():
        return ctx.sites.list()

    @app.get("/v1/sites/{site}", dependencies=auth)
    def get_site(site: str):
        bench = require_site(site)
        return ctx.sites.info(bench, site)

    @app.post("/v1/sites/{site}/apps", dependencies=auth, status_code=202)
    def install_app(site: str, body: dict[str, Any] = Body(...)):
        return accepted("site.install_app", body_with(body, site=site))

    @app.delete("/v1/sites/{site}/apps/{app}", dependencies=auth, status_code=202)
    def uninstall_app(site: str, app: str):
        return accepted("site.uninstall_app", {"site": site, "app": app})

    @app.post("/v1/sites/{site}/domains", dependencies=auth, status_code=202)
    def add_domain(site: str, body: dict[str, Any] = Body(...)):
        return accepted("site.add_domain", body_with(body, site=site))

    @app.delete("/v1/sites/{site}/domains/{domain}", dependencies=auth, status_code=202)
    def remove_domain(site: str, domain: str):
        return accepted("site.remove_domain", {"site": site, "domain": domain})

    @app.post("/v1/sites/{site}/backup", dependencies=auth, status_code=202)
    def backup_site(site: str, body: dict[str, Any] | None = Body(None)):
        return accepted("site.backup", body_with(body, site=site))

    @app.get("/v1/sites/{site}/backups", dependencies=auth)
    def list_backups(site: str, remote: bool = Query(True)):
        v.site_name(site)
        local = ctx.backups.list_local(site)
        remote_list: list[dict[str, Any]] = []
        if remote:
            benches = {b["bench"] for b in local}
            owner = ctx.sites.find_bench(site)
            if owner:
                benches.add(owner)
            for bench in sorted(benches):
                remote_list += ctx.backups.list_remote(bench, site)
        return {"local": local, "remote": remote_list}

    @app.post("/v1/sites/{site}/backups/prune-remote", dependencies=auth, status_code=202)
    def prune_remote_backups(site: str, body: dict[str, Any] | None = Body(None)):
        params = body_with(body, site=site)
        if not params.get("bench"):
            params["bench"] = require_site(site)
        return accepted("backup.prune_remote", params)

    @app.post("/v1/sites/{site}/restore", dependencies=auth, status_code=202)
    def restore_site(site: str, body: dict[str, Any] = Body(...)):
        return accepted("site.restore", body_with(body, site=site))

    @app.post("/v1/sites/{site}/migrate", dependencies=auth, status_code=202)
    def migrate_site(site: str):
        return accepted("site.migrate", {"site": site})

    @app.post("/v1/sites/{site}/config", dependencies=auth, status_code=202)
    def set_config(site: str, body: dict[str, Any] = Body(...)):
        return accepted("site.set_config", body_with(body, site=site))

    @app.get("/v1/sites/{site}/config", dependencies=auth)
    def get_config(site: str):
        from .runtime.site import redact_site_config

        bench = require_site(site)
        config, redacted = redact_site_config(ctx.sites.site_config(bench, site))
        return {"site": site, "bench": bench, "config": config, "redacted": redacted}

    @app.patch("/v1/sites/{site}/config", dependencies=auth, status_code=202)
    def update_config(site: str, body: dict[str, Any] = Body(...)):
        return accepted("site.update_config", body_with(body, site=site))

    @app.post("/v1/usage", dependencies=auth, status_code=202)
    def collect_usage(body: dict[str, Any] | None = Body(None)):
        return accepted("site.usage", dict(body or {}))

    @app.delete("/v1/sites/{site}", dependencies=auth, status_code=202)
    def drop_site(site: str, skip_backup: bool = Query(False), body: dict[str, Any] | None = Body(None)):
        params = body_with(body, site=site)
        params["skip_backup"] = bool(params.get("skip_backup") or skip_backup)
        return accepted("site.drop", params)

    # ---- lifecycle (§10): staging clone, rename ------------------------------------------
    @app.post("/v1/sites/{site}/clone", dependencies=auth, status_code=202)
    def clone_site(site: str, body: dict[str, Any] = Body(...)):
        params = body_with(body, site=site)
        params.setdefault("bench", require_site(site))
        return accepted("site.clone", params)

    @app.post("/v1/sites/{site}/rename", dependencies=auth, status_code=202)
    def rename_site(site: str, body: dict[str, Any] = Body(...)):
        params = body_with(body, site=site)
        params.setdefault("bench", require_site(site))
        return accepted("site.rename", params)

    # ---- observability (§10): logs, analytics, slow queries, SQL, console ----------------
    @app.get("/v1/sites/{site}/logs", dependencies=auth, status_code=202)
    def site_logs(site: str, log: str = Query("web"), lines: int = Query(200),
                  since: str | None = Query(None)):
        params: dict[str, Any] = {"site": site, "log": log, "lines": lines}
        if since:
            params["since"] = since
        return accepted("site.logs", params)

    @app.get("/v1/sites/{site}/analytics", dependencies=auth, status_code=202)
    def site_analytics(site: str, date_from: str | None = Query(None), date_to: str | None = Query(None)):
        return accepted("site.analytics", {"site": site, "date_from": date_from, "date_to": date_to})

    @app.get("/v1/sites/{site}/slow-queries", dependencies=auth, status_code=202)
    def site_slow_queries(site: str, limit: int = Query(50), min_seconds: float = Query(0.0)):
        return accepted("site.slow_queries", {"site": site, "limit": limit, "min_seconds": min_seconds})

    @app.post("/v1/sites/{site}/sql", dependencies=auth, status_code=202)
    def site_sql(site: str, body: dict[str, Any] = Body(...)):
        return accepted("site.sql", body_with(body, site=site))

    @app.post("/v1/sites/{site}/console", dependencies=auth, status_code=202)
    def site_console(site: str, body: dict[str, Any] = Body(...)):
        return accepted("site.console", body_with(body, site=site))

    # ---- safe update: smoke check of a (test) site -------------------------------------
    @app.post("/v1/sites/{site}/smoke-check", dependencies=auth, status_code=202)
    def site_smoke_check(site: str, body: dict[str, Any] | None = Body(None)):
        return accepted("site.smoke_check", body_with(body or {}, site=site))

    return app


def app_factory() -> FastAPI:  # for `uvicorn --factory nexaploy_agent.api:app_factory`
    return create_app()


def _contains_mask(value) -> bool:
    from .redact import MASK

    if isinstance(value, dict):
        return any(_contains_mask(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_mask(v) for v in value)
    return isinstance(value, str) and MASK in value
