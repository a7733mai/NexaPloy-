"""Scheduler jobs (see hooks.scheduler_events)."""

import frappe
from frappe.utils import add_to_date, now_datetime

from nexaploy import ops_utils as ou

OFFLINE_AFTER_MINUTES = 5


def mark_offline_servers():
	"""Active servers without a heartbeat for 5 minutes -> Offline."""
	cutoff = add_to_date(now_datetime(), minutes=-OFFLINE_AFTER_MINUTES)
	stale = frappe.get_all(
		"Server",
		filters={"status": "Active", "last_heartbeat": ["<", cutoff]},
		pluck="name",
	)
	never = frappe.get_all(
		"Server",
		filters={"status": "Active", "last_heartbeat": ["is", "not set"], "creation": ["<", cutoff]},
		pluck="name",
	)
	for name in set(stale) | set(never):
		frappe.db.set_value("Server", name, "status", "Offline", update_modified=False)
	if stale or never:
		frappe.db.commit()


def prune_registration_tokens():
	"""Delete unused registration tokens that expired (used ones are kept for audit)."""
	expired = frappe.get_all(
		"Server Registration Token",
		filters={"used": 0, "expires_on": ["<", now_datetime()]},
		pluck="name",
	)
	for name in expired:
		frappe.delete_doc("Server Registration Token", name, ignore_permissions=True, force=True)
	if expired:
		frappe.db.commit()


# --------------------------------------------------------------------------- operations
# (scheduler entries: see control-plane/README.md "Operations")


ACTIVE_JOB_STATUSES = ["Pending", "Queued", "Running"]
UPTIME_TIMEOUT = 10
UPTIME_WORKERS = 16


def schedule_backups():
	"""Hourly: enqueue site.backup (Site Backup, purpose Scheduled) for every Active site whose
	backup_frequency interval has elapsed since last_backup_at. Skips sites that already have a
	backup in flight and sites on servers that are not Active.
	"""
	now = now_datetime()
	active_servers = set(frappe.get_all("Server", filters={"status": "Active"}, pluck="name"))
	sites = frappe.get_all(
		"Site",
		filters={"status": "Active", "backup_frequency": ["!=", "Off"]},
		fields=["name", "server", "backup_frequency", "backup_with_files", "last_backup_at"],
	)
	queued = 0
	for site in sites:
		if site.server not in active_servers:
			continue
		try:
			if not ou.is_backup_due(site.backup_frequency, site.last_backup_at, now):
				continue
		except ValueError:
			continue
		if frappe.db.exists("Site Backup", {"site": site.name, "status": ["in", ["Pending", "Running"]]}):
			continue
		try:
			frappe.get_doc(
				{
					"doctype": "Site Backup",
					"site": site.name,
					"with_files": 1 if site.backup_with_files is None else int(site.backup_with_files),
					"purpose": "Scheduled",
				}
			).insert(ignore_permissions=True)
			queued += 1
		except Exception:
			frappe.log_error(title=f"Nexaploy: scheduled backup failed for {site.name}")
	if queued:
		frappe.db.commit()


def prune_remote_backups():
	"""Daily: backup.prune_remote per site and remote location (prefix/bench) its backups live in,
	keeping backup_retention_days (0 = forever) and always the newest 3. Archived sites are kept.
	"""
	sites = frappe.get_all(
		"Site",
		filters={"status": ["in", ["Active", "Broken", "Suspended"]], "backup_retention_days": [">", 0]},
		fields=["name", "server", "backup_retention_days"],
	)
	from nexaploy.agent_job import create_agent_job

	for site in sites:
		if frappe.db.get_value("Server", site.server, "status") != "Active":
			continue
		if frappe.db.exists(
			"Agent Job",
			{"kind": "backup.prune_remote", "reference_doctype": "Site", "reference_name": site.name,
			 "status": ["in", ACTIVE_JOB_STATUSES]},
		):
			continue
		paths = frappe.get_all(
			"Site Backup", filters={"site": site.name, "status": "Success"}, pluck="remote_path"
		)
		for prefix, bench in ou.prune_targets(paths, site.name):
			try:
				create_agent_job(
					site.server,
					"backup.prune_remote",
					{"site": site.name, "bench": bench, "prefix": prefix or None,
					 "keep_days": int(site.backup_retention_days), "keep_min": ou.KEEP_MIN_DEFAULT},
					"Site",
					site.name,
				)
			except Exception:
				frappe.log_error(title=f"Nexaploy: prune_remote failed for {site.name}")
	frappe.db.commit()


