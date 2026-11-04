"""Offsite attachments, DR replica and trash: customer portal + operator API (ARCHITECTURE.md §15).

Customer methods resolve the site's team and check it with nexaploy.teams.check_team_access:
read -> state / trash listing, write -> failover / restore / status refresh, write + billing ->
changing what is enabled (add-ons are billed). Team buckets (BYO profiles) need `admin`.
Operator methods (`*_storage_settings`, `admin_*`) are platform-admin only
(nexaploy.teams.is_platform_admin) for the unified Administrator dashboard.
"""

import frappe
from frappe import _

from nexaploy import storage
from nexaploy import storage_utils as su

POLICY_FIELDS = ("attachments_mode", "primary_profile", "replica_profile", "trash_profile", "trash_retention_days")


# --------------------------------------------------------------------------- access


def _require_user():
	if not frappe.session.user or frappe.session.user == "Guest":
		raise frappe.PermissionError(_("Please log in"))


def _check(team, action):
	from nexaploy.teams import check_team_access

	check_team_access(team, action)


def _allowed(team, action):
	try:
		_check(team, action)
		return True
	except frappe.PermissionError:
		return False


def _site(site, action="read"):
	_require_user()
	if not site:
		frappe.throw(_("Site is required"))
	row = frappe.db.get_value("Site", site, ["name", "team", "server", "status"], as_dict=True)
	if not row:
		raise frappe.DoesNotExistError(_("Site {0} not found").format(site))
	_check(row.team, action)
	return row


def _require_platform_admin():
	from nexaploy.teams import is_platform_admin

	_require_user()
	if not is_platform_admin():
		raise frappe.PermissionError(_("Only platform administrators can do this"))


def _log(team, action, site, details=None):
	from nexaploy.activity import log_activity

	log_activity(team, action, "Site", site, details)


def _active_site(row):
	if row.status not in ("Active", "Suspended"):
		frappe.throw(_("Site {0} is {1}").format(row.name, _(row.status)))


def _plan_view(name):
	if not name:
		return None
	row = frappe.db.get_value("Nexaploy Plan", name, ["name", "title", "price_monthly", "currency", "enabled"],
	                          as_dict=True)
	return dict(row) if row else None


def _addons(settings):
	return {key: _plan_view(settings.get(field)) for key, field in su.ADDON_FIELDS.items()}


def _features(settings):
	return {
		"enabled": bool(settings.enabled),
		"replica": bool(settings.replica_enabled),
		"trash": bool(settings.trash_enabled),
		"byo": bool(settings.allow_byo_profiles),
		"default_trash_retention_days": settings.default_trash_retention_days or 30,
		"max_trash_retention_days": settings.max_trash_retention_days or 365,
	}


def _policy_view(policy):
	if not policy:
		return None
	last_status = None
	if policy.last_status:
		try:
			last_status = frappe.parse_json(policy.last_status)
		except Exception:
			last_status = None
	return {
		**{f: policy.get(f) for f in POLICY_FIELDS},
		"status": policy.status,
		"active_target": policy.active_target or "primary",
		"offloaded_at": policy.offloaded_at,
		"last_error": policy.last_error,
		"last_sync_at": policy.last_sync_at,
		"last_sync_status": policy.last_sync_status,
		"last_sync_error": policy.last_sync_error,
		"last_trashed": policy.last_trashed,
		"last_prune_at": policy.last_prune_at,
		"last_status": last_status,
		"last_status_at": policy.last_status_at,
	}


def _jobs(policy_name, limit=10):
	return frappe.get_all(
		"Agent Job",
		filters={"reference_doctype": storage.POLICY, "reference_name": policy_name},
		fields=["name", "kind", "status", "creation", "error"],
		order_by="creation desc", limit=limit,
	)


# --------------------------------------------------------------------------- customer: site


