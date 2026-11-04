"""Customer billing API and payment gateway webhooks.

Invoices belong to a Team (ARCHITECTURE.md §9): every member may read the current team's
invoices; paying needs the `billing` action (Owner/Admin/Billing). Platform admins pass
every check. Webhooks are guest endpoints authenticated by the gateway's
signature only - nothing in their payload is trusted before verification.

Prepaid credits: get_credits (balance + ledger, any member) and topup (billing action)
which creates a "Credit Top-up" invoice and starts its hosted checkout; the gateway
webhook marks it Paid and nexaploy.billing.credits.credit_topup adds the credits.
"""

import frappe
from frappe import _

from nexaploy.activity import log_activity
from nexaploy.billing import credits, invoicing
from nexaploy.billing.gateways import GATEWAYS
from nexaploy.billing.gateways.base import (
	GatewayError,
	WebhookRequest,
	WebhookVerificationError,
	get_gateway,
)
from nexaploy.team_utils import role_allows
from nexaploy.teams import check_team_access, get_current_team, team_notification_emails

INVOICE = "Nexaploy Invoice"
LIST_FIELDS = (
	"name", "status", "currency", "period_start", "period_end", "due_date", "paid_on",
	"subtotal", "tax", "total", "gateway", "invoice_type", "credits_applied", "amount_due",
)
ITEM_FIELDS = ("description", "document_type", "document_name", "site", "quantity_days", "unit_price", "amount")


def _user():
	user = frappe.session.user
	if user == "Guest":
		raise frappe.PermissionError(_("Login required"))
	return user


def _get_invoice_for_user(name, action="read"):
	user = _user()
	if not frappe.db.exists(INVOICE, name):
		raise frappe.DoesNotExistError(_("Invoice {0} not found").format(name))
	invoice = frappe.get_doc(INVOICE, name)
	try:
		check_team_access(invoice.team, "read", user)
	except (frappe.PermissionError, frappe.DoesNotExistError):
		# same error as a missing invoice: do not leak other teams' invoice names
		raise frappe.DoesNotExistError(_("Invoice {0} not found").format(name)) from None
	if action != "read":
		check_team_access(invoice.team, action, user)
	return invoice


@frappe.whitelist()
def get_invoices(status=None, limit=50):
	user = _user()
	team = get_current_team(user)
	check_team_access(team, "read", user)
	filters = {"status": ["!=", "Draft"], "team": team}
	if status:
		filters["status"] = status
	return frappe.get_all(
		INVOICE,
		filters=filters,
		fields=list(LIST_FIELDS),
		order_by="period_start desc, creation desc",
		limit_page_length=min(int(limit or 50), 500),
	)


@frappe.whitelist()
def get_invoice(name):
	invoice = _get_invoice_for_user(name)
	data = {f: invoice.get(f) for f in LIST_FIELDS}
	data.update(team=invoice.team, tax_percent=invoice.tax_percent, payment_url=invoice.payment_url)
	data["items"] = [{f: row.get(f) for f in ITEM_FIELDS} for row in invoice.items]
	data["payments"] = frappe.get_all(
		"Nexaploy Payment",
		filters={"invoice": invoice.name},
		fields=["gateway", "status", "amount", "currency", "reference", "creation"],
		order_by="creation desc",
	)
	data["gateways"] = enabled_gateways()
	return data


@frappe.whitelist()
def get_payment_gateways():
	_user()
	return enabled_gateways()


def enabled_gateways():
	s = invoicing.settings()
	return [g for g in GATEWAYS if s.get(f"enable_{g.lower()}")]


@frappe.whitelist(methods=["POST"])
def pay_invoice(name, gateway):
	"""Start a hosted checkout for an Unpaid/Overdue invoice; returns {url, reference}."""
	invoice = _get_invoice_for_user(name, "billing")
	if invoice.status not in invoicing.OPEN_STATUSES:
		frappe.throw(_("Invoice {0} is {1}").format(invoice.name, invoice.status))
	if gateway not in enabled_gateways():
		frappe.throw(_("Payment gateway {0} is not available").format(gateway))
	if invoice.invoice_type != credits.cu.TOPUP_INVOICE_TYPE:
		# spend available credits first; a fully covered invoice needs no checkout
		credits.apply_credits_to_invoice(invoice)
		invoice.reload()
		if invoice.status == "Paid":
			return {"paid": True, "url": None, "reference": None}
	session = _checkout(invoice, gateway)
	log_activity(
		invoice.team, "invoice.pay", INVOICE, invoice.name,
		{"gateway": gateway, "amount": invoice.amount_due, "currency": invoice.currency},
	)
	return {"url": session.url, "reference": session.reference}


