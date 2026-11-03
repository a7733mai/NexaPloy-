# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_to_date, get_url, now_datetime

from nexaploy.utils import generate_token, hash_token, install_command

DEFAULT_TTL_HOURS = 24


class ServerRegistrationToken(Document):
	def validate(self):
		if not self.token_hash:
			frappe.throw(_("Use generate_registration_token() to create registration tokens"))


@frappe.whitelist()
def generate_registration_token(team=None, expires_in_hours=DEFAULT_TTL_HOURS):
	"""Create a single-use BYOS registration token.

	Returns the plaintext token (shown once; only its sha256 is stored) and the install
	one-liner. install.sh is agent/install/install.sh, served by the web server at /install.sh.
	"""
	user = frappe.session.user
	roles = frappe.get_roles(user)
	if "System Manager" not in roles:
		if "Nexaploy Customer" not in roles:
			raise frappe.PermissionError(_("Not allowed"))
		# customers can only create tokens for themselves
		team = user
	team = team or user

	expires_in_hours = min(max(int(expires_in_hours or DEFAULT_TTL_HOURS), 1), 24 * 7)
	token = generate_token()
	doc = frappe.get_doc(
		{
			"doctype": "Server Registration Token",
			"token_hash": hash_token(token),
			"token_prefix": token[:6],
			"team": team,
			"expires_on": add_to_date(now_datetime(), hours=expires_in_hours),
			"used": 0,
		}
	)
	doc.insert(ignore_permissions=True)
	host = get_url()
	return {
		"name": doc.name,
		"token": token,
		"expires_on": str(doc.expires_on),
		"install_command": install_command(host, token),
	}
