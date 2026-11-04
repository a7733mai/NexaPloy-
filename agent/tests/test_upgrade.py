"""agent.upgrade / agent.rollback jobs, venv swap and the watchdog (runtime/upgrade.py)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from nexaploy_agent import __version__
from nexaploy_agent.jobs import agent_jobs
from nexaploy_agent.jobs.agent_jobs import UpgradeParams
from nexaploy_agent.runtime import upgrade as up
from nexaploy_agent.runtime.docker import Call

from .conftest import run_job

NEW = "9.9.9"


def _script(path: Path, venv: Path, body: str = "print('hi')") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{venv}/bin/python\n{body}\n")
    path.chmod(0o755)


def make_venv(venv: Path, version: str) -> None:
    """A fake venv: bin/python marker plus console scripts with absolute shebangs."""
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    (venv / "bin" / "python").write_text(version)
    for name in ("nexaploy-agent", "nexaploy", "pip"):
        _script(venv / "bin" / name, venv)
    (venv / "bin" / "activate").write_text(f'VIRTUAL_ENV="{venv}"\nexport VIRTUAL_ENV\n')
    (venv / "pyvenv.cfg").write_text(f"home = /usr/bin\ncommand = /usr/bin/python3 -m venv {venv}\n")


def make_source(src: Path) -> None:
    (src / "agent" / "scripts").mkdir(parents=True, exist_ok=True)
    (src / "agent" / "pyproject.toml").write_text("[project]\nname='nexaploy-agent'\n")
    (src / "agent" / "scripts" / "sync_templates.sh").write_text("#!/bin/bash\n")
    (src / "bench-template").mkdir()
    (src / "image-builder").mkdir()
    (src / "image-builder" / "build.sh").write_text("#!/bin/bash\necho new-builder\n")
    (src / "image-builder" / "versions.json").write_text("{}")


@pytest.fixture
def installed(ctx):
    inst = up.AgentInstall(ctx.settings.root / "agent")
    make_venv(inst.venv, __version__)
    (inst.src).mkdir(parents=True)
    (inst.src / "OLD").write_text("old")
    return inst


@pytest.fixture
def scripted(runner, installed, monkeypatch):
    """FakeRunner emulating git fetch, venv creation and the new venv's self-check."""
    monkeypatch.setattr(agent_jobs, "systemd_available", lambda: True)
    inst = installed

    def fetch(call: Call):
        make_source(inst.src_new)

    def venv(call: Call):
        make_venv(Path(call.args[-1]), NEW)

    runner.on(["fetch"], result=fetch)
    runner.on(["-m", "venv"], result=venv)
    runner.on(["rev-parse", "HEAD"], stdout="abc123\n")
    runner.on(lambda a: a[-1] == "version" and a[0].endswith(".venv.new/bin/nexaploy-agent"), stdout=f"{NEW}\n")
    return inst


# --------------------------------------------------------------------------- params
def test_params_defaults_and_validation():
    p = UpgradeParams.model_validate({"version": "v0.2.0"})
    assert p.version == "0.2.0" and p.ref == "v0.2.0" and p.restart and p.watchdog_seconds == 180
    assert UpgradeParams.model_validate({"version": "0.2.0", "wheel_url": "https://x.io/a/n-0.2.0-py3-none-any.whl"}).ref is None
    for bad in (
        {"version": "latest"},
        {"version": "0.2.0", "ref": "--upload-pack=touch /tmp/x"},
        {"version": "0.2.0", "ref": "v1/../../etc"},
        {"version": "0.2.0", "repo_url": "http://insecure.example.com/r.git"},
        {"version": "0.2.0", "repo_url": "ext::sh -c touch% /tmp/pwned"},
        {"version": "0.2.0", "wheel_url": "https://x.io/a.tar.gz"},
        {"version": "0.2.0", "wheel_sha256": "abc"},
        {"version": "0.2.0", "restart_delay_seconds": 0},
    ):
        with pytest.raises(ValidationError):
            UpgradeParams.model_validate(bad)


def test_jobs_registered():
    from nexaploy_agent.jobs.registry import REGISTRY

    assert REGISTRY["agent.upgrade"].steps == ("prepare", "download", "install", "verify", "activate", "restart")
    assert REGISTRY["agent.rollback"].steps == ("rollback", "restart")


# --------------------------------------------------------------------------- relocation
def test_relocate_rewrites_whole_paths_only(tmp_path):
    venv = tmp_path / "agent" / ".venv.new"
    make_venv(venv, NEW)
    (venv / "bin" / "binary").write_bytes(b"\x7fELF\0" + str(venv).encode())
    changed = up.relocate_venv(venv, str(venv), str(tmp_path / "agent" / ".venv"))
    assert {"nexaploy-agent", "nexaploy", "pip", "activate", "pyvenv.cfg"} <= set(changed)
    assert "binary" not in changed
    first = (venv / "bin" / "nexaploy-agent").read_text().splitlines()[0]
    assert first == f"#!{tmp_path}/agent/.venv/bin/python"
    assert os.access(venv / "bin" / "nexaploy-agent", os.X_OK)
    # .venv must not match inside .venv.prev / .venv.new
    other = tmp_path / "x" / ".venv.prev"
    make_venv(other, "1")
    assert up.relocate_venv(other, str(tmp_path / "x" / ".venv"), "/elsewhere") == []


