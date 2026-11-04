"""after_install / after_migrate: role + default plans (idempotent)."""

import frappe

CUSTOMER_ROLE = "Nexaploy Customer"

DEFAULT_PLANS = [
	{
		"title": "BYOS Server",
		"plan_type": "Server BYOS",
		"price_monthly": 10,
		"currency": "USD",
		"max_sites": 0,
		"description": "Management fee for a Bring-Your-Own-Server host. You pay your VM provider.",
	},
	{
		"title": "Hosted Small",
		"plan_type": "Server Hosted",
		"price_monthly": 40,
		"currency": "USD",
		"max_sites": 10,
		"description": "Dedicated server on Nexaploy infrastructure (2 vCPU / 4 GB).",
	},
	{
		"title": "Site Basic",
		"plan_type": "Site",
		"price_monthly": 5,
		"currency": "USD",
		"max_sites": 1,
		"description": "One site on a shared Nexaploy bench.",
	},
	# per-site storage add-ons (ARCHITECTURE.md §15), priced by the operator
	{
		"title": "Offsite Attachments",
		"plan_type": "Add-on",
		"price_monthly": 5,
		"currency": "USD",
		"max_sites": 0,
		"description": "Site attachments on S3-compatible object storage instead of the server's disk.",
	},
	{
		"title": "DR Replica",
		"plan_type": "Add-on",
		"price_monthly": 3,
		"currency": "USD",
		"max_sites": 0,
		"description": "A replica of the site's attachments in another region (disaster recovery).",
	},
	{
		"title": "Trash Bin",
		"plan_type": "Add-on",
		"price_monthly": 2,
		"currency": "USD",
		"max_sites": 0,
		"description": "Deleted attachments are kept in a separate trash bucket for a retention period.",
	},
]


BILLING_DEFAULTS = {
	"enabled": 1,
	"currency": "USD",
	"invoice_day": 1,
	"grace_days": 7,
	"suspend_after_days": 14,
	"tax_percent": 0,
	"default_byos_plan": "BYOS Server",
	"default_site_plan": "Site Basic",
	"paymob_currency": "EGP",
	"paymob_exchange_rate": 1,
	"paymob_base_url": "https://accept.paymob.com",
}


def after_install():
	create_customer_role()
	create_default_plans()
	create_billing_settings()
	create_storage_settings()


def after_migrate():
	create_customer_role()
	create_default_plans()
	create_billing_settings()
	create_storage_settings()


def create_customer_role():
	if frappe.db.exists("Role", CUSTOMER_ROLE):
		return
	frappe.get_doc(
		{"doctype": "Role", "role_name": CUSTOMER_ROLE, "desk_access": 0, "is_custom": 0}
	).insert(ignore_permissions=True)


def create_default_plans():
	for plan in DEFAULT_PLANS:
		if frappe.db.exists("Nexaploy Plan", plan["title"]):
			continue
		doc = frappe.get_doc({"doctype": "Nexaploy Plan", "enabled": 1, **plan})
		if not frappe.db.exists("Currency", plan["currency"]):
			# A bare frappe site may not have currencies until the setup wizard ran;
			# never fail the install over it.
			doc.currency = None
		doc.insert(ignore_permissions=True)


def create_billing_settings():
	"""Fill Billing Settings once (a Single never saved has no values, not even defaults).
	Skipped once a currency is stored, so admin changes survive migrations."""
	if frappe.db.get_single_value("Billing Settings", "currency"):
		return
	doc = frappe.get_single("Billing Settings")
	doc.update(BILLING_DEFAULTS)
	# Currencies may not exist before the setup wizard ran; never fail the install over it.
	doc.flags.ignore_links = True
	doc.flags.ignore_mandatory = True
	doc.save(ignore_permissions=True)


# --------------------------------------------------------------------------- support & publishers
# (appended: wraps the hooks above so hooks.py does not change)

_base_after_install = after_install
_base_after_migrate = after_migrate


def create_support_role():
	"""Role "Nexaploy Support" (desk access): staff who answer Support Tickets."""
	try:
		from nexaploy.support import ensure_support_role

		ensure_support_role()
	except Exception:
		frappe.log_error(title="Nexaploy: creating the support role failed")


def after_install():  # noqa: F811
	_base_after_install()
	create_support_role()


def after_migrate():  # noqa: F811
	_base_after_migrate()
	create_support_role()


# --------------------------------------------------------------------------- official marketplace catalog
# (appended: wraps after_install so hooks.py does not change; existing sites get it from patch
# v0_4.seed_official_marketplace_apps - not after_migrate, so apps an admin deleted stay deleted)

_catalog_base_after_install = after_install


def after_install():  # noqa: F811
	_catalog_base_after_install()
	from nexaploy.marketplace_seed import safe_seed

	safe_seed()


def create_storage_settings():
	"""Link the default add-on plans in Storage Settings (features stay off until enabled)."""
	from nexaploy.storage_utils import ADDON_DEFAULT_PLANS, ADDON_FIELDS

	settings = frappe.get_single("Storage Settings")
	changed = False
	for key, field in ADDON_FIELDS.items():
		plan = ADDON_DEFAULT_PLANS[key]
		if not settings.get(field) and frappe.db.exists("Nexaploy Plan", plan):
			settings.set(field, plan)
			changed = True
	if changed:
		settings.flags.ignore_permissions = True
		settings.save()
