"""DigitalOcean API v2 (https://docs.digitalocean.com/reference/api/). Pure: no frappe import."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from nexaploy.provisioning.base import Provider


def _key(value: str) -> Any:
	"""DigitalOcean accepts SSH key ids (int) or fingerprints (str)."""
	return int(value) if str(value).isdigit() else str(value)


def labels_to_tags(labels: Mapping[str, str] | None) -> list[str]:
	"""DO has tags, not labels: {"a": "b"} -> ["a:b"] (letters, digits, ':', '-', '_')."""
	tags = ["nexaploy"]
	for k, v in (labels or {}).items():
		tag = f"{k}:{v}" if v not in (None, "") else str(k)
		tag = "".join(c if c.isalnum() or c in ":-_" else "-" for c in tag)[:255]
		if tag and tag not in tags:
			tags.append(tag)
	return tags


def _public(addresses) -> str | None:
	for net in addresses or []:
		if net.get("type") == "public" and net.get("ip_address"):
			return net["ip_address"]
	return None


def normalize_droplet(droplet: Mapping[str, Any] | None) -> dict[str, Any] | None:
	if not droplet:
		return None
	networks = droplet.get("networks") or {}
	return {
		"id": str(droplet.get("id")),
		"ipv4": _public(networks.get("v4")),
		"ipv6": _public(networks.get("v6")),
		"status": droplet.get("status") or "",
	}


class DigitalOceanProvider(Provider):
	kind = "DigitalOcean"
	base_url = "https://api.digitalocean.com/v2"

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
			"region": region,
			"size": size,
			"image": image,
			"user_data": user_data,
			"ipv6": True,
			"monitoring": True,
			"tags": labels_to_tags(labels),
		}
		if ssh_keys:
			body["ssh_keys"] = [_key(k) for k in ssh_keys]
		return body

	def create_server(self, name, region, size, image, user_data, ssh_keys=None, labels=None):
		data = self.request(
			"POST", "/droplets", self.create_server_body(name, region, size, image, user_data, ssh_keys, labels)
		)
		return normalize_droplet((data or {}).get("droplet"))

	def get_server(self, server_id):
		data = self.request("GET", f"/droplets/{server_id}", ok=(404,))
		if not data or "droplet" not in data:
			return None
		return normalize_droplet(data["droplet"])

	def delete_server(self, server_id):
		self.request("DELETE", f"/droplets/{server_id}", ok=(404,))
		return True

	def list_regions(self):
		data = self.request("GET", "/regions?per_page=200") or {}
		return [
			{
				"code": r.get("slug"),
				"title": r.get("name"),
				"country": None,
				"available": bool(r.get("available")),
			}
			for r in data.get("regions") or []
		]

	def list_sizes(self):
		data = self.request("GET", "/sizes?per_page=200") or {}
		out = []
		for s in data.get("sizes") or []:
			if s.get("available") is False:
				continue
			out.append(
				{
					"code": s.get("slug"),
					"vcpu": s.get("vcpus"),
					"memory_gb": round((s.get("memory") or 0) / 1024, 2),
					"disk_gb": s.get("disk"),
					"monthly_cost": s.get("price_monthly"),
					"regions": list(s.get("regions") or []),
				}
			)
		return out
