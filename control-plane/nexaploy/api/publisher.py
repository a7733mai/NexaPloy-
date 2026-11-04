"""Marketplace publishers: profile, listings (from the team's Custom Apps), review, revenue.

Portal (/dashboard/publisher, current team via nexaploy.teams):

| Method | Team action |
|---|---|
| get_publisher() | read |
| save_publisher(title, website, support_email, payout_method, payout_details, payout_account) | admin |
| list_listings() | read |
| listable_custom_apps() | admin |
| create_listing(custom_app, title, pricing, price_monthly, category, description, ...) | admin |
| update_listing(app, values) / submit_listing(app) / withdraw_listing(app) | admin |
| get_revenue() | billing |

Review (desk buttons on Marketplace App, System Manager only): approve_listing(app, notes),
reject_listing(app, notes), unpublish_listing(app, notes). Approval generates one release per
Frappe major from the source custom app's branches (publisher_utils.releases_from_branches).
Pure rules: nexaploy/publisher_utils.py; payouts: nexaploy/billing/payouts.py.
"""

import json

import frappe
from frappe import _
from frappe.utils import add_months, get_url, getdate, now_datetime, today

from nexaploy import publisher_utils as pu
from nexaploy.api.dashboard import _ctx
from nexaploy.team_utils import role_allows
from nexaploy.utils import parse_frappe_majors

PUBLISHER = "Marketplace Publisher"
APP = "Marketplace App"
PASSWORD_MASK = "********"
PUBLISHER_FIELDS = [
	"name", "title", "team", "verified", "website", "support_email", "payout_method",
	"payout_details", "revenue_share_percent",
]
LISTING_FIELDS = [
	"name", "app_name", "title", "status", "approved", "pricing", "price_monthly", "category",
	"description", "logo", "screenshots", "documentation_url", "review_notes", "submitted_on",
	"reviewed_on", "source_custom_app", "repo_url", "modified",
]


# --------------------------------------------------------------------------- helpers


def _throw(exc):
	frappe.throw(_(str(exc)))


def _publisher(team):
	pub = frappe.db.get_value(PUBLISHER, {"team": team}, PUBLISHER_FIELDS, as_dict=True)
	if pub:
		pub.has_account = bool(frappe.db.get_value(PUBLISHER, pub.name, "payout_account"))
	return pub


def _require_publisher(team):
	pub = _publisher(team)
	if not pub:
		frappe.throw(_("Create your publisher profile first"))
	return pub


def _listing(app, action="admin"):
	"""(context, Marketplace App doc) for a listing of the current team's publisher."""
	c = _ctx(action)
	if not app or not frappe.db.exists(APP, app):
		raise frappe.DoesNotExistError(_("App {0} not found").format(app))
	doc = frappe.get_doc(APP, app)
	team = frappe.db.get_value(PUBLISHER, doc.publisher_profile, "team") if doc.publisher_profile else None
	if team != c.team:
		raise frappe.PermissionError(_("App {0} is not listed by your team").format(app))
	return c, doc


def _log(team, action, name=None, details=None, doctype=APP):
	try:
		from nexaploy.activity import log_activity

		log_activity(team, action, doctype if name else None, name, details)
	except Exception:
		pass


def _listing_dict(doc):
	row = {f: doc.get(f) for f in LISTING_FIELDS}
	row["screenshots"] = [s for s in (doc.get("screenshots") or "").split("\n") if s]
	row["releases"] = [{"frappe_major": r.frappe_major, "branch": r.branch} for r in doc.get("releases") or []]
	row["editable"] = list(pu.editable_listing_fields(doc.get("status")))
	return row


def _parse_values(values):
	if isinstance(values, str):
		try:
			values = json.loads(values or "{}")
		except ValueError:
			frappe.throw(_("Invalid listing values"))
	return dict(values or {})


def _installs(apps):
	"""{app: active sites} from Site App rows of non-archived sites."""
	if not apps:
		return {}
	rows = frappe.db.sql(
		"""
		select sa.app, sa.parent as site, sa.status
		from `tabSite App` sa inner join `tabSite` s on s.name = sa.parent
		where sa.parenttype = 'Site' and sa.app in %(apps)s and s.status != 'Archived'
		""",
		{"apps": tuple(apps)},
		as_dict=True,
	)
	return pu.installs_by_app(rows)


# --------------------------------------------------------------------------- profile


