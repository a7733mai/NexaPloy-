"""Pure marketplace publisher logic (no frappe import; tested in tests/test_publisher.py).

Publishers (a team's **Marketplace Publisher** profile) list their own Custom Apps on the
marketplace. A listing is a Marketplace App with `status`:

    Draft --submit--> In Review --approve--> Published --unpublish--> Draft
      ^                  |  \\--reject--> Rejected --submit--> In Review
      +----withdraw------+

`approved` (what the build pipeline checks) is True exactly when the status is Published.
On approval releases are generated per Frappe major from the custom app's branches
(`version-15`, `v16`, ...) and the majors the app declares (`Custom App.frappe_majors`).
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

from nexaploy.github_utils import is_valid_branch, majors_from_branch

SUPPORTED_MAJORS = ("14", "15", "16")

DRAFT = "Draft"
IN_REVIEW = "In Review"
PUBLISHED = "Published"
REJECTED = "Rejected"
LISTING_STATUSES = (DRAFT, IN_REVIEW, PUBLISHED, REJECTED)

PAYOUT_METHODS = ("Bank", "PayPal", "Credits")
DEFAULT_REVENUE_SHARE = Decimal("80")
PRICING = ("Free", "Paid")
CATEGORIES = (
	"ERP", "HR", "CRM", "Support", "Analytics", "Education", "Website", "Productivity", "E-commerce",
	"Integration", "Utility", "Other",
)
MAX_SCREENSHOTS = 8
MAX_PRICE = Decimal("10000")

# action -> (allowed source statuses, target status, who may do it)
LISTING_TRANSITIONS = {
	"submit": ((DRAFT, REJECTED), IN_REVIEW, "publisher"),
	"withdraw": ((IN_REVIEW,), DRAFT, "publisher"),
	"approve": ((IN_REVIEW,), PUBLISHED, "admin"),
	"reject": ((IN_REVIEW,), REJECTED, "admin"),
	"unpublish": ((PUBLISHED,), DRAFT, "admin"),
}

# Fields a publisher may change from the portal, per listing status. Pricing of a published
# listing is locked (customers subscribed at that price): unpublish + resubmit to change it.
LISTING_FIELDS = (
	"title", "category", "description", "logo", "screenshots", "documentation_url", "pricing", "price_monthly",
	"required_apps",
)
_EDITABLE = {
	DRAFT: LISTING_FIELDS,
	REJECTED: LISTING_FIELDS,
	IN_REVIEW: (),
	PUBLISHED: ("title", "category", "description", "logo", "screenshots", "documentation_url"),
}


class ListingError(ValueError):
	"""An invalid listing/publisher operation; the message is shown to the user."""


# --------------------------------------------------------------------------- review state machine


def listing_transition(status: str | None, action: str, is_admin: bool = False, notes: str | None = None) -> str:
	"""Target status for `action` on a listing in `status`; raises ListingError when the
	action is unknown, not allowed from `status` or reserved to platform admins. Rejecting
	needs review notes."""
	status = status or DRAFT
	if action not in LISTING_TRANSITIONS:
		raise ListingError(f"Unknown listing action {action}")
	sources, target, actor = LISTING_TRANSITIONS[action]
	if actor == "admin" and not is_admin:
		raise ListingError("Only platform administrators can review listings")
	if status not in sources:
		raise ListingError(f"Cannot {action} a listing that is {status}")
	if action == "reject" and not (notes or "").strip():
		raise ListingError("Explain why the listing is rejected in the review notes")
	return target


def sync_approval(
	status: str | None,
	approved: Any,
	prev_status: str | None = None,
	prev_approved: Any = None,
	is_new: bool = False,
) -> tuple[str, bool]:
	"""Keep `status` and `approved` consistent: approved <=> Published.

	A new document that is approved is Published (curated apps created by staff). On an
	update, a changed status wins; otherwise a changed `approved` flag moves the status
	(approve -> Published, unapprove a Published app -> Draft)."""
	status = status if status in LISTING_STATUSES else DRAFT
	approved = bool(int(approved or 0))
	if is_new:
		if approved:
			return PUBLISHED, True
		return status, status == PUBLISHED
	if status != (prev_status or DRAFT):
		return status, status == PUBLISHED
	if approved != bool(int(prev_approved or 0)):
		if approved:
			return PUBLISHED, True
		return (DRAFT if status == PUBLISHED else status), False
	return status, status == PUBLISHED


def editable_listing_fields(status: str | None) -> tuple[str, ...]:
	return _EDITABLE.get(status or DRAFT, ())


# --------------------------------------------------------------------------- validation


def _clean_text(value: Any, max_len: int) -> str:
	return re.sub(r"\s+", " ", str(value or "")).strip()[:max_len]


def is_valid_url(url: Any, allow_files: bool = False) -> bool:
	"""https?:// URL without whitespace/credentials (or a /files/ path when allowed)."""
	text = str(url or "").strip()
	if not text or len(text) > 500 or re.search(r"[\s<>\"'`]", text):
		return False
	if allow_files and re.match(r"^/(private/)?files/[^/]", text):
		return not text.startswith("/private/")  # listing media must be public
	m = re.match(r"^https?://([^/?#]+)", text, re.I)
	return bool(m) and "@" not in m.group(1)


