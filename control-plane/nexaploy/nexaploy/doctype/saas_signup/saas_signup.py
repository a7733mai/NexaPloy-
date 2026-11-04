# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class SaaSSignup(Document):
	"""One self-service signup (nexaploy/api/signup.py). Tokens are stored as sha256 only;
	the chosen password is kept encrypted until the e-mail is verified, then cleared."""

	pass
