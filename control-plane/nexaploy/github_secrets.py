"""Clone credentials for custom apps, resolved only when the build server polls its job.

A Deploy Candidate stores a credential-free `apps_json` plus non-secret `app_sources`
(custom app, installation, repo). Its `image.build` Agent Job carries the placeholder

    __secret__:github:<deploy candidate>:apps_json

instead of the apps list. `nexaploy.api.agent.poll` resolves placeholders through
`_resolve_secret(doctype, name, fieldname)`; for the pseudo-doctype ``github`` it calls
:func:`resolve`, which mints a fresh, repository-scoped, read-only GitHub App installation
token (valid 1 h) per installation and returns apps.json with
``https://x-access-token:<token>@github.com/owner/repo`` URLs (or the Custom App's
`access_token` for other providers). Nothing tokenised is ever written to the database.
"""

import frappe

from nexaploy.github_utils import inject_tokens
from nexaploy.utils import CUSTOM_SOURCE, SECRET_PREFIX

SECRET_KIND = "github"


def apps_json_secret_ref(deploy_candidate):
	if ":" in deploy_candidate:
		raise ValueError("deploy candidate names must not contain ':'")
	return f"{SECRET_PREFIX}{SECRET_KIND}:{deploy_candidate}:apps_json"


def needs_clone_tokens(sources):
	return any(s.get("source_type") == CUSTOM_SOURCE for s in sources or ())


def resolve(name, fieldname):
	"""Resolver for `__secret__:github:<name>:<fieldname>` (only `apps_json` exists)."""
	if fieldname != "apps_json":
		return None
	dc = frappe.db.get_value("Deploy Candidate", name, ["apps_json", "app_sources"], as_dict=True)
	if not dc:
		return []
	redacted = frappe.parse_json(dc.apps_json or "[]") or []
	sources = frappe.parse_json(dc.app_sources or "[]") or []
	try:
		return build_tokenised_apps_json(sources)
	except Exception:
		# Never break poll for the other jobs: hand out the credential-free list; the clone of
		# a private repo then fails visibly in the build log.
		frappe.log_error(title=f"Nexaploy: could not mint clone tokens for Deploy Candidate {name}")
		return redacted


def build_tokenised_apps_json(sources):
	from nexaploy.api.github import installation_token

	# one scoped token per installation covering all of its repos in this build
	repos_by_installation = {}
	for src in sources:
		if src.get("source_type") == CUSTOM_SOURCE and src.get("provider") == "GitHub" and src.get("installation"):
			repo = (src.get("repo") or "").split("/")[-1]
			repos_by_installation.setdefault(src["installation"], set()).add(repo)
	tokens = {
		inst: installation_token(inst, repositories=sorted(repos), cache=False)
		for inst, repos in repos_by_installation.items()
	}

	def token_for(src):
		if src.get("provider") == "GitHub":
			return tokens.get(src.get("installation"))
		if src.get("custom_app"):
			from frappe.utils.password import get_decrypted_password

			return get_decrypted_password("Custom App", src["custom_app"], "access_token", raise_exception=False)
		return None

	return inject_tokens(sources, token_for)
