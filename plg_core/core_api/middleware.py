from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import uuid
from contextlib import closing

from fastapi.responses import JSONResponse
from starlette.responses import Response


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


def _error_response(
    *,
    status_code: int,
    detail: str,
    request_id: str,
):
    response = JSONResponse(
        status_code=status_code,
        content={
            "detail": detail,
        },
    )

    _set_security_headers(
        response,
        request_id=request_id,
        api_response=True,
    )

    return response


def _fingerprint_request(
    *,
    method: str,
    path: str,
    query: str,
    body: bytes,
) -> str:
    digest = hashlib.sha256(
        body
    ).hexdigest()

    operation = f"{method} {path}"

    if query:
        operation += f"?{query}"

    return (
        f"{operation}"
        f"|sha256={digest}"
    )


def _claim_idempotency_key(
    *,
    key: str,
    operation: str,
):
    from legacy_app import get_connection

    with closing(
        get_connection()
    ) as connection:
        connection.execute(
            """
            DELETE FROM api_idempotency_keys
            WHERE expires_at IS NOT NULL
              AND datetime(expires_at)
                    <= datetime('now')
            """
        )

        existing = connection.execute(
            """
            SELECT *
            FROM api_idempotency_keys
            WHERE idempotency_key=?
            """,
            (key,),
        ).fetchone()

        if existing is not None:
            connection.commit()

            return {
                "state": "existing",
                "row": dict(existing),
            }

        try:
            connection.execute(
                """
                INSERT INTO api_idempotency_keys (
                    idempotency_key,
                    operation,
                    response_code,
                    response_body,
                    expires_at
                )
                VALUES (
                    ?,
                    ?,
                    NULL,
                    '',
                    datetime(
                        'now',
                        '+24 hours'
                    )
                )
                """,
                (
                    key,
                    operation,
                ),
            )

            connection.commit()

            return {
                "state": "claimed",
                "row": None,
            }

        except sqlite3.IntegrityError:
            connection.rollback()

            existing = connection.execute(
                """
                SELECT *
                FROM api_idempotency_keys
                WHERE idempotency_key=?
                """,
                (key,),
            ).fetchone()

            return {
                "state": "existing",
                "row": (
                    dict(existing)
                    if existing is not None
                    else None
                ),
            }


def _store_idempotent_response(
    *,
    key: str,
    operation: str,
    status_code: int,
    body: bytes,
) -> None:
    from legacy_app import get_connection

    with closing(
        get_connection()
    ) as connection:
        connection.execute(
            """
            UPDATE api_idempotency_keys
            SET response_code=?,
                response_body=?
            WHERE idempotency_key=?
              AND operation=?
            """,
            (
                int(status_code),
                body.decode(
                    "utf-8",
                    errors="replace",
                ),
                key,
                operation,
            ),
        )

        connection.commit()


def _release_idempotency_key(
    *,
    key: str,
    operation: str,
) -> None:
    from legacy_app import get_connection

    with closing(
        get_connection()
    ) as connection:
        connection.execute(
            """
            DELETE FROM api_idempotency_keys
            WHERE idempotency_key=?
              AND operation=?
              AND response_code IS NULL
            """,
            (
                key,
                operation,
            ),
        )

        connection.commit()


