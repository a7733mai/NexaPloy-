"""Pure helpers for the Nexaploy control plane.

This module MUST NOT import frappe: it is unit-tested with plain pytest
(control-plane/tests/test_utils.py) outside a bench. Everything that needs the
database lives in the DocType controllers, nexaploy/api/* and nexaploy/agent_callbacks.py.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

# --------------------------------------------------------------------------- names (§2)

SITE_NAME_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
BENCH_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{2,30}$")
# Release Group titles are used inside image tags (nexaploy/bench:<release_group>-<build_id>),
# so they follow the same slug rule as bench names.
RELEASE_GROUP_RE = BENCH_NAME_RE
APP_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,60}$")

SUPPORTED_MAJORS = ("14", "15", "16")


def is_valid_site_name(name: str | None) -> bool:
	return bool(name) and SITE_NAME_RE.match(name) is not None


def is_valid_domain(domain: str | None) -> bool:
	"""Custom domains follow the same FQDN rule as site names."""
	return is_valid_site_name(domain)


def is_valid_bench_name(name: str | None) -> bool:
	return bool(name) and BENCH_NAME_RE.match(name) is not None


def is_valid_release_group_name(name: str | None) -> bool:
	return bool(name) and RELEASE_GROUP_RE.match(name) is not None


def is_valid_app_name(name: str | None) -> bool:
	return bool(name) and APP_NAME_RE.match(name) is not None


def site_slug(site: str) -> str:
	"""§2: slug = site with '.' -> '-'."""
	return site.replace(".", "-")


# --------------------------------------------------------------------------- tokens

TOKEN_BYTES = 32


def generate_token(nbytes: int = TOKEN_BYTES) -> str:
	"""URL-safe random token.

	Never contains ':' - important because Frappe's own `Authorization: token <key>:<secret>`
	parser splits on ':' and would otherwise try to treat it as an API key pair.
	"""
	token = secrets.token_urlsafe(nbytes)
	assert ":" not in token
	return token


def hash_token(token: str) -> str:
	"""sha256 hex digest; the only form in which tokens are stored."""
	return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(token: str | None, token_hash: str | None) -> bool:
	"""Constant-time comparison of sha256(token) against a stored hash."""
	if not token or not token_hash:
		return False
	return hmac.compare_digest(hash_token(token), token_hash)


def parse_authorization_header(header: str | None) -> str | None:
	"""Return <T> from `Authorization: token <T>` (case-insensitive scheme), else None.

	`token key:secret` (Frappe API key pairs) is rejected - agent tokens never contain ':'.
	"""
	if not header:
		return None
	parts = header.strip().split(None, 1)
	if len(parts) != 2 or parts[0].lower() != "token":
		return None
	token = parts[1].strip()
	if not token or ":" in token or " " in token:
		return None
	return token


def generate_server_id() -> str:
	return "srv-" + secrets.token_hex(4)


def install_command(host: str, token: str) -> str:
	"""One-liner shown to a BYOS customer (install.sh lives in agent/install/install.sh)."""
	host = host.rstrip("/")
	return f"curl -fsSL {host}/install.sh | sudo bash -s -- --token {token} --control-plane {host}"


def token_expired(expires_on: datetime | None, now: datetime) -> bool:
	return expires_on is None or expires_on <= now


# --------------------------------------------------------------------------- job statuses (§5.3 / §8)

JOB_STATUS_MAP = {
	"queued": "Queued",
	"running": "Running",
	"success": "Success",
	"failure": "Failure",
	# The agent may cancel a job; the control plane has no Cancelled state (§8) -> Failure.
	"cancelled": "Failure",
	"canceled": "Failure",
}

JOB_STATUS_RANK = {"Pending": 0, "Queued": 1, "Running": 2, "Success": 3, "Failure": 3}
TERMINAL_JOB_STATUSES = ("Success", "Failure")
STEP_STATUSES = ("pending", "running", "success", "failure", "skipped")


def map_agent_status(status: str | None) -> str:
	"""Agent status (queued/running/success/failure[/cancelled]) -> Agent Job status."""
	try:
		return JOB_STATUS_MAP[(status or "").strip().lower()]
	except KeyError:
		raise ValueError(f"unknown agent job status: {status!r}") from None


def merge_job_status(current: str, incoming: str) -> str:
	"""Status transitions only move forward; terminal statuses are sticky.

	Updates may arrive out of order (retries with backoff), so a late 'running' must not
	resurrect a job that already reported success/failure.
	"""
	if current in TERMINAL_JOB_STATUSES:
		return current
	if JOB_STATUS_RANK.get(incoming, -1) >= JOB_STATUS_RANK.get(current, 0):
		return incoming
	return current


def normalize_step_status(status: str | None) -> str:
	s = (status or "pending").strip().lower()
	return s if s in STEP_STATUSES else "pending"


def parse_agent_datetime(value: Any) -> datetime | None:
	"""Parse an ISO-8601 timestamp from the agent into a naive UTC datetime.

	Accepts 'Z' suffixes and offsets; naive input is assumed to be UTC already.
	"""
	if not value:
		return None
	if isinstance(value, datetime):
		dt = value
	else:
		s = str(value).strip()
		if s.endswith("Z"):
			s = s[:-1] + "+00:00"
		try:
			dt = datetime.fromisoformat(s)
		except ValueError:
			return None
	if dt.tzinfo is not None:
		dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
	return dt


def heartbeat_stale(last_heartbeat: datetime | None, now: datetime, minutes: int = 5) -> bool:
	return last_heartbeat is None or (now - last_heartbeat) > timedelta(minutes=minutes)


# --------------------------------------------------------------------------- release groups / marketplace


def frappe_branch_for(major: str | int, override: str | None = None) -> str:
	return override or f"version-{major}"


def release_for_major(releases: Iterable[Mapping[str, Any]], major: str | int) -> Mapping[str, Any] | None:
	"""First Marketplace App Release whose frappe_major matches."""
	major = str(major)
	for rel in releases or ():
		if str(rel.get("frappe_major")) == major:
			return rel
	return None


def check_compatibility(major: str | int, apps: Sequence[Mapping[str, Any]]) -> list[str]:
	"""Validate a Release Group's app list against the marketplace.

	`apps` items: {app_name, approved, releases:[{frappe_major, branch, commit}], branch?}.
	Returns a list of human-readable error strings (empty == compatible).
	"""
	errors: list[str] = []
	major = str(major)
	if major not in SUPPORTED_MAJORS:
		errors.append(f"Unsupported Frappe major {major}; expected one of {', '.join(SUPPORTED_MAJORS)}")
	seen: set[str] = set()
	for app in apps:
		name = app.get("app_name") or "?"
		if name in seen:
			errors.append(f"App {name} is listed more than once")
			continue
		seen.add(name)
		if name == "frappe":
			errors.append("Do not add frappe as an app; it is selected by frappe_major/frappe_branch")
			continue
		if not app.get("approved"):
			errors.append(f"App {name} is not approved in the marketplace")
		if release_for_major(app.get("releases") or (), major) is None:
			errors.append(f"App {name} has no release for Frappe {major}")
	return errors


def build_apps_json(apps: Sequence[Mapping[str, Any]], major: str | int | None = None) -> list[dict[str, str]]:
	"""apps.json for image-builder (§4): list of {url, branch}.

	Each item needs `repo_url` and either `branch` or (with `major`) `releases` to pick
	the default branch from. frappe itself is never included (it comes from FRAPPE_BRANCH).
	"""
	out: list[dict[str, str]] = []
	for app in apps:
		if app.get("app_name") == "frappe":
			continue
		url = app.get("repo_url")
		branch = app.get("branch")
		if not branch and major is not None:
			rel = release_for_major(app.get("releases") or (), major)
			branch = rel.get("branch") if rel else None
		if not url or not branch:
			raise ValueError(f"app {app.get('app_name')!r} needs repo_url and branch")
		out.append({"url": url, "branch": branch})
	return out


def compatible_apps(apps: Iterable[Mapping[str, Any]], major: str | int | None = None) -> list[dict[str, Any]]:
	"""Marketplace listing filter: approved apps, releases restricted to `major` if given."""
	result = []
	for app in apps:
		if not app.get("approved"):
			continue
		releases = list(app.get("releases") or [])
		if major is not None and str(major) != "":
			releases = [r for r in releases if str(r.get("frappe_major")) == str(major)]
			if not releases:
				continue
		item = dict(app)
		item["releases"] = releases
		result.append(item)
	return result


def make_build_id(now: datetime) -> str:
	"""e.g. 20261103T2130 (§2 image tag example). Seconds appended to avoid collisions."""
	return now.strftime("%Y%m%dT%H%M%S")


def image_tag(release_group: str, build_id: str) -> str:
	return f"nexaploy/bench:{release_group}-{build_id}"


# --------------------------------------------------------------------------- site moves (§1 rule)


def plan_site_move(same_server: bool) -> list[str]:
	"""Ordered agent job kinds for moving a site to another bench (e.g. v14 -> v15).

	Always backup -> restore on the target, never in place (§1). site.restore runs
	`migrate` afterwards (§5.5).
	- Different server: create + restore on the target first, drop the source last, so the
	  old site keeps serving until the new one is ready.
	- Same server: the agent resolves a site's bench by scanning benches/*/sites/<s>, so the
	  same name cannot exist in two benches at once -> drop the source (skip_backup, we
	  just made one) before creating on the target.
	"""
	if same_server:
		return ["site.backup", "site.drop", "site.create", "site.restore"]
	return ["site.backup", "site.create", "site.restore", "site.drop"]


# --------------------------------------------------------------------------- misc

SECRET_KEY_RE = re.compile(r"password|secret|token|key", re.IGNORECASE)


def redact_params(params: Any) -> Any:
	"""Mirror of the agent's redaction rule (§5.3) for display/logging."""
	if isinstance(params, Mapping):
		return {k: ("*****" if SECRET_KEY_RE.search(str(k)) else redact_params(v)) for k, v in params.items()}
	if isinstance(params, list):
		return [redact_params(v) for v in params]
	return params


