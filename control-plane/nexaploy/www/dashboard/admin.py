"""/dashboard/admin - the platform Administrator area (overview, features & pricing, storage,
e-mail & brands, agent releases, marketplace review, teams & servers). Platform admins only;
every action goes through nexaploy.api.admin, which checks is_platform_admin again."""

import frappe
from frappe import _

from nexaploy.api.dashboard import prepare_page

no_cache = 1


def get_context(context):
	from nexaploy.teams import is_platform_admin

	prepare_page(context, "admin", _("Administration"))
	if not is_platform_admin():
		raise frappe.PermissionError(_("Only platform administrators can do this"))
	return context