def test_real_venv_survives_relocation_and_swap(tmp_path):
    """A real venv built at .venv.new runs its console script after the swap to .venv."""
    home = tmp_path / "agent"
    inst = up.AgentInstall(home)
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(inst.venv_new)], check=True)
    _script(inst.venv_new / "bin" / "tool", inst.venv_new, "import sys; print(sys.prefix)")
    inst.activate()
    assert not inst.venv_new.exists()
    out = subprocess.run([str(inst.venv / "bin" / "tool")], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == str(inst.venv)


# --------------------------------------------------------------------------- swaps
def test_activate_and_rollback(installed):
    inst = installed
    make_venv(inst.venv_new, NEW)
    inst.src_new.mkdir()
    inst.activate()
    assert (inst.venv / "bin" / "python").read_text() == NEW
    assert (inst.venv_prev / "bin" / "python").read_text() == __version__
    assert (inst.src_prev / "OLD").exists()
    # old venv keeps shebangs pointing at .venv: right again after the rollback
    assert str(inst.venv) + "/bin/python" in (inst.venv_prev / "bin" / "nexaploy").read_text()
    inst.rollback()
    assert (inst.venv / "bin" / "python").read_text() == __version__
    assert (inst.venv_failed / "bin" / "python").read_text() == NEW
    assert (inst.src / "OLD").exists()
    with pytest.raises(up.UpgradeError):
        inst.rollback()  # nothing left to roll back to


def test_swap_undone_when_second_rename_fails(installed, monkeypatch):
    inst = installed
    real = os.rename
    calls = []

    def flaky(a, b):
        calls.append((a, b))
        if len(calls) == 2:
            raise OSError("boom")
        return real(a, b)

    make_venv(inst.venv_new, NEW)
    monkeypatch.setattr(up.os, "rename", flaky)
    with pytest.raises(OSError):
        inst.activate()
    assert (inst.venv / "bin" / "python").read_text() == __version__


# --------------------------------------------------------------------------- jobs
def test_upgrade_job_success(ctx, worker, runner, scripted):
    inst = scripted
    job = run_job(ctx, worker, "agent.upgrade", {"version": NEW, "repo_url": "https://github.com/acme/nx.git"})
    assert job["status"] == "success", job["error"]
    cmds = runner.cmdlines()
    fetch = next(c for c in runner.calls if "fetch" in c.args)
    assert fetch.args[-3:] == ["--", "https://github.com/acme/nx.git", f"v{NEW}"]
    assert any("sync_templates.sh" in c and "bench-template" in c for c in cmds)
    assert any(c.endswith(f".venv/bin/python -m venv {inst.venv_new}") for c in cmds)
    # swap done; previous venv + source kept
    assert (inst.venv / "bin" / "python").read_text() == NEW
    assert (inst.venv_prev / "bin" / "python").read_text() == __version__
    assert f"#!{inst.venv}/bin/python" in (inst.venv / "bin" / "nexaploy-agent").read_text()
    assert (inst.src / "agent" / "pyproject.toml").exists() and (inst.src_prev / "OLD").exists()
    # image-builder refreshed, benches untouched
    assert "new-builder" in (ctx.settings.images_dir / "build.sh").read_text()
    assert not any("compose" in c for c in cmds)
    # restart + watchdog scheduled through systemd-run, restart after the delay
    restart = [c for c in runner.calls if c.args[0] == "systemd-run" and "restart" in c.args]
    assert restart and "--on-active=10s" in restart[0].args
    assert restart[0].args[-2:] == ["nexaploy-agent-api.service", "nexaploy-agent-worker.service"]
    watchdog = [c for c in runner.calls if "upgrade-watchdog" in c.args]
    assert watchdog and watchdog[0].args[watchdog[0].args.index("--expect-version") + 1] == NEW
    assert any(a.endswith(".venv.prev/bin/python") for a in watchdog[0].args)
    state = inst.read_state()
    assert state["status"] == "activated" and state["to_version"] == NEW and state["from_version"] == __version__
    assert job["result"]["commit"] == "abc123" and job["result"]["restart_scheduled"] is True


def test_upgrade_self_check_failure_keeps_running_venv(ctx, worker, runner, scripted):
    inst = scripted
    runner.on(lambda a: a[-1] == "version" and a[0].endswith(".venv.new/bin/nexaploy-agent"), stdout="0.0.1\n")
    job = run_job(ctx, worker, "agent.upgrade", {"version": NEW})
    assert job["status"] == "failure" and "self-check" in job["error"]
    assert (inst.venv / "bin" / "python").read_text() == __version__
    assert not inst.venv_prev.exists()
    assert not runner.called("systemd-run")


def test_upgrade_resumes_after_activation(ctx, worker, runner, scripted):
    """A retried job whose swap already happened does not swap twice."""
    inst = scripted
    job = run_job(ctx, worker, "agent.upgrade", {"version": NEW, "restart": False})
    assert job["status"] == "success"
    record = ctx.store.get(job["id"])
    for step in record["steps"]:
        if step["name"] not in ("download", "install", "verify", "activate"):
            continue
        rerun = agent_jobs.AgentUpgrade(ctx, {**record, "params": {"version": NEW, "restart": False}})
        rerun.run_step(step["idx"], step["name"])
        out = next(s["output"] for s in ctx.store.get(job["id"])["steps"] if s["idx"] == step["idx"])
        assert "already" in out, step["name"]
    assert (inst.venv / "bin" / "python").read_text() == NEW
    assert (inst.venv_prev / "bin" / "python").read_text() == __version__


def test_upgrade_without_systemd(ctx, worker, runner, scripted, monkeypatch):
    monkeypatch.setattr(agent_jobs, "systemd_available", lambda: False)
    job = run_job(ctx, worker, "agent.upgrade", {"version": NEW})
    assert job["status"] == "success"
    assert job["result"]["restart_scheduled"] is False
    assert not runner.called("systemd-run")


def test_upgrade_from_wheel_checks_sha256(ctx, worker, runner, scripted):
    def download(call: Call):
        Path(call.args[call.args.index("-o") + 1]).write_bytes(b"wheel")

    runner.on(["curl"], result=download)
    url = "https://releases.example.com/nexaploy_agent-9.9.9-py3-none-any.whl"
    job = run_job(ctx, worker, "agent.upgrade", {"version": NEW, "wheel_url": url, "wheel_sha256": "0" * 64})
    assert job["status"] == "failure" and "checksum mismatch" in job["error"]
    import hashlib

    good = hashlib.sha256(b"wheel").hexdigest()
    job = run_job(ctx, worker, "agent.upgrade", {"version": NEW, "wheel_url": url, "wheel_sha256": good})
    assert job["status"] == "success", job["error"]
    assert not runner.called("git")
    pip = [c for c in runner.calls if c.args[0].endswith("/pip") and c.args[-1].endswith(".whl")]
    assert pip


def test_upgrade_requires_installed_venv(ctx, worker):
    job = run_job(ctx, worker, "agent.upgrade", {"version": NEW})
    assert job["status"] == "failure" and "not installed by install.sh" in job["error"]


def test_rollback_job(ctx, worker, runner, installed, monkeypatch):
    monkeypatch.setattr(agent_jobs, "systemd_available", lambda: True)
    inst = installed
    make_venv(inst.venv_new, NEW)
    inst.activate()
    job = run_job(ctx, worker, "agent.rollback", {})
    assert job["status"] == "success", job["error"]
    assert (inst.venv / "bin" / "python").read_text() == __version__
    assert inst.read_state()["status"] == "rolled_back"
    assert runner.called("systemd-run")
    job = run_job(ctx, worker, "agent.rollback", {})
    assert job["status"] == "failure" and "nothing to roll back" in job["error"]


# --------------------------------------------------------------------------- watchdog
def test_watchdog_decision():
    st = {"to_version": NEW, "status": "activated"}
    assert up.watchdog_decision(st, NEW, True) == "confirm"
    assert up.watchdog_decision(st, NEW, False) == "rollback"
    assert up.watchdog_decision({**st, "status": "confirmed"}, NEW, False) == "skip"
    assert up.watchdog_decision({**st, "to_version": "1.0.0"}, NEW, False) == "skip"
    assert up.watchdog_decision({}, NEW, False) == "skip"


def test_wait_healthy():
    t = [0.0]
    answers = iter([False, RuntimeError("down"), True])

    def check():
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a

    assert up.wait_healthy(check, timeout=60, interval=5, sleep=lambda s: t.__setitem__(0, t[0] + s),
                           clock=lambda: t[0])
    assert not up.wait_healthy(lambda: False, timeout=10, interval=5,
                               sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])


def test_cli_rollback(ctx, installed, monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from nexaploy_agent import agent_cli
    from nexaploy_agent.config import update_config_file

    cfg = tmp_path / "agent.toml"
    update_config_file(cfg, {"agent": {"root": str(ctx.settings.root), "token": "t"}})
    inst = installed
    make_venv(inst.venv_new, NEW)
    inst.activate()
    res = CliRunner().invoke(agent_cli.app, ["rollback", "--no-restart", "--config", str(cfg)])
    assert res.exit_code == 0, res.output
    assert (inst.venv / "bin" / "python").read_text() == __version__
    assert json.loads(inst.state_path.read_text())["rolled_back_by"] == "cli"


def test_restart_command_shape():
    cmd = up.restart_command("01HXYZ", 15)
    assert cmd[:3] == ["systemd-run", "--quiet", "--collect"]
    assert "--unit=nexaploy-agent-restart-01hxyz" in cmd and "--on-active=15s" in cmd
    assert cmd[-3:] == ["restart", "nexaploy-agent-api.service", "nexaploy-agent-worker.service"]
