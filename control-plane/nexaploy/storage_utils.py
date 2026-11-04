"""Pure logic for offsite attachments, DR replica and trash (ARCHITECTURE.md §15).

No frappe import: unit-tested with plain pytest (control-plane/tests/test_storage.py).
The agent mirrors the provider presets and the policy rules in
``agent/nexaploy_agent/runtime/storage.py``; keep both in sync.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,62}$")
REGION_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
ENDPOINT_RE = re.compile(r"^(https?://)?[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$")
PREFIX_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
SIZE_RE = re.compile(r"^[0-9]{1,6}[KMGT]?$")
TRASH_ITEM_RE = re.compile(r"^\d{8}T\d{6}Z(/(public|private)(/[^\r\n\0\\]+)?)?/?$")

# preset -> (label, required fields)
PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
	"AWS": {"label": "Amazon S3", "needs": ("region",)},
	"IDrive": {"label": "iDrive e2", "needs": ("endpoint",)},
	"Wasabi": {"label": "Wasabi", "needs": ("region",)},
	"B2": {"label": "Backblaze B2", "needs": ("region",)},
	"R2": {"label": "Cloudflare R2", "needs": ("endpoint",)},
	"MinIO": {"label": "MinIO", "needs": ("endpoint",)},
	"Hetzner": {"label": "Hetzner Object Storage", "needs": ("region",)},
	"Other": {"label": "Other S3-compatible", "needs": ("endpoint",)},
}
ADDRESSING = {"Preset default": None, "Path style": True, "Virtual-hosted style": False}
ATTACHMENT_MODES = ("Local", "Offloaded")
COMPARE_MODES = ("size", "modtime", "checksum")
# Policy.status values (Site Storage Policy.status)
BUSY_STATUSES = ("Migrating", "Migrating Back")

# add-on key -> Storage Settings field holding its plan
ADDON_FIELDS = {
	"offload": "offload_addon_plan",
	"replica": "replica_addon_plan",
	"trash": "trash_addon_plan",
}
ADDON_DEFAULT_PLANS = {
	"offload": "Offsite Attachments",
	"replica": "DR Replica",
	"trash": "Trash Bin",
}


# --------------------------------------------------------------------------- profiles


def validate_profile(p: Mapping[str, Any]) -> list[str]:
	"""Errors for a Storage Profile dict (profile_name, provider, endpoint, region, bucket,
	path_prefix, access_key, has_secret). Empty list = valid."""
	errors = []
	name = (p.get("profile_name") or "").strip()
	if not PROFILE_NAME_RE.match(name):
		errors.append("Profile name must be 2-41 lowercase letters, numbers or dashes")
	preset = p.get("provider") or "Other"
	if preset not in PROVIDER_PRESETS:
		errors.append("Unknown storage provider")
		return errors
	if not BUCKET_RE.match((p.get("bucket") or "").strip()):
		errors.append("Invalid bucket name")
	region = (p.get("region") or "").strip()
	if region and not REGION_RE.match(region):
		errors.append("Invalid region")
	endpoint = (p.get("endpoint") or "").strip()
	if endpoint and not ENDPOINT_RE.match(endpoint):
		errors.append("Invalid endpoint")
	prefix = (p.get("path_prefix") or "").strip().strip("/")
	if prefix and (not PREFIX_RE.match(prefix) or any(s in ("", ".", "..") for s in prefix.split("/"))):
		errors.append("Invalid path prefix")
	key = p.get("access_key") or ""
	if not key.strip() or any(c in key for c in "\r\n\0"):
		errors.append("Access key is required")
	if not p.get("has_secret"):
		errors.append("Secret key is required")
	for need in PROVIDER_PRESETS[preset]["needs"]:
		if not (p.get(need) or "").strip():
			errors.append("{0} needs a {1}".format(PROVIDER_PRESETS[preset]["label"], need))
	return errors


def profile_job_params(p: Mapping[str, Any], secret: str) -> dict[str, Any]:
	"""Storage Profile -> the agent's profile params (secret = a __secret__ placeholder)."""
	return {
		"name": p["profile_name"] if p.get("profile_name") else p["name"],
		"preset": p.get("provider") or "Other",
		"bucket": (p.get("bucket") or "").strip(),
		"access_key_id": (p.get("access_key") or "").strip(),
		"secret_access_key": secret,
		"endpoint": (p.get("endpoint") or "").strip() or None,
		"region": (p.get("region") or "").strip() or None,
		"path_style": ADDRESSING.get(p.get("addressing") or "Preset default"),
		"prefix": (p.get("path_prefix") or "").strip().strip("/") or None,
	}


def profile_fingerprint_fields() -> tuple[str, ...]:
	return ("provider", "endpoint", "region", "addressing", "bucket", "path_prefix", "access_key")


