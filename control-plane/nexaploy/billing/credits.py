"""Prepaid credits: the `Team Credit Transaction` ledger (database side).

Pure math lives in nexaploy.billing.credits_utils. Balances are per (team, currency);
Team.credit_balance caches the balance in the team's billing currency.

    team_currency(team) -> str
    get_balance(team, currency=None) -> Decimal
    add_credits(team, amount, type, ref=None, description=None, currency=None) -> name
    apply_credits_to_invoice(invoice) -> Decimal applied
    apply_credits_to_open_invoices(team, currency=None) -> [(invoice, applied)]
    grant_signup_credits(team) -> name | None
    create_topup_invoice(team, amount) -> Nexaploy Invoice doc
    credit_topup(invoice) -> name | None   (called when a top-up invoice is paid)
"""

import frappe
from frappe import _
from frappe.utils import now_datetime, today

from nexaploy.billing import credits_utils as cu
from nexaploy.billing.pricing import money

LEDGER = "Team Credit Transaction"
INVOICE = "Nexaploy Invoice"
PAYMENT = "Nexaploy Payment"
SIGNUP_REFERENCE = "signup"


def _settings():
	return frappe.get_cached_doc("Billing Settings")


def team_currency(team):
	"""The team's billing currency: Team.currency, else Billing Settings.currency, else USD."""
	currency = frappe.db.get_value("Team", team, "currency") if team else None
	if not currency:
		try:
			currency = frappe.db.get_single_value("Billing Settings", "currency")
		except Exception:
			currency = None
	return (currency or "USD").upper()


def get_balance(team, currency=None):
	"""Credit balance of `team` in `currency` (default: the team currency), from the ledger."""
	currency = (currency or team_currency(team)).upper()
	rows = frappe.get_all(LEDGER, filters={"team": team, "currency": currency}, fields=["amount", "currency"])
	return cu.ledger_balance(rows, currency)


def balances(team):
	"""{currency: balance} for every currency the team has ledger rows in."""
	rows = frappe.get_all(LEDGER, filters={"team": team}, fields=["amount", "currency"])
	result = {}
	for currency in sorted({(r.currency or "").upper() for r in rows if r.currency}):
		result[currency] = cu.ledger_balance(rows, currency)
	return result


def _lock_team(team):
	"""Serialise ledger writes per team (row lock on the Team until commit)."""
	if not frappe.db.get_value("Team", team, "name", for_update=True):
		raise frappe.DoesNotExistError(_("Team {0} not found").format(team))


def refresh_team_balance(team):
	"""Recompute Team.credit_balance (team currency) from the ledger."""
	if not team:
		return
	frappe.db.set_value(
		"Team", team, "credit_balance", float(get_balance(team)), update_modified=False
	)


def prepare_transaction(doc):
	"""Fill currency/balance_after on a ledger row inserted directly (desk Adjustment)."""
	_lock_team(doc.team)
	doc.currency = (doc.currency or team_currency(doc.team)).upper()
	try:
		amount = cu.check_transaction(doc.type, doc.amount)
		doc.balance_after = float(cu.next_balance(get_balance(doc.team, doc.currency), amount))
	except cu.CreditError as exc:
		frappe.throw(_(str(exc)))
	doc.amount = float(amount)


def add_credits(team, amount, type, ref=None, description=None, currency=None):
	"""Append a ledger row (positive adds, negative spends) and return its name.

	`ref` is (reference_doctype, reference_name) or a Document. Raises frappe.ValidationError
	for a wrong sign or when the balance would go below zero."""
	_lock_team(team)
	currency = (currency or team_currency(team)).upper()
	try:
		value = cu.check_transaction(type, amount)
		balance_after = cu.next_balance(get_balance(team, currency), value)
	except cu.CreditError as exc:
		frappe.throw(_(str(exc)))
	ref_dt, ref_name = _ref(ref)
	doc = frappe.get_doc(
		{
			"doctype": LEDGER,
			"team": team,
			"type": type,
			"amount": float(value),
			"currency": currency,
			"balance_after": float(balance_after),
			"reference_doctype": ref_dt,
			"reference_name": ref_name,
			"description": description,
		}
	)
	doc.flags.nexaploy_ledger = True
	doc.insert(ignore_permissions=True)
	return doc.name


