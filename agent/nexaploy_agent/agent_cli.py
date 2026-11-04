"""``nexaploy-agent`` entry point: ``api``, ``worker``, ``register``, ``version``."""

from __future__ import annotations

import logging
import signal
import socket
import sys
import threading
from typing import Optional

import typer

from . import __version__
from .config import config_path, load_settings, update_config_file

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Nexaploy agent daemons.")


def _logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@app.command()
def api(
    config: Optional[str] = typer.Option(None, "--config", "-c", help="agent.toml path"),
    host: Optional[str] = typer.Option(None, help="override agent.bind"),
    port: Optional[int] = typer.Option(None, help="override agent.port"),
    log_level: str = typer.Option("info"),
) -> None:
    """Run the REST API (uvicorn)."""
    import uvicorn

    from .api import create_app

    _logging(log_level)
    settings = load_settings(config)
    if not settings.agent.token:
        typer.echo("warning: agent.token is empty; every API request will be rejected", err=True)
    uvicorn.run(
        create_app(settings=settings),
        host=host or settings.agent.bind,
        port=port or settings.agent.port,
        log_level=log_level,
        proxy_headers=False,
    )


@app.command()
def worker(
    config: Optional[str] = typer.Option(None, "--config", "-c", help="agent.toml path"),
    log_level: str = typer.Option("info"),
) -> None:
    """Run the job worker (+ control-plane heartbeat/poll threads)."""
    from .context import AgentContext
    from .control_plane import ControlPlaneService
    from .jobs.worker import Worker

    _logging(log_level)
    settings = load_settings(config)
    ctx = AgentContext(settings)
    stop = threading.Event()

    def _stop(signum, _frame):
        logging.getLogger("nexaploy").info("signal %s: finishing current step and stopping", signum)
        stop.set()
        ctx.wake.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    cp = ControlPlaneService(ctx)
    w = Worker(ctx, on_change=cp.notify)
    cp.start(stop)
    w.run_forever(stop)
    cp.join()


@app.command()
def register(
    control_plane: str = typer.Option(..., "--control-plane", help="control plane base URL"),
    token: str = typer.Option(..., "--token", help="registration token"),
    config: Optional[str] = typer.Option(None, "--config", "-c", help="agent.toml path"),
    site_name: Optional[str] = typer.Option(
        None, "--site-name", help="Frappe site of the control plane when the URL is not its host "
        "(all-in-one installs: http://127.0.0.1:<port>)"),
) -> None:
    """Register this server (BYOS) and store server_id + server_token in agent.toml."""
    from .control_plane import ControlPlaneClient, ControlPlaneError
    from .runtime.docker import Docker, SubprocessRunner
    from .runtime.server import host_facts

    settings = load_settings(config)
    settings.control_plane.url = control_plane
    settings.control_plane.token = ""
    if site_name is not None:
        settings.control_plane.site_name = site_name
    client = ControlPlaneClient(settings)
    facts = host_facts(Docker(SubprocessRunner(), settings.benches_dir), settings.root)
    try:
        creds = client.register(token, socket.getfqdn(), facts)
    except ControlPlaneError as exc:
        typer.echo(f"registration failed: {exc}", err=True)
        raise typer.Exit(1)
    path = config_path(config)
    update_config_file(path, {
        "agent": {"server_id": creds["server_id"]},
        "control_plane": {"url": control_plane, "token": creds["server_token"],
                          **({"site_name": site_name} if site_name is not None else {})},
        "backup": {"prefix": settings.backup.prefix or creds["server_id"]},
    })
    typer.echo(f"registered as {creds['server_id']} (credentials saved to {path})")


@app.command()
def version() -> None:
    """Print the agent version."""
    typer.echo(__version__)


def main() -> None:
    app(prog_name="nexaploy-agent")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
