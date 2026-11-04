"""Offsite attachments, DR replica and trash: control-plane glue (ARCHITECTURE.md §15).

* Storage Profile        – an S3-compatible bucket (platform, or a team's own bucket)
* Site Storage Policy    – per site: Local / Offloaded, primary / replica / trash profiles
* Storage Settings       – operator switches, defaults, add-on plans, backup profile

Policy changes enqueue agent jobs (site.storage_*); the scheduler replicates and prunes the
trash per server (server.storage_*). Callbacks (bottom) write results back. Pure logic lives
in nexaploy.storage_utils.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime

from nexaploy import storage_utils as su
from nexaploy.agent_job import create_agent_job
from nexaploy.utils import secret_ref

SETTINGS = "Storage Settings"
PROFILE = "Storage Profile"
POLICY = "Site Storage Policy"
ACTIVE_JOB_STATUSES = ("Pending", "Queued", "Running")
SITE_KINDS = (
	"site.storage_configure",
	"site.storage_migrate",
	"site.storage_status",
	"site.storage_failover",
	"site.storage_trash_list",
	"site.storage_trash_restore",
)


# --------------------------------------------------------------------------- settings & profiles


def get_settings():
	return frappe.get_cached_doc(SETTINGS)


def settings_dict():
	return get_settings().as_dict()


def addon_plans(settings=None):
	settings = settings or get_settings()
	return {key: settings.get(field) for key, field in su.ADDON_FIELDS.items()}


def profile_params(name):
	row = frappe.db.get_value(
		PROFILE, name, ["name", "profile_name", "provider", "endpoint", "region", "addressing", "bucket",
		                "path_prefix", "access_key"], as_dict=True,
	)
	if not row:
		frappe.throw(_("Storage profile {0} not found").format(name))
	return su.profile_job_params(row, secret_ref(PROFILE, row.name, "secret_key"))


def profiles_params(names):
	return {n: profile_params(n) for n in dict.fromkeys(n for n in names if n)}


def profile_view(row):
	"""A profile without its secret (portal / admin API)."""
	return {
		"name": row.name,
		"title": row.title or row.name,
		"provider": row.provider,
		"provider_label": su.PROVIDER_PRESETS.get(row.provider, {}).get("label", row.provider),
		"region_label": row.region_label,
		"endpoint": row.endpoint,
		"region": row.region,
		"bucket": row.bucket,
		"path_prefix": row.path_prefix,
		"addressing": row.addressing,
		"access_key": row.access_key,
		"team": row.team,
		"enabled": bool(row.enabled),
	}


PROFILE_FIELDS = ["name", "title", "provider", "region_label", "endpoint", "region", "bucket", "path_prefix",
                  "addressing", "access_key", "team", "enabled"]


def usable_profiles(team):
	"""Enabled platform profiles + the team's own."""
	rows = frappe.get_all(PROFILE, filters={"enabled": 1}, fields=PROFILE_FIELDS, order_by="name asc")
	return [profile_view(r) for r in rows if su.profile_usable(r, team)]


# --------------------------------------------------------------------------- policies


def site_row(site):
	row = frappe.db.get_value("Site", site, ["name", "team", "server", "bench", "status"], as_dict=True)
	if not row:
		frappe.throw(_("Site {0} not found").format(site))
	return row


def get_policy(site, create=False):
	if frappe.db.exists(POLICY, site):
		return frappe.get_doc(POLICY, site)
	if not create:
		return None
	s = get_settings()
	doc = frappe.get_doc({
		"doctype": POLICY,
		"site": site,
		"attachments_mode": "Local",
		"status": "Local",
		"primary_profile": s.default_primary_profile,
		"replica_profile": s.default_replica_profile if s.replica_enabled else None,
		"trash_profile": s.default_trash_profile if s.trash_enabled else None,
		"trash_retention_days": s.default_trash_retention_days or 30,
	})
	doc.flags.skip_validate_profiles = True
	doc.insert(ignore_permissions=True)
	return doc