def extract_backup_info(result: Any) -> dict[str, Any]:
	"""Pull {remote_path, size, created_at} out of a site.backup job result.

	The agent's result is expected to carry `remote_path` (§5.5:
	<remote>:<prefix>/<bench>/<s>/<ts>/); tolerate a few shapes, including the manifest
	(`{created_at, files:[{name,size,sha256}]}`) nested under `manifest`.
	"""
	info: dict[str, Any] = {"remote_path": None, "size": None, "created_at": None}
	if not isinstance(result, Mapping):
		return info
	manifest = result.get("manifest") if isinstance(result.get("manifest"), Mapping) else {}
	info["remote_path"] = (
		result.get("remote_path") or result.get("remote") or manifest.get("remote_path") or result.get("path")
	)
	size = result.get("size")
	if size is None:
		files = result.get("files") or manifest.get("files") or []
		sizes = [f.get("size") for f in files if isinstance(f, Mapping) and isinstance(f.get("size"), int)]
		size = sum(sizes) if sizes else None
	info["size"] = size
	info["created_at"] = parse_agent_datetime(result.get("created_at") or manifest.get("created_at"))
	return info


# --------------------------------------------------------------------------- secret placeholders
# Secrets (e.g. a site's admin password) are never written into Agent Job.params. A
# placeholder "__secret__:<doctype>:<name>:<fieldname>" is stored instead and resolved from
# the Password field only when `poll` hands the job to the agent.

