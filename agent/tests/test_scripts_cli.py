import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from nexaploy_agent import cli
from nexaploy_agent.api import create_app

from .conftest import TOKEN, make_bench

AGENT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("script", ["install/install.sh", "scripts/sync_templates.sh"])
def test_shell_syntax(script):
    subprocess.run(["bash", "-n", str(AGENT / script)], check=True)


def test_systemd_units():
    for name, cmd in (("api", "nexaploy-agent api"), ("worker", "nexaploy-agent worker")):
        text = (AGENT / "install" / f"nexaploy-agent-{name}.service").read_text()
        assert f"ExecStart=/opt/nexaploy/agent/.venv/bin/{cmd}" in text
        assert "NEXAPLOY_CONFIG=/opt/nexaploy/config/agent.toml" in text and "User=root" in text


@pytest.fixture
def cli_against_app(ctx, monkeypatch):
    """Route the CLI's httpx client to the in-process FastAPI app."""
    app_client = TestClient(create_app(ctx))

    def fake_client():
        return httpx.Client(transport=app_client._transport, base_url="http://testserver",
                            headers={"Authorization": f"Bearer {TOKEN}"})

    monkeypatch.setattr(cli, "_client", fake_client)
    return CliRunner()


def test_cli_commands(cli_against_app, ctx, worker, runner):
    r = cli_against_app.invoke(cli.app, ["ping"])
    assert r.exit_code == 0 and '"server_id": "srv-test"' in r.output
    make_bench(ctx, "erp-v15")
    r = cli_against_app.invoke(cli.app, ["bench", "list"])
    assert r.exit_code == 0 and "erp-v15" in r.output
    r = cli_against_app.invoke(cli.app, ["bench", "restart", "erp-v15"])
    job_id = r.output.strip()
    assert r.exit_code == 0 and len(job_id) == 26
    worker.run_once()
    r = cli_against_app.invoke(cli.app, ["jobs", job_id, "--wait"])
    assert r.exit_code == 0 and "restart: success" in r.output and "succe" in r.output
    r = cli_against_app.invoke(cli.app, ["bench", "start", "nope-bench"])
    assert r.exit_code == 1 and "404" in r.output