@frappe.whitelist()
def get_site_storage(site):
	"""The site's storage policy, usable profiles, feature switches and add-on prices."""
	row = _site(site, "read")
	settings = storage.get_settings()
	policy = storage.get_policy(row.name)
	return {
		"site": row.name,
		"policy": _policy_view(policy),
		"features": _features(settings),
		"addons": _addons(settings),
		"profiles": storage.usable_profiles(row.team),
		"defaults": {
			"primary_profile": settings.default_primary_profile,
			"replica_profile": settings.default_replica_profile,
			"trash_profile": settings.default_trash_profile,
		},
		"jobs": _jobs(row.name) if policy else [],
		"can_write": _allowed(row.team, "write"),
		"can_billing": _allowed(row.team, "billing"),
		"can_admin": _allowed(row.team, "admin"),
	}


@frappe.whitelist(methods=["POST"])
def update_site_storage(site, attachments_mode, primary_profile=None, replica_profile=None, trash_profile=None,
                        trash_retention_days=None):
	"""Turn offsite attachments / replica / trash on or off for a site (billed add-ons).

	Offloaded <- Local moves the files to the primary bucket; Local <- Offloaded copies them
	back. The primary of an offloaded site cannot change (bring the files back first)."""
	row = _site(site, "write")
	_check(row.team, "billing")
	_active_site(row)
	settings = storage.get_settings()
	policy = storage.get_policy(row.name, create=True)
	if policy.status in su.BUSY_STATUSES or storage.storage_job_running(policy.name):
		frappe.throw(_("A storage change is already in progress for {0}").format(row.name))
	if policy.status == "Failed Over":
		frappe.throw(_("The site is served from its replica; switch back to the primary first"))
	previous = storage.policy_dict(policy)
	values = {
		"attachments_mode": attachments_mode,
		"primary_profile": primary_profile or None,
		"replica_profile": replica_profile or None,
		"trash_profile": trash_profile or None,
		"trash_retention_days": int(trash_retention_days or settings.default_trash_retention_days or 30),
	}
	if attachments_mode == "Local":
		# keep the profiles for next time; replication and trash stop with the offload
		values.update({"primary_profile": policy.primary_profile or values["primary_profile"]})
	errors = su.validate_policy(
		values["attachments_mode"], values["primary_profile"], values["replica_profile"], values["trash_profile"],
		values["trash_retention_days"], settings.max_trash_retention_days or 365,
	) + su.feature_errors(values, settings.as_dict())
	if previous["attachments_mode"] == "Offloaded" and attachments_mode == "Offloaded" and (
		values["primary_profile"] != previous["primary_profile"]
	):
		errors.append("Move the files back to the server before changing the primary bucket")
	if errors:
		frappe.throw("<br>".join(_(e) for e in errors))
	policy.update(values)
	policy.save(ignore_permissions=True)
	job = storage.apply_policy(policy, previous)
	_log(row.team, "site.storage_update", row.name, {k: values[k] for k in POLICY_FIELDS})
	return {"policy": _policy_view(frappe.get_doc(storage.POLICY, policy.name)), "job": job}


@frappe.whitelist(methods=["POST"])
def failover_site_storage(site, target):
	"""Serve the site's files from the replica (primary region down) or back from the primary
	(after syncing the replica back). Replication pauses while failed over."""
	row = _site(site, "write")
	if target not in ("primary", "replica"):
		frappe.throw(_("Target must be primary or replica"))
	policy = storage.get_policy(row.name)
	if not policy or policy.attachments_mode != "Offloaded" or not policy.replica_profile:
		frappe.throw(_("Failover needs offloaded attachments with a replica"))
	if policy.status in su.BUSY_STATUSES or storage.storage_job_running(policy.name):
		frappe.throw(_("A storage change is already in progress for {0}").format(row.name))
	if (policy.active_target or "primary") == target:
		frappe.throw(_("The site is already served from the {0}").format(_(target)))
	job = storage.enqueue_site_job(policy, "site.storage_failover", target=target, resync=True,
	                               max_delete=int(storage.get_settings().replication_max_delete or 1000))
	_log(row.team, "site.storage_failover", row.name, {"target": target})
	return {"job": job}


@frappe.whitelist(methods=["POST"])
def refresh_site_storage_status(site, with_size=0):
	"""Queue site.storage_status (mounts, cache, pending uploads, bucket size)."""
	row = _site(site, "write")
	policy = storage.get_policy(row.name)
	if not policy:
		frappe.throw(_("Offsite attachments are not set up for {0}").format(row.name))
	return {"job": storage.enqueue_site_job(policy, "site.storage_status", with_size=bool(int(with_size or 0)))}