@frappe.whitelist()
def get_publisher():
	c = _ctx("read")
	pub = _publisher(c.team)
	return {
		"team": c.team,
		"publisher": dict(pub) if pub else None,
		"permissions": {
			"manage": c.admin or role_allows(c.role, "admin"),
			"revenue": c.admin or role_allows(c.role, "billing"),
		},
		"payout_methods": list(pu.PAYOUT_METHODS),
		"categories": list(pu.CATEGORIES),
		"default_revenue_share": float(pu.DEFAULT_REVENUE_SHARE),
	}


@frappe.whitelist(methods=["POST"])
def save_publisher(title, website=None, support_email=None, payout_method="Credits", payout_details=None, payout_account=None):
	"""Create or update the current team's publisher profile (team admins).
	`payout_account` (bank account / IBAN) is write-only: empty or the mask keeps the stored one."""
	c = _ctx("admin")
	try:
		values = pu.clean_publisher_values(
			{
				"title": title,
				"website": website,
				"support_email": support_email,
				"payout_method": payout_method,
				"payout_details": payout_details,
			}
		)
	except pu.ListingError as exc:
		_throw(exc)
	name = frappe.db.get_value(PUBLISHER, {"team": c.team}, "name")
	doc = frappe.get_doc(PUBLISHER, name) if name else frappe.new_doc(PUBLISHER)
	doc.update(values)
	account = (payout_account or "").strip()
	if account and account != PASSWORD_MASK:
		if len(account) > 140:
			frappe.throw(_("Account number is too long"))
		doc.payout_account = account
	has_account = bool(account and account != PASSWORD_MASK) or bool(name and doc.get_password("payout_account", raise_exception=False))
	problem = pu.payout_details_required(doc.payout_method, doc.payout_details, has_account)
	if problem:
		frappe.throw(_(problem))
	if not name:
		doc.team = c.team
		doc.revenue_share_percent = float(pu.DEFAULT_REVENUE_SHARE)
		doc.insert(ignore_permissions=True)
	else:
		doc.save(ignore_permissions=True)
	_log(c.team, "publisher.create" if not name else "publisher.update", doc.name,
	     {"title": doc.title, "payout_method": doc.payout_method}, PUBLISHER)
	return dict(_publisher(c.team))


# --------------------------------------------------------------------------- listings


@frappe.whitelist()
def list_listings():
	c = _ctx("read")
	pub = _publisher(c.team)
	if not pub:
		return {"listings": [], "publisher": None}
	names = frappe.get_all(APP, filters={"publisher_profile": pub.name}, pluck="name", order_by="creation desc")
	installs = _installs(names)
	listings = []
	for name in names:
		row = _listing_dict(frappe.get_doc(APP, name))
		row["installs"] = installs.get(name, 0)
		listings.append(row)
	return {"listings": listings, "publisher": pub.name}


@frappe.whitelist()
def listable_custom_apps():
	"""Active custom apps of the team that are not listed yet."""
	c = _ctx("admin")
	apps = frappe.get_all(
		"Custom App",
		filters={"team": c.team, "status": "Active"},
		fields=["name", "title", "app_name", "repo_url", "branch", "frappe_majors", "provider"],
		order_by="title asc",
	)
	listed_sources = set(frappe.get_all(APP, filters={"source_custom_app": ["in", [a.name for a in apps] or [""]]}, pluck="source_custom_app"))
	taken = set(frappe.get_all(APP, filters={"name": ["in", [a.app_name for a in apps] or [""]]}, pluck="name"))
	return [dict(a, name_taken=a.app_name in taken) for a in apps if a.name not in listed_sources]