def backup_settings_from_profile(p: Mapping[str, Any]) -> dict[str, Any]:
	"""Storage Profile -> Nexaploy Settings backup_* values (DB backups to that bucket).

	The agent's backup remote is plain S3 (rclone provider string), so presets map to an
	rclone provider and the endpoint derived the same way the agent does for mounts."""
	preset = p.get("provider") or "Other"
	region = (p.get("region") or "").strip() or None
	endpoint = (p.get("endpoint") or "").strip() or None
	provider = {"AWS": "AWS", "IDrive": "IDrive", "Wasabi": "Wasabi", "R2": "Cloudflare", "MinIO": "Minio"}.get(
		preset, "Other"
	)
	if not endpoint:
		endpoint = {
			"Wasabi": "s3.{region}.wasabisys.com",
			"B2": "s3.{region}.backblazeb2.com",
			"Hetzner": "{region}.your-objectstorage.com",
		}.get(preset, "").format(region=region or "") or None
	if preset == "R2" and not region:
		region = "auto"
	return {
		"backup_remote_type": "s3",
		"backup_provider": provider,
		"backup_endpoint": endpoint,
		"backup_region": region,
		"backup_bucket": (p.get("bucket") or "").strip(),
		"backup_prefix": (p.get("path_prefix") or "").strip().strip("/") or None,
		"backup_access_key": (p.get("access_key") or "").strip(),
	}


def profile_usable(profile: Mapping[str, Any] | None, team: str | None) -> bool:
	"""A site of `team` may use a profile that is enabled and either the platform's or the team's."""
	if not profile or not profile.get("enabled"):
		return False
	owner = profile.get("team")
	return not owner or owner == team


# --------------------------------------------------------------------------- policy


def validate_policy(
	mode: str,
	primary: str | None,
	replica: str | None,
	trash: str | None,
	trash_retention_days: Any,
	max_retention_days: int = 365,
) -> list[str]:
	"""Errors for a Site Storage Policy (same rules as the agent's validate_policy)."""
	errors = []
	if mode not in ATTACHMENT_MODES:
		return ["Unknown attachments mode"]
	if mode == "Local":
		return errors
	if not primary:
		errors.append("Choose a primary storage profile")
	if replica and replica == primary:
		errors.append("The replica must be a different profile than the primary")
	if trash and not replica:
		errors.append("The trash bin needs a replica")
	if trash and trash in (primary, replica):
		errors.append("The trash must be a different profile than the primary and the replica")
	if trash:
		try:
			days = int(trash_retention_days or 0)
		except (TypeError, ValueError):
			days = 0
		if days < 1 or days > max_retention_days:
			errors.append("Trash retention must be between 1 and {0} days".format(max_retention_days))
	return errors


