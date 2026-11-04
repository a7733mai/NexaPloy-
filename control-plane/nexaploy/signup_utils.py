"""Pure SaaS signup / trial logic (no frappe import; tests/test_provisioning.py).

Flow (nexaploy/api/signup.py, nexaploy/saas.py):

    signup()        -> SaaS Signup "Pending Verification" + e-mail with a verification link
    verify_email()  -> User + Team + Site on the least loaded bench -> "Provisioning"
    signup_status() -> "Active" once the Site is Active ("Failed" if site.create failed)

Trials: Site.trial_end_date is the first billable day (subscriptions start on it, see
billing/subscriptions.sync_site). Site.trial_state tracks the end of the trial:
"" (in trial) -> Notified -> Suspended, or -> Converted once the team can pay.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable, Mapping

from nexaploy.portal_utils import normalize_root_domain, normalize_subdomain

PENDING_VERIFICATION = "Pending Verification"
VERIFIED = "Verified"
PROVISIONING = "Provisioning"
ACTIVE = "Active"
FAILED = "Failed"
SIGNUP_STATUSES = (PENDING_VERIFICATION, VERIFIED, PROVISIONING, ACTIVE, FAILED)

DEFAULT_TRIAL_DAYS = 14
TRIAL_GRACE_DAYS = 3
VERIFICATION_TTL_HOURS = 24
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
BENCH_SELECTION_STRATEGIES = ("Least Sites",)

TRIAL_NOTIFIED = "Notified"
TRIAL_SUSPENDED = "Suspended"
TRIAL_CONVERTED = "Converted"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


# --------------------------------------------------------------------------- inputs


def normalize_slug(value: Any) -> str:
	text = str(value or "").strip().lower().strip("/")
	if not SLUG_RE.match(text):
		raise ValueError("Invalid product")
	return text


def normalize_email(value: Any) -> str:
	text = str(value or "").strip().lower()
	if not EMAIL_RE.match(text) or len(text) > 140:
		raise ValueError("Enter a valid e-mail address")
	return text


def check_password(value: Any) -> str:
	text = str(value or "")
	if len(text) < MIN_PASSWORD_LENGTH:
		raise ValueError("Password must have at least 8 characters")
	if len(text) > MAX_PASSWORD_LENGTH:
		raise ValueError("Password is too long")
	if text.strip() != text or not any(c.isalpha() for c in text) or not any(c.isdigit() for c in text):
		raise ValueError("Password must contain letters and digits and no leading or trailing spaces")
	return text


def clean_text(value: Any, max_length: int = 140) -> str:
	return re.sub(r"\s+", " ", str(value or "")).strip()[:max_length]


def site_name_for(subdomain: Any, root_domain: Any) -> tuple[str, str]:
	"""(subdomain, site FQDN) for what the visitor typed. Only one label: dots are not allowed.
	Raises ValueError (messages from portal_utils.normalize_subdomain)."""
	text = str(subdomain or "").strip().lower()
	if "." in text:
		raise ValueError("Only one subdomain level is allowed")
	sub = normalize_subdomain(text)
	return sub, f"{sub}.{normalize_root_domain(root_domain)}"


def subdomain_status(value: Any, root_domain: Any, is_taken: Callable[[str], bool]) -> dict[str, Any]:
	"""Live availability check result for the signup form."""
	try:
		sub, site = site_name_for(value, root_domain)
	except ValueError as exc:
		return {"available": False, "subdomain": None, "site_name": None, "reason": str(exc)}
	if is_taken(site):
		return {"available": False, "subdomain": sub, "site_name": site, "reason": "This subdomain is already taken"}
	return {"available": True, "subdomain": sub, "site_name": site, "reason": None}


# --------------------------------------------------------------------------- bench selection


def select_bench(
	benches: Iterable[Mapping[str, Any]], site_counts: Mapping[str, int], strategy: str = "Least Sites"
) -> str | None:
	"""Bench for a new SaaS site: Active bench on an Active server with the fewest
	non-archived sites (ties: oldest bench name first). None when nothing is usable."""
	if strategy not in BENCH_SELECTION_STRATEGIES:
		raise ValueError(f"Unknown bench selection strategy {strategy}")
	usable = [
		b for b in benches
		if b.get("status") == "Active" and (b.get("server_status") or "Active") == "Active"
	]
	if not usable:
		return None
	usable.sort(key=lambda b: (int(site_counts.get(b["name"], 0)), str(b["name"])))
	return usable[0]["name"]


# --------------------------------------------------------------------------- trials


def as_date(value: Any) -> date | None:
	if value in (None, ""):
		return None
	if isinstance(value, datetime):
		return value.date()
	if isinstance(value, date):
		return value
	return date.fromisoformat(str(value)[:10])


def trial_end_date(start: Any, trial_days: Any) -> date | None:
	"""First billable day: `start` + trial_days. None when the product has no trial."""
	days = int(trial_days or 0)
	if days <= 0:
		return None
	return as_date(start) + timedelta(days=days)


def billing_start_date(today: Any, trial_end: Any) -> date:
	"""Subscriptions of a site start on its trial end (or today when there is no trial)."""
	today, trial_end = as_date(today), as_date(trial_end)
	if trial_end and trial_end > today:
		return trial_end
	return today


def in_trial(trial_end: Any, today: Any) -> bool:
	trial_end = as_date(trial_end)
	return bool(trial_end and as_date(today) < trial_end)


def trial_action(
	trial_end: Any,
	today: Any,
	has_payment: bool,
	state: str | None,
	site_status: str | None = "Active",
	grace_days: int = TRIAL_GRACE_DAYS,
) -> str | None:
	"""What expire_trials does for one site: None, "notify", "suspend", "convert", "unsuspend".

	- still in trial / no trial                  -> None
	- team can pay (payment method or credits)   -> "convert" (or "unsuspend" if we suspended it)
	- trial over, not notified yet               -> "notify" (e-mail, grace period starts)
	- notified and grace_days after trial end     -> "suspend" (Site Suspended + maintenance_mode)
	"""
	trial_end, today = as_date(trial_end), as_date(today)
	state = state or ""
	if not trial_end or today < trial_end or state == TRIAL_CONVERTED:
		return None
	if has_payment:
		if state == TRIAL_SUSPENDED:
			return "unsuspend" if site_status == "Suspended" else "convert"
		return "convert"
	if state == "":
		return "notify"
	if state == TRIAL_NOTIFIED and today >= trial_end + timedelta(days=grace_days) and site_status == "Active":
		return "suspend"
	return None


def team_can_pay(credit_balance: Any, successful_payments: int) -> bool:
	"""A team "has a payment method" when it paid through a gateway once or holds credits."""
	try:
		credits = float(credit_balance or 0)
	except (TypeError, ValueError):
		credits = 0.0
	return credits > 0 or int(successful_payments or 0) > 0


# --------------------------------------------------------------------------- signup status


def verification_expired(expires_on: datetime | None, now: datetime) -> bool:
	return expires_on is None or expires_on <= now


def next_signup_status(current: str, site_status: str | None, create_job_status: str | None) -> str:
	"""Advance a Provisioning signup from its Site / site.create job state."""
	if current != PROVISIONING:
		return current
	if site_status == "Active":
		return ACTIVE
	if site_status in ("Broken", "Archived") or create_job_status == "Failure":
		return FAILED
	return current
