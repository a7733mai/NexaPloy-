"""Keep Nexaploy Subscription rows in step with Servers, Sites and paid Site Apps.

Wired from hooks.doc_events (Server/Site after_insert + on_update) and re-run daily by
`reconcile_subscriptions` (catches status changes written with db_set, e.g. by agent
callbacks or Server.archive, which do not fire doc events). Everything is idempotent: a
subscription is keyed by (document_type, document_name, site) and only one Active row per
key exists.

Changes take effect from today: a cancelled subscription ends yesterday (or is dropped if
it started today), a new one starts today, so proration never double-bills a day.
"""

import frappe
from frappe.utils import add_days, getdate, today

from nexaploy.billing import pricing

SUBSCRIPTION = "Nexaploy Subscription"


# --------------------------------------------------------------------------- doc events


def sync_subscription(doc, method=None):
	"""doc_events handler for Server and Site (after_insert / on_update)."""
	if not _billing_enabled():
		return
	if doc.doctype == "Server":
		sync_server(doc)
	elif doc.doctype == "Site":
		sync_site(doc)


def cancel_document_subscriptions(doc, method=None):
	"""doc_events on_trash handler: stop billing a deleted Server/Site."""
	cancel_subscriptions(doc.doctype, doc.name)
	if doc.doctype == "Site":
		cancel_site_app_subscriptions(doc.name)


# --------------------------------------------------------------------------- servers & sites


def sync_server(doc):
	# a provisioned Hosted server is billed once its VM exists (Bootstrapping) until Deleted
	from nexaploy.provisioning.utils import is_billable

	if (doc.status == "Archived" or not doc.team or not is_billable(doc.get("provisioning_status"))
			or _billing_exempt(doc.team)):
		cancel_subscriptions("Server", doc.name)
		return
	settings = _settings()
	plan = pricing.server_plan(doc.ownership, doc.plan, settings.default_byos_plan)
	ensure_plan_subscription(doc.team, "Server", doc.name, plan)


def sync_site(doc):
	# staging sites and billing-exempt (platform) teams are free
	if doc.status == "Archived" or not doc.team or doc.get("is_staging") or _billing_exempt(doc.team):
		cancel_subscriptions("Site", doc.name)
		cancel_site_app_subscriptions(doc.name)
		return
	settings = _settings()
	start = _site_billing_start(doc)
	ensure_plan_subscription(
		doc.team, "Site", doc.name, pricing.site_plan(doc.plan, settings.default_site_plan), start_date=start
	)
	sync_site_apps(doc, start_date=start)


def _site_billing_start(doc):
	"""SaaS trials: a site is billed from its trial_end_date (signup_utils.billing_start_date)."""
	from nexaploy.signup_utils import billing_start_date

	return billing_start_date(today(), doc.get("trial_end_date"))


def sync_site_apps(doc, start_date=None):
	"""One Active subscription per paid Marketplace App installed (or installing) on the site."""
	rows = [{"app": r.app, "status": r.status} for r in (doc.get("apps") or [])]
	apps = [r["app"] for r in rows if r["app"]]
	catalog = {}
	if apps:
		for app in frappe.get_all(
			"Marketplace App",
			filters={"name": ["in", apps]},
			fields=["name", "pricing", "price_monthly"],
		):
			catalog[app.name] = app
	desired = pricing.paid_app_prices(rows, catalog)
	existing = {s.document_name: s for s in _active("Marketplace App", site=doc.name)}
	to_create, to_cancel = pricing.diff_subscriptions(existing, desired)
	for app in to_cancel:
		_end(existing[app])
	for app in to_create:
		_create(doc.team, "Marketplace App", app, desired[app], site=doc.name, start_date=start_date)


def ensure_plan_subscription(team, document_type, document_name, plan, start_date=None):
	"""Exactly one Active subscription on `plan` (none when `plan` is falsy).
	`start_date` (default today) may lie in the future (trial); a kept subscription that has
	not started yet follows a changed start date."""
	current = _active(document_type, document_name)
	if not plan:
		for sub in current:
			_end(sub)
		return None
	keep = None
	for sub in current:
		if keep is None and sub.plan == plan and sub.team == team:
			keep = sub
		else:
			_end(sub)
	if keep:
		if start_date and getdate(keep.start_date) > getdate(today()) and getdate(keep.start_date) != getdate(start_date):
			frappe.db.set_value(SUBSCRIPTION, keep.name, "start_date", start_date)
		return keep.name
	price, currency = frappe.db.get_value("Nexaploy Plan", plan, ["price_monthly", "currency"]) or (0, None)
	return _create(
		team, document_type, document_name, price, plan=plan, currency=currency, start_date=start_date
	)


def cancel_subscriptions(document_type, document_name):
	for sub in _active(document_type, document_name):
		_end(sub)


def cancel_site_app_subscriptions(site):
	for sub in _active("Marketplace App", site=site):
		_end(sub)


# --------------------------------------------------------------------------- reconcile


def reconcile_subscriptions():
	"""Re-sync every Server and Site, and close subscriptions whose document is gone.
	Scheduler (daily) and the first step of generate_monthly_invoices."""
	if not _billing_enabled():
		return
	for name in frappe.get_all("Server", pluck="name"):
		sync_server(frappe.get_doc("Server", name))
	for name in frappe.get_all("Site", pluck="name"):
		sync_site(frappe.get_doc("Site", name))
	for sub in frappe.get_all(
		SUBSCRIPTION,
		filters={"status": "Active"},
		fields=["name", "document_type", "document_name", "start_date", "site"],
	):
		gone = not frappe.db.exists(sub.document_type, sub.document_name)
		if sub.site and not frappe.db.exists("Site", sub.site):
			gone = True
		if gone:
			_end(sub)


# --------------------------------------------------------------------------- helpers


def _billing_exempt(team):
	return bool(frappe.db.get_value("Team", team, "billing_exempt"))


def _billing_enabled():
	return bool(frappe.db.get_single_value("Billing Settings", "enabled"))


def _settings():
	return frappe.get_cached_doc("Billing Settings")


def _active(document_type, document_name=None, site=None):
	filters = {"document_type": document_type, "status": "Active"}
	if document_name:
		filters["document_name"] = document_name
	if site:
		filters["site"] = site
	return frappe.get_all(
		SUBSCRIPTION,
		filters=filters,
		fields=["name", "team", "plan", "document_name", "start_date"],
		order_by="creation asc",
	)


def _create(team, document_type, document_name, price, plan=None, site=None, currency=None, start_date=None):
	doc = frappe.get_doc(
		{
			"doctype": SUBSCRIPTION,
			"team": team,
			"document_type": document_type,
			"document_name": document_name,
			"site": site,
			"plan": plan,
			"price_monthly": price or 0,
			"currency": currency or frappe.db.get_single_value("Billing Settings", "currency") or "USD",
			"status": "Active",
			"start_date": start_date or today(),
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _end(sub):
	"""Stop billing from today. A subscription that started today never gets billed."""
	start = getdate(sub.start_date)
	if start >= getdate(today()):
		frappe.delete_doc(SUBSCRIPTION, sub.name, ignore_permissions=True, force=True)
		return
	frappe.db.set_value(
		SUBSCRIPTION, sub.name, {"status": "Cancelled", "end_date": add_days(today(), -1)}
	)
