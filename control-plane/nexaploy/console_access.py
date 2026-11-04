"""Scoped console permissions and the Console Audit Log (frappe side).

Pure logic: nexaploy.console_access_utils. Enforced server-side by nexaploy.api.observability
(the portal only hides buttons). Every SQL / console / logs execution - and every refused
attempt by a team member - writes one Console Audit Log row:

    check_capability(team, kind, read_only, site, params) -> role   # raises PermissionError
    get_capabilities(team, user=None) -> {can_view_logs, can_run_sql, can_run_sql_write, can_use_console}
    start_audit(team, site, kind, params, job) -> name               # with the queued Agent Job
    finish_audit(job)                                               # agent_callbacks, terminal job

The audit row is inserted in the same transaction as the Agent Job, so no job is ever queued
without its audit row (fail closed). A refused attempt is committed before the error is raised.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime

from nexaploy import console_access_utils as cu

AUDIT = "Console Audit Log"
MEMBER_FIELDS = ["user", "role", "custom_capabilities", *cu.CAPABILITIES]


def _user(user=None):
	return user or frappe.session.user


def member_row(team, user=None):
	"""The user's Team Member row (dict) in `team`, or None."""
	if not team:
		return None
	rows = frappe.get_all(
		"Team Member",
		filters={"parenttype": "Team", "parentfield": "members", "parent": team, "user": _user(user)},
		fields=MEMBER_FIELDS,
		limit_page_length=1,
	)
	return rows[0] if rows else None


def get_capabilities(team, user=None):
	from nexaploy.teams import get_team_role, is_platform_admin

	user = _user(user)
	role = get_team_role(team, user)
	return cu.effective_capabilities(role, member_row(team, user) if role else None, is_platform_admin(user))


def console_two_factor_required(team):
	return bool(team and frappe.db.get_value("Team", team, "console_two_factor_required"))


def user_has_two_factor(user=None):
	"""True when the user's login is protected by Frappe two-factor authentication."""
	from nexaploy.teams import is_platform_admin, two_factor_status

	user = _user(user)
	if is_platform_admin(user):
		return True
	try:
		return bool(two_factor_status(user).get("enforced"))
	except Exception:
		return False


def check_capability(team, kind, read_only=True, site=None, params=None, user=None):
	"""Raise frappe.PermissionError unless the user may run `kind` on a site of `team`.

	Membership first (check_team_access "read"), then the member's capability and the team's
	"require 2FA for console" flag. Refusals of audited kinds are recorded (status Denied)."""
	from nexaploy.teams import check_team_access, is_platform_admin

	user = _user(user)
	role = check_team_access(team, "read", user)  # not a member -> PermissionError, not audited
	caps = cu.effective_capabilities(role, member_row(team, user), is_platform_admin(user))
	reason = None
	if not cu.capability_allows(caps, kind, read_only):
		cap = cu.required_capability(kind, read_only)
		reason = _("Your team permissions do not include: {0}").format(_(cu.CAPABILITY_LABELS.get(cap, cap)))
	elif cu.needs_two_factor(kind, console_two_factor_required(team)) and not user_has_two_factor(user):
		reason = _("Team {0} requires two-factor authentication for SQL and the console. Enable it for your account and sign in again.").format(team)
	if reason:
		if kind in cu.AUDIT_KINDS:
			record_denied(team, site, kind, params, reason, read_only=read_only)
		raise frappe.PermissionError(reason)
	return role


# --------------------------------------------------------------------------- audit rows


def _request_ip():
	try:
		ip = getattr(frappe.local, "request_ip", None)
		return str(ip)[:140] if ip else None
	except Exception:
		return None


def _audit_doc(team, site, kind, params, status, job=None, error=None, read_only=True):
	params = params or {}
	command, truncated = cu.audit_command(kind, params)
	user = _user()
	doc = frappe.get_doc(
		{
			"doctype": AUDIT,
			"team": team,
			"user": user if user and user != "Guest" else None,
			"site": site,
			"kind": cu.audit_kind(kind),
			"action": kind,
			"status": status,
			"executed_at": now_datetime(),
			"ip_address": _request_ip(),
			"agent_job": job,
			"read_only_mode": int(bool(read_only)) if kind == "site.sql" else 0,
			"commit_changes": int(bool(params.get("commit"))) if kind == "site.console" else 0,
			"command": command,
			"command_truncated": int(truncated),
			"error": error,
		}
	)
	doc.flags.ignore_links = True
	doc.insert(ignore_permissions=True)
	return doc


