"""Pure billing math: proration, invoice items, totals, billing calendar.

This module MUST NOT import frappe: it is unit-tested with plain pytest
(control-plane/tests/test_billing.py). Money is `Decimal`, rounded to 2 decimal places
(ROUND_HALF_UP) per invoice line and per total.

Pricing rules (ARCHITECTURE.md §8):
  * BYOS server   -> its plan, or the default BYOS plan ($10/month)
  * Hosted server -> its plan (no plan, no charge)
  * Site          -> its plan, or the default Site plan (Site Basic)
  * Paid Marketplace App installed on a site -> the app's price_monthly, per site
Every charge is prorated by the days it was active in the invoiced period.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable, Mapping, Sequence

CENT = Decimal("0.01")
SUBSCRIBABLE = ("Server", "Site", "Marketplace App", "Nexaploy Plan")  # Nexaploy Plan = per-site add-on
# Paused subscriptions are not billed; Cancelled ones are billed up to their end_date.
BILLABLE_STATUSES = ("Active", "Cancelled")


# --------------------------------------------------------------------------- primitives


def to_decimal(value: Any) -> Decimal:
	if value is None or value == "":
		return Decimal("0")
	if isinstance(value, Decimal):
		return value
	if isinstance(value, float):
		# go through repr so 0.1 stays 0.1 instead of 0.1000000000000000055...
		return Decimal(repr(value))
	return Decimal(str(value))


def money(value: Any) -> Decimal:
	return to_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def as_date(value: Any) -> date | None:
	if value is None or value == "":
		return None
	if isinstance(value, datetime):
		return value.date()
	if isinstance(value, date):
		return value
	return date.fromisoformat(str(value)[:10])


def days_inclusive(start: Any, end: Any) -> int:
	start, end = as_date(start), as_date(end)
	if start is None or end is None or end < start:
		return 0
	return (end - start).days + 1


def active_days(start: Any, end: Any, period_start: Any, period_end: Any) -> int:
	"""Days (inclusive) that [start, end] overlaps [period_start, period_end].
	`end` None means still running."""
	period_start, period_end = as_date(period_start), as_date(period_end)
	start = as_date(start) or period_start
	end = as_date(end) or period_end
	return days_inclusive(max(start, period_start), min(end, period_end))


def prorate(amount: Any, start: Any, end: Any, period_start: Any, period_end: Any) -> Decimal:
	"""`amount` (a monthly price) scaled by the share of the period it was active, 2dp."""
	total = days_inclusive(period_start, period_end)
	if total == 0:
		return money(0)
	used = active_days(start, end, period_start, period_end)
	return money(to_decimal(amount) * used / total)


# --------------------------------------------------------------------------- invoices


def describe(sub: Mapping[str, Any]) -> str:
	dt, dn = sub.get("document_type"), sub.get("document_name")
	plan = sub.get("plan")
	if dt == "Marketplace App":
		text = f"App {dn}"
		if sub.get("site"):
			text += f" on {sub['site']}"
		return text
	if dt == "Nexaploy Plan":  # add-on (e.g. offsite attachments) on a site
		return f"Add-on {dn} on {sub['site']}" if sub.get("site") else f"Add-on {dn}"
	text = f"{dt} {dn}"
	return f"{text} ({plan})" if plan else text


def build_invoice_items(
	subscriptions: Iterable[Mapping[str, Any]], period: Sequence[Any]
) -> list[dict[str, Any]]:
	"""Invoice lines for `subscriptions` over `period` = (period_start, period_end).

	Each subscription is a mapping with document_type, document_name, price_monthly,
	start_date, end_date (None = open), status and optionally name, plan, site, description.
	Paused, free and not-active-in-period subscriptions produce no line. Output is sorted
	(document_type, site, document_name) so invoices are deterministic.
	"""
	period_start, period_end = as_date(period[0]), as_date(period[1])
	total_days = days_inclusive(period_start, period_end)
	items = []
	for sub in subscriptions:
		if (sub.get("status") or "Active") not in BILLABLE_STATUSES:
			continue
		price = money(sub.get("price_monthly"))
		if price <= 0:
			continue
		used = active_days(sub.get("start_date"), sub.get("end_date"), period_start, period_end)
		if used <= 0:
			continue
		amount = prorate(price, sub.get("start_date"), sub.get("end_date"), period_start, period_end)
		description = sub.get("description") or describe(sub)
		if used < total_days:
			description += f" - {used}/{total_days} days"
		items.append(
			{
				"description": description,
				"document_type": sub.get("document_type"),
				"document_name": sub.get("document_name"),
				"site": sub.get("site"),
				"subscription": sub.get("name"),
				"quantity_days": used,
				"unit_price": price,
				"amount": amount,
			}
		)
	order = {dt: i for i, dt in enumerate(SUBSCRIBABLE)}
	items.sort(
		key=lambda i: (order.get(i["document_type"], 99), i.get("site") or "", str(i["document_name"]))
	)
	return items


def compute_totals(items: Iterable[Mapping[str, Any]], tax_percent: Any = 0) -> dict[str, Decimal]:
	subtotal = money(sum((money(i.get("amount")) for i in items), Decimal("0")))
	tax = money(subtotal * to_decimal(tax_percent) / 100)
	return {"subtotal": subtotal, "tax": tax, "total": money(subtotal + tax)}


# --------------------------------------------------------------------------- pricing rules


def server_plan(ownership: str | None, plan: str | None, default_byos_plan: str | None) -> str | None:
	"""Plan a server is billed on: its own plan; BYOS falls back to the default BYOS plan;
	a Hosted server without a plan is not billed."""
	if plan:
		return plan
	if ownership == "BYOS":
		return default_byos_plan or None
	return None


def site_plan(plan: str | None, default_site_plan: str | None) -> str | None:
	return plan or default_site_plan or None


def paid_app_prices(
	app_rows: Iterable[Mapping[str, Any]], catalog: Mapping[str, Mapping[str, Any]]
) -> dict[str, Decimal]:
	"""{app: monthly price} for the paid apps among a site's `app_rows` ({app, status}).
	Failed installs are not billed. `catalog` maps app_name -> {pricing, price_monthly}."""
	out = {}
	for row in app_rows:
		app = row.get("app")
		if not app or row.get("status") == "Failed":
			continue
		info = catalog.get(app) or {}
		price = money(info.get("price_monthly"))
		if info.get("pricing") == "Paid" and price > 0:
			out[app] = price
	return out


def diff_subscriptions(
	existing: Mapping[str, Any], desired: Mapping[str, Any]
) -> tuple[list[str], list[str]]:
	"""(keys to create, keys to cancel) to go from `existing` active subscriptions to `desired`."""
	create = sorted(k for k in desired if k not in existing)
	cancel = sorted(k for k in existing if k not in desired)
	return create, cancel


# --------------------------------------------------------------------------- calendar


def previous_month(today: Any) -> tuple[date, date]:
	"""(first day, last day) of the month before `today`."""
	first_this = as_date(today).replace(day=1)
	last_prev = first_this - timedelta(days=1)
	return last_prev.replace(day=1), last_prev


def is_invoice_day(today: Any, invoice_day: Any) -> bool:
	"""True on `invoice_day` of the month; days past the month's end mean its last day."""
	today = as_date(today)
	last = calendar.monthrange(today.year, today.month)[1]
	day = int(invoice_day or 1)
	day = min(max(day, 1), last)
	return today.day == day


def due_date(issue_date: Any, grace_days: Any) -> date:
	return as_date(issue_date) + timedelta(days=int(grace_days or 0))


def days_overdue(due: Any, today: Any) -> int:
	due = as_date(due)
	if due is None:
		return 0
	return max((as_date(today) - due).days, 0)


def should_suspend(due: Any, today: Any, suspend_after_days: Any) -> bool:
	return days_overdue(due, today) > int(suspend_after_days or 0)


# --------------------------------------------------------------------------- currency


ZERO_DECIMAL_CURRENCIES = frozenset(
	"BIF CLP DJF GNF JPY KMF KRW MGA PYG RWF UGX VND VUV XAF XOF XPF".split()
)


def to_minor_units(amount: Any, currency: str = "USD", rate: Any = 1) -> int:
	"""Integer amount in the currency's minor unit (cents/piasters), after `rate` conversion."""
	value = to_decimal(amount) * to_decimal(rate or 1)
	if (currency or "").upper() in ZERO_DECIMAL_CURRENCIES:
		return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
	return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_minor_units(amount: Any, currency: str = "USD") -> Decimal:
	if (currency or "").upper() in ZERO_DECIMAL_CURRENCIES:
		return money(amount)
	return money(to_decimal(amount) / 100)
