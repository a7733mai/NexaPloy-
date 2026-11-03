"""Pure-logic tests for nexaploy.billing (no frappe needed)."""

import ast
import json
import os
import time
from datetime import date
from decimal import Decimal

import pytest

from nexaploy.billing import pricing as p
from nexaploy.billing.gateways import paymob, stripe
from nexaploy.billing.gateways.base import WebhookRequest, WebhookVerificationError, get_gateway

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "nexaploy")
D = Decimal


@pytest.mark.parametrize(
	"rel",
	["billing/pricing.py", "billing/gateways/base.py", "billing/gateways/stripe.py", "billing/gateways/paymob.py"],
)
def test_pure_modules_do_not_import_frappe(rel):
	tree = ast.parse(open(os.path.join(APP, rel)).read())
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			assert all(not a.name.startswith("frappe") for a in node.names)
		if isinstance(node, ast.ImportFrom):
			assert not (node.module or "").startswith("frappe")


# ------------------------------------------------------------------ proration

NOV = (date(2026, 11, 1), date(2026, 11, 30))


def test_prorate_full_month():
	assert p.prorate(10, None, None, *NOV) == D("10.00")
	assert p.prorate(10, date(2026, 1, 1), None, *NOV) == D("10.00")
	assert p.prorate(10, date(2026, 1, 1), date(2027, 1, 1), *NOV) == D("10.00")


def test_prorate_partial():
	# started on the 16th: 15 of 30 days
	assert p.prorate(10, date(2026, 11, 16), None, *NOV) == D("5.00")
	# ended on the 10th (inclusive): 10 of 30 days
	assert p.prorate(40, date(2026, 10, 1), date(2026, 11, 10), *NOV) == D("13.33")
	# single day
	assert p.prorate(5, "2026-11-30", "2026-11-30", *NOV) == D("0.17")
	# rounding half up: 1 day of 31 of 15.50 -> 0.5
	assert p.prorate("15.50", date(2026, 12, 31), None, date(2026, 12, 1), date(2026, 12, 31)) == D("0.50")


def test_prorate_outside_period():
	assert p.prorate(10, date(2026, 12, 1), None, *NOV) == D("0.00")
	assert p.prorate(10, date(2026, 9, 1), date(2026, 10, 31), *NOV) == D("0.00")
	assert p.prorate(10, None, None, date(2026, 11, 2), date(2026, 11, 1)) == D("0.00")


def test_active_days_and_dates():
	assert p.active_days("2026-11-05", "2026-11-05T12:00:00", *NOV) == 1
	assert p.days_inclusive(date(2026, 2, 1), date(2026, 2, 28)) == 28
	assert p.as_date("2026-11-03 10:00:00") == date(2026, 11, 3)
	assert p.as_date(None) is None


# ------------------------------------------------------------------ invoice items & totals


def subs():
	return [
		{"name": "s1", "document_type": "Site", "document_name": "a.example.com", "plan": "Site Basic",
		 "price_monthly": 5, "start_date": "2026-10-01", "end_date": None, "status": "Active"},
		{"name": "s2", "document_type": "Server", "document_name": "srv-1", "plan": "BYOS Server",
		 "price_monthly": 10, "start_date": "2026-11-16", "end_date": None, "status": "Active"},
		{"name": "s3", "document_type": "Marketplace App", "document_name": "helpdesk", "site": "a.example.com",
		 "price_monthly": "12.50", "start_date": "2026-11-01", "end_date": "2026-11-10", "status": "Cancelled"},
		{"name": "s4", "document_type": "Site", "document_name": "paused.example.com",
		 "price_monthly": 5, "start_date": "2026-10-01", "end_date": None, "status": "Paused"},
		{"name": "s5", "document_type": "Site", "document_name": "free.example.com",
		 "price_monthly": 0, "start_date": "2026-10-01", "end_date": None, "status": "Active"},
		{"name": "s6", "document_type": "Site", "document_name": "old.example.com",
		 "price_monthly": 5, "start_date": "2026-01-01", "end_date": "2026-10-31", "status": "Cancelled"},
		{"name": "s7", "document_type": "Site", "document_name": "future.example.com",
		 "price_monthly": 5, "start_date": "2026-12-01", "end_date": None, "status": "Active"},
	]


