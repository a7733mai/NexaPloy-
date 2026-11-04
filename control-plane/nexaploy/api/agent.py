"""Agent-facing API (ARCHITECTURE.md §5.6).

All endpoints are `allow_guest=True` and authenticate the agent themselves:

    Authorization: token <server_token>          (heartbeat, poll, job_update)
    body.registration_token                      (register)

Server tokens are stored only as sha256 (Server.token_hash) and compared with
hmac.compare_digest. The server is looked up by `server_id` from the query string / body.
"""

import json

import frappe
from frappe import _
from frappe.utils import now_datetime

from nexaploy.agent_callbacks import dispatch_callback
from nexaploy.utils import (
	TERMINAL_JOB_STATUSES,
	generate_server_id,
	generate_token,
	hash_token,
	map_agent_status,
	merge_job_status,
	normalize_step_status,
	parse_agent_datetime,
	parse_authorization_header,
	resolve_secrets,
	token_expired,
	verify_token,
)

AGENT_METHODS = ("register", "heartbeat", "poll", "job_update")
POLL_LIMIT = 20


# --------------------------------------------------------------------------- auth helpers


def _request_token():
	return parse_authorization_header(frappe.get_request_header("Authorization"))


def _load_json(value, default=None):
	if value in (None, ""):
		return default
	if isinstance(value, (dict, list)):
		return value
	try:
		return json.loads(value)
	except (TypeError, ValueError):
		return default


def _verify_server_token(server_id, token):
	"""Return the Server name if `token` matches its stored hash, else None."""
	if not server_id or not token:
		return None
	row = frappe.db.get_value(
		"Server", {"server_id": server_id}, ["name", "token_hash", "status"], as_dict=True
	)
	if not row or row.status == "Archived":
		# still burn a comparison so timing does not reveal unknown server ids
		verify_token(token, "0" * 64)
		return None
	return row.name if verify_token(token, row.token_hash) else None


def _authenticate_server(server_id):
	"""Authenticate the calling agent or raise frappe.AuthenticationError."""
	server = _verify_server_token(server_id, _request_token())
	if not server:
		frappe.local.response["http_status_code"] = 401
		raise frappe.AuthenticationError(_("Invalid server_id or server token"))
	return server


def _agent_method_from_path(path):
	"""'nexaploy.api.agent.poll' style method name from /api[/v1|/v2]/method/<m>."""
	if not path:
		return None
	marker = "/method/"
	if marker not in path:
		return None
	method = path.split(marker, 1)[1].strip("/")
	prefix = "nexaploy.api.agent."
	if method.startswith(prefix) and method[len(prefix) :] in AGENT_METHODS:
		return method[len(prefix) :]
	return None


def validate_agent_auth():
	"""`auth_hooks` entry (see hooks.py).

	Frappe v15's validate_auth() raises AuthenticationError for an `Authorization: token X`
	header that didn't log a user in. For our agent endpoints we verify the server token here
	and run the request as Administrator; the endpoint re-verifies it. Every other request is
	left untouched.
	"""
	request = getattr(frappe.local, "request", None)
	method = _agent_method_from_path(getattr(request, "path", None))
	if not method:
		return
	token = _request_token()
	if not token:
		return
	form = frappe.local.form_dict or {}
	if method == "register":
		ok = _registration_token_row(form.get("registration_token") or token) is not None
	else:
		ok = _verify_server_token(form.get("server_id"), token) is not None
	if ok:
		# frappe.set_user() resets frappe.local.form_dict: keep the request arguments,
		# or the endpoint receives server_id=None (found on a real v15 bench).
		form_dict = frappe.local.form_dict
		frappe.set_user("Administrator")
		frappe.local.form_dict = form_dict


# --------------------------------------------------------------------------- register


def _registration_token_row(token):
	if not token:
		return None
	row = frappe.db.get_value(
		"Server Registration Token",
		{"token_hash": hash_token(token)},
		["name", "team", "expires_on", "used"],
		as_dict=True,
	)
	if not row or row.used or token_expired(row.expires_on, now_datetime()):
		return None
	return row


