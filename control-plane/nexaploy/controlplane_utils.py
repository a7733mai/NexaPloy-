"""Pure logic (no frappe import) for the control plane's own lifecycle (ARCHITECTURE.md §5.9):
self-update (release lookup, availability, agent job params, progress) and the first-run setup
checklist on /dashboard/admin. Frappe glue: nexaploy/controlplane.py; API: nexaploy.api.controlplane.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping

DEFAULT_IMAGE_REPO = "ghcr.io/a7733mai/nexaploy-control-plane"
DEFAULT_RELEASE_REPO = "a7733mai/NexaPloy-"
CHANNELS = ("stable", "beta")
RELEASE_CACHE_SECONDS = 3600
IMAGE_RE = re.compile(
	r"^(?:[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?/)?"
	r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*"
	r":[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$"
)
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")
UPDATE_STEPS = ("prepare", "backup", "pull", "switch", "migrate", "health", "finalize")
TERMINAL = ("Success", "Failure")

HOSTED_EXPLANATION = (
	"This control plane does not run on a server managed by Nexaploy (it was not installed with "
	"install.sh --with-control-plane), so it cannot update itself. Update it where it runs: back up the "
	"site, pull the new image (docker compose pull), restart (docker compose up -d) and run "
	"bench --site <site> migrate."
)


class ControlPlaneError(Exception):
	def __init__(self, message, *params):
		super().__init__(message)
		self.message = message
		self.params = params


# --------------------------------------------------------------------------- releases
def version_key(tag: str) -> tuple:
	m = VERSION_RE.match((tag or "").strip())
	if not m:
		return (-1,)
	major, minor, patch, pre = m.groups()
	return (int(major), int(minor), int(patch), 1 if pre is None else 0, pre or "")


def pick_release(releases: Iterable[Mapping[str, Any]] | None, channel: str = "stable") -> dict | None:
	"""Newest release of a GitHub releases API list (drafts ignored; pre-releases only on beta)."""
	best = None
	for r in releases or []:
		if not isinstance(r, Mapping) or r.get("draft") or (r.get("prerelease") and channel != "beta"):
			continue
		if version_key(r.get("tag_name") or "") == (-1,):
			continue
		if best is None or version_key(r["tag_name"]) > version_key(best["tag_name"]):
			best = r
	if not best:
		return None
	return {
		"version": best["tag_name"].lstrip("v"),
		"tag": best["tag_name"],
		"name": best.get("name") or best["tag_name"],
		"url": best.get("html_url"),
		"published_at": best.get("published_at"),
		"prerelease": bool(best.get("prerelease")),
		"notes": (best.get("body") or "")[:4000],
	}


def image_for_version(version: str, repo: str | None = None) -> str:
	return f"{(repo or DEFAULT_IMAGE_REPO).rstrip('/')}:{str(version).lstrip('v')}"


def is_newer(latest: str | None, current: str | None) -> bool:
	if not latest:
		return False
	return version_key(latest) > version_key(current or "")


def release_settings(settings: Mapping[str, Any]) -> dict[str, str]:
	channel = settings.get("control_plane_release_channel") or "stable"
	repo = (settings.get("control_plane_image_repo") or "").strip() or DEFAULT_IMAGE_REPO
	release_repo = (settings.get("control_plane_release_repo") or "").strip() or DEFAULT_RELEASE_REPO
	return {
		"channel": channel if channel in CHANNELS else "stable",
		"image_repo": repo.rstrip("/"),
		"release_repo": release_repo if REPO_RE.match(release_repo) else DEFAULT_RELEASE_REPO,
	}


def releases_api_url(release_repo: str) -> str:
	return f"https://api.github.com/repos/{release_repo}/releases?per_page=30"


# --------------------------------------------------------------------------- update
def current_image(benches_report: Any, bench: str | None) -> str | None:
	"""Image of the control plane bench from the host's heartbeat bench report."""
	if isinstance(benches_report, str):
		try:
			benches_report = json.loads(benches_report)
		except ValueError:
			return None
	for b in benches_report or []:
		if isinstance(b, Mapping) and b.get("name") == bench:
			return b.get("image")
	return None


def update_mode(host: Mapping[str, Any] | None) -> dict[str, Any]:
	"""Can this control plane update itself? Only when it runs as a bench of a managed server."""
	if not host or not host.get("control_plane_bench") or not host.get("control_plane_site"):
		return {"mode": "hosted", "can_update": False, "reason": HOSTED_EXPLANATION}
	if host.get("status") not in (None, "Active"):
		return {"mode": "all-in-one", "can_update": False,
		        "reason": f"The server hosting the control plane is {host.get('status')}."}
	return {"mode": "all-in-one", "can_update": True, "reason": None}