@frappe.whitelist(methods=["POST"])
def list_site_trash(site, path=None):
	"""Queue a trash listing; poll get_storage_job(site, job) for `result.entries`."""
	row = _site(site, "read")
	policy = storage.get_policy(row.name)
	if not policy or not policy.trash_profile:
		frappe.throw(_("The trash bin is not enabled for {0}").format(row.name))
	extra = {}
	if path:
		if not su.valid_trash_item(path):
			frappe.throw(_("Invalid trash path"))
		extra["item"] = path
	return {"job": storage.enqueue_site_job(policy, "site.storage_trash_list", **extra)}


@frappe.whitelist(methods=["POST"])
def restore_site_trash(site, item):
	"""Copy `<timestamp>/<public|private>/<path>` (or a whole folder) back into the live bucket."""
	row = _site(site, "write")
	_active_site(row)
	policy = storage.get_policy(row.name)
	if not policy or not policy.trash_profile or policy.attachments_mode != "Offloaded":
		frappe.throw(_("The trash bin is not enabled for {0}").format(row.name))
	if not su.valid_trash_item(item):
		frappe.throw(_("Invalid trash path"))
	job = storage.enqueue_site_job(policy, "site.storage_trash_restore", item=item.strip())
	_log(row.team, "site.storage_restore", row.name, {"item": item})
	return {"job": job}


@frappe.whitelist()
def get_storage_job(site, job):
	"""Status/result of a storage Agent Job of this site."""
	row = _site(site, "read")
	j = frappe.db.get_value("Agent Job", job, ["name", "kind", "status", "result", "error", "reference_doctype",
	                                           "reference_name"], as_dict=True)
	if not j or j.reference_doctype != storage.POLICY or j.reference_name != row.name:
		raise frappe.DoesNotExistError(_("Job {0} not found").format(job))
	result = frappe.parse_json(j.result) if j.result and j.status == "Success" else None
	return {"name": j.name, "kind": j.kind, "status": j.status, "error": j.error, "result": result}


# --------------------------------------------------------------------------- customer: team buckets (BYO)


def _team():
	from nexaploy.teams import get_current_team

	return get_current_team()


def _profile_values(values):
	values = frappe.parse_json(values) if isinstance(values, str) else (values or {})
	allowed = ("profile_name", "title", "provider", "region_label", "endpoint", "region", "addressing", "bucket",
	           "path_prefix", "access_key", "secret_key", "enabled", "description")
	return {k: values.get(k) for k in allowed if k in values}


def _save_profile(values, team):
	values = _profile_values(values)
	name = (values.get("profile_name") or "").strip().lower()
	if not name:
		frappe.throw(_("Profile name is required"))
	if frappe.db.exists(storage.PROFILE, name):
		doc = frappe.get_doc(storage.PROFILE, name)
		if (doc.team or None) != (team or None):
			raise frappe.PermissionError(_("Storage profile {0} belongs to someone else").format(name))
		values.pop("profile_name", None)
		if not values.get("secret_key"):
			values.pop("secret_key", None)
		doc.update(values)
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc({"doctype": storage.PROFILE, **values, "profile_name": name, "team": team})
		doc.insert(ignore_permissions=True)
	return storage.profile_view(frappe.db.get_value(storage.PROFILE, doc.name, storage.PROFILE_FIELDS, as_dict=True))


@frappe.whitelist()
def list_team_profiles():
	team = _team()
	_check(team, "read")
	rows = frappe.get_all(storage.PROFILE, filters={"team": team}, fields=storage.PROFILE_FIELDS, order_by="name")
	return {"profiles": [storage.profile_view(r) for r in rows], "byo": bool(storage.get_settings().allow_byo_profiles),
	        "presets": {k: v["label"] for k, v in su.PROVIDER_PRESETS.items()}}


