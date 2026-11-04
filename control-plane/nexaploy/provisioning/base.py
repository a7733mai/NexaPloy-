"""Cloud provider interface (pure: no frappe import).

Every provider talks JSON over HTTPS with a Bearer token. All HTTP goes through a
*transport* callable so tests can swap the network for a fake:

    transport(method, url, headers, body, timeout) -> (status_code, data)

`data` is the decoded JSON body (or None for an empty body). The default transport
(`requests_transport`) imports `requests` lazily, so importing this module never needs it.

Normalised server dict returned by create_server/get_server:

    {"id": "<provider id as str>", "ipv4": "1.2.3.4" | None, "ipv6": "2a01::1" | None,
     "status": "<provider status>", "raw_status": ...}
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

DEFAULT_TIMEOUT = 30
USER_AGENT = "nexaploy-control-plane"

Transport = Callable[[str, str, Mapping[str, str], Any, int], "tuple[int, Any]"]


class ProviderError(Exception):
	"""An API call failed. `status` is the HTTP status (0 = network error)."""

	def __init__(self, message: str, status: int = 0, data: Any = None):
		super().__init__(message)
		self.status = status
		self.data = data


def requests_transport(method: str, url: str, headers: Mapping[str, str], body: Any = None, timeout: int = DEFAULT_TIMEOUT):
	"""Real HTTP via `requests` (imported lazily)."""
	import requests

	try:
		response = requests.request(method, url, headers=dict(headers), json=body, timeout=timeout)
	except requests.RequestException as exc:  # pragma: no cover - network
		raise ProviderError(f"{method} {url}: {exc}") from exc
	if not response.content:
		return response.status_code, None
	try:
		return response.status_code, response.json()
	except ValueError:
		return response.status_code, {"raw": response.text[:2000]}


class Provider:
	"""Base class: subclasses set `kind`, `base_url` and implement the five operations."""

	kind = ""
	base_url = ""

	def __init__(self, token: str, transport: Transport | None = None, timeout: int = DEFAULT_TIMEOUT):
		if not token:
			raise ProviderError(f"{self.kind or 'provider'}: API token is not configured")
		self.token = token
		self.transport = transport or requests_transport
		self.timeout = timeout

	# ------------------------------------------------------------------ http

	def headers(self) -> dict[str, str]:
		return {
			"Authorization": f"Bearer {self.token}",
			"Content-Type": "application/json",
			"Accept": "application/json",
			"User-Agent": USER_AGENT,
		}

	def request(self, method: str, path: str, body: Any = None, ok: Sequence[int] = ()) -> Any:
		url = self.base_url.rstrip("/") + "/" + path.lstrip("/")
		status, data = self.transport(method, url, self.headers(), body, self.timeout)
		if status in ok:
			return data
		if status >= 400 or status == 0:
			raise ProviderError(f"{self.kind} {method} {path} failed ({status}): {self.error_message(data)}", status, data)
		return data

	@staticmethod
	def error_message(data: Any) -> str:
		if isinstance(data, Mapping):
			err = data.get("error")
			if isinstance(err, Mapping):
				return str(err.get("message") or err.get("code") or err)
			if data.get("message"):
				return str(data["message"])
			if err:
				return str(err)
		return str(data)[:500] if data else "no response body"

	# ------------------------------------------------------------------ interface

	def create_server(
		self,
		name: str,
		region: str,
		size: str,
		image: str,
		user_data: str,
		ssh_keys: Sequence[str] | None = None,
		labels: Mapping[str, str] | None = None,
	) -> dict[str, Any]:
		raise NotImplementedError

	def get_server(self, server_id: str) -> dict[str, Any] | None:
		"""The normalised server, or None when the provider no longer knows it."""
		raise NotImplementedError

	def delete_server(self, server_id: str) -> bool:
		"""Delete the server; True when it is gone (also when it was already gone)."""
		raise NotImplementedError

	def list_regions(self) -> list[dict[str, Any]]:
		"""[{code, title, country, available}]"""
		raise NotImplementedError

	def list_sizes(self) -> list[dict[str, Any]]:
		"""[{code, vcpu, memory_gb, disk_gb, monthly_cost, regions}]"""
		raise NotImplementedError


PROVIDERS = ("Hetzner", "DigitalOcean")


def get_provider(kind: str, token: str, transport: Transport | None = None) -> Provider:
	"""Provider instance for a Cloud Provider.provider value (Hetzner / DigitalOcean)."""
	if kind == "Hetzner":
		from nexaploy.provisioning.hetzner import HetznerProvider

		return HetznerProvider(token, transport)
	if kind == "DigitalOcean":
		from nexaploy.provisioning.digitalocean import DigitalOceanProvider

		return DigitalOceanProvider(token, transport)
	raise ProviderError(f"Unknown cloud provider {kind!r}")


def parse_ssh_keys(value: Any) -> list[str]:
	"""Cloud Provider.ssh_key_ids (Small Text: comma / whitespace / newline separated)."""
	if not value:
		return []
	if isinstance(value, (list, tuple)):
		items = value
	else:
		items = str(value).replace(",", " ").split()
	out = []
	for item in items:
		item = str(item).strip()
		if item and item not in out:
			out.append(item)
	return out
