"""Image builds via image-builder/build.sh (§4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import validation as v
from ..config import Settings
from ..fsutil import write_if_changed
from .docker import Result, Runner

SUPPORTED_MAJORS = (14, 15, 16)


class ImageError(RuntimeError):
    pass


def normalise_apps_json(apps_json: Any) -> list[dict[str, str]]:
    """Accept a list or JSON string; each item must be ``{url, branch}``."""
    if isinstance(apps_json, str):
        try:
            apps_json = json.loads(apps_json)
        except json.JSONDecodeError as exc:
            raise v.ValidationError(f"apps_json is not valid JSON: {exc}") from exc
    if not isinstance(apps_json, list):
        raise v.ValidationError("apps_json must be a list of {url, branch}")
    out = []
    for i, item in enumerate(apps_json):
        if not isinstance(item, dict) or not isinstance(item.get("url"), str) or not item["url"]:
            raise v.ValidationError(f"apps_json[{i}] must have a url")
        url = item["url"]
        if not url.startswith(("https://", "http://", "git@", "ssh://")):
            raise v.ValidationError(f"apps_json[{i}].url must be a git URL")
        entry = {"url": url}
        if item.get("branch") is not None:
            branch = str(item["branch"])
            if not branch or any(c.isspace() for c in branch) or branch.startswith("-"):
                raise v.ValidationError(f"apps_json[{i}].branch is invalid")
            entry["branch"] = branch
        out.append(entry)
    return out


def build_dir_name(tag: str) -> str:
    """``nexaploy/bench:erp-v15-main-2026`` -> ``erp-v15-main-2026``."""
    return tag.rsplit(":", 1)[-1]


class ImageBuilder:
    def __init__(self, settings: Settings, runner: Runner):
        self.settings = settings
        self.runner = runner

    def build_dir(self, tag: str) -> Path:
        return self.settings.builds_dir / build_dir_name(v.image_ref(tag))

    def prepare(self, tag: str, apps_json: Any) -> Path:
        d = self.build_dir(tag)
        d.mkdir(parents=True, exist_ok=True)
        apps = normalise_apps_json(apps_json)
        write_if_changed(d / "apps.json", json.dumps(apps, indent=2) + "\n", mode=0o600)
        return d

    def build_args(self, major: int, tag: str, frappe_branch: str | None = None,
                   python: str | None = None, node: str | None = None,
                   push: bool = False) -> list[str]:
        if major not in SUPPORTED_MAJORS:
            raise v.ValidationError(f"unsupported Frappe major {major}")
        d = self.build_dir(tag)
        args = [
            "bash", str(self.settings.build_script),
            "--major", str(major),
            "--apps-json", str(d / "apps.json"),
            "--tag", tag,
            "--context", str(self.settings.build_context),
            "--log", str(d / "build.log"),
        ]
        if frappe_branch:
            args += ["--frappe-branch", frappe_branch]
        if python:
            args += ["--python", python]
        if node:
            args += ["--node", node]
        if push:
            args.append("--push")
        return args

    def build(self, major: int, tag: str, **kw: Any) -> Result:
        if not self.settings.build_script.exists():
            raise ImageError(f"build script not found: {self.settings.build_script}")
        return self.runner.run(
            self.build_args(major, tag, **kw),
            cwd=self.settings.images_dir,
            timeout=self.settings.images.build_timeout_seconds,
        )
