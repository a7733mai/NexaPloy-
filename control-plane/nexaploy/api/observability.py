"""Site observability for the customer portal (/dashboard/insights): logs, request analytics,
slow queries, SQL playground and Python console (ARCHITECTURE.md §10, Phase 4).

Every call queues one agent job (`site.logs`, `site.analytics`, `site.slow_queries`, `site.sql`,
`site.console`) referencing the Site and returns `{job, status}`; the portal then polls
`get_result(job)` until `done`. Results come back from the agent through `job_update` and are
stored in `Agent Job.result` (the agent caps them well below 2 MB).

Permissions: team membership (`nexaploy.teams.check_team_access`, read) plus the member's
console capabilities (`nexaploy.console_access.check_capability`, enforced here, never trusted
from the client):
  analytics                     -> any member
  logs, slow queries            -> can_view_logs
  SQL read-only / write mode    -> can_run_sql / can_run_sql_write
  console                       -> can_use_console
Role defaults: Owner/Admin everything, Developer logs + read-only SQL, Billing nothing; Owners and
Admins can customise them per member. A team may also require 2FA for SQL and the console.
Every SQL / console / logs / slow-query execution (and every refused attempt) is written to the
Console Audit Log (redacted, truncated command text) and to Team Activity.
Pure logic: nexaploy.observability_utils, nexaploy.console_access_utils.
"""

from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import now_datetime

from nexaploy import observability_utils as obs

# reuse a finished read-only job of the same kind/params younger than this
REUSE_SECONDS = 60
# at most this many unfinished observability jobs per site
MAX_IN_FLIGHT = 5
IN_FLIGHT = ("Pending", "Queued", "Running")
BLOCKED_STATUSES = ("Pending", "Archived")


# --------------------------------------------------------------------------- access


def _require_user():
	if not frappe.session.user or frappe.session.user == "Guest":
		raise frappe.PermissionError(_("Please log in"))


def _check(team, action):
	from nexaploy.teams import check_team_access

	return check_team_access(team, action)


def _site(site, kind, read_only=True, command=None):
	"""Site row after the membership + capability check for observability job `kind`.
	`command` (query / code / log params) is only used to audit a refused attempt."""
	from nexaploy.console_access import check_capability

	_require_user()
	if not site:
		frappe.throw(_("Site is required"))
	row = frappe.db.get_value("Site", site, ["name", "site_name", "team", "server", "status"], as_dict=True)
	if not row:
		raise frappe.DoesNotExistError(_("Site {0} not found").format(site))
	check_capability(row.team, kind, read_only, site=row.name, params=command)
	if row.status in BLOCKED_STATUSES:
		frappe.throw(_("Site {0} is {1}").format(row.name, _(row.status)))
	if not row.server:
		frappe.throw(_("Site {0} has no server").format(row.name))
	return row


def _log_activity(team, kind, site, params):
	try:
		from nexaploy.activity import log_activity
	except ImportError:
		return
	try:
		log_activity(team, obs.activity_action(kind), "Site", site, obs.activity_details(kind, params))
	except Exception:
		frappe.log_error(title=f"Nexaploy: activity log failed for {kind}")


# --------------------------------------------------------------------------- jobs


def _in_flight(site):
	return frappe.db.count(
		"Agent Job",
		{
			"reference_doctype": "Site",
			"reference_name": site,
			"kind": ["in", list(obs.OBS_KINDS)],
			"status": ["in", list(IN_FLIGHT)],
		},
	)


def _reusable(row, kind, params):
	"""A recent (or still running) job with identical params, for read-only kinds."""
	jobs = frappe.get_all(
		"Agent Job",
		filters={
			"reference_doctype": "Site",
			"reference_name": row.name,
			"kind": kind,
			"status": ["!=", "Failure"],
			"creation": [">=", now_datetime() - timedelta(seconds=REUSE_SECONDS)],
		},
		fields=["name", "status", "params"],
		order_by="creation desc",
		limit_page_length=5,
	)
	for job in jobs:
		if obs.same_request(frappe.parse_json(job.params or "{}") or {}, params):
			return job
	return None


def _audit(row, kind, params, job):
	"""Console Audit Log row for every audited execution (raises: no job without its audit row)."""
	from nexaploy.console_access import start_audit
	from nexaploy.console_access_utils import AUDIT_KINDS

	if kind in AUDIT_KINDS:
		start_audit(row.team, row.name, kind, params, job, read_only=obs.to_bool(params.get("read_only"), True))


def _enqueue(row, kind, params, reuse=False):
	from nexaploy.agent_job import create_agent_job

	_log_activity(row.team, kind, row.name, params)
	if reuse:
		job = _reusable(row, kind, params)
		if job:
			_audit(row, kind, params, job.name)
			return {"job": job.name, "status": job.status, "reused": True}
	if _in_flight(row.name) >= MAX_IN_FLIGHT:
		frappe.throw(_("Too many requests are in progress for this site. Wait for them to finish."))
	job = create_agent_job(row.server, kind, params, "Site", row.name)
	_audit(row, kind, params, job.name)
	return {"job": job.name, "status": job.status}