def policy_dict(policy):
	return {
		"site": policy.site,
		"attachments_mode": policy.attachments_mode,
		"primary_profile": policy.primary_profile,
		"replica_profile": policy.replica_profile,
		"trash_profile": policy.trash_profile,
		"trash_retention_days": policy.trash_retention_days,
		"active_target": policy.active_target or "primary",
		"status": policy.status,
	}


def job_params(policy, **extra):
	p = policy_dict(policy)
	names = [p["primary_profile"], p["replica_profile"], p["trash_profile"]]
	params = su.policy_job_params(p, profiles_params(names), settings_dict())
	params.update(extra)
	return params


def enqueue_site_job(policy, kind, **extra):
	site = site_row(policy.site)
	if not site.server:
		frappe.throw(_("Site {0} has no server").format(site.name))
	if kind in ("site.storage_trash_list", "site.storage_trash_restore", "site.storage_status"):
		params = {"site": policy.site, **extra}
		names = [policy.primary_profile, policy.replica_profile, policy.trash_profile]
		params["profiles"] = list(profiles_params(names).values())
		params.setdefault("primary", policy.primary_profile)
		params.setdefault("replica", policy.replica_profile or None)
		if policy.trash_profile:
			params.setdefault("trash", policy.trash_profile)
	else:
		params = job_params(policy, **extra)
	job = create_agent_job(site.server, kind, params, POLICY, policy.name)
	if policy.get("server") != site.server:
		frappe.db.set_value(POLICY, policy.name, "server", site.server, update_modified=False)
	return job.name


def storage_job_running(policy_name):
	return frappe.db.exists(
		"Agent Job",
		{"reference_doctype": POLICY, "reference_name": policy_name, "status": ["in", ACTIVE_JOB_STATUSES],
		 "kind": ["in", ["site.storage_migrate", "site.storage_failover", "site.storage_configure"]]},
	)


def apply_policy(policy, previous):
	"""Enqueue what moves the site from `previous` (policy_dict before the change) to `policy`.

	Returns the Agent Job name (or None)."""
	mode, before = policy.attachments_mode, (previous or {}).get("attachments_mode") or "Local"
	job = None
	s = get_settings()
	if mode == "Offloaded" and before != "Offloaded":
		policy.db_set({"status": "Migrating", "last_error": None, "active_target": "primary"})
		policy.active_target = "primary"
		job = enqueue_site_job(policy, "site.storage_migrate", direction="to_bucket",
		                       keep_local_copy=bool(s.keep_local_copy))
	elif mode == "Local" and before == "Offloaded":
		policy.db_set({"status": "Migrating Back", "last_error": None})
		job = enqueue_site_job(policy, "site.storage_migrate", direction="to_local")
	elif mode == "Offloaded":
		changed = any(policy.get(k) != (previous or {}).get(k) for k in
		              ("replica_profile", "trash_profile", "trash_retention_days"))
		if changed:
			job = enqueue_site_job(policy, "site.storage_configure")
	sync_site_addons(policy.site)
	return job


def reconfigure_profile_users(profile_name):
	"""A profile's bucket or credentials changed: refresh every offloaded site that uses it."""
	names = frappe.get_all(
		POLICY,
		filters={"attachments_mode": "Offloaded", "status": ["in", ["Offloaded", "Failed Over"]]},
		or_filters=[["primary_profile", "=", profile_name], ["replica_profile", "=", profile_name],
		            ["trash_profile", "=", profile_name]],
		pluck="name",
	)
	for name in names:
		try:
			enqueue_site_job(frappe.get_doc(POLICY, name), "site.storage_configure")
		except Exception:
			frappe.log_error(title=f"Nexaploy: storage reconfigure failed for {name}")
	return names


def apply_backup_profile(profile_name):
	"""DB backups to a Storage Profile: copy it into Nexaploy Settings' backup storage.

	The existing Nexaploy Settings.on_update then pushes server.configure_backups to every
	server, so backups keep a single code path (§5.5)."""
	profile = frappe.get_doc(PROFILE, profile_name)
	if profile.team:
		frappe.throw(_("Only a platform profile can hold the platform's database backups"))
	values = su.backup_settings_from_profile(profile.as_dict())
	ns = frappe.get_single("Nexaploy Settings")
	if not values.get("backup_prefix"):
		values["backup_prefix"] = ns.backup_prefix or "backups"
	for key, value in values.items():
		ns.set(key, value)
	ns.backup_secret_key = profile.get_password("secret_key", raise_exception=False)
	ns.flags.ignore_permissions = True
	ns.save()


