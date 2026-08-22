from __future__ import annotations

import logging

from fastapi import HTTPException
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError

from plg_core.ai.context import build_internal_job_context
from plg_core.ai.models import InternalJobContext
from plg_core.jobs.service import resolve_job_id_by_number
from plg_core.mcp.auth import require_job_context_read
from plg_core.mcp.models import JobNumber


LOGGER = logging.getLogger("pps.mcp")


async def get_job_operational_context(job_number: JobNumber, ctx: Context) -> InternalJobContext:
    """Return one policy-filtered, authoritative INTERNAL PPS Job context."""
    require_job_context_read(ctx)
    try:
        job_id = resolve_job_id_by_number(job_number)
        return build_internal_job_context(job_id)
    except HTTPException as error:
        if error.status_code == 404:
            raise ToolError("PPS Job not found.") from error
        LOGGER.warning("MCP Job context rejected status=%s", error.status_code)
        raise ToolError("PPS could not provide the requested Job context.") from error
    except ToolError:
        raise
    except Exception as error:
        LOGGER.exception("MCP Job context service failure")
        raise ToolError("PPS operational context is temporarily unavailable.") from error
