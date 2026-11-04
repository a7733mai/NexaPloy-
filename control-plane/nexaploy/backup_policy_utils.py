"""Backup retention policy (per site, team default, operator limits): pure helpers, no frappe.

A policy has five integers:

* ``retention_days``       - remote copies (bucket / Drive / ...) older than this are pruned
                             (``backup.prune_remote``); 0 = keep forever;
* ``keep_daily`` / ``keep_weekly`` / ``keep_monthly`` - additionally keep the newest backup of
                             the last N days / ISO weeks / months (grandfather-father-son);
* ``local_retention_days`` - copies on the server after a successful upload (``site.backup``
                             ``local_retention_days``); 0 = delete right after the upload.

A site uses the team default unless ``Site.backup_retention_custom`` is set. The platform
operator bounds both in Nexaploy Settings (min/max days, max local days, max monthly keeps)
and may sell longer retention: anything kept longer than ``backup_retention_included_days``
needs the team's paid add-on (``Team.extended_backup_retention``, set by billing - see
:func:`nexaploy.ops.retention_addon_active`, the hook point for the add-on).
"""

from __future__ import annotations

from typing import Any, Mapping

POLICY_FIELDS = ("retention_days", "keep_daily", "keep_weekly", "keep_monthly", "local_retention_days")
DEFAULT_POLICY = {
	"retention_days": 14, "keep_daily": 0, "keep_weekly": 0, "keep_monthly": 0, "local_retention_days": 14,
}
# Site / Team field name for each policy key
DOC_FIELDS = {
	"retention_days": "backup_retention_days",
	"keep_daily": "backup_keep_daily",
	"keep_weekly": "backup_keep_weekly",
	"keep_monthly": "backup_keep_monthly",
	"local_retention_days": "backup_local_retention_days",
}
# hard caps (the agent validates the same)
HARD_MAX = {"keep_daily": 366, "keep_weekly": 520, "keep_monthly": 240, "local_retention_days": 3650,
            "retention_days": 36500}

# Nexaploy Settings limit fields and their defaults (0 = no limit)
LIMIT_FIELDS = {
	"backup_retention_min_days": 1,
	"backup_retention_max_days": 0,
	"backup_retention_included_days": 0,
	"backup_local_retention_max_days": 0,
	"backup_keep_monthly_max": 0,
}
KEEP_MIN = 3  # always keep the newest N remote backups
FOREVER = float("inf")


class PolicyError(ValueError):
	"""Invalid or not-allowed retention (message is shown to the customer)."""


def _int(value: Any, key: str) -> int:
	if value in (None, ""):
		return DEFAULT_POLICY[key]
	try:
		number = int(value)
	except (TypeError, ValueError):
		raise PolicyError(f"{key} must be a whole number") from None
	if number < 0:
		raise PolicyError(f"{key} must be 0 or more")
	if number > HARD_MAX[key]:
		raise PolicyError(f"{key} must be at most {HARD_MAX[key]}")
	return number


def normalize_policy(values: Mapping[str, Any] | None) -> dict[str, int]:
	values = values or {}
	return {k: _int(values.get(k), k) for k in POLICY_FIELDS}


def from_doc(row: Mapping[str, Any] | None) -> dict[str, int]:
	"""Policy from a Site/Team row (backup_* field names); missing values -> defaults."""
	row = row or {}
	return normalize_policy({k: row.get(f) for k, f in DOC_FIELDS.items()})


def to_doc(policy: Mapping[str, int]) -> dict[str, int]:
	return {DOC_FIELDS[k]: int(policy[k]) for k in POLICY_FIELDS}


def effective_policy(site: Mapping[str, Any] | None, team: Mapping[str, Any] | None) -> dict[str, int]:
	"""The site's own policy when it has `backup_retention_custom`, else the team default."""
	if site and site.get("backup_retention_custom"):
		return from_doc(site)
	return from_doc(team)


