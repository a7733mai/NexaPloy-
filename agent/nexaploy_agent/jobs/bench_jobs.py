"""bench.* jobs (§5.2)."""

from __future__ import annotations

import shutil
from typing import Any

from pydantic import field_validator

from .. import validation as v
from ..fsutil import ensure_dir
from ..runtime.backup import new_ts
from ..runtime.bench import BenchManager
from ..runtime.image import normalise_apps_json
from ..runtime.registry import Registry, docker_login
from .base import BaseParams, BenchParams, Conflict, Job, NotFound

NETWORKS = ("proxy-network", "nexaploy-db")


def _require_bench(ctx, bench: str) -> None:
    if not ctx.benches.exists(bench):
        raise NotFound(f"bench {bench!r} not found")


class _BenchJob(Job):
    Params = BenchParams

    @property
    def bench(self) -> str:
        return self.p.bench

    @classmethod
    def precheck(cls, ctx, params: dict[str, Any]) -> None:
        _require_bench(ctx, params["bench"])

    def require_bench(self) -> None:
        if not self.ctx.benches.exists(self.bench):
            self.fail(f"bench {self.bench!r} does not exist")

    # Shared check-then-act steps --------------------------------------------------
    def ensure_image(self, image: str) -> None:
        if self.ctx.docker.image_exists(image):
            self.skip(f"image {image} present locally")
            return
        reg: Registry | None = getattr(self.p, "registry", None)
        if reg and reg.has_credentials:
            self.secret(reg.password)
            self.log(f"logging in to registry {reg.host or 'docker.io'} as {reg.username}")
            self.check(docker_login(self.ctx.docker, reg), "docker login")
        self.log(f"image {image} not present locally; pulling")
        self.check(self.ctx.docker.pull(image), f"docker pull {image}")

    def configure(self) -> None:
        # Re-render first: cheap and deterministic, and a retried job then picks up
        # template fixes even though its earlier "prepare" step is skipped.
        if self.ctx.benches.render(self.bench):
            self.log("docker-compose.yml re-rendered")
        # The configurator is itself idempotent (bench set-config -g ...).
        self.check(self.ctx.benches.run_configurator(self.bench), "configurator")

    def up(self) -> None:
        self.check(self.ctx.benches.up(self.bench), "docker compose up")


# --------------------------------------------------------------------------- create
class BenchCreateParams(BaseParams):
    name: str
    image: str
    env: dict[str, Any] = {}
    apps_json: Any = None
    registry: Registry | None = None  # credentials to pull a private image

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return v.bench_name(value)

    @field_validator("image")
    @classmethod
    def _image(cls, value: str) -> str:
        return v.image_ref(value)

    @field_validator("env")
    @classmethod
    def _env(cls, value: dict[str, Any]) -> dict[str, str]:
        return BenchManager.validate_env_overrides(value)

    @field_validator("apps_json")
    @classmethod
    def _apps(cls, value: Any) -> Any:
        return None if value in (None, "", []) else normalise_apps_json(value)


class BenchCreate(_BenchJob):
    """Render compose + .env, ensure image, run configurator, up -d."""

    kind = "bench.create"
    steps = ("prepare", "ensure_image", "configure", "start")
    Params = BenchCreateParams

    @property
    def bench(self) -> str:
        return self.p.name

    @classmethod
    def precheck(cls, ctx, params: dict[str, Any]) -> None:
        if ctx.benches.exists(params["name"]):
            raise Conflict(f"bench {params['name']!r} already exists")

    def step_prepare(self) -> None:
        b = self.ctx.benches
        current = b.image(self.bench) if b.exists(self.bench) else None
        if current and current != self.p.image:
            self.fail(f"bench {self.bench} already exists with image {current}; use bench.update")
        for net in NETWORKS:
            if self.ctx.docker.ensure_network(net):
                self.log(f"created docker network {net}")
        b.ensure_dirs(self.bench)
        env = {**b.default_env(self.bench, self.p.image), **b.read_env(self.bench), **self.p.env}
        env["IMAGE"] = self.p.image
        changed = b.write_env(self.bench, env)
        self.log(".env written" if changed else ".env unchanged")
        if self.p.apps_json is not None:
            b.write_apps_json(self.bench, self.p.apps_json)
        changed = b.render(self.bench)
        self.log("docker-compose.yml rendered" if changed else "docker-compose.yml unchanged")
        self.result.update({"bench": self.bench, "image": self.p.image, "path": str(b.path(self.bench))})

    def step_ensure_image(self) -> None:
        self.ensure_image(self.p.image)

    def step_configure(self) -> None:
        self.configure()

    def step_start(self) -> None:
        self.up()


