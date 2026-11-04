"""Control plane self-update and first-run setup checklist (/dashboard/admin, platform admins only).

| Method | |
|---|---|
| get_status(force) | version + image vs latest release (GitHub, cached 1 h), update mode, last update job |
| start_update(image, version) | agent job `controlplane.update` on the host server (all-in-one only) |
| get_update(name) | progress of one update job (polled by the page; fails while the control plane restarts) |
| get_setup_checklist() / dismiss_setup(dismissed) | first-run checklist (computed from settings) |
"""

import frappe
from frappe.utils import cint

from nexaploy import controlplane as cp
from nexaploy.api.admin import _admin


@frappe.whitelist()
def get_status(force=0):
	_admin()
	return cp.status(force=bool(cint(force)))


@frappe.whitelist(methods=["POST"])
def start_update(image=None, version=None):
	_admin()
	return cp.start_update(image=image or None, version=version or None)


@frappe.whitelist()
def get_update(name):
	_admin()
	if frappe.db.get_value("Agent Job", name, "kind") != "controlplane.update":
		raise frappe.DoesNotExistError(name)
	return cp.job_progress(name)


@frappe.whitelist()
def get_setup_checklist():
	_admin()
	return cp.setup_checklist()


@frappe.whitelist(methods=["POST"])
def dismiss_setup(dismissed=1):
	_admin()
	result = cp.dismiss_setup(cint(dismissed))
	from nexaploy.activity import log_platform_activity

	log_platform_activity("platform.setup_checklist", "Nexaploy Settings", "Nexaploy Settings",
	                      {"dismissed": cint(dismissed)})
	return result