def test_build_invoice_items():
	items = p.build_invoice_items(subs(), NOV)
	assert [i["subscription"] for i in items] == ["s2", "s1", "s3"]  # Server, Site, App
	server, site, app = items
	assert server["amount"] == D("5.00") and server["quantity_days"] == 15
	assert server["unit_price"] == D("10.00")
	assert server["description"] == "Server srv-1 (BYOS Server) - 15/30 days"
	assert site["amount"] == D("5.00") and site["quantity_days"] == 30
	assert site["description"] == "Site a.example.com (Site Basic)"
	assert app["amount"] == D("4.17") and app["quantity_days"] == 10
	assert app["description"] == "App helpdesk on a.example.com - 10/30 days"
	assert app["site"] == "a.example.com"
	assert p.build_invoice_items([], NOV) == []


def test_compute_totals():
	items = p.build_invoice_items(subs(), NOV)
	t = p.compute_totals(items, 14)
	assert t == {"subtotal": D("14.17"), "tax": D("1.98"), "total": D("16.15")}
	assert p.compute_totals(items) == {"subtotal": D("14.17"), "tax": D("0.00"), "total": D("14.17")}
	assert p.compute_totals([], 14) == {"subtotal": D("0.00"), "tax": D("0.00"), "total": D("0.00")}
	# float inputs do not pick up binary noise
	assert p.compute_totals([{"amount": 0.1}, {"amount": 0.2}], 0)["total"] == D("0.30")


# ------------------------------------------------------------------ pricing rules & calendar


def test_plan_rules():
	assert p.server_plan("BYOS", None, "BYOS Server") == "BYOS Server"
	assert p.server_plan("BYOS", "Custom", "BYOS Server") == "Custom"
	assert p.server_plan("Hosted", "Hosted Small", "BYOS Server") == "Hosted Small"
	assert p.server_plan("Hosted", None, "BYOS Server") is None
	assert p.site_plan(None, "Site Basic") == "Site Basic"
	assert p.site_plan("Site Pro", "Site Basic") == "Site Pro"


def test_paid_app_prices_and_diff():
	catalog = {
		"helpdesk": {"pricing": "Paid", "price_monthly": 12},
		"erpnext": {"pricing": "Free", "price_monthly": 0},
		"broken": {"pricing": "Paid", "price_monthly": 3},
		"zero": {"pricing": "Paid", "price_monthly": 0},
	}
	rows = [
		{"app": "helpdesk", "status": "Installed"}, {"app": "erpnext", "status": "Installed"},
		{"app": "broken", "status": "Failed"}, {"app": "zero", "status": "Pending"},
		{"app": "unknown", "status": "Installed"},
	]
	assert p.paid_app_prices(rows, catalog) == {"helpdesk": D("12.00")}
	create, cancel = p.diff_subscriptions({"old": 1, "helpdesk": 1}, {"helpdesk": 12, "new": 3})
	assert create == ["new"] and cancel == ["old"]


def test_calendar():
	assert p.previous_month(date(2026, 12, 1)) == NOV
	assert p.previous_month("2026-01-15") == (date(2025, 12, 1), date(2025, 12, 31))
	assert p.previous_month(date(2028, 3, 1)) == (date(2028, 2, 1), date(2028, 2, 29))
	assert p.is_invoice_day(date(2026, 12, 1), 1)
	assert not p.is_invoice_day(date(2026, 12, 2), 1)
	assert p.is_invoice_day(date(2026, 2, 28), 31)  # clamps to the month's last day
	assert p.is_invoice_day(date(2026, 12, 1), None)
	assert p.due_date(date(2026, 12, 1), 7) == date(2026, 12, 8)
	assert p.days_overdue(date(2026, 12, 8), date(2026, 12, 7)) == 0
	assert p.days_overdue("2026-12-08", date(2026, 12, 20)) == 12
	assert not p.should_suspend(date(2026, 12, 8), date(2026, 12, 22), 14)
	assert p.should_suspend(date(2026, 12, 8), date(2026, 12, 23), 14)


def test_minor_units():
	assert p.to_minor_units(D("16.15"), "USD") == 1615
	assert p.to_minor_units("10", "EGP", rate="48.5") == 48500
	assert p.to_minor_units(500, "JPY") == 500
	assert p.from_minor_units(1615, "usd") == D("16.15")
	assert p.from_minor_units(500, "JPY") == D("500.00")


# ------------------------------------------------------------------ Stripe

SECRET = "whsec_test_secret"
EVENT = {
	"id": "evt_1",
	"type": "checkout.session.completed",
	"data": {"object": {
		"id": "cs_test_1", "payment_intent": "pi_1", "payment_status": "paid", "amount_total": 1615,
		"currency": "usd", "client_reference_id": "INV-2026-00001", "metadata": {"invoice": "INV-2026-00001"},
	}},
}


