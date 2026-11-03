"""Site tools for the customer portal: plan usage & upgrades, backup downloads, site config editor.

Every method resolves the site's team and checks it with `nexaploy.teams.check_team_access`
(ARCHITECTURE.md §9): read -> usage / backup files / downloads, write -> site config,
billing -> plan changes. Pure logic lives in nexaploy.ops_utils.
"""

from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import now_datetime

from nexaploy import ops_utils as ou

CONFIG_CACHE_SECONDS = 60


# --------------------------------------------------------------------------- access


def _require_user():
	if not frappe.session.user or frappe.session.user == "Guest":
		raise frappe.PermissionError(_("Please log in"))


def _check(team, action):
	from nexaploy.teams import check_team_access

	check_team_access(team, action)


def _site(site, action="read", fields=("name", "site_name", "team", "server", "status", "plan")):
	_require_user()
	if not site:
		frappe.throw(_("Site is required"))
	row = frappe.db.get_value("Site", site, list(fields), as_dict=True)
	if not row:
		raise frappe.DoesNotExistError(_("Site {0} not found").format(site))
	_check(row.team, action)
	return row


def _active_site(row):
	if row.status not in ("Active", "Suspended"):
		frappe.throw(_("Site {0} is {1}").format(row.name, _(row.status)))


# --------------------------------------------------------------------------- usage & plans


def _plan_dict(plan):
	if not plan:
		return None
	return {
		"name": plan.name,
		"title": plan.title,
		"price_monthly": plan.price_monthly,
		"currency": plan.currency,
		"cpu_seconds_per_day": plan.cpu_seconds_per_day or 0,
		"max_database_mb": plan.max_database_mb or 0,
		"max_storage_mb": plan.max_storage_mb or 0,
		"overage_action": ou.normalize_overage_action(plan.overage_action),
		"plan_order": plan.plan_order or 0,
	}


def _site_plans():
	from nexaploy.ops import PLAN_LIMIT_FIELDS

	return frappe.get_all(
		"Nexaploy Plan", filters={"plan_type": "Site", "enabled": 1}, fields=PLAN_LIMIT_FIELDS,
		order_by="plan_order asc, price_monthly asc",
	)


@frappe.whitelist()
def get_usage(site, days=30):
	"""Usage today vs the plan, recent history and upgrade options."""
	from nexaploy.ops import SITE_USAGE_FIELDS, plan_limits, site_usage_today

	row = _site(site, "read")
	doc = frappe.db.get_value("Site", row.name, SITE_USAGE_FIELDS + ["usage_updated_at"], as_dict=True)
	plan = plan_limits(doc.plan)
	usage = site_usage_today(doc)
	status = ou.usage_status(plan, usage)
	days = max(1, min(int(days or 30), 90))
	since = (now_datetime() - timedelta(days=days)).date()
	history = frappe.get_all(
		"Site Usage",
		filters={"site": row.name, "date": [">=", since]},
		fields=["date", "cpu_seconds", "requests", "database_mb", "storage_mb"],
		order_by="date asc",
	)
	plans = _site_plans()
	return {
		"site": row.name,
		"plan": _plan_dict(frappe._dict(plan)) if plan else None,
		"usage": usage,
		"status": status,
		"over_limit": bool(doc.over_limit),
		"throttled": bool(doc.throttled),
		"suspended": bool(doc.usage_suspended),
		"updated_at": doc.usage_updated_at,
		"history": history,
		"plans": [_plan_dict(p) for p in plans],
		"upgrades": [p.name for p in ou.upgrade_plans(plans, doc.plan)],
		"can_change_plan": _allowed(row.team, "billing"),
	}


def _allowed(team, action):
	try:
		_check(team, action)
		return True
	except frappe.PermissionError:
		return False


@frappe.whitelist(methods=["POST"])
def change_plan(site, plan):
	"""Move a site to another enabled Site plan (team `billing` permission). The Site on_update
	hook re-syncs the billing subscription; limits are re-evaluated right away."""
	row = _site(site, "billing")
	_active_site(row)
	target = frappe.db.get_value("Nexaploy Plan", plan, ["name", "plan_type", "enabled"], as_dict=True)
	if not target or not target.enabled or target.plan_type != "Site":
		frappe.throw(_("Plan {0} is not an available Site plan").format(plan))
	if row.plan == target.name:
		return {"site": row.name, "plan": target.name, "changed": False}
	doc = frappe.get_doc("Site", row.name)
	doc.plan = target.name
	doc.flags.ignore_permissions = True
	doc.save()  # Site on_update -> nexaploy.billing.subscriptions.sync_subscription
	doc.add_comment("Info", _("Plan changed from {0} to {1} by {2}").format(row.plan or "-", target.name, frappe.session.user))
	try:
		from nexaploy.ops import enforce_site_limits

		enforce_site_limits(row.name)
	except Exception:
		frappe.log_error(title=f"Nexaploy: limit re-check after plan change failed for {row.name}")
	return {"site": row.name, "plan": target.name, "changed": True}


# --------------------------------------------------------------------------- backups


