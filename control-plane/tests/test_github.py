"""Custom apps from GitHub: pure helpers (nexaploy/github_utils.py + release group logic in
nexaploy/utils.py). No frappe needed."""

import ast
import base64
import json
import os
import sys
import time

import pytest

from nexaploy import github_utils as gu
from nexaploy import utils as u

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "nexaploy")


def test_pure_modules_do_not_import_frappe():
	for name in ("github_utils.py", "utils.py"):
		tree = ast.parse(open(os.path.join(APP, name)).read())
		for node in ast.walk(tree):
			if isinstance(node, ast.Import):
				assert all(not a.name.startswith("frappe") for a in node.names), name
			if isinstance(node, ast.ImportFrom):
				assert not (node.module or "").startswith("frappe"), name


# --------------------------------------------------------------------------- JWT

try:
	from cryptography.hazmat.primitives import hashes, serialization
	from cryptography.hazmat.primitives.asymmetric import padding, rsa

	HAVE_CRYPTO = True
except ImportError:  # pragma: no cover
	HAVE_CRYPTO = False

needs_crypto = pytest.mark.skipif(not HAVE_CRYPTO, reason="cryptography not installed")


@pytest.fixture(scope="module")
def rsa_key():
	key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
	pem = key.private_bytes(
		serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
	).decode()
	return key, pem


def _verify_rs256(token, public_key):
	header_b64, claims_b64, sig_b64 = token.split(".")
	sig = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
	public_key.verify(sig, f"{header_b64}.{claims_b64}".encode(), padding.PKCS1v15(), hashes.SHA256())


def test_jwt_claims():
	c = gu.app_jwt_claims(12345, now=1_000_000)
	assert c == {"iat": 1_000_000 - 60, "exp": 1_000_000 + 540, "iss": "12345"}
	# GitHub rejects exp more than 10 minutes in the future
	assert gu.app_jwt_claims(1, now=0, ttl=3600)["exp"] == 600
	with pytest.raises(gu.GitHubError):
		gu.app_jwt_claims("")


@needs_crypto
def test_build_app_jwt_verifies(rsa_key):
	key, pem = rsa_key
	now = time.time()
	token = gu.build_app_jwt("98765", pem, now=now)
	header, claims = gu.decode_jwt_unverified(token)
	assert header["alg"] == "RS256"
	assert claims["iss"] == "98765"
	assert claims["iat"] <= now and claims["exp"] - claims["iat"] <= 660
	_verify_rs256(token, key.public_key())


@needs_crypto
def test_build_app_jwt_without_pyjwt(rsa_key, monkeypatch):
	"""Falls back to `cryptography` directly when PyJWT is missing."""
	key, pem = rsa_key
	monkeypatch.setitem(sys.modules, "jwt", None)  # import jwt -> ImportError
	token = gu.build_app_jwt(42, pem.encode(), now=time.time())
	_verify_rs256(token, key.public_key())
	assert gu.decode_jwt_unverified(token)[1]["iss"] == "42"


def test_build_app_jwt_rejects_bad_keys():
	with pytest.raises(gu.GitHubError, match="not configured"):
		gu.build_app_jwt(1, "")
	with pytest.raises(gu.GitHubError, match="PEM"):
		gu.build_app_jwt(1, "not a key")


@needs_crypto
def test_build_app_jwt_invalid_pem(monkeypatch):
	monkeypatch.setitem(sys.modules, "jwt", None)
	with pytest.raises(gu.GitHubError, match="invalid GitHub App private key"):
		gu.build_app_jwt(1, "-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----")


def test_build_app_jwt_missing_crypto(monkeypatch):
	for mod in ("jwt", "cryptography", "cryptography.hazmat.primitives"):
		monkeypatch.setitem(sys.modules, mod, None)
	pem = "-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----"
	with pytest.raises(gu.GitHubError, match="cryptography"):
		gu.build_app_jwt(1, pem)


# --------------------------------------------------------------------------- repo URLs


@pytest.mark.parametrize(
	"value",
	[
		"acme/acme_app",
		"https://github.com/acme/acme_app",
		"https://github.com/acme/acme_app.git",
		"https://github.com/acme/acme_app/",
		"http://github.com/acme/acme_app",
		"git@github.com:acme/acme_app.git",
		"https://x-access-token:ghs_abc@github.com/acme/acme_app",
		"ssh://git@github.com/acme/acme_app.git",
	],
)
def test_parse_repo_github(value):
	assert gu.parse_repo(value) == ("github.com", "acme", "acme_app")
	assert gu.repo_full_name(value) == "acme/acme_app"
	assert gu.public_repo_url(value) == "https://github.com/acme/acme_app"


