"""Administrator area: require two-factor authentication for platform admins.

Uses Frappe's own 2FA: System Settings.enable_two_factor_auth + two_factor_method, and
Role "System Manager".two_factor_auth = 1 (Frappe asks every user holding a role with
two_factor_auth at login; Administrator holds every role). Customers' team-level 2FA keeps
working through the "Nexaploy 2FA" role (nexaploy.teams).
"""

import frappe
from frappe import _

from nexaploy import admin_security_utils as su
from nexaploy.api.admin import _admin


def _admins():
	users = frappe.get_all("Has Role", filters={"role": su.ADMIN_ROLE, "parenttype": "User"}, pluck="parent")
	users = sorted(set(users) | {"Administrator"})
	rows = []
	for user in users:
		info = frappe.db.get_value("User", user, ["enabled", "full_name"], as_dict=True)
		if not info or not info.enabled:
			continue
		rows.append({
			"user": user, "full_name": info.full_name or user,
			# Frappe keeps the TOTP secret in the user's defaults once the app was set up
			"otp_configured": bool(frappe.db.get_default(f"{user}_otpsecret")),
		})
	return rows


def _status():
	return su.summarize(
		frappe.db.get_single_value("System Settings", "enable_two_factor_auth"),
		frappe.db.get_single_value("System Settings", "two_factor_method"),
		frappe.db.get_value("Role", su.ADMIN_ROLE, "two_factor_auth"),
		_admins(),
	)


@frappe.whitelist()
def get_two_factor_status():
	_admin()
	return _status()


@frappe.whitelist(methods=["POST"])
def require_admin_two_factor(enabled=1, method="OTP App"):
	"""Turn admin 2FA on (System Settings + System Manager role) or off (role only: team 2FA
	through the "Nexaploy 2FA" role keeps the global switch on)."""
	_admin()
	enabled = int(enabled or 0)
	try:
		method = su.validate_method(method)
	except ValueError as exc:
		frappe.throw(_(str(exc)))
	if enabled:
		blocker = su.enable_blocker(
			method,
			bool(frappe.db.exists("Email Account", {"enable_outgoing": 1, "default_outgoing": 1})),
			bool(frappe.db.get_single_value("SMS Settings", "sms_gateway_url")),
		)
		if blocker:
			frappe.throw(_(blocker))
		# only these two fields: a full save fails on sites whose setup wizard never set
		# language/time_zone (mandatory in System Settings)
		frappe.db.set_single_value("System Settings", {"enable_two_factor_auth": 1, "two_factor_method": method})
	frappe.db.set_value("Role", su.ADMIN_ROLE, "two_factor_auth", enabled)
	frappe.clear_cache()
	from nexaploy.api.admin import _log

	_log("platform.admin_two_factor", "Role", su.ADMIN_ROLE, {"enabled": enabled, "method": method})
	return _status()