@frappe.whitelist()
def list_backup_files(site):
	"""{backup name: [{file_type, size, downloadable}]} for the site's recent backups."""
	row = _site(site, "read")
	backups = frappe.get_all(
		"Site Backup", filters={"site": row.name, "status": "Success"}, pluck="name",
		order_by="creation desc", limit_page_length=50,
	)
	if not backups:
		return {}
	files = frappe.get_all(
		"Site Backup File",
		filters={"parent": ["in", backups], "parenttype": "Site Backup"},
		fields=["parent", "file_type", "size", "remote_key"],
		order_by="idx asc",
	)
	out = {}
	for f in files:
		out.setdefault(f.parent, []).append(
			{"file_type": f.file_type, "size": f.size, "downloadable": bool(f.remote_key)}
		)
	return out


@frappe.whitelist(methods=["POST"])
def download_backup(backup, file_type):
	"""Presigned (15 min) download URL for one file of a backup. Team `read`; every call is
	logged as a comment on the Site Backup."""
	from nexaploy.ops import DOWNLOAD_EXPIRES, backup_download_url

	_require_user()
	if file_type not in ou.BACKUP_FILE_TYPES:
		frappe.throw(_("Unknown backup file type {0}").format(file_type))
	row = frappe.db.get_value("Site Backup", backup, ["name", "site", "team"], as_dict=True)
	if not row:
		raise frappe.DoesNotExistError(_("Backup {0} not found").format(backup))
	team = row.team or frappe.db.get_value("Site", row.site, "team")
	_check(team, "read")
	url = backup_download_url(row.name, file_type, DOWNLOAD_EXPIRES)
	frappe.get_doc(
		{
			"doctype": "Comment",
			"comment_type": "Info",
			"reference_doctype": "Site Backup",
			"reference_name": row.name,
			"content": _("{0} downloaded the {1} file").format(frappe.session.user, file_type),
		}
	).insert(ignore_permissions=True)
	frappe.logger("nexaploy").info(f"backup download: {frappe.session.user} {row.name} {file_type}")
	return {"url": url, "expires_in": DOWNLOAD_EXPIRES}


# --------------------------------------------------------------------------- site config


def _config_job_result(name):
	job = frappe.db.get_value("Agent Job", name, ["name", "status", "result", "error", "kind", "reference_name"],
	                          as_dict=True)
	return job


def _config_payload(site, job):
	out = {"site": site, "job": job.name, "status": job.status, "error": job.error}
	if job.status == "Success":
		result = frappe.parse_json(job.result) if job.result else {}
		view = ou.redact_site_config((result or {}).get("config"))
		out.update(view)
		out["editable_keys_hint"] = sorted(ou.CONFIG_WARN)
	return out


@frappe.whitelist(methods=["POST"])
def get_site_config(site, refresh=0):
	"""Read site_config.json via the agent (`site.get_config`, secrets redacted by the agent and
	again here). Returns the latest result if it is under a minute old, otherwise queues a job:
	poll `get_site_config_result(site, job)` until status is Success/Failure."""
	from nexaploy.agent_job import create_agent_job

	row = _site(site, "write")
	_active_site(row)
	if not frappe.parse_json(refresh):
		recent = frappe.get_all(
			"Agent Job",
			filters={
				"reference_doctype": "Site", "reference_name": row.name, "kind": "site.get_config",
				"creation": [">=", now_datetime() - timedelta(seconds=CONFIG_CACHE_SECONDS)],
				"status": ["!=", "Failure"],
			},
			pluck="name", order_by="creation desc", limit_page_length=1,
		)
		if recent:
			return _config_payload(row.name, _config_job_result(recent[0]))
	job = create_agent_job(row.server, "site.get_config", {"site": row.site_name}, "Site", row.name)
	return {"site": row.name, "job": job.name, "status": job.status}


@frappe.whitelist()
def get_site_config_result(site, job):
	row = _site(site, "write")
	data = _config_job_result(job)
	if not data or data.kind != "site.get_config" or data.reference_name != row.name:
		raise frappe.PermissionError(_("Not allowed"))
	return _config_payload(row.name, data)


@frappe.whitelist(methods=["POST"])
def update_site_config(site, changes=None, remove=None):
	"""Set / remove site_config keys: `changes` {key: JSON value}, `remove` [keys]. Validated by
	ops_utils.validate_config_changes (deny-list: db_*, encryption_key, host_name, rate_limit,
	monitor, ...). Queues one `site.update_config` job. Returns {job, set, remove, warnings}."""
	from nexaploy.ops import enqueue_config_update

	row = _site(site, "write")
	_active_site(row)
	try:
		clean, removed, warnings = ou.validate_config_changes(
			frappe.parse_json(changes) if changes else {}, frappe.parse_json(remove) if remove else []
		)
	except ValueError as exc:
		frappe.throw(_(str(exc)))
	if not clean and not removed:
		frappe.throw(_("Nothing to change"))
	job = enqueue_config_update(row.name, clean, removed)
	frappe.get_doc("Site", row.name).add_comment(
		"Info",
		_("{0} changed site config: set {1}; removed {2}").format(
			frappe.session.user, ", ".join(sorted(clean)) or "-", ", ".join(removed) or "-"
		),
	)
	return {"job": job, "set": sorted(clean), "remove": removed, "warnings": warnings}