def test_parse_repo_other_hosts():
	assert gu.parse_repo("https://gitlab.com/group/sub/app.git") == ("gitlab.com", "group/sub", "app")
	assert gu.parse_repo("https://git.example.com:8443/team/app") == ("git.example.com:8443", "team", "app")
	with pytest.raises(gu.GitHubError):
		gu.repo_full_name("https://gitlab.com/group/app")


@pytest.mark.parametrize(
	"value",
	["", "acme", "https://github.com/acme", "https://github.com/a/b/c", "file:///etc/passwd",
	 "https://github.com/acme/app?x=1", "https://github.com/-bad/app", "https://github.com/acme/..",
	 "acme/app name"],
)
def test_parse_repo_rejects(value):
	with pytest.raises(gu.GitHubError):
		gu.parse_repo(value)


@pytest.mark.parametrize("name", ["main", "version-15", "feature/x-1", "release_2026.11", "v15.2"])
def test_valid_branches(name):
	assert gu.is_valid_branch(name)


@pytest.mark.parametrize(
	"name",
	["", "-x", "a..b", "a b", "a~1", "a^", "a:b", "a?", "a*", "a[", "x.lock", "a/.hidden", "a/", "a//b",
	 "a@{1}", "$(id)", "main;rm", "a\\b", "x" * 201],
)
def test_invalid_branches(name):
	assert not gu.is_valid_branch(name)


def test_clone_url_and_redaction():
	url = gu.github_clone_url("acme/acme_app", "ghs_SECRET123")
	assert url == "https://x-access-token:ghs_SECRET123@github.com/acme/acme_app"
	assert gu.redact_url(url) == "https://***@github.com/acme/acme_app"
	assert "SECRET" not in gu.redact_text(f"Cloning into 'x'... {url} failed")
	# bare tokens
	assert gu.redact_text("token ghp_" + "A" * 36 + " end") == "token *** end"
	assert gu.redact_text("github_pat_" + "a1_" * 10) == "***"
	# other providers / usernames, special characters are quoted
	other = gu.authenticated_clone_url("https://gitlab.com/g/app.git", "t:o/k@n", "oauth2")
	assert other == "https://oauth2:t%3Ao%2Fk%40n@gitlab.com/g/app"
	assert gu.redact_url(other) == "https://***@gitlab.com/g/app"
	assert gu.redact_url("https://github.com/acme/app") == "https://github.com/acme/app"
	assert gu.redact_url(None) is None
	with pytest.raises(gu.GitHubError):
		gu.authenticated_clone_url("https://github.com/a/b", "")


def test_redact_apps_json_deep():
	apps = [{"url": gu.github_clone_url("a/b", "ghs_X"), "branch": "main"}, {"url": "https://github.com/c/d", "branch": "v"}]
	red = gu.redact_apps_json(apps)
	assert red == [{"url": "https://***@github.com/a/b", "branch": "main"}, {"url": "https://github.com/c/d", "branch": "v"}]
	assert apps[0]["url"].startswith("https://x-access-token:")  # input untouched


def test_inject_tokens():
	sources = [
		{"source_type": "Marketplace", "url": "https://github.com/frappe/erpnext", "branch": "version-15"},
		{"source_type": "Custom", "provider": "GitHub", "installation": "1", "repo": "acme/a", "url": "https://github.com/acme/a", "branch": "main"},
		{"source_type": "Custom", "provider": "Other", "custom_app": "x", "access_username": "oauth2", "url": "https://gitlab.com/g/b", "branch": "dev"},
		{"source_type": "Custom", "provider": "Other", "custom_app": "y", "url": "https://gitlab.com/g/public", "branch": "main"},
	]
	tokens = {"1": "ghs_T", "x": "glpat_Y"}
	out = gu.inject_tokens(sources, lambda s: tokens.get(s.get("installation") or s.get("custom_app")))
	assert out == [
		{"url": "https://github.com/frappe/erpnext", "branch": "version-15"},
		{"url": "https://x-access-token:ghs_T@github.com/acme/a", "branch": "main"},
		{"url": "https://oauth2:glpat_Y@gitlab.com/g/b", "branch": "dev"},
		{"url": "https://gitlab.com/g/public", "branch": "main"},
	]
	with pytest.raises(gu.GitHubError):
		gu.inject_tokens([{"source_type": "Custom", "url": "https://github.com/a/b"}], lambda s: "t")