def is_valid_email(value: Any) -> bool:
	return bool(re.match(r"^[^@\s<>\"']+@[^@\s<>\"']+\.[A-Za-z]{2,}$", str(value or "").strip()))


def parse_screenshots(value: Any) -> list[str]:
	"""Screenshot URLs, one per line (or a list). Raises ListingError for an invalid URL or
	more than MAX_SCREENSHOTS."""
	if value is None or value == "":
		return []
	items = value if isinstance(value, (list, tuple)) else re.split(r"[\r\n]+", str(value))
	urls = []
	for item in items:
		url = str(item or "").strip()
		if not url:
			continue
		if not is_valid_url(url, allow_files=True):
			raise ListingError(f"Invalid screenshot URL {url[:80]}")
		if url not in urls:
			urls.append(url)
	if len(urls) > MAX_SCREENSHOTS:
		raise ListingError(f"At most {MAX_SCREENSHOTS} screenshots")
	return urls


def parse_price(value: Any) -> Decimal:
	try:
		price = Decimal(str(value if value not in (None, "") else "0").strip())
	except (InvalidOperation, ValueError):
		raise ListingError("Invalid price") from None
	if not price.is_finite() or price < 0 or price > MAX_PRICE:
		raise ListingError("Invalid price")
	return price.quantize(Decimal("0.01"))


def clean_listing_values(values: Mapping[str, Any], status: str | None = None) -> dict[str, Any]:
	"""Validated subset of `values` the publisher may set on a listing in `status`.
	Unknown keys are ignored; keys not editable in `status` raise ListingError."""
	allowed = editable_listing_fields(status)
	out: dict[str, Any] = {}
	for key in LISTING_FIELDS:
		if key not in values:
			continue
		if key not in allowed:
			raise ListingError(f"{key} cannot be changed while the listing is {status or DRAFT}")
		value = values[key]
		if key == "title":
			value = _clean_text(value, 140)
			if len(value) < 2:
				raise ListingError("Title is required")
		elif key == "category":
			value = value or ""
			if value and value not in CATEGORIES:
				raise ListingError(f"Unknown category {value}")
		elif key == "description":
			value = str(value or "")[:20000]
		elif key == "logo":
			value = str(value or "").strip()
			if value and not is_valid_url(value, allow_files=True):
				raise ListingError("Invalid logo URL")
		elif key == "screenshots":
			value = "\n".join(parse_screenshots(value))
		elif key == "documentation_url":
			value = str(value or "").strip()
			if value and not is_valid_url(value):
				raise ListingError("Invalid documentation URL")
		elif key == "pricing":
			if value not in PRICING:
				raise ListingError("Pricing must be Free or Paid")
		elif key == "price_monthly":
			value = parse_price(value)
		elif key == "required_apps":
			from nexaploy.marketplace_utils import RequirementError, format_required_apps, parse_required_apps

			try:
				value = format_required_apps(parse_required_apps(value, values.get("app_name")))
			except RequirementError as exc:
				raise ListingError(str(exc)) from None
		out[key] = value
	pricing = out.get("pricing", values.get("pricing"))
	if "pricing" in out or "price_monthly" in out:
		price = out.get("price_monthly", parse_price(values.get("price_monthly")))
		if pricing == "Paid" and price <= 0:
			raise ListingError("Paid apps need a monthly price")
		if pricing == "Free":
			out["price_monthly"] = Decimal("0.00")
	return out


def check_submittable(listing: Mapping[str, Any]) -> list[str]:
	"""Problems that block submitting a listing for review (empty == ok)."""
	problems = []
	if not _clean_text(listing.get("title"), 140):
		problems.append("Title is required")
	if not _clean_text(re.sub(r"<[^>]*>", " ", str(listing.get("description") or "")), 20000):
		problems.append("Add a description")
	if not listing.get("source_custom_app"):
		problems.append("The listing has no source custom app")
	if listing.get("pricing") == "Paid" and parse_price(listing.get("price_monthly")) <= 0:
		problems.append("Paid apps need a monthly price")
	return problems


def validate_share_percent(value: Any) -> Decimal:
	"""Publisher revenue share in percent, 0..100 (default 80)."""
	if value is None or value == "":
		return DEFAULT_REVENUE_SHARE
	try:
		pct = Decimal(str(value))
	except (InvalidOperation, ValueError):
		raise ListingError("Revenue share must be a number") from None
	if not pct.is_finite() or pct < 0 or pct > 100:
		raise ListingError("Revenue share must be between 0 and 100 percent")
	return pct