# --------------------------------------------------------------------------- start/stop/restart
class BenchStart(_BenchJob):
    kind = "bench.start"
    steps = ("start",)

    def step_start(self) -> None:
        self.require_bench()
        self.up()


class BenchStop(_BenchJob):
    kind = "bench.stop"
    steps = ("stop",)

    def step_stop(self) -> None:
        self.require_bench()
        self.check(self.ctx.benches.stop(self.bench), "docker compose stop")


class BenchRestart(_BenchJob):
    kind = "bench.restart"
    steps = ("restart",)

    def step_restart(self) -> None:
        self.require_bench()
        if not self.ctx.docker.is_running(self.bench):
            self.log("bench is not running; starting it")
            self.up()
            return
        self.check(self.ctx.benches.restart(self.bench), "docker compose restart")


# --------------------------------------------------------------------------- update
class BenchUpdateParams(BenchParams):
    image: str
    registry: Registry | None = None  # credentials to pull a private image

    @field_validator("image")
    @classmethod
    def _image(cls, value: str) -> str:
        return v.image_ref(value)


class BenchUpdate(_BenchJob):
    """New IMAGE in .env, configurator, up -d, migrate every site."""

    kind = "bench.update"
    steps = ("ensure_image", "write_config", "configure", "start", "migrate_sites")
    Params = BenchUpdateParams

    def step_ensure_image(self) -> None:
        self.require_bench()
        self.ensure_image(self.p.image)

    def step_write_config(self) -> None:
        b = self.ctx.benches
        env = b.read_env(self.bench)
        previous = env.get("IMAGE")
        self.result.setdefault("previous_image", previous)
        if previous == self.p.image:
            self.skip(f".env already uses {self.p.image}")
        else:
            env["IMAGE"] = self.p.image
            b.write_env(self.bench, env)
            self.log(f"IMAGE {previous} -> {self.p.image}")
        # Re-render so template upgrades shipped with the agent land too.
        if b.render(self.bench):
            self.log("docker-compose.yml re-rendered")
        self.result["image"] = self.p.image

    def step_configure(self) -> None:
        self.configure()

    def step_start(self) -> None:
        self.up()

    def step_migrate_sites(self) -> None:
        done: list[str] = self.result.setdefault("migrated", [])
        for site in self.ctx.benches.sites(self.bench):
            if site in done:
                self.skip(f"{site} migrated")
                continue
            self.log(f"migrating {site}")
            self.check(self.ctx.sites.migrate(self.bench, site), f"migrate {site}")
            done.append(site)
            self.save()


# --------------------------------------------------------------------------- remove
class BenchRemoveParams(BenchParams):
    force: bool = False


class BenchRemove(_BenchJob):
    """compose down, drop routes, delete (or archive when forced with sites)."""

    kind = "bench.remove"
    steps = ("check", "down", "remove_routes", "remove_files")
    Params = BenchRemoveParams

    @classmethod
    def precheck(cls, ctx, params: dict[str, Any]) -> None:
        _require_bench(ctx, params["bench"])
        sites = ctx.benches.sites(params["bench"])
        if sites and not params.get("force"):
            raise Conflict(f"bench {params['bench']} still has sites: {', '.join(sites)} (use force)")

    def step_check(self) -> None:
        if not self.ctx.benches.path(self.bench).exists():
            self.skip("bench directory already removed")
            self.result["sites"] = self.result.get("sites", [])
            return
        sites = self.ctx.benches.sites(self.bench)
        if sites and not self.p.force:
            self.fail(f"bench {self.bench} still has sites: {', '.join(sites)} (use force)")
        if "sites" not in self.result:
            self.result["sites"] = sites
        if sites:
            self.log(f"force: sites {', '.join(sites)} will be archived with the bench (databases are kept)")

    def step_down(self) -> None:
        if not self.ctx.benches.compose_file(self.bench).exists():
            self.skip("no compose file")
            return
        self.check(self.ctx.benches.down(self.bench), "docker compose down")

    def step_remove_routes(self) -> None:
        for site in self.result.get("sites", []):
            if self.ctx.proxy.remove_route(site):
                self.log(f"removed route for {site}")

    def step_remove_files(self) -> None:
        path = self.ctx.benches.path(self.bench)
        if not path.exists():
            self.skip("bench directory already removed")
            return
        if self.result.get("sites"):
            archive = ensure_dir(self.ctx.benches.archive_dir(), mode=0o700)
            dest = archive / f"{self.bench}-{new_ts()}"
            shutil.move(str(path), str(dest))
            self.result["archived_to"] = str(dest)
            self.log(f"bench directory archived to {dest}")
        else:
            shutil.rmtree(path)
            self.log(f"removed {path}")
