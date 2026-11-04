"""The control plane's own lifecycle (ARCHITECTURE.md §5.9): self-update through the agent of the
server hosting it (all-in-one) and the first-run setup checklist. Pure logic: controlplane_utils.

The update runs as ONE agent job, `controlplane.update` (backup -> pull -> switch image -> migrate
-> health check -> automatic rollback), because this process restarts in the middle of it; the
agent reports the outcome once the (new or rolled back) control plane answers again.
"""

import frappe
from frappe import _

import nexaploy
from nexaploy import controlplane_utils as cu

SETTINGS = "Nexaploy Settings"
CACHE_KEY = "nexaploy:control_plane_latest_release"


def _settings():
	doc = frappe.get_single(SETTINGS)
	return {f: doc.get(f) for f in (
		"control_plane_release_channel", "control_plane_image_repo", "control_plane_release_repo",
		"setup_checklist_dismissed", "root_domain",
	)}


def host():
	"""The Server hosting this control plane (all-in-one), with its bench report."""
	return frappe.db.get_value(
		"Server",
		{"hosts_control_plane": 1, "status": ["!=", "Archived"]},
		["name", "hostname", "status", "control_plane_bench", "control_plane_site", "benches_report",
		 "agent_version", "last_heartbeat"],
		as_dict=True,
	)


# --------------------------------------------------------------------------- releases
def latest_release(force=False):
	"""Newest GitHub release for the configured channel, cached 1 h (errors cached 5 min)."""
	rs = cu.release_settings(_settings())
	key = f"{CACHE_KEY}:{rs['release_repo']}:{rs['channel']}"
	cache = frappe.cache()
	if not force:
		cached = cache.get_value(key)
		if cached is not None:
			return cached
	import requests

	try:
		resp = requests.get(cu.releases_api_url(rs["release_repo"]), timeout=10,
		                    headers={"Accept": "application/vnd.github+json"})
		resp.raise_for_status()
		value = {"release": cu.pick_release(resp.json(), rs["channel"]), "error": None,
		         "checked_at": str(frappe.utils.now_datetime())}
		ttl = cu.RELEASE_CACHE_SECONDS
	except Exception:
		# full detail in the Error Log; the card only says the lookup failed
		frappe.log_error(title="Nexaploy: GitHub release lookup failed")
		value = {"release": None, "error": "Could not check GitHub for new releases (see the Error Log).",
		         "checked_at": str(frappe.utils.now_datetime())}
		ttl = 300
	cache.set_value(key, value, expires_in_sec=ttl)
	return value


# --------------------------------------------------------------------------- update
def last_update_job(server):
	name = frappe.db.get_value("Agent Job", {"server": server, "kind": "controlplane.update"}, "name",
	                           order_by="creation desc")
	return job_progress(name) if name else None


def job_progress(name):
	job = frappe.get_doc("Agent Job", name)
	return cu.job_progress({
		"name": job.name, "status": job.status, "error": job.error, "creation": str(job.creation),
		"result": frappe.parse_json(job.result) if job.result else {},
		"params": frappe.parse_json(job.params) if job.params else {},
		"steps": [{"step_idx": s.step_idx, "step_name": s.step_name, "status": s.status} for s in job.steps],
	})


def status(force=False):
	h = host()
	mode = cu.update_mode(h)
	rs = cu.release_settings(_settings())
	latest = latest_release(force=force)
	current_image = cu.current_image(h.benches_report, h.control_plane_bench) if h else None
	rel = latest.get("release")
	return {
		**mode,
		"version": nexaploy.__version__,
		"image": current_image,
		"site": h.control_plane_site if h else None,
		"server": h.name if h else None,
		"channel": rs["channel"],
		"image_repo": rs["image_repo"],
		"latest": rel,
		"latest_error": latest.get("error"),
		"checked_at": latest.get("checked_at"),
		"latest_image": cu.image_for_version(rel["version"], rs["image_repo"]) if rel else None,
		"update_available": bool(rel) and cu.is_newer(rel["version"], nexaploy.__version__),
		"job": last_update_job(h.name) if h else None,
	}