def _ref(ref):
	if ref is None:
		return None, None
	if hasattr(ref, "doctype") and hasattr(ref, "name"):
		return ref.doctype, ref.name
	dt, name = ref
	return dt, name


# --------------------------------------------------------------------------- invoices


def apply_credits_to_invoice(invoice):
	"""Pay an Unpaid/Overdue subscription invoice from the team's credits, fully or
	partially. Updates credits_applied / amount_due and marks the invoice Paid when fully
	covered. Returns the amount applied (Decimal). Top-up invoices are never paid with
	credits; credits only apply in the invoice's own currency."""
	if isinstance(invoice, str):
		invoice = frappe.get_doc(INVOICE, invoice)
	if invoice.status not in cu.PAYABLE_STATUSES:
		return cu.ZERO
	if (invoice.invoice_type or cu.SUBSCRIPTION_INVOICE_TYPE) == cu.TOPUP_INVOICE_TYPE:
		return cu.ZERO
	currency = (invoice.currency or team_currency(invoice.team)).upper()
	_lock_team(invoice.team)
	balance = get_balance(invoice.team, currency)
	plan = cu.apply_to_invoice(invoice.total, invoice.credits_applied or 0, balance)
	if plan["apply"] <= 0:
		return cu.ZERO
	add_credits(
		invoice.team,
		-plan["apply"],
		cu.INVOICE_PAYMENT,
		(INVOICE, invoice.name),
		description=_("Applied to invoice {0}").format(invoice.name),
		currency=currency,
	)
	invoice.db_set(
		{"credits_applied": float(plan["credits_applied"]), "amount_due": float(plan["amount_due"])}
	)
	if plan["paid"]:
		frappe.get_doc(
			{
				"doctype": PAYMENT,
				"invoice": invoice.name,
				"team": invoice.team,
				"gateway": "Credits",
				"status": "Success",
				"amount": float(plan["credits_applied"]),
				"currency": currency,
				"reference": f"credits-{invoice.name}",
			}
		).insert(ignore_permissions=True)
		from nexaploy.billing.invoicing import mark_paid

		mark_paid(invoice, "Credits")
	_log(invoice.team, "credits.apply", invoice, {"amount": str(plan["apply"]), "currency": currency, "paid": plan["paid"]})
	return plan["apply"]


def apply_credits_to_open_invoices(team, currency=None):
	"""Spend the team's credits on its open subscription invoices, oldest due first."""
	currency = (currency or team_currency(team)).upper()
	invoices = frappe.get_all(
		INVOICE,
		filters={
			"team": team,
			"currency": currency,
			"status": ["in", list(cu.PAYABLE_STATUSES)],
		},
		fields=["name", "status", "due_date", "total", "credits_applied", "amount_due", "invoice_type"],
	)
	# (not a SQL filter: rows created before the field existed may have invoice_type NULL)
	invoices = [i for i in invoices if i.invoice_type != cu.TOPUP_INVOICE_TYPE]
	for inv in invoices:
		inv["amount_due"] = cu.amount_due(inv.total, inv.credits_applied or 0)
	result = []
	for name, _amount in cu.allocate_credits(get_balance(team, currency), invoices):
		applied = apply_credits_to_invoice(name)
		if applied > 0:
			result.append((name, applied))
	return result


# --------------------------------------------------------------------------- signup credits


def grant_signup_credits(team):
	"""Billing Settings.signup_credits as a Promo row, once per team and once per owner.
	Never raises (team creation must not fail over a promo)."""
	try:
		if not frappe.db.get_single_value("Billing Settings", "enabled"):
			return None
		configured = frappe.db.get_single_value("Billing Settings", "signup_credits") or 0
		if money(configured) <= 0:
			return None
		owner = frappe.db.get_value("Team", team, "owner_user")
		team_granted = bool(
			frappe.db.exists(LEDGER, {"team": team, "type": cu.PROMO, "description": SIGNUP_REFERENCE})
		)
		owner_granted = False
		if owner:
			owned = frappe.get_all("Team", filters={"owner_user": owner, "name": ["!=", team]}, pluck="name")
			owner_granted = bool(
				owned
				and frappe.db.exists(
					LEDGER, {"team": ["in", owned], "type": cu.PROMO, "description": SIGNUP_REFERENCE}
				)
			)
		amount = cu.signup_credit_amount(configured, team_granted, owner_granted)
		if amount <= 0:
			return None
		currency = (frappe.db.get_single_value("Billing Settings", "currency") or "USD").upper()
		name = add_credits(team, amount, cu.PROMO, ("Team", team), description=SIGNUP_REFERENCE, currency=currency)
		_log(team, "credits.promo", None, {"amount": str(amount), "currency": currency, "reason": "signup"})
		return name
	except Exception:
		frappe.log_error(title=f"Nexaploy: signup credits for {team} failed")
		return None


