from __future__ import annotations

import hmac
import secrets

from fastapi import HTTPException, Request


CSRF_COOKIE_NAME = "pps_csrf_token"


def csrf_token_for_request(request: Request) -> str:
    token = str(request.cookies.get(CSRF_COOKIE_NAME, "") or "").strip()
    if 32 <= len(token) <= 256:
        return token
    return secrets.token_urlsafe(32)


def require_valid_csrf(request: Request, submitted_token: str) -> None:
    cookie_token = str(
        request.cookies.get(CSRF_COOKIE_NAME, "") or ""
    ).strip()
    submitted_token = str(submitted_token or "").strip()
    if (
        not cookie_token
        or not submitted_token
        or not hmac.compare_digest(cookie_token, submitted_token)
    ):
        raise HTTPException(
            status_code=403,
            detail="Invalid or missing CSRF token.",
        )


def request_actor(request: Request) -> str:
    actor = getattr(request.state, "actor", None)
    if actor:
        return str(actor).strip() or "system"

    user = request.scope.get("user")
    if user is not None and getattr(user, "is_authenticated", True):
        for attribute in ("display_name", "username", "email", "identity"):
            value = getattr(user, attribute, None)
            if value:
                return str(value).strip()
        value = str(user).strip()
        if value and value.lower() not in {"none", "anonymous"}:
            return value

    return "system"


def request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "") or "").strip()


def new_idempotency_key() -> str:
    return secrets.token_urlsafe(32)