def start_update(image=None, version=None):
	from nexaploy.agent_job import create_agent_job

	h = host()
	mode = cu.update_mode(h)
	if not mode["can_update"]:
		frappe.throw(_(mode["reason"]))
	running = frappe.db.exists("Agent Job", {"server": h.name, "kind": "controlplane.update",
	                                         "status": ["in", ["Pending", "Queued", "Running"]]})
	if running:
		frappe.throw(_("A control plane update is already in progress ({0})").format(running))
	rs = cu.release_settings(_settings())
	latest = latest_release().get("release") if not (image or version) else None
	try:
		target = cu.resolve_target(image, version, latest, rs["image_repo"])
	except cu.ControlPlaneError as exc:
		frappe.throw(_(exc.message).format(*exc.params))
	previous = cu.current_image(h.benches_report, h.control_plane_bench)
	job = create_agent_job(h.name, "controlplane.update", cu.update_params(h, target, previous),
	                       reference_doctype="Server", reference_name=h.name)
	from nexaploy.activity import log_platform_activity

	log_platform_activity("platform.control_plane_update", "Agent Job", job.name,
	                      {"image": target, "previous_image": previous})
	return job_progress(job.name)


def on_update_job(job):
	"""Agent callback: record the outcome (the image itself shows up in the next heartbeat)."""
	if job.status not in cu.TERMINAL:
		return
	from nexaploy.activity import log_platform_activity

	result = frappe.parse_json(job.result) if job.result else {}
	log_platform_activity(
		"platform.control_plane_updated" if job.status == "Success" else "platform.control_plane_update_failed",
		"Agent Job", job.name,
		{"image": result.get("image"), "previous_image": result.get("previous_image"),
		 "rolled_back": result.get("rolled_back"), "error": (job.error or "")[:500]},
	)


# --------------------------------------------------------------------------- setup checklist
def _password_set(doctype, field):
	try:
		return bool(frappe.get_single(doctype).get_password(field, raise_exception=False))
	except Exception:
		return False


def setup_facts():
	s = frappe.get_single(SETTINGS)
	b = frappe.get_single("Billing Settings")
	brand = frappe.db.get_value("Brand", {"is_default": 1, "enabled": 1}, "title")
	app_name = frappe.db.get_single_value("Website Settings", "app_name")
	return {
		"platform_name": brand or (app_name if app_name and app_name != "Frappe" else None),
		"root_domain": s.get("root_domain"),
		"billing_enabled": b.get("enabled"),
		"currency": b.get("currency"),
		"stripe_ready": b.get("enable_stripe") and _password_set("Billing Settings", "stripe_secret_key"),
		"paymob_ready": b.get("enable_paymob") and _password_set("Billing Settings", "paymob_api_key"),
		"backup_s3": s.get("backup_bucket") and s.get("backup_access_key"),
		"backup_oauth": s.get("backup_remote_type") in ("drive", "onedrive", "dropbox") and s.get("backup_config_hash"),
		"storage_profiles": frappe.db.count("Storage Profile", {"enabled": 1}),
		"relay_enabled": frappe.db.get_single_value("Email Relay Settings", "enabled"),
		"outgoing_account": frappe.db.exists("Email Account", {"default_outgoing": 1, "enable_outgoing": 1}),
		"plans": frappe.db.count("Nexaploy Plan", {"enabled": 1}),
		"external_probes_seen": frappe.db.count("Uptime Probe", {"enabled": 1, "is_builtin": 0,
		                                                        "last_seen_at": ["is", "set"]}),
		"active_sites": frappe.db.count("Site", {"status": "Active"}),
		"dismissed": s.get("setup_checklist_dismissed"),
	}


def setup_checklist():
	return cu.setup_checklist(setup_facts())


def dismiss_setup(dismissed=1):
	frappe.db.set_single_value(SETTINGS, "setup_checklist_dismissed", 1 if dismissed else 0)
	return setup_checklist()
