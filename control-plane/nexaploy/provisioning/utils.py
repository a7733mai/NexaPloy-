"""Pure provisioning rules (no frappe import; tests/test_provisioning.py).

Server.provisioning_status life cycle (Hosted servers created by the control plane):

    Not Provisioned  -- BYOS servers and Hosted servers installed by hand
    Creating         -- Server row exists, provider call queued / running
    Bootstrapping    -- VM exists, cloud-init is running install.sh
    Ready            -- the agent registered (api/agent.register) or heartbeated
    Failed           -- provider error, or no registration within BOOTSTRAP_TIMEOUT_MINUTES
    Deleting         -- delete requested, provider call queued / running
    Deleted          -- VM gone; Server is Archived
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

NOT_PROVISIONED = "Not Provisioned"
CREATING = "Creating"
BOOTSTRAPPING = "Bootstrapping"
READY = "Ready"
FAILED = "Failed"
DELETING = "Deleting"
DELETED = "Deleted"
PROVISIONING_STATUSES = (NOT_PROVISIONED, CREATING, BOOTSTRAPPING, READY, FAILED, DELETING, DELETED)

# statuses in which the VM is (being) set up and the agent has not registered yet
IN_PROGRESS = (CREATING, BOOTSTRAPPING)
# a Hosted server is billed from Bootstrapping on (the VM costs us money) until Deleted;
# a server that failed or is being removed is not billed
NOT_BILLED = (CREATING, FAILED, DELETING, DELETED)

BOOTSTRAP_TIMEOUT_MINUTES = 20
REGISTRATION_TOKEN_TTL_HOURS = 24

LABEL_RE = re.compile(r"[^A-Za-z0-9._-]+")
HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


# --------------------------------------------------------------------------- naming


def server_hostname(title: Any, server_id: str) -> str:
	"""Hostname for the VM: slug of the customer's title, else the server id (srv-xxxx).

	The provider name, the cloud-init hostname and Server.hostname all use it.
	"""
	text = str(title or "").strip().lower()
	text = re.sub(r"[^a-z0-9-]+", "-", text)
	text = re.sub(r"-{2,}", "-", text).strip("-")[:63].strip("-")
	if not text or not HOSTNAME_RE.match(text):
		return server_id
	return text


def provider_labels(server_id: str, team: str | None = None) -> dict[str, str]:
	"""Labels/tags on the VM so ops can map it back (values limited to [A-Za-z0-9._-], 63 chars)."""
	labels = {"nexaploy": "1", "nexaploy-server": server_id}
	if team:
		labels["nexaploy-team"] = team
	return {k: LABEL_RE.sub("-", str(v)).strip("-._")[:63] or "x" for k, v in labels.items()}


def cloud_region_name(provider: str, code: str) -> str:
	"""Document name of a Cloud Region / Cloud Server Size: "<provider>-<code>"."""
	return f"{provider}-{code}"


# --------------------------------------------------------------------------- state rules


def is_billable(provisioning_status: Any) -> bool:
	return (provisioning_status or NOT_PROVISIONED) not in NOT_BILLED


def bootstrap_timed_out(started_at: datetime | None, now: datetime, minutes: int = BOOTSTRAP_TIMEOUT_MINUTES) -> bool:
	"""True when a server has been Creating/Bootstrapping for longer than `minutes`."""
	if started_at is None:
		return True
	return now - started_at > timedelta(minutes=minutes)


def deletion_blockers(server: Mapping[str, Any], active_sites: int, active_benches: int) -> list[str]:
	"""Reasons a hosted server cannot be deleted (empty = may delete)."""
	reasons = []
	if server.get("ownership") != "Hosted" or not server.get("provider"):
		reasons.append("Only servers created by Nexaploy can be deleted here")
	if server.get("provisioning_status") in (DELETING, DELETED) or server.get("status") == "Archived":
		reasons.append("This server is already deleted")
	if active_sites:
		reasons.append("Delete or move the sites on this server first")
	if active_benches:
		reasons.append("Archive the benches on this server first")
	return reasons


# --------------------------------------------------------------------------- registration


def registration_action(token_server: str | None, server: Mapping[str, Any] | None) -> str:
	"""What `api/agent.register` does with a valid, unused registration token.

	- token without a pre-created server -> "create" (BYOS: a new Server row)
	- token for a pre-created server      -> "attach" (activate that Server)
	Raises ValueError when the pre-created server cannot take a registration.
	"""
	if not token_server:
		return "create"
	if not server:
		raise ValueError("The server of this registration token no longer exists")
	if server.get("status") == "Archived" or server.get("provisioning_status") in (DELETING, DELETED):
		raise ValueError("The server of this registration token was deleted")
	if server.get("token_hash"):
		raise ValueError("The server of this registration token is already registered")
	return "attach"


def attach_values(
	server: Mapping[str, Any],
	hostname: str | None,
	agent_version: str | None,
	facts_json: str,
	token_hash: str,
	now: datetime,
) -> dict[str, Any]:
	"""Fields written on the pre-created Server when its agent registers."""
	values = {
		"status": "Active",
		"token_hash": token_hash,
		"agent_version": agent_version,
		"facts": facts_json,
		"last_heartbeat": now,
	}
	if hostname:
		values["hostname"] = str(hostname)[:140]
	if server.get("provider") or server.get("provisioning_status") not in (None, "", NOT_PROVISIONED):
		values["provisioning_status"] = READY
	return values


def ready_on_heartbeat(provisioning_status: Any) -> bool:
	"""A heartbeat proves the agent runs: Creating/Bootstrapping/Failed -> Ready."""
	return provisioning_status in (CREATING, BOOTSTRAPPING, FAILED)


# --------------------------------------------------------------------------- portal options


def build_options(
	providers: Iterable[Mapping[str, Any]],
	regions: Iterable[Mapping[str, Any]],
	sizes: Iterable[Mapping[str, Any]],
	plans: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
	"""Portal region/size picker: enabled providers with their enabled regions and the
	enabled sizes that have an enabled "Server Hosted" plan (the price the customer pays).

	providers: [{name, provider, title?}]; regions: [{name, provider, code, title}];
	sizes: [{name, provider, code, vcpu, memory_gb, disk_gb, plan}];
	plans: {plan name: {title, price_monthly, currency, plan_type, enabled}}.
	Providers without a region or a sellable size are left out.
	"""
	out = []
	regions = list(regions)
	sizes = list(sizes)
	for p in providers:
		p_regions = [
			{"name": r["name"], "code": r.get("code"), "title": r.get("title") or r.get("code")}
			for r in regions
			if r.get("provider") == p["name"]
		]
		p_sizes = []
		for s in sizes:
			if s.get("provider") != p["name"]:
				continue
			plan = plans.get(s.get("plan") or "")
			if not plan or not plan.get("enabled") or plan.get("plan_type") != "Server Hosted":
				continue
			p_sizes.append(
				{
					"name": s["name"],
					"code": s.get("code"),
					"vcpu": s.get("vcpu"),
					"memory_gb": s.get("memory_gb"),
					"disk_gb": s.get("disk_gb"),
					"plan": s.get("plan"),
					"plan_title": plan.get("title") or s.get("plan"),
					"price_monthly": float(Decimal(str(plan.get("price_monthly") or 0))),
					"currency": plan.get("currency") or "USD",
				}
			)
		p_sizes.sort(key=lambda s: (s["price_monthly"], s.get("vcpu") or 0, s["code"] or ""))
		p_regions.sort(key=lambda r: r["title"] or "")
		if p_regions and p_sizes:
			out.append(
				{
					"name": p["name"],
					"provider": p.get("provider"),
					"title": p.get("title") or p["name"],
					"regions": p_regions,
					"sizes": p_sizes,
				}
			)
	return out
