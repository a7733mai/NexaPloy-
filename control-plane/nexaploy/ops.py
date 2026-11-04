"""Operations glue (frappe side): registry params, backup configuration push, metrics,
alerts, auto updates, plan usage limits, backup downloads and site config edits.
Pure logic lives in nexaploy.ops_utils.
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
	"""Enqueue server.configure_backups for one server: its Backup Destination (explicit, else
	the team default) when usable, else the platform storage of Nexaploy Settings (no-op when
	neither is configured)."""
	row = frappe.db.get_value(
		"Server", server, ["server_id", "status", "team", "backup_destination"], as_dict=True
	)
	if not row or row.status == "Archived":
		return None
	dest = server_destination(row)
	if dest is not None:
		params = destination_params(dest, row.server_id)
	else:
		settings = get_settings()
		if not ou.backup_configured(settings.as_dict()):
			return None
		params = ou.backup_job_params(
			settings.as_dict(), row.server_id, secret_ref(SETTINGS, SETTINGS, "backup_secret_key")
		)
	return create_agent_job(server, "server.configure_backups", params, "Server", server).name


# --------------------------------------------------------------------------- backup destinations
# Team-owned Backup Destination (S3 / Google Drive / OneDrive / Dropbox); pure rules in
# nexaploy.backup_destination_utils. Secrets travel as __secret__ placeholders.

DESTINATION = "Backup Destination"


def oauth_client(provider, with_secret=False):
	"""{client_id, client_secret, tenant} of the platform OAuth app for `provider`.
	client_secret is the decrypted value (with_secret) or a __secret__ placeholder."""
	from nexaploy import backup_destination_utils as bdu

	id_field, secret_field = bdu.SETTINGS_CLIENT_FIELDS[provider]
	settings = get_settings()
	client_id = (settings.get(id_field) or "").strip()
	if with_secret:
		secret = settings.get_password(secret_field, raise_exception=False) if client_id else None
	else:
		secret = secret_ref(SETTINGS, SETTINGS, secret_field) if client_id else None
	return {"client_id": client_id, "client_secret": secret, "tenant": settings.get("microsoft_tenant") or "common"}


def oauth_configured(provider):
	client = oauth_client(provider, with_secret=True)
	return bool(client["client_id"] and client["client_secret"])


def team_default_destination(team):
	if not team:
		return None
	return frappe.db.get_value(DESTINATION, {"team": team, "is_default": 1}, "name")


def server_destination(row):
	"""The usable Backup Destination doc of a Server row {team, backup_destination}, or None."""
	from nexaploy import backup_destination_utils as bdu

	name = bdu.effective_destination(row.get("backup_destination"), team_default_destination(row.get("team")))
	if not name or not frappe.db.exists(DESTINATION, name):
		return None
	dest = frappe.get_doc(DESTINATION, name)
	if dest.team != row.get("team") or not bdu.destination_usable(dest.as_dict()):
		return None
	return dest


def _destination_secret_field(dest):
	from nexaploy import backup_destination_utils as bdu

	return "s3_secret_key" if dest.destination_type == bdu.S3 else "oauth_token"


def destination_params(dest, server_id, resolve=False):
	"""server.configure_backups params for `server_id` using `dest` (placeholders, or real
	secrets with resolve=True for fingerprints)."""
	from nexaploy import backup_destination_utils as bdu

	field = _destination_secret_field(dest)
	if resolve:
		secret = dest.get_password(field, raise_exception=False) or ""
	else:
		secret = secret_ref(DESTINATION, dest.name, field)
	client = None
	if dest.destination_type in bdu.OAUTH_DESTINATION_TYPES:
		client = oauth_client(bdu.provider_for_type(dest.destination_type), with_secret=resolve)
	try:
		return bdu.destination_job_params(dest.as_dict(), server_id, secret, client)
	except bdu.DestinationError as e:
		frappe.throw(str(e))


def servers_using_destination(dest):
	"""Non-archived servers whose effective destination is `dest`."""
	explicit = frappe.get_all(
		"Server", filters={"backup_destination": dest.name, "status": ["!=", "Archived"]}, pluck="name"
	)
	implicit = []
	if dest.is_default:
		implicit = frappe.get_all(
			"Server",
			filters={"team": dest.team, "status": ["!=", "Archived"], "backup_destination": ["in", ["", None]]},
			pluck="name",
		)
	return sorted(set(explicit) | set(implicit))


def push_destination_if_changed(dest):
	"""Re-push server.configure_backups to the servers using `dest` when what they receive
	changed (incl. secrets and the OAuth client, compared by hash), or when it became the team
	default / stopped being it."""
	from nexaploy import backup_destination_utils as bdu

	field = _destination_secret_field(dest)
	client = None
	if dest.destination_type in bdu.OAUTH_DESTINATION_TYPES:
		client = oauth_client(bdu.provider_for_type(dest.destination_type), with_secret=True)
	values = dest.as_dict()
	fingerprint = bdu.destination_fingerprint(values, dest.get_password(field, raise_exception=False), client)
	if not bdu.destination_usable(values):
		fingerprint = ""
	default_changed = dest.has_value_changed("is_default") if hasattr(dest, "has_value_changed") else False
	if fingerprint == (dest.config_hash or "") and not default_changed:
		return []
	dest.db_set("config_hash", fingerprint, update_modified=False)
	targets = servers_using_destination(dest)
	if default_changed and not dest.is_default:
		# servers that followed this default now fall back to the platform storage
		targets = sorted(set(targets) | set(frappe.get_all(
			"Server",
			filters={"team": dest.team, "status": ["!=", "Archived"], "backup_destination": ["in", ["", None]]},
			pluck="name",
		)))
	jobs = []
	for server in targets:
		if frappe.db.get_value("Server", server, "status") not in ("Active", "Offline"):
			continue
		try:
			jobs.append(configure_backups(server))
		except Exception:
			frappe.log_error(title=f"Nexaploy: configure_backups failed for {server}")
	return [j for j in jobs if j]


def test_destination_job(dest, server):
	"""Enqueue backup.test_remote on `server` for `dest` (temporary rclone config on the agent,
	the server's active backup remote is not touched)."""
	from nexaploy import backup_destination_utils as bdu

	server_id = frappe.db.get_value("Server", server, "server_id")
	params = bdu.remote_test_params(destination_params(dest, server_id))
	return create_agent_job(server, "backup.test_remote", params, DESTINATION, dest.name).name


# --------------------------------------------------------------------------- backup retention

def retention_addon_active(team):
	"""Hook point for the paid "extended backup retention" add-on: an app may register
	`nexaploy_backup_retention_addon = "dotted.path"` in hooks.py (called with the team, returns
	bool); otherwise Team.extended_backup_retention (set by billing / the operator) decides."""
	for path in frappe.get_hooks("nexaploy_backup_retention_addon") or []:
		try:
			return bool(frappe.get_attr(path)(team))
		except Exception:
			frappe.log_error(title="Nexaploy: backup retention add-on hook failed")
	return bool(team and frappe.db.get_value("Team", team, "extended_backup_retention"))


def retention_limits():
	from nexaploy import backup_policy_utils as bpu

	return bpu.limits_from(get_settings().as_dict())


def _policy_row(doctype, name):
	from nexaploy import backup_policy_utils as bpu

	fields = list(bpu.DOC_FIELDS.values())
	if doctype == "Site":
		fields.append("backup_retention_custom")
	return frappe.db.get_value(doctype, name, fields, as_dict=True) if name else None


def site_backup_policy(site):
	"""Effective retention policy of a site (its own when custom, else the team default)."""
	from nexaploy import backup_policy_utils as bpu

	team = frappe.db.get_value("Site", site, "team")
	return bpu.effective_policy(_policy_row("Site", site), _policy_row("Team", team))


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
	"""Team ops recipients (Owner/Admin/Developer, nexaploy.teams.team_notification_emails)
	plus Nexaploy Settings.alert_email. A legacy user-valued `team` (before the
	v0_2 user_team_to_team patch) still resolves to that user's email."""
	from nexaploy.teams import team_notification_emails

	recipients = []
	if team:
		if frappe.db.exists("Team", team):
			recipients.extend(team_notification_emails(team, "ops"))
		else:
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


# --------------------------------------------------------------------------- plan limits & usage

SITE_USAGE_FIELDS = [
	"name", "site_name", "team", "server", "status", "plan", "throttled", "usage_suspended",
	"suspended_for_billing", "database_mb", "storage_mb", "current_cpu_seconds_today",
	"over_limit", "usage_alert_state",
]
PLAN_LIMIT_FIELDS = [
	"name", "title", "cpu_seconds_per_day", "max_database_mb", "max_storage_mb", "overage_action",
	"plan_order", "price_monthly", "currency",
]


def utc_today():
	return datetime.utcnow().date()


def ingest_usage(server, usage):
	"""Upsert Site Usage rows (one per site per UTC day) from a heartbeat / site.usage block.

	Only sites that live on `server` are accepted. CPU seconds and requests are daily totals
	computed by the agent (never decreased here); database/storage are point-in-time values
	that also update the Site (with today's CPU seconds). Never raises: usage must not break the
	heartbeat. Returns the number of rows written.
	"""
	try:
		return _ingest_usage(server, usage)
	except Exception:
		frappe.log_error(title=f"Nexaploy: usage ingestion failed for {server}")
		return 0


def _ingest_usage(server, usage):
	rows = ou.usage_rows(usage)
	if not rows or not server:
		return 0
	owned = set(
		frappe.get_all(
			"Site",
			filters={"name": ["in", sorted({r["site"] for r in rows})], "server": server,
			         "status": ["!=", "Archived"]},
			pluck="name",
		)
	)
	today = utc_today().isoformat()
	written = 0
	now = now_datetime()
	for row in rows:
		site = row["site"]
		if site not in owned:
			continue
		values = {k: row[k] for k in ("cpu_seconds", "requests", "database_mb", "storage_mb") if k in row}
		existing = frappe.db.get_value(
			"Site Usage", {"site": site, "date": row["date"]}, ["name", "cpu_seconds", "requests"], as_dict=True
		)
		if existing:
			for k in ("cpu_seconds", "requests"):
				if k in values:
					values[k] = max(values[k], existing.get(k) or 0)
			frappe.db.set_value("Site Usage", existing.name, values, update_modified=False)
		else:
			frappe.get_doc(
				{"doctype": "Site Usage", "site": site, "date": row["date"], "server": server, **values}
			).insert(ignore_permissions=True)
		written += 1
		if row["date"] == today:
			site_values = {"usage_updated_at": now}
			if "cpu_seconds" in values:
				site_values["current_cpu_seconds_today"] = values["cpu_seconds"]
			for k in ("database_mb", "storage_mb"):
				if k in values:
					site_values[k] = values[k]
			frappe.db.set_value("Site", site, site_values, update_modified=False)
	return written


def site_usage_today(site):
	"""{cpu_seconds, requests, database_mb, storage_mb} for today (UTC); CPU resets each day."""
	row = frappe.db.get_value(
		"Site Usage", {"site": site.name, "date": utc_today()}, ["cpu_seconds", "requests"], as_dict=True
	) or {}
	return {
		"cpu_seconds": row.get("cpu_seconds") or 0,
		"requests": row.get("requests") or 0,
		"database_mb": site.database_mb or 0,
		"storage_mb": site.storage_mb or 0,
	}


def plan_limits(plan):
	if not plan:
		return {}
	return frappe.db.get_value("Nexaploy Plan", plan, PLAN_LIMIT_FIELDS, as_dict=True) or {}


def team_emails(team, kind="ops"):
	"""Team notification recipients (nexaploy.teams), falling back to the ops alert recipients."""
	try:
		from nexaploy.teams import team_notification_emails

		emails = list(team_notification_emails(team, kind=kind) or [])
	except Exception:
		emails = []
	return emails or alert_recipients(team)


def enqueue_config_update(site, set_values=None, remove=None):
	"""site.update_config {site, set, remove} on the site's server; returns the Agent Job name.

	Values of secret-like keys are scrubbed from Agent Job.params once the job finishes
	(agent_callbacks.on_site_update_config).
	"""
	row = frappe.db.get_value("Site", site, ["site_name", "server"], as_dict=True)
	if not row:
		frappe.throw(f"Site {site} not found")
	params = {"site": row.site_name, "set": set_values or {}, "remove": list(remove or [])}
	return create_agent_job(row.server, "site.update_config", params, "Site", site).name


def _usage_message(site, plan, status, level):
	lines = [
		f"Site {site.name} is {'over' if level == 'over' else 'close to'} the limits of its plan "
		f"{plan.get('title') or site.plan} today:",
		"",
	]
	labels = {"cpu": "CPU time (s/day)", "database": "Database (MB)", "storage": "Storage (MB)"}
	for name, m in status["metrics"].items():
		if m["limit"]:
			lines.append(f"- {labels[name]}: {m['used']:.1f} of {m['limit']:.0f} ({m['percent']}%)")
	action = ou.normalize_overage_action(plan.get("overage_action"))
	if level == "over" and action == "Throttle":
		lines += ["", "Requests are rate limited until the next UTC day or until you upgrade."]
	elif level == "over" and action == "Suspend":
		lines += ["", "The site is in maintenance mode until usage is back under the limits or you upgrade."]
	lines += ["", f"Upgrade the plan from the dashboard: /dashboard/site?name={site.name}#usage"]
	return "\n".join(lines)


def enforce_site_limits(site_name, now=None):
	"""Apply the plan's overage action to one site (pure decision in ops_utils). Returns actions."""
	site = frappe.db.get_value("Site", site_name, SITE_USAGE_FIELDS, as_dict=True)
	if not site or site.status not in ("Active", "Suspended"):
		return []
	if site.status == "Suspended" and not site.usage_suspended:
		return []  # suspended by billing / staff: not ours
	now = now or now_datetime()
	plan = plan_limits(site.plan)
	usage = site_usage_today(site)
	status = ou.usage_status(plan, usage)
	actions = ou.enforcement_actions(plan, status, site)
	updates = {
		"over_limit": int(status["status"] == "over"),
		"current_cpu_seconds_today": usage["cpu_seconds"],
	}
	set_values, remove = {}, []
	state = ou.load_alert_state(site.usage_alert_state)
	state_changed = False
	for action in actions:
		if action in ("notify_warn", "notify_over"):
			level = action.split("_", 1)[1]
			kind = f"usage_{level}"
			if not ou.should_alert(state, kind, now, ou.USAGE_ALERT_INTERVAL):
				continue
			recipients = team_emails(site.team)
			if recipients:
				try:
					frappe.sendmail(
						recipients=recipients,
						subject=f"[Nexaploy] {site.name} {'is over' if level == 'over' else 'is near'} its plan limits",
						message=_usage_message(site, plan, status, level),
						delayed=True,
					)
				except Exception:
					frappe.log_error(title=f"Nexaploy: usage email failed for {site.name}")
					continue
			state = ou.record_alert(state, kind, now)
			state_changed = True
		elif action == "throttle":
			set_values["rate_limit"] = ou.throttle_rate_limit(plan)
			updates["throttled"] = 1
		elif action == "unthrottle":
			remove.append("rate_limit")
			updates["throttled"] = 0
		elif action == "suspend":
			set_values["maintenance_mode"] = 1
			updates.update({"status": "Suspended", "usage_suspended": 1})
		elif action == "unsuspend":
			set_values["maintenance_mode"] = 0
			updates["usage_suspended"] = 0
			if site.status == "Suspended" and not site.suspended_for_billing:
				updates["status"] = "Active"
	if state_changed:
		updates["usage_alert_state"] = frappe.as_json(state)
	if set_values or remove:
		try:
			enqueue_config_update(site.name, set_values, remove)
		except Exception:
			frappe.log_error(title=f"Nexaploy: usage enforcement job failed for {site.name}")
			for k in ("throttled", "usage_suspended", "status"):
				updates.pop(k, None)
	frappe.db.set_value("Site", site.name, updates, update_modified=False)
	return actions


# --------------------------------------------------------------------------- backup downloads

DOWNLOAD_EXPIRES = 900


def backup_download_url(backup, file_type, expires=DOWNLOAD_EXPIRES):
	"""Presigned S3 GET URL (SigV4, `expires` seconds) for one file of a Site Backup."""
	doc = frappe.get_doc("Site Backup", backup)
	if doc.status != "Success":
		frappe.throw(f"Backup {backup} is {doc.status}; only successful backups can be downloaded")
	row = next((f for f in doc.get("files") or [] if f.file_type == file_type), None)
	if not row:
		frappe.throw(f"Backup {backup} has no {file_type} file")
	if not row.remote_key:
		frappe.throw(f"The {file_type} file of backup {backup} was not uploaded to backup storage")
	server = frappe.db.get_value("Site", doc.site, "server")
	srow = frappe.db.get_value("Server", server, ["team", "backup_destination"], as_dict=True) if server else None
	dest = server_destination(srow) if srow else None
	if dest is not None:
		# the backup went to the team's own destination
		from nexaploy import backup_destination_utils as bdu

		if dest.destination_type != bdu.S3:
			frappe.throw(
				f"This backup is stored in {dest.destination_type} ({dest.account_email or dest.title}); "
				"download it from there, or restore it from the dashboard"
			)
		secret = dest.get_password("s3_secret_key", raise_exception=False)
		if not secret:
			frappe.throw("Backup storage secret key is not set")
		return ou.presign_s3_get(
			dest.s3_endpoint, dest.s3_region, dest.s3_bucket, row.remote_key, dest.s3_access_key, secret,
			expires=expires,
		)
	settings = get_settings()
	if not ou.backup_configured(settings.as_dict()):
		frappe.throw("Backup storage is not configured")
	secret = settings.get_password("backup_secret_key", raise_exception=False)
	if not secret:
		frappe.throw("Backup storage secret key is not set")
	return ou.presign_s3_get(
		settings.backup_endpoint, settings.backup_region, settings.backup_bucket, row.remote_key,
		settings.backup_access_key, secret, expires=expires,
	)
