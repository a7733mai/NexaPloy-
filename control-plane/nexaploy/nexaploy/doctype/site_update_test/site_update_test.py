# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class SiteUpdateTest(Document):
	"""A "Safe update" run for one site (nexaploy.update_test, ARCHITECTURE.md §13): clone the
	site onto a bench with the new image, migrate, smoke check, then optionally apply (move the
	production site, fresh backup first). Created and advanced by the control plane only."""
