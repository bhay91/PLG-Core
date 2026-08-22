from __future__ import annotations

import logging
import time
import uuid

from fastapi import APIRouter, Header, HTTPException, Request

from plg_core.ai.context import build_internal_job_context
from plg_core.ai.models import AskPPSRequest, AskPPSResponse
from plg_core.ai.service import (
    AIConfigurationError,
    AIMalformedResponseError,
    AITimeoutError,
    AIUnavailableError,
    ask_job,
    get_ai_settings,
)


router = APIRouter(prefix="/api/v1/ai", tags=["internal-ai"])
LOGGER = logging.getLogger("pps.ai.audit")
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _require_local_operator(request: Request, local_operator: str | None) -> str:
    host = request.client.host if request.client else ""
    if host not in _LOOPBACK_HOSTS or local_operator != "1":
        raise HTTPException(status_code=403, detail="Ask PPS is available only to the local PPS operator.")
    return "local-operator"


@router.post("/jobs/{job_id}/ask", response_model=AskPPSResponse)
def ask_pps_job(
    job_id: int,
    payload: AskPPSRequest,
    request: Request,
    x_pps_local_operator: str | None = Header(default=None),
) -> AskPPSResponse:
    actor = _require_local_operator(request, x_pps_local_operator)
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    started = time.monotonic()
    context = build_internal_job_context(job_id)
    model = get_ai_settings().model
    outcome = "error"
    try:
        answer, facts, model = ask_job(payload.question, context)
        outcome = "success"
        return AskPPSResponse(
            job_number=context.job["job_number"],
            context_id=context.context_id,
            answer=answer,
            source_facts=facts,
        )
    except AIConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except AITimeoutError as error:
        raise HTTPException(status_code=504, detail=str(error)) from error
    except AIMalformedResponseError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    except AIUnavailableError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    finally:
        LOGGER.info(
            "ask_pps actor=%s job_id=%s context_id=%s model=%s request_id=%s latency_ms=%s outcome=%s",
            actor, job_id, context.context_id, model, request_id,
            round((time.monotonic() - started) * 1000), outcome,
        )
