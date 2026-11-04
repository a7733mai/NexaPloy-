"""Backup destinations (S3 / Google Drive / OneDrive / Dropbox): pure helpers, no frappe import.

A **Backup Destination** (team-owned DocType) is where a server's backups go. S3 takes static
keys; Google Drive, OneDrive and Dropbox are connected with an OAuth "Connect" button: the
platform operator registers one OAuth app per provider (Nexaploy Settings) and the customer
only signs in. The resulting refresh token is stored in a Password field and reaches the agent
as a `__secret__:Backup Destination:<name>:oauth_token` placeholder in
`server.configure_backups` params (ARCHITECTURE.md §5.2), where rclone uses and refreshes it.

This module holds everything that can be tested without Frappe: provider endpoints and
scopes, authorization URLs, PKCE, signed OAuth `state`, the token exchange request, turning a
provider token response into rclone's token JSON, account-info parsing and the agent params.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from urllib.parse import urlencode

# --------------------------------------------------------------------------- types

S3 = "S3"
GOOGLE_DRIVE = "Google Drive"
ONEDRIVE = "OneDrive"
DROPBOX = "Dropbox"
DESTINATION_TYPES = (S3, GOOGLE_DRIVE, ONEDRIVE, DROPBOX)
OAUTH_DESTINATION_TYPES = (GOOGLE_DRIVE, ONEDRIVE, DROPBOX)

# Backup Destination.destination_type -> agent `server.configure_backups` `type`
RCLONE_TYPES = {S3: "s3", GOOGLE_DRIVE: "drive", ONEDRIVE: "onedrive", DROPBOX: "dropbox"}

GOOGLE = "google"
MICROSOFT = "microsoft"
DROPBOX_PROVIDER = "dropbox"
PROVIDER_FOR_TYPE = {GOOGLE_DRIVE: GOOGLE, ONEDRIVE: MICROSOFT, DROPBOX: DROPBOX_PROVIDER}
TYPE_FOR_PROVIDER = {v: k for k, v in PROVIDER_FOR_TYPE.items()}

NOT_CONNECTED = "Not Connected"
CONNECTED = "Connected"
ERROR = "Error"
STATUSES = (NOT_CONNECTED, CONNECTED, ERROR)

# Password fields of Backup Destination that a `__secret__:` placeholder may name (poll time).
SECRET_FIELDS = frozenset({"oauth_token", "s3_secret_key"})

# Nexaploy Settings fields holding each provider's OAuth app (client id, Password secret).
SETTINGS_CLIENT_FIELDS = {
	GOOGLE: ("google_client_id", "google_client_secret"),
	MICROSOFT: ("microsoft_client_id", "microsoft_client_secret"),
	DROPBOX_PROVIDER: ("dropbox_app_key", "dropbox_app_secret"),
}

BACKUP_REMOTE = "nexaploy-backups"  # same rclone remote name as the platform storage
DEFAULT_FOLDER = "nexaploy-backups"
STATE_TTL = 600  # seconds the customer has to finish signing in

FOLDER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}(?:/[A-Za-z0-9._-]{1,64}){0,4}$")
TENANT_RE = re.compile(r"^[A-Za-z0-9.-]{1,128}$")
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,62}$")
REGION_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
PROVIDER_RE = re.compile(r"^[A-Za-z0-9]{1,32}$")
ENDPOINT_RE = re.compile(r"^(https?://)?[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$")
DRIVE_FOLDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
ONEDRIVE_ID_RE = re.compile(r"^[A-Za-z0-9!._-]{1,256}$")
ONEDRIVE_DRIVE_TYPES = ("personal", "business", "documentLibrary")


class DestinationError(ValueError):
	"""Invalid destination input or provider response (message is safe to show)."""


# --------------------------------------------------------------------------- providers


@dataclass(frozen=True)
class OAuthProvider:
	name: str
	authorize_url: str
	token_url: str
	scopes: tuple[str, ...]
	auth_params: Mapping[str, str] = field(default_factory=dict)


PROVIDERS: dict[str, OAuthProvider] = {
	GOOGLE: OAuthProvider(
		GOOGLE,
		"https://accounts.google.com/o/oauth2/v2/auth",
		"https://oauth2.googleapis.com/token",
		# drive.file: only files this app created - never the customer's other files
		("https://www.googleapis.com/auth/drive.file",),
		# offline + consent: Google returns a refresh token every time (also on reconnect)
		{"access_type": "offline", "prompt": "consent"},
	),
	MICROSOFT: OAuthProvider(
		MICROSOFT,
		"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
		"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
		("offline_access", "Files.ReadWrite", "User.Read"),
		{"response_mode": "query", "prompt": "select_account"},
	),
	DROPBOX_PROVIDER: OAuthProvider(
		DROPBOX_PROVIDER,
		"https://www.dropbox.com/oauth2/authorize",
		"https://api.dropboxapi.com/oauth2/token",
		(
			"account_info.read", "files.metadata.read", "files.metadata.write",
			"files.content.read", "files.content.write",
		),
		{"token_access_type": "offline"},
	),
}


def get_provider(name: str | None) -> OAuthProvider:
	try:
		return PROVIDERS[str(name or "").lower()]
	except KeyError:
		raise DestinationError(f"Unknown OAuth provider {name!r}") from None


def provider_for_type(destination_type: str | None) -> str:
	try:
		return PROVIDER_FOR_TYPE[destination_type]
	except KeyError:
		raise DestinationError(f"{destination_type} destinations do not use OAuth") from None


def normalize_tenant(value: str | None) -> str:
	value = (value or "").strip() or "common"
	if not TENANT_RE.match(value):
		raise DestinationError("Invalid Microsoft tenant")
	return value


def _endpoint(url: str, tenant: str | None) -> str:
	return url.replace("{tenant}", normalize_tenant(tenant)) if "{tenant}" in url else url


def callback_url(site_url: str) -> str:
	"""The redirect URI to register with every provider."""
	return site_url.rstrip("/") + "/api/method/nexaploy.api.backup_destinations.oauth_callback"


# --------------------------------------------------------------------------- PKCE + state


def _b64(data: bytes) -> str:
	return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
	return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_pkce(random_bytes: bytes | None = None) -> tuple[str, str]:
	"""(code_verifier, S256 code_challenge) per RFC 7636."""
	verifier = _b64(random_bytes if random_bytes is not None else os.urandom(48))
	challenge = _b64(hashlib.sha256(verifier.encode()).digest())
	return verifier, challenge


def new_nonce() -> str:
	return _b64(os.urandom(18))


def sign_state(claims: Mapping[str, str], secret: str | bytes, now: float | None = None,
               ttl: int = STATE_TTL) -> str:
	"""Signed, expiring OAuth `state`: `<base64url json>.<base64url hmac-sha256>`.

	`claims` binds the flow to the user, team, destination, provider and a one-time `nonce`
	(the caller also stores the nonce server-side and deletes it on use).
	"""
	if not secret:
		raise DestinationError("state secret missing")
	key = secret.encode() if isinstance(secret, str) else secret
	body = dict(claims)
	body["exp"] = int((now if now is not None else time.time()) + ttl)
	payload = _b64(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
	sig = _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())
	return f"{payload}.{sig}"


def verify_state(state: str | None, secret: str | bytes, now: float | None = None) -> dict[str, Any] | None:
	"""Claims when `state` is authentic and unexpired, else None (never raises)."""
	if not state or not secret or not isinstance(state, str) or state.count(".") != 1 or len(state) > 4096:
		return None
	key = secret.encode() if isinstance(secret, str) else secret
	payload, sig = state.split(".")
	expected = _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())
	if not hmac.compare_digest(expected, sig):
		return None
	try:
		claims = json.loads(_unb64(payload))
	except (ValueError, json.JSONDecodeError):
		return None
	if not isinstance(claims, dict) or not isinstance(claims.get("exp"), int):
		return None
	if claims["exp"] < (now if now is not None else time.time()):
		return None
	return claims


# --------------------------------------------------------------------------- authorization + token


def authorization_url(provider: str, client_id: str, redirect_uri: str, state: str,
                      code_challenge: str | None = None, tenant: str | None = None) -> str:
	if not client_id:
		raise DestinationError("OAuth client is not configured")
	p = get_provider(provider)
	params = {
		"client_id": client_id,
		"redirect_uri": redirect_uri,
		"response_type": "code",
		"state": state,
		"scope": " ".join(p.scopes),
	}
	params.update(p.auth_params)
	if code_challenge:
		params["code_challenge"] = code_challenge
		params["code_challenge_method"] = "S256"
	return _endpoint(p.authorize_url, tenant) + "?" + urlencode(params)


def token_request(provider: str, code: str, client_id: str, client_secret: str, redirect_uri: str,
                  code_verifier: str | None = None, tenant: str | None = None) -> tuple[str, dict[str, str]]:
	"""(token URL, form body) exchanging an authorization `code` for tokens."""
	if not code:
		raise DestinationError("Missing authorization code")
	p = get_provider(provider)
	data = {
		"grant_type": "authorization_code",
		"code": code,
		"redirect_uri": redirect_uri,
		"client_id": client_id,
		"client_secret": client_secret,
	}
	if code_verifier:
		data["code_verifier"] = code_verifier
	if p.name == MICROSOFT:
		data["scope"] = " ".join(p.scopes)
	return _endpoint(p.token_url, tenant), data


def rfc3339(dt: datetime) -> str:
	return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rclone_token(response: Mapping[str, Any], now: datetime | None = None) -> dict[str, str]:
	"""Provider token response -> rclone's token JSON
	`{access_token, token_type, refresh_token, expiry}` (expiry RFC 3339, UTC)."""
	if not isinstance(response, Mapping):
		raise DestinationError("Unexpected token response")
	if response.get("error"):
		desc = response.get("error_description") or response.get("error")
		raise DestinationError(f"The provider refused the sign-in: {desc}")
	access = response.get("access_token")
	refresh = response.get("refresh_token")
	if not isinstance(access, str) or not access:
		raise DestinationError("The provider returned no access token")
	if not isinstance(refresh, str) or not refresh:
		raise DestinationError(
			"The provider returned no refresh token; remove the app's access in your account and connect again"
		)
	now = now or datetime.now(timezone.utc)
	try:
		expires_in = int(response.get("expires_in") or 3600)
	except (TypeError, ValueError):
		expires_in = 3600
	token_type = str(response.get("token_type") or "Bearer")
	if token_type.lower() == "bearer":
		token_type = "Bearer"  # Dropbox/Microsoft send "bearer"; rclone/oauth2 expect "Bearer"
	token = {
		"access_token": access,
		"token_type": token_type,
		"refresh_token": refresh,
		"expiry": rfc3339(now + timedelta(seconds=max(expires_in - 60, 0))),
	}
	return parse_token(json.dumps(token))


TOKEN_KEYS = ("access_token", "token_type", "refresh_token", "expiry")
EXPIRY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$")


def parse_token(value: Any) -> dict[str, str]:
	"""Validate rclone token JSON (same rules as the agent): an object with non-empty
	`access_token` and `refresh_token`; optional `token_type` and RFC 3339 `expiry`."""
	if isinstance(value, str):
		if len(value) > 16384:
			raise DestinationError("token too long")
		try:
			value = json.loads(value)
		except json.JSONDecodeError:
			raise DestinationError("token must be a JSON object") from None
	if not isinstance(value, Mapping):
		raise DestinationError("token must be a JSON object")
	out: dict[str, str] = {}
	for key in TOKEN_KEYS:
		val = value.get(key)
		if val in (None, "") and key in ("token_type", "expiry"):
			continue
		if not isinstance(val, str) or not val or len(val) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in val):
			raise DestinationError(f"token.{key} missing or invalid")
		out[key] = val
	if "expiry" in out and not EXPIRY_RE.match(out["expiry"]):
		raise DestinationError("token.expiry must be an RFC 3339 timestamp")
	out.setdefault("token_type", "Bearer")
	return out


def token_json(token: Mapping[str, str]) -> str:
	return json.dumps(parse_token(dict(token)), separators=(",", ":"))


# --------------------------------------------------------------------------- account info


def account_requests(provider: str) -> list[dict[str, Any]]:
	"""HTTP requests (with the new access token as Bearer) that identify the account."""
	if provider == GOOGLE:
		return [{"method": "GET", "url": "https://www.googleapis.com/drive/v3/about?fields=user(emailAddress,displayName)"}]
	if provider == MICROSOFT:
		return [
			{"method": "GET", "url": "https://graph.microsoft.com/v1.0/me?$select=mail,userPrincipalName,displayName"},
			{"method": "GET", "url": "https://graph.microsoft.com/v1.0/me/drive?$select=id,driveType"},
		]
	if provider == DROPBOX_PROVIDER:
		# Dropbox RPC endpoints take a JSON `null` body
		return [{"method": "POST", "url": "https://api.dropboxapi.com/2/users/get_current_account", "json": None}]
	raise DestinationError(f"Unknown OAuth provider {provider!r}")


def _str(value: Any, limit: int = 140) -> str | None:
	return str(value)[:limit] if isinstance(value, str) and value else None


def parse_account(provider: str, responses: list[Any]) -> dict[str, str | None]:
	"""Responses of :func:`account_requests` -> {email, name, drive_id, drive_type}."""
	out: dict[str, str | None] = {"email": None, "name": None, "drive_id": None, "drive_type": None}
	first = responses[0] if responses and isinstance(responses[0], Mapping) else {}
	if provider == GOOGLE:
		user = first.get("user") or {}
		out["email"], out["name"] = _str(user.get("emailAddress")), _str(user.get("displayName"))
	elif provider == MICROSOFT:
		out["email"] = _str(first.get("mail")) or _str(first.get("userPrincipalName"))
		out["name"] = _str(first.get("displayName"))
		drive = responses[1] if len(responses) > 1 and isinstance(responses[1], Mapping) else {}
		drive_id, drive_type = drive.get("id"), drive.get("driveType")
		if not (isinstance(drive_id, str) and ONEDRIVE_ID_RE.match(drive_id)):
			raise DestinationError("Could not read the OneDrive of this account (is OneDrive enabled for it?)")
		if drive_type not in ONEDRIVE_DRIVE_TYPES:
			raise DestinationError(f"Unsupported OneDrive type {drive_type!r}")
		out["drive_id"], out["drive_type"] = drive_id, drive_type
	elif provider == DROPBOX_PROVIDER:
		out["email"] = _str(first.get("email"))
		name = first.get("name") or {}
		out["name"] = _str(name.get("display_name")) if isinstance(name, Mapping) else None
	else:
		raise DestinationError(f"Unknown OAuth provider {provider!r}")
	return out


def revoke_request(provider: str, token: Mapping[str, str]) -> dict[str, Any] | None:
	"""Best-effort revocation on disconnect (Microsoft has no per-token revocation)."""
	if provider == GOOGLE and token.get("refresh_token"):
		return {"method": "POST", "url": "https://oauth2.googleapis.com/revoke",
		        "data": {"token": token["refresh_token"]}}
	if provider == DROPBOX_PROVIDER and token.get("access_token"):
		return {"method": "POST", "url": "https://api.dropboxapi.com/2/auth/token/revoke", "json": None,
		        "bearer": token["access_token"]}
	return None


# --------------------------------------------------------------------------- validation


def normalize_folder(value: str | None) -> str:
	"""Folder (path prefix) inside the storage; each server writes under `<folder>/<server_id>`."""
	text = (value or "").strip().strip("/") or DEFAULT_FOLDER
	if not FOLDER_RE.match(text) or any(p in (".", "..") for p in text.split("/")):
		raise DestinationError("Folder may only contain letters, digits, '.', '_', '-' and '/'")
	return text


def validate_s3(values: Mapping[str, Any]) -> None:
	"""Same rules the agent applies to s3 params (fail early in the portal)."""
	bucket = values.get("s3_bucket") or ""
	if not BUCKET_RE.match(bucket):
		raise DestinationError("Invalid bucket name")
	if not values.get("s3_access_key") or any(c in str(values["s3_access_key"]) for c in "\r\n\0"):
		raise DestinationError("Access key ID is required")
	if values.get("s3_region") and not REGION_RE.match(values["s3_region"]):
		raise DestinationError("Invalid region")
	if values.get("s3_endpoint") and not ENDPOINT_RE.match(values["s3_endpoint"]):
		raise DestinationError("Invalid endpoint")
	if values.get("s3_provider") and not PROVIDER_RE.match(values["s3_provider"]):
		raise DestinationError("Invalid S3 provider")


# --------------------------------------------------------------------------- agent params


def destination_usable(dest: Mapping[str, Any]) -> bool:
	"""Ready to push to servers: S3 with bucket + key, or an OAuth destination Connected."""
	if dest.get("destination_type") == S3:
		return bool(dest.get("s3_bucket") and dest.get("s3_access_key"))
	return dest.get("destination_type") in OAUTH_DESTINATION_TYPES and dest.get("status") == CONNECTED


def destination_job_params(dest: Mapping[str, Any], server_id: str, secret: str,
                           client: Mapping[str, Any] | None = None) -> dict[str, Any]:
	"""`server.configure_backups` params for one server using `dest`.

	`secret` is the S3 secret key or the OAuth token JSON (or a `__secret__:` placeholder).
	`client` = {client_id, client_secret (placeholder), tenant} of the platform OAuth app; rclone
	needs it to refresh the token. Paths stay `<remote>:<folder>/<server_id>/<bench>/<site>/<ts>`.
	"""
	dtype = dest.get("destination_type")
	if dtype not in RCLONE_TYPES:
		raise DestinationError(f"Unknown destination type {dtype!r}")
	prefix = f"{normalize_folder(dest.get('folder'))}/{server_id}"
	params: dict[str, Any] = {"remote": BACKUP_REMOTE, "type": RCLONE_TYPES[dtype], "prefix": prefix}
	if dtype == S3:
		params.update({
			"provider": dest.get("s3_provider") or "Other",
			"endpoint": dest.get("s3_endpoint") or None,
			"region": dest.get("s3_region") or None,
			"bucket": dest.get("s3_bucket"),
			"access_key_id": dest.get("s3_access_key"),
			"secret_access_key": secret,
		})
		return params
	params["token"] = secret
	client = client or {}
	if client.get("client_id") and client.get("client_secret"):
		params["client_id"] = client["client_id"]
		params["client_secret"] = client["client_secret"]
	if dtype == GOOGLE_DRIVE and dest.get("root_folder_id"):
		if not DRIVE_FOLDER_ID_RE.match(dest["root_folder_id"]):
			raise DestinationError("Invalid Google Drive folder id")
		params["root_folder_id"] = dest["root_folder_id"]
	if dtype == ONEDRIVE:
		if not dest.get("drive_id") or dest.get("drive_type") not in ONEDRIVE_DRIVE_TYPES:
			raise DestinationError("Connect the OneDrive account again (drive id missing)")
		params["drive_id"] = dest["drive_id"]
		params["drive_type"] = dest["drive_type"]
		tenant = normalize_tenant(client.get("tenant"))
		if tenant != "common":
			params["tenant"] = tenant
	return params


def destination_fingerprint(dest: Mapping[str, Any], secret: str | None, client: Mapping[str, Any] | None = None) -> str:
	"""sha256 over what the agent receives (secret and client hashed in), to detect changes."""
	keys = ("destination_type", "folder", "s3_provider", "s3_endpoint", "s3_region", "s3_bucket",
	        "s3_access_key", "root_folder_id", "drive_id", "drive_type")
	blob = {k: dest.get(k) or None for k in keys}
	blob["secret"] = hashlib.sha256((secret or "").encode()).hexdigest()
	blob["client"] = hashlib.sha256(json.dumps(dict(client or {}), sort_keys=True, default=str).encode()).hexdigest()
	return hashlib.sha256(json.dumps(blob, sort_keys=True).encode()).hexdigest()


def effective_destination(server_destination: str | None, team_default: str | None) -> str | None:
	"""A server's explicit destination wins, then its team's default (else platform storage)."""
	return server_destination or team_default or None


def remote_test_params(dest_params: Mapping[str, Any]) -> dict[str, Any]:
	"""`backup.test_remote` params that test `dest_params` without touching the active remote."""
	return {"destination": dict(dest_params), "prefix": dest_params.get("prefix")}
