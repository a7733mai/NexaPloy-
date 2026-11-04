"""Pure logic for scoped console permissions and the Console Audit Log.

No frappe import: unit-tested with plain pytest (tests/test_console_access.py).
nexaploy/console_access.py does the database work; nexaploy/api/observability.py enforces it.

Capabilities (per Team Member, ARCHITECTURE.md §9 + "Console access"):

    can_view_logs      site.logs, site.slow_queries (log lines and query text contain data)
    can_run_sql        site.sql read-only (SELECT/SHOW/... in a read-only transaction)
    can_run_sql_write  site.sql write mode (implies can_run_sql)
    can_use_console    site.console (Python with frappe connected)

A member without `custom_capabilities` gets the defaults of their role (Owner/Admin: all;
Developer: logs + read-only SQL; Billing: none). Owners always have every capability (a team
can never lock itself out). Platform admins pass every check.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Mapping

CAPABILITIES = ("can_view_logs", "can_run_sql", "can_run_sql_write", "can_use_console")
CAPABILITY_LABELS = {
	"can_view_logs": "View logs",
	"can_run_sql": "Run read-only SQL",
	"can_run_sql_write": "Run SQL that changes data",
	"can_use_console": "Use the Python console",
}
_ALL = {c: True for c in CAPABILITIES}
_NONE = {c: False for c in CAPABILITIES}
ROLE_DEFAULTS: dict[str, dict[str, bool]] = {
	"Owner": dict(_ALL),
	"Admin": dict(_ALL),
	"Developer": {"can_view_logs": True, "can_run_sql": True, "can_run_sql_write": False, "can_use_console": False},
	"Billing": dict(_NONE),
}

# observability job kind -> audit kind (Console Audit Log.kind)
AUDIT_KINDS = {"site.sql": "sql", "site.console": "console", "site.logs": "logs", "site.slow_queries": "logs"}
AUDIT_STATUSES = ("Queued", "Running", "Success", "Error", "Failed", "Denied")
MAX_COMMAND_CHARS = 10_000
MASK = "********"


def _truthy(value: Any) -> bool:
	if isinstance(value, str):
		return value.strip().lower() in ("1", "true", "yes", "on")
	return bool(value)


# --------------------------------------------------------------------------- capabilities


def default_capabilities(role: str | None) -> dict[str, bool]:
	return dict(ROLE_DEFAULTS.get(role or "", _NONE))


def normalize_capabilities(caps: Mapping[str, Any] | None) -> dict[str, bool]:
	"""Only known keys, booleans; SQL write implies read-only SQL."""
	caps = caps or {}
	out = {c: _truthy(caps.get(c)) for c in CAPABILITIES}
	if out["can_run_sql_write"]:
		out["can_run_sql"] = True
	return out


def effective_capabilities(role: str | None, member: Mapping[str, Any] | None = None,
                           platform_admin: bool = False) -> dict[str, bool]:
	"""What `member` (a Team Member row: role, custom_capabilities, can_*) may do."""
	if platform_admin or role == "Owner":
		return dict(_ALL)
	if not role:
		return dict(_NONE)
	member = member or {}
	if not _truthy(member.get("custom_capabilities")):
		return default_capabilities(role)
	return normalize_capabilities(member)


def required_capability(kind: str, read_only: bool = True) -> str | None:
	"""Capability needed to run / read an observability job (None: plain team `read`)."""
	if kind in ("site.logs", "site.slow_queries"):
		return "can_view_logs"
	if kind == "site.sql":
		return "can_run_sql" if read_only else "can_run_sql_write"
	if kind == "site.console":
		return "can_use_console"
	if kind == "site.analytics":
		return None
	raise ValueError(f"not an observability job: {kind}")


def capability_allows(caps: Mapping[str, bool], kind: str, read_only: bool = True) -> bool:
	cap = required_capability(kind, read_only)
	return cap is None or bool(caps.get(cap))


def needs_two_factor(kind: str, team_requires: bool) -> bool:
	"""Team "require 2FA for console": SQL (any mode) and the Python console."""
	return bool(team_requires) and kind in ("site.sql", "site.console")


def can_edit_capabilities(actor_role: str | None, target_role: str | None, platform_admin: bool = False) -> bool:
	"""Owners edit Admin/Developer/Billing members, Admins edit Developer/Billing members.
	Owners always have everything (nobody edits an Owner)."""
	if target_role == "Owner" or target_role not in ROLE_DEFAULTS:
		return False
	if platform_admin or actor_role == "Owner":
		return True
	return actor_role == "Admin" and target_role in ("Developer", "Billing")


def capability_changes(role: str, member: Mapping[str, Any] | None, new: Mapping[str, Any] | None,
                       reset: bool = False) -> dict[str, Any]:
	"""Fields to write on the Team Member row: back to role defaults (reset) or custom flags."""
	if reset:
		return {"custom_capabilities": 0, **{c: 0 for c in CAPABILITIES}}
	caps = normalize_capabilities({**effective_capabilities(role, member), **(new or {})})
	return {"custom_capabilities": 1, **{c: int(v) for c, v in caps.items()}}


# --------------------------------------------------------------------------- audit text

_Q = r"""(?:'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|[^\s,;)]+)"""
_SECRET_WORDS = (
	r"password|passwd|pwd|secret|token|api_?key|apikey|access_?key|secret_?key|private_?key|"
	r"client_?secret|auth|authorization|encryption_key|db_password|admin_password"
)
_REDACTIONS: tuple[tuple[re.Pattern[str], Any], ...] = (
	# -----BEGIN ... PRIVATE KEY----- ... -----END ... PRIVATE KEY-----
	(re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
	 "-----PRIVATE KEY " + MASK + "-----"),
	# Authorization header values
	(re.compile(r"\b(token)(\s+)[A-Za-z0-9]{6,}:[A-Za-z0-9]{6,}", re.I), r"\1\2" + MASK),
	(re.compile(r"\b(bearer|basic)(\s+)[A-Za-z0-9._~+/-]{12,}=*", re.I), r"\1\2" + MASK),
	# IDENTIFIED BY [PASSWORD] '...'
	(re.compile(r"(\bidentified\s+(?:with\s+\S+\s+)?(?:by|as)\s+(?:password\s+)?)" + _Q, re.I), r"\1'" + MASK + "'"),
	# PASSWORD('...') / set password = '...'
	(re.compile(r"(\bpassword\s*\(\s*)" + _Q, re.I), r"\1'" + MASK + "'"),
	# key = value / "key": "value" / key: value  (python kwargs, dicts, SQL assignments, config)
	(re.compile(r"""(["']?\b[\w.-]*(?:""" + _SECRET_WORDS + r""")[\w.-]*["']?\s*(?:=|:)\s*)""" + _Q, re.I),
	 r"\1'" + MASK + "'"),
	# frappe helpers that take a password positionally: set_password / update_password / check_password
	(re.compile(r"(\b(?:set|update|check)_password\s*\(\s*[^,()]+,\s*)" + _Q, re.I), r"\1'" + MASK + "'"),
	# credentials in URLs
	(re.compile(r"(\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)[^@\s/]+@", re.I), r"\1" + MASK + "@"),
	# well-known key shapes
	(re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{8,}\b"), MASK),
	(re.compile(r"\bAKIA[0-9A-Z]{16}\b"), MASK),
	(re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), MASK),
)


def redact_command(text: Any) -> str:
	"""Mask credentials in SQL / Python / log parameters before they are stored."""
	out = "" if text is None else str(text)
	for pattern, repl in _REDACTIONS:
		out = pattern.sub(repl, out)
	return out


def truncate_command(text: str, limit: int = MAX_COMMAND_CHARS) -> tuple[str, bool]:
	if len(text) <= limit:
		return text, False
	return text[:limit] + f"\n… [truncated, {len(text) - limit} more characters]", True


def command_text(kind: str, params: Mapping[str, Any] | None) -> str:
	"""Human-readable command of an observability call."""
	p = params or {}
	if kind == "site.sql":
		return str(p.get("query") or "")
	if kind == "site.console":
		return str(p.get("code") or "")
	if kind == "site.logs":
		parts = [f"log={p.get('log') or 'web'}", f"lines={p.get('lines')}"]
		if p.get("since"):
			parts.append(f"since={p.get('since')}")
		return "logs " + " ".join(parts)
	if kind == "site.slow_queries":
		return f"slow_queries limit={p.get('limit')} min_seconds={p.get('min_seconds')}"
	return kind


def audit_command(kind: str, params: Mapping[str, Any] | None) -> tuple[str, bool]:
	"""(redacted, truncated command text, truncated?) for Console Audit Log.command."""
	return truncate_command(redact_command(command_text(kind, params)))


def audit_kind(kind: str) -> str:
	try:
		return AUDIT_KINDS[kind]
	except KeyError:
		raise ValueError(f"{kind} is not audited") from None


def audit_status(job_status: str | None, result: Any = None) -> str:
	"""Agent Job status (+ result) -> Console Audit Log.status.

	A console whose code raised, or SQL that the database rejected, ran successfully as a job
	but is recorded as Error."""
	if job_status in ("Pending", "Queued", None, ""):
		return "Queued"
	if job_status == "Running":
		return "Running"
	if job_status == "Failure":
		return "Failed"
	if job_status == "Success":
		if isinstance(result, Mapping) and (result.get("error") or result.get("ok") is False):
			return "Error"
		return "Success"
	return "Failed"


def audit_error(job_error: Any, result: Any = None, limit: int = 2000) -> str | None:
	text = None
	if isinstance(result, Mapping) and result.get("error"):
		text = str(result.get("error"))
	elif job_error:
		text = str(job_error)
	if not text:
		return None
	text = redact_command(text)
	return text if len(text) <= limit else text[:limit] + "…"


def _dt(value: Any) -> datetime | None:
	if isinstance(value, datetime):
		return value
	if isinstance(value, str) and value:
		try:
			return datetime.fromisoformat(value.replace("Z", "+00:00").split(".")[0])
		except ValueError:
			return None
	return None


def audit_duration(started_at: Any, ended_at: Any, result: Any = None) -> float | None:
	"""Seconds: the console/SQL's own duration when reported, else job start -> end."""
	if isinstance(result, Mapping):
		for key in ("duration", "elapsed"):
			value = result.get(key)
			if isinstance(value, (int, float)) and value >= 0:
				return round(float(value), 3)
	start, end = _dt(started_at), _dt(ended_at)
	if start and end:
		if (start.tzinfo is None) != (end.tzinfo is None):
			start, end = start.replace(tzinfo=None), end.replace(tzinfo=None)
		return round(max((end - start).total_seconds(), 0.0), 3)
	return None