def retention_span_days(policy: Mapping[str, int]) -> float:
	"""How far back remote backups reach (days); inf when kept forever."""
	if not policy["retention_days"]:
		return FOREVER
	return max(policy["retention_days"], policy["keep_daily"], policy["keep_weekly"] * 7,
	           policy["keep_monthly"] * 31)


def limits_from(settings: Mapping[str, Any] | None) -> dict[str, int]:
	settings = settings or {}
	out = {}
	for key, default in LIMIT_FIELDS.items():
		try:
			out[key] = max(int(settings.get(key) if settings.get(key) not in (None, "") else default), 0)
		except (TypeError, ValueError):
			out[key] = default
	return out


def check_policy(policy: Mapping[str, int], limits: Mapping[str, int], addon_active: bool = False) -> dict[str, Any]:
	"""Raise PolicyError when `policy` breaks the operator's limits; returns
	{span_days, requires_addon} (requires_addon: longer than the included retention)."""
	lim = {**LIMIT_FIELDS, **(limits or {})}
	days = policy["retention_days"]
	span = retention_span_days(policy)
	if days and days < lim["backup_retention_min_days"]:
		raise PolicyError(f"Backups must be kept at least {lim['backup_retention_min_days']} days")
	if lim["backup_retention_max_days"] and span > lim["backup_retention_max_days"]:
		raise PolicyError(f"Backups can be kept at most {lim['backup_retention_max_days']} days")
	if lim["backup_local_retention_max_days"] and policy["local_retention_days"] > lim["backup_local_retention_max_days"]:
		raise PolicyError(
			f"Backups can be kept on the server at most {lim['backup_local_retention_max_days']} days"
		)
	if lim["backup_keep_monthly_max"] and policy["keep_monthly"] > lim["backup_keep_monthly_max"]:
		raise PolicyError(f"At most {lim['backup_keep_monthly_max']} monthly backups can be kept")
	included = lim["backup_retention_included_days"]
	requires_addon = bool(included) and span > included
	if requires_addon and not addon_active:
		raise PolicyError(
			f"Keeping backups longer than {included} days needs the extended backup retention add-on"
		)
	return {"span_days": None if span == FOREVER else span, "requires_addon": requires_addon}


def prune_params(policy: Mapping[str, int]) -> dict[str, int] | None:
	"""`backup.prune_remote` retention params, or None when backups are kept forever."""
	if not policy["retention_days"]:
		return None
	return {
		"keep_days": int(policy["retention_days"]),
		"keep_min": KEEP_MIN,
		"keep_daily": int(policy["keep_daily"]),
		"keep_weekly": int(policy["keep_weekly"]),
		"keep_monthly": int(policy["keep_monthly"]),
	}


# --------------------------------------------------------------------------- restore

RESTORE_MODES = ("all", "database", "files")


def restore_params(site_name: str, remote_path: str, mode: str = "all", bench: str | None = None) -> dict[str, Any]:
	"""`site.restore` params for restoring `remote_path` into `site_name` (on `bench` = new site)."""
	if mode not in RESTORE_MODES:
		raise PolicyError("Restore what: all, database or files")
	if not remote_path:
		raise PolicyError("This backup was not uploaded and cannot be restored")
	if mode == "files" and bench:
		raise PolicyError("A new site needs the database: restore everything or the database")
	params: dict[str, Any] = {"site": site_name, "remote_path": remote_path, "restore": mode}
	if mode == "database":
		params["with_files"] = False
	if bench:
		params["bench"] = bench
	return params


def restorable_parts(backup_files: list[Mapping[str, Any]], with_files: Any) -> list[str]:
	"""Restore modes a backup supports: files modes only when it has public/private files."""
	types = {f.get("file_type") for f in backup_files or ()}
	has_files = bool(types & {"public", "private"}) or (not types and bool(with_files))
	modes = ["all", "database"]
	if has_files:
		modes.append("files")
	return modes