# --------------------------------------------------------------------------- webhooks & state


def test_webhook_signature():
	body = json.dumps({"ref": "refs/heads/main"}).encode()
	sig = gu.webhook_signature("s3cret", body)
	assert sig.startswith("sha256=") and len(sig) == 71
	assert gu.verify_webhook_signature("s3cret", body, sig)
	assert gu.verify_webhook_signature(b"s3cret", body.decode(), sig.upper().replace("SHA256=", "sha256="))
	assert not gu.verify_webhook_signature("other", body, sig)
	assert not gu.verify_webhook_signature("s3cret", body + b" ", sig)
	assert not gu.verify_webhook_signature("s3cret", body, None)
	assert not gu.verify_webhook_signature("", body, sig)
	assert not gu.verify_webhook_signature(None, body, sig)
	assert not gu.verify_webhook_signature("s3cret", body, "sha1=" + sig[7:])
	# GitHub's documented example
	assert gu.webhook_signature("It's a Secret to Everybody", b"Hello, World!") == (
		"sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
	)


def test_parse_push_event():
	sha = "a" * 40
	payload = {
		"ref": "refs/heads/main",
		"after": sha,
		"repository": {"full_name": "Acme/acme_app"},
		"installation": {"id": 777},
		"head_commit": {"message": "Fix invoices\n\nlong body", "timestamp": "2026-11-20T10:00:00Z"},
	}
	ev = gu.parse_push_event(payload)
	assert ev["repo"] == "Acme/acme_app" and ev["branch"] == "main" and ev["commit"] == sha
	assert ev["installation_id"] == "777" and ev["message"] == "Fix invoices"
	assert gu.parse_push_event(dict(payload, ref="refs/tags/v1")) is None
	assert gu.parse_push_event(dict(payload, deleted=True)) is None
	assert gu.parse_push_event(dict(payload, after="0" * 40)) is None
	assert gu.parse_push_event(dict(payload, repository={})) is None
	assert gu.parse_push_event(None) is None


def test_install_state_roundtrip():
	key = b"k" * 32
	state = gu.sign_state("acme", "a@x.com", key, now=1000, ttl=3600)
	assert gu.verify_state(state, key, now=2000) == {"team": "acme", "user": "a@x.com"}
	assert gu.verify_state(state, key, now=1000 + 3601) is None  # expired
	assert gu.verify_state(state, b"other", now=2000) is None
	body, sig = state.split(".")
	forged = base64.urlsafe_b64encode(json.dumps({"t": "evil", "u": "a@x.com", "e": 9999999999, "n": "x"}).encode()).decode().rstrip("=")
	assert gu.verify_state(f"{forged}.{sig}", key, now=2000) is None
	assert gu.verify_state("garbage", key) is None and gu.verify_state(None, key) is None
	assert gu.sign_state("acme", "a@x.com", key) != gu.sign_state("acme", "a@x.com", key)  # nonce
	link = gu.install_link("nexaploy-cloud", state)
	assert link.startswith("https://github.com/apps/nexaploy-cloud/installations/new?state=")
	with pytest.raises(gu.GitHubError):
		gu.install_link("", state)


# --------------------------------------------------------------------------- app detection

PYPROJECT = """
[project]
name = "acme-erp"
version = "0.0.1"
dependencies = []

[tool.bench.frappe-dependencies]
frappe = ">=15.0.0,<17.0.0"
"""


def test_detect_from_pyproject():
	files = ["pyproject.toml", "README.md", "acme_erp/hooks.py", "acme_erp/__init__.py"]
	assert gu.detect_app_name(files, {"pyproject.toml": PYPROJECT}) == "acme_erp"
	assert gu.pyproject_name(PYPROJECT) == "acme_erp"
	assert gu.frappe_dependency(PYPROJECT) == ">=15.0.0,<17.0.0"


def test_detect_from_setup_py():
	setup = "from setuptools import setup\nsetup(name='hr_extras', version='1')\n"
	assert gu.setup_py_name(setup) == "hr_extras"
	assert gu.detect_app_name(["setup.py", "hr_extras/hooks.py", "other/hooks.py"], {"setup.py": setup}) == "hr_extras"