def install_optional_api_hardening(app) -> None:
    """
    Always add request tracing and safe headers.

    When PPS_ENABLE_API_HARDENING is enabled:
    - mutating /api/v1/* requests require X-PPS-API-Key
    - mutating /api/v1/* requests require Idempotency-Key

    Idempotency is also honored in development whenever
    an Idempotency-Key is supplied.
    """

    hardening_enabled = _truthy(
        os.getenv(
            "PPS_ENABLE_API_HARDENING"
        )
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
            request.headers.get(
                "X-Request-ID"
            )
            or str(uuid.uuid4())
        )
        request.state.request_id = request_id

        path = str(
            request.url.path or ""
        )

        query = str(
            request.url.query or ""
        )

        method = str(
            request.method or ""
        ).upper()

        api_write = (
            path.startswith("/api/v1/")
            and method in WRITE_METHODS
        )

        protected_write = (
            hardening_enabled
            and api_write
        )

        if protected_write:
            supplied_key = str(
                request.headers.get(
                    "X-PPS-API-Key",
                    "",
                )
                or ""
            ).strip()

            if not expected_key:
                return _error_response(
                    status_code=503,
                    detail=(
                        "API hardening is enabled "
                        "but PPS_API_KEY is not "
                        "configured."
                    ),
                    request_id=request_id,
                )

            if (
                not supplied_key
                or not hmac.compare_digest(
                    supplied_key,
                    expected_key,
                )
            ):
                return _error_response(
                    status_code=401,
                    detail=(
                        "Invalid PPS API key."
                    ),
                    request_id=request_id,
                )

        idempotency_key = str(
            request.headers.get(
                "Idempotency-Key",
                "",
            )
            or ""
        ).strip()

        if (
            protected_write
            and not idempotency_key
        ):
            return _error_response(
                status_code=400,
                detail=(
                    "Idempotency-Key is required "
                    "for PPS API write requests."
                ),
                request_id=request_id,
            )

        if (
            idempotency_key
            and len(idempotency_key) > 200
        ):
            return _error_response(
                status_code=400,
                detail=(
                    "Idempotency-Key must be "
                    "200 characters or fewer."
                ),
                request_id=request_id,
            )

        operation = ""
        claimed = False

        if (
            api_write
            and idempotency_key
        ):
            body = await request.body()

            operation = _fingerprint_request(
                method=method,
                path=path,
                query=query,
                body=body,
            )

            claim = _claim_idempotency_key(
                key=idempotency_key,
                operation=operation,
            )

            if claim["state"] == "existing":
                existing = claim["row"]

                if existing is None:
                    return _error_response(
                        status_code=409,
                        detail=(
                            "Idempotency request "
                            "could not be resolved."
                        ),
                        request_id=request_id,
                    )

                if (
                    str(
                        existing.get(
                            "operation"
                        )
                        or ""
                    )
                    != operation
                ):
                    return _error_response(
                        status_code=409,
                        detail=(
                            "Idempotency-Key was "
                            "already used for a "
                            "different request."
                        ),
                        request_id=request_id,
                    )

                response_code = (
                    existing.get(
                        "response_code"
                    )
                )

                if response_code is None:
                    return _error_response(
                        status_code=409,
                        detail=(
                            "A request with this "
                            "Idempotency-Key is "
                            "already in progress."
                        ),
                        request_id=request_id,
                    )

                replay = Response(
                    content=str(
                        existing.get(
                            "response_body"
                        )
                        or ""
                    ),
                    status_code=int(
                        response_code
                    ),
                    media_type=(
                        "application/json"
                    ),
                )

                replay.headers[
                    "Idempotency-Replayed"
                ] = "true"

                replay.headers[
                    "Idempotency-Key"
                ] = idempotency_key

                _set_security_headers(
                    replay,
                    request_id=request_id,
                    api_response=True,
                )

                return replay

            claimed = True

        try:
            response = await call_next(
                request
            )

        except Exception:
            if (
                claimed
                and idempotency_key
                and operation
            ):
                _release_idempotency_key(
                    key=idempotency_key,
                    operation=operation,
                )

            raise

        if (
            claimed
            and idempotency_key
            and operation
        ):
            chunks = []

            body_iterator = getattr(
                response,
                "body_iterator",
                None,
            )

            if body_iterator is not None:
                async for chunk in body_iterator:
                    if isinstance(
                        chunk,
                        bytes,
                    ):
                        chunks.append(chunk)
                    else:
                        chunks.append(
                            str(chunk).encode()
                        )

                response_body = b"".join(
                    chunks
                )

            else:
                response_body = bytes(
                    getattr(
                        response,
                        "body",
                        b"",
                    )
                    or b""
                )

            status_code = int(
                response.status_code
            )

            original_headers = dict(
                response.headers
            )

            background = getattr(
                response,
                "background",
                None,
            )

            response = Response(
                content=response_body,
                status_code=status_code,
                headers=original_headers,
                background=background,
            )

            if 200 <= status_code < 300:
                _store_idempotent_response(
                    key=idempotency_key,
                    operation=operation,
                    status_code=status_code,
                    body=response_body,
                )

                response.headers[
                    "Idempotency-Replayed"
                ] = "false"

                response.headers[
                    "Idempotency-Key"
                ] = idempotency_key

            else:
                _release_idempotency_key(
                    key=idempotency_key,
                    operation=operation,
                )

        _set_security_headers(
            response,
            request_id=request_id,
            api_response=(
                path.startswith("/api/")
            ),
        )

        return response


def _set_security_headers(
    response,
    *,
    request_id: str,
    api_response: bool,
) -> None:
    response.headers[
        "X-Request-ID"
    ] = request_id

    response.headers[
        "X-Content-Type-Options"
    ] = "nosniff"

    response.headers[
        "Referrer-Policy"
    ] = "same-origin"

    response.headers[
        "X-Frame-Options"
    ] = "SAMEORIGIN"

    response.headers[
        "Permissions-Policy"
    ] = (
        "camera=(), "
        "microphone=(), "
        "geolocation=()"
    )

    if api_response:
        response.headers[
            "Cache-Control"
        ] = "no-store"