def stripe_header(payload, secret=SECRET, ts=None):
	ts = int(time.time()) if ts is None else ts
	return f"t={ts},v1={stripe.compute_signature(payload, secret, ts)}"


def test_stripe_signature_valid():
	payload = json.dumps(EVENT).encode()
	ts = int(time.time())
	assert stripe.verify_signature(payload, stripe_header(payload, ts=ts), SECRET) == ts
	# several v1 signatures (secret rotation): any match is enough
	header = f"t={ts},v1={'0' * 64},v1={stripe.compute_signature(payload, SECRET, ts)},v0=abc"
	assert stripe.verify_signature(payload, header, SECRET) == ts
	# known vector: HMAC-SHA256("secret", "1.{}")
	expected = "1122767b193110cfec322b6f199b599edbf608ed087f2d27afb0b97d99523908"
	assert stripe.compute_signature(b"{}", "secret", 1) == expected
	assert stripe.compute_signature("{}", "secret", "1") == expected


def test_stripe_signature_invalid():
	payload = json.dumps(EVENT).encode()
	header = stripe_header(payload)
	with pytest.raises(WebhookVerificationError):
		stripe.verify_signature(payload + b" ", header, SECRET)  # body tampered
	with pytest.raises(WebhookVerificationError):
		stripe.verify_signature(payload, header, "whsec_other")
	with pytest.raises(WebhookVerificationError):
		stripe.verify_signature(payload, stripe_header(payload, secret="whsec_other"), SECRET)
	for bad in (None, "", "v1=abc", "t=123", "t=abc,v1=def"):
		with pytest.raises(WebhookVerificationError):
			stripe.verify_signature(payload, bad, SECRET)
	with pytest.raises(WebhookVerificationError):
		stripe.verify_signature(payload, header, "")


def test_stripe_signature_expired():
	payload = json.dumps(EVENT).encode()
	now = 1_800_000_000
	old = stripe_header(payload, ts=now - 301)
	with pytest.raises(WebhookVerificationError, match="tolerance"):
		stripe.verify_signature(payload, old, SECRET, now=now)
	assert stripe.verify_signature(payload, stripe_header(payload, ts=now - 299), SECRET, now=now)
	future = stripe_header(payload, ts=now + 301)
	with pytest.raises(WebhookVerificationError):
		stripe.verify_signature(payload, future, SECRET, now=now)
	# tolerance 0 disables the timestamp check
	assert stripe.verify_signature(payload, old, SECRET, tolerance=0, now=now)


def test_stripe_event_mapping():
	ev = stripe.event_to_payment(EVENT)
	assert (ev.status, ev.invoice, ev.amount, ev.currency) == ("paid", "INV-2026-00001", D("16.15"), "USD")
	assert ev.reference == "pi_1" and ev.checkout_reference == "cs_test_1"
	unpaid = json.loads(json.dumps(EVENT))
	unpaid["data"]["object"]["payment_status"] = "unpaid"
	assert stripe.event_to_payment(unpaid).status == "ignored"
	failed = json.loads(json.dumps(EVENT))
	failed["type"] = "checkout.session.async_payment_failed"
	assert stripe.event_to_payment(failed).status == "failed"
	assert stripe.event_to_payment({"type": "customer.created", "data": {"object": {}}}).status == "ignored"


def test_stripe_gateway_verify_webhook():
	payload = json.dumps(EVENT).encode()
	gw = get_gateway("Stripe", {"stripe_webhook_secret": SECRET})
	req = WebhookRequest(body=payload, headers={"stripe-signature": stripe_header(payload)})
	assert gw.verify_webhook(req).status == "paid"
	with pytest.raises(WebhookVerificationError):
		gw.verify_webhook(WebhookRequest(body=payload, headers={}))


def test_stripe_checkout_form():
	form = stripe.checkout_form(
		{"name": "INV-1", "total": 16.15, "currency": "USD", "team": "a@b.com"}, "https://s", "https://c"
	)
	assert form["line_items[0][price_data][unit_amount]"] == "1615"
	assert form["line_items[0][price_data][currency]"] == "usd"
	assert form["metadata[invoice]"] == "INV-1" and form["customer_email"] == "a@b.com"


# ------------------------------------------------------------------ Paymob

