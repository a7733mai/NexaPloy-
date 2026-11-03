"""Payment gateway interface. No frappe import: gateways get their configuration as a
plain mapping (see nexaploy.billing.invoicing.gateway_settings) and requests as a
`WebhookRequest`, so signature verification is unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping


class GatewayError(Exception):
	"""The gateway API call failed or is misconfigured."""


class WebhookVerificationError(Exception):
	"""A webhook failed signature / timestamp verification. Never process its payload."""


@dataclass
class CheckoutSession:
	url: str
	reference: str


@dataclass
class WebhookRequest:
	"""What a webhook needs from the HTTP request."""

	body: bytes = b""
	headers: Mapping[str, str] = field(default_factory=dict)
	args: Mapping[str, Any] = field(default_factory=dict)  # query string / form
	method: str = "POST"

	def header(self, name: str) -> str | None:
		lname = name.lower()
		for key, value in self.headers.items():
			if key.lower() == lname:
				return value
		return None


@dataclass
class PaymentEvent:
	"""A verified gateway notification, normalised.

	status: "paid" (money captured), "failed", or "ignored" (an event we do not act on).
	"""

	gateway: str
	status: str
	reference: str | None = None  # the payment / transaction id (idempotency key)
	checkout_reference: str | None = None  # what create_checkout returned (Invoice.gateway_reference)
	invoice: str | None = None
	amount: Decimal | None = None
	currency: str | None = None
	raw: Any = None


class Gateway:
	name = ""

	def __init__(self, settings: Mapping[str, Any]):
		self.settings = settings

	def create_checkout(self, invoice: Mapping[str, Any]) -> CheckoutSession:
		"""Start a hosted payment for `invoice` ({name, total, currency, team}) and return the
		URL to send the customer to plus the gateway's reference for it."""
		raise NotImplementedError

	def verify_webhook(self, request: WebhookRequest) -> PaymentEvent:
		"""Verify `request` came from the gateway (raise WebhookVerificationError otherwise)
		and return the normalised event."""
		raise NotImplementedError


def get_gateway(name: str, settings: Mapping[str, Any]) -> Gateway:
	if name == "Stripe":
		from nexaploy.billing.gateways.stripe import StripeGateway

		return StripeGateway(settings)
	if name == "Paymob":
		from nexaploy.billing.gateways.paymob import PaymobGateway

		return PaymobGateway(settings)
	raise GatewayError(f"Unknown payment gateway {name!r}")
