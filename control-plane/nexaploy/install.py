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


def after_migrate():
	create_customer_role()
	create_default_plans()
	create_billing_settings()


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
