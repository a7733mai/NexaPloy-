"""Public marketplace API (the app library) and ratings & reviews.

Listings are version-aware: only approved apps with a *Verified* release for the requested
Frappe major are offered (see nexaploy.marketplace_utils). Reviews: a team that has the app
installed on at least one of its sites may leave one 1-5 rating + text per app (editable);
the app's publisher team may reply; platform admins may hide abusive reviews.
"""

import frappe
import frappe.utils
from frappe import _

from nexaploy import marketplace_utils as mu

LISTED_FIELDS = (
	"app_name",
	"title",
	"repo_url",
	"publisher",
	"category",
	"description",
	"pricing",
	"price_monthly",
	"approved",
	"logo",
	"documentation_url",
	"required_apps",
)
REVIEW = "Marketplace App Review"


def _throw(exc):
	frappe.throw(_(str(exc)))


def _major(frappe_major, default=None):
	try:
		return mu.normalize_major(frappe_major, default)
	except ValueError as exc:
		_throw(exc)


def _releases_by_app(app_names):
	rows = frappe.get_all(
		"Marketplace App Release",
		filters={"parenttype": "Marketplace App", "parent": ["in", list(app_names) or [""]]},
		fields=["parent", "frappe_major", "branch", "commit", "status"],
		order_by="frappe_major asc",
	)
	by_app = {}
	for rel in rows:
		by_app.setdefault(rel.parent, []).append(
			{"frappe_major": str(rel.frappe_major), "branch": rel.branch, "commit": rel.commit, "status": rel.status}
		)
	return by_app


def load_catalog(approved_only=False):
	"""{app_name: {app_name, title, approved, repo_url, required_apps:[...], releases:[..status]}}
	for every Marketplace App (dependency resolution / verification builds)."""
	filters = {"approved": 1} if approved_only else {}
	apps = frappe.get_all(
		"Marketplace App", filters=filters, fields=["app_name", "title", "approved", "repo_url", "required_apps"]
	)
	releases = _releases_by_app([a.app_name for a in apps])
	catalog = {}
	for a in apps:
		try:
			required = mu.parse_required_apps(a.required_apps, a.app_name)
		except mu.RequirementError:
			required = []
		catalog[a.app_name] = {
			"app_name": a.app_name,
			"title": a.title,
			"approved": bool(a.approved),
			"repo_url": a.repo_url,
			"required_apps": required,
			"releases": releases.get(a.app_name, []),
		}
	return catalog


def compatible_catalog(major):
	"""Approved apps with a Verified release for `major` (what customers may pick)."""
	return {
		name: entry
		for name, entry in load_catalog(approved_only=True).items()
		if mu.has_release({"releases": mu.visible_releases(entry["releases"])}, major)
	}


def _ratings(app_names):
	if not app_names:
		return {}
	rows = frappe.get_all(
		REVIEW, filters={"app": ["in", list(app_names)], "hidden": 0}, fields=["app", "rating", "hidden"]
	)
	return mu.rating_summaries(rows)


@frappe.whitelist(allow_guest=True)
def list_apps(frappe_major=None, query=None, category=None):
	"""Approved Marketplace Apps; with `frappe_major`, only apps that have a Verified release for
	it (and only those releases). Returns
	[{app_name, title, repo_url, publisher, category, description, pricing, price_monthly,
	  logo, documentation_url, releases: [{frappe_major, branch, commit}], supported_majors,
	  required_apps, rating: {average, count}}].
	"""
	major = _major(frappe_major)
	apps = frappe.get_all(
		"Marketplace App", filters={"approved": 1}, fields=list(LISTED_FIELDS), order_by="title asc"
	)
	releases = _releases_by_app([a.app_name for a in apps])
	result = []
	for app in apps:
		item = dict(app)
		item["releases"] = releases.get(app.app_name, [])
		item = mu.listing_item(item, major)
		if item is None:
			continue
		item.pop("approved", None)
		result.append(item)
	result = [dict(a) for a in mu.search_apps(result, query, category or None)]
	ratings = _ratings([a["app_name"] for a in result])
	for item in result:
		item["rating"] = ratings.get(item["app_name"]) or {"average": None, "count": 0}
	return result


