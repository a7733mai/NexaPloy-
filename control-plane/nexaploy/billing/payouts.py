"""Marketplace publisher payouts (database side; pure math in payouts_utils).

Scheduler (daily) `generate_publisher_payouts`: on Billing Settings.invoice_day + 5 days,
for the month before that invoice day, every line of a **Paid** Nexaploy Invoice whose
`document_type` is "Marketplace App" and that was paid (`paid_on`) in that month earns the
app's publisher `amount * revenue_share_percent / 100`. One Draft **Publisher Payout** per
(publisher, period, currency) with one **Publisher Payout Item** per invoice line.

Idempotent: an invoice line already contained in any payout is never paid twice, and a
payout that already exists for the same (publisher, period, currency) only gets lines that
were not yet included (while it is Draft; otherwise a new payout is created for the rest).

Staff approve and pay payouts in the desk (Draft -> Approved -> Paid); a Credits payout adds
`net` to the publisher team's prepaid credits when it is marked Paid (PublisherPayout
controller).
"""

import frappe
from frappe.utils import get_datetime, getdate, today

from nexaploy.billing import payouts_utils as pou

PAYOUT = "Publisher Payout"
PAYOUT_ITEM = "Publisher Payout Item"
PUBLISHER = "Marketplace Publisher"


def generate_publisher_payouts(date=None):
	"""Daily scheduler entry. Returns the names of the payouts created or extended."""
	s = frappe.get_cached_doc("Billing Settings")
	if not s.enabled:
		return []
	period = pou.payout_period(getdate(date or today()), s.invoice_day)
	if not period:
		return []
	try:
		names = generate_payouts_for_period(*period)
		frappe.db.commit()
		return names
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Nexaploy: generating publisher payouts failed")
		return []


def paid_app_lines(period_start, period_end, apps=None):
	"""Invoice lines of paid marketplace apps, paid within [period_start, period_end]."""
	start = get_datetime(f"{getdate(period_start)} 00:00:00")
	end = get_datetime(f"{getdate(period_end)} 23:59:59.999999")
	conditions = ""
	values = {"start": start, "end": end}
	if apps is not None:
		if not apps:
			return []
		conditions = "and item.document_name in %(apps)s"
		values["apps"] = tuple(apps)
	return frappe.db.sql(
		f"""
		select inv.name as invoice, inv.currency, inv.paid_on, item.name as invoice_item,
			item.document_type, item.document_name as app, item.site, item.amount
		from `tabNexaploy Invoice Item` item
		inner join `tabNexaploy Invoice` inv on inv.name = item.parent
		where item.parenttype = 'Nexaploy Invoice'
			and item.document_type = 'Marketplace App'
			and inv.status = 'Paid'
			and inv.paid_on between %(start)s and %(end)s
			{conditions}
		order by inv.paid_on asc, item.idx asc
		""",
		values,
		as_dict=True,
	)


def generate_payouts_for_period(period_start, period_end):
	period_start, period_end = getdate(period_start), getdate(period_end)
	rows = paid_app_lines(period_start, period_end)
	if not rows:
		return []
	app_publishers = dict(
		frappe.get_all(
			"Marketplace App",
			filters={"name": ["in", sorted({r.app for r in rows})], "publisher_profile": ["is", "set"]},
			fields=["name", "publisher_profile"],
			as_list=True,
		)
	)
	already = set(
		frappe.get_all(
			PAYOUT_ITEM,
			filters={"parenttype": PAYOUT, "invoice_item": ["in", [r.invoice_item for r in rows]]},
			pluck="invoice_item",
		)
	)
	created = []
	for (publisher, currency), lines in sorted(pou.group_lines(rows, app_publishers, already).items()):
		name = _upsert_payout(publisher, currency, period_start, period_end, lines)
		if name:
			created.append(name)
	return created


def _upsert_payout(publisher, currency, period_start, period_end, lines):
	pub = frappe.db.get_value(
		PUBLISHER, publisher, ["name", "team", "payout_method", "revenue_share_percent"], as_dict=True
	)
	if not pub:
		return None
	existing = frappe.db.get_value(
		PAYOUT,
		{"publisher": publisher, "period_start": period_start, "period_end": period_end, "currency": currency, "status": pou.DRAFT},
		"name",
	)
	if existing:
		doc = frappe.get_doc(PAYOUT, existing)
		percent = doc.revenue_share_percent
		lines = [
			{"invoice": i.invoice, "invoice_item": i.invoice_item, "app": i.app, "site": i.site, "amount": i.amount}
			for i in doc.items
		] + list(lines)
	else:
		percent = pub.revenue_share_percent
		doc = frappe.get_doc(
			{
				"doctype": PAYOUT,
				"publisher": publisher,
				"team": pub.team,
				"status": pou.DRAFT,
				"payout_method": pub.payout_method,
				"period_start": period_start,
				"period_end": period_end,
				"currency": currency,
				"revenue_share_percent": percent,
			}
		)
	payout = pou.build_payout(lines, percent)
	if payout["net"] <= 0 and not existing:
		return None
	doc.set("items", [])
	for item in payout["items"]:
		doc.append(
			"items",
			{
				"invoice": item["invoice"],
				"invoice_item": item["invoice_item"],
				"app": item["app"],
				"site": item["site"],
				"amount": float(item["amount"]),
				"share": float(item["share"]),
			},
		)
	doc.gross = float(payout["gross"])
	doc.net = float(payout["net"])
	doc.platform_fee = float(payout["platform_fee"])
	doc.flags.ignore_links = True  # invoices/apps may have been renamed or removed since
	doc.save(ignore_permissions=True)
	_log(pub.team, "payout.create", doc, {"net": str(payout["net"]), "currency": currency, "period_start": str(period_start)})
	return doc.name


def on_payout_status_change(doc, old_status):
	"""Called by PublisherPayout.on_update when the status changes: activity + email."""
	team = frappe.db.get_value(PUBLISHER, doc.publisher, "team")
	action = {pou.APPROVED: "payout.approve", pou.PAID: "payout.paid", pou.DRAFT: "payout.reopen"}.get(doc.status)
	if not team or not action:
		return
	_log(team, action, doc, {"net": doc.net, "currency": doc.currency, "method": doc.payout_method, "from": old_status})
	if doc.status == pou.PAID:
		_notify_paid(doc, team)


def _notify_paid(doc, team):
	try:
		from frappe import _
		from frappe.utils import get_url

		from nexaploy.teams import team_notification_emails

		recipients = team_notification_emails(team, "billing")
		if not recipients:
			return
		frappe.sendmail(
			recipients=recipients,
			subject=_("Marketplace payout {0} paid").format(doc.name),
			message=_("Your marketplace payout {0} for {1} to {2} has been paid: {3} {4} ({5}).").format(
				doc.name, doc.period_start, doc.period_end, doc.net, doc.currency or "", _(doc.payout_method or "")
			)
			+ "<br><br>"
			+ get_url("/dashboard/publisher"),
			reference_doctype=doc.doctype,
			reference_name=doc.name,
		)
	except Exception:
		frappe.log_error(title=f"Nexaploy: payout email for {doc.name} failed")


def _log(team, action, doc, details):
	try:
		from nexaploy.activity import log_activity

		log_activity(team, action, doc.doctype, doc.name, details)
	except Exception:
		pass


def payout_query(user=None, doctype=None):
	"""permission_query_conditions for Publisher Payout: rows of the user's teams."""
	from nexaploy.permissions import team_query

	return team_query(user, PAYOUT)
