from __future__ import annotations

import hmac
import os
import uuid

from fastapi.responses import JSONResponse


WRITE_METHODS = {
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
}


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def install_optional_api_hardening(app) -> None:
    """
    Always install request tracing and safe response headers.

    API-key enforcement remains environment controlled so
    local browser development is not broken. When enabled,
    only mutating /api/v1/* requests require the PPS API key.
    """

    hardening_enabled = _truthy(
        os.getenv("PPS_ENABLE_API_HARDENING")
    )

    expected_key = os.getenv(
        "PPS_API_KEY",
        "",
    ).strip()

    @app.middleware("http")
    async def pps_api_hardening(
        request,
        call_next,
    ):
        request_id = (
            request.headers.get("X-Request-ID")
            or str(uuid.uuid4())
        )

        path = str(request.url.path or "")
        method = str(request.method or "").upper()

        protected_write = (
            hardening_enabled
            and path.startswith("/api/v1/")
            and method in WRITE_METHODS
        )

        if protected_write:
            supplied = str(
                request.headers.get(
                    "X-PPS-API-Key",
                    "",
                )
                or ""
            ).strip()

            if not expected_key:
                response = JSONResponse(
                    status_code=503,
                    content={
                        "detail": (
                            "API hardening is enabled but "
                            "PPS_API_KEY is not configured."
                        )
                    },
                )

                _set_security_headers(
                    response,
                    request_id=request_id,
                    api_response=True,
                )

                return response

            if (
                not supplied
                or not hmac.compare_digest(
                    supplied,
                    expected_key,
                )
            ):
                response = JSONResponse(
                    status_code=401,
                    content={
                        "detail": "Invalid PPS API key."
                    },
                )

                _set_security_headers(
                    response,
                    request_id=request_id,
                    api_response=True,
                )

                return response

        response = await call_next(request)

        _set_security_headers(
            response,
            request_id=request_id,
            api_response=path.startswith("/api/"),
        )

        return response


def _set_security_headers(
    response,
    *,
    request_id: str,
    api_response: bool,
) -> None:
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers[
        "Permissions-Policy"
    ] = "camera=(), microphone=(), geolocation=()"

    if api_response:
        response.headers[
            "Cache-Control"
        ] = "no-store"