@frappe.whitelist(methods=["POST"])
def create_listing(custom_app, title=None, pricing="Free", price_monthly=0, category=None, description=None,
                   documentation_url=None, logo=None, screenshots=None):
	c = _ctx("admin")
	pub = _require_publisher(c.team)
	custom = frappe.db.get_value(
		"Custom App", custom_app, ["name", "team", "status", "app_name", "title", "repo_url"], as_dict=True
	)
	if not custom or custom.team != c.team:
		raise frappe.PermissionError(_("Custom app {0} does not belong to your team").format(custom_app))
	if custom.status != "Active":
		frappe.throw(_("Only active custom apps can be listed"))
	if frappe.db.exists(APP, custom.app_name):
		frappe.throw(_("An app named {0} is already on the marketplace").format(custom.app_name))
	if frappe.db.exists(APP, {"source_custom_app": custom.name}):
		frappe.throw(_("This custom app is already listed"))
	try:
		values = pu.clean_listing_values(
			{
				"title": title or custom.title or custom.app_name,
				"pricing": pricing or "Free",
				"price_monthly": price_monthly,
				"category": category,
				"description": description,
				"documentation_url": documentation_url,
				"logo": logo,
				"screenshots": screenshots,
			},
			pu.DRAFT,
		)
	except pu.ListingError as exc:
		_throw(exc)
	doc = frappe.get_doc(
		{
			"doctype": APP,
			"app_name": custom.app_name,
			"repo_url": custom.repo_url,
			"publisher_profile": pub.name,
			"publisher": pub.title,
			"source_custom_app": custom.name,
			"status": pu.DRAFT,
			"approved": 0,
			**{k: (float(v) if k == "price_monthly" else v) for k, v in values.items()},
		}
	)
	doc.insert(ignore_permissions=True)
	_log(c.team, "marketplace.listing_create", doc.name, {"custom_app": custom.name, "pricing": doc.pricing})
	return _listing_dict(doc)


@frappe.whitelist(methods=["POST"])
def update_listing(app, values=None):
	c, doc = _listing(app)
	try:
		clean = pu.clean_listing_values(_parse_values(values), doc.status)
	except pu.ListingError as exc:
		_throw(exc)
	for key, value in clean.items():
		doc.set(key, float(value) if key == "price_monthly" else value)
	doc.save(ignore_permissions=True)
	_log(c.team, "marketplace.listing_update", doc.name, {"fields": sorted(clean)})
	return _listing_dict(doc)


@frappe.whitelist(methods=["POST"])
def submit_listing(app):
	c, doc = _listing(app)
	problems = pu.check_submittable(doc.as_dict())
	if problems:
		frappe.throw("<br>".join(_(p) for p in problems))
	try:
		doc.status = pu.listing_transition(doc.status, "submit")
	except pu.ListingError as exc:
		_throw(exc)
	doc.submitted_on = now_datetime()
	doc.save(ignore_permissions=True)
	_log(c.team, "marketplace.listing_submit", doc.name)
	_notify_reviewers(doc)
	return _listing_dict(doc)


@frappe.whitelist(methods=["POST"])
def withdraw_listing(app):
	c, doc = _listing(app)
	try:
		doc.status = pu.listing_transition(doc.status, "withdraw")
	except pu.ListingError as exc:
		_throw(exc)
	doc.save(ignore_permissions=True)
	_log(c.team, "marketplace.listing_withdraw", doc.name)
	return _listing_dict(doc)


# --------------------------------------------------------------------------- review (desk)


def _review(app, action, notes=None):
	frappe.only_for("System Manager")
	doc = frappe.get_doc(APP, app)
	if not (notes or "").strip():
		notes = None  # keep notes typed into the form
	try:
		target = pu.listing_transition(doc.status, action, is_admin=True, notes=notes or doc.review_notes)
	except pu.ListingError as exc:
		_throw(exc)
	if notes is not None:
		doc.review_notes = notes
	if action == "approve":
		build_releases(doc)
	doc.status = target
	doc.approved = 1 if target == pu.PUBLISHED else 0
	doc.reviewed_by = frappe.session.user
	doc.reviewed_on = now_datetime()
	doc.save()
	team = frappe.db.get_value(PUBLISHER, doc.publisher_profile, "team") if doc.publisher_profile else None
	if team:
		_log(team, f"marketplace.listing_{action}", doc.name, {"notes": notes, "status": target})
		_notify_publisher(doc, team, action)
	return {"status": doc.status, "releases": [{"frappe_major": r.frappe_major, "branch": r.branch} for r in doc.releases]}


@frappe.whitelist(methods=["POST"])
def approve_listing(app, notes=None):
	return _review(app, "approve", notes)


@frappe.whitelist(methods=["POST"])
def reject_listing(app, notes=None):
	return _review(app, "reject", notes)


@frappe.whitelist(methods=["POST"])
def unpublish_listing(app, notes=None):
	return _review(app, "unpublish", notes)


