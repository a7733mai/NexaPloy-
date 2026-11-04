"""Agent self-upgrade jobs (operator-controlled: the control plane enqueues them).

* ``agent.upgrade``  ``{version, ref, repo_url?, wheel_url?, wheel_sha256?, restart?,
  restart_delay_seconds?, watchdog_seconds?}``
  download -> install into a new venv -> verify (``nexaploy-agent version``) -> swap
  (keeps ``.venv.prev``) -> schedule a restart of both units *after* the job reported
  success, plus a watchdog that rolls back when the new agent does not come up.
* ``agent.rollback`` ``{restart?, restart_delay_seconds?}`` - put ``.venv.prev`` back.

See runtime/upgrade.py for the directory layout. Benches and sites are never touched.
"""

from __future__ import annotations

import shutil

from pydantic import Field, field_validator, model_validator

from .. import __version__
from ..runtime.upgrade import (
    DEFAULT_REPO, AgentInstall, UpgradeError, restart_command, systemd_available,
    valid_ref, valid_repo_url, valid_version, valid_wheel_url, watchdog_command,
)
from .base import BaseParams, Job

PIP_TIMEOUT = 15 * 60
GIT_TIMEOUT = 5 * 60


class _RestartParams(BaseParams):
    restart: bool = True
    restart_delay_seconds: int = Field(default=10, ge=3, le=600)


class UpgradeParams(_RestartParams):
    version: str
    ref: str | None = None
    repo_url: str | None = None
    wheel_url: str | None = None
    wheel_sha256: str | None = None
    # 0 disables the automatic rollback watchdog
    watchdog_seconds: int = Field(default=180, ge=0, le=3600)

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        return valid_version(value.lstrip("v"))

    @field_validator("ref")
    @classmethod
    def _ref(cls, value: str | None) -> str | None:
        return valid_ref(value) if value else None

    @field_validator("repo_url")
    @classmethod
    def _repo(cls, value: str | None) -> str | None:
        return valid_repo_url(value) if value else None

    @field_validator("wheel_url")
    @classmethod
    def _wheel(cls, value: str | None) -> str | None:
        return valid_wheel_url(value) if value else None

    @field_validator("wheel_sha256")
    @classmethod
    def _sha(cls, value: str | None) -> str | None:
        if not value:
            return None
        value = value.lower()
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("wheel_sha256 must be 64 hex characters")
        return value

    @model_validator(mode="after")
    def _source(self) -> "UpgradeParams":
        if not self.wheel_url and not self.ref:
            self.ref = f"v{self.version}"
        return self


class _AgentJob(Job):
    @property
    def install(self) -> AgentInstall:
        return AgentInstall(self.settings.root / "agent")

    def _schedule_restart(self, extra: list[list[str]] | None = None) -> None:
        p = self.p
        if not p.restart:
            self.log("restart disabled: restart nexaploy-agent-api and nexaploy-agent-worker yourself")
            self.result["restart_scheduled"] = False
            return
        if not systemd_available():
            self.log("systemd not available (installed with --no-systemd?): restart "
                     "`nexaploy-agent api` and `nexaploy-agent worker` yourself")
            self.result["restart_scheduled"] = False
            return
        for cmd in extra or []:
            res = self.ctx.runner.run(cmd, timeout=30)
            if not res.ok:
                self.log(f"warning: could not schedule {cmd[3]}: {res.stderr.strip()[:300]}")
        self.check(self.ctx.runner.run(restart_command(self.id, p.restart_delay_seconds), timeout=30),
                   "scheduling the agent restart")
        self.result["restart_scheduled"] = True
        self.log(f"agent units restart in {p.restart_delay_seconds}s (after this job is reported)")