def check_alerts():
	"""Every 5 min: disk > 85%, server Offline, unhealthy containers -> email the team (and
	Nexaploy Settings.alert_email), at most once per 6 h per server and kind.
	"""
	from nexaploy.ops import send_alert

	now = now_datetime()
	servers = frappe.get_all(
		"Server",
		filters={"status": ["in", ["Active", "Offline"]]},
		fields=["name", "server_id", "hostname", "team", "status", "last_heartbeat",
		        "disk_used_percent", "unhealthy_containers", "alert_state"],
	)
	for server in servers:
		state = ou.load_alert_state(server.alert_state)
		changed = False
		for kind, message in ou.server_alerts(server):
			if not ou.should_alert(state, kind, now):
				continue
			try:
				send_alert(server.team, f"{kind} alert on {server.hostname or server.name}", message)
			except Exception:
				frappe.log_error(title=f"Nexaploy: alert email failed for {server.name}")
				continue
			state = ou.record_alert(state, kind, now)
			changed = True
		if changed:
			frappe.db.set_value(
				"Server", server.name, {"alert_state": frappe.as_json(state), "last_alert_at": now},
				update_modified=False,
			)
	frappe.db.commit()


def prune_server_metrics():
	"""Daily: delete Server Metric rows older than 30 days."""
	cutoff = add_to_date(now_datetime(), days=-ou.METRIC_RETENTION_DAYS)
	frappe.db.delete("Server Metric", {"timestamp": ["<", cutoff]})
	frappe.db.commit()


def _ping(site):
	import requests

	try:
		resp = requests.get(ou.ping_url(site), timeout=UPTIME_TIMEOUT, allow_redirects=False)
		return resp.status_code == 200
	except Exception:
		return False


def check_sites_uptime():
	"""Every 5 min: GET https://<site>/api/method/ping (10 s timeout) for Active sites; store
	is_up / last_uptime_check and email the team when a site goes down or comes back.
	"""
	from concurrent.futures import ThreadPoolExecutor

	from nexaploy.ops import send_alert

	sites = frappe.get_all(
		"Site", filters={"status": "Active"}, fields=["name", "team", "is_up", "last_uptime_check"]
	)
	if not sites:
		return
	with ThreadPoolExecutor(max_workers=min(UPTIME_WORKERS, len(sites))) as pool:
		results = dict(zip([s.name for s in sites], pool.map(_ping, [s.name for s in sites])))
	now = now_datetime()
	for site in sites:
		up = results[site.name]
		transition = ou.uptime_transition(site.is_up, bool(site.last_uptime_check), up)
		frappe.db.set_value(
			"Site", site.name, {"is_up": int(up), "last_uptime_check": now}, update_modified=False
		)
		if transition:
			try:
				send_alert(
					site.team,
					f"{site.name} is {'DOWN' if transition == 'down' else 'back UP'}",
					f"{ou.ping_url(site.name)} {'stopped responding' if transition == 'down' else 'responds again'} at {now}.",
				)
			except Exception:
				frappe.log_error(title=f"Nexaploy: uptime alert failed for {site.name}")
	frappe.db.commit()


def apply_auto_updates():
	"""Every 15 min: roll benches of auto_update Release Groups to the latest successful build
	when their server is inside the group's update_window (server-local time).
	"""
	from nexaploy.ops import apply_auto_updates as apply

	for group in frappe.get_all("Release Group", filters={"auto_update": 1}, pluck="name"):
		try:
			apply(group)
		except Exception:
			frappe.log_error(title=f"Nexaploy: auto update failed for {group}")
	frappe.db.commit()


# --------------------------------------------------------------------------- plan limits & usage

SITE_USAGE_RETENTION_DAYS = 400


def enforce_plan_limits():
	"""Every 15 min: compare each site's usage today (UTC; Site Usage from agent heartbeats) with
	its plan limits and apply the plan's overage_action (ops_utils.usage_status /
	enforcement_actions): Notify -> email the team at 80 % and 100 % (once per 24 h per level);
	Throttle -> Frappe `rate_limit` in site_config while over the CPU budget, lifted the next UTC
	day or when back under; Suspend -> Site Suspended + maintenance_mode while over any limit,
	lifted automatically. Sites suspended for billing are never touched.
	"""
	from nexaploy.ops import enforce_site_limits

	sites = frappe.get_all(
		"Site",
		filters={"status": ["in", ["Active", "Suspended"]]},
		or_filters={"plan": ["is", "set"], "throttled": 1, "usage_suspended": 1},
		pluck="name",
	)
	for name in sites:
		try:
			enforce_site_limits(name)
		except Exception:
			frappe.log_error(title=f"Nexaploy: plan limit enforcement failed for {name}")
	frappe.db.commit()


def prune_site_usage():
	"""Daily: delete Site Usage rows older than 400 days."""
	from datetime import datetime, timedelta

	cutoff = (datetime.utcnow() - timedelta(days=SITE_USAGE_RETENTION_DAYS)).date()
	frappe.db.delete("Site Usage", {"date": ["<", cutoff]})
	frappe.db.commit()
