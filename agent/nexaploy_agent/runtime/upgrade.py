"""Agent self-upgrade and rollback (jobs ``agent.upgrade`` / ``agent.rollback``).

Layout under ``<root>/agent`` (install.sh creates ``src`` and ``.venv``)::

    src/            agent source the running venv was built from (git checkout or copy)
    src.new/        source being installed            (download step)
    src.prev/       source of the previous version    (kept for rollback)
    .venv/          the running venv  (systemd: .venv/bin/nexaploy-agent api|worker)
    .venv.new/      venv being built                  (install + verify steps)
    .venv.prev/     previous venv                     (kept for rollback)
    .venv.failed/   venv that was rolled back         (removed by the next upgrade)
    data/upgrade.json   state of the last upgrade (for the watchdog and ``rollback``)

Virtualenvs are not relocatable: console scripts carry an absolute shebang. The new
venv is built at ``.venv.new`` and its scripts are rewritten to ``.venv`` right before
the swap. A venv renamed to ``.venv.prev`` keeps shebangs that point at ``.venv``, which
is exactly where it goes back on rollback, so it never needs rewriting. Invoking
``<venv>/bin/python`` directly works from any location (``pyvenv.cfg`` is found next
to the executable path), which is how the watchdog runs from ``.venv.prev``.

Benches, sites, the proxy and the shared DB are never touched: only the agent's own
directories, the packaged templates and the image-builder files change. Infra stack
changes (proxy/db compose files) still need ``install.sh``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# imported eagerly: the watchdog renames the venv it runs from before writing the state
from ..fsutil import atomic_write

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
# git branch / tag / commit; no "..", no leading "-" (never parsed as an option)
REF_RE = re.compile(r"^(?!.*\.\.)(?!-)[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
REPO_URL_RE = re.compile(r"^(https://[A-Za-z0-9.-]+(:\d+)?/[A-Za-z0-9._~/-]+|/[A-Za-z0-9._~/-]+)$")
WHEEL_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:\d+)?/[A-Za-z0-9._~/%+-]+\.whl$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

DEFAULT_REPO = "https://github.com/a7733mai/NexaPloy-.git"
UNITS = ("nexaploy-agent-api.service", "nexaploy-agent-worker.service")
STATE_FILE = "upgrade.json"


class UpgradeError(RuntimeError):
    pass


def valid_version(value: str) -> str:
    if not SEMVER_RE.match(value or ""):
        raise ValueError(f"invalid version {value!r} (expected semver, e.g. 0.2.0)")
    return value


def valid_ref(value: str) -> str:
    if not REF_RE.match(value or ""):
        raise ValueError(f"invalid git ref {value!r}")
    return value


def valid_repo_url(value: str) -> str:
    if not REPO_URL_RE.match(value or "") or ".." in value:
        raise ValueError("repo_url must be an https:// git URL or an absolute local path")
    return value


def valid_wheel_url(value: str) -> str:
    if not WHEEL_URL_RE.match(value or "") or ".." in value:
        raise ValueError("wheel_url must be an https:// URL of a .whl file")
    return value


# --------------------------------------------------------------------------- shebangs
def relocate_venv(venv: Path, old: str, new: str) -> list[str]:
    """Rewrite absolute references to ``old`` (a venv path) as ``new`` in ``venv/bin``
    scripts (shebangs, activate*) and ``pyvenv.cfg``. Returns the changed file names.

    Only whole path components match: ``/x/.venv`` is not rewritten inside ``/x/.venv.new``.
    """
    pattern = re.compile(re.escape(old.rstrip("/")).encode() + rb"(?=[/\s'\"]|$)", re.M)
    replacement = new.rstrip("/").encode()
    changed: list[str] = []
    candidates: list[Path] = []
    bin_dir = venv / "bin"
    if bin_dir.is_dir():
        candidates += sorted(p for p in bin_dir.iterdir() if p.is_file() and not p.is_symlink())
    cfg = venv / "pyvenv.cfg"
    if cfg.is_file():
        candidates.append(cfg)
    for path in candidates:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        is_script = data.startswith(b"#!") or path.name.startswith("activate") or path.name == "pyvenv.cfg"
        if not is_script or b"\0" in data[:1024]:
            continue
        new_data = pattern.sub(lambda _m: replacement, data)
        if new_data != data:
            mode = path.stat().st_mode & 0o7777
            tmp = path.with_name(f".{path.name}.relocate")
            tmp.write_bytes(new_data)
            os.chmod(tmp, mode)
            os.replace(tmp, path)
            changed.append(path.name)
    return changed


# --------------------------------------------------------------------------- layout
@dataclass
class AgentInstall:
    """Paths of the agent installation under ``<root>/agent``."""

    home: Path

    @property
    def venv(self) -> Path:
        return self.home / ".venv"

    @property
    def venv_new(self) -> Path:
        return self.home / ".venv.new"

    @property
    def venv_prev(self) -> Path:
        return self.home / ".venv.prev"

    @property
    def venv_failed(self) -> Path:
        return self.home / ".venv.failed"

    @property
    def src(self) -> Path:
        return self.home / "src"

    @property
    def src_new(self) -> Path:
        return self.home / "src.new"

    @property
    def src_prev(self) -> Path:
        return self.home / "src.prev"

    @property
    def src_failed(self) -> Path:
        return self.home / "src.failed"

    @property
    def dist_new(self) -> Path:
        return self.home / "dist.new"

    @property
    def state_path(self) -> Path:
        return self.home / "data" / STATE_FILE

    # ---- helpers ------------------------------------------------------------------------
    @staticmethod
    def bin(venv: Path, name: str) -> Path:
        return venv / "bin" / name

    def package_dir(self, src: Path) -> Path | None:
        """The agent package directory inside a source tree (repo root or agent/)."""
        if (src / "agent" / "pyproject.toml").is_file():
            return src / "agent"
        if (src / "pyproject.toml").is_file():
            return src
        return None

    def repo_dir(self, src: Path) -> Path | None:
        pkg = self.package_dir(src)
        if pkg is None:
            return None
        return pkg.parent if pkg.name == "agent" and pkg != src else None

    # ---- state --------------------------------------------------------------------------
    def read_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def write_state(self, **values: Any) -> dict[str, Any]:
        state = self.read_state()
        state.update(values)
        state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        atomic_write(self.state_path, json.dumps(state, indent=2, sort_keys=True) + "\n", mode=0o600)
        return state

    # ---- swaps --------------------------------------------------------------------------
    def clean_leftovers(self) -> None:
        """Remove what a previous (finished) upgrade left behind: never the running venv."""
        for path in (self.venv_failed, self.src_failed, self.venv_new, self.src_new, self.dist_new):
            if path.exists() or path.is_symlink():
                _rm(path)

    def activate(self) -> None:
        """``.venv.new`` -> ``.venv`` (old one kept as ``.venv.prev``); same for ``src``.

        The new venv's scripts are rewritten to their final path first. The two renames
        per pair are not one atomic operation, but the window is microseconds and a
        failure in between is undone.
        """
        if not self.venv_new.is_dir():
            raise UpgradeError(f"{self.venv_new} does not exist")
        relocate_venv(self.venv_new, str(self.venv_new), str(self.venv))
        _swap_in(self.venv_new, self.venv, self.venv_prev)
        if self.src_new.is_dir():
            _swap_in(self.src_new, self.src, self.src_prev)

    def rollback(self) -> dict[str, Any]:
        """Put ``.venv.prev`` (and ``src.prev``) back; the current ones become ``*.failed``.

        The running process may live in ``.venv``: it is renamed, never deleted.
        """
        if not self.bin(self.venv_prev, "python").exists():
            raise UpgradeError(f"nothing to roll back to: {self.venv_prev} does not exist")
        for path in (self.venv_failed, self.src_failed):
            if path.exists():
                _rm(path)
        _swap_in(self.venv_prev, self.venv, self.venv_failed)
        if self.src_prev.is_dir():
            _swap_in(self.src_prev, self.src, self.src_failed)
        return {"venv": str(self.venv), "failed": str(self.venv_failed)}


def _rm(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _swap_in(new: Path, current: Path, keep_as: Path) -> None:
    """current -> keep_as (replacing it), new -> current; undo the first rename on error."""
    if keep_as.exists() or keep_as.is_symlink():
        _rm(keep_as)
    moved = False
    if current.exists() or current.is_symlink():
        os.rename(current, keep_as)
        moved = True
    try:
        os.rename(new, current)
    except OSError:
        if moved:
            os.rename(keep_as, current)
        raise


# --------------------------------------------------------------------------- systemd
def systemd_available(run_dir: Path = Path("/run/systemd/system")) -> bool:
    return run_dir.is_dir() and shutil.which("systemd-run") is not None


def unit_suffix(job_id: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (job_id or "").lower())[:26] or str(int(time.time()))


def restart_command(job_id: str, delay_seconds: int) -> list[str]:
    """Transient timer that restarts both agent units after the job reported success.

    The worker running this job is one of the units being restarted, so the restart must
    not happen inside it: systemd-run puts it in its own unit, outside the worker's cgroup.
    """
    return [
        "systemd-run", "--quiet", "--collect",
        f"--unit=nexaploy-agent-restart-{unit_suffix(job_id)}",
        f"--on-active={int(delay_seconds)}s",
        "--timer-property=AccuracySec=1s",
        "systemctl", "restart", *UNITS,
    ]


def watchdog_command(job_id: str, delay_seconds: int, python: Path, config: Path | None,
                     expect_version: str) -> list[str]:
    """Transient timer running ``upgrade-watchdog`` from the *previous* venv's python, so a
    new agent that cannot even start is still rolled back."""
    cmd = [
        "systemd-run", "--quiet", "--collect",
        f"--unit=nexaploy-agent-upgrade-watchdog-{unit_suffix(job_id)}",
        f"--on-active={int(delay_seconds)}s",
        "--timer-property=AccuracySec=5s",
        str(python), "-m", "nexaploy_agent.agent_cli", "upgrade-watchdog",
        "--expect-version", expect_version,
    ]
    if config:
        cmd += ["--config", str(config)]
    return cmd


# --------------------------------------------------------------------------- watchdog
def watchdog_decision(state: dict[str, Any], expect_version: str, healthy: bool) -> str:
    """What the watchdog does: ``skip`` (state is about another upgrade or already settled),
    ``confirm`` (new version healthy) or ``rollback``."""
    if not state or state.get("to_version") != expect_version or state.get("status") != "activated":
        return "skip"
    return "confirm" if healthy else "rollback"


def wait_healthy(check: Callable[[], bool], timeout: float = 60.0, interval: float = 5.0,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> bool:
    deadline = clock() + timeout
    while True:
        try:
            if check():
                return True
        except Exception:
            pass
        if clock() >= deadline:
            return False
        sleep(interval)
