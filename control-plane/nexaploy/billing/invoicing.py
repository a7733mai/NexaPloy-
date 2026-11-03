"""Monthly invoices, dunning (overdue -> suspend) and applying gateway payments.

Scheduler (daily):
  * generate_monthly_invoices: on Billing Settings.invoice_day, invoice the previous month
    for every team with billable subscriptions. Idempotent per (team, period, currency).
  * mark_overdue_and_suspend: Unpaid past due -> Overdue; Overdue for more than
    suspend_after_days -> the team's Active sites go Suspended + maintenance_mode=1.
Payments (nexaploy.api.billing webhooks) go through apply_payment, which marks the invoice
Paid and unsuspends the team once nothing is past the suspension threshold.

Prepaid credits (nexaploy.billing.credits): a new subscription invoice is paid from the
team's credits first (fully -> Paid, partially -> amount_due is what the gateway charges).
"Credit Top-up" invoices are never dunned; paying one adds credits.
"""

import frappe
from frappe.utils import get_url, getdate, now_datetime, today

from nexaploy.agent_job import create_agent_job
from nexaploy.billing import credits_utils, pricing
from nexaploy.billing.subscriptions import reconcile_subscriptions

INVOICE = "Nexaploy Invoice"
PAYMENT = "Nexaploy Payment"
SUBSCRIPTION = "Nexaploy Subscription"
OPEN_STATUSES = ("Unpaid", "Overdue")


def settings():
	return frappe.get_cached_doc("Billing Settings")


def gateway_settings():
	"""Billing Settings as a plain dict with decrypted secrets, for nexaploy.billing.gateways."""
	s = frappe.get_single("Billing Settings")
	data = s.as_dict()
	for field in ("stripe_secret_key", "stripe_webhook_secret", "paymob_api_key", "paymob_hmac_secret"):
		data[field] = s.get_password(field, raise_exception=False)
	data["return_url"] = s.return_url or get_url("/dashboard/billing")
	return data


# --------------------------------------------------------------------------- invoices


def generate_monthly_invoices(date=None):
	s = settings()
	if not s.enabled:
		return []
	date = getdate(date or today())
	if not pricing.is_invoice_day(date, s.invoice_day):
		return []
	reconcile_subscriptions()
	period_start, period_end = pricing.previous_month(date)
	created = []
	for team in _teams_with_subscriptions(period_start, period_end):
		try:
			created.extend(generate_invoices_for_team(team, period_start, period_end, issue_date=date))
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Nexaploy: invoicing {team} failed")
	return created


def generate_invoices_for_team(team, period_start, period_end, issue_date=None):
	"""Create the Unpaid invoice(s) - one per currency - for `team` over the period.
	Skips a currency that already has a non-Void invoice for the same period."""
	s = settings()
	period_start, period_end = getdate(period_start), getdate(period_end)
	subs = frappe.get_all(
		SUBSCRIPTION,
		filters={
			"team": team,
			"status": ["in", list(pricing.BILLABLE_STATUSES)],
			"start_date": ["<=", period_end],
		},
		or_filters=[["end_date", "is", "not set"], ["end_date", ">=", period_start]],
		fields=[
			"name", "document_type", "document_name", "site", "plan", "price_monthly",
			"currency", "start_date", "end_date", "status",
		],
	)
	by_currency = {}
	for sub in subs:
		by_currency.setdefault(sub.currency or s.currency or "USD", []).append(sub)

	created = []
	for currency, rows in sorted(by_currency.items()):
		existing = frappe.get_all(
			INVOICE,
			filters={
				"team": team,
				"period_start": period_start,
				"period_end": period_end,
				"currency": currency,
				"status": ["!=", "Void"],
			},
			fields=["name", "invoice_type"],
		)
		if any(e.invoice_type != credits_utils.TOPUP_INVOICE_TYPE for e in existing):
			continue
		items = pricing.build_invoice_items(rows, (period_start, period_end))
		if not items:
			continue
		invoice = frappe.get_doc(
			{
				"doctype": INVOICE,
				"team": team,
				"invoice_type": credits_utils.SUBSCRIPTION_INVOICE_TYPE,
				"status": "Unpaid",
				"currency": currency,
				"period_start": period_start,
				"period_end": period_end,
				"due_date": pricing.due_date(issue_date or today(), s.grace_days),
				"tax_percent": s.tax_percent or 0,
				"items": [{k: (float(v) if k in ("unit_price", "amount") else v) for k, v in i.items()} for i in items],
			}
		)
		invoice.insert(ignore_permissions=True)
		if invoice.total <= 0:
			invoice.db_set("status", "Paid")
		else:
			_apply_credits(invoice)
		created.append(invoice.name)
	return created