def _checkout(invoice, gateway):
	"""Hosted checkout for what is left to pay on `invoice` (total minus credits)."""
	due = invoicing._due(invoice)
	try:
		session = get_gateway(gateway, invoicing.gateway_settings()).create_checkout(
			{
				"name": invoice.name,
				"total": due,
				"currency": invoice.currency,
				"team": invoice.team,
				"email": next(iter(team_notification_emails(invoice.team, "billing")), None),
			}
		)
	except GatewayError as e:
		frappe.log_error(title=f"Nexaploy: {gateway} checkout for {invoice.name} failed")
		frappe.throw(_("Could not start the payment: {0}").format(str(e)))
	invoice.db_set(
		{"gateway": gateway, "gateway_reference": session.reference, "payment_url": session.url}
	)
	return session


# --------------------------------------------------------------------------- prepaid credits


@frappe.whitelist()
def get_credits(limit=50, start=0):
	"""{team, currency, balance, balances, ledger, can_topup, min_topup, max_topup, gateways}
	for the current team (any member may read)."""
	user = _user()
	team = get_current_team(user)
	role = check_team_access(team, "read", user)
	s = invoicing.settings()
	return {
		"team": team,
		"currency": credits.team_currency(team),
		"balance": float(credits.get_balance(team)),
		"balances": {k: float(v) for k, v in credits.balances(team).items()},
		"ledger": credits.ledger(team, limit=limit, start=start),
		"can_topup": bool(s.enabled) and role_allows(role, "billing"),
		"min_topup": float(s.get("min_topup") or credits.cu.DEFAULT_MIN_TOPUP),
		"max_topup": float(s.get("max_topup") or credits.cu.DEFAULT_MAX_TOPUP),
		"gateways": enabled_gateways(),
	}


@frappe.whitelist(methods=["POST"])
def topup(amount, gateway):
	"""Buy prepaid credits: creates a "Credit Top-up" invoice of `amount` (team currency)
	and returns {invoice, url, reference} for its hosted checkout (billing action)."""
	user = _user()
	team = get_current_team(user)
	check_team_access(team, "billing", user)
	if not invoicing.settings().enabled:
		frappe.throw(_("Billing is not enabled"))
	if gateway not in enabled_gateways():
		frappe.throw(_("Payment gateway {0} is not available").format(gateway))
	invoice = credits.create_topup_invoice(team, amount)
	session = _checkout(invoice, gateway)
	log_activity(
		team, "credits.topup", INVOICE, invoice.name,
		{"amount": invoice.total, "currency": invoice.currency, "gateway": gateway, "status": "checkout"},
	)
	return {"invoice": invoice.name, "url": session.url, "reference": session.reference}


# --------------------------------------------------------------------------- webhooks


def _webhook_request():
	req = frappe.request
	args = dict(req.args) if req is not None else {}
	return WebhookRequest(
		body=req.get_data() if req is not None else b"",
		headers=dict(req.headers) if req is not None else {},
		args=args,
		method=req.method if req is not None else "POST",
	)


def _handle(gateway):
	try:
		event = get_gateway(gateway, invoicing.gateway_settings()).verify_webhook(_webhook_request())
	except WebhookVerificationError as e:
		frappe.local.response["http_status_code"] = 400
		return None, str(e)
	payment = invoicing.apply_payment(event)
	frappe.db.commit()
	return event, payment


@frappe.whitelist(allow_guest=True, methods=["POST"])
def stripe_webhook():
	event, result = _handle("Stripe")
	if event is None:
		return {"ok": False, "error": result}
	return {"ok": True, "status": event.status}


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
def paymob_webhook():
	"""POST = transaction processed callback; GET = transaction response callback (the
	customer's browser is redirected here after paying, then on to the billing page)."""
	event, result = _handle("Paymob")
	if frappe.request is not None and frappe.request.method == "GET":
		return_url = invoicing.gateway_settings()["return_url"]
		sep = "&" if "?" in return_url else "?"
		state = "paid=1" if event is not None and event.status == "paid" else "failed=1"
		invoice = f"invoice={event.invoice}&" if event is not None and event.invoice else ""
		frappe.local.response.pop("http_status_code", None)
		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = f"{return_url}{sep}{invoice}{state}"
		return None
	if event is None:
		return {"ok": False, "error": result}
	return {"ok": True, "status": event.status}
