"""Pure helpers for the site lifecycle features (staging, move, transfer, rename).

This module MUST NOT import frappe: it is unit-tested with plain pytest
(control-plane/tests/test_lifecycle.py). nexaploy/api/site_lifecycle.py and the Site
controller do the database work.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping
from urllib.parse import quote

from nexaploy.utils import is_valid_site_name

# --------------------------------------------------------------------------- staging

STAGING_PREFIX = "staging-"
STAGING_CONFIG_KEY = "nexaploy_staging_of"  # written by the agent into the staging site_config


def staging_site_name(site_name: str) -> str:
	"""`erp.example.com` -> `staging-erp.example.com` (prefix on the first label).

	Raises ValueError when the result is not a valid site name (first label > 63 chars).
	"""
	name = str(site_name or "").strip().lower()
	if not is_valid_site_name(name):
		raise ValueError("Invalid site name")
	first, _, rest = name.partition(".")
	label = f"{STAGING_PREFIX}{first}"
	if len(label) > 63:
		raise ValueError("Site name is too long for a staging copy")
	out = f"{label}.{rest}"
	if not is_valid_site_name(out):
		raise ValueError("Site name is too long for a staging copy")
	return out


def clone_params(bench_name: str, source: str, target: str, scrub: bool = True) -> dict[str, Any]:
	"""Agent job `site.clone` params (ARCHITECTURE.md §10)."""
	return {"bench": bench_name, "site": source, "target_site": target, "scrub": bool(scrub)}


# --------------------------------------------------------------------------- move


def _major(value: Any) -> int | None:
	try:
		return int(str(value).strip())
	except (TypeError, ValueError):
		return None


def move_compatibility(source_major: Any, target_major: Any, allow_upgrade: bool = False) -> dict[str, Any]:
	"""May a site on Frappe `source_major` move to a bench on `target_major`?

	Same major: always. Higher major: only with `allow_upgrade` (the restore runs
	`migrate`). Lower major: never (Frappe cannot downgrade a database).
	Returns {ok, upgrade, reason}.
	"""
	src, dst = _major(source_major), _major(target_major)
	if src is None or dst is None:
		return {"ok": False, "upgrade": False, "reason": "Unknown Frappe version of the source or target bench"}
	if dst == src:
		return {"ok": True, "upgrade": False, "reason": None}
	if dst < src:
		return {
			"ok": False, "upgrade": False,
			"reason": f"Cannot move a v{src} site to a v{dst} bench (downgrades are not possible)",
		}
	if not allow_upgrade:
		return {
			"ok": False, "upgrade": True,
			"reason": f"Target bench runs v{dst}; moving from v{src} is an upgrade - confirm it with allow_upgrade",
		}
	return {"ok": True, "upgrade": True, "reason": None}


def plan_move(same_server: bool) -> list[str]:
	"""Ordered agent job kinds of a site move (generalised `Site.move_to_bench`).

	The target is restored straight from the backup (`site.restore` with `bench` creates
	the site and migrates it), never upgraded in place (§1).
	- Different server: restore on the target first, drop the source last, so the old
	  site keeps serving until the new one is ready (a failure before the drop leaves the
	  source untouched).
	- Same server: a site name can only live in one bench per server (the agent resolves
	  a site's bench by directory scan), so the source is dropped before the restore.
	"""
	if same_server:
		return ["site.backup", "site.drop", "site.restore"]
	return ["site.backup", "site.restore", "site.drop"]


def move_source_dropped(steps: list[str], idx: int) -> bool:
	"""True when the source site has already been dropped before step `idx` failed."""
	return "site.drop" in list(steps or [])[: max(0, int(idx or 0))]


def move_restore_params(site: str, bench_name: str, same_server: bool, backup_id: str | None,
                        remote_path: str | None) -> dict[str, Any]:
	"""`site.restore` params for the move target: restore as a new site on `bench_name`.

	Same server prefers the local backup (`backup_id`, no bucket needed); another server
	needs the uploaded copy (`remote_path`). Raises ValueError when the backup is unusable.
	"""
	params: dict[str, Any] = {"site": site, "bench": bench_name, "with_files": True}
	if same_server and backup_id:
		params["backup_id"] = backup_id
	elif remote_path:
		params["remote_path"] = remote_path
	elif same_server:
		raise ValueError("The move backup has neither a local id nor a remote copy")
	else:
		raise ValueError(
			"The move backup was not uploaded to the backup bucket, so the target server cannot fetch it. "
			"Configure backup storage in Nexaploy Settings."
		)
	return params


def dns_hint(site_name: str, domains: Iterable[str], root_domain: str | None, server_hostname: str | None,
             server_ip: str | None = None) -> dict[str, Any] | None:
	"""What DNS must change after a cross-server move. None when nothing points anywhere.

	Every host of the site (primary + custom domains) must resolve to the new server.
	Hosts under the platform root domain are managed by the platform operator; custom
	domains by the customer.
	"""
	root = str(root_domain or "").strip(".").lower()
	hosts = [site_name] + [d for d in (domains or []) if d and d != site_name]
	managed = [h for h in hosts if root and (h == root or h.endswith("." + root))]
	custom = [h for h in hosts if h not in managed]
	target = server_ip or server_hostname or ""
	if not hosts:
		return None
	return {
		"target": target,
		"record": "A" if server_ip else "CNAME",
		"managed_hosts": managed,
		"custom_hosts": custom,
		"message": (
			f"Point the DNS records of {', '.join(hosts)} to the new server {target}".strip()
			+ " (A record to its public IP, or a CNAME to its hostname). Until DNS is updated, "
			"visitors still reach the old server, where the site no longer exists."
		),
	}


# --------------------------------------------------------------------------- transfer

TRANSFER_TOKEN_BYTES = 32
TRANSFER_TTL_DAYS = 7
TRANSFER_STATUSES = ("Pending", "Accepted", "Rejected", "Cancelled")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TEAM_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,139}$")


def generate_transfer_token(nbytes: int = TRANSFER_TOKEN_BYTES) -> str:
	return secrets.token_urlsafe(nbytes)


def hash_transfer_token(token: str) -> str:
	"""sha256 hex; only the hash is stored (Site Transfer.token_hash)."""
	return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def verify_transfer_token(token: str | None, token_hash: str | None) -> bool:
	if not token or not token_hash:
		return False
	return hmac.compare_digest(hash_transfer_token(token), str(token_hash))


def transfer_expiry(now: datetime, days: int = TRANSFER_TTL_DAYS) -> datetime:
	return now + timedelta(days=days)


def transfer_expired(expires_on: datetime | None, now: datetime) -> bool:
	return expires_on is None or expires_on <= now


def transfer_url(base_url: str, token: str) -> str:
	return f"{str(base_url or '').rstrip('/')}/dashboard/transfer?token={quote(str(token), safe='')}"


def parse_transfer_target(value: Any) -> tuple[str, str]:
	"""'someone@example.com' -> ('email', ...); 'acme-corp' -> ('team', ...)."""
	text = str(value or "").strip()
	if not text:
		raise ValueError("Enter the email of the new owner or the name of the target team")
	if "@" in text:
		email = text.lower()
		if not EMAIL_RE.match(email) or len(email) > 254:
			raise ValueError("Enter a valid email address")
		return "email", email
	team = text.lower()
	if not TEAM_NAME_RE.match(team):
		raise ValueError("Enter a valid team name (as shown in the team switcher)")
	return "team", team


def is_shared_hosted(bench_team: Any, server_team: Any, server_ownership: Any) -> bool:
	"""A bench nobody owns on a platform (Hosted) server without a team."""
	return not bench_team and not server_team and server_ownership == "Hosted"


def transfer_block_reason(bench_team: Any, server_team: Any, server_ownership: Any) -> str | None:
	"""Why a site cannot change teams where it is, or None.

	A site may only change teams while it lives on a shared hosted bench: a bench or
	server owned by the source team (its own BYOS/hosted server) stays with that team, so
	the site must be moved to a shared bench (or a bench of the target team) first.
	"""
	if is_shared_hosted(bench_team, server_team, server_ownership):
		return None
	return (
		"This site runs on a server or bench owned by your team. Move it to a shared hosted bench "
		"first (Lifecycle > Move), then transfer it; the target team can move it to its own server afterwards."
	)


# --------------------------------------------------------------------------- rename


def validate_rename(old: str, new: str, taken: Iterable[str] = ()) -> str:
	"""Normalise and check a rename target. Raises ValueError."""
	new_name = str(new or "").strip().lower().strip(".")
	if not is_valid_site_name(new_name):
		raise ValueError("Site name must be a lowercase FQDN, e.g. erp.example.com")
	if new_name == str(old or "").strip().lower():
		raise ValueError("The new name is the current name")
	if new_name in set(taken or ()):
		raise ValueError(f"{new_name} is already taken")
	if new_name.startswith(STAGING_PREFIX):
		raise ValueError(f"Names starting with '{STAGING_PREFIX}' are reserved for staging sites")
	return new_name


def renamed_domains(domains: Iterable[str], old: str, new: str, keep_old: bool = True) -> list[str]:
	"""Custom domain list after a rename: the new name is no longer a custom domain, the
	old name becomes one (so existing links keep working) when `keep_old`."""
	out = [d for d in (domains or []) if d and d not in (new, old)]
	if keep_old:
		out.append(old)
	return out


def rename_params(bench_name: str, old: str, new: str, keep_old_domain: bool = True) -> dict[str, Any]:
	"""Agent job `site.rename` params (ARCHITECTURE.md §10)."""
	return {"bench": bench_name, "site": old, "new_name": new, "keep_old_domain": bool(keep_old_domain)}


def failed_step_index(steps: Iterable[Mapping[str, Any]]) -> int | None:
	"""Index of the first failed step of an agent job (None when no step failed)."""
	for step in steps or []:
		status = str(step.get("status") or "").lower()
		if status == "failure":
			try:
				return int(step.get("step_idx", step.get("idx")))
			except (TypeError, ValueError):
				return None
	return None