def custom_app_branches(custom):
	"""Branch names of the custom app's repository (GitHub installation), else its tracked branch."""
	names = [custom.branch] if custom.branch else []
	if custom.provider == "GitHub" and custom.installation and custom.repo_full_name:
		try:
			from nexaploy.api.github import _paginate, installation_token

			token = installation_token(custom.installation)
			names += [b.get("name") for b in _paginate(f"/repos/{custom.repo_full_name}/branches", token) if b.get("name")]
		except Exception:
			frappe.log_error(title=f"Nexaploy: listing branches of {custom.repo_full_name} failed")
	return names


def build_releases(doc):
	"""Fill Marketplace App releases from the source custom app (existing rows win)."""
	generated = []
	if doc.source_custom_app and frappe.db.exists("Custom App", doc.source_custom_app):
		custom = frappe.get_doc("Custom App", doc.source_custom_app)
		generated = pu.releases_from_branches(
			custom_app_branches(custom), custom.branch, parse_frappe_majors(custom.frappe_majors)
		)
	merged = pu.merge_releases([r.as_dict() for r in doc.releases], generated)
	if not merged:
		frappe.throw(
			_("Cannot tell which Frappe versions the app supports: add releases to the listing or set the custom app's Frappe majors")
		)
	doc.set("releases", merged)
	return merged


def _notify_reviewers(doc):
	try:
		recipients = []
		alert = frappe.db.get_single_value("Nexaploy Settings", "alert_email")
		if alert:
			recipients.append(alert)
		if not recipients:
			return
		frappe.sendmail(
			recipients=recipients,
			subject=_("Marketplace listing {0} submitted for review").format(doc.app_name),
			message=_("{0} submitted {1} for review.").format(doc.publisher or doc.publisher_profile, doc.title)
			+ "<br><br>" + get_url(f"/app/marketplace-app/{doc.name}"),
			reference_doctype=APP,
			reference_name=doc.name,
		)
	except Exception:
		frappe.log_error(title=f"Nexaploy: review email for {doc.name} failed")


def _notify_publisher(doc, team, action):
	try:
		from nexaploy.teams import team_notification_emails

		recipients = team_notification_emails(team, "ops")
		if not recipients:
			return
		subjects = {
			"approve": _("Your app {0} is published on the marketplace"),
			"reject": _("Your marketplace listing {0} needs changes"),
			"unpublish": _("Your app {0} was unpublished from the marketplace"),
		}
		body = frappe.utils.escape_html(doc.review_notes or "")
		frappe.sendmail(
			recipients=recipients,
			subject=subjects[action].format(doc.title),
			message=(body + "<br><br>" if body else "") + get_url("/dashboard/publisher"),
			reference_doctype=APP,
			reference_name=doc.name,
		)
	except Exception:
		frappe.log_error(title=f"Nexaploy: publisher email for {doc.name} failed")


# --------------------------------------------------------------------------- revenue & payouts


@frappe.whitelist()
def get_revenue(months=12):
	"""Revenue by month (paid invoice lines of the publisher's apps, by payment date) with
	the estimated publisher share, plus the publisher's payouts."""
	c = _ctx("billing")
	pub = _publisher(c.team)
	if not pub:
		return {"months": [], "payouts": [], "share_percent": None}
	months = min(max(int(months or 12), 1), 36)
	apps = frappe.get_all(APP, filters={"publisher_profile": pub.name}, pluck="name")
	from nexaploy.billing import payouts_utils as pou
	from nexaploy.billing.payouts import paid_app_lines

	start = add_months(getdate(today()).replace(day=1), -(months - 1))
	rows = paid_app_lines(start, getdate(today()), apps)
	payouts = frappe.get_all(
		"Publisher Payout",
		filters={"publisher": pub.name},
		fields=["name", "status", "period_start", "period_end", "currency", "gross", "platform_fee", "net", "paid_on", "payout_method"],
		order_by="period_start desc",
		limit_page_length=50,
	)
	return {
		"months": [
			{**m, "gross": float(m["gross"]), "share": float(m["share"])}
			for m in pou.revenue_by_month(rows, pub.revenue_share_percent)
		],
		"payouts": payouts,
		"share_percent": pub.revenue_share_percent,
	}


# --------------------------------------------------------------------------- permissions (hooks.py)


def publisher_query(user=None, doctype=None):
	"""permission_query_conditions for Marketplace Publisher: rows of the user's teams."""
	from nexaploy.permissions import team_query

	return team_query(user, PUBLISHER)