@frappe.whitelist(allow_guest=True, methods=["POST"])
def register(registration_token=None, hostname=None, facts=None, agent_version=None, **kwargs):
	"""BYOS registration. Consumes a single-use registration token and returns
	`{server_id, server_token}`; the plaintext server token is shown exactly once.

	A token bound to a pre-created Server (Server Registration Token.server set before use,
	Hosted servers provisioned by nexaploy.provisioning) activates that Server instead of
	creating a new BYOS one.
	"""
	token = registration_token or _request_token()
	if not token:
		raise frappe.AuthenticationError(_("registration_token is required"))

	row = frappe.db.get_value(
		"Server Registration Token",
		{"token_hash": hash_token(token)},
		["name", "team", "expires_on", "used", "server"],
		as_dict=True,
		for_update=True,
	)
	if not row or row.used or token_expired(row.expires_on, now_datetime()):
		frappe.local.response["http_status_code"] = 401
		raise frappe.AuthenticationError(_("Invalid, used or expired registration token"))

	if row.server:
		# Hosted server pre-created by nexaploy.provisioning: activate it instead of a new Server
		return _attach_registration(row, hostname, facts, agent_version)

	# the token's team is a Team (§9); a legacy user-valued token maps to that user's team
	team = row.team
	if team and not frappe.db.exists("Team", team):
		from nexaploy.teams import personal_team_for

		team = personal_team_for(team)

	server_token = generate_token()
	server_id = generate_server_id()
	while frappe.db.exists("Server", {"server_id": server_id}):
		server_id = generate_server_id()

	byos_plan = frappe.db.get_value(
		"Nexaploy Plan", {"plan_type": "Server BYOS", "enabled": 1}, "name", order_by="price_monthly asc"
	)
	server = frappe.get_doc(
		{
			"doctype": "Server",
			"server_id": server_id,
			"hostname": (hostname or "")[:140],
			"ownership": "BYOS",
			"team": team,
			"status": "Active",
			"plan": byos_plan,
			"token_hash": hash_token(server_token),
			"agent_version": agent_version,
			"facts": frappe.as_json(_load_json(facts, {})),
			"last_heartbeat": now_datetime(),
		}
	)
	server.insert(ignore_permissions=True)

	frappe.db.set_value(
		"Server Registration Token", row.name, {"used": 1, "server": server.name}, update_modified=True
	)
	# Server is Active: push the backup storage config (polled once the worker starts).
	from nexaploy.ops import on_server_activated

	on_server_activated(server.name)
	return {"server_id": server_id, "server_token": server_token}


def _attach_registration(row, hostname, facts, agent_version):
	"""Registration token bound to a pre-created (provisioned) Server: give that Server its
	token and activate it (provisioning_status -> Ready)."""
	from nexaploy.provisioning.utils import attach_values, registration_action

	server = frappe.db.get_value(
		"Server",
		row.server,
		["name", "server_id", "status", "token_hash", "provider", "provisioning_status"],
		as_dict=True,
		for_update=True,
	)
	try:
		registration_action(row.server, server)
	except ValueError as exc:
		frappe.local.response["http_status_code"] = 401
		raise frappe.AuthenticationError(_(str(exc)))

	server_token = generate_token()
	values = attach_values(
		server,
		hostname,
		agent_version,
		frappe.as_json(_load_json(facts, {})),
		hash_token(server_token),
		now_datetime(),
	)
	frappe.db.set_value("Server", server.name, values)
	frappe.db.set_value("Server Registration Token", row.name, {"used": 1}, update_modified=True)

	from nexaploy.ops import on_server_activated, sync_billing

	sync_billing(frappe.get_doc("Server", server.name))
	on_server_activated(server.name)
	return {"server_id": server.server_id, "server_token": server_token}


# --------------------------------------------------------------------------- heartbeat


@frappe.whitelist(allow_guest=True, methods=["POST"])
def heartbeat(server_id=None, facts=None, benches=None, agent_version=None, **kwargs):
	server = _authenticate_server(server_id)
	values = {"last_heartbeat": now_datetime()}
	if facts is not None:
		values["facts"] = frappe.as_json(_load_json(facts, {}))
	if benches is not None:
		values["benches_report"] = frappe.as_json(_load_json(benches, []))
	if agent_version:
		values["agent_version"] = agent_version
	request_ip = getattr(frappe.local, "request_ip", None)
	if request_ip:
		values["agent_ip"] = request_ip  # fallback DNS target for custom domains
	previous_status, provisioning_status = frappe.db.get_value(
		"Server", server, ["status", "provisioning_status"]
	)
	if previous_status in ("Offline", "Pending"):
		values["status"] = "Active"
	frappe.db.set_value("Server", server, values, update_modified=False)
	if provisioning_status in ("Creating", "Bootstrapping", "Failed"):
		# a provisioned server whose agent runs (e.g. registered after the bootstrap timeout)
		from nexaploy.provisioning.jobs import mark_ready

		mark_ready(server)

	from nexaploy import ops

	if previous_status == "Pending":
		# first heartbeat of a Hosted server (created manually, installed with --server-id)
		ops.on_server_activated(server)
	try:
		ops.record_heartbeat_metrics(server, _load_json(facts, None))
	except Exception:
		frappe.log_error(title=f"Nexaploy: heartbeat metrics failed for {server}")
	ops.ingest_usage(server, kwargs.get("usage"))  # never raises
	if kwargs.get("tls"):
		from nexaploy.domains import ingest_tls

		ingest_tls(server, kwargs.get("tls"))  # never raises
	pending = frappe.db.count("Agent Job", {"server": server, "status": "Pending"})
	return {"ok": True, "pending_jobs": pending}


