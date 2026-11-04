"""``controlplane.update``: update the control plane that runs as a bench of this server.

The control plane restarts during its own update, so the whole sequence runs here, in the agent
(ARCHITECTURE.md §5.9), and the result reaches the new control plane once it answers again (job
updates are retried):

``{bench, site, image, previous_image?, backup=true, upload=true, port?, health_timeout=300}``

1. ``prepare``  - bench + site exist; the running image (bench .env) is the rollback target and is
   pinned under a local tag (``nexaploy-cp-rollback:<job>``) since a floating tag (``:16``,
   ``:latest``) moves when the new one is pulled;
2. ``backup``   - ``bench backup --with-files`` of the control plane site, staged + uploaded like
   ``site.backup`` (kept locally when no backup storage is configured);
3. ``pull``     - ``docker pull <image>`` (a local-only image is used as is); same name and same
   image id as before -> already up to date, the next three steps are skipped;
4. ``switch``   - IMAGE in the bench .env, compose re-rendered, configurator, ``up -d``;
5. ``migrate``  - ``bench --site <site> migrate``;
6. ``health``   - ``GET http://127.0.0.1:<port>/api/method/ping`` with ``X-Frappe-Site-Name``
   until it answers ``pong`` (port from ``config/control-plane.env``, default 8090);
7. ``finalize`` - IMAGE in control-plane.env, rollback tag removed.

When ``switch``, ``migrate`` or ``health`` fails the job rolls back by itself before failing
(:func:`runtime.controlplane.rollback_plan`): previous image back up, and the database restored
from the backup of step 2 when ``migrate`` had started; then the health check again. The result
carries ``rollback: {failed_step, actions, done, errors}`` and ``rolled_back``.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import Field, field_validator

from .. import validation as v
from ..config import FRAPPE_UID
from ..fsutil import chown, ensure_dir
from ..runtime import controlplane as rc
from ..runtime.backup import BackupError, classify
from ..runtime.registry import Registry, docker_login
from .base import Conflict, NotFound, SiteParams, StepFailed
from .site_jobs import RESTORE_STAGING, _SiteJob


def probe_http(url: str, site: str, timeout: float = 10.0) -> tuple[int | None, str, str | None]:
    """``(status, body, error)`` of one GET with ``X-Frappe-Site-Name: site``."""
    try:
        resp = httpx.get(url, headers={"X-Frappe-Site-Name": site, "Host": site}, timeout=timeout)
        return resp.status_code, resp.text[:2000], None
    except httpx.HTTPError as exc:
        return None, "", f"{type(exc).__name__}: {exc}"[:300]


class ControlPlaneUpdateParams(SiteParams):
    bench: str
    image: str
    previous_image: str | None = None
    backup: bool = True
    upload: bool = True
    port: int | None = Field(default=None, ge=1, le=65535)
    health_timeout: int = Field(default=300, ge=10, le=3600)
    registry: Registry | None = None

    @field_validator("bench")
    @classmethod
    def _bench(cls, value: str) -> str:
        return v.bench_name(value)

    @field_validator("image", "previous_image")
    @classmethod
    def _image(cls, value: str | None) -> str | None:
        return v.image_ref(value) if value else None


class ControlPlaneUpdate(_SiteJob):
    kind = "controlplane.update"
    steps = rc.UPDATE_STEPS
    Params = ControlPlaneUpdateParams
    HEALTH_INTERVAL = 5.0
    probe = staticmethod(probe_http)

    @classmethod
    def precheck(cls, ctx, params: dict[str, Any]) -> None:
        if not ctx.benches.exists(params["bench"]):
            raise NotFound(f"bench {params['bench']!r} not found")
        if not ctx.sites.exists(params["bench"], params["site"]):
            raise NotFound(f"site {params['site']!r} not found on bench {params['bench']}")
        if any(ctx.store.list(status=st, kind=cls.kind, limit=1) for st in ("queued", "running")):
            raise Conflict("a control plane update is already queued or running")

    # ---- helpers -------------------------------------------------------------------------
    @property
    def bench(self) -> str:
        return self.p.bench

    def image_id(self, image: str) -> str | None:
        res = self.ctx.docker.run(["image", "inspect", "--format", "{{.Id}}", image])
        return (res.stdout.strip() or None) if res.ok else None

    def switch_to(self, image: str) -> None:
        b = self.ctx.benches
        env = b.read_env(self.bench)
        if env.get("IMAGE") != image:
            env["IMAGE"] = image
            b.write_env(self.bench, env)
        self.log(f"IMAGE={image}")
        if b.render(self.bench):
            self.log("docker-compose.yml re-rendered")
        self.check(b.run_configurator(self.bench), "configurator")
        self.check(b.up(self.bench), "docker compose up")

    def port(self) -> int:
        return rc.cp_port(rc.read_cp_env(self.settings.config_dir), self.p.port)

    def wait_healthy(self, timeout: float | None = None) -> tuple[bool, str]:
        url = rc.ping_url(self.port())
        deadline = time.monotonic() + (timeout if timeout is not None else self.p.health_timeout)
        last = ""
        tries = 0
        while True:
            tries += 1
            status, body, err = self.probe(url, self.site)
            if rc.is_pong(status, body):
                return True, f"{url} answered pong (try {tries})"
            last = err or f"HTTP {status}: {body[:200]}"
            if time.monotonic() >= deadline:
                return False, f"{url} did not answer pong after {tries} tries: {last}"
            time.sleep(self.HEALTH_INTERVAL)

    # ---- steps -------------------------------------------------------------------------------
    def step_prepare(self) -> None:
        if not self.ctx.benches.exists(self.bench):
            self.fail(f"bench {self.bench} does not exist")
        if not self.ctx.sites.exists(self.bench, self.site):
            self.fail(f"site {self.site} not found on bench {self.bench}")
        self.result["bench"] = self.bench
        self.result["image"] = self.p.image
        current = self.ctx.benches.image(self.bench)
        if "previous_image" not in self.result:
            if self.p.previous_image and current and self.p.previous_image != current:
                self.log(f"warning: the bench runs {current}, not {self.p.previous_image}; rolling back would use {current}")
            self.result["previous_image"] = current or self.p.previous_image
        previous = self.result["previous_image"]
        self.log(f"control plane {self.site} on bench {self.bench}: {previous} -> {self.p.image}")
        if previous and "rollback_image" not in self.result:
            prev_id = self.image_id(previous)
            if prev_id:
                tag = rc.rollback_tag(self.id)
                self.check(self.ctx.docker.run(["tag", previous, tag]), "docker tag (rollback image)")
                self.result.update({"rollback_image": tag, "previous_id": prev_id})
                self.log(f"previous image pinned as {tag}")
            else:
                self.log(f"warning: {previous} is not present locally; a rollback would have to pull it")
                self.result["previous_id"] = None

    def step_backup(self) -> None:
        if not self.p.backup:
            self.skip("backup not requested")
            return
        self.take_backup(self.bench, True)
        if "manifest" not in self.result:
            self.stage_backup(self.bench)
        self.upload_backup(self.bench, self.p.upload)
        self.log(f"pre-update backup {self.result.get('backup_id')} in {self.result.get('path')}")

    def step_pull(self) -> None:
        image = self.p.image
        reg = self.p.registry
        if reg and reg.has_credentials:
            self.secret(reg.password)
            self.check(docker_login(self.ctx.docker, reg), "docker login")
        res = self.ctx.docker.pull(image)
        if not res.ok:
            if self.ctx.docker.image_exists(image):
                self.log(f"warning: docker pull {image} failed; using the local image")
            else:
                self.fail(f"cannot pull {image}: {(res.stderr or res.stdout).strip()[-500:]}")
        new_id = self.image_id(image)
        self.result["image_id"] = new_id
        same = rc.up_to_date(self.result.get("previous_image"), image, self.result.get("previous_id"), new_id)
        self.result["up_to_date"] = same
        if same:
            self.log(f"{image} is unchanged ({new_id}): the control plane is already up to date")

    def step_switch(self) -> None:
        if self.result.get("up_to_date"):
            self.skip("already up to date")
            return
        self.result["switched"] = True
        self.save()
        try:
            self.switch_to(self.p.image)
        except StepFailed as exc:
            self.rollback("switch", str(exc))

    def step_migrate(self) -> None:
        if self.result.get("up_to_date"):
            self.skip("already up to date")
            return
        self.result["migrate_started"] = True
        self.save()
        res = self.ctx.sites.migrate(self.bench, self.site)
        if not res.ok:
            self.rollback("migrate", f"bench migrate failed (exit {res.returncode}): {(res.stderr or res.stdout).strip()[-800:]}")
        self.log(f"{self.site} migrated")

    def step_health(self) -> None:
        ok, detail = self.wait_healthy()
        self.log(detail)
        if not ok:
            self.rollback("health", detail)
        self.result["healthy"] = True

    def step_finalize(self) -> None:
        if rc.set_cp_env(self.settings.config_dir, IMAGE=self.p.image):
            self.log(f"{rc.CP_ENV_NAME}: IMAGE={self.p.image}")
        tag = self.result.get("rollback_image")
        if tag:
            self.ctx.docker.run(["image", "rm", tag])  # untag only; the old image may be pruned later
        self.result["updated"] = not self.result.get("up_to_date")

    # ---- rollback ----------------------------------------------------------------------------
    def rollback(self, step: str, error: str) -> None:
        backup_id = self.result.get("backup_id") if self.result.get("path") else None
        actions = rc.rollback_plan(step, switched=bool(self.result.get("switched")),
                                   migrate_started=bool(self.result.get("migrate_started")), backup_id=backup_id)
        done: list[str] = []
        errors: list[str] = []
        if actions:
            self.log(f"{step} failed; rolling back: {', '.join(actions)}")
        for action in actions:
            try:
                getattr(self, f"_rb_{action}")()
                done.append(action)
            except Exception as exc:  # noqa: BLE001 - keep going: the health check tells the truth
                errors.append(f"{action}: {exc}")
                self.log(f"rollback {action} failed: {exc}")
        self.result["rollback"] = {"failed_step": step, "actions": actions, "done": done, "errors": errors}
        self.result["rolled_back"] = bool(actions) and not errors
        self.save()
        msg = f"control plane update to {self.p.image} failed at {step}: {error}"
        if actions and not errors:
            msg += f"; rolled back to {self.result.get('previous_image')}"
            if "restore_database" in done:
                msg += f" and restored the database from backup {backup_id}"
        elif actions:
            msg += "; ROLLBACK INCOMPLETE (" + "; ".join(errors) + "). See `nexaploy controlplane status`"
        self.fail(msg)

    def _rb_switch_back(self) -> None:
        previous = self.result.get("previous_image")
        if not previous:
            raise StepFailed("previous image unknown")
        pinned = self.result.get("rollback_image")
        if pinned:
            # the floating tag may point at the new image now: put it back on the old content
            self.check(self.ctx.docker.run(["tag", pinned, previous]), "docker tag (restore previous)")
        self.switch_to(previous)

    def _rb_restore_database(self) -> None:
        d = Path(self.result["path"])
        try:
            manifest = self.ctx.backups.verify(d)
        except BackupError as exc:
            raise StepFailed(str(exc)) from exc
        db = next((f["name"] for f in manifest.get("files", []) if (f.get("type") or classify(f["name"])) == "database"), None)
        if not db:
            raise StepFailed("pre-update backup has no database dump")
        staging = self.ctx.benches.sites_dir(self.bench) / RESTORE_STAGING / self.id
        ensure_dir(staging, FRAPPE_UID, FRAPPE_UID, 0o750)
        try:
            shutil.copy2(d / db, staging / db)
            chown(staging / db, FRAPPE_UID, FRAPPE_UID)
            args = ["--site", self.site, "restore", self.ctx.sites.container_path(self.bench, staging / db),
                    "--db-root-username", "root", "--db-root-password", self.db_root_password(), "--force"]
            self.check(self.ctx.sites.bench(self.bench, args, timeout=6 * 3600), "bench restore (rollback)")
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _rb_health(self) -> None:
        ok, detail = self.wait_healthy(timeout=min(self.p.health_timeout, 300))
        self.log(detail)
        if not ok:
            raise StepFailed(detail)