# --------------------------------------------------------------------------- top-ups


def create_topup_invoice(team, amount):
	"""Unpaid "Credit Top-up" invoice of `amount` (team currency, no tax). Paying it through a
	gateway adds the same amount of credits (credit_topup)."""
	s = _settings()
	try:
		value = cu.validate_topup_amount(amount, s.get("min_topup"), s.get("max_topup"))
	except cu.CreditError as exc:
		frappe.throw(_(str(exc)))
	currency = team_currency(team)
	invoice = frappe.get_doc(
		{
			"doctype": INVOICE,
			"team": team,
			"invoice_type": cu.TOPUP_INVOICE_TYPE,
			"status": "Unpaid",
			"currency": currency,
			"period_start": today(),
			"period_end": today(),
			"due_date": today(),
			"tax_percent": 0,
			"items": [
				{
					"description": _("Prepaid credits"),
					"document_type": "Team",
					"document_name": team,
					"quantity_days": 0,
					"unit_price": float(value),
					"amount": float(value),
				}
			],
		}
	)
	invoice.insert(ignore_permissions=True)
	return invoice


def credit_topup(invoice):
	"""Add the credits bought by a paid top-up invoice (idempotent), then spend them on
	open invoices. Returns the ledger row name (existing one when already credited)."""
	if isinstance(invoice, str):
		invoice = frappe.get_doc(INVOICE, invoice)
	if (invoice.invoice_type or "") != cu.TOPUP_INVOICE_TYPE:
		return None
	_lock_team(invoice.team)
	existing = frappe.db.get_value(
		LEDGER, {"reference_doctype": INVOICE, "reference_name": invoice.name, "type": cu.TOPUP}, "name"
	)
	if existing:
		return existing
	currency = (invoice.currency or team_currency(invoice.team)).upper()
	name = add_credits(
		invoice.team,
		money(invoice.total),
		cu.TOPUP,
		(INVOICE, invoice.name),
		description=_("Top-up {0}").format(invoice.name),
		currency=currency,
	)
	_log(invoice.team, "credits.topup", invoice, {"amount": str(money(invoice.total)), "currency": currency, "status": "paid"})
	try:
		apply_credits_to_open_invoices(invoice.team, currency)
	except Exception:
		frappe.log_error(title=f"Nexaploy: applying credits for {invoice.team} failed")
	return name


def void_stale_topups(days=3):
	"""Abandoned top-up checkouts: Unpaid top-up invoices older than `days` -> Void.
	A late gateway success still credits them (invoicing.apply_payment)."""
	from frappe.utils import add_days

	cutoff = add_days(now_datetime(), -int(days))
	for name in frappe.get_all(
		INVOICE,
		filters={"invoice_type": cu.TOPUP_INVOICE_TYPE, "status": "Unpaid", "creation": ["<", cutoff]},
		pluck="name",
	):
		frappe.db.set_value(INVOICE, name, "status", "Void")


def ledger(team, limit=50, start=0):
	return frappe.get_all(
		LEDGER,
		filters={"team": team},
		fields=[
			"name", "creation", "type", "amount", "currency", "balance_after",
			"reference_doctype", "reference_name", "description",
		],
		order_by="creation desc",
		start=int(start or 0),
		limit_page_length=min(int(limit or 50), 500),
	)


def _log(team, action, ref, details):
	from nexaploy.activity import log_activity

	log_activity(
		team,
		action,
		ref.doctype if ref is not None else None,
		ref.name if ref is not None else None,
		details,
	)