def clean_publisher_values(values: Mapping[str, Any]) -> dict[str, Any]:
	"""Validated publisher profile fields a team admin may set (not share/verified)."""
	out: dict[str, Any] = {}
	if "title" in values:
		out["title"] = _clean_text(values.get("title"), 140)
		if len(out["title"]) < 2:
			raise ListingError("Publisher name is required")
	if "website" in values:
		website = str(values.get("website") or "").strip()
		if website and not is_valid_url(website):
			raise ListingError("Invalid website URL")
		out["website"] = website
	if "support_email" in values:
		email = str(values.get("support_email") or "").strip()
		if email and not is_valid_email(email):
			raise ListingError("Invalid support email")
		out["support_email"] = email
	if "payout_method" in values:
		method = values.get("payout_method") or "Credits"
		if method not in PAYOUT_METHODS:
			raise ListingError("Payout method must be Bank, PayPal or Credits")
		out["payout_method"] = method
	if "payout_details" in values:
		out["payout_details"] = str(values.get("payout_details") or "").strip()[:1000]
	return out


def payout_details_required(method: str | None, details: str | None, has_account: bool) -> str | None:
	"""Problem with the payout setup for `method`, or None."""
	if method == "PayPal" and not is_valid_email(details) and not has_account:
		return "Enter the PayPal email address in the payout details"
	if method == "Bank" and not has_account:
		return "Enter the bank account number (IBAN)"
	return None


# --------------------------------------------------------------------------- releases


def _branch_rank(branch: str, major: str) -> tuple[int, str]:
	if branch == f"version-{major}":
		return (0, branch)
	if branch == f"v{major}":
		return (1, branch)
	return (2, branch)


def releases_from_branches(
	branches: Iterable[str],
	default_branch: str | None,
	declared_majors: Iterable[str] = (),
	supported: Sequence[str] = SUPPORTED_MAJORS,
) -> list[dict[str, str]]:
	"""One release per Frappe major the custom app supports.

	* A branch named for a major (`version-15`, `v15`, `version-15-hotfix`) is that major's
	  release; `version-NN` wins over `vNN` over other suffixes.
	* Majors declared on the custom app (`frappe_majors`) without such a branch use the
	  app's tracked branch.
	* Nothing declared: the tracked branch's own name may name the major (`version-15`).
	Returns [{frappe_major, branch}] sorted by major; empty when nothing can be determined.
	"""
	branches = [b for b in dict.fromkeys(str(b) for b in branches or []) if is_valid_branch(b)]
	declared = [m for m in (str(x) for x in declared_majors or []) if m in supported]
	by_major: dict[str, str] = {}
	for branch in branches:
		for major in majors_from_branch(branch):
			if major not in supported:
				continue
			if major not in by_major or _branch_rank(branch, major) < _branch_rank(by_major[major], major):
				by_major[major] = branch
	default_ok = bool(default_branch) and is_valid_branch(default_branch)
	if default_ok:
		for major in declared:
			by_major.setdefault(major, default_branch)
		if not declared and not by_major:
			for major in majors_from_branch(default_branch):
				by_major.setdefault(major, default_branch)
	if declared:
		# the app said which majors it supports: version branches of other majors are stale
		by_major = {m: b for m, b in by_major.items() if m in declared}
	return [{"frappe_major": m, "branch": by_major[m]} for m in sorted(by_major, key=int)]


def merge_releases(existing: Iterable[Mapping[str, Any]], generated: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
	"""Existing release rows (curated by staff) win; generated rows fill missing majors."""
	out: dict[str, dict[str, Any]] = {}
	for row in existing or []:
		major = str(row.get("frappe_major") or "")
		if major and row.get("branch") and major not in out:
			out[major] = {"frappe_major": major, "branch": row.get("branch"), "commit": row.get("commit") or None}
	for row in generated or []:
		major = str(row.get("frappe_major") or "")
		if major and row.get("branch") and major not in out:
			out[major] = {"frappe_major": major, "branch": row.get("branch"), "commit": row.get("commit") or None}
	return [out[m] for m in sorted(out, key=int)]


# --------------------------------------------------------------------------- portal stats


def month_key(value: Any) -> str:
	"""'YYYY-MM' of a date/datetime/ISO string."""
	text = value.isoformat() if hasattr(value, "isoformat") else str(value or "")
	return text[:7]


def installs_by_app(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
	"""{app: number of sites} from Site App rows ({app, site/parent, status}). Failed installs
	and the same site twice are not counted."""
	seen: dict[str, set] = {}
	for row in rows or []:
		app = row.get("app")
		if not app or row.get("status") == "Failed":
			continue
		seen.setdefault(app, set()).add(row.get("site") or row.get("parent"))
	return {app: len(sites) for app, sites in seen.items()}