HMAC_SECRET = "paymob_hmac_secret"
TXN = {
	"id": 192036465,
	"pending": False,
	"amount_cents": 78325,
	"success": True,
	"is_auth": False,
	"is_capture": False,
	"is_standalone_payment": True,
	"is_voided": False,
	"is_refunded": False,
	"is_3d_secure": True,
	"integration_id": 4097558,
	"has_parent_transaction": False,
	"order": {"id": 217503754, "merchant_order_id": "INV-2026-00001~1798000000"},
	"created_at": "2024-06-13T11:33:44.592345",
	"currency": "EGP",
	"source_data": {"pan": "2346", "type": "card", "sub_type": "MasterCard"},
	"error_occured": False,
	"owner": 302852,
}
EXPECTED_MESSAGE = (
	"78325" "2024-06-13T11:33:44.592345" "EGP" "false" "false" "192036465" "4097558" "true"
	"false" "false" "false" "true" "false" "217503754" "302852" "false" "2346" "MasterCard"
	"card" "true"
)


def test_paymob_hmac_message_order():
	assert paymob.hmac_message(TXN) == EXPECTED_MESSAGE


def test_paymob_hmac_valid_and_invalid():
	import hashlib
	import hmac as _hmac

	good = _hmac.new(HMAC_SECRET.encode(), EXPECTED_MESSAGE.encode(), hashlib.sha512).hexdigest()
	assert paymob.compute_hmac(TXN, HMAC_SECRET) == good
	paymob.verify_hmac(TXN, HMAC_SECRET, good)
	paymob.verify_hmac(TXN, HMAC_SECRET, good.upper())  # hex case does not matter
	tampered = json.loads(json.dumps(TXN))
	tampered["amount_cents"] = 100
	with pytest.raises(WebhookVerificationError):
		paymob.verify_hmac(tampered, HMAC_SECRET, good)
	with pytest.raises(WebhookVerificationError):
		paymob.verify_hmac(TXN, "other", good)
	with pytest.raises(WebhookVerificationError):
		paymob.verify_hmac(TXN, HMAC_SECRET, None)
	with pytest.raises(WebhookVerificationError):
		paymob.verify_hmac(TXN, "", good)


def flat_query(txn):
	"""The GET response callback: flattened string values, `order` = order id."""
	q = {}
	for k, v in txn.items():
		if isinstance(v, dict):
			if k == "order":
				q["order"] = str(v["id"])
				q["merchant_order_id"] = v["merchant_order_id"]
			else:
				for sk, sv in v.items():
					q[f"{k}.{sk}"] = str(sv)
		else:
			q[k] = ("true" if v else "false") if isinstance(v, bool) else str(v)
	return q


def test_paymob_gateway_post_and_get_callbacks():
	gw = get_gateway("Paymob", {"paymob_hmac_secret": HMAC_SECRET})
	sig = paymob.compute_hmac(TXN, HMAC_SECRET)
	body = json.dumps({"type": "TRANSACTION", "obj": TXN}).encode()
	ev = gw.verify_webhook(WebhookRequest(body=body, args={"hmac": sig}, method="POST"))
	assert (ev.status, ev.invoice, ev.amount, ev.currency) == ("paid", "INV-2026-00001", D("783.25"), "EGP")
	assert ev.reference == "192036465" and ev.checkout_reference == "217503754"

	q = flat_query(TXN)
	assert paymob.compute_hmac(q, HMAC_SECRET) == sig  # same HMAC for the flattened form
	ev = gw.verify_webhook(WebhookRequest(args={**q, "hmac": sig}, method="GET"))
	assert ev.status == "paid" and ev.invoice == "INV-2026-00001" and ev.checkout_reference == "217503754"

	with pytest.raises(WebhookVerificationError):
		gw.verify_webhook(WebhookRequest(body=body, args={"hmac": "0" * 128}, method="POST"))
	with pytest.raises(WebhookVerificationError):
		gw.verify_webhook(WebhookRequest(body=body, args={}, method="POST"))


def test_paymob_transaction_statuses():
	failed = {**TXN, "success": False}
	assert paymob.transaction_to_payment(failed).status == "failed"
	pending = {**TXN, "success": False, "pending": True}
	assert paymob.transaction_to_payment(pending).status == "ignored"
	refunded = {**TXN, "is_refunded": True}
	assert paymob.transaction_to_payment(refunded).status == "failed"
	assert paymob.transaction_to_payment({**flat_query(TXN), "success": "false"}).status == "failed"


def test_paymob_merchant_order_id():
	mid = paymob.merchant_order_id("INV-2026-00007", now=1798000000)
	assert mid == "INV-2026-00007~1798000000"
	assert paymob.invoice_from_merchant_order_id(mid) == "INV-2026-00007"
	assert paymob.invoice_from_merchant_order_id(None) is None
	data = paymob.billing_data("ahmed@example.eg")
	assert data["first_name"] == "ahmed" and data["email"] == "ahmed@example.eg" and data["city"] == "NA"
