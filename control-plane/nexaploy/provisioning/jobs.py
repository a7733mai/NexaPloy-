"""Background provisioning of Hosted servers (frappe glue; pure rules in utils.py).

    create  : api/provisioning.create_hosted_server -> Server(Creating) -> enqueue provision_server
              -> provider.create_server(cloud-init = install.sh with a registration token
                 bound to the Server) -> Bootstrapping
    ready   : the agent registers (api/agent.register attaches to the Server) -> Ready
    timeout : check_provisioning (every 5 min): Creating/Bootstrapping > 20 min -> Failed + alert
    delete  : api/provisioning.delete_hosted_server -> Deleting -> enqueue deprovision_server
              -> provider.delete_server -> Deleted, Server Archived (billing stops)

Provider calls never run in a web request: they go through frappe.enqueue (queue "long").
"""

import frappe
from frappe.utils import add_to_date, get_url, now_datetime

from nexaploy.provisioning import utils as pu
from nexaploy.provisioning.base import ProviderError, get_provider, parse_ssh_keys
from nexaploy.provisioning.cloud_init import build_cloud_init
from nexaploy.utils import generate_token, hash_token

QUEUE = "long"


# --------------------------------------------------------------------------- helpers


def provider_client(provider_name, transport=None):
	row = frappe.db.get_value("Cloud Provider", provider_name, ["name", "provider", "enabled"], as_dict=True)
	if not row:
		raise ProviderError(f"Cloud Provider {provider_name} not found")
	from frappe.utils.password import get_decrypted_password

	token = get_decrypted_password("Cloud Provider", provider_name, "api_token", raise_exception=False)
	return get_provider(row.provider, token, transport)


def _set(server, values):
	frappe.db.set_value("Server", server, values, update_modified=True)


def _sync_billing(server):
	from nexaploy.ops import sync_billing

	sync_billing(frappe.get_doc("Server", server))


def _alert(server, subject, message):
	try:
		from nexaploy.ops import send_alert

		team = frappe.db.get_value("Server", server, "team")
		send_alert(team, subject, message)
	except Exception:
		frappe.log_error(title=f"Nexaploy: provisioning alert failed for {server}")


def _fail(server, error):
	_set(server, {"provisioning_status": pu.FAILED, "provisioning_error": str(error)[:2000]})
	_sync_billing(server)
	_alert(
		server,
		f"Server {server} could not be provisioned",
		f"Provisioning of server {server} failed:\n\n{error}\n\n"
		"Nothing is billed for a failed server. Delete it from the dashboard and try again, "
		"or contact support.",
	)


def _acme_email(team):
	email = frappe.db.get_single_value("Nexaploy Settings", "alert_email")
	if not email and team:
		email = frappe.db.get_value("Team", team, "billing_email")
	return email or None


def _control_plane_url():
	return (frappe.conf.get("nexaploy_control_plane_url") or get_url()).rstrip("/")


def create_registration_token(team, server):
	"""Single-use registration token bound to `server` (agent.register attaches to it).
	Returns the plaintext token; only its sha256 is stored."""
	token = generate_token()
	frappe.get_doc(
		{
			"doctype": "Server Registration Token",
			"token_hash": hash_token(token),
			"token_prefix": token[:6],
			"team": team,
			"expires_on": add_to_date(now_datetime(), hours=pu.REGISTRATION_TOKEN_TTL_HOURS),
			"used": 0,
			"server": server,
		}
	).insert(ignore_permissions=True)
	return token


# --------------------------------------------------------------------------- create


def enqueue_provision(server):
	frappe.enqueue(
		"nexaploy.provisioning.jobs.provision_server",
		queue=QUEUE,
		timeout=600,
		enqueue_after_commit=True,
		job_id=f"nexaploy-provision-{server}",
		deduplicate=True,
		server=server,
	)


