"""Seed the official app catalog (erpnext, hrms, crm, helpdesk, ...) on existing sites.
Fresh installs get it from nexaploy.install.after_install. Idempotent."""


def execute():
	from nexaploy.marketplace_seed import safe_seed

	safe_seed()
