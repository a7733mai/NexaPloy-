"""image.build (§4, §5.2)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import field_validator

from .. import validation as v
from ..runtime.image import normalise_apps_json
from ..runtime.registry import Registry, docker_login
from .base import BaseParams, Job

BUILD_EXIT_CODES = {
    1: "build or push failed",
    2: "invalid input",
    3: "docker or git missing",
    4: "bad build context",
}


class ImageBuildParams(BaseParams):
    major: Literal[14, 15, 16]
    apps_json: Any
    tag: str
    frappe_branch: str | None = None
    python: str | None = None
    node: str | None = None
    push: bool = False
    # Optional registry credentials: ``docker login --password-stdin`` before pushing.
    registry: Registry | None = None

    @field_validator("tag")
    @classmethod
    def _tag(cls, value: str) -> str:
        return v.image_ref(value)

    @field_validator("apps_json")
    @classmethod
    def _apps(cls, value: Any) -> list[dict[str, str]]:
        return normalise_apps_json(value)

    @field_validator("frappe_branch", "python", "node")
    @classmethod
    def _simple(cls, value: str | None) -> str | None:
        if value is not None and (not value or value.startswith("-") or any(c.isspace() for c in value)):
            raise ValueError("invalid value")
        return value or None


class ImageBuild(Job):
    kind = "image.build"
    steps = ("prepare", "build")
    Params = ImageBuildParams

    def step_prepare(self) -> None:
        d = self.ctx.images.prepare(self.p.tag, self.p.apps_json)
        self.result.update({"tag": self.p.tag, "build_dir": str(d), "log": str(d / "build.log")})
        self.log(f"apps.json written to {d / 'apps.json'}")

    def login(self) -> None:
        reg = self.p.registry
        if not reg or not reg.has_credentials:
            return
        self.secret(reg.password)
        self.log(f"logging in to registry {reg.host or 'docker.io'} as {reg.username}")
        self.check(docker_login(self.ctx.docker, reg), "docker login")

    def step_build(self) -> None:
        if self.p.push:
            self.login()
        if self.ctx.docker.image_exists(self.p.tag):
            self.skip(f"image {self.p.tag} already exists")
            self.result["built"] = False
            if self.p.push and not self.result.get("pushed"):
                # A previous run may have built the image but failed to push it.
                self.check(self.ctx.docker.run(["push", self.p.tag], timeout=3600), f"docker push {self.p.tag}")
                self.result["pushed"] = True
            return
        res = self.ctx.images.build(
            self.p.major, self.p.tag, frappe_branch=self.p.frappe_branch,
            python=self.p.python, node=self.p.node, push=self.p.push,
        )
        if not res.ok:
            log_tail = self._log_tail()
            if log_tail:
                self.log("--- build.log (tail) ---\n" + log_tail)
            reason = BUILD_EXIT_CODES.get(res.returncode, f"exit {res.returncode}")
            self.fail(f"image build failed: {reason}")
        lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
        built = lines[-1] if lines else self.p.tag
        if built != self.p.tag:
            self.log(f"warning: build.sh reported tag {built!r}, expected {self.p.tag!r}")
        self.result.update({"built": True, "tag": built, "pushed": bool(self.p.push)})

    def _log_tail(self, n: int = 60) -> str:
        try:
            lines = (self.ctx.images.build_dir(self.p.tag) / "build.log").read_text(errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-n:])