@frappe.whitelist(methods=["POST"])
def save_team_profile(values):
	"""Add or update the current team's own bucket (needs Storage Settings.allow_byo_profiles)."""
	team = _team()
	_check(team, "admin")
	if not storage.get_settings().allow_byo_profiles:
		frappe.throw(_("Customer buckets are not enabled on this platform"))
	profile = _save_profile(values, team)
	from nexaploy.activity import log_activity

	log_activity(team, "storage.profile_save", storage.PROFILE, profile["name"])
	return profile


@frappe.whitelist(methods=["POST"])
def delete_team_profile(name):
	team = _team()
	_check(team, "admin")
	if frappe.db.get_value(storage.PROFILE, name, "team") != team:
		raise frappe.DoesNotExistError(_("Storage profile {0} not found").format(name))
	frappe.delete_doc(storage.PROFILE, name, ignore_permissions=True)
	from nexaploy.activity import log_activity

	log_activity(team, "storage.profile_delete", storage.PROFILE, name)
	return {"ok": True}


# --------------------------------------------------------------------------- operator (platform admin)


def _settings_view():
	s = storage.get_settings()
	profiles = frappe.get_all(storage.PROFILE, fields=storage.PROFILE_FIELDS, order_by="team asc, name asc")
	counts = {}
	for row in frappe.get_all(storage.POLICY, fields=["status", "last_sync_status"]):
		counts[row.status] = counts.get(row.status, 0) + 1
		if row.last_sync_status == "Failure":
			counts["replication_failures"] = counts.get("replication_failures", 0) + 1
	return {
		"settings": {k: s.get(k) for k in su.SETTINGS_FIELDS},
		"addons": _addons(s),
		"profiles": [storage.profile_view(r) for r in profiles],
		"presets": {k: v["label"] for k, v in su.PROVIDER_PRESETS.items()},
		"stats": counts,
	}


@frappe.whitelist()
def get_storage_settings():
	"""Platform settings for offsite attachments / DR replica / trash / backup profile."""
	_require_platform_admin()
	return _settings_view()


@frappe.whitelist(methods=["POST"])
def update_storage_settings(values=None, addon_prices=None):
	"""Update Storage Settings (`values`: subset of storage_utils.SETTINGS_FIELDS) and the
	add-on plans' prices (`addon_prices`: {offload|replica|trash: {price_monthly?, enabled?, currency?}})."""
	_require_platform_admin()
	values = frappe.parse_json(values) if isinstance(values, str) else (values or {})
	addon_prices = frappe.parse_json(addon_prices) if isinstance(addon_prices, str) else (addon_prices or {})
	try:
		clean = su.clean_settings_update(values)
	except ValueError as e:
		frappe.throw(_(str(e)))
	doc = frappe.get_single(storage.SETTINGS)
	doc.update(clean)
	doc.flags.ignore_permissions = True
	doc.save()
	for key, change in (addon_prices or {}).items():
		if key not in su.ADDON_FIELDS:
			frappe.throw(_("Unknown add-on {0}").format(key))
		plan = doc.get(su.ADDON_FIELDS[key])
		if not plan:
			frappe.throw(_("No plan is linked for add-on {0}").format(key))
		p = frappe.get_doc("Nexaploy Plan", plan)
		for field in ("price_monthly", "enabled", "currency"):
			if field in (change or {}):
				p.set(field, change[field])
		p.save(ignore_permissions=True)
	frappe.clear_document_cache(storage.SETTINGS, storage.SETTINGS)
	return _settings_view()


@frappe.whitelist(methods=["POST"])
def admin_save_storage_profile(values, team=None):
	"""Create/update any Storage Profile (platform profile when `team` is empty)."""
	_require_platform_admin()
	return _save_profile(values, team or None)


@frappe.whitelist(methods=["POST"])
def admin_delete_storage_profile(name):
	_require_platform_admin()
	frappe.delete_doc(storage.PROFILE, name, ignore_permissions=True)
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def admin_use_profile_for_backups(profile):
	"""Point the platform's database backups at a Storage Profile (Nexaploy Settings backup_*)."""
	_require_platform_admin()
	doc = frappe.get_single(storage.SETTINGS)
	doc.backup_storage_profile = profile
	doc.flags.ignore_permissions = True
	doc.save()
	return _settings_view()
