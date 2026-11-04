"""Hetzner Cloud API v1 (https://docs.hetzner.cloud/). Pure: no frappe import."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from nexaploy.provisioning.base import Provider


def _key(value: str) -> Any:
	"""Hetzner accepts SSH key ids (int) or names (str)."""
	return int(value) if str(value).isdigit() else str(value)


def ipv6_address(network: str | None) -> str | None:
	"""Hetzner returns the server's /64 (`2a01:4f8:c0c:1::/64`); the host is `::1` in it."""
	if not network:
		return None
	prefix = str(network).split("/", 1)[0]
	if prefix.endswith("::"):
		return prefix + "1"
	return prefix


def normalize_server(server: Mapping[str, Any] | None) -> dict[str, Any] | None:
	if not server:
		return None
	net = server.get("public_net") or {}
	ipv4 = (net.get("ipv4") or {}).get("ip")
	ipv6 = ipv6_address((net.get("ipv6") or {}).get("ip"))
	return {
		"id": str(server.get("id")),
		"ipv4": ipv4 or None,
		"ipv6": ipv6,
		"status": server.get("status") or "",
	}


class HetznerProvider(Provider):
	kind = "Hetzner"
	base_url = "https://api.hetzner.cloud/v1"

	def create_server_body(
		self,
		name: str,
		region: str,
		size: str,
		image: str,
		user_data: str,
		ssh_keys: Sequence[str] | None = None,
		labels: Mapping[str, str] | None = None,
	) -> dict[str, Any]:
		body = {
			"name": name,
			"server_type": size,
			"location": region,
			"image": image,
			"user_data": user_data,
			"start_after_create": True,
			"public_net": {"enable_ipv4": True, "enable_ipv6": True},
			"labels": dict(labels or {}),
		}
		if ssh_keys:
			body["ssh_keys"] = [_key(k) for k in ssh_keys]
		return body

	def create_server(self, name, region, size, image, user_data, ssh_keys=None, labels=None):
		data = self.request(
			"POST", "/servers", self.create_server_body(name, region, size, image, user_data, ssh_keys, labels)
		)
		return normalize_server((data or {}).get("server"))

	def get_server(self, server_id):
		data = self.request("GET", f"/servers/{server_id}", ok=(404,))
		if not data or "server" not in data:
			return None
		return normalize_server(data["server"])

	def delete_server(self, server_id):
		self.request("DELETE", f"/servers/{server_id}", ok=(404,))
		return True

	def list_regions(self):
		data = self.request("GET", "/locations") or {}
		return [
			{
				"code": loc.get("name"),
				"title": " ".join(x for x in (loc.get("city"), f"({loc.get('name')})") if x),
				"country": loc.get("country"),
				"available": True,
			}
			for loc in data.get("locations") or []
		]

	def list_sizes(self):
		data = self.request("GET", "/server_types?per_page=50") or {}
		out = []
		for st in data.get("server_types") or []:
			if st.get("deprecated") or st.get("deprecation"):
				continue
			prices = st.get("prices") or []
			monthly = None
			for p in prices:
				gross = ((p.get("price_monthly") or {}).get("gross"))
				if gross is not None:
					value = float(gross)
					monthly = value if monthly is None else min(monthly, value)
			out.append(
				{
					"code": st.get("name"),
					"vcpu": st.get("cores"),
					"memory_gb": st.get("memory"),
					"disk_gb": st.get("disk"),
					"monthly_cost": monthly,
					"regions": [p.get("location") for p in prices if p.get("location")],
				}
			)
		return out