def test_detect_from_hooks_only_and_ambiguous():
	assert gu.detect_app_name(["README.md", "my_app/hooks.py"]) == "my_app"
	assert gu.detect_app_name(["a_app/hooks.py", "b_app/hooks.py"]) is None
	# declared name without hooks listing (hooks not fetched) is still used
	assert gu.detect_app_name(["pyproject.toml"], {"pyproject.toml": PYPROJECT}) == "acme_erp"
	# declared name that does not match the hooks dir -> the hooks dir wins
	assert gu.detect_app_name(["pyproject.toml", "real_app/hooks.py"], {"pyproject.toml": PYPROJECT}) == "real_app"
	assert gu.detect_app_name([]) is None
	assert gu.detect_app_name(["Bad-Dir/hooks.py"]) is None


def test_candidate_dirs():
	entries = [
		{"name": "acme_erp", "type": "dir"}, {"name": "tests", "type": "dir"}, {"name": ".github", "type": "dir"},
		{"name": "setup.py", "type": "file"}, {"name": "Docs", "type": "dir"}, "other_app/",
	]
	assert gu.candidate_app_dirs(entries) == ["acme_erp", "other_app"]


@pytest.mark.parametrize(
	"spec,expected",
	[
		(">=15.0.0,<17.0.0", ["15", "16"]),
		("~=15.0", ["15"]),
		("~=15.1.2", ["15"]),
		(">=14", ["14", "15", "16"]),
		("<16", ["14", "15"]),
		("==15.*", ["15"]),
		("^16.0.0", ["16"]),
		(">=15.5,<15.7", ["15"]),
		(">=13.0.0,<15.0.0", ["14"]),
		("<=15.0.0", ["14", "15"]),
		(">15,<15", []),
		("junk", []),
		("", []),
		(None, []),
	],
)
def test_majors_from_spec(spec, expected):
	assert gu.majors_from_spec(spec) == expected


def test_majors_from_branch():
	assert gu.majors_from_branch("version-15") == ["15"]
	assert gu.majors_from_branch("version-16-hotfix") == ["16"]
	assert gu.majors_from_branch("v14") == ["14"]
	assert gu.majors_from_branch("main") == []
	assert gu.majors_from_branch("version-13") == []


def test_normalize_app_name():
	assert gu.normalize_app_name("Acme-ERP") == "acme_erp"
	assert gu.normalize_app_name("acme.erp") == "acme_erp"
	assert gu.normalize_app_name("1app") is None
	assert gu.normalize_app_name(None) is None


# --------------------------------------------------------------------------- release groups (utils)


def market(name, majors=("15",), approved=True, branch=None):
	return {
		"source_type": "Marketplace", "app_name": name, "approved": approved, "branch": branch,
		"repo_url": f"https://github.com/frappe/{name}",
		"releases": [{"frappe_major": m, "branch": f"version-{m}", "commit": None} for m in majors],
	}


def custom(name, team="acme", majors="15,16", status="Active", branch="main", ca=None):
	return {
		"source_type": "Custom", "custom_app": ca or f"ca-{name}", "app_name": name, "team": team,
		"status": status, "frappe_majors": majors, "repo_url": f"https://github.com/acme/{name}",
		"repo": f"acme/{name}", "branch": branch, "provider": "GitHub", "installation": "777",
	}


def test_parse_frappe_majors():
	assert u.parse_frappe_majors("16, 14,15") == ["14", "15", "16"]
	assert u.parse_frappe_majors("v15 15;13") == ["15"]
	assert u.parse_frappe_majors(["16"]) == ["16"]
	assert u.parse_frappe_majors("") == [] and u.parse_frappe_majors(None) == []


def test_group_apps_ok():
	rows = [market("erpnext"), custom("acme_erp")]
	assert u.check_group_apps("15", "acme", rows) == []
	# custom app without declared majors is not restricted
	assert u.check_group_apps("14", "acme", [custom("acme_erp", majors="")]) == []


def test_group_apps_marketplace_rules_unchanged():
	errors = u.check_group_apps("14", "acme", [market("erpnext", majors=("15",)), market("hrms", approved=False, majors=("14",))])
	assert "App erpnext has no release for Frappe 14" in errors
	assert "App hrms is not approved in the marketplace" in errors
	assert u.check_group_apps("15", "acme", [market("erpnext")]) == u.check_compatibility("15", [market("erpnext")])