SECRET_PREFIX = "__secret__:"


def secret_ref(doctype: str, name: str, fieldname: str) -> str:
	for part in (doctype, name, fieldname):
		if ":" in part:
			raise ValueError("secret reference parts must not contain ':'")
	return f"{SECRET_PREFIX}{doctype}:{name}:{fieldname}"


def parse_secret_ref(value: Any) -> tuple[str, str, str] | None:
	if not isinstance(value, str) or not value.startswith(SECRET_PREFIX):
		return None
	parts = value[len(SECRET_PREFIX) :].split(":")
	if len(parts) != 3 or not all(parts):
		return None
	return parts[0], parts[1], parts[2]


def resolve_secrets(params: Any, resolver) -> Any:
	"""Replace secret placeholders anywhere in params using resolver(doctype, name, field)."""
	if isinstance(params, Mapping):
		return {k: resolve_secrets(v, resolver) for k, v in params.items()}
	if isinstance(params, list):
		return [resolve_secrets(v, resolver) for v in params]
	ref = parse_secret_ref(params)
	if ref:
		return resolver(*ref)
	return params


# --------------------------------------------------------------------------- release groups with custom apps
# Release Group App rows are either a Marketplace App (unchanged rules, see check_compatibility)
# or a team's Custom App (private GitHub/other repo). Row dicts used below:
#   {source_type: "Marketplace"|"Custom", app_name, repo_url, branch, approved, releases,   # marketplace
#    custom_app, team, status, frappe_majors, provider, installation, repo, access_username}  # custom

MARKETPLACE_SOURCE = "Marketplace"
CUSTOM_SOURCE = "Custom"
APP_SOURCE_TYPES = (MARKETPLACE_SOURCE, CUSTOM_SOURCE)


def parse_frappe_majors(value: Any) -> list[str]:
	"""Custom App.frappe_majors ('14,15,16', '15 16', ['15']) -> sorted supported majors."""
	if value is None or value == "":
		return []
	items = value if isinstance(value, (list, tuple)) else re.split(r"[\s,;]+", str(value))
	result = {str(i).strip().lstrip("v") for i in items if str(i).strip()}
	return sorted((m for m in result if m in SUPPORTED_MAJORS), key=int)