# --------------------------------------------------------------------------- billing


def site_addon_plans(site):
	"""{plan name: price} of the storage add-ons `site` must be subscribed to."""
	try:
		policy = frappe.db.get_value(
			POLICY, site, ["site", "attachments_mode", "replica_profile", "trash_profile"], as_dict=True
		)
	except Exception:  # table missing before migrate
		return {}
	if not policy:
		return {}
	team = frappe.db.get_value("Site", site, "team")
	if team and frappe.get_meta("Team").has_field("billing_exempt") and frappe.db.get_value(
		"Team", team, "billing_exempt"
	):
		return {}
	out = {}
	for plan in su.desired_addons(policy, addon_plans()).values():
		row = frappe.db.get_value("Nexaploy Plan", plan, ["name", "enabled", "price_monthly"], as_dict=True)
		if row and row.enabled:
			out[row.name] = row.price_monthly or 0
	return out


def sync_site_addons(site):
	if not frappe.db.get_single_value("Billing Settings", "enabled"):
		return
	from nexaploy.billing.subscriptions import sync_site_addons as _sync

	_sync(frappe.get_doc("Site", site))


# --------------------------------------------------------------------------- scheduler


def _server_busy(server, kind):
	return frappe.db.exists(
		"Agent Job", {"server": server, "kind": kind, "status": ["in", ACTIVE_JOB_STATUSES]}
	)


def _live_policies(extra_filters=None):
	filters = {"attachments_mode": "Offloaded"}
	filters.update(extra_filters or {})
	rows = frappe.get_all(
		POLICY, filters=filters,
		fields=["name", "site", "attachments_mode", "status", "primary_profile", "replica_profile",
		        "trash_profile", "trash_retention_days", "active_target", "last_sync_at", "server"],
	)
	out = []
	for r in rows:
		site = frappe.db.get_value("Site", r.site, ["server", "status"], as_dict=True)
		if not site or site.status in ("Archived", "Broken") or not site.server:
			continue
		r.site_server = site.server
		out.append(r)
	return out


def schedule_replication():
	"""Scheduler (5 min): one server.storage_replicate per server with sites due.

	Also re-mounts sites whose server changed (site moved): their DB-only backup was restored
	on the new server, the bucket already holds the files."""
	s = get_settings()
	if not s.enabled:
		return
	now = now_datetime()
	due = []
	for r in _live_policies():
		if r.server and r.server != r.site_server and r.status in ("Offloaded", "Failed Over"):
			try:
				policy = frappe.get_doc(POLICY, r.name)
				policy.db_set("status", "Migrating")
				enqueue_site_job(policy, "site.storage_migrate", direction="to_bucket",
				                 keep_local_copy=bool(s.keep_local_copy))
			except Exception:
				frappe.log_error(title=f"Nexaploy: storage remount after move failed for {r.site}")
			continue
		if not s.replica_enabled or not su.replicate_site_params(r, s.as_dict()):
			continue
		if su.replication_due(r.last_sync_at, s.replication_interval_minutes, now):
			r.server = r.site_server
			due.append(r)
	for server, rows in su.group_by_server(due).items():
		if _server_busy(server, "server.storage_replicate"):
			continue
		try:
			sites = [su.replicate_site_params(r, s.as_dict()) for r in rows]
			names = [n for r in rows for n in (r.primary_profile, r.replica_profile, r.trash_profile)]
			params = {"sites": sites, "profiles": list(profiles_params(names).values())}
			create_agent_job(server, "server.storage_replicate", params, "Server", server)
		except Exception:
			frappe.log_error(title=f"Nexaploy: storage replication scheduling failed for {server}")