# --------------------------------------------------------------------------- app detail & reviews


def _current_team():
	if frappe.session.user == "Guest":
		return None
	try:
		from nexaploy.teams import get_current_team

		return get_current_team()
	except Exception:
		return None


def _is_admin():
	from nexaploy.teams import is_platform_admin

	return frappe.session.user != "Guest" and is_platform_admin()


def _publisher_team(app):
	profile = frappe.db.get_value("Marketplace App", app, "publisher_profile")
	return frappe.db.get_value("Marketplace Publisher", profile, "team") if profile else None


def team_has_app_installed(team, app):
	"""True when one of the team's (non-archived) sites has `app` Installed."""
	if not team:
		return False
	sites = frappe.get_all("Site", filters={"team": team, "status": ["!=", "Archived"]}, pluck="name")
	if not sites:
		return False
	return bool(
		frappe.get_all(
			"Site App",
			filters={"parenttype": "Site", "parent": ["in", sites], "app": app, "status": "Installed"},
			limit=1,
		)
	)


def _review_dict(row, team_titles, admin=False):
	item = {
		"name": row.name,
		"team_title": team_titles.get(row.team) or _("A customer"),
		"rating": row.rating,
		"review": row.review,
		"publisher_reply": row.publisher_reply,
		"replied_on": row.replied_on,
		"creation": row.creation,
		"modified": row.modified,
	}
	if admin:
		item.update(hidden=bool(row.hidden), hidden_reason=row.hidden_reason, team=row.team)
	return item


def _listed_app(app):
	if not app or not frappe.db.exists("Marketplace App", {"name": app, "approved": 1}):
		raise frappe.DoesNotExistError(_("App {0} not found").format(app))


@frappe.whitelist(allow_guest=True)
def get_app(app, frappe_major=None):
	"""Marketplace card + reviews of one approved app, and what the caller may do:
	{app, reviews, my_review, can_review, can_reply, is_admin}."""
	_listed_app(app)
	items = [a for a in list_apps() if a["app_name"] == app]
	if not items:
		raise frappe.DoesNotExistError(_("App {0} has no verified release yet").format(app))
	item = items[0]
	admin = _is_admin()
	team = _current_team()
	filters = {"app": app} if admin else {"app": app, "hidden": 0}
	rows = frappe.get_all(
		REVIEW,
		filters=filters,
		fields=[
			"name", "team", "rating", "review", "publisher_reply", "replied_on", "hidden", "hidden_reason",
			"creation", "modified",
		],
		order_by="modified desc",
		limit_page_length=200,
	)
	titles = {}
	if rows:
		titles = {
			t.name: t.title
			for t in frappe.get_all("Team", filters={"name": ["in", list({r.team for r in rows})]}, fields=["name", "title"])
		}
	mine = frappe.db.get_value(REVIEW, {"app": app, "team": team}, ["name", "rating", "review", "hidden"], as_dict=True) if team else None
	publisher_team = _publisher_team(app)
	return {
		"app": item,
		"reviews": [_review_dict(r, titles, admin) for r in rows],
		"my_review": mine,
		"can_review": bool(team) and team_has_app_installed(team, app),
		"can_reply": bool(team) and (admin or (publisher_team and publisher_team == team)),
		"is_admin": admin,
		"selected_major": _major(frappe_major),
	}


