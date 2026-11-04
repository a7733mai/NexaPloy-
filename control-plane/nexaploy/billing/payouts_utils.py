"""Pure publisher payout math (no frappe import; tested in tests/test_publisher.py).

Revenue of paid Marketplace Apps is shared with their publisher: for every line of a
**Paid** invoice whose `document_type` is "Marketplace App" and that was paid in the payout
period, the publisher earns `amount * revenue_share_percent / 100` (pre-tax line amount).

Rounding: each line's share is rounded to cents (ROUND_HALF_UP); `net` is the sum of the
rounded shares and `platform_fee = gross - net`, so gross == net + platform_fee exactly.

Status flow of a Publisher Payout: Draft -> Approved -> Paid (Approved -> Draft undoes an
approval). Paying a payout whose method is Credits adds `net` to the publisher team's
prepaid credits (nexaploy.billing.credits.add_credits).
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from nexaploy.billing.pricing import as_date, money, previous_month, to_decimal

PAYOUT_DELAY_DAYS = 5  # payouts are generated on Billing Settings.invoice_day + 5
DRAFT, APPROVED, PAID = "Draft", "Approved", "Paid"
PAYOUT_STATUSES = (DRAFT, APPROVED, PAID)
PAYOUT_TRANSITIONS = {
	DRAFT: (APPROVED,),
	APPROVED: (PAID, DRAFT),
	PAID: (),
}
APP_DOCTYPE = "Marketplace App"


class PayoutError(ValueError):
	pass


# --------------------------------------------------------------------------- calendar


def _invoice_date(year: int, month: int, invoice_day: Any) -> date:
	last = calendar.monthrange(year, month)[1]
	day = min(max(int(invoice_day or 1), 1), last)
	return date(year, month, day)


def payout_period(today: Any, invoice_day: Any, delay: int = PAYOUT_DELAY_DAYS) -> tuple[date, date] | None:
	"""(period_start, period_end) when `today` is a payout day, else None.

	The payout day is `delay` days after the monthly invoice day (which may fall in the next
	month, e.g. invoice day 28 -> the 2nd/3rd). The period is the month before that invoice
	day: invoice day 1 of March -> payouts on March 6 for February."""
	today = as_date(today)
	for months_back in (0, 1):
		year, month = today.year, today.month - months_back
		if month == 0:
			year, month = year - 1, 12
		inv = _invoice_date(year, month, invoice_day)
		if inv + timedelta(days=int(delay)) == today:
			return previous_month(inv)
	return None


def is_payout_day(today: Any, invoice_day: Any, delay: int = PAYOUT_DELAY_DAYS) -> bool:
	return payout_period(today, invoice_day, delay) is not None


# --------------------------------------------------------------------------- math


def publisher_share(amount: Any, percent: Any) -> Decimal:
	"""Publisher's part of one invoice line, 2 dp ROUND_HALF_UP."""
	pct = to_decimal(percent)
	if pct < 0 or pct > 100:
		raise PayoutError("Revenue share must be between 0 and 100 percent")
	return money(to_decimal(amount) * pct / 100)


def build_payout(lines: Iterable[Mapping[str, Any]], percent: Any) -> dict[str, Any]:
	"""Payout totals and items for one publisher and currency.

	lines: [{invoice, invoice_item, app, site, amount}] -> {items: [{..., amount, share}],
	gross, net, platform_fee}. Lines with a non-positive amount are skipped. Items are
	sorted (invoice, app, site) so payouts are deterministic."""
	items = []
	for line in lines or []:
		amount = money(line.get("amount"))
		if amount <= 0:
			continue
		items.append(
			{
				"invoice": line.get("invoice"),
				"invoice_item": line.get("invoice_item"),
				"app": line.get("app"),
				"site": line.get("site"),
				"amount": amount,
				"share": publisher_share(amount, percent),
			}
		)
	items.sort(key=lambda i: (str(i["invoice"] or ""), str(i["app"] or ""), str(i["site"] or "")))
	gross = money(sum((i["amount"] for i in items), Decimal("0")))
	net = money(sum((i["share"] for i in items), Decimal("0")))
	return {"items": items, "gross": gross, "net": net, "platform_fee": money(gross - net)}


def group_lines(
	rows: Iterable[Mapping[str, Any]],
	app_publishers: Mapping[str, str | None],
	already_paid: Iterable[str] = (),
) -> dict[tuple[str, str], list[dict[str, Any]]]:
	"""{(publisher, currency): [line]} from invoice item rows.

	rows: [{invoice, invoice_item, document_type, app (= document_name), site, amount,
	currency}]. Skips other document types, apps without a publisher, non-positive amounts
	and invoice items already contained in a payout (`already_paid`, invoice item names)."""
	done = set(already_paid or ())
	out: dict[tuple[str, str], list[dict[str, Any]]] = {}
	for row in rows or []:
		if (row.get("document_type") or APP_DOCTYPE) != APP_DOCTYPE:
			continue
		app = row.get("app") or row.get("document_name")
		publisher = app_publishers.get(app)
		if not publisher:
			continue
		if row.get("invoice_item") and row.get("invoice_item") in done:
			continue
		if money(row.get("amount")) <= 0:
			continue
		currency = (row.get("currency") or "USD").upper()
		out.setdefault((publisher, currency), []).append(
			{
				"invoice": row.get("invoice"),
				"invoice_item": row.get("invoice_item"),
				"app": app,
				"site": row.get("site"),
				"amount": row.get("amount"),
			}
		)
	return out


def revenue_by_month(rows: Iterable[Mapping[str, Any]], percent: Any) -> list[dict[str, Any]]:
	"""[{month: 'YYYY-MM', currency, gross, share, sales}] newest first, from paid invoice
	item rows ({paid_on, amount, currency}); `share` is the publisher's estimated part."""
	acc: dict[tuple[str, str], dict[str, Any]] = {}
	for row in rows or []:
		paid_on = as_date(row.get("paid_on"))
		if paid_on is None:
			continue
		key = (paid_on.strftime("%Y-%m"), (row.get("currency") or "USD").upper())
		bucket = acc.setdefault(key, {"month": key[0], "currency": key[1], "gross": Decimal("0"), "share": Decimal("0"), "sales": 0})
		amount = money(row.get("amount"))
		bucket["gross"] += amount
		bucket["share"] += publisher_share(amount, percent)
		bucket["sales"] += 1
	result = []
	for key in sorted(acc, reverse=True):
		b = acc[key]
		b["gross"], b["share"] = money(b["gross"]), money(b["share"])
		result.append(b)
	return result


# --------------------------------------------------------------------------- status


def check_payout_transition(old: str | None, new: str | None) -> str:
	"""Validate a status change; returns the new status. Same status is always allowed."""
	old = old or DRAFT
	new = new or DRAFT
	if new not in PAYOUT_STATUSES:
		raise PayoutError(f"Unknown payout status {new}")
	if old == new:
		return new
	if new not in PAYOUT_TRANSITIONS.get(old, ()):
		raise PayoutError(f"A payout cannot go from {old} to {new}")
	return new


def credits_due(payout_method: str | None, old: str | None, new: str | None, already_credited: bool = False) -> bool:
	"""True when this status change must add the payout to the publisher's credits."""
	return payout_method == "Credits" and old == APPROVED and new == PAID and not already_credited
