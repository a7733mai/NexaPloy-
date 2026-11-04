"""``nexaploy-agent upgrade | rollback | upgrade-watchdog`` (see runtime/upgrade.py).

* ``upgrade``  queue a local ``agent.upgrade`` job (standalone servers; with a control
  plane, use "Upgrade agent" there so it tracks the result)
* ``rollback`` put the previous venv back now and restart the units (manual recovery,
  works even when the new agent cannot start: run it as
  ``/opt/nexaploy/agent/.venv.prev/bin/python -m nexaploy_agent.agent_cli rollback``)
* ``upgrade-watchdog`` run by systemd-run from the previous venv after an upgrade:
  confirms the new version answers on the local API or rolls back
"""

from __future__ import annotations

import subprocess
from typing import Optional

import typer

from .config import load_settings
from .runtime.upgrade import UNITS, AgentInstall, UpgradeError, systemd_available, wait_healthy, watchdog_decision


def _install(config: Optional[str]) -> tuple[AgentInstall, object]:
    settings = load_settings(config)
    return AgentInstall(settings.root / "agent"), settings


def _restart_units() -> bool:
    if not systemd_available():
        typer.echo("systemd not available: restart `nexaploy-agent api` and `nexaploy-agent worker` yourself")
        return False
    res = subprocess.run(["systemctl", "restart", *UNITS], check=False)
    return res.returncode == 0


def upgrade(
    version: str = typer.Option(..., "--version", help="target agent version, e.g. 0.2.0"),
    ref: Optional[str] = typer.Option(None, "--ref", help="git tag/branch (default: v<version>)"),
    repo_url: Optional[str] = typer.Option(None, "--repo-url", help="git URL or local path of the repo"),
    wheel_url: Optional[str] = typer.Option(None, "--wheel-url", help="install this https:// wheel instead of git"),
    wheel_sha256: Optional[str] = typer.Option(None, "--wheel-sha256"),
    restart: bool = typer.Option(True, "--restart/--no-restart"),
    config: Optional[str] = typer.Option(None, "--config", "-c", help="agent.toml path"),
) -> None:
    """Queue an agent.upgrade job on this server (the worker runs it)."""
    from .context import AgentContext

    settings = load_settings(config)
    ctx = AgentContext(settings)
    params = {"version": version, "ref": ref, "repo_url": repo_url, "wheel_url": wheel_url,
              "wheel_sha256": wheel_sha256, "restart": restart}
    try:
        job = ctx.enqueue("agent.upgrade", {k: v for k, v in params.items() if v is not None})
    except Exception as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2)
    typer.echo(f"queued agent.upgrade job {job['id']} (follow it with: nexaploy jobs {job['id']} --wait)")


def rollback(
    restart: bool = typer.Option(True, "--restart/--no-restart", help="restart the agent units afterwards"),
    config: Optional[str] = typer.Option(None, "--config", "-c", help="agent.toml path"),
) -> None:
    """Swap the previous agent venv (.venv.prev) back in and restart the agent."""
    inst, _settings = _install(config)
    try:
        inst.rollback()
    except (OSError, UpgradeError) as exc:
        typer.echo(f"rollback failed: {exc}", err=True)
        raise typer.Exit(1)
    inst.write_state(status="rolled_back", rolled_back_by="cli")
    typer.echo(f"rolled back: {inst.venv} is the previous agent again (failed one kept at {inst.venv_failed})")
    if restart and not _restart_units():
        raise typer.Exit(1)


def upgrade_watchdog(
    expect_version: str = typer.Option(..., "--expect-version"),
    timeout: float = typer.Option(60.0, "--timeout", help="seconds to wait for a healthy agent"),
    config: Optional[str] = typer.Option(None, "--config", "-c", help="agent.toml path"),
) -> None:
    """Confirm the upgraded agent answers with the expected version, else roll back."""
    import httpx

    inst, settings = _install(config)

    def healthy() -> bool:
        for unit in UNITS:
            if subprocess.run(["systemctl", "is-active", "--quiet", unit], check=False).returncode != 0:
                return False
        resp = httpx.get(f"{settings.api_url}/v1/ping", timeout=5,
                         headers={"Authorization": f"Bearer {settings.agent.token}"})
        return resp.status_code == 200 and resp.json().get("version") == expect_version

    state = inst.read_state()
    if watchdog_decision(state, expect_version, True) == "skip":
        typer.echo("watchdog: nothing to do")
        return
    ok = wait_healthy(healthy, timeout=timeout)
    decision = watchdog_decision(state, expect_version, ok)
    if decision == "confirm":
        inst.write_state(status="confirmed")
        typer.echo(f"watchdog: agent {expect_version} is healthy")
        return
    typer.echo(f"watchdog: agent {expect_version} is not healthy; rolling back", err=True)
    try:
        inst.rollback()
    except (OSError, UpgradeError) as exc:
        inst.write_state(status="rollback_failed", error=str(exc))
        typer.echo(f"watchdog: rollback failed: {exc}", err=True)
        raise typer.Exit(1)
    inst.write_state(status="rolled_back", rolled_back_by="watchdog")
    _restart_units()


def register_commands(app: typer.Typer) -> None:
    app.command("upgrade")(upgrade)
    app.command("rollback")(rollback)
    app.command("upgrade-watchdog", hidden=True)(upgrade_watchdog)
