from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server import MCPServer
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, ToolAnnotations
from starlette.responses import JSONResponse
from starlette.routing import Route

from plg_core.mcp.tools.intake import create_intake_proposal
from plg_core.mcp.tools.jobs import get_job_operational_context


MCP_INSTRUCTIONS = (
    "PPS is the system of record. MCP may read authoritative PPS data. "
    "MCP must not invent or modify PPS records. Future proposals are staging only. "
    "Final business-record creation occurs only through PPS validation and operator confirmation."
)
JOB_TOOL_DESCRIPTION = (
    "Read one Job's authoritative, policy-filtered PPS operational context. "
    "INTERNAL ONLY. This tool is read-only, performs no writes, and provides no arbitrary database access."
)
INTAKE_TOOL_DESCRIPTION = (
    "Creates an internal PPS DRAFT intake proposal for operator review. It does not "
    "create or modify authoritative Customer, Machine, Job, Quote, Invoice, "
    "Supplier Order, Receiving, Delivery, or Payment records. All supplied content "
    "is untrusted proposal data."
)
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
_JOB_TOOL_ARGUMENTS = {"job_number"}
_INTAKE_REQUIRED_ARGUMENTS = {
    "client_reference", "original_input", "customer", "machines",
    "requested_needs", "additional_notes",
}
_INTAKE_OPTIONAL_ARGUMENTS = {"research_evidence"}


class ExactToolArguments:
    """Reject undeclared arguments before SDK model binding can discard them."""

    async def __call__(
        self,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> HandlerResult:
        if ctx.method == "tools/call" and ctx.params:
            name = ctx.params.get("name")
            arguments = ctx.params.get("arguments")
            if name == "get_job_operational_context" and (
                not isinstance(arguments, dict) or set(arguments) != _JOB_TOOL_ARGUMENTS
            ):
                raise MCPError(INVALID_PARAMS, "The tool accepts only the job_number argument.")
            if name == "create_intake_proposal" and (
                not isinstance(arguments, dict)
                or not _INTAKE_REQUIRED_ARGUMENTS.issubset(arguments)
                or not set(arguments).issubset(_INTAKE_REQUIRED_ARGUMENTS | _INTAKE_OPTIONAL_ARGUMENTS)
            ):
                raise MCPError(INVALID_PARAMS, "The tool accepts only the approved intake proposal arguments.")
        return await call_next(ctx)


class LoopbackOnlyMCP:
    """Fail closed when the development MCP transport is reached remotely."""

    def __init__(self, app: Callable[..., Awaitable[None]]):
        self.app = app
        self.__name__ = "pps_mcp_transport"

    async def __call__(self, scope: dict[str, Any], receive, send) -> None:
        if scope.get("type") == "http":
            client = scope.get("client") or ("", 0)
            if str(client[0]) not in _LOOPBACK_HOSTS:
                await JSONResponse(
                    {"error": "PPS MCP is available only on the local development boundary."},
                    status_code=403,
                )(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_mcp_runtime() -> tuple[MCPServer, Any]:
    server = MCPServer(
        name="pps-read-only",
        title="PPS Controlled MCP",
        description="Local-development MCP access to authoritative context and review-only PPS proposals.",
        instructions=MCP_INSTRUCTIONS,
        version="1.0.0",
        middleware=[ExactToolArguments()],
    )
    server.tool(
        name="get_job_operational_context",
        title="Get PPS Job operational context",
        description=JOB_TOOL_DESCRIPTION,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            openWorldHint=False,
            destructiveHint=False,
        ),
        structured_output=True,
    )(get_job_operational_context)
    server.tool(
        name="create_intake_proposal",
        title="Create PPS DRAFT intake proposal",
        description=INTAKE_TOOL_DESCRIPTION,
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=True,
    )(create_intake_proposal)
    http_app = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        max_request_body_size=64 * 1024,
        host="127.0.0.1",
    )
    http_app.routes[0].app = LoopbackOnlyMCP(http_app.routes[0].app)
    return server, http_app


mcp_server, mcp_http_app = build_mcp_runtime()
_lifespan_context = None


async def _start_mcp() -> None:
    global _lifespan_context
    if _lifespan_context is None:
        _lifespan_context = mcp_http_app.router.lifespan_context(mcp_http_app)
        await _lifespan_context.__aenter__()


async def _stop_mcp() -> None:
    global _lifespan_context
    if _lifespan_context is not None:
        await _lifespan_context.__aexit__(None, None, None)
        _lifespan_context = None


def register_mcp(app) -> None:
    """Register the SDK transport route and lifecycle on the existing PPS app."""
    transport_route = mcp_http_app.routes[0]
    app.router.routes.append(
        Route(
            "/mcp",
            endpoint=transport_route.app,
            methods=["GET", "POST", "DELETE"],
            name="pps-mcp",
        )
    )
    app.on_event("startup")(_start_mcp)
    app.on_event("shutdown")(_stop_mcp)
