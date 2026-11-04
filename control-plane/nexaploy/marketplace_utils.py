"""Pure helpers for the version-aware app marketplace ("app library").

This module MUST NOT import frappe (unit-tested in control-plane/tests/test_marketplace.py).

Catalog entries used below are plain dicts keyed by python app name:
    {app_name: {"app_name", "title", "approved", "required_apps": [..], "releases": [{frappe_major, branch}]}}

- `required_apps` on a Marketplace App lists the python app names the app needs (hrms -> erpnext).
- A Release Group must contain every required app of its apps (marketplace or a custom app with
  the same python name, e.g. a fork of erpnext); adding an app auto-includes what it needs.
- A site installs required apps first (dependency order) and refuses when one is not on the bench.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

SUPPORTED_MAJORS = ("14", "15", "16")
DEFAULT_MAJOR = "16"
APP_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,60}$")


class RequirementError(ValueError):
	"""A dependency problem; the message is shown to the user."""


# --------------------------------------------------------------------------- parsing


def parse_required_apps(value: Any, app_name: str | None = None) -> list[str]:
	"""Marketplace App.required_apps ("erpnext", "erpnext, payments", one per line, or a list)
	-> unique python app names in the given order. `frappe` is implied and dropped.

	Raises RequirementError for invalid names or an app requiring itself."""
	if value is None or value == "":
		return []
	items = value if isinstance(value, (list, tuple)) else re.split(r"[\s,;]+", str(value))
	out: list[str] = []
	for item in items:
		name = str(item or "").strip().lower()
		if not name or name == "frappe":
			continue
		if not APP_NAME_RE.match(name):
			raise RequirementError(f"Invalid required app name {name!r}")
		if app_name and name == app_name:
			raise RequirementError(f"{app_name} cannot require itself")
		if name not in out:
			out.append(name)
	return out


def format_required_apps(apps: Iterable[str]) -> str:
	"""Canonical stored form: one app name per line."""
	return "\n".join(apps)


def supported_majors(releases: Iterable[Mapping[str, Any]] | None) -> list[str]:
	"""Sorted Frappe majors an app has a release for."""
	majors = {str(r.get("frappe_major")) for r in releases or () if str(r.get("frappe_major")) in SUPPORTED_MAJORS}
	return sorted(majors, key=int)


def normalize_major(value: Any, default: str | None = None) -> str | None:
	"""'16', 16, 'v16' -> '16'; '', None, 'all' -> default. ValueError for an unknown major."""
	if value is None or str(value).strip().lower() in ("", "all", "none", "null"):
		return default
	major = str(value).strip().lower().lstrip("v")
	if major not in SUPPORTED_MAJORS:
		raise ValueError(f"Unsupported Frappe version {value}")
	return major


def has_release(app: Mapping[str, Any] | None, major: str | int) -> bool:
	return bool(app) and str(major) in supported_majors(app.get("releases"))


# --------------------------------------------------------------------------- dependency graph


def requirements_of(app_name: str, catalog: Mapping[str, Mapping[str, Any]]) -> list[str]:
	entry = catalog.get(app_name) or {}
	try:
		return parse_required_apps(entry.get("required_apps"), app_name)
	except RequirementError:
		return []


def dependency_order(apps: Sequence[str], catalog: Mapping[str, Mapping[str, Any]]) -> list[str]:
	"""`apps` plus every (transitive) requirement, requirements before the apps needing them;
	otherwise the given order is kept. Apps unknown to the catalog (custom apps) have no
	requirements. RequirementError on a cycle."""
	order: list[str] = []
	state: dict[str, str] = {}

	def visit(name: str, chain: list[str]) -> None:
		if state.get(name) == "done":
			return
		if state.get(name) == "visiting":
			raise RequirementError("Circular app requirements: " + " -> ".join(chain + [name]))
		state[name] = "visiting"
		for req in requirements_of(name, catalog):
			visit(req, chain + [name])
		state[name] = "done"
		order.append(name)

	for app in apps:
		visit(app, [])
	return order


def sort_by_dependencies(apps: Sequence[str], catalog: Mapping[str, Mapping[str, Any]]) -> list[str]:
	"""Reorder `apps` (no additions) so requirements come first. Used for apps.json/site.create."""
	full = dependency_order(apps, catalog)
	wanted = set(apps)
	return [a for a in full if a in wanted]


def _requirement_problem(req: str, needed_by: str, catalog: Mapping[str, Mapping[str, Any]], major: str) -> str | None:
	entry = catalog.get(req)
	if not entry:
		return f"{needed_by} requires {req}, which is not in the marketplace"
	if not entry.get("approved"):
		return f"{needed_by} requires {req}, which is not approved in the marketplace"
	if not has_release(entry, major):
		return f"{needed_by} requires {req}, which has no release for Frappe {major}"
	return None


def apps_to_add(
	new_apps: Sequence[str],
	present: Iterable[str],
	catalog: Mapping[str, Mapping[str, Any]],
	major: str | int,
) -> list[str]:
	"""Marketplace apps to append to a Release Group when the customer picks `new_apps`:
	the picks plus their missing (transitive) requirements, requirements first. Requirements
	already in the group (`present` python names, custom apps included) are not added again.

	Raises RequirementError with a readable message when a requirement cannot be added
	(unknown, not approved or no release for `major`)."""
	major = str(major)
	present = set(present)
	order = dependency_order(list(new_apps), catalog)
	picked = set(new_apps)
	problems: list[str] = []
	needed_by = _needed_by(order, catalog)
	result = []
	for app in order:
		if app in present:
			continue
		if app not in picked:
			problem = _requirement_problem(app, needed_by.get(app, "?"), catalog, major)
			if problem:
				problems.append(problem)
				continue
		result.append(app)
	if problems:
		raise RequirementError("; ".join(problems))
	return result


def _needed_by(order: Sequence[str], catalog: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
	out: dict[str, str] = {}
	for app in order:
		for req in requirements_of(app, catalog):
			out.setdefault(req, app)
	return out


def missing_requirements(rows: Sequence[Mapping[str, Any]]) -> list[str]:
	"""Errors for a Release Group whose apps lack a requirement.

	rows: [{app_name, required_apps}] - every app of the group (custom apps have no
	required_apps but satisfy requirements by python name)."""
	names = {r.get("app_name") for r in rows if r.get("app_name")}
	errors = []
	for r in rows:
		try:
			reqs = parse_required_apps(r.get("required_apps"), r.get("app_name"))
		except RequirementError as exc:
			errors.append(str(exc))
			continue
		for req in reqs:
			if req not in names:
				errors.append(f"{r.get('app_name')} requires {req}; add {req} to the group")
	return errors


def dependents(app: str, apps: Iterable[str], catalog: Mapping[str, Mapping[str, Any]]) -> list[str]:
	"""Apps among `apps` that (directly) require `app`."""
	return [a for a in apps if a != app and app in requirements_of(a, catalog)]


def install_plan(
	requested: Sequence[str],
	installed: Iterable[str],
	available: Iterable[str],
	catalog: Mapping[str, Mapping[str, Any]],
	group: str | None = None,
) -> list[str]:
	"""Ordered apps a site must install to get `requested`: missing (transitive) requirements
	first. `available` are the bench's apps. Already installed apps are skipped.

	Raises RequirementError when a requested app or a requirement is not on the bench."""
	installed = set(installed)
	available = set(available)
	order = dependency_order(list(requested), catalog)
	needed_by = _needed_by(order, catalog)
	where = f"bench group {group}" if group else "this bench"
	problems = []
	plan = []
	for app in order:
		if app in installed:
			continue
		if app not in available:
			if app in requested:
				problems.append(f"{app} is not available on {where}")
			else:
				problems.append(
					f"{needed_by.get(app, '?')} requires {app}, which is not part of {where}. "
					f"Add {app} to the bench group and deploy first"
				)
			continue
		plan.append(app)
	if problems:
		raise RequirementError("; ".join(problems))
	return plan


# --------------------------------------------------------------------------- listing


def listing_item(app: Mapping[str, Any], major: str | None = None) -> dict[str, Any] | None:
	"""Public marketplace card for an approved app: only Verified releases; adds
	`supported_majors` (all verified majors, also when filtered) and `required_apps` (list).
	With `major`, None when the app has no verified release for it, and `releases` is
	restricted to that major. Release rows are reduced to {frappe_major, branch, commit}."""
	if not app.get("approved"):
		return None
	releases = list(visible_releases(app.get("releases")))
	if not releases:
		return None  # nothing installable yet (e.g. every release still being verified)
	item = dict(app)
	item["supported_majors"] = supported_majors(releases)
	try:
		item["required_apps"] = parse_required_apps(app.get("required_apps"), app.get("app_name"))
	except RequirementError:
		item["required_apps"] = []
	if major:
		releases = [r for r in releases if str(r.get("frappe_major")) == str(major)]
		if not releases:
			return None
	item["releases"] = [
		{"frappe_major": str(r.get("frappe_major")), "branch": r.get("branch"), "commit": r.get("commit")}
		for r in releases
	]
	return item


def search_apps(
	apps: Iterable[Mapping[str, Any]], query: str | None = None, category: str | None = None
) -> list[Mapping[str, Any]]:
	"""Case-insensitive search over title/app_name/publisher/description + category filter."""
	q = (query or "").strip().lower()
	out = []
	for a in apps:
		if category and a.get("category") != category:
			continue
		if q:
			text = " ".join(
				str(a.get(k) or "") for k in ("title", "app_name", "publisher", "category", "description")
			).lower()
			if q not in re.sub(r"<[^>]*>", " ", text):
				continue
		out.append(a)
	return out


# --------------------------------------------------------------------------- official catalog
# Seeded by nexaploy.marketplace_seed (after_install + patch v0_4). Branches were checked
# against the public repositories (Nov 2026): a major is listed only when a branch exists whose
# declared frappe dependency covers it. Not listed on purpose:
#   - v14 for crm, lms, insights, print_designer, builder, drive, ecommerce_integrations: no
#     v14-compatible branch (their stable branches require frappe >= 15).
#   - v16 for print_designer: its stable `main` declares only frappe >= 15 and there is no
#     version-16 branch; add a release once one is confirmed working on v16.
# Apps without version-N branches use their stable branch (insights/wiki: version-3,
# builder/drive: master), whose pyproject declares support for both v15 and v16.

OFFICIAL_TEAM = "nexaploy-official"
OFFICIAL_PUBLISHER_TITLE = "Nexaploy Official"

OFFICIAL_APPS: tuple[dict[str, Any], ...] = (
	{
		"app_name": "erpnext",
		"title": "ERPNext",
		"repo_url": "https://github.com/frappe/erpnext",
		"category": "ERP",
		"documentation_url": "https://docs.frappe.io/erpnext",
		"description": "Open source ERP: accounting, inventory, manufacturing, sales, purchasing and projects.",
		"required_apps": [],
		"releases": {"14": "version-14", "15": "version-15", "16": "version-16"},
	},
	{
		"app_name": "hrms",
		"title": "Frappe HR",
		"repo_url": "https://github.com/frappe/hrms",
		"category": "HR",
		"documentation_url": "https://docs.frappe.io/hr",
		"description": "HR and payroll: employees, leaves, attendance, expense claims, recruitment and payroll.",
		"required_apps": ["erpnext"],
		"releases": {"14": "version-14", "15": "version-15", "16": "version-16"},
	},
	{
		"app_name": "crm",
		"title": "Frappe CRM",
		"repo_url": "https://github.com/frappe/crm",
		"category": "CRM",
		"documentation_url": "https://docs.frappe.io/crm",
		"description": "Modern CRM for sales teams: leads, deals, notes, tasks, calls and email.",
		"required_apps": [],
		"releases": {"15": "version-15", "16": "version-16"},
	},
	{
		"app_name": "helpdesk",
		"title": "Helpdesk",
		"repo_url": "https://github.com/frappe/helpdesk",
		"category": "Support",
		"documentation_url": "https://docs.frappe.io/helpdesk",
		"description": "Customer support ticketing with SLAs, canned responses, knowledge base and a customer portal.",
		"required_apps": [],
		"releases": {"14": "version-14", "15": "version-15", "16": "version-16"},
	},
	{
		"app_name": "insights",
		"title": "Insights",
		"repo_url": "https://github.com/frappe/insights",
		"category": "Analytics",
		"documentation_url": "https://docs.frappe.io/insights",
		"description": "Reporting and BI: build queries, charts and dashboards on your site's data.",
		"required_apps": [],
		"releases": {"15": "version-3", "16": "version-3"},
	},
	{
		"app_name": "payments",
		"title": "Payments",
		"repo_url": "https://github.com/frappe/payments",
		"category": "Integration",
		"description": "Payment gateway integrations (Stripe, Razorpay, PayPal, Braintree and more) for Frappe apps.",
		"required_apps": [],
		"releases": {"14": "version-14", "15": "version-15", "16": "version-16"},
	},
	{
		"app_name": "lms",
		"title": "Frappe Learning",
		"repo_url": "https://github.com/frappe/lms",
		"category": "Education",
		"documentation_url": "https://docs.frappe.io/learning",
		"description": "Learning management system: courses, batches, quizzes, assignments and certificates.",
		# lms declares `payments` in its [tool.bench.frappe-dependencies]
		"required_apps": ["payments"],
		"releases": {"15": "version-15", "16": "version-16"},
	},
	{
		"app_name": "print_designer",
		"title": "Print Designer",
		"repo_url": "https://github.com/frappe/print_designer",
		"category": "Utility",
		"description": "Design print formats visually with a drag-and-drop editor.",
		"required_apps": [],
		"releases": {"15": "main"},
	},
	{
		"app_name": "wiki",
		"title": "Wiki",
		"repo_url": "https://github.com/frappe/wiki",
		"category": "Website",
		"description": "Documentation and knowledge base wiki with versioning and change requests.",
		"required_apps": [],
		"releases": {"14": "version-14", "15": "version-3", "16": "version-3"},
	},
	{
		"app_name": "builder",
		"title": "Builder",
		"repo_url": "https://github.com/frappe/builder",
		"category": "Website",
		"documentation_url": "https://docs.frappe.io/builder",
		"description": "Visual website builder: design pages without code and publish them from your site.",
		"required_apps": [],
		"releases": {"15": "master", "16": "master"},
	},
	{
		"app_name": "drive",
		"title": "Drive",
		"repo_url": "https://github.com/frappe/drive",
		"category": "Productivity",
		"description": "File storage and sharing with folders, previews and collaborative documents.",
		"required_apps": [],
		"releases": {"15": "master", "16": "master"},
	},
	{
		"app_name": "ecommerce_integrations",
		"title": "Ecommerce Integrations",
		"repo_url": "https://github.com/frappe/ecommerce_integrations",
		"category": "E-commerce",
		"description": "Connect ERPNext to Shopify, Unicommerce, Zenoti and Amazon.",
		"required_apps": ["erpnext"],
		"releases": {"15": "version-15", "16": "version-16"},
	},
	{
		"app_name": "india_compliance",
		"title": "India Compliance",
		"repo_url": "https://github.com/resilient-tech/india-compliance",
		"category": "Integration",
		"documentation_url": "https://docs.indiacompliance.app",
		"description": "GST, e-Invoice and e-Waybill compliance for Indian businesses on ERPNext.",
		"required_apps": ["erpnext"],
		"releases": {"14": "version-14", "15": "version-15", "16": "version-16"},
	},
)


def official_app_values(seed: Mapping[str, Any]) -> dict[str, Any]:
	"""Field values of a Marketplace App for an OFFICIAL_APPS entry (publisher set by caller)."""
	return {
		"app_name": seed["app_name"],
		"title": seed["title"],
		"repo_url": seed["repo_url"],
		"category": seed.get("category") or "Other",
		"description": seed.get("description") or "",
		"documentation_url": seed.get("documentation_url") or None,
		"pricing": "Free",
		"price_monthly": 0,
		"approved": 1,
		"status": "Published",
		"required_apps": format_required_apps(seed.get("required_apps") or []),
		"releases": seed_releases(seed),
	}


def seed_releases(seed: Mapping[str, Any]) -> list[dict[str, str]]:
	return [{"frappe_major": m, "branch": seed["releases"][m]} for m in sorted(seed["releases"], key=int)]


def missing_seed_releases(existing: Iterable[Mapping[str, Any]], seed: Mapping[str, Any]) -> list[dict[str, str]]:
	"""Seed releases for majors the existing app has no release for (curated rows win)."""
	have = set(supported_majors(existing))
	return [r for r in seed_releases(seed) if r["frappe_major"] not in have]


# --------------------------------------------------------------------------- release verification
# A Marketplace App Release added or changed by a publisher is "Pending verification" until a
# test build (image.build of frappe@<major branch> + required apps + the app) succeeds on the
# default build server. Customers only see Verified releases. Platform admins may set the
# status by hand (desk, or nexaploy.api.publisher.set_release_status). Rows created before
# verification existed have no status and count as Verified.

VERIFIED = "Verified"
PENDING = "Pending verification"
FAILED = "Failed"
RELEASE_STATUSES = (PENDING, VERIFIED, FAILED)


def release_visible(release: Mapping[str, Any]) -> bool:
	return (release.get("status") or VERIFIED) == VERIFIED


def visible_releases(releases: Iterable[Mapping[str, Any]] | None) -> list[Mapping[str, Any]]:
	return [r for r in releases or () if release_visible(r)]


def verification_tag(app_name: str, major: str | int, build_id: str) -> str:
	"""Image tag of a verification build (never pushed), e.g. nexaploy/verify:hrms-v16-20261104T101500."""
	return f"nexaploy/verify:{app_name}-v{major}-{build_id.lower()}"


def verification_apps_json(
	app: Mapping[str, Any],
	branch: str,
	catalog: Mapping[str, Mapping[str, Any]],
	major: str | int,
) -> list[dict[str, str]]:
	"""Minimal apps.json for a release test build: the app's (transitive) required apps at their
	Verified release for `major`, in dependency order, then the app itself at `branch`.
	frappe comes from the image's frappe_branch. RequirementError when a requirement has no
	verified release for the major."""
	major = str(major)
	name = app["app_name"]
	local = dict(catalog)
	local[name] = {**(catalog.get(name) or {}), **app}
	order = dependency_order([name], local)
	out: list[dict[str, str]] = []
	for req in order[:-1]:
		entry = local.get(req)
		rel = None
		if entry:
			rel = next((r for r in visible_releases(entry.get("releases")) if str(r.get("frappe_major")) == major), None)
		if not entry or not entry.get("repo_url") or rel is None:
			raise RequirementError(f"{name} requires {req}, which has no verified release for Frappe {major}")
		out.append({"url": entry["repo_url"], "branch": rel["branch"]})
	if not app.get("repo_url") or not branch:
		raise RequirementError(f"{name} needs a repository URL and a branch")
	out.append({"url": app["repo_url"], "branch": branch})
	return out


def release_changed(before: Mapping[str, Any] | None, after: Mapping[str, Any]) -> bool:
	"""A publisher edit that needs a new verification (new row, other branch or commit)."""
	if not before:
		return True
	return (before.get("branch") or "") != (after.get("branch") or "") or (before.get("commit") or "") != (
		after.get("commit") or ""
	)


# --------------------------------------------------------------------------- ratings & reviews

MAX_REVIEW_LENGTH = 4000


def clean_rating(value: Any) -> int:
	try:
		rating = int(str(value).strip())
	except (TypeError, ValueError):
		raise RequirementError("Rating must be a whole number from 1 to 5") from None
	if rating < 1 or rating > 5:
		raise RequirementError("Rating must be a whole number from 1 to 5")
	return rating


def clean_review_text(value: Any, max_len: int = MAX_REVIEW_LENGTH) -> str:
	"""Plain text (tags stripped), at most `max_len` characters."""
	text = re.sub(r"<[^>]*>", "", str(value or "")).replace("\r\n", "\n").strip()
	text = re.sub(r"\n{3,}", "\n\n", text)
	if len(text) > max_len:
		raise RequirementError(f"Keep it under {max_len} characters")
	return text


def rating_summary(reviews: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
	"""{average (1 decimal, None without reviews), count} over visible (not hidden) reviews."""
	ratings = [int(r.get("rating") or 0) for r in reviews if not r.get("hidden") and r.get("rating")]
	if not ratings:
		return {"average": None, "count": 0}
	return {"average": round(sum(ratings) / len(ratings), 1), "count": len(ratings)}


def rating_summaries(reviews: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
	"""{app: rating_summary} for review rows carrying `app`."""
	by_app: dict[str, list[Mapping[str, Any]]] = {}
	for r in reviews:
		by_app.setdefault(r.get("app"), []).append(r)
	return {app: rating_summary(rows) for app, rows in by_app.items()}
