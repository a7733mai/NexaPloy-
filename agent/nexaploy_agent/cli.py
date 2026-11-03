"""``nexaploy`` CLI: a thin client for the local agent API.

URL and token come from agent.toml (``NEXAPLOY_CONFIG``), overridable with
``--url``/``--token`` or ``NEXAPLOY_URL``/``NEXAPLOY_TOKEN``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, List, Optional

import httpx
import typer

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Nexaploy server CLI.")
bench_app = typer.Typer(no_args_is_help=True, help="Manage benches.")
site_app = typer.Typer(no_args_is_help=True, help="Manage sites.")
app.add_typer(bench_app, name="bench")
app.add_typer(site_app, name="site")

FINAL = {"success", "failure", "cancelled"}


class State:
    url: Optional[str] = None
    token: Optional[str] = None
    config: Optional[str] = None
    as_json: bool = False


state = State()


@app.callback()
def _main(
    url: Optional[str] = typer.Option(None, "--url", envvar="NEXAPLOY_URL", help="agent API base URL"),
    token: Optional[str] = typer.Option(None, "--token", envvar="NEXAPLOY_TOKEN", help="agent API token"),
    config: Optional[str] = typer.Option(None, "--config", "-c", help="agent.toml path"),
    as_json: bool = typer.Option(False, "--json", help="print raw JSON"),
) -> None:
    state.url, state.token, state.config, state.as_json = url, token, config, as_json


# ---- HTTP helpers ------------------------------------------------------------------------
def _client() -> httpx.Client:
    url, token = state.url, state.token
    if not url or not token:
        from .config import load_settings

        try:
            settings = load_settings(state.config)
        except Exception as exc:
            _die(f"cannot load config: {exc}")
        url = url or settings.api_url
        token = token or settings.agent.token
    if not token:
        _die("no API token (set agent.token in agent.toml, NEXAPLOY_TOKEN or --token)")
    return httpx.Client(base_url=url.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, timeout=60)


def _die(msg: str, code: int = 1):
    typer.secho(msg, fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


def call(method: str, path: str, body: Any = None, params: dict | None = None) -> Any:
    try:
        with _client() as c:
            resp = c.request(method, f"/v1{path}", json=body, params=params)
    except httpx.HTTPError as exc:
        _die(f"cannot reach agent API: {exc}")
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail")
        except ValueError:
            detail = resp.text
        _die(f"HTTP {resp.status_code}: {detail if isinstance(detail, str) else json.dumps(detail)}")
    return resp.json() if resp.content else None


def show(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


def table(rows: list[dict[str, Any]], cols: list[str]) -> None:
    if state.as_json:
        show(rows)
        return
    if not rows:
        typer.echo("(none)")
        return
    cells = [[_fmt(r.get(c)) for c in cols] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(cols)]
    typer.echo("  ".join(c.upper().ljust(w) for c, w in zip(cols, widths)))
    for row in cells:
        typer.echo("  ".join(val.ljust(w) for val, w in zip(row, widths)))


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, list):
        return ",".join(str(v) for v in value) or "-"
    return str(value)


def follow(job_id: str, interval: float = 2.0) -> dict[str, Any]:
    """Poll a job until it is final, printing step output as it grows."""
    seen: dict[int, int] = {}
    status: dict[int, str] = {}
    while True:
        job = call("GET", f"/jobs/{job_id}")
        for step in job.get("steps", []):
            idx, st = step["idx"], step["status"]
            changed = status.get(idx) != st and st != "pending"
            if changed and idx not in status and st != "running":
                typer.secho(f"==> [{idx}] {step['name']}", bold=True)
            elif changed and st == "running":
                typer.secho(f"==> [{idx}] {step['name']}: running", bold=True)
            out = step.get("output") or ""
            if len(out) > seen.get(idx, 0):
                sys.stdout.write(out[seen.get(idx, 0):])
                sys.stdout.flush()
                seen[idx] = len(out)
            if changed and st != "running":
                color = {"success": typer.colors.GREEN, "failure": typer.colors.RED}.get(st)
                typer.secho(f"    [{idx}] {step['name']}: {st}", fg=color, bold=True)
            if changed:
                status[idx] = st
        if job["status"] in FINAL:
            color = typer.colors.GREEN if job["status"] == "success" else typer.colors.RED
            typer.secho(f"job {job_id} {job['status']}" + (f": {job['error']}" if job.get("error") else ""),
                        fg=color, bold=True)
            return job
        time.sleep(interval)


def submit(method: str, path: str, body: Any = None, wait: bool = False, params: dict | None = None) -> None:
    res = call(method, path, body, params)
    job_id = res["job_id"]
    if not wait:
        typer.echo(job_id)
        return
    typer.echo(f"job {job_id} queued")
    job = follow(job_id)
    if job["status"] != "success":
        raise typer.Exit(1)


WaitOpt = typer.Option(False, "--wait", "-w", help="follow the job until it finishes")


def _parse_env(pairs: List[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            _die(f"--env expects KEY=VALUE, got {pair!r}")
        k, _, val = pair.partition("=")
        env[k] = val
    return env


def _read_json_arg(value: str) -> Any:
    if value.startswith("@"):
        with open(value[1:]) as fh:
            return json.load(fh)
    if os.path.exists(value):
        with open(value) as fh:
            return json.load(fh)
    return json.loads(value)


# ---- top level -------------------------------------------------------------------------------
@app.command()
def ping() -> None:
    """Check the agent API."""
    show(call("GET", "/ping"))


@app.command()
def server() -> None:
    """Host facts."""
    show(call("GET", "/server"))


@app.command()
def jobs(
    job_id: Optional[str] = typer.Argument(None),
    status: Optional[str] = typer.Option(None),
    kind: Optional[str] = typer.Option(None),
    limit: int = typer.Option(20),
    wait: bool = WaitOpt,
) -> None:
    """List jobs, or show one job (with --wait to follow it)."""
    if job_id:
        if wait:
            job = follow(job_id)
            raise typer.Exit(0 if job["status"] == "success" else 1)
        job = call("GET", f"/jobs/{job_id}")
        if state.as_json:
            show(job)
            return
        typer.echo(f"{job['id']}  {job['kind']}  {job['status']}  origin={job['origin']}")
        typer.echo(f"params: {json.dumps(job['params'])}")
        if job.get("error"):
            typer.secho(f"error: {job['error']}", fg=typer.colors.RED)
        for step in job.get("steps", []):
            typer.secho(f"==> [{step['idx']}] {step['name']}: {step['status']}", bold=True)
            if step.get("output"):
                typer.echo(step["output"].rstrip())
        return
    params = {"limit": limit}
    if status:
        params["status"] = status
    if kind:
        params["kind"] = kind
    table(call("GET", "/jobs", params=params), ["id", "kind", "status", "origin", "created_at", "error"])


@app.command()
def cancel(job_id: str) -> None:
    """Cancel a queued job."""
    show(call("POST", f"/jobs/{job_id}/cancel"))


@app.command()
def build(
    tag: str = typer.Option(..., help="image tag, e.g. nexaploy/bench:erp-v15-main-20261103T2130"),
    major: int = typer.Option(..., help="Frappe major (14/15/16)"),
    apps_json: str = typer.Option(..., "--apps-json", help="apps.json path or inline JSON"),
    frappe_branch: Optional[str] = typer.Option(None),
    push: bool = typer.Option(False),
    wait: bool = WaitOpt,
) -> None:
    """Build a bench image (image.build)."""
    body = {"tag": tag, "major": major, "apps_json": _read_json_arg(apps_json), "push": push}
    if frappe_branch:
        body["frappe_branch"] = frappe_branch
    submit("POST", "/images/build", body, wait)


# ---- bench ---------------------------------------------------------------------------------------
@bench_app.command("list")
def bench_list() -> None:
    table(call("GET", "/benches"), ["name", "image", "status", "sites"])


@bench_app.command("create")
def bench_create(
    name: str,
    image: str = typer.Option(...),
    env: List[str] = typer.Option([], "--env", "-e", help="KEY=VALUE .env override"),
    apps_json: Optional[str] = typer.Option(None, "--apps-json"),
    wait: bool = WaitOpt,
) -> None:
    body: dict[str, Any] = {"name": name, "image": image, "env": _parse_env(env)}
    if apps_json:
        body["apps_json"] = _read_json_arg(apps_json)
    submit("POST", "/benches", body, wait)


@bench_app.command("update")
def bench_update(name: str, image: str = typer.Option(...), wait: bool = WaitOpt) -> None:
    submit("POST", f"/benches/{name}/update", {"image": image}, wait)


@bench_app.command("start")
def bench_start(name: str, wait: bool = WaitOpt) -> None:
    submit("POST", f"/benches/{name}/start", None, wait)


@bench_app.command("stop")
def bench_stop(name: str, wait: bool = WaitOpt) -> None:
    submit("POST", f"/benches/{name}/stop", None, wait)


@bench_app.command("restart")
def bench_restart(name: str, wait: bool = WaitOpt) -> None:
    submit("POST", f"/benches/{name}/restart", None, wait)


@bench_app.command("remove")
def bench_remove(name: str, force: bool = typer.Option(False), wait: bool = WaitOpt) -> None:
    submit("DELETE", f"/benches/{name}", None, wait, params={"force": str(force).lower()})


# ---- site ----------------------------------------------------------------------------------------
@site_app.command("list")
def site_list(bench: Optional[str] = typer.Option(None)) -> None:
    rows = call("GET", f"/benches/{bench}/sites") if bench else call("GET", "/sites")
    table(rows, ["name", "bench", "domains"])


@site_app.command("create")
def site_create(
    name: str,
    bench: str = typer.Option(...),
    admin_password: str = typer.Option(..., prompt=True, hide_input=True, confirmation_prompt=True,
                                       envvar="NEXAPLOY_ADMIN_PASSWORD"),
    app: List[str] = typer.Option([], "--app", "-a", help="app to install (repeatable)"),
    wait: bool = WaitOpt,
) -> None:
    submit("POST", f"/benches/{bench}/sites",
           {"name": name, "admin_password": admin_password, "apps": app}, wait)


@site_app.command("install-app")
def site_install_app(site: str, app: str, wait: bool = WaitOpt) -> None:
    submit("POST", f"/sites/{site}/apps", {"app": app}, wait)


@site_app.command("uninstall-app")
def site_uninstall_app(site: str, app: str, wait: bool = WaitOpt) -> None:
    submit("DELETE", f"/sites/{site}/apps/{app}", None, wait)


@site_app.command("add-domain")
def site_add_domain(site: str, domain: str, wait: bool = WaitOpt) -> None:
    submit("POST", f"/sites/{site}/domains", {"domain": domain}, wait)


@site_app.command("remove-domain")
def site_remove_domain(site: str, domain: str, wait: bool = WaitOpt) -> None:
    submit("DELETE", f"/sites/{site}/domains/{domain}", None, wait)


@site_app.command("backup")
def site_backup(
    site: str,
    with_files: bool = typer.Option(True, "--with-files/--no-files"),
    upload: bool = typer.Option(True, "--upload/--no-upload"),
    wait: bool = WaitOpt,
) -> None:
    submit("POST", f"/sites/{site}/backup", {"with_files": with_files, "upload": upload}, wait)


@site_app.command("backups")
def site_backups(site: str) -> None:
    data = call("GET", f"/sites/{site}/backups")
    if state.as_json:
        show(data)
        return
    typer.secho("local:", bold=True)
    table(data["local"], ["id", "bench", "size", "created_at"])
    typer.secho("remote:", bold=True)
    table(data["remote"], ["id", "bench", "remote_path"])


@site_app.command("restore")
def site_restore(
    site: str,
    backup_id: Optional[str] = typer.Option(None, "--backup-id"),
    remote_path: Optional[str] = typer.Option(None, "--remote-path"),
    bench: Optional[str] = typer.Option(None, help="target bench when the site does not exist yet"),
    wait: bool = WaitOpt,
) -> None:
    if bool(backup_id) == bool(remote_path):
        _die("pass exactly one of --backup-id or --remote-path")
    body: dict[str, Any] = {"backup_id": backup_id} if backup_id else {"remote_path": remote_path}
    if bench:
        body["bench"] = bench
    submit("POST", f"/sites/{site}/restore", body, wait)


@site_app.command("migrate")
def site_migrate(site: str, wait: bool = WaitOpt) -> None:
    submit("POST", f"/sites/{site}/migrate", None, wait)


@site_app.command("set-config")
def site_set_config(site: str, key: str, value: str,
                    parse: bool = typer.Option(False, "--json", "-p", help="parse value as JSON"),
                    wait: bool = WaitOpt) -> None:
    submit("POST", f"/sites/{site}/config", {"key": key, "value": json.loads(value) if parse else value}, wait)


@site_app.command("drop")
def site_drop(site: str, skip_backup: bool = typer.Option(False),
              yes: bool = typer.Option(False, "--yes", "-y"), wait: bool = WaitOpt) -> None:
    if not yes:
        typer.confirm(f"Drop site {site}?", abort=True)
    submit("DELETE", f"/sites/{site}", None, wait, params={"skip_backup": str(skip_backup).lower()})


def main() -> None:
    app(prog_name="nexaploy")


if __name__ == "__main__":  # pragma: no cover
    main()
