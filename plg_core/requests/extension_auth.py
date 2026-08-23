from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request


FIREFOX_INBOX_CREATE_SCOPE = "pps:firefox:inbox:create"
FIREFOX_JOB_UPDATE_SCOPE = "pps:firefox:jobs:update"
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _remote_firefox_inbox_enabled() -> bool:
    return os.getenv("PPS_FIREFOX_REMOTE_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _require_firefox_authorization(
    request: Request, required_scope: str, *, allow_remote: bool = False,
) -> str:
    client_host = str(request.client.host if request.client else "")
    is_loopback = client_host in _LOOPBACK_HOSTS
    if not is_loopback and not (allow_remote and _remote_firefox_inbox_enabled()):
        raise HTTPException(status_code=403, detail="PPS Firefox Inbox is available only on loopback.")

    configured_token = os.getenv("PPS_FIREFOX_INBOX_TOKEN", "").strip()
    configured_scopes = {
        value.strip()
        for value in os.getenv("PPS_FIREFOX_INBOX_SCOPES", "").split(",")
        if value.strip()
    }
    if not configured_token:
        raise HTTPException(status_code=503, detail="PPS Firefox Inbox authorization is not configured.")
    if required_scope not in configured_scopes:
        raise HTTPException(status_code=403, detail=f"Required Firefox scope is not configured: {required_scope}.")

    authorization = str(request.headers.get("Authorization") or "")
    scheme, separator, supplied = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not supplied.strip():
        raise HTTPException(status_code=401, detail="PPS Firefox Inbox authorization is required.")
    if not hmac.compare_digest(supplied.strip(), configured_token):
        raise HTTPException(status_code=401, detail="PPS Firefox Inbox authorization is invalid.")
    return required_scope


def require_firefox_inbox_authorization(request: Request) -> str:
    return _require_firefox_authorization(
        request, FIREFOX_INBOX_CREATE_SCOPE, allow_remote=True,
    )


def require_firefox_job_update_authorization(request: Request) -> str:
    return _require_firefox_authorization(request, FIREFOX_JOB_UPDATE_SCOPE)