def policy_job_params(policy: Mapping[str, Any], profiles: Mapping[str, Mapping[str, Any]],
                      settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
	"""Site Storage Policy -> params shared by the site.storage_* agent jobs.

	`profiles` maps profile name -> agent profile params (profile_job_params)."""
	settings = settings or {}
	names = [policy.get("primary_profile"), policy.get("replica_profile"), policy.get("trash_profile")]
	params = {
		"site": policy["site"],
		"primary": policy.get("primary_profile"),
		"replica": policy.get("replica_profile") or None,
		"trash": policy.get("trash_profile") or None,
		"trash_retention_days": int(policy.get("trash_retention_days") or 0) or None,
		"active": policy.get("active_target") or "primary",
		"profiles": [profiles[n] for n in names if n],
	}
	cache = (settings.get("cache_max_size") or "").strip()
	if cache and SIZE_RE.match(cache):
		params["cache_max_size"] = cache
	return params


def desired_addons(policy: Mapping[str, Any] | None, plans: Mapping[str, str | None]) -> dict[str, str]:
	"""{addon key: plan} a site must be subscribed to. `plans` maps addon key -> plan name."""
	if not policy or policy.get("attachments_mode") != "Offloaded":
		return {}
	out = {}
	if plans.get("offload"):
		out["offload"] = plans["offload"]
	if policy.get("replica_profile") and plans.get("replica"):
		out["replica"] = plans["replica"]
	if policy.get("trash_profile") and plans.get("trash"):
		out["trash"] = plans["trash"]
	return out


def feature_errors(policy: Mapping[str, Any], settings: Mapping[str, Any]) -> list[str]:
	"""Features the operator has not enabled (Storage Settings)."""
	errors = []
	if policy.get("attachments_mode") == "Offloaded" and not settings.get("enabled"):
		errors.append("Offsite attachments are not available")
	if policy.get("replica_profile") and not settings.get("replica_enabled"):
		errors.append("The DR replica is not available")
	if policy.get("trash_profile") and not settings.get("trash_enabled"):
		errors.append("The trash bin is not available")
	return errors


# --------------------------------------------------------------------------- scheduling


def replication_due(last_sync_at: datetime | None, interval_minutes: int, now: datetime) -> bool:
	if not last_sync_at:
		return True
	interval = max(5, int(interval_minutes or 15))
	# small slack so a 15-min schedule on a 5-min scheduler does not slip to 20 min
	return now - last_sync_at >= timedelta(minutes=interval) - timedelta(seconds=60)


def group_by_server(rows: Iterable[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
	out: dict[str, list[Mapping[str, Any]]] = {}
	for row in rows:
		if row.get("server"):
			out.setdefault(row["server"], []).append(row)
	return out


def replicate_site_params(policy: Mapping[str, Any], settings: Mapping[str, Any]) -> dict[str, Any] | None:
	"""One entry of server.storage_replicate `sites` (None when the site is not replicated)."""
	if policy.get("attachments_mode") != "Offloaded" or not policy.get("replica_profile"):
		return None
	if policy.get("status") != "Offloaded" or (policy.get("active_target") or "primary") != "primary":
		return None  # migrating, failed over or broken: never sync from a stale primary
	compare = settings.get("replication_compare") or "size"
	return {
		"site": policy["site"],
		"primary": policy["primary_profile"],
		"replica": policy["replica_profile"],
		"trash": policy.get("trash_profile") or None,
		"max_delete": int(settings.get("replication_max_delete") or 1000),
		"compare": compare if compare in COMPARE_MODES else "size",
	}


def valid_trash_item(item: str | None) -> bool:
	if not item or ".." in item.split("/"):
		return False
	return bool(TRASH_ITEM_RE.match(item.strip()))


def parse_agent_ts(value: str | None) -> datetime | None:
	"""Agent timestamps are ``YYYYmmddTHHMMSSZ`` (UTC)."""
	if not value:
		return None
	try:
		return datetime.strptime(value, "%Y%m%dT%H%M%SZ")
	except ValueError:
		return None


def replication_outcome(entry: Mapping[str, Any] | None) -> dict[str, Any]:
	"""server.storage_replicate result entry -> Site Storage Policy field values."""
	entry = entry or {}
	status = {"success": "Success", "failure": "Failure", "skipped": "Skipped"}.get(entry.get("status"), "Failure")
	return {
		"last_sync_status": status,
		"last_sync_error": (entry.get("error") or entry.get("reason") or "")[:1000] if status != "Success" else "",
		"last_trashed": int(entry.get("trashed") or 0),
	}


# --------------------------------------------------------------------------- operator settings

SETTINGS_FIELDS: dict[str, type] = {
	"enabled": bool,
	"allow_byo_profiles": bool,
	"replica_enabled": bool,
	"trash_enabled": bool,
	"default_primary_profile": str,
	"default_replica_profile": str,
	"default_trash_profile": str,
	"backup_storage_profile": str,
	"replication_interval_minutes": int,
	"replication_max_delete": int,
	"replication_compare": str,
	"default_trash_retention_days": int,
	"max_trash_retention_days": int,
	"cache_max_size": str,
	"keep_local_copy": bool,
	"offload_addon_plan": str,
	"replica_addon_plan": str,
	"trash_addon_plan": str,
}


def clean_settings_update(values: Mapping[str, Any]) -> dict[str, Any]:
	"""Whitelist + coerce an admin settings update. Raises ValueError on bad values."""
	out: dict[str, Any] = {}
	for key, value in (values or {}).items():
		if key not in SETTINGS_FIELDS:
			raise ValueError("Unknown setting {0}".format(key))
		kind = SETTINGS_FIELDS[key]
		if kind is bool:
			out[key] = 1 if value in (True, 1, "1", "true", "True", "on") else 0
		elif kind is int:
			try:
				out[key] = int(value)
			except (TypeError, ValueError):
				raise ValueError("{0} must be a number".format(key)) from None
			if out[key] < 0:
				raise ValueError("{0} cannot be negative".format(key))
		else:
			out[key] = (str(value).strip() if value not in (None, "") else None)
	if "replication_interval_minutes" in out and out["replication_interval_minutes"] < 5:
		raise ValueError("Replication interval must be at least 5 minutes")
	if out.get("replication_compare") and out["replication_compare"] not in COMPARE_MODES:
		raise ValueError("Compare mode must be size, modtime or checksum")
	if out.get("cache_max_size") and not SIZE_RE.match(out["cache_max_size"]):
		raise ValueError("Cache size must look like 512M or 2G")
	if "max_trash_retention_days" in out and out["max_trash_retention_days"] < 1:
		raise ValueError("Max trash retention must be at least 1 day")
	return out
