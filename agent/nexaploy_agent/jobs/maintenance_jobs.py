"""Server maintenance jobs pushed by the control plane (ARCHITECTURE.md §17).

* ``server.os_update``    ``{mode: all|security, reboot: never|if_required|always, reboot_delay_seconds}``
  update_lists -> upgrade -> cleanup -> status -> reboot (scheduled with ``systemd-run`` *after*
  the job reported success; fallback ``shutdown -r +1``)
* ``server.os_reboot``    ``{delay_seconds}``
* ``server.os_configure`` ``{unattended_security}``: 20auto-upgrades + 52nexaploy-unattended
  (security origins only, ``Automatic-Reboot "false"``)
* ``server.diagnose``     full health facts (runtime/health.py ``full_health``)
* ``server.cleanup``      ``{targets, dry_run, log_retention_days, backup_local_keep_days, ...}``:
  one step per target (runtime/cleanup.py), then ``summary``
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from ..fsutil import atomic_write
from ..runtime.cleanup import TARGETS, Cleaner, CleanupOptions, fmt_bytes, summarize
from ..runtime.health import full_health
from ..runtime.os_updates import (
    APT_ENV, AUTO_UPGRADES_FILE, NEXAPLOY_UNATTENDED_FILE, SHUTDOWN_FALLBACK, apt_get,
    auto_upgrades_content, reboot_command, unattended_content, upgrade_command,
)
from ..runtime.server import server_info
from ..runtime.upgrade import systemd_available
from .base import BaseParams, Job

APT_TIMEOUT = 60 * 60


def _tail(text: str, n: int = 3000) -> str:
    return (text or "")[-n:]


class _OsJob(Job):
    @property
    def osu(self):
        return self.ctx.os_updates

    def _require_apt(self) -> None:
        if not self.osu.supported():
            self.fail("system updates need an apt based distribution (Ubuntu / Debian); this server "
                      "reports another one: update it with its own tools")

    def _schedule_reboot(self, delay: int) -> None:
        run = self.ctx.runner
        if systemd_available():
            self.check(run.run(reboot_command(self.id, delay), timeout=30), "scheduling the reboot")
            self.result.update({"reboot_scheduled": True, "reboot_delay_seconds": delay})
            self.log(f"reboot scheduled in {delay}s (after this job is reported)")
            return
        res = run.run(SHUTDOWN_FALLBACK, timeout=30)
        self.check(res, "shutdown -r +1")
        self.result.update({"reboot_scheduled": True, "reboot_delay_seconds": 60})
        self.log("systemd-run not available: reboot scheduled with shutdown -r +1")


# --------------------------------------------------------------------------- server.os_update
class OsUpdateParams(BaseParams):
    mode: Literal["all", "security"] = "all"
    reboot: Literal["never", "if_required", "always"] = "if_required"
    reboot_delay_seconds: int = Field(default=60, ge=10, le=3600)


class ServerOsUpdate(_OsJob):
    kind = "server.os_update"
    steps = ("update_lists", "upgrade", "cleanup", "status", "reboot")
    Params = OsUpdateParams

    def step_update_lists(self) -> None:
        self._require_apt()
        res = self.ctx.runner.run(apt_get("update"), env=APT_ENV, timeout=APT_TIMEOUT)
        self.log(_tail(res.stdout))
        self.check(res, "apt-get update")
        before = self.osu.status(force=True)
        self.result["before"] = {k: before.get(k) for k in ("pending", "security", "running_kernel")}
        self.log(f"{before.get('pending', 0)} upgradable package(s), {before.get('security', 0)} security")

    def step_upgrade(self) -> None:
        if self.p.mode == "security":
            probe = self.ctx.runner.run(["which", "unattended-upgrade"], timeout=30)
            if not probe.ok or not probe.stdout.strip():
                self.fail("unattended-upgrades is not installed: install it (apt-get install unattended-upgrades) "
                          "or enable Automatic security updates, or update all packages instead")
        res = self.ctx.runner.run(upgrade_command(self.p.mode), env=APT_ENV, timeout=APT_TIMEOUT)
        self.log(_tail(res.stdout, 8000))
        self.check(res, "unattended-upgrade" if self.p.mode == "security" else "apt-get full-upgrade")

    def step_cleanup(self) -> None:
        res = self.ctx.runner.run(apt_get("-y", "autoremove", "--purge"), env=APT_ENV, timeout=APT_TIMEOUT)
        self.log(_tail(res.stdout))
        if not res.ok:
            self.log(f"warning: apt-get autoremove failed: {_tail(res.stderr, 300)}")
        res = self.ctx.runner.run(apt_get("clean"), env=APT_ENV, timeout=600)
        if not res.ok:
            self.log(f"warning: apt-get clean failed: {_tail(res.stderr, 300)}")

    def step_status(self) -> None:
        self.osu.invalidate()
        status = self.osu.status(force=True)
        self.result["os_updates"] = status
        self.log(f"remaining: {status.get('pending', 0)} package(s); reboot required: "
                 f"{bool(status.get('reboot_required'))}")

    def step_reboot(self) -> None:
        if self.result.get("reboot_scheduled"):
            self.skip("reboot already scheduled")
            return
        policy = self.p.reboot
        required = bool((self.result.get("os_updates") or {}).get("reboot_required"))
        if policy == "never" or (policy == "if_required" and not required):
            self.result["reboot_scheduled"] = False
            self.log("no reboot" + (" (a reboot is required: schedule one)" if required else ""))
            return
        self._schedule_reboot(self.p.reboot_delay_seconds)


# --------------------------------------------------------------------------- server.os_reboot
class OsRebootParams(BaseParams):
    delay_seconds: int = Field(default=60, ge=10, le=3600)


class ServerOsReboot(_OsJob):
    kind = "server.os_reboot"
    steps = ("reboot",)
    Params = OsRebootParams

    def step_reboot(self) -> None:
        if self.result.get("reboot_scheduled"):
            self.skip("reboot already scheduled")
            return
        self.osu.invalidate()
        self._schedule_reboot(self.p.delay_seconds)


# --------------------------------------------------------------------------- server.os_configure
class OsConfigureParams(BaseParams):
    unattended_security: bool


class ServerOsConfigure(_OsJob):
    kind = "server.os_configure"
    steps = ("configure", "status")
    Params = OsConfigureParams

    def step_configure(self) -> None:
        self._require_apt()
        conf = self.osu.paths.apt_conf_dir
        enable = self.p.unattended_security
        if enable:
            probe = self.ctx.runner.run(["which", "unattended-upgrade"], timeout=30)
            if not probe.ok or not probe.stdout.strip():
                res = self.ctx.runner.run(apt_get("install", "-y", "unattended-upgrades"), env=APT_ENV,
                                          timeout=APT_TIMEOUT)
                self.log(_tail(res.stdout))
                self.check(res, "apt-get install unattended-upgrades")
        conf.mkdir(parents=True, exist_ok=True)
        atomic_write(conf / AUTO_UPGRADES_FILE, auto_upgrades_content(enable), mode=0o644)
        atomic_write(conf / NEXAPLOY_UNATTENDED_FILE, unattended_content(), mode=0o644)
        self.log(f"automatic security updates {'enabled' if enable else 'disabled'} "
                 f"({conf / AUTO_UPGRADES_FILE}); automatic reboots stay off")

    def step_status(self) -> None:
        self.osu.invalidate()
        self.result["os_updates"] = self.osu.status(force=True)


# --------------------------------------------------------------------------- server.diagnose
class ServerDiagnose(Job):
    kind = "server.diagnose"
    steps = ("collect",)
    Params = BaseParams

    def step_collect(self) -> None:
        facts = server_info(self.ctx.docker, self.ctx.benches, self.settings.agent.server_id)
        facts.pop("benches", None)
        facts["os_updates"] = self.ctx.os_updates.status()
        facts["health"] = full_health(self.ctx)
        self.result.update(facts)
        h = facts["health"]
        self.log(f"filesystems: {[(f['path'], f['used_percent']) for f in h.get('filesystems') or []]}")
        self.log(f"failed units: {h.get('failed_units')}, OOM kills since boot: {h.get('oom_kills')}, "
                 f"NTP synchronized: {h.get('ntp_synchronized')}")


# --------------------------------------------------------------------------- server.cleanup
class CleanupParams(BaseParams):
    targets: list[Literal[TARGETS]] = Field(default_factory=lambda: list(TARGETS), min_length=1)  # type: ignore[valid-type]
    dry_run: bool = True
    log_retention_days: int = Field(default=14, ge=1, le=3650)
    backup_local_keep_days: int | None = Field(default=None, ge=0, le=3650)
    docker_log_max_mb: int = Field(default=100, ge=10, le=100_000)
    bench_log_max_mb: int = Field(default=200, ge=10, le=100_000)
    image_min_age_hours: int = Field(default=72, ge=1, le=24 * 365)
    job_retention_days: int = Field(default=30, ge=7, le=3650)
    images_scope: Literal["unused", "dangling"] = "unused"

    @field_validator("targets")
    @classmethod
    def _dedupe(cls, value: list[str]) -> list[str]:
        # run in the canonical order (cheap, safe targets first does not matter: each is independent)
        return [t for t in TARGETS if t in set(value)]


class ServerCleanup(Job):
    kind = "server.cleanup"
    Params = CleanupParams

    @classmethod
    def step_names(cls, params: dict[str, Any]) -> list[str]:
        return [*params.get("targets", TARGETS), "summary"]

    def _cleaner(self) -> Cleaner:
        p = self.p
        opts = CleanupOptions(
            dry_run=p.dry_run, log_retention_days=p.log_retention_days,
            backup_local_keep_days=p.backup_local_keep_days, docker_log_max_mb=p.docker_log_max_mb,
            bench_log_max_mb=p.bench_log_max_mb, image_min_age_hours=p.image_min_age_hours,
            job_retention_days=p.job_retention_days, images_scope=p.images_scope,
        )
        return Cleaner(self.ctx, opts, log=self.log)

    def _target(self, name: str) -> None:
        done = self.result.setdefault("targets", {})
        if name in done:
            self.skip(f"{name} already cleaned")
            return
        done[name] = self._cleaner().run(name)

    def __getattr__(self, item: str):
        if item.startswith("step_") and item[5:] in TARGETS:
            return lambda: self._target(item[5:])
        raise AttributeError(item)

    def step_summary(self) -> None:
        total = summarize((self.result.get("targets") or {}).values())
        self.result.update({"dry_run": self.p.dry_run, **total})
        verb = "would free" if self.p.dry_run else "freed"
        self.log(f"{verb} {fmt_bytes(total['freed_bytes'])} in total ({total['items']} item(s), "
                 f"{total['errors']} error(s))")
        if not self.p.dry_run:
            self.ctx.os_updates.invalidate()  # apt cache / autoremove change the package state


MAINTENANCE_JOB_CLASSES = (ServerOsUpdate, ServerOsReboot, ServerOsConfigure, ServerDiagnose, ServerCleanup)
