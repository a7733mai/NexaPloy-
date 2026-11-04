"""Paymob Accept (Egypt): card payments through the hosted iframe.

Checkout (https://developers.paymob.com/egypt/accept-standard-integration):
  1. POST /api/auth/tokens                {api_key}                      -> token
  2. POST /api/ecommerce/orders           {auth_token, amount_cents, currency,
                                           merchant_order_id, items: []} -> order id
  3. POST /api/acceptance/payment_keys    {auth_token, amount_cents, order_id,
                                           billing_data, currency, integration_id} -> token
  4. customer goes to /api/acceptance/iframes/<iframe_id>?payment_token=<token>

Callbacks carry an `hmac` (query string) = HMAC-SHA512(hmac_secret, concatenation of the
transaction fields in TRANSACTION_HMAC_FIELDS order). The "processed" callback is a POST
with {"type": "TRANSACTION", "obj": {...}}; the "response" callback is a GET redirect with
the same fields flattened into the query string (`order` = order id, `source_data.pan`...).

Module level is pure (no frappe import); `requests` is imported only when calling Paymob.
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

DEFAULT_BASE_URL = "https://accept.paymob.com"
MERCHANT_ORDER_SEP = "~"

# Documented order for the transaction callback HMAC.
TRANSACTION_HMAC_FIELDS = (
	"amount_cents",
	"created_at",
	"currency",
	"error_occured",
	"has_parent_transaction",
	"id",
	"integration_id",
	"is_3d_secure",
	"is_auth",
	"is_capture",
	"is_refunded",
	"is_standalone_payment",
	"is_voided",
	"order.id",
	"owner",
	"pending",
	"source_data.pan",
	"source_data.sub_type",
	"source_data.type",
	"success",
)


# --------------------------------------------------------------------------- HMAC (pure)


def _field(obj: Mapping[str, Any], dotted: str) -> Any:
	"""Value of `dotted` in a nested callback object or a flattened query string."""
	if dotted in obj:  # flattened GET callback: "source_data.pan"
		return obj[dotted]
	head, _, rest = dotted.partition(".")
	value = obj.get(head)
	if not rest:
		return value
	if isinstance(value, Mapping):
		return _field(value, rest)
	if head == "order" and rest == "id":  # GET callback: order=<id>
		return value
	return None


def _stringify(value: Any) -> str:
	if value is None:
		return ""
	if isinstance(value, bool):
		return "true" if value else "false"
	return str(value)


def hmac_message(obj: Mapping[str, Any]) -> str:
	return "".join(_stringify(_field(obj, f)) for f in TRANSACTION_HMAC_FIELDS)


def compute_hmac(obj: Mapping[str, Any], secret: str) -> str:
	return hmac.new(secret.encode("utf-8"), hmac_message(obj).encode("utf-8"), hashlib.sha512).hexdigest()


def verify_hmac(obj: Mapping[str, Any], secret: str | None, received: str | None) -> None:
	"""Raise WebhookVerificationError unless `received` is the HMAC of `obj`."""
	if not secret:
		raise WebhookVerificationError("Paymob HMAC secret is not configured")
	if not received:
		raise WebhookVerificationError("Missing Paymob hmac")
	if not hmac.compare_digest(compute_hmac(obj, secret), str(received).strip().lower()):
		raise WebhookVerificationError("Paymob HMAC mismatch")


def _truthy(value: Any) -> bool:
	if isinstance(value, str):
		return value.strip().lower() == "true"
	return bool(value)


def merchant_order_id(invoice: str, now: float | None = None) -> str:
	"""Paymob rejects a reused merchant_order_id, so every checkout attempt gets a suffix."""
	return f"{invoice}{MERCHANT_ORDER_SEP}{int(time.time() if now is None else now)}"


def invoice_from_merchant_order_id(value: Any) -> str | None:
	if not value:
		return None
	return str(value).partition(MERCHANT_ORDER_SEP)[0] or None


def transaction_to_payment(obj: Mapping[str, Any]) -> PaymentEvent:
	"""Normalise a (verified) transaction object (nested POST obj or flattened GET args)."""
	order_id = _field(obj, "order.id")
	merchant = _field(obj, "order.merchant_order_id") or obj.get("merchant_order_id")
	currency = (_stringify(obj.get("currency")) or "EGP").upper()
	amount = obj.get("amount_cents")
	success = _truthy(obj.get("success"))
	pending = _truthy(obj.get("pending"))
	reversed_ = _truthy(obj.get("is_voided")) or _truthy(obj.get("is_refunded"))
	if success and not pending and not reversed_:
		status = "paid"
	elif pending:
		status = "ignored"
	else:
		status = "failed"
	return PaymentEvent(
		gateway="Paymob",
		status=status,
		reference=_stringify(obj.get("id")) or None,
		checkout_reference=_stringify(order_id) or None,
		invoice=invoice_from_merchant_order_id(merchant),
		amount=from_minor_units(amount, currency) if amount not in (None, "") else None,
		currency=currency,
		raw=dict(obj),
	)


def billing_data(email: str | None) -> dict[str, str]:
	"""Paymob requires every billing_data key; "NA" is accepted for unknown values."""
	if email and "@" not in str(email):
		email = None  # e.g. a Team name: not an address
	name = (email or "customer").split("@")[0] or "customer"
	data = {
		k: "NA"
		for k in (
			"apartment", "floor", "street", "building", "shipping_method", "postal_code",
			"city", "country", "state", "phone_number",
		)
	}
	data.update({"first_name": name[:50], "last_name": "NA", "email": email or "NA@nexaploy.invalid"})
	return data


# --------------------------------------------------------------------------- gateway


class PaymobGateway(Gateway):
	name = "Paymob"

	def _post(self, path: str, body: dict) -> dict:
		import requests

		base = (self.settings.get("paymob_base_url") or DEFAULT_BASE_URL).rstrip("/")
		resp = requests.post(f"{base}{path}", json=body, timeout=30)
		try:
			data = resp.json()
		except ValueError:
			data = {}
		if resp.status_code >= 400:
			detail = data.get("detail") or data.get("message") or resp.text[:300]
			raise GatewayError(f"Paymob {path} failed: {detail}")
		return data

	def create_checkout(self, invoice):
		s = self.settings
		for key in ("paymob_api_key", "paymob_integration_id", "paymob_iframe_id"):
			if not s.get(key):
				raise GatewayError(f"Paymob is not configured ({key})")
		if money(invoice.get("total")) <= 0:
			raise GatewayError("Nothing to pay")
		currency = (s.get("paymob_currency") or "EGP").upper()
		rate = 1 if (invoice.get("currency") or currency).upper() == currency else s.get("paymob_exchange_rate") or 1
		amount_cents = to_minor_units(invoice["total"], currency, rate)

		token = self._post("/api/auth/tokens", {"api_key": s["paymob_api_key"]}).get("token")
		if not token:
			raise GatewayError("Paymob authentication failed")
		order = self._post(
			"/api/ecommerce/orders",
			{
				"auth_token": token,
				"delivery_needed": False,
				"amount_cents": amount_cents,
				"currency": currency,
				"merchant_order_id": merchant_order_id(invoice["name"]),
				"items": [],
			},
		)
		order_id = order.get("id")
		if not order_id:
			raise GatewayError("Paymob did not return an order id")
		payment_key = self._post(
			"/api/acceptance/payment_keys",
			{
				"auth_token": token,
				"amount_cents": amount_cents,
				"expiration": 3600,
				"order_id": order_id,
				"billing_data": billing_data(invoice.get("email") or invoice.get("team")),
				"currency": currency,
				"integration_id": int(s["paymob_integration_id"]),
				"lock_order_when_paid": True,
			},
		).get("token")
		if not payment_key:
			raise GatewayError("Paymob did not return a payment key")
		base = (s.get("paymob_base_url") or DEFAULT_BASE_URL).rstrip("/")
		url = f"{base}/api/acceptance/iframes/{s['paymob_iframe_id']}?payment_token={payment_key}"
		return CheckoutSession(url=url, reference=str(order_id))

	def verify_webhook(self, request: WebhookRequest) -> PaymentEvent:
		secret = self.settings.get("paymob_hmac_secret")
		received = request.args.get("hmac")
		if request.method.upper() == "GET":
			obj = {k: v for k, v in request.args.items() if k != "hmac"}
		else:
			try:
				payload = json.loads(request.body or b"{}")
			except ValueError as e:
				raise WebhookVerificationError("Paymob payload is not JSON") from e
			if payload.get("type") not in (None, "TRANSACTION"):
				verify_hmac(payload.get("obj") or {}, secret, received)
				return PaymentEvent(gateway="Paymob", status="ignored", raw=payload)
			obj = payload.get("obj") or {}
		verify_hmac(obj, secret, received)
		return transaction_to_payment(obj)
