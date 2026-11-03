"""Pure helpers for the customer portal (/dashboard).

This module MUST NOT import frappe: it is unit-tested with plain pytest
(control-plane/tests/test_portal.py). nexaploy/api/dashboard.py does the database work.
"""

from __future__ import annotations

import json
import re
import secrets
import string
from datetime import datetime
from typing import Any, Iterable, Mapping
from urllib.parse import quote

from nexaploy.utils import is_valid_site_name

DEFAULT_ROOT_DOMAIN = "nexaploy.cloud"
ROOT_DOMAIN_CONFIG_KEY = "nexaploy_root_domain"

PORTAL_ROLES = ("Nexaploy Customer", "System Manager")
ADMIN_ROLES = ("System Manager",)

RTL_LANGS = ("ar", "he", "fa", "ur")

TERMINAL_JOB_STATUSES = ("Success", "Failure")

# Subdomains that must not be handed out to customers on the shared root domain.
RESERVED_SUBDOMAINS = frozenset(
	{
		"www", "api", "app", "admin", "administrator", "cp", "control", "dashboard", "portal",
		"mail", "smtp", "imap", "pop", "ftp", "ns", "ns1", "ns2", "dns", "status", "docs", "help",
		"support", "billing", "login", "auth", "sso", "static", "assets", "cdn", "files", "blog",
		"marketplace", "registry", "agent", "install", "root", "test", "staging", "dev",
	}
)

SUBDOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{1,61}[a-z0-9])$")


# --------------------------------------------------------------------------- site names


def normalize_root_domain(value: Any) -> str:
	"""site_config `nexaploy_root_domain`, lowercased, without dots at the ends."""
	root = str(value or "").strip().lower().strip(".")
	if not root or not is_valid_site_name(root):
		return DEFAULT_ROOT_DOMAIN
	return root


def normalize_subdomain(value: Any) -> str:
	"""'  My Shop_1 ' -> 'my-shop-1'. Raises ValueError when nothing valid is left.

	Rules: lowercase a-z, 0-9 and '-', 3..63 chars, no leading/trailing '-', not reserved.
	"""
	text = str(value or "").strip().lower()
	text = re.sub(r"[\s_.]+", "-", text)
	text = re.sub(r"[^a-z0-9-]", "", text)
	text = re.sub(r"-{2,}", "-", text).strip("-")
	if len(text) < 3:
		raise ValueError("Subdomain must have at least 3 characters (a-z, 0-9, -)")
	if len(text) > 63:
		raise ValueError("Subdomain must have at most 63 characters")
	if not SUBDOMAIN_RE.match(text):
		raise ValueError("Invalid subdomain")
	if text in RESERVED_SUBDOMAINS:
		raise ValueError("This subdomain is reserved")
	return text


def build_site_name(value: Any, root_domain: Any = None, allow_custom: bool = False) -> str:
	"""Turn what the customer typed into the Site name (FQDN).

	- 'shop'                    -> 'shop.<root>'
	- 'shop.<root>'             -> 'shop.<root>'
	- 'erp.example.com'         -> 'erp.example.com' only when allow_custom (own server),
	                                ValueError otherwise.
	"""
	root = normalize_root_domain(root_domain)
	text = str(value or "").strip().lower().strip(".")
	if not text:
		raise ValueError("Site name is required")
	if text.endswith("." + root):
		sub = text[: -(len(root) + 1)]
		if "." in sub:
			raise ValueError("Only one subdomain level is allowed")
		return f"{normalize_subdomain(sub)}.{root}"
	if "." in text:
		if not allow_custom:
			raise ValueError("Custom domains are only allowed on your own servers; use a subdomain")
		if not is_valid_site_name(text):
			raise ValueError("Site name must be a lowercase FQDN, e.g. erp.example.com")
		return text
	return f"{normalize_subdomain(text)}.{root}"


# --------------------------------------------------------------------------- passwords

PASSWORD_ALPHABET = "".join(c for c in string.ascii_letters + string.digits if c not in "lIO0o1")


def generate_password(length: int = 16) -> str:
	"""Random admin password with at least one lowercase, uppercase and digit."""
	length = max(int(length), 12)
	while True:
		pwd = "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))
		if any(c.islower() for c in pwd) and any(c.isupper() for c in pwd) and any(c.isdigit() for c in pwd):
			return pwd


# --------------------------------------------------------------------------- request parsing