def prune_trash():
	"""Scheduler (daily): server.storage_prune_trash per server for sites with a trash bin."""
	s = get_settings()
	if not s.trash_enabled:
		return
	rows = [r for r in _live_policies({"trash_profile": ["is", "set"]})]
	for r in rows:
		r.server = r.site_server
	for server, group in su.group_by_server(rows).items():
		if _server_busy(server, "server.storage_prune_trash"):
			continue
		try:
			sites = [{"site": r.site, "trash": r.trash_profile,
			          "retention_days": int(r.trash_retention_days or s.default_trash_retention_days or 30)}
			         for r in group]
			params = {"sites": sites, "profiles": list(profiles_params(r.trash_profile for r in group).values())}
			create_agent_job(server, "server.storage_prune_trash", params, "Server", server)
		except Exception:
			frappe.log_error(title=f"Nexaploy: trash pruning scheduling failed for {server}")


# --------------------------------------------------------------------------- callbacks
# Registered with nexaploy.agent_callbacks.handles (imported at the bottom of that module).


def _result(job):
	if not job.result:
		return {}
	try:
		return frappe.parse_json(job.result) or {}
	except Exception:
		return {}


def _params(job):
	try:
		return frappe.parse_json(job.params) or {}
	except Exception:
		return {}


def register(handles):
	@handles("site.storage_migrate")
	def on_storage_migrate(job):
		if job.status not in ("Success", "Failure"):
			return
		policy = frappe.get_doc(POLICY, job.reference_name)
		to_local = _params(job).get("direction") == "to_local"
		if job.status == "Success":
			if to_local:
				policy.db_set({"status": "Local", "attachments_mode": "Local", "active_target": "primary",
				               "last_error": None})
			else:
				policy.db_set({"status": "Offloaded", "attachments_mode": "Offloaded", "last_error": None,
				               "offloaded_at": policy.offloaded_at or now_datetime()})
		else:
			# the agent rolled back: the site keeps serving from where it was
			policy.db_set({
				"status": "Offloaded" if to_local else "Local",
				"attachments_mode": "Offloaded" if to_local else "Local",
				"last_error": (job.error or "")[:2000],
			})
		sync_site_addons(policy.site)

	@handles("site.storage_configure")
	def on_storage_configure(job):
		if job.status == "Failure":
			frappe.db.set_value(POLICY, job.reference_name, "last_error", (job.error or "")[:2000])
		elif job.status == "Success":
			frappe.db.set_value(POLICY, job.reference_name, "last_error", None)

	@handles("site.storage_failover")
	def on_storage_failover(job):
		if job.status not in ("Success", "Failure"):
			return
		target = _params(job).get("target")
		if job.status == "Success":
			frappe.db.set_value(POLICY, job.reference_name, {
				"active_target": target,
				"status": "Failed Over" if target == "replica" else "Offloaded",
				"last_error": None,
			})
		else:
			frappe.db.set_value(POLICY, job.reference_name, "last_error", (job.error or "")[:2000])

	@handles("site.storage_status")
	def on_storage_status(job):
		if job.status == "Success":
			frappe.db.set_value(POLICY, job.reference_name, {
				"last_status": frappe.as_json(_result(job)), "last_status_at": now_datetime(),
			}, update_modified=False)

	@handles("site.storage_trash_list", "site.storage_trash_restore")
	def on_storage_trash(job):
		"""Nothing to store: the portal reads the job result."""

	@handles("server.storage_replicate")
	def on_storage_replicate(job):
		if job.status not in ("Success", "Failure"):
			return
		sites = (_result(job) or {}).get("sites") or {}
		for site, entry in sites.items():
			if not frappe.db.exists(POLICY, site):
				continue
			values = su.replication_outcome(entry)
			values["last_sync_at"] = now_datetime()
			frappe.db.set_value(POLICY, site, values, update_modified=False)

	@handles("server.storage_prune_trash")
	def on_storage_prune(job):
		if job.status not in ("Success", "Failure"):
			return
		for site, status in ((_result(job) or {}).get("sites") or {}).items():
			if status == "success" and frappe.db.exists(POLICY, site):
				frappe.db.set_value(POLICY, site, "last_prune_at", now_datetime(), update_modified=False)