def resolve_target(image: str | None, version: str | None, latest: Mapping | None, repo: str) -> str:
	"""Image to update to: an explicit image, else <repo>:<version>, else the latest release."""
	image = (image or "").strip()
	if image:
		if not IMAGE_RE.match(image):
			raise ControlPlaneError("Invalid image {0}", image)
		return image
	version = (version or "").strip()
	if version:
		if version_key(version) == (-1,):
			raise ControlPlaneError("Invalid version {0}", version)
		return image_for_version(version, repo)
	if latest and latest.get("version"):
		return image_for_version(latest["version"], repo)
	raise ControlPlaneError("No release found; enter an image or a version")


def update_params(host: Mapping[str, Any], image: str, previous_image: str | None) -> dict[str, Any]:
	return {
		"bench": host["control_plane_bench"],
		"site": host["control_plane_site"],
		"image": image,
		"previous_image": previous_image or None,
		"backup": True,
		"upload": True,
	}


def job_progress(job: Mapping[str, Any] | None) -> dict[str, Any] | None:
	"""Agent Job -> what the admin page shows while the control plane updates itself."""
	if not job:
		return None
	result = job.get("result") or {}
	if isinstance(result, str):
		try:
			result = json.loads(result)
		except ValueError:
			result = {}
	steps = sorted(job.get("steps") or [], key=lambda s: s.get("step_idx") or 0)
	current = next((s.get("step_name") for s in steps if s.get("status") == "Running"), None)
	done = [s.get("step_name") for s in steps if s.get("status") in ("Success", "Skipped")]
	rollback = result.get("rollback") or {}
	status = job.get("status")
	if status == "Success":
		state = "up_to_date" if result.get("up_to_date") else "updated"
	elif status == "Failure":
		state = "rolled_back" if result.get("rolled_back") else ("rollback_incomplete" if rollback else "failed")
	else:
		state = "running" if status in ("Running", "Queued") else "pending"
	return {
		"name": job.get("name"),
		"status": status,
		"state": state,
		"finished": status in TERMINAL,
		"step": current,
		"done": done,
		"total": len(UPDATE_STEPS),
		"image": result.get("image") or (job.get("params") or {}).get("image"),
		"previous_image": result.get("previous_image"),
		"backup_id": result.get("backup_id"),
		"rollback": rollback or None,
		"error": job.get("error"),
		"creation": job.get("creation"),
	}


# --------------------------------------------------------------------------- setup checklist
SETUP_ITEMS = (
	# key, label, link (admin tab or desk/portal URL)
	("platform", "Platform name and root domain", "/dashboard/admin?tab=features"),
	("currency", "Billing currency", "/dashboard/admin?tab=features"),
	("payments", "Payment gateway (Stripe or Paymob)", "/dashboard/admin?tab=features"),
	("backups", "Backup storage", "/dashboard/admin?tab=storage"),
	("email", "Outgoing e-mail", "/dashboard/admin?tab=email"),
	("plans", "First plans", "/dashboard/admin?tab=features"),
	("probe", "External uptime probe", "/app/uptime-probe/new"),
	("test_site", "Create and verify a test site", "/dashboard/sites"),
)


def setup_checklist(facts: Mapping[str, Any]) -> dict[str, Any]:
	"""Each item done/not done from real settings (`facts` gathered by controlplane.setup_facts)."""
	f = facts or {}
	done = {
		"platform": bool(f.get("platform_name")) and bool(f.get("root_domain")),
		"currency": bool(f.get("billing_enabled")) and bool(f.get("currency")),
		"payments": bool(f.get("stripe_ready") or f.get("paymob_ready")),
		"backups": bool(f.get("backup_s3") or f.get("backup_oauth") or f.get("storage_profiles")),
		"email": bool(f.get("relay_enabled") or f.get("outgoing_account")),
		"plans": int(f.get("plans") or 0) > 0,
		"probe": int(f.get("external_probes_seen") or 0) > 0,
		"test_site": int(f.get("active_sites") or 0) > 0,
	}
	items = [{"key": k, "label": label, "link": link, "done": done[k]} for k, label, link in SETUP_ITEMS]
	completed = sum(1 for i in items if i["done"])
	dismissed = bool(f.get("dismissed"))
	return {
		"items": items,
		"completed": completed,
		"total": len(items),
		"complete": completed == len(items),
		"dismissed": dismissed,
		"show": not dismissed and completed < len(items),
	}