def check_group_apps(major: str | int, group_team: str | None, rows: Sequence[Mapping[str, Any]]) -> list[str]:
	"""Validate a Release Group's mixed app list. Returns error strings (empty == ok).

	- Marketplace rows: check_compatibility (approved + release for the major).
	- Custom rows: the Custom App must belong to the group's team (so a shared/teamless
	  group can never use one), be Active, and - when it declares frappe_majors - support
	  the major. Names must be unique across both kinds and never be frappe.
	"""
	errors: list[str] = []
	market = [r for r in rows if (r.get("source_type") or MARKETPLACE_SOURCE) == MARKETPLACE_SOURCE]
	custom = [r for r in rows if r.get("source_type") == CUSTOM_SOURCE]
	unknown = [r for r in rows if (r.get("source_type") or MARKETPLACE_SOURCE) not in APP_SOURCE_TYPES]
	for r in unknown:
		errors.append(f"Unknown app source {r.get('source_type')!r}")
	errors.extend(check_compatibility(major, market))
	seen = {r.get("app_name") for r in market}
	major = str(major)
	for r in custom:
		name = r.get("app_name") or r.get("custom_app") or "?"
		if not r.get("custom_app"):
			errors.append(f"App {name}: select a custom app")
			continue
		if name in seen:
			errors.append(f"App {name} is listed more than once")
			continue
		seen.add(name)
		if name == "frappe":
			errors.append("Do not add frappe as an app; it is selected by frappe_major/frappe_branch")
			continue
		if not is_valid_app_name(r.get("app_name")):
			errors.append(f"Custom app {r.get('custom_app')} has no valid app name")
		if not group_team or r.get("team") != group_team:
			errors.append(f"Custom app {name} belongs to another team")
		if (r.get("status") or "Active") != "Active":
			errors.append(f"Custom app {name} is disabled")
		majors = parse_frappe_majors(r.get("frappe_majors"))
		if majors and major not in majors:
			errors.append(f"Custom app {name} does not support Frappe {major} (supports {', '.join(majors)})")
		if not r.get("repo_url"):
			errors.append(f"Custom app {name} has no repository URL")
		if not (r.get("branch")):
			errors.append(f"Custom app {name} has no branch")
	return errors


def build_group_apps_json(rows: Sequence[Mapping[str, Any]], major: str | int) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
	"""(apps_json, app_sources) for a Deploy Candidate.

	apps_json never contains credentials (custom repos use their plain https URL) and is
	what gets stored on the Deploy Candidate / sent with bench.create. app_sources carries
	the non-secret metadata nexaploy.github_secrets needs to mint tokens at poll time.
	"""
	apps_json: list[dict[str, str]] = []
	sources: list[dict[str, Any]] = []
	for r in rows:
		if r.get("source_type") == CUSTOM_SOURCE:
			url, branch = r.get("repo_url"), r.get("branch")
			if not url or not branch:
				raise ValueError(f"custom app {r.get('app_name')!r} needs repo_url and branch")
			item = {"url": url, "branch": branch}
			src = {
				"source_type": CUSTOM_SOURCE,
				"app_name": r.get("app_name"),
				"custom_app": r.get("custom_app"),
				"provider": r.get("provider") or "GitHub",
				"installation": r.get("installation"),
				"repo": r.get("repo"),
				"access_username": r.get("access_username"),
			}
		else:
			item = build_apps_json([r], major)[0] if r.get("app_name") != "frappe" else None
			if item is None:
				continue
			src = {"source_type": MARKETPLACE_SOURCE, "app_name": r.get("app_name")}
		apps_json.append(item)
		src.update(item)
		sources.append(src)
	return apps_json, sources


def custom_app_needs_update(rows: Iterable[Mapping[str, Any]], repo: str, branch: str) -> list[str]:
	"""Release Group names whose Custom App rows track `repo`@`branch` (push webhook).

	rows: {parent, branch, repo} (Release Group App joined with Custom App.repo_full_name).
	"""
	repo = (repo or "").lower()
	groups = []
	for r in rows:
		if (r.get("repo") or "").lower() == repo and r.get("branch") == branch and r.get("parent") not in groups:
			groups.append(r.get("parent"))
	return groups


def unique_bench_name(release_group: str, existing: Iterable[str], suffix: str) -> str:
	"""<group>-<suffix> trimmed to the bench regex (^[a-z][a-z0-9-]{2,30}$), unique in existing."""
	suffix = re.sub(r"[^a-z0-9]", "", suffix.lower())[:6] or "1"
	base = release_group[: 31 - len(suffix) - 1].rstrip("-")
	name = f"{base}-{suffix}"
	existing = set(existing)
	n = 2
	while name in existing:
		tail = f"{suffix[: max(1, 6 - len(str(n)))]}{n}"
		name = f"{release_group[: 31 - len(tail) - 1].rstrip('-')}-{tail}"
		n += 1
	return name
