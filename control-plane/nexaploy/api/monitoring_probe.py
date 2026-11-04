"""API of external uptime probe workers (`nexaploy-probe`, nexaploy/probe.py; ARCHITECTURE.md §16).

Probes authenticate with the header ``X-Nexaploy-Probe-Token: <token>`` (not `Authorization`,
which Frappe would try to log in with). Tokens are stored as sha256 only (Uptime Probe.token_hash).

    GET  checks()               -> {probe, region, poll_seconds, checks: [{monitor, url, method,
                                    expected_status, keyword, timeout, interval}]}
    POST results(results)       -> {accepted}
    GET  download_script()      -> the probe worker itself (public source code, no secrets)
    rotate_token(probe)         -> {probe, token}   (System Manager only; shown once)

A probe only receives - and may only report on - enabled monitors of Active sites assigned to it
(Uptime Monitor.probes empty = every enabled probe).
"""

import hashlib
import hmac
import os
import secrets

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import now_datetime

from nexaploy import monitor_utils as mu

PROBE = "Uptime Probe"
POLL_SECONDS = 60
MAX_RESULTS = 2000


def _hash(token):
	return hashlib.sha256(str(token).encode()).hexdigest()


def set_new_token(probe_name):
	"""Generate, store (sha256) and return a new token for an external probe."""
	token = "nxp_" + secrets.token_urlsafe(32)
	frappe.db.set_value(PROBE, probe_name, {"token_hash": _hash(token), "token_hint": "…" + token[-4:]})
	return token


def _authenticate():
	"""The enabled external Uptime Probe of the request's token, else PermissionError."""
	request = getattr(frappe.local, "request", None)
	token = request.headers.get(mu.PROBE_TOKEN_HEADER) if request is not None else None
	if not token or len(token) > 200:
		raise frappe.PermissionError(_("Invalid probe token"))
	digest = _hash(token)
	row = frappe.db.get_value(PROBE, {"token_hash": digest}, ["name", "enabled", "is_builtin", "token_hash", "region"], as_dict=True)
	if not row or not row.token_hash or not hmac.compare_digest(row.token_hash, digest) or row.is_builtin:
		raise frappe.PermissionError(_("Invalid probe token"))
	if not row.enabled:
		raise frappe.PermissionError(_("This probe is disabled"))
	values = {"last_seen_at": now_datetime()}
	ip = getattr(frappe.local, "request_ip", None)
	if ip:
		values["last_ip"] = str(ip)[:64]
	agent = request.headers.get("User-Agent") or ""
	if agent.startswith("Nexaploy-Uptime-Probe/"):
		values["last_version"] = agent.split("/", 1)[1].split(" ", 1)[0][:20]
	frappe.db.set_value(PROBE, row.name, values, update_modified=False)
	return row


@frappe.whitelist(allow_guest=True, methods=["GET"])
@rate_limit(limit=120, seconds=60)
def checks():
	from nexaploy.monitoring import check_payload, monitors_for_probe

	probe = _authenticate()
	payload = [check_payload(m) for m in monitors_for_probe(probe.name)]
	frappe.db.commit()
	return {"probe": probe.name, "region": probe.region, "poll_seconds": POLL_SECONDS, "checks": payload}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=120, seconds=60)
def results(results=None):
	from nexaploy.monitoring import ingest

	probe = _authenticate()
	if isinstance(results, str):
		results = frappe.parse_json(results)
	if not isinstance(results, list):
		frappe.throw(_("results must be a list"))
	accepted = ingest(probe.name, results[:MAX_RESULTS])
	frappe.db.commit()
	return {"accepted": accepted}


@frappe.whitelist(allow_guest=True, methods=["GET"])
@rate_limit(limit=30, seconds=60)
def download_script():
	"""The standalone probe worker (nexaploy/probe.py) as `nexaploy-probe`."""
	path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "probe.py")
	with open(path, "rb") as fh:
		content = fh.read()
	frappe.response.filename = "nexaploy-probe"
	frappe.response.filecontent = content
	frappe.response.type = "download"
	frappe.response.display_content_as = "attachment"


@frappe.whitelist(methods=["POST"])
def rotate_token(probe):
	"""System Manager: issue a new token for an external probe (the old one stops working)."""
	frappe.only_for("System Manager")
	row = frappe.db.get_value(PROBE, probe, ["name", "is_builtin"], as_dict=True)
	if not row:
		raise frappe.DoesNotExistError(_("Uptime probe {0} not found").format(probe))
	if row.is_builtin:
		frappe.throw(_("The built-in probe has no token"))
	token = set_new_token(row.name)
	frappe.db.commit()
	return {"probe": row.name, "token": token}