def start_audit(team, site, kind, params, job, read_only=True):
	"""Audit row for a queued (or reused) observability job. Raises on failure (fail closed)."""
	row = frappe.db.get_value("Agent Job", job, ["status", "result", "error", "started_at", "ended_at"], as_dict=True) if job else None
	status, error, duration = "Queued", None, None
	if row and row.status in ("Success", "Failure"):
		result = frappe.parse_json(row.result) if row.result else None
		status = cu.audit_status(row.status, result)
		error = cu.audit_error(row.error, result) if status in ("Error", "Failed") else None
		duration = cu.audit_duration(row.started_at, row.ended_at, result)
	elif row and row.status == "Running":
		status = "Running"
	doc = _audit_doc(team, site, kind, params, status, job=job, error=error, read_only=read_only)
	if duration is not None:
		frappe.db.set_value(AUDIT, doc.name, "duration", duration, update_modified=False)
	return doc.name


def record_denied(team, site, kind, params, reason, read_only=True):
	"""Audit a refused attempt and commit it (the request is about to fail and roll back)."""
	try:
		_audit_doc(team, site, kind, params, "Denied", error=str(reason)[:2000], read_only=read_only)
		frappe.db.commit()
	except Exception:
		frappe.log_error(title=f"Nexaploy: console audit (denied {kind}) for {team} not recorded")


def finish_audit(job):
	"""Agent job callback: status, duration and error of every audit row of `job`."""
	names = frappe.get_all(AUDIT, filters={"agent_job": job.name, "status": ["in", ["Queued", "Running"]]}, pluck="name")
	if not names:
		return
	result = frappe.parse_json(job.result) if job.get("result") else None
	status = cu.audit_status(job.status, result)
	values = {"status": status}
	if status in ("Success", "Error", "Failed"):
		values["duration"] = cu.audit_duration(job.get("started_at"), job.get("ended_at"), result)
		values["error"] = cu.audit_error(job.get("error"), result) if status != "Success" else None
	for name in names:
		doc = frappe.get_doc(AUDIT, name)
		doc.update(values)
		doc.flags.nexaploy_audit_update = True
		doc.flags.ignore_permissions = True
		doc.flags.ignore_links = True
		doc.save()


# --------------------------------------------------------------------------- permissions (hooks.py)

SENSITIVE_KINDS = ("site.logs", "site.slow_queries", "site.sql", "site.console")


def has_agent_job_permission(doc, ptype=None, user=None):
	"""Agent Job (desk / REST): SQL, console and log jobs carry queries, code and data, so a
	customer reads them only with the capability that was needed to run them. Other kinds keep
	the default behaviour (role permissions + agent_job_query)."""
	from nexaploy.teams import is_platform_admin

	user = user or frappe.session.user
	if is_platform_admin(user) or doc.get("kind") not in SENSITIVE_KINDS:
		return True
	team = frappe.db.get_value("Site", doc.reference_name, "team") if doc.get("reference_doctype") == "Site" else None
	if not team:
		return False
	params = frappe.parse_json(doc.params) if doc.get("params") else {}
	read_only = bool((params or {}).get("read_only", True)) if isinstance(params, dict) else True
	return cu.capability_allows(get_capabilities(team, user), doc.kind, read_only)



def console_audit_query(user=None):
	"""Console Audit Log list: team Owners/Admins only (like Team Activity)."""
	from nexaploy.permissions import _is_restricted, _teams_with_roles_sql
	from nexaploy.team_utils import ROLE_MATRIX

	user = user or frappe.session.user
	if not _is_restricted(user):
		return ""
	return f"`tab{AUDIT}`.`team` in {_teams_with_roles_sql(user, ROLE_MATRIX['admin'])}"