def _apply_credits(invoice):
	"""Pay a new invoice from prepaid credits (never fails invoicing)."""
	from nexaploy.billing.credits import apply_credits_to_invoice

	try:
		frappe.db.savepoint("nexaploy_credits")
		apply_credits_to_invoice(invoice)
	except Exception:
		frappe.db.rollback(save_point="nexaploy_credits")
		frappe.log_error(title=f"Nexaploy: applying credits to {invoice.name} failed")


def _teams_with_subscriptions(period_start, period_end):
	rows = frappe.get_all(
		SUBSCRIPTION,
		filters={"status": ["in", list(pricing.BILLABLE_STATUSES)], "start_date": ["<=", period_end]},
		or_filters=[["end_date", "is", "not set"], ["end_date", ">=", period_start]],
		fields=["team"],
		distinct=True,
	)
	return sorted({r.team for r in rows if r.team})


# --------------------------------------------------------------------------- dunning


def mark_overdue_and_suspend(date=None):
	s = settings()
	if not s.enabled:
		return
	date = getdate(date or today())
	for inv in frappe.get_all(
		INVOICE, filters={"status": "Unpaid", "due_date": ["<", date]}, fields=["name", "invoice_type"]
	):
		if inv.invoice_type == credits_utils.TOPUP_INVOICE_TYPE:
			continue  # an abandoned top-up checkout is not a debt
		frappe.db.set_value(INVOICE, inv.name, "status", "Overdue")
	try:
		from nexaploy.billing.credits import void_stale_topups

		void_stale_topups()
	except Exception:
		frappe.log_error(title="Nexaploy: voiding stale top-ups failed")

	teams = set()
	for inv in frappe.get_all(INVOICE, filters={"status": "Overdue"}, fields=["team", "due_date"]):
		if pricing.should_suspend(inv.due_date, date, s.suspend_after_days):
			teams.add(inv.team)
	for team in sorted(teams):
		try:
			suspend_team(team)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Nexaploy: suspending {team} failed")


def team_must_stay_suspended(team, date=None):
	s = settings()
	date = getdate(date or today())
	return any(
		pricing.should_suspend(inv.due_date, date, s.suspend_after_days)
		for inv in frappe.get_all(INVOICE, filters={"team": team, "status": "Overdue"}, fields=["due_date"])
	)


def suspend_team(team):
	"""Active sites of `team` -> Suspended + maintenance_mode=1 on the server."""
	for site in frappe.get_all("Site", filters={"team": team, "status": "Active"}, fields=["name", "server"]):
		_set_maintenance(site, 1)
		frappe.db.set_value("Site", site.name, "status", "Suspended")


def unsuspend_team(team):
	"""Suspended sites of `team` -> Active + maintenance_mode=0."""
	for site in frappe.get_all("Site", filters={"team": team, "status": "Suspended"}, fields=["name", "server"]):
		_set_maintenance(site, 0)
		frappe.db.set_value("Site", site.name, "status", "Active")


