# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy.billing.credits_utils import CreditError, check_transaction


class TeamCreditTransaction(Document):
	"""Append-only prepaid credit ledger row. Create rows with
	nexaploy.billing.credits.add_credits (it locks the team, computes balance_after and
	refreshes Team.credit_balance); a desk insert by a System Manager goes through the
	same validation (Adjustment rows)."""

	def validate(self):
		if not self.is_new():
			frappe.throw(_("Credit transactions cannot be changed; add an Adjustment instead"))
		try:
			self.amount = float(check_transaction(self.type, self.amount))
		except CreditError as exc:
			frappe.throw(_(str(exc)))

	def before_insert(self):
		if self.flags.nexaploy_ledger:
			return  # balance_after already computed under the team lock (add_credits)
		from nexaploy.billing.credits import prepare_transaction

		prepare_transaction(self)

	def after_insert(self):
		from nexaploy.billing.credits import refresh_team_balance

		refresh_team_balance(self.team)

	def on_trash(self):
		frappe.throw(_("Credit transactions cannot be deleted; add an Adjustment instead"))
