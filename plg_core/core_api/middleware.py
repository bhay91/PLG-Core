import os
import uuid

def install_optional_api_hardening(app) -> None:
    enabled = os.getenv("PPS_ENABLE_API_HARDENING", "").strip().lower() in {"1","true","yes","on"}
    if not enabled:
        return

    @app.middleware("http")
    async def pps_api_hardening(request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response