def _set_maintenance(site, value):
	if not site.server:
		return
	if frappe.db.get_value("Server", site.server, "status") == "Archived":
		return
	create_agent_job(
		site.server,
		"site.set_config",
		{"site": site.name, "key": "maintenance_mode", "value": value},
		reference_doctype="Site",
		reference_name=site.name,
	)


# --------------------------------------------------------------------------- payments


def find_invoice(event):
	"""Invoice for a verified gateway PaymentEvent (metadata first, then checkout reference)."""
	if event.invoice and frappe.db.exists(INVOICE, event.invoice):
		return event.invoice
	if event.checkout_reference:
		return frappe.db.get_value(
			INVOICE, {"gateway": event.gateway, "gateway_reference": event.checkout_reference}, "name"
		)
	return None


def apply_payment(event):
	"""Record a verified gateway event. Idempotent on (gateway, reference). Returns the
	Nexaploy Payment name, or None when the event is ignored/unmatched."""
	if event.status == "ignored":
		return None
	invoice_name = find_invoice(event)
	if not invoice_name:
		frappe.log_error(title=f"Nexaploy: {event.gateway} payment for unknown invoice", message=frappe.as_json(event.raw))
		return None
	status = "Success" if event.status == "paid" else "Failed"
	reference = event.reference or event.checkout_reference
	existing = frappe.db.get_value(
		PAYMENT, {"gateway": event.gateway, "reference": reference, "status": status}, "name"
	)
	if existing:
		return existing

	invoice = frappe.get_doc(INVOICE, invoice_name)
	payment = frappe.get_doc(
		{
			"doctype": PAYMENT,
			"invoice": invoice.name,
			"team": invoice.team,
			"gateway": event.gateway,
			"status": status,
			"amount": float(event.amount) if event.amount is not None else None,
			"currency": event.currency,
			"reference": reference,
			"raw": frappe.as_json(event.raw or {}),
		}
	)
	payment.insert(ignore_permissions=True)

	is_topup = invoice.invoice_type == credits_utils.TOPUP_INVOICE_TYPE
	payable = invoice.status in ("Unpaid", "Overdue", "Draft") or (is_topup and invoice.status == "Void")
	if status == "Success" and payable:
		if _underpaid(event, invoice):
			frappe.log_error(
				title=f"Nexaploy: underpayment on {invoice.name}",
				message=f"{event.amount} {event.currency} < {_due(invoice)} {invoice.currency}",
			)
		else:
			mark_paid(invoice, event.gateway, event.checkout_reference or reference)
			if not is_topup:
				_log_payment(invoice, event.gateway)
	return payment.name


def mark_paid(invoice, gateway=None, reference=None):
	if isinstance(invoice, str):
		invoice = frappe.get_doc(INVOICE, invoice)
	values = {"status": "Paid", "paid_on": now_datetime(), "amount_due": 0}
	if gateway:
		values["gateway"] = gateway
	if reference:
		values["gateway_reference"] = reference
	invoice.db_set(values)
	if invoice.invoice_type == credits_utils.TOPUP_INVOICE_TYPE:
		from nexaploy.billing.credits import credit_topup

		credit_topup(invoice)
	if not team_must_stay_suspended(invoice.team):
		unsuspend_team(invoice.team)


def _log_payment(invoice, gateway):
	from nexaploy.activity import log_activity

	log_activity(
		invoice.team,
		"invoice.paid",
		INVOICE,
		invoice.name,
		{"gateway": gateway, "amount": invoice.total, "currency": invoice.currency},
	)


def _underpaid(event, invoice):
	if event.amount is None or not event.currency:
		return False
	if (event.currency or "").upper() != (invoice.currency or "").upper():
		return False  # converted (e.g. Paymob EGP for a USD invoice): trust the gateway amount
	return pricing.money(event.amount) < _due(invoice)


def _due(invoice):
	"""What the gateway had to collect: total minus credits already applied."""
	return credits_utils.amount_due(invoice.total, invoice.credits_applied or 0)
