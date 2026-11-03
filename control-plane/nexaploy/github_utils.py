"""Pure GitHub helpers for custom apps (no frappe import; tested in tests/test_github.py).

- GitHub App JWT (RS256): PyJWT if installed, else `cryptography` directly.
- Repository URL parsing, branch validation, authenticated clone URLs + redaction.
- Webhook signature verification (X-Hub-Signature-256).
- Signed install `state` tokens (team + user, HMAC-SHA256, expiring).
- Frappe app name / Frappe major detection from repository files.
- Push event parsing and token injection into apps.json.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote, urlsplit, urlunsplit

GITHUB_HOST = "github.com"
GITHUB_API = "https://api.github.com"
SUPPORTED_MAJORS = ("14", "15", "16")

APP_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,60}$")
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
NULL_SHA = "0" * 40


class GitHubError(Exception):
	"""Raised for invalid input or a missing crypto dependency."""


# --------------------------------------------------------------------------- base64url


def _b64url(data: bytes) -> str:
	return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
	pad = "=" * (-len(text) % 4)
	return base64.urlsafe_b64decode(text + pad)


# --------------------------------------------------------------------------- GitHub App JWT


def app_jwt_claims(app_id: str | int, now: float | None = None, ttl: int = 540) -> dict[str, Any]:
	"""Claims for a GitHub App JWT: iat 60 s in the past (clock drift), exp <= 10 min."""
	if not app_id or not str(app_id).strip():
		raise GitHubError("GitHub App ID (or client ID) is required")
	now = int(time.time() if now is None else now)
	ttl = max(60, min(int(ttl), 600))
	return {"iat": now - 60, "exp": now + ttl, "iss": str(app_id).strip()}


def build_app_jwt(app_id: str | int, private_key_pem: str | bytes, now: float | None = None, ttl: int = 540) -> str:
	"""RS256 JWT used as `Authorization: Bearer <jwt>` for /app/* endpoints.

	Uses PyJWT (`pip install "PyJWT[crypto]"`) when available, else `cryptography`.
	Raises GitHubError with an actionable message when neither is installed.
	"""
	if not private_key_pem:
		raise GitHubError("GitHub App private key is not configured")
	pem = private_key_pem.encode() if isinstance(private_key_pem, str) else private_key_pem
	pem = pem.strip() + b"\n"
	if b"PRIVATE KEY" not in pem:
		raise GitHubError("GitHub App private key must be a PEM file (-----BEGIN RSA PRIVATE KEY-----)")
	claims = app_jwt_claims(app_id, now, ttl)
	try:
		import jwt as pyjwt  # PyJWT

		if hasattr(pyjwt, "encode"):
			token = pyjwt.encode(claims, pem, algorithm="RS256")
			return token.decode() if isinstance(token, bytes) else token
	except ImportError:
		pass
	except Exception as exc:  # NotImplementedError when PyJWT has no crypto backend
		if "cryptography" not in str(exc).lower() and "algorithm" not in str(exc).lower():
			raise GitHubError(f"could not sign GitHub App JWT: {exc}") from exc
	try:
		from cryptography.hazmat.primitives import hashes, serialization
		from cryptography.hazmat.primitives.asymmetric import padding
	except ImportError as exc:
		raise GitHubError(
			"Signing GitHub App JWTs needs the 'cryptography' package "
			'(pip install "PyJWT[crypto]" or pip install cryptography)'
		) from exc
	try:
		key = serialization.load_pem_private_key(pem, password=None)
	except (ValueError, TypeError) as exc:
		raise GitHubError(f"invalid GitHub App private key: {exc}") from exc
	header = {"alg": "RS256", "typ": "JWT"}
	signing_input = (
		_b64url(json.dumps(header, separators=(",", ":")).encode())
		+ "."
		+ _b64url(json.dumps(claims, separators=(",", ":")).encode())
	)
	signature = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
	return signing_input + "." + _b64url(signature)


def decode_jwt_unverified(token: str) -> tuple[dict, dict]:
	"""(header, claims) of a JWT without verifying it (debugging/tests)."""
	parts = token.split(".")
	if len(parts) != 3:
		raise GitHubError("not a JWT")
	return json.loads(_b64url_decode(parts[0])), json.loads(_b64url_decode(parts[1]))


# --------------------------------------------------------------------------- repositories


def parse_repo(value: str | None) -> tuple[str, str, str]:
	"""(host, owner, repo) from 'owner/repo', https URLs (with or without .git / credentials)
	or 'git@host:owner/repo.git'. Raises GitHubError for anything else.
	"""
	text = (value or "").strip()
	if not text:
		raise GitHubError("repository is required")
	host = GITHUB_HOST
	if text.startswith("git@"):
		m = re.match(r"^git@([^:/\s]+):(.+)$", text)
		if not m:
			raise GitHubError(f"unsupported repository URL {redact_url(text)}")
		host, path = m.group(1).lower(), m.group(2)
	elif "://" in text:
		parts = urlsplit(text)
		if parts.scheme not in ("https", "http", "ssh", "git"):
			raise GitHubError(f"unsupported repository URL {redact_url(text)}")
		host = (parts.hostname or "").lower()
		if parts.port:
			host = f"{host}:{parts.port}"
		path = parts.path
		if parts.query or parts.fragment:
			raise GitHubError("repository URL must not have a query or fragment")
	else:
		path = text
	path = path.strip("/")
	if path.endswith(".git"):
		path = path[:-4]
	segments = [s for s in path.split("/") if s]
	if not host or len(segments) < 2:
		raise GitHubError(f"expected owner/repo, got {redact_url(text)}")
	if host == GITHUB_HOST and len(segments) != 2:
		raise GitHubError(f"expected github.com/owner/repo, got {redact_url(text)}")
	owner, repo = "/".join(segments[:-1]), segments[-1]
	if host == GITHUB_HOST and not OWNER_RE.match(owner):
		raise GitHubError(f"invalid GitHub owner {owner!r}")
	if not REPO_RE.match(repo) or repo in (".", ".."):
		raise GitHubError(f"invalid repository name {repo!r}")
	return host, owner, repo


def repo_full_name(value: str | None) -> str:
	"""'owner/repo' for a GitHub repository (see parse_repo)."""
	host, owner, repo = parse_repo(value)
	if host != GITHUB_HOST:
		raise GitHubError("not a github.com repository")
	return f"{owner}/{repo}"


def public_repo_url(value: str | None) -> str:
	"""Canonical credential-free https URL: https://host/owner/repo"""
	host, owner, repo = parse_repo(value)
	return f"https://{host}/{owner}/{repo}"


_BRANCH_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")


def is_valid_branch(name: str | None) -> bool:
	"""git check-ref-format rules (subset) + build.sh's ^[A-Za-z0-9][A-Za-z0-9._/-]*$."""
	if not name or len(name) > 200:
		return False
	if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$", name):
		return False
	if _BRANCH_FORBIDDEN.search(name) or ".." in name or "//" in name or "@{" in name:
		return False
	if name.endswith((".", "/", ".lock")) or name.startswith("-"):
		return False
	if any(part.startswith(".") or part.endswith(".lock") for part in name.split("/")):
		return False
	return True


def authenticated_clone_url(repo_url: str, token: str, username: str = "x-access-token") -> str:
	"""https://<username>:<token>@host/owner/repo for git clone (https only)."""
	if not token:
		raise GitHubError("token is required")
	host, owner, repo = parse_repo(repo_url)
	user = quote(username or "x-access-token", safe="")
	secret = quote(token, safe="")
	return f"https://{user}:{secret}@{host}/{owner}/{repo}"


def github_clone_url(owner_repo: str, token: str) -> str:
	"""https://x-access-token:<token>@github.com/owner/repo"""
	return authenticated_clone_url(f"https://{GITHUB_HOST}/{repo_full_name(owner_repo)}", token)


_CRED_RE = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s\"']+@")
_GH_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")


def redact_url(url: Any) -> Any:
	"""Strip userinfo from a URL: https://x-access-token:T@github.com/a/b -> https://***@github.com/a/b"""
	if not isinstance(url, str):
		return url
	return _CRED_RE.sub(lambda m: m.group("scheme") + "***@", url)


def redact_text(text: Any) -> Any:
	"""Redact URL credentials and bare GitHub tokens (ghp_/ghs_/github_pat_...) in free text."""
	if not isinstance(text, str):
		return text
	return _GH_TOKEN_RE.sub("***", redact_url(text))


def redact_apps_json(apps_json: Any) -> Any:
	"""Deep copy of apps.json (or any JSON value) with URL credentials removed."""
	if isinstance(apps_json, Mapping):
		return {k: redact_apps_json(v) for k, v in apps_json.items()}
	if isinstance(apps_json, list):
		return [redact_apps_json(v) for v in apps_json]
	return redact_text(apps_json)


def inject_tokens(sources: Sequence[Mapping[str, Any]], token_for: Callable[[Mapping[str, Any]], str | None]) -> list[dict[str, str]]:
	"""Build the real apps.json from Deploy Candidate `app_sources`.

	`sources` items: {url, branch, source_type, [custom_app, provider, installation, repo,
	access_username]}. `token_for(source)` returns a token (or None for public repos).
	"""
	out = []
	for src in sources:
		url, branch = src.get("url"), src.get("branch")
		if not url or not branch:
			raise GitHubError("each app source needs url and branch")
		token = token_for(src) if src.get("source_type") == "Custom" else None
		if token:
			username = "x-access-token" if src.get("provider") == "GitHub" else (src.get("access_username") or "oauth2")
			url = authenticated_clone_url(url, token, username)
		out.append({"url": url, "branch": branch})
	return out


# --------------------------------------------------------------------------- webhooks


def webhook_signature(secret: str | bytes, body: bytes) -> str:
	key = secret.encode() if isinstance(secret, str) else secret
	return "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()


def verify_webhook_signature(secret: str | bytes | None, body: bytes | str, signature_header: str | None) -> bool:
	"""Constant-time check of `X-Hub-Signature-256: sha256=<hex>`; False without a secret."""
	if not secret or not signature_header:
		return False
	if isinstance(body, str):
		body = body.encode()
	header = signature_header.strip()
	if not header.lower().startswith("sha256="):
		return False
	expected = webhook_signature(secret, body or b"")
	return hmac.compare_digest(expected, "sha256=" + header[7:].lower())


def parse_push_event(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
	"""{repo, branch, commit, installation_id, pusher} of a branch push; None for tags and
	branch deletions."""
	if not isinstance(payload, Mapping):
		return None
	ref = payload.get("ref") or ""
	if not ref.startswith("refs/heads/") or payload.get("deleted"):
		return None
	commit = payload.get("after") or ""
	if not SHA_RE.match(commit) or commit == NULL_SHA:
		return None
	repo = (payload.get("repository") or {}).get("full_name")
	if not repo:
		return None
	installation = (payload.get("installation") or {}).get("id")
	head = payload.get("head_commit") or {}
	return {
		"repo": repo,
		"branch": ref[len("refs/heads/") :],
		"commit": commit,
		"installation_id": str(installation) if installation else None,
		"message": (head.get("message") or "").split("\n", 1)[0][:140],
		"timestamp": head.get("timestamp"),
	}


# --------------------------------------------------------------------------- install state


def sign_state(team: str, user: str, secret: str | bytes, now: float | None = None, ttl: int = 3600) -> str:
	"""Signed, expiring `state` for the GitHub App install link (binds team + user)."""
	if not team or not user:
		raise GitHubError("team and user are required")
	key = secret.encode() if isinstance(secret, str) else secret
	payload = {
		"t": team,
		"u": user,
		"e": int((time.time() if now is None else now) + ttl),
		"n": secrets.token_hex(6),
	}
	body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
	sig = _b64url(hmac.new(key, body.encode(), hashlib.sha256).digest())
	return f"{body}.{sig}"


def verify_state(state: str | None, secret: str | bytes, now: float | None = None) -> dict[str, str] | None:
	"""{team, user} when `state` is authentic and not expired, else None."""
	if not state or "." not in state or not secret:
		return None
	key = secret.encode() if isinstance(secret, str) else secret
	body, _, sig = state.partition(".")
	expected = _b64url(hmac.new(key, body.encode(), hashlib.sha256).digest())
	if not hmac.compare_digest(expected, sig):
		return None
	try:
		payload = json.loads(_b64url_decode(body))
	except (ValueError, TypeError):
		return None
	if not isinstance(payload, dict) or int(payload.get("e") or 0) < int(time.time() if now is None else now):
		return None
	if not payload.get("t") or not payload.get("u"):
		return None
	return {"team": payload["t"], "user": payload["u"]}


def install_link(app_slug: str, state: str) -> str:
	if not app_slug or not re.match(r"^[a-z0-9][a-z0-9-]*$", app_slug):
		raise GitHubError("GitHub App slug is not configured")
	return f"https://github.com/apps/{app_slug}/installations/new?state={quote(state, safe='')}"


# --------------------------------------------------------------------------- app detection


def normalize_app_name(name: str | None) -> str | None:
	"""PyPI-style project name -> python package name ('My-App' -> 'my_app')."""
	if not name:
		return None
	text = re.sub(r"[-.\s]+", "_", str(name).strip().lower())
	return text if APP_NAME_RE.match(text) else None


def _load_toml(text: str) -> dict:
	try:
		import tomllib  # py3.11+
	except ImportError:  # pragma: no cover - py3.10
		tomllib = None
	if tomllib is not None:
		try:
			return tomllib.loads(text)
		except Exception:
			return {}
	# minimal fallback: only what we read ([project] name, [tool.bench.frappe-dependencies] frappe)
	data: dict = {}
	section: dict = data
	for line in text.splitlines():
		line = line.split("#", 1)[0].strip()
		m = re.match(r"^\[([^\]]+)\]$", line)
		if m:
			section = data
			for part in m.group(1).split("."):
				section = section.setdefault(part.strip().strip('"'), {})
			continue
		m = re.match(r'^([A-Za-z0-9_."-]+)\s*=\s*["\'](.*)["\']$', line)
		if m:
			section[m.group(1).strip('"')] = m.group(2)
	return data


def pyproject_name(text: str | None) -> str | None:
	if not text:
		return None
	return normalize_app_name(((_load_toml(text).get("project") or {}).get("name")))


def setup_py_name(text: str | None) -> str | None:
	if not text:
		return None
	m = re.search(r"""\bname\s*=\s*["']([A-Za-z0-9_.-]+)["']""", text)
	return normalize_app_name(m.group(1)) if m else None


def frappe_dependency(pyproject_text: str | None) -> str | None:
	"""`[tool.bench.frappe-dependencies] frappe = ">=15.0.0,<17.0.0"` -> the spec string."""
	if not pyproject_text:
		return None
	deps = ((_load_toml(pyproject_text).get("tool") or {}).get("bench") or {}).get("frappe-dependencies") or {}
	spec = deps.get("frappe")
	return str(spec).strip() if spec else None


def _version_tuple(text: str) -> tuple[int, int, int]:
	parts = [int(p) for p in re.findall(r"\d+", text)[:3]]
	while len(parts) < 3:
		parts.append(0)
	return tuple(parts)  # type: ignore[return-value]


def majors_from_spec(spec: str | None, supported: Iterable[str] = SUPPORTED_MAJORS) -> list[str]:
	"""Frappe majors that overlap a PEP 440-ish spec ('>=15.0.0,<17.0.0' -> ['15', '16']).

	Supports >=, >, <=, <, ==, !=, ~=, ^ and wildcards like '==15.*'. Unknown syntax -> [].
	"""
	if not spec:
		return []
	# interval [lo, hi) with inclusive flags
	bounds = {"lo": ((0, 0, 0), True), "hi": ((10**6, 0, 0), False)}

	def raise_lo(v, inc):
		cur, cur_inc = bounds["lo"]
		if v > cur or (v == cur and cur_inc and not inc):
			bounds["lo"] = (v, inc)

	def lower_hi(v, inc):
		cur, cur_inc = bounds["hi"]
		if v < cur or (v == cur and cur_inc and not inc):
			bounds["hi"] = (v, inc)

	for clause in [c.strip() for c in spec.split(",") if c.strip()]:
		m = re.match(r"^(>=|<=|==|~=|!=|>|<|\^)?\s*v?(\d+(?:\.\d+)*)(\.\*)?$", clause)
		if not m:
			return []
		op, ver, wildcard = m.group(1) or "==", m.group(2), bool(m.group(3))
		nums = [int(x) for x in ver.split(".")][:3]
		v = _version_tuple(ver)
		if op == "!=":
			continue
		if op == "==" and wildcard:
			upper = nums[:-1] + [nums[-1] + 1]
			raise_lo(v, True)
			lower_hi(_version_tuple(".".join(map(str, upper))), False)
		elif wildcard:
			return []
		elif op == ">=":
			raise_lo(v, True)
		elif op == ">":
			raise_lo(v, False)
		elif op == "<=":
			lower_hi(v, True)
		elif op == "<":
			lower_hi(v, False)
		elif op == "==":
			raise_lo(v, True)
			lower_hi(v, True)
		else:  # ~= / ^
			if op == "^" or len(nums) <= 2:
				upper = (nums[0] + 1, 0, 0)
			else:
				upper = (nums[0], nums[1] + 1, 0)
			raise_lo(v, True)
			lower_hi(upper, False)

	(lo, lo_inc), (hi, hi_inc) = bounds["lo"], bounds["hi"]
	if lo > hi or (lo == hi and not (lo_inc and hi_inc)):
		return []
	result = []
	for major in supported:
		m_lo, m_hi = (int(major), 0, 0), (int(major) + 1, 0, 0)
		if (hi > m_lo or (hi == m_lo and hi_inc)) and lo < m_hi:
			result.append(str(major))
	return result


def majors_from_branch(branch: str | None) -> list[str]:
	"""'version-15' / 'version-15-hotfix' / 'v15' -> ['15']."""
	m = re.match(r"^(?:version-|v)(\d+)(?:\b|[-_.])", branch or "")
	return [m.group(1)] if m and m.group(1) in SUPPORTED_MAJORS else []


def candidate_app_dirs(root_entries: Iterable[Mapping[str, Any] | str]) -> list[str]:
	"""Top-level directories that could be the app package (valid python names)."""
	out = []
	for e in root_entries:
		if isinstance(e, str):
			name, kind = e.rstrip("/"), "dir" if e.endswith("/") else "file"
		else:
			name, kind = e.get("name") or e.get("path") or "", e.get("type") or "file"
		if kind in ("dir", "tree") and APP_NAME_RE.match(name) and name not in ("tests", "docs", "scripts"):
			out.append(name)
	return out


def detect_app_name(files: Iterable[str], contents: Mapping[str, str] | None = None) -> str | None:
	"""Frappe app (python package) name from repository paths + a few file contents.

	`files`: repo-relative paths (e.g. 'pyproject.toml', 'setup.py', 'acme/hooks.py').
	`contents`: {'pyproject.toml': '...', 'setup.py': '...'} when fetched.
	Order: a name from pyproject [project] / setup.py that has '<name>/hooks.py';
	else the only '<dir>/hooks.py'; else the declared name when valid.
	"""
	contents = contents or {}
	paths = {p.strip("/") for p in files}
	hooks_dirs = sorted(
		p.split("/")[0] for p in paths if p.count("/") == 1 and p.endswith("/hooks.py") and APP_NAME_RE.match(p.split("/")[0])
	)
	declared = [n for n in (pyproject_name(contents.get("pyproject.toml")), setup_py_name(contents.get("setup.py"))) if n]
	for name in declared:
		if name in hooks_dirs:
			return name
	if len(hooks_dirs) == 1:
		return hooks_dirs[0]
	if declared and not hooks_dirs:
		return declared[0]
	return None

