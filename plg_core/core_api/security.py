import hmac
import os
from fastapi import Header, HTTPException

def require_api_key(x_pps_api_key: str | None = Header(default=None)) -> None:
    """Opt-in API-key dependency. It is not globally enabled by the generator."""
    expected = os.getenv("PPS_API_KEY", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="PPS_API_KEY is not configured.")
    supplied = (x_pps_api_key or "").strip()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid PPS API key.")
