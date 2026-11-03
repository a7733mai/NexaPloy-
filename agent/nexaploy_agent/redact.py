"""Redaction of secrets in params, logs and command output (§5.3)."""

from __future__ import annotations

import re
from typing import Any, Iterable

SENSITIVE_KEY_RE = re.compile(r"password|secret|token|key", re.IGNORECASE)
MASK = "********"


def is_sensitive(key: str) -> bool:
    return bool(SENSITIVE_KEY_RE.search(key))


def redact_params(value: Any) -> Any:
    """Recursively replace values whose key matches password|secret|token|key.

    ``site.set_config`` params are ``{key, value}``: the ``key`` field is
    redacted by the rule above, and ``value`` is additionally redacted when the
    config key it targets is itself sensitive (e.g. ``key="api_secret"``).
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        sensitive_target = isinstance(value.get("key"), str) and is_sensitive(value["key"])
        for k, v in value.items():
            if isinstance(k, str) and is_sensitive(k):
                out[k] = MASK if v not in (None, "") else v
            elif k == "value" and sensitive_target:
                out[k] = MASK
            else:
                out[k] = redact_params(v)
        return out
    if isinstance(value, list):
        return [redact_params(v) for v in value]
    return value


def collect_secrets(value: Any) -> set[str]:
    """Every string value stored under a sensitive key (for output masking)."""
    found: set[str] = set()
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str) and is_sensitive(k) and isinstance(v, str) and k != "key":
                found.add(v)
            else:
                found |= collect_secrets(v)
    elif isinstance(value, list):
        for v in value:
            found |= collect_secrets(v)
    return found


_URL_CRED_RE = re.compile(r"(https?://)[^/@\s:]+(?::[^/@\s]*)?@")


def mask_text(text: str, secrets: Iterable[str]) -> str:
    """Replace each secret (>= 4 chars) and URL credentials in ``text``."""
    if not text:
        return text
    for s in sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True):
        text = text.replace(s, MASK)
    return _URL_CRED_RE.sub(r"\1" + MASK + "@", text)