def _params(fn, *args):
	try:
		return fn(*args)
	except ValueError as exc:
		frappe.throw(_(str(exc)))


# --------------------------------------------------------------------------- read


@frappe.whitelist()
def get_logs(site, log="web", lines=200, since=None, refresh=0):
	"""Tail a log of the site: web | worker | scheduler | frappe | error (Error Log rows)."""
	row = _site(site, "site.logs", command={"log": log, "lines": lines, "since": since})
	params = _params(obs.logs_params, row.site_name, log, lines, since)
	return _enqueue(row, "site.logs", params, reuse=not obs.to_bool(refresh))


@frappe.whitelist()
def get_analytics(site, days=7, refresh=0):
	"""Request / background-job analytics of the last `days` (1-31) UTC days."""
	row = _site(site, "site.analytics")
	params = obs.analytics_params(row.site_name, days, now_datetime().date())
	return _enqueue(row, "site.analytics", params, reuse=not obs.to_bool(refresh))


@frappe.whitelist()
def get_slow_queries(site, limit=50, min_seconds=0, refresh=0):
	"""Slow queries of the site's database grouped by normalised query."""
	row = _site(site, "site.slow_queries", command={"limit": limit, "min_seconds": min_seconds})
	params = obs.slow_query_params(row.site_name, limit, min_seconds)
	return _enqueue(row, "site.slow_queries", params, reuse=not obs.to_bool(refresh))


# --------------------------------------------------------------------------- run


@frappe.whitelist(methods=["POST"])
def run_sql(site, query, read_only=True, max_rows=None, timeout=None):
	"""Run SQL as the site's own DB user. Read-only (can_run_sql): SELECT/SHOW/DESCRIBE/
	EXPLAIN/WITH inside a read-only transaction. Write mode needs can_run_sql_write."""
	ro = obs.to_bool(read_only, True)
	row = _site(site, "site.sql", ro, command={"query": query, "read_only": ro})
	try:
		params = obs.sql_params(row.site_name, query, ro, max_rows, timeout)
	except obs.QueryError as exc:
		frappe.throw(_(str(exc)))
	return _enqueue(row, "site.sql", params)


@frappe.whitelist(methods=["POST"])
def run_console(site, code, commit=False, timeout=None):
	"""Execute Python in the site's bench (frappe initialised and connected). Needs
	can_use_console; rolled back unless `commit`."""
	row = _site(site, "site.console", command={"code": code, "commit": obs.to_bool(commit)})
	params = _params(obs.console_params, row.site_name, code, commit, timeout)
	# the bench container can read every site_config.json on the bench: never run a customer's
	# code next to another team's site
	bench = frappe.db.get_value("Site", row.name, "bench")
	others = frappe.get_all(
		"Site",
		filters={"bench": bench, "name": ["!=", row.name], "status": ["!=", "Archived"]},
		pluck="team",
	) if bench else []
	if any(t != row.team for t in others):
		frappe.throw(_("The console is only available for sites on a bench dedicated to your team."))
	params["allow_shared_bench"] = bool(others)
	return _enqueue(row, "site.console", params)


# --------------------------------------------------------------------------- results


@frappe.whitelist()
def get_result(job):
	"""Status and (when finished) data of an observability job. The caller needs the same
	capability as for starting it (logs / SQL / SQL write / console)."""
	_require_user()
	row = frappe.db.get_value(
		"Agent Job", job, ["name", "kind", "status", "params", "result", "error", "reference_doctype", "reference_name"],
		as_dict=True,
	)
	if not row or row.kind not in obs.OBS_KINDS or row.reference_doctype != "Site":
		raise frappe.PermissionError(_("Not allowed"))
	from nexaploy.console_access import get_capabilities
	from nexaploy.console_access_utils import capability_allows

	team = frappe.db.get_value("Site", row.reference_name, "team")
	params = frappe.parse_json(row.params or "{}") or {}
	_check(team, "read")
	if not capability_allows(get_capabilities(team), row.kind, obs.to_bool(params.get("read_only"), True)):
		raise frappe.PermissionError(_("Not allowed"))
	result = frappe.parse_json(row.result) if row.result else None
	out = obs.shape_result(row.kind, row.status, result, row.error)
	out["job"] = row.name
	out["site"] = row.reference_name
	return out


@frappe.whitelist()
def get_permissions(site):
	"""What the current user may do on the insights page of `site` (console capabilities)."""
	from nexaploy.console_access import console_two_factor_required, get_capabilities, user_has_two_factor

	_require_user()
	team = frappe.db.get_value("Site", site, "team")
	if team is None and not frappe.db.exists("Site", site):
		raise frappe.DoesNotExistError(_("Site {0} not found").format(site))
	_check(team, "read")
	caps = get_capabilities(team)
	two_factor = console_two_factor_required(team)
	return {
		"site": site,
		"read": True,
		"logs": caps["can_view_logs"],
		"sql": caps["can_run_sql"],
		"sql_write": caps["can_run_sql_write"],
		"console": caps["can_use_console"],
		"capabilities": caps,
		"two_factor_required": two_factor,
		"two_factor_ok": (not two_factor) or user_has_two_factor(),
	}