@frappe.whitelist(methods=["POST"])
def save_review(app, rating, review=None):
	"""Create or update the current team's review of `app` (needs the app installed on one of
	the team's sites)."""
	from nexaploy.activity import log_activity
	from nexaploy.teams import check_team_access, get_current_team

	team = get_current_team()
	check_team_access(team, "write")
	_listed_app(app)
	if not team_has_app_installed(team, app):
		frappe.throw(_("Install {0} on one of your sites before reviewing it").format(app))
	try:
		rating = mu.clean_rating(rating)
		text = mu.clean_review_text(review)
	except mu.RequirementError as exc:
		_throw(exc)
	name = frappe.db.get_value(REVIEW, {"app": app, "team": team}, "name")
	if name:
		doc = frappe.get_doc(REVIEW, name)
		doc.rating, doc.review, doc.reviewer = rating, text or None, frappe.session.user
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc(
			{"doctype": REVIEW, "app": app, "team": team, "reviewer": frappe.session.user, "rating": rating, "review": text or None}
		).insert(ignore_permissions=True)
	log_activity(team, "marketplace.review", REVIEW, doc.name, {"app": app, "rating": rating})
	return {"name": doc.name, "rating": doc.rating, "review": doc.review}


@frappe.whitelist(methods=["POST"])
def delete_review(app):
	from nexaploy.teams import check_team_access, get_current_team

	team = get_current_team()
	check_team_access(team, "write")
	name = frappe.db.get_value(REVIEW, {"app": app, "team": team}, "name")
	if not name:
		frappe.throw(_("You have not reviewed {0}").format(app))
	frappe.delete_doc(REVIEW, name, ignore_permissions=True)
	return {"deleted": name}


@frappe.whitelist(methods=["POST"])
def reply_review(review, reply=None):
	"""The app's publisher team (or a platform admin) answers a review; empty clears it."""
	from nexaploy.teams import check_team_access, get_current_team

	doc = frappe.get_doc(REVIEW, review)
	if not _is_admin():
		team = get_current_team()
		check_team_access(team, "write")
		if not team or _publisher_team(doc.app) != team:
			raise frappe.PermissionError(_("Only the publisher of {0} can reply to its reviews").format(doc.app))
	try:
		text = mu.clean_review_text(reply)
	except mu.RequirementError as exc:
		_throw(exc)
	doc.publisher_reply = text or None
	doc.replied_by = frappe.session.user if text else None
	doc.replied_on = frappe.utils.now_datetime() if text else None
	doc.save(ignore_permissions=True)
	return {"name": doc.name, "publisher_reply": doc.publisher_reply, "replied_on": doc.replied_on}


@frappe.whitelist(methods=["POST"])
def hide_review(review, hidden=1, reason=None):
	"""Platform admins hide (or show again) an abusive review."""
	if not _is_admin():
		raise frappe.PermissionError(_("Only platform administrators can hide reviews"))
	doc = frappe.get_doc(REVIEW, review)
	doc.hidden = 1 if str(hidden).lower() in ("1", "true", "yes") else 0
	doc.hidden_reason = ((reason or "").strip() or None) if doc.hidden else None
	doc.hidden_by = frappe.session.user if doc.hidden else None
	doc.save(ignore_permissions=True)
	return {"name": doc.name, "hidden": bool(doc.hidden)}


# --------------------------------------------------------------------------- desk link query


@frappe.whitelist()
def app_query(doctype, txt, searchfield, start, page_len, filters):
	"""Marketplace App link search for the Release Group form: approved apps with a Verified
	release for `filters.frappe_major`."""
	filters = frappe.parse_json(filters) if isinstance(filters, str) else (filters or {})
	major = _major(filters.get("frappe_major"))
	txt = str(txt or "").lower()  # matched in python, never in SQL
	catalog = compatible_catalog(major) if major else load_catalog(approved_only=True)
	rows = [
		(name, entry["title"])
		for name, entry in sorted(catalog.items())
		if not txt or txt in name or txt in (entry["title"] or "").lower()
	]
	start, page_len = int(start or 0), int(page_len or 20)
	return rows[start : start + page_len]
