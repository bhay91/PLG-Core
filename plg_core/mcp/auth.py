from __future__ import annotations

import hmac
import os

from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError


JOB_CONTEXT_READ_SCOPE = "pps:mcp:job-context:read"
PROPOSAL_CREATE_SCOPE = "pps:mcp:proposal:create"


def _require_scope(ctx: Context, required_scope: str) -> str:
    """Validate the dedicated local-development MCP credential and one scope."""
    configured_token = os.getenv("PPS_MCP_DEV_TOKEN", "").strip()
    configured_scopes = {
        value.strip() for value in os.getenv("PPS_MCP_DEV_SCOPES", "").split(",") if value.strip()
    }
    if not configured_token or required_scope not in configured_scopes:
        raise ToolError("PPS MCP local development authorization is not configured.")

    headers = ctx.headers or {}
    authorization = str(headers.get("authorization") or headers.get("Authorization") or "")
    scheme, separator, supplied_token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not supplied_token.strip():
        raise ToolError("PPS MCP authorization is required.")
    if not hmac.compare_digest(supplied_token.strip(), configured_token):
        raise ToolError("PPS MCP authorization is invalid.")
    return required_scope


def require_job_context_read(ctx: Context) -> str:
    return _require_scope(ctx, JOB_CONTEXT_READ_SCOPE)


def require_proposal_create(ctx: Context) -> str:
    return _require_scope(ctx, PROPOSAL_CREATE_SCOPE)