# --------------------------------------------------------------------------- agent.upgrade
class AgentUpgrade(_AgentJob):
    kind = "agent.upgrade"
    steps = ("prepare", "download", "install", "verify", "activate", "restart")
    Params = UpgradeParams

    def _require_installed(self) -> None:
        inst = self.install
        if not inst.bin(inst.venv, "python").exists():
            self.fail(f"{inst.venv} not found: this agent was not installed by install.sh; "
                      "upgrade it the way it was installed")

    def step_prepare(self) -> None:
        self._require_installed()
        inst = self.install
        state = inst.read_state()
        if state.get("job_id") == self.id and state.get("status") in ("activated", "confirmed"):
            self.skip(f"upgrade to {self.p.version} already activated")
            return
        inst.clean_leftovers()
        self.result.update({"from_version": __version__, "to_version": self.p.version})
        self.log(f"upgrading agent {__version__} -> {self.p.version}")
        inst.write_state(job_id=self.id, from_version=__version__, to_version=self.p.version,
                         ref=self.p.ref, status="preparing")

    def _activated(self) -> bool:
        state = self.install.read_state()
        return state.get("job_id") == self.id and state.get("status") in ("activated", "confirmed")

    def step_download(self) -> None:
        if self._activated():
            self.skip("already activated")
            return
        inst, p, run = self.install, self.p, self.ctx.runner
        if p.wheel_url:
            inst.dist_new.mkdir(parents=True, exist_ok=True)
            wheel = inst.dist_new / p.wheel_url.rsplit("/", 1)[-1]
            self.check(run.run(["curl", "-fsSL", "--retry", "3", "-o", str(wheel), p.wheel_url],
                               timeout=GIT_TIMEOUT), "wheel download")
            if p.wheel_sha256:
                from ..fsutil import sha256_file

                got = sha256_file(wheel)
                if got != p.wheel_sha256:
                    self.fail(f"wheel checksum mismatch: expected {p.wheel_sha256}, got {got}")
                self.log("wheel checksum verified")
            self.result["wheel"] = str(wheel)
            return
        repo = p.repo_url or DEFAULT_REPO
        if inst.src_new.exists():
            shutil.rmtree(inst.src_new)
        self.check(run.run(["git", "init", "-q", str(inst.src_new)], timeout=60), "git init")
        self.check(run.run(["git", "-C", str(inst.src_new), "fetch", "-q", "--depth", "1", "--", repo, p.ref],
                           timeout=GIT_TIMEOUT), f"git fetch {p.ref}")
        self.check(run.run(["git", "-C", str(inst.src_new), "checkout", "-q", "--detach", "FETCH_HEAD"],
                           timeout=60), "git checkout")
        res = run.run(["git", "-C", str(inst.src_new), "rev-parse", "HEAD"], timeout=30)
        if res.ok and res.stdout.strip():
            self.result["commit"] = res.stdout.strip()
            self.log(f"{repo} {p.ref} at {self.result['commit']}")
        pkg = inst.package_dir(inst.src_new)
        if pkg is None:
            self.fail(f"no agent pyproject.toml in {repo}@{p.ref}")
        # Ship the bench-template files of this release with the package (install.sh does
        # the same): new benches use them, running benches are not re-rendered.
        repo_dir = inst.repo_dir(inst.src_new)
        sync = pkg / "scripts" / "sync_templates.sh"
        if repo_dir is not None and (repo_dir / "bench-template").is_dir() and sync.is_file():
            self.check(run.run(["bash", str(sync), str(repo_dir / "bench-template")], timeout=120),
                       "sync_templates.sh")
            self.log("bench templates synced from the new source")

    def step_install(self) -> None:
        if self._activated():
            self.skip("already activated")
            return
        inst, run = self.install, self.ctx.runner
        target = self.result.get("wheel")
        if not target:
            pkg = inst.package_dir(inst.src_new)
            if pkg is None:
                self.fail(f"agent source missing in {inst.src_new}")
            target = str(pkg)
        if inst.venv_new.exists():
            shutil.rmtree(inst.venv_new)
        python = inst.bin(inst.venv, "python")
        self.check(run.run([str(python), "-m", "venv", str(inst.venv_new)], timeout=300), "venv creation")
        pip = inst.bin(inst.venv_new, "pip")
        self.check(run.run([str(pip), "install", "-q", "--upgrade", "pip"], timeout=PIP_TIMEOUT), "pip upgrade")
        self.check(run.run([str(pip), "install", "-q", target], timeout=PIP_TIMEOUT), "pip install")
        self.log(f"installed {target} into {inst.venv_new}")

    def step_verify(self) -> None:
        if self._activated():
            self.skip("already activated")
            return
        inst, run = self.install, self.ctx.runner
        res = self.check(run.run([str(inst.bin(inst.venv_new, "nexaploy-agent")), "version"], timeout=60),
                         "nexaploy-agent version (self-check)")
        got = res.stdout.strip().splitlines()[-1].strip() if res.stdout.strip() else ""
        if got != self.p.version:
            self.fail(f"self-check: new venv reports version {got!r}, expected {self.p.version!r}")
        # import the daemons' modules: catches missing/broken dependencies before the swap
        self.check(run.run([str(inst.bin(inst.venv_new, "python")), "-c",
                            "import nexaploy_agent.api, nexaploy_agent.jobs.worker, nexaploy_agent.cli"],
                           timeout=120), "import check")
        if not inst.bin(inst.venv_new, "nexaploy").exists():
            self.fail("new venv has no `nexaploy` CLI")
        self.log(f"self-check ok: nexaploy-agent {got}")

    def step_activate(self) -> None:
        inst = self.install
        if self._activated():
            self.skip("already activated")
            return
        try:
            inst.activate()
        except (OSError, UpgradeError) as exc:
            self.fail(f"swap failed: {exc}")
        self.log(f"{inst.venv} is now {self.p.version}; previous venv kept at {inst.venv_prev}")
        self._refresh_image_builder()
        inst.write_state(job_id=self.id, from_version=self.result.get("from_version"),
                         to_version=self.p.version, ref=self.p.ref, commit=self.result.get("commit"),
                         status="activated")

    def _refresh_image_builder(self) -> None:
        """Copy image-builder files of the new source to <root>/images (used by image.build
        jobs only; jobs run one at a time, so no build is in flight)."""
        repo_dir = self.install.repo_dir(self.install.src)
        if repo_dir is None or not (repo_dir / "image-builder" / "build.sh").is_file():
            return
        src = repo_dir / "image-builder"
        dst = self.settings.images_dir
        dst.mkdir(parents=True, exist_ok=True)
        for name in ("build.sh", "Containerfile", "versions.json"):
            if (src / name).is_file():
                shutil.copy2(src / name, dst / name)
        (dst / "build.sh").chmod(0o755)
        if (src / "resources").is_dir():
            shutil.copytree(src / "resources", dst / "resources", dirs_exist_ok=True)
        self.log(f"image-builder files refreshed in {dst}")

    def step_restart(self) -> None:
        extra = []
        inst, p = self.install, self.p
        if p.restart and p.watchdog_seconds and inst.bin(inst.venv_prev, "python").exists():
            extra.append(watchdog_command(
                self.id, p.restart_delay_seconds + p.watchdog_seconds, inst.bin(inst.venv_prev, "python"),
                self.settings.source, p.version,
            ))
            self.log(f"watchdog: rolls back automatically unless {p.version} answers within "
                     f"{p.restart_delay_seconds + p.watchdog_seconds}s")
        self._schedule_restart(extra)


# --------------------------------------------------------------------------- agent.rollback
class AgentRollback(_AgentJob):
    kind = "agent.rollback"
    steps = ("rollback", "restart")
    Params = _RestartParams

    def step_rollback(self) -> None:
        inst = self.install
        state = inst.read_state()
        if state.get("rollback_job_id") == self.id:
            self.skip("already rolled back")
            return
        try:
            inst.rollback()
        except (OSError, UpgradeError) as exc:
            self.fail(str(exc))
        inst.write_state(status="rolled_back", rollback_job_id=self.id)
        self.result["rolled_back_from"] = __version__
        self.log(f"rolled back: {inst.venv} is the previous agent again (failed one at {inst.venv_failed})")

    def step_restart(self) -> None:
        self._schedule_restart()

