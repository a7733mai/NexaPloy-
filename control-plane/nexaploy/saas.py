"""SaaS products: one-click signup sites and trials (frappe glue; pure rules in signup_utils).

    pick_bench(product)        least loaded shared bench of the product's release group
    provision_signup(signup)   User + Team + Site for a verified SaaS Signup
    refresh_signup(signup)     Provisioning -> Active / Failed from the Site
    expire_trials()            daily: trial over -> e-mail, +3 days -> suspend (unless the team can pay)
"""

import frappe
from frappe import _
from frappe.utils import get_url, getdate, now_datetime, today

from nexaploy import signup_utils as su
from nexaploy.portal_utils import normalize_root_domain, parse_app_list

PRODUCT = "SaaS Product"
SIGNUP = "SaaS Signup"
CUSTOMER_ROLE = "Nexaploy Customer"
PAYING_GATEWAYS = ("Stripe", "Paymob", "Manual")
WRITE_ROLES = ("Owner", "Admin", "Developer")


# --------------------------------------------------------------------------- products


def get_product(slug):
	"""Enabled SaaS Product by slug (frappe._dict) or None."""
	try:
		slug = su.normalize_slug(slug)
	except ValueError:
		return None
	row = frappe.db.get_value(
		PRODUCT,
		{"slug": slug, "enabled": 1},
		["name", "title", "slug", "description", "release_group", "bench_selection", "apps",
		 "default_plan", "trial_days", "domain"],
		as_dict=True,
	)
	if row:
		row.root_domain = product_root_domain(row)
	return row


def product_root_domain(product):
	if product.get("domain"):
		return normalize_root_domain(product.domain)
	from nexaploy.api.dashboard import _root_domain

	return _root_domain()


def product_apps(product):
	try:
		return parse_app_list((product.apps or "").replace("\n", ","))
	except ValueError:
		return []


def site_name_taken(site_name, email=None):
	"""Site / domain exists, or another visitor's signup (not `email`) reserved it."""
	if frappe.db.exists("Site", site_name) or frappe.db.exists(
		"Site Domain", {"domain": site_name, "parenttype": "Site"}
	):
		return True
	filters = {
		"site_name": site_name,
		"status": ["in", [su.PENDING_VERIFICATION, su.VERIFIED, su.PROVISIONING]],
		"expires_on": [">", now_datetime()],
	}
	if email:
		filters["email"] = ["!=", email]
	return bool(frappe.db.exists(SIGNUP, filters))


def pick_bench(product):
	"""Shared bench for a new site: Active, no team, on an Active Hosted server without a team."""
	benches = frappe.get_all(
		"Bench",
		filters={"release_group": product.release_group, "status": "Active"},
		fields=["name", "server", "team", "status"],
	)
	benches = [b for b in benches if not b.team]
	if not benches:
		return None
	servers = {
		s.name: s
		for s in frappe.get_all(
			"Server",
			filters={"name": ["in", list({b.server for b in benches})]},
			fields=["name", "status", "ownership", "team"],
		)
	}
	usable = []
	for b in benches:
		s = servers.get(b.server)
		if not s or s.team or s.ownership != "Hosted":
			continue
		b.server_status = s.status
		usable.append(b)
	counts = {}
	if usable:
		for row in frappe.get_all(
			"Site",
			filters={"bench": ["in", [b.name for b in usable]], "status": ["!=", "Archived"]},
			fields=["bench"],
		):
			counts[row.bench] = counts.get(row.bench, 0) + 1
	return su.select_bench(usable, counts, product.bench_selection or "Least Sites")


# --------------------------------------------------------------------------- users & teams


def create_user(email, full_name, password):
	first, _sep, last = (full_name or email.split("@")[0]).strip().partition(" ")
	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": first[:140] or email,
			"last_name": last[:140] or None,
			"enabled": 1,
			"user_type": "Website User",
			"send_welcome_email": 0,
		}
	)
	user.flags.ignore_permissions = True
	user.flags.ignore_password_policy = True
	user.new_password = password
	user.insert(ignore_permissions=True)
	user.add_roles(CUSTOMER_ROLE)
	return user.name


def team_for_user(user, company=None):
	"""A team the user may create sites in; a new team named after the company otherwise."""
	from nexaploy.teams import create_team, ensure_personal_team, get_user_teams

	for team in get_user_teams(user):
		if team.get("role") in WRITE_ROLES:
			return team["name"]
	if company:
		return create_team(company, owner=user).name
	return ensure_personal_team(user)


# --------------------------------------------------------------------------- signup -> site


def provision_signup(signup):
	"""Verified SaaS Signup -> User (if new) + Team + Site on the least loaded bench.
	Sets the signup to Provisioning (or Failed). Returns the signup doc."""
	doc = frappe.get_doc(SIGNUP, signup) if isinstance(signup, str) else signup
	product = frappe.db.get_value(
		PRODUCT, doc.product,
		["name", "title", "slug", "release_group", "bench_selection", "apps", "default_plan", "trial_days", "domain"],
		as_dict=True,
	)
	user = frappe.db.get_value("User", {"email": doc.email}, "name")
	if not user:
		user = create_user(doc.email, doc.full_name, doc.get_password("password", raise_exception=False))
	team = team_for_user(user, doc.company)
	doc.user, doc.team = user, team

	bench = pick_bench(product)
	if not bench:
		doc.status = su.FAILED
		doc.error = "No server is available for this product right now. Our team has been notified."
		doc.save(ignore_permissions=True)
		_alert_ops(f"SaaS signup {doc.name}: no bench available for {product.name}")
		return doc

	from nexaploy.activity import log_activity
	from nexaploy.portal_utils import generate_password

	plan = product.default_plan or None
	trial_end = su.trial_end_date(today(), product.trial_days)
	site = frappe.get_doc(
		{
			"doctype": "Site",
			"site_name": doc.site_name,
			"bench": bench,
			"server": frappe.db.get_value("Bench", bench, "server"),
			"team": team,
			"plan": plan,
			# the site's Administrator password is the one chosen at signup
			"admin_password": doc.get_password("password", raise_exception=False) or generate_password(),
			"apps": [{"app": a} for a in product_apps(product)],
			"saas_product": product.name,
			"trial_end_date": trial_end,
		}
	)
	site.insert(ignore_permissions=True)
	log_activity(
		team, "site.create", "Site", site.name,
		{"bench": bench, "plan": plan, "source": "saas_signup", "product": product.name,
		 "trial_end_date": str(trial_end) if trial_end else None},
	)
	doc.site = site.name
	doc.status = su.PROVISIONING
	doc.password = None  # never keep it once the user and site exist
	doc.save(ignore_permissions=True)
	return doc