def provision_server(server):
	"""Background job: create the VM at the provider; Creating -> Bootstrapping (or Failed)."""
	doc = frappe.get_doc("Server", server)
	if doc.provisioning_status != pu.CREATING:
		return
	try:
		provider = frappe.get_doc("Cloud Provider", doc.provider)
		if not provider.enabled:
			raise ProviderError(f"Cloud Provider {doc.provider} is disabled")
		region = frappe.db.get_value("Cloud Region", doc.region, "code")
		size = frappe.db.get_value("Cloud Server Size", doc.size, "code")
		token = create_registration_token(doc.team, doc.name)
		user_data = build_cloud_init(
			_control_plane_url(), token, acme_email=_acme_email(doc.team), hostname=doc.hostname
		)
		client = provider_client(doc.provider)
		result = client.create_server(
			name=doc.hostname or doc.server_id,
			region=region,
			size=size,
			image=provider.default_image,
			user_data=user_data,
			ssh_keys=parse_ssh_keys(provider.ssh_key_ids),
			labels=pu.provider_labels(doc.server_id, doc.team),
		)
	except Exception as exc:
		frappe.db.rollback()
		frappe.log_error(title=f"Nexaploy: provisioning {server} failed")
		_fail(server, exc)
		frappe.db.commit()
		return
	_set(
		server,
		{
			"provisioning_status": pu.BOOTSTRAPPING,
			"provider_server_id": result.get("id"),
			"public_ipv4": result.get("ipv4"),
			"public_ipv6": result.get("ipv6"),
			"provisioning_error": None,
		},
	)
	_sync_billing(server)  # billed from Bootstrapping on (the VM exists)
	frappe.db.commit()


# --------------------------------------------------------------------------- delete


def enqueue_deprovision(server):
	frappe.enqueue(
		"nexaploy.provisioning.jobs.deprovision_server",
		queue=QUEUE,
		timeout=600,
		enqueue_after_commit=True,
		job_id=f"nexaploy-deprovision-{server}",
		deduplicate=True,
		server=server,
	)


def deprovision_server(server):
	"""Background job: delete the VM; Deleting -> Deleted + Server Archived."""
	doc = frappe.get_doc("Server", server)
	if doc.provisioning_status != pu.DELETING:
		return
	try:
		if doc.provider_server_id:
			provider_client(doc.provider).delete_server(doc.provider_server_id)
	except Exception as exc:
		frappe.db.rollback()
		frappe.log_error(title=f"Nexaploy: deleting server {server} at the provider failed")
		_set(server, {"provisioning_error": f"delete failed: {exc}"[:2000]})
		_alert(server, f"Server {server} could not be deleted", f"Deleting server {server} failed:\n\n{exc}")
		frappe.db.commit()
		return
	_set(server, {"provisioning_status": pu.DELETED, "status": "Archived", "token_hash": None})
	frappe.db.set_value(
		"Server Registration Token", {"server": server, "used": 0}, "used", 1, update_modified=False
	)
	_sync_billing(server)
	frappe.db.commit()


# --------------------------------------------------------------------------- scheduler


def check_provisioning():
	"""Every 5 min: servers stuck in Creating/Bootstrapping for more than 20 minutes -> Failed
	+ alert; fill in IPs the provider assigned after create (DigitalOcean)."""
	now = now_datetime()
	rows = frappe.get_all(
		"Server",
		filters={"provisioning_status": ["in", list(pu.IN_PROGRESS)]},
		fields=["name", "provider", "provider_server_id", "provisioning_status", "provisioning_started_at",
		        "public_ipv4", "creation"],
	)
	for row in rows:
		try:
			started = row.provisioning_started_at or row.creation
			if pu.bootstrap_timed_out(started, now):
				_fail(
					row.name,
					f"The server did not register within {pu.BOOTSTRAP_TIMEOUT_MINUTES} minutes "
					f"(status {row.provisioning_status}). See /var/log/nexaploy-install.log on the server.",
				)
			elif row.provisioning_status == pu.BOOTSTRAPPING and row.provider_server_id and not row.public_ipv4:
				info = provider_client(row.provider).get_server(row.provider_server_id) or {}
				if info.get("ipv4") or info.get("ipv6"):
					_set(row.name, {"public_ipv4": info.get("ipv4"), "public_ipv6": info.get("ipv6")})
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Nexaploy: check_provisioning failed for {row.name}")


def mark_ready(server):
	"""The agent registered / heartbeated: Creating/Bootstrapping/Failed -> Ready."""
	status = frappe.db.get_value("Server", server, "provisioning_status")
	if pu.ready_on_heartbeat(status):
		_set(server, {"provisioning_status": pu.READY, "provisioning_error": None})
		_sync_billing(server)
