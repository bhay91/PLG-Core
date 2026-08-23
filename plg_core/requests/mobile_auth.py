from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request


MOBILE_INBOX_CREATE_SCOPE = "pps:mobile:inbox:create"


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def require_mobile_inbox_authorization(request: Request) -> str:
    if not _truthy(os.getenv("PPS_MOBILE_INBOX_ENABLED")):
        raise HTTPException(status_code=403, detail="PPS Mobile Inbox is disabled.")

    configured_token = os.getenv("PPS_MOBILE_INBOX_TOKEN", "").strip()
    configured_scopes = {
        value.strip()
        for value in os.getenv("PPS_MOBILE_INBOX_SCOPES", "").split(",")
        if value.strip()
    }
    if not configured_token:
        raise HTTPException(status_code=503, detail="PPS Mobile Inbox authorization is not configured.")
    if MOBILE_INBOX_CREATE_SCOPE not in configured_scopes:
        raise HTTPException(
            status_code=403,
            detail=f"Required mobile scope is not configured: {MOBILE_INBOX_CREATE_SCOPE}.",
        )

    authorization = str(request.headers.get("Authorization") or "")
    x_mobile_header = str(request.headers.get("X-PPS-Mobile-Token") or "").strip()
    mobile_header = str(request.headers.get("PPS-Mobile-Token") or "").strip()
    if sum(bool(value) for value in (authorization, x_mobile_header, mobile_header)) > 1:
        raise HTTPException(
            status_code=401,
            detail="PPS Mobile Inbox accepts only one authorization method per request.",
        )

    supplied = x_mobile_header or mobile_header
    if authorization:
        scheme, separator, bearer = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not bearer.strip():
            raise HTTPException(status_code=401, detail="PPS Mobile Inbox authorization is required.")
        supplied = bearer.strip()
    if not supplied:
        raise HTTPException(status_code=401, detail="PPS Mobile Inbox authorization is required.")
    if not hmac.compare_digest(supplied, configured_token):
        raise HTTPException(status_code=401, detail="PPS Mobile Inbox authorization is invalid.")
    return MOBILE_INBOX_CREATE_SCOPE