def refresh_signup(doc):
	"""Advance a Provisioning signup from its Site; returns the (possibly saved) doc."""
	if doc.status != su.PROVISIONING or not doc.site:
		return doc
	site_status = frappe.db.get_value("Site", doc.site, "status")
	job_status = frappe.db.get_value(
		"Agent Job",
		{"reference_doctype": "Site", "reference_name": doc.site, "kind": "site.create"},
		"status",
		order_by="creation desc",
	)
	new = su.next_signup_status(doc.status, site_status, job_status)
	if new != doc.status:
		doc.status = new
		if new == su.FAILED:
			doc.error = "Your site could not be created. Our team has been notified."
			_alert_ops(f"SaaS signup {doc.name}: site {doc.site} failed")
		doc.save(ignore_permissions=True)
		if new == su.ACTIVE:
			_send_welcome(doc)
	return doc


def _send_welcome(doc):
	try:
		title = frappe.db.get_value(PRODUCT, doc.product, "title")
		frappe.sendmail(
			recipients=[doc.email],
			subject=_("Your {0} site is ready").format(title),
			message=_("Your site {0} is ready. Log in at https://{0} as Administrator with the password you chose at signup. Manage it from {1}.").format(
				doc.site_name, get_url("/dashboard")
			),
		)
	except Exception:
		frappe.log_error(title=f"Nexaploy: welcome e-mail failed for {doc.name}")


def _alert_ops(message):
	try:
		from nexaploy.ops import send_alert

		send_alert(None, "SaaS signup problem", message)
	except Exception:
		frappe.log_error(title="Nexaploy: SaaS signup alert failed")


# --------------------------------------------------------------------------- trials


def team_can_pay(team):
	credits = frappe.db.get_value("Team", team, "credit_balance") or 0
	payments = frappe.db.count(
		"Nexaploy Payment", {"team": team, "status": "Success", "gateway": ["in", list(PAYING_GATEWAYS)]}
	)
	return su.team_can_pay(credits, payments)


def _billing_emails(team):
	from nexaploy.ops import team_emails

	return team_emails(team, kind="billing")


def _trial_mail(site, subject, message):
	recipients = _billing_emails(site.team)
	if recipients:
		frappe.sendmail(recipients=recipients, subject=f"[Nexaploy] {subject}", message=message, delayed=True)


def expire_trials():
	"""Daily: trial over and the team cannot pay (no gateway payment, no credits) -> e-mail;
	TRIAL_GRACE_DAYS later -> Site Suspended + maintenance_mode. Paying lifts it."""
	from nexaploy.billing.invoicing import _set_maintenance

	date = getdate(today())
	sites = frappe.get_all(
		"Site",
		filters={"trial_end_date": ["<=", date], "status": ["in", ["Active", "Suspended"]]},
		fields=["name", "server", "team", "status", "trial_end_date", "trial_state",
		        "suspended_for_billing", "usage_suspended"],
	)
	can_pay = {}
	for site in sites:
		if site.trial_state == su.TRIAL_CONVERTED or not site.team:
			continue
		if site.team not in can_pay:
			can_pay[site.team] = team_can_pay(site.team)
		action = su.trial_action(site.trial_end_date, date, can_pay[site.team], site.trial_state, site.status)
		if not action:
			continue
		try:
			if action == "convert":
				frappe.db.set_value("Site", site.name, "trial_state", su.TRIAL_CONVERTED, update_modified=False)
			elif action == "unsuspend":
				values = {"trial_state": su.TRIAL_CONVERTED}
				if not site.suspended_for_billing and not site.usage_suspended:
					_set_maintenance(site, 0)
					values["status"] = "Active"
				frappe.db.set_value("Site", site.name, values)
			elif action == "notify":
				_trial_mail(
					site,
					f"The trial of {site.name} has ended",
					f"The free trial of {site.name} ended on {site.trial_end_date}.\n\n"
					f"Add a payment method or credits within {su.TRIAL_GRACE_DAYS} days to keep the site "
					f"running: {get_url('/dashboard/billing')}",
				)
				frappe.db.set_value("Site", site.name, "trial_state", su.TRIAL_NOTIFIED, update_modified=False)
			elif action == "suspend":
				_set_maintenance(site, 1)
				frappe.db.set_value("Site", site.name, {"status": "Suspended", "trial_state": su.TRIAL_SUSPENDED})
				_trial_mail(
					site,
					f"{site.name} is suspended",
					f"{site.name} is in maintenance mode because its trial ended without a payment method. "
					f"Add a payment method or credits to reactivate it: {get_url('/dashboard/billing')}",
				)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Nexaploy: expire_trials failed for {site.name}")
