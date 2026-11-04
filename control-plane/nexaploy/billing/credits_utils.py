"""Pure prepaid-credit math (no frappe import; tested in control-plane/tests/test_security.py).

The ledger is `Team Credit Transaction`: append-only rows with a signed `amount` (+ adds
credit, - spends it) and the running `balance_after` per (team, currency). Money is
`Decimal`, 2 dp, ROUND_HALF_UP (nexaploy.billing.pricing.money).

Transaction types and the sign they must carry:
  TopUp          +  customer bought credits through a gateway (a "Credit Top-up" invoice)
  Promo          +  signup credits / coupons granted by the platform
  Refund         +  money returned as credit
  InvoicePayment -  credits spent on a Nexaploy Invoice
  Adjustment     +/- manual correction by a platform admin
A balance never goes below zero.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable, Mapping

from nexaploy.billing.pricing import money, to_decimal

TOPUP = "TopUp"
INVOICE_PAYMENT = "InvoicePayment"
REFUND = "Refund"
PROMO = "Promo"
ADJUSTMENT = "Adjustment"
TRANSACTION_TYPES = (TOPUP, INVOICE_PAYMENT, REFUND, PROMO, ADJUSTMENT)
POSITIVE_TYPES = frozenset({TOPUP, REFUND, PROMO})
NEGATIVE_TYPES = frozenset({INVOICE_PAYMENT})

TOPUP_INVOICE_TYPE = "Credit Top-up"
SUBSCRIPTION_INVOICE_TYPE = "Subscription"
INVOICE_TYPES = (SUBSCRIPTION_INVOICE_TYPE, TOPUP_INVOICE_TYPE)
# Invoice statuses credits may be applied to
PAYABLE_STATUSES = ("Unpaid", "Overdue")

DEFAULT_MIN_TOPUP = Decimal("5")
DEFAULT_MAX_TOPUP = Decimal("10000")
ZERO = Decimal("0.00")


class CreditError(ValueError):
	"""An invalid credit operation (wrong sign, insufficient balance, bad amount)."""


def check_transaction(tx_type: str, amount: Any) -> Decimal:
	"""Validated, rounded signed amount for a ledger row of `tx_type`."""
	if tx_type not in TRANSACTION_TYPES:
		raise CreditError(f"Unknown credit transaction type {tx_type!r}")
	value = money(amount)
	if value == 0:
		raise CreditError("Credit amount must not be zero")
	if tx_type in POSITIVE_TYPES and value < 0:
		raise CreditError(f"{tx_type} must add credits")
	if tx_type in NEGATIVE_TYPES and value > 0:
		raise CreditError(f"{tx_type} must spend credits")
	return value


def ledger_balance(rows: Iterable[Mapping[str, Any]], currency: str | None = None) -> Decimal:
	"""Sum of `amount` over ledger rows (only those in `currency` when given)."""
	total = Decimal("0")
	for row in rows:
		if currency and (row.get("currency") or "").upper() != currency.upper():
			continue
		total += money(row.get("amount"))
	return money(total)


def next_balance(balance: Any, amount: Any) -> Decimal:
	"""Balance after adding `amount`; CreditError when it would go below zero."""
	result = money(to_decimal(balance) + to_decimal(amount))
	if result < 0:
		raise CreditError("Insufficient credit balance")
	return result


def credits_to_apply(balance: Any, amount_due: Any) -> Decimal:
	"""How much of `balance` to spend on an invoice with `amount_due` (never negative)."""
	balance, due = money(balance), money(amount_due)
	if balance <= 0 or due <= 0:
		return ZERO
	return min(balance, due)


def amount_due(total: Any, credits_applied: Any) -> Decimal:
	"""What is left to pay on an invoice after credits (never negative)."""
	return max(money(to_decimal(total) - to_decimal(credits_applied)), ZERO)


def apply_to_invoice(total: Any, credits_applied: Any, balance: Any) -> dict[str, Any]:
	"""Plan applying `balance` to an invoice of `total` that already has `credits_applied`.

	Returns {apply, credits_applied, amount_due, paid, balance_after}: `apply` is the
	(positive) credit to spend now; `paid` is True when nothing is left to pay."""
	due_before = amount_due(total, credits_applied)
	apply = credits_to_apply(balance, due_before)
	new_applied = money(to_decimal(credits_applied) + apply)
	due_after = amount_due(total, new_applied)
	return {
		"apply": apply,
		"credits_applied": new_applied,
		"amount_due": due_after,
		"paid": due_after == 0,
		"balance_after": money(to_decimal(balance) - apply),
	}


def allocate_credits(balance: Any, invoices: Iterable[Mapping[str, Any]]) -> list[tuple[str, Decimal]]:
	"""Spread `balance` over open invoices, oldest due date first:
	[(invoice name, amount to apply)]. invoices: [{name, amount_due, due_date?, status?}]."""
	remaining = money(balance)
	result = []
	ordered = sorted(
		(i for i in invoices if (i.get("status") or "Unpaid") in PAYABLE_STATUSES),
		key=lambda i: (str(i.get("due_date") or ""), str(i.get("name") or "")),
	)
	for inv in ordered:
		if remaining <= 0:
			break
		apply = credits_to_apply(remaining, inv.get("amount_due"))
		if apply > 0:
			result.append((inv["name"], apply))
			remaining = money(remaining - apply)
	return result


def validate_topup_amount(amount: Any, minimum: Any = None, maximum: Any = None) -> Decimal:
	"""Rounded top-up amount within [minimum, maximum] (defaults 5 and 10000)."""
	try:
		value = money(amount)
	except Exception as exc:  # decimal.InvalidOperation
		raise CreditError("Enter a valid amount") from exc
	low = money(minimum) if minimum not in (None, "", 0) else DEFAULT_MIN_TOPUP
	high = money(maximum) if maximum not in (None, "", 0) else DEFAULT_MAX_TOPUP
	if value < low:
		raise CreditError(f"The minimum top-up is {low}")
	if value > high:
		raise CreditError(f"The maximum top-up is {high}")
	return value


def signup_credit_amount(configured: Any, team_already_granted: bool, owner_already_granted: bool) -> Decimal:
	"""Signup credits for a new team: the configured amount, once per team and once per
	owner (a user creating more teams does not collect them again)."""
	value = money(configured)
	if value <= 0 or team_already_granted or owner_already_granted:
		return ZERO
	return value