def test_group_apps_custom_rules():
	errs = u.check_group_apps("15", "acme", [custom("x_app", team="other")])
	assert errs == ["Custom app x_app belongs to another team"]
	# shared (teamless) release groups can never use custom apps
	assert "Custom app x_app belongs to another team" in u.check_group_apps("15", None, [custom("x_app")])
	assert u.check_group_apps("14", "acme", [custom("x_app", majors="15,16")]) == [
		"Custom app x_app does not support Frappe 14 (supports 15, 16)"
	]
	assert u.check_group_apps("15", "acme", [custom("x_app", status="Disabled")]) == ["Custom app x_app is disabled"]
	assert "App erpnext is listed more than once" in u.check_group_apps("15", "acme", [market("erpnext"), custom("erpnext")])
	assert "App x_app is listed more than once" in u.check_group_apps("15", "acme", [custom("x_app", ca="a"), custom("x_app", ca="b")])
	assert any("frappe" in e for e in u.check_group_apps("15", "acme", [custom("frappe")]))
	assert u.check_group_apps("15", "acme", [{"source_type": "Custom", "app_name": "y"}]) == ["App y: select a custom app"]
	assert u.check_group_apps("15", "acme", [{"source_type": "Git", "app_name": "y"}])[0] == "Unknown app source 'Git'"
	assert "Custom app x_app has no branch" in u.check_group_apps("15", "acme", [custom("x_app", branch="")])


def test_build_group_apps_json_never_has_tokens():
	rows = [market("erpnext"), custom("acme_erp")]
	apps_json, sources = u.build_group_apps_json(rows, "15")
	assert apps_json == [
		{"url": "https://github.com/frappe/erpnext", "branch": "version-15"},
		{"url": "https://github.com/acme/acme_erp", "branch": "main"},
	]
	assert "@" not in json.dumps(apps_json)
	assert sources[0] == {"source_type": "Marketplace", "app_name": "erpnext", **apps_json[0]}
	assert sources[1]["custom_app"] == "ca-acme_erp" and sources[1]["installation"] == "777"
	assert sources[1]["repo"] == "acme/acme_erp" and sources[1]["url"] == apps_json[1]["url"]
	# what the build server receives after poll-time injection
	real = gu.inject_tokens(sources, lambda s: "ghs_T")
	assert real[1]["url"] == "https://x-access-token:ghs_T@github.com/acme/acme_erp"
	assert gu.redact_apps_json(real) != real and "ghs_T" not in json.dumps(gu.redact_apps_json(real))
	with pytest.raises(ValueError):
		u.build_group_apps_json([custom("x", branch="")], "15")


def test_custom_app_needs_update():
	rows = [
		{"parent": "acme-v15", "branch": "main", "repo": "Acme/acme_erp"},
		{"parent": "acme-v15", "branch": "main", "repo": "acme/acme_erp"},
		{"parent": "acme-dev", "branch": "develop", "repo": "acme/acme_erp"},
		{"parent": "other", "branch": "main", "repo": "acme/other"},
	]
	assert u.custom_app_needs_update(rows, "acme/acme_erp", "main") == ["acme-v15"]
	assert u.custom_app_needs_update(rows, "acme/acme_erp", "develop") == ["acme-dev"]
	assert u.custom_app_needs_update(rows, "acme/acme_erp", "feature") == []


def test_unique_bench_name():
	n = u.unique_bench_name("acme-v15", [], "ab12")
	assert n == "acme-v15-ab12" and u.is_valid_bench_name(n)
	long = "a" * 31
	n = u.unique_bench_name(long, [], "beef")
	assert u.is_valid_bench_name(n) and n.endswith("-beef")
	n2 = u.unique_bench_name("acme-v15", ["acme-v15-ab12"], "ab12")
	assert n2 != "acme-v15-ab12" and u.is_valid_bench_name(n2)


def test_secret_placeholder_shape():
	"""The github placeholder must parse with the existing poll-time resolver contract."""
	ref = f"{u.SECRET_PREFIX}github:abc123:apps_json"
	assert u.parse_secret_ref(ref) == ("github", "abc123", "apps_json")
	resolved = u.resolve_secrets({"apps_json": ref, "tag": "x"}, lambda d, n, f: [{"url": "u", "branch": "b"}] if d == "github" else None)
	assert resolved == {"apps_json": [{"url": "u", "branch": "b"}], "tag": "x"}
	src = open(os.path.join(APP, "github_secrets.py")).read()
	assert 'SECRET_KIND = "github"' in src
