"""Stripe Checkout through the REST API (no stripe SDK).

* create_checkout: POST /v1/checkout/sessions (mode=payment, one line = the invoice total),
  metadata.invoice = invoice name; the customer is sent to session.url.
* webhooks: `Stripe-Signature: t=<unix>,v1=<hex>[,v1=...]`; the signature is
  HMAC-SHA256(webhook_secret, "<t>.<raw body>") and `t` must be within the tolerance
  (default 300 s) - https://docs.stripe.com/webhooks#verify-manually.

Module level is pure (no frappe import); `requests` is imported only when calling Stripe.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Mapping

from nexaploy.billing.gateways.base import (
	CheckoutSession,
	Gateway,
	GatewayError,
	PaymentEvent,
	WebhookRequest,
	WebhookVerificationError,
)
from nexaploy.billing.pricing import from_minor_units, money, to_minor_units

API_BASE = "https://api.stripe.com/v1"
DEFAULT_TOLERANCE = 300
PAID_EVENTS = ("checkout.session.completed", "checkout.session.async_payment_succeeded")
FAILED_EVENTS = ("checkout.session.async_payment_failed", "checkout.session.expired")


# --------------------------------------------------------------------------- signatures (pure)


def compute_signature(payload: bytes | str, secret: str, timestamp: int | str) -> str:
	if isinstance(payload, bytes):
		payload = payload.decode("utf-8")
	signed = f"{timestamp}.{payload}".encode("utf-8")
	return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def parse_signature_header(header: str | None) -> tuple[int, list[str]]:
	"""(timestamp, [v1 signatures]) from a Stripe-Signature header."""
	if not header:
		raise WebhookVerificationError("Missing Stripe-Signature header")
	timestamp = None
	signatures = []
	for part in header.split(","):
		key, sep, value = part.strip().partition("=")
		if not sep:
			continue
		if key == "t":
			try:
				timestamp = int(value)
			except ValueError as e:
				raise WebhookVerificationError("Invalid timestamp in Stripe-Signature") from e
		elif key == "v1":
			signatures.append(value)
	if timestamp is None:
		raise WebhookVerificationError("No timestamp in Stripe-Signature")
	if not signatures:
		raise WebhookVerificationError("No v1 signature in Stripe-Signature")
	return timestamp, signatures


def verify_signature(
	payload: bytes | str,
	header: str | None,
	secret: str | None,
	tolerance: int = DEFAULT_TOLERANCE,
	now: float | None = None,
) -> int:
	"""Raise WebhookVerificationError unless `header` signs `payload` with `secret` and its
	timestamp is within `tolerance` seconds of `now`. Returns the timestamp."""
	if not secret:
		raise WebhookVerificationError("Stripe webhook secret is not configured")
	timestamp, signatures = parse_signature_header(header)
	expected = compute_signature(payload, secret, timestamp)
	if not any(hmac.compare_digest(expected, sig) for sig in signatures):
		raise WebhookVerificationError("Stripe signature mismatch")
	now = time.time() if now is None else now
	if tolerance and abs(now - timestamp) > tolerance:
		raise WebhookVerificationError("Stripe signature timestamp outside the tolerance")
	return timestamp


def event_to_payment(event: Mapping[str, Any]) -> PaymentEvent:
	"""Normalise a (verified) Stripe event."""
	etype = event.get("type")
	obj = (event.get("data") or {}).get("object") or {}
	currency = (obj.get("currency") or "").upper() or None
	common = {
		"gateway": "Stripe",
		"reference": obj.get("payment_intent") or obj.get("id"),
		"checkout_reference": obj.get("id"),
		"invoice": (obj.get("metadata") or {}).get("invoice") or obj.get("client_reference_id"),
		"currency": currency,
		"amount": from_minor_units(obj["amount_total"], currency or "USD")
		if obj.get("amount_total") is not None
		else None,
		"raw": dict(event),
	}
	if etype in PAID_EVENTS and obj.get("payment_status") in ("paid", "no_payment_required"):
		return PaymentEvent(status="paid", **common)
	if etype in FAILED_EVENTS:
		return PaymentEvent(status="failed", **common)
	return PaymentEvent(status="ignored", **common)


def checkout_form(invoice: Mapping[str, Any], success_url: str, cancel_url: str) -> dict[str, str]:
	"""Form fields for POST /v1/checkout/sessions."""
	currency = (invoice.get("currency") or "USD").lower()
	form = {
		"mode": "payment",
		"success_url": success_url,
		"cancel_url": cancel_url,
		"client_reference_id": invoice["name"],
		"metadata[invoice]": invoice["name"],
		"payment_intent_data[metadata][invoice]": invoice["name"],
		"line_items[0][quantity]": "1",
		"line_items[0][price_data][currency]": currency,
		"line_items[0][price_data][unit_amount]": str(to_minor_units(invoice["total"], currency)),
		"line_items[0][price_data][product_data][name]": f"Nexaploy invoice {invoice['name']}",
	}
	email = invoice.get("email") or invoice.get("team")
	if email and "@" in str(email):
		form["customer_email"] = email
	return form


# --------------------------------------------------------------------------- gateway


class StripeGateway(Gateway):
	name = "Stripe"

	def create_checkout(self, invoice):
		import requests

		secret = self.settings.get("stripe_secret_key")
		if not secret:
			raise GatewayError("Stripe secret key is not configured")
		if money(invoice.get("total")) <= 0:
			raise GatewayError("Nothing to pay")
		return_url = self.settings.get("return_url") or ""
		sep = "&" if "?" in return_url else "?"
		form = checkout_form(
			invoice,
			success_url=f"{return_url}{sep}invoice={invoice['name']}&paid=1",
			cancel_url=f"{return_url}{sep}invoice={invoice['name']}&cancelled=1",
		)
		resp = requests.post(
			f"{API_BASE}/checkout/sessions",
			data=form,
			auth=(secret, ""),
			timeout=30,
		)
		try:
			data = resp.json()
		except ValueError:
			data = {}
		if resp.status_code >= 400 or not data.get("url"):
			message = (data.get("error") or {}).get("message") or resp.text[:300]
			raise GatewayError(f"Stripe checkout failed: {message}")
		return CheckoutSession(url=data["url"], reference=data["id"])

	def verify_webhook(self, request: WebhookRequest) -> PaymentEvent:
		verify_signature(
			request.body,
			request.header("Stripe-Signature"),
			self.settings.get("stripe_webhook_secret"),
			tolerance=int(self.settings.get("stripe_tolerance") or DEFAULT_TOLERANCE),
		)
		try:
			event = json.loads(request.body)
		except ValueError as e:
			raise WebhookVerificationError("Stripe payload is not JSON") from e
		return event_to_payment(event)
