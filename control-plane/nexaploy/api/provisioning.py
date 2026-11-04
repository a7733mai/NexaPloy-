"""Portal API: Hosted servers created on a cloud provider (Hetzner Cloud, DigitalOcean).

    list_options()                              regions + sizes (with the plan price)
    create_hosted_server(region, size, title)   team Owner/Admin with billing rights
    provisioning_status(server)                 poll target for the portal
    delete_hosted_server(server)                team Owner/Admin

Provider calls run in background jobs (nexaploy.provisioning.jobs); see that module for the
state machine. The server is billed on its size's Server Hosted plan (Server.plan, via
billing/subscriptions.sync_server) from Bootstrapping until Deleted.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime

from nexaploy.activity import log_activity
from nexaploy.api.dashboard import _check_team, _ctx
from nexaploy.provisioning import utils as pu
from nexaploy.teams import check_team_access
from nexaploy.utils import generate_server_id


def _options():
	providers = frappe.get_all(
		"Cloud Provider", filters={"enabled": 1}, fields=["name", "provider", "title"], order_by="title asc"
	)
	names = [p.name for p in providers]
	if not names:
		return []
	regions = frappe.get_all(
		"Cloud Region",
		filters={"enabled": 1, "provider": ["in", names]},
		fields=["name", "provider", "code", "title"],
	)
	sizes = frappe.get_all(
		"Cloud Server Size",
		filters={"enabled": 1, "provider": ["in", names], "plan": ["is", "set"]},
		fields=["name", "provider", "code", "vcpu", "memory_gb", "disk_gb", "plan"],
	)
	plans = {}
	plan_names = list({s.plan for s in sizes if s.plan})
	if plan_names:
		for p in frappe.get_all(
			"Nexaploy Plan",
			filters={"name": ["in", plan_names]},
			fields=["name", "title", "price_monthly", "currency", "plan_type", "enabled"],
		):
			plans[p.name] = p
	return pu.build_options(providers, regions, sizes, plans)


@frappe.whitelist()
def list_options():
	"""{providers: [{name, provider, title, regions: [...], sizes: [{..., price_monthly, currency}]}],
	can_create}"""
	c = _ctx()
	from nexaploy.team_utils import role_allows

	can_create = role_allows(c.role, "admin") and role_allows(c.role, "billing")
	return {"providers": _options(), "can_create": bool(c.admin or can_create)}


@frappe.whitelist(methods=["POST"])
def create_hosted_server(region, size, title=None):
	"""Create a Hosted server for the current team. Returns {name, server_id, provisioning_status}.

	Needs the `admin` and `billing` actions (Owner/Admin) in the team."""
	c = _ctx("admin")
	check_team_access(c.team, "billing", c.user)

	region_row = frappe.db.get_value("Cloud Region", region, ["name", "provider", "enabled"], as_dict=True)
	size_row = frappe.db.get_value("Cloud Server Size", size, ["name", "provider", "enabled", "plan"], as_dict=True)
	if not region_row or not region_row.enabled:
		frappe.throw(_("Region {0} is not available").format(region))
	if not size_row or not size_row.enabled or not size_row.plan:
		frappe.throw(_("Server size {0} is not available").format(size))
	if region_row.provider != size_row.provider:
		frappe.throw(_("Server size {0} is not offered in region {1}").format(size, region))
	if not frappe.db.get_value("Cloud Provider", region_row.provider, "enabled"):
		frappe.throw(_("Region {0} is not available").format(region))
	plan = frappe.db.get_value("Nexaploy Plan", size_row.plan, ["plan_type", "enabled"], as_dict=True)
	if not plan or not plan.enabled or plan.plan_type != "Server Hosted":
		frappe.throw(_("Server size {0} is not available").format(size))

	server_id = generate_server_id()
	while frappe.db.exists("Server", {"server_id": server_id}):
		server_id = generate_server_id()
	doc = frappe.get_doc(
		{
			"doctype": "Server",
			"server_id": server_id,
			"hostname": pu.server_hostname(title, server_id),
			"ownership": "Hosted",
			"team": c.team,
			"status": "Pending",
			"plan": size_row.plan,
			"provider": region_row.provider,
			"region": region_row.name,
			"size": size_row.name,
			"provisioning_status": pu.CREATING,
			"provisioning_started_at": now_datetime(),
		}
	)
	doc.insert(ignore_permissions=True)
	log_activity(
		c.team, "server.create_hosted", "Server", doc.name,
		{"region": region_row.name, "size": size_row.name, "plan": size_row.plan},
	)
	from nexaploy.provisioning.jobs import enqueue_provision

	enqueue_provision(doc.name)
	return {"name": doc.name, "server_id": doc.server_id, "provisioning_status": doc.provisioning_status}


@frappe.whitelist()
def provisioning_status(server):
	c = _ctx()
	_check_team("Server", server, c, "read")
	return frappe.db.get_value(
		"Server",
		server,
		["name", "server_id", "hostname", "status", "provisioning_status", "public_ipv4", "public_ipv6",
		 "provisioning_error", "provisioning_started_at"],
		as_dict=True,
	)


@frappe.whitelist(methods=["POST"])
def delete_hosted_server(server):
	"""Delete a Hosted server created by Nexaploy (refused while sites or benches remain)."""
	c = _ctx()
	_check_team("Server", server, c, "admin")
	row = frappe.db.get_value(
		"Server", server, ["name", "team", "ownership", "provider", "status", "provisioning_status"], as_dict=True
	)
	sites = frappe.db.count("Site", {"server": server, "status": ["!=", "Archived"]})
	benches = frappe.db.count("Bench", {"server": server, "status": ["!=", "Archived"]})
	blockers = pu.deletion_blockers(row, sites, benches)
	if blockers:
		frappe.throw(_(blockers[0]))
	frappe.db.set_value("Server", server, {"provisioning_status": pu.DELETING})
	log_activity(row.team, "server.delete_hosted", "Server", server)
	from nexaploy.ops import sync_billing
	from nexaploy.provisioning.jobs import enqueue_deprovision

	sync_billing(frappe.get_doc("Server", server))  # billing stops now
	enqueue_deprovision(server)
	return {"name": server, "provisioning_status": pu.DELETING}
