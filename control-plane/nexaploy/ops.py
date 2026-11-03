"""Operations glue (frappe side): registry params, backup configuration push, metrics,
alerts, auto updates. Pure logic lives in nexaploy.ops_utils.
"""

from datetime import datetime

import frappe
from frappe.utils import now_datetime

from nexaploy import ops_utils as ou
from nexaploy.agent_job import create_agent_job
from nexaploy.utils import secret_ref

SETTINGS = "Nexaploy Settings"
ACTIVE_JOB_STATUSES = ("Pending", "Queued", "Running")


def get_settings():
	return frappe.get_cached_doc(SETTINGS)


def sync_billing(doc):
	"""Let billing re-evaluate a Server/Site subscription; never breaks operations."""
	try:
		from nexaploy.billing.subscriptions import sync_subscription

		sync_subscription(doc)
	except Exception:
		frappe.log_error(title=f"Nexaploy billing sync failed: {doc.doctype} {doc.name}")


# --------------------------------------------------------------------------- registry


def registry_url():
	try:
		return ou.normalize_registry_url(get_settings().registry_url)
	except ValueError:
		return None


def image_tag_for(release_group, build_id):
	return ou.build_image_tag(release_group, build_id, registry_url())


def registry_params(image=None):
	"""`{url, username, password}` for agent jobs, or None.

	With `image`, only when that image lives on the configured registry. The password is a
	secret placeholder resolved at poll time (never stored in Agent Job.params).
	"""
	url = registry_url()
	if not url or (image and not ou.registry_covers(url, image)):
		return None
	settings = get_settings()
	params = {"url": url, "username": settings.registry_username or ""}
	if settings.registry_username:
		params["password"] = secret_ref(SETTINGS, SETTINGS, "registry_password")
	return params


def default_build_server():
	return get_settings().default_build_server


# --------------------------------------------------------------------------- backups configuration


def backup_settings_values(settings=None):
	settings = settings or get_settings()
	values = {f: settings.get(f) for f in ou.BACKUP_SETTINGS_FIELDS}
	values["backup_secret_key"] = settings.get_password("backup_secret_key", raise_exception=False)
	return values


def configure_backups(server):
	"""Enqueue server.configure_backups for one server (no-op if storage not configured)."""
	settings = get_settings()
	if not ou.backup_configured(settings.as_dict()):
		return None
	row = frappe.db.get_value("Server", server, ["server_id", "status"], as_dict=True)
	if not row or row.status == "Archived":
		return None
	params = ou.backup_job_params(
		settings.as_dict(), row.server_id, secret_ref(SETTINGS, SETTINGS, "backup_secret_key")
	)
	return create_agent_job(server, "server.configure_backups", params, "Server", server).name


def configure_backups_everywhere():
	for server in frappe.get_all("Server", filters={"status": ["in", ["Active", "Offline"]]}, pluck="name"):
		try:
			configure_backups(server)
		except Exception:
			frappe.log_error(title=f"Nexaploy: configure_backups failed for {server}")


def on_server_activated(server):
	"""A server just became Active (register / first heartbeat): push backup storage."""
	try:
		configure_backups(server)
	except Exception:
		frappe.log_error(title=f"Nexaploy: configure_backups failed for {server}")


# --------------------------------------------------------------------------- metrics


def record_heartbeat_metrics(server, facts):
	"""Update Server monitoring fields; insert a Server Metric at most every 5 minutes."""
	if not isinstance(facts, dict):
		return
	m = ou.metric_from_facts(facts)
	frappe.db.set_value(
		"Server",
		server,
		{
			"cpu_count": m["cpu_count"],
			"memory_total_mb": m["memory_total_mb"],
			"disk_total_gb": m["disk_total_gb"],
			"disk_used_percent": m["disk_used_percent"],
			"unhealthy_containers": m["unhealthy_containers"],
		},
		update_modified=False,
	)
	now = now_datetime()
	last = frappe.db.get_value("Server Metric", {"server": server}, "timestamp", order_by="timestamp desc")
	if not ou.should_record_metric(last, now):
		return
	frappe.get_doc(
		{
			"doctype": "Server Metric",
			"server": server,
			"timestamp": now,
			"load_1": m["load_1"],
			"memory_used_mb": m["memory_used_mb"],
			"memory_total_mb": m["memory_total_mb"],
			"disk_used_gb": m["disk_used_gb"],
			"disk_total_gb": m["disk_total_gb"],
			"unhealthy_containers": m["unhealthy_containers"],
			"raw": frappe.as_json(facts),
		}
	).insert(ignore_permissions=True)


# --------------------------------------------------------------------------- alerts


def alert_recipients(team=None):
	recipients = []
	if team:
		email = frappe.db.get_value("User", team, "email") or (team if "@" in team else None)
		if email:
			recipients.append(email)
	ops = get_settings().alert_email
	if ops and ops not in recipients:
		recipients.append(ops)
	return recipients


def send_alert(team, subject, message):
	recipients = alert_recipients(team)
	if not recipients:
		return False
	frappe.sendmail(recipients=recipients, subject=f"[Nexaploy] {subject}", message=message, delayed=True)
	return True


# --------------------------------------------------------------------------- auto updates


def bench_update_in_progress(bench):
	return bool(
		frappe.db.exists(
			"Agent Job",
			{
				"reference_doctype": "Bench",
				"reference_name": bench,
				"kind": "bench.update",
				"status": ["in", list(ACTIVE_JOB_STATUSES)],
			},
		)
	)


def apply_auto_updates(release_group, force_window=False):
	"""Enqueue bench.update for Active benches of an auto_update group not on the latest build,
	when the bench's server is inside the group's update window (server-local time).
	Returns the bench names updated.
	"""
	group = frappe.get_doc("Release Group", release_group)
	if not group.auto_update:
		return []
	dc_name, latest = group.latest_image()
	if not latest:
		return []
	benches = frappe.get_all(
		"Bench",
		filters={"release_group": release_group, "status": "Active"},
		fields=["name", "status", "image_tag", "server"],
	)
	for b in benches:
		b["update_in_progress"] = bench_update_in_progress(b["name"])
	updated = []
	utc_now = datetime.utcnow()
	for name in ou.benches_to_update(benches, latest):
		bench = frappe.get_doc("Bench", name)
		facts = frappe.parse_json(frappe.db.get_value("Server", bench.server, "facts") or "{}") or {}
		try:
			inside = ou.in_update_window(group.update_window, ou.server_local_time(utc_now, facts))
		except ValueError:
			inside = False
		if not (inside or force_window):
			continue
		bench.flags.ignore_permissions = True
		bench.update_image(dc_name)
		updated.append(name)
	return updated
