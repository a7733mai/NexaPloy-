"""Bearer token authentication for the local API (§5.2)."""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status


def token_matches(expected: str, presented: str | None) -> bool:
    """Constant-time comparison; an empty configured token never matches."""
    if not expected or not presented:
        return False
    return hmac.compare_digest(expected.encode(), presented.encode())


def bearer_token(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def require_token(request: Request) -> None:
    """FastAPI dependency: 401 unless ``Authorization: Bearer <agent.token>``."""
    settings = request.app.state.ctx.settings
    presented = bearer_token(request.headers.get("authorization"))
    if not token_matches(settings.agent.token, presented):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