# --------------------------------------------------------------------------- poll


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
def poll(server_id=None, **kwargs):
	"""Hand Pending jobs to the agent (-> Queued). Returns [{job_id, kind, params}]."""
	server = _authenticate_server(server_id)
	# frappe.get_all() has no for_update in v15: lock the rows with the query builder so two
	# concurrent polls never hand out the same job.
	AgentJob = frappe.qb.DocType("Agent Job")
	jobs = (
		frappe.qb.from_(AgentJob)
		.select(AgentJob.name, AgentJob.kind, AgentJob.params)
		.where((AgentJob.server == server) & (AgentJob.status == "Pending"))
		.orderby(AgentJob.creation)
		.limit(POLL_LIMIT)
		.for_update()
		.run(as_dict=True)
	)
	now = now_datetime()
	out = []
	for job in jobs:
		frappe.db.set_value("Agent Job", job.name, {"status": "Queued", "queued_at": now})
		params = resolve_secrets(_load_json(job.params, {}), _resolve_secret)
		out.append({"job_id": job.name, "kind": job.kind, "params": params})
	if jobs:
		# §5.6 polls with GET, and Frappe only auto-commits unsafe methods.
		frappe.db.commit()
	return out


def _resolve_secret(doctype, name, fieldname):
	if doctype == "github":
		# __secret__:github:<deploy candidate>:apps_json — fresh GitHub App tokens
		from nexaploy.github_secrets import resolve

		return resolve(name, fieldname)
	from frappe.utils.password import get_decrypted_password

	return get_decrypted_password(doctype, name, fieldname, raise_exception=False)


# --------------------------------------------------------------------------- job_update


def _to_system_datetime(value):
	dt = parse_agent_datetime(value)
	if dt is None:
		return None
	try:
		from frappe.utils.data import convert_utc_to_system_timezone

		return convert_utc_to_system_timezone(dt).replace(tzinfo=None)
	except Exception:
		return dt


@frappe.whitelist(allow_guest=True, methods=["POST"])
def job_update(server_id=None, job_id=None, status=None, steps=None, error=None, result=None, **kwargs):
	"""Agent reports job progress. Maps statuses, upserts steps, stores error/result and runs
	the callback for the job kind once the job reaches a terminal status.
	"""
	server = _authenticate_server(server_id)
	if not job_id or not frappe.db.exists("Agent Job", job_id):
		frappe.throw(_("Unknown job {0}").format(job_id), frappe.DoesNotExistError)

	job = frappe.get_doc("Agent Job", job_id, for_update=True)
	if job.server != server:
		raise frappe.PermissionError(_("Job {0} does not belong to this server").format(job_id))

	previous = job.status
	if status:
		try:
			incoming = map_agent_status(status)
		except ValueError as e:
			frappe.throw(str(e))
		job.status = merge_job_status(job.status, incoming)
		if (status or "").lower() in ("cancelled", "canceled") and not error:
			error = "cancelled by agent"

	now = now_datetime()
	if job.status == "Running" and not job.started_at:
		job.started_at = now
	if job.status in TERMINAL_JOB_STATUSES and not job.ended_at:
		job.ended_at = now
		job.started_at = job.started_at or now

	_upsert_steps(job, _load_json(steps, []) or [])

	if error:
		job.error = str(error)
	parsed_result = _load_json(result, result)
	if parsed_result not in (None, ""):
		job.result = frappe.as_json(parsed_result)

	job.flags.ignore_permissions = True
	job.save()

	if job.status != previous:
		dispatch_callback(job)
	return {"ok": True, "status": job.status}


def _upsert_steps(job, steps):
	by_idx = {int(s.step_idx or 0): s for s in job.steps}
	for step in steps:
		if not isinstance(step, dict):
			continue
		try:
			idx = int(step.get("idx"))
		except (TypeError, ValueError):
			continue
		row = by_idx.get(idx)
		if row is None:
			row = job.append("steps", {"step_idx": idx})
			by_idx[idx] = row
		if step.get("name"):
			row.step_name = step.get("name")
		row.status = normalize_step_status(step.get("status"))
		if step.get("output") is not None:
			# The agent sends the full (appended) output each time; keep the latest copy.
			row.output = step.get("output")
		row.started_at = _to_system_datetime(step.get("started_at")) or row.started_at
		row.ended_at = _to_system_datetime(step.get("ended_at")) or row.ended_at
	job.steps.sort(key=lambda r: int(r.step_idx or 0))
	for i, row in enumerate(job.steps, start=1):
		row.idx = i
