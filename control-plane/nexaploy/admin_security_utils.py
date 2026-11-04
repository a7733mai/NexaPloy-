"""Two-factor authentication for platform admins (frappe-free logic)."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

METHODS = ("OTP App", "Email", "SMS")
ADMIN_ROLE = "System Manager"


def summarize(enabled: Any, method: Any, role_requires: Any,
              admins: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
	"""Status shown on the admin card. `admins`: [{user, full_name, otp_configured}]."""
	admins = list(admins)
	enforced = bool(int(enabled or 0)) and bool(int(role_requires or 0))
	pending = [a["user"] for a in admins if enforced and method == "OTP App" and not a.get("otp_configured")]
	if not enforced:
		level, message = "danger", "Two-factor authentication is not required for administrators."
	elif pending:
		level, message = "warning", "Some administrators will set up their authenticator app at their next login."
	else:
		level, message = "success", "Administrators must use two-factor authentication."
	return {
		"enabled": bool(int(enabled or 0)), "method": method or "OTP App", "role_requires": bool(int(role_requires or 0)),
		"enforced": enforced, "admins": admins, "pending_setup": pending, "level": level, "message": message,
	}


def validate_method(method: Any) -> str:
	method = (method or "OTP App").strip()
	if method not in METHODS:
		raise ValueError(f"unsupported two-factor method {method!r}")
	return method


def enable_blocker(method: str, has_outgoing_email: bool, has_sms: bool) -> str | None:
	"""Why 2FA must not be switched on yet (None = OK). Frappe e-mails the authenticator QR
	code on first use (OTP App) or the code itself (Email), and texts it for SMS: without that
	channel every administrator would be locked out at the next login."""
	if method in ("OTP App", "Email") and not has_outgoing_email:
		return "Set up an outgoing e-mail account first: Frappe e-mails the authenticator QR code (or the login code) to administrators."
	if method == "SMS" and not has_sms:
		return "Set up SMS Settings first: Frappe texts the login code to administrators."
	return None