def parse_app_list(apps: Any) -> list[str]:
	"""Accept a list, a JSON list string or a comma separated string; dedupe, keep order."""
	if apps is None or apps == "":
		return []
	if isinstance(apps, str):
		text = apps.strip()
		if text.startswith("["):
			try:
				apps = json.loads(text)
			except ValueError as exc:
				raise ValueError("apps must be a list") from exc
		else:
			apps = text.split(",")
	if not isinstance(apps, (list, tuple)):
		raise ValueError("apps must be a list")
	result: list[str] = []
	for app in apps:
		name = str(app or "").strip()
		if name and name not in result:
			result.append(name)
	return result


def to_bool(value: Any, default: bool = False) -> bool:
	if value is None or value == "":
		return default
	if isinstance(value, bool):
		return value
	if isinstance(value, (int, float)):
		return bool(value)
	return str(value).strip().lower() in ("1", "true", "yes", "on")


# Site actions the portal may trigger -> allowed keyword arguments.
SITE_ACTIONS: dict[str, tuple[str, ...]] = {
	"backup": ("with_files",),
	"install_app": ("app",),
	"uninstall_app": ("app",),
	"add_domain": ("domain",),
	"remove_domain": ("domain",),
	"migrate": (),
	"drop": ("skip_backup",),
	"restore": ("backup",),
}
REQUIRED_ACTION_ARGS = {
	"install_app": ("app",),
	"uninstall_app": ("app",),
	"add_domain": ("domain",),
	"remove_domain": ("domain",),
	"restore": ("backup",),
}


def filter_action_args(action: str, args: Mapping[str, Any] | None) -> dict[str, Any]:
	"""Keep only the arguments `action` accepts; ValueError for unknown actions/missing args."""
	if action not in SITE_ACTIONS:
		raise ValueError(f"Unknown action {action}")
	args = args or {}
	clean = {k: args[k] for k in SITE_ACTIONS[action] if k in args and args[k] not in (None, "")}
	for key in REQUIRED_ACTION_ARGS.get(action, ()):
		if key not in clean:
			raise ValueError(f"{action} needs {key}")
	for key in ("app", "domain", "backup"):
		if key in clean:
			clean[key] = str(clean[key]).strip()
	if "domain" in clean:
		clean["domain"] = clean["domain"].lower()
	return clean


# --------------------------------------------------------------------------- page helpers


def text_direction(lang: Any) -> str:
	base = str(lang or "en").lower().replace("_", "-").split("-")[0]
	return "rtl" if base in RTL_LANGS else "ltr"


def safe_redirect_path(path: Any, default: str = "/dashboard") -> str:
	"""Only allow local absolute paths (no scheme, no '//host')."""
	text = str(path or "").strip()
	if not text.startswith("/") or text.startswith("//") or "\\" in text or "\n" in text or "\r" in text:
		return default
	return text


def login_redirect_url(path: Any, query: Any = None) -> str:
	target = safe_redirect_path(path)
	if query:
		target = f"{target}?{query if isinstance(query, str) else query.decode()}"
	return "/login?redirect-to=" + quote(target, safe="/")


def has_portal_access(roles: Iterable[str], user: str | None = None) -> bool:
	if user == "Administrator":
		return True
	roles = set(roles or ())
	return any(r in roles for r in PORTAL_ROLES)


def is_portal_admin(roles: Iterable[str], user: str | None = None) -> bool:
	if user == "Administrator":
		return True
	return any(r in set(roles or ()) for r in ADMIN_ROLES)


def is_job_active(status: Any) -> bool:
	return bool(status) and status not in TERMINAL_JOB_STATUSES


def seconds_since(then: datetime | None, now: datetime) -> int | None:
	if not then:
		return None
	return max(int((now - then).total_seconds()), 0)


def summarize_counts(rows: Iterable[Mapping[str, Any]], key: str = "status") -> dict[str, int]:
	counts: dict[str, int] = {}
	for row in rows:
		value = row.get(key) or "Unknown"
		counts[value] = counts.get(value, 0) + 1
	return counts


def pick_default_plan(plans: Iterable[Mapping[str, Any]]) -> str | None:
	"""Cheapest enabled plan (by price_monthly, then title)."""
	enabled = [p for p in plans if p.get("enabled", 1)]
	if not enabled:
		return None
	enabled.sort(key=lambda p: (float(p.get("price_monthly") or 0), str(p.get("title") or p.get("name"))))
	return enabled[0].get("name") or enabled[0].get("title")
