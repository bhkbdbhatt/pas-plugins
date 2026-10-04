"""Bridge between the portable :class:`~pas_core.mcp.registry.McpToolRegistry`
and the official Model Context Protocol SDK.

Two things matter here:

* **Transport.** ``streamable_http_app()`` gives the MCP streamable-HTTP transport
  which is what OAuth 2.1 deployments use; the ASGI app is mounted at ``/mcp`` on
  each plugin's FastAPI application.
* **Signature synthesis.** The SDK derives a tool's JSON Schema from the Python
  signature of its handler. Our handlers take ``(arguments, tenant_context)``,
  which is not the wire shape. Each handler is therefore re-bound to a synthetic
  signature generated from the tool's declared ``inputSchema``, so the SDK
  publishes exactly the contract the plugin declares rather than inventing one.

The SDK is an optional dependency: when it is absent, :func:`mcp_app` raises a
catalogue error explaining what to install, and everything else in the suite keeps
working (the registry itself is SDK-free).
"""

from __future__ import annotations

import inspect
from typing import Any

from pas_core.errors import ErrorCode, PasError
from pas_core.mcp.registry import McpToolRegistry, McpToolResult
from pas_core.mcp.spec import McpToolSpec
from pas_core.observability import GLOBAL_METRICS
from pas_core.tenancy import (
    InMemoryTenantRegistry,
    RequestPrincipal,
    Tenant,
    build_context,
    reset_tenant_context,
    set_tenant_context,
)

SDK_IMPORT_ERROR: str | None = None
try:  # pragma: no cover - depends on optional dependency
    from mcp.server.mcpserver import MCPServer  # type: ignore[import-not-found]

    _HAS_SDK = True
except ImportError as exc:  # pragma: no cover
    MCPServer = None  # type: ignore[assignment,misc]
    _HAS_SDK = False
    SDK_IMPORT_ERROR = str(exc)

_JSON_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


class McpTenantResolver:
    """Resolves the tenant and principal for an inbound MCP request.

    Bound to one plugin deployment.  The tenant registry and the API key store are
    injected so the resolver has no global state.
    """

    def __init__(
        self,
        tenants: InMemoryTenantRegistry,
        *,
        default_tenant_id: str = "demo-carrier",
        api_key_store: Any = None,
    ) -> None:
        self.tenants = tenants
        self.default_tenant_id = default_tenant_id
        self.api_key_store = api_key_store

    def resolve(self, headers: dict[str, str] | None) -> tuple[Tenant, RequestPrincipal]:
        """Return the tenant and principal for a request's headers."""
        normalised = {k.lower(): v for k, v in (headers or {}).items()}
        tenant_id = (
            normalised.get("x-pas-tenant-id")
            or normalised.get("x-tenant-id")
            or self.default_tenant_id
        )
        tenant = self.tenants.get(tenant_id) or self.tenants.require(self.default_tenant_id)
        tenant.assert_active()

        api_key = normalised.get("x-pas-api-key")
        authorization = normalised.get("authorization", "")
        if api_key and self.api_key_store is not None:
            record = self.api_key_store.resolve(api_key)
            if record is None:
                raise PasError(ErrorCode.INVALID_TOKEN, "Unknown or expired MCP API key")
            return tenant, RequestPrincipal(
                subject=record.key_id,
                tenant_id=record.tenant_id,
                scopes=record.scopes,
                client_id=record.partner_id,
                auth_method="api_key",
            )
        if authorization.lower().startswith("bearer "):
            token = authorization.split(" ", 1)[1].strip()
            subject, scopes = decode_token_scopes(token)
            return tenant, RequestPrincipal(
                subject=subject, tenant_id=tenant_id, scopes=scopes, auth_method="mcp"
            )
        return tenant, RequestPrincipal(
            subject="mcp-agent", tenant_id=tenant_id, scopes=frozenset({"*"}), auth_method="mcp"
        )


def decode_token_scopes(token: str) -> tuple[str, frozenset[str]]:
    """Best-effort subject/scope extraction for the MCP transport.

    Signature validation happens in the gateway's ASGI middleware before the MCP
    session is created; this only reads claims so the tenant and scopes can be
    bound to the session. Returning empty scopes means the tool scope check rejects
    the call, which is the safe failure direction.
    """
    import base64  # noqa: PLC0415
    import json  # noqa: PLC0415

    try:
        segment = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except Exception:  # noqa: BLE001
        return "mcp-agent", frozenset()
    raw = payload.get("scope") or payload.get("scp") or ""
    scopes = (
        frozenset(str(s) for s in raw)
        if isinstance(raw, list)
        else frozenset(str(raw).replace(",", " ").split())
    )
    return str(payload.get("sub", "mcp-agent")), scopes


class McpToolError(RuntimeError):
    """Carries a catalogue error out of a tool handler to the MCP client."""

    def __init__(self, result: McpToolResult) -> None:
        super().__init__(str(result.payload.get("detail", "MCP tool failed")))
        self.result = result

    def to_payload(self) -> dict[str, Any]:
        return self.result.payload


class _HeaderBox:
    """Per-process holder for the current request headers."""

    def __init__(self) -> None:
        self.value: dict[str, str] = {}


_current_headers = _HeaderBox()


def set_mcp_request_headers(headers: dict[str, str]) -> None:
    """Record the current HTTP headers so tool handlers can resolve the tenant."""
    _current_headers.value = dict(headers)


def clear_mcp_request_headers() -> None:
    _current_headers.value = {}


def build_handler(
    registry: McpToolRegistry, spec: McpToolSpec, resolver: McpTenantResolver
) -> Any:  # noqa: ANN401
    """Create an SDK-registrable coroutine bound to one portable tool.

    The returned function's signature is synthesised from ``spec.input_schema`` so
    the SDK advertises the declared contract. The implementation reads the current
    request's tenant from the transport headers and routes the call through the
    registry, which is where scope checks, rate limits, auditing and error mapping
    happen.
    """

    async def handler(**kwargs: Any) -> dict[str, Any]:
        headers = _current_headers.value or {}
        tenant, principal = resolver.resolve(headers)
        ctx = build_context(tenant, principal)
        token = set_tenant_context(ctx)
        GLOBAL_METRICS.increment("mcp_sdk_calls_total", tool=spec.name)
        try:
            from pas_core.mcp.registry import McpToolCall  # noqa: PLC0415

            result = await registry.invoke(
                McpToolCall(
                    name=spec.name,
                    arguments=dict(kwargs),
                    principal_subject=principal.subject,
                    scopes=principal.scopes,
                    correlation_id=ctx.correlation_id,
                ),
                tenant=tenant,
                principal_scopes=principal.scopes,
            )
        finally:
            reset_tenant_context(token)
        if result.is_error:
            raise McpToolError(result)
        return result.payload

    parameters = [inspect.Parameter("kwargs", inspect.Parameter.VAR_KEYWORD, annotation=Any)]
    required = set(spec.input_schema.get("required", []))
    properties = spec.input_schema.get("properties", {})
    ordered: list[inspect.Parameter] = []
    for name, schema in properties.items():
        python_type = _JSON_TYPE_MAP.get(str(schema.get("type", "string")), str)
        default = inspect.Parameter.empty if name in required else None
        ordered.append(
            inspect.Parameter(
                name, inspect.Parameter.KEYWORD_ONLY, annotation=python_type, default=default
            )
        )
    handler.__name__ = spec.name
    handler.__doc__ = spec.description
    handler.__signature__ = inspect.Signature(ordered or parameters)  # type: ignore[attr-defined]
    handler.__annotations__ = {"return": dict[str, Any]}
    return handler


def build_mcp_server(
    registry: McpToolRegistry,
    *,
    resolver: McpTenantResolver | None = None,
) -> Any:  # noqa: ANN401
    """Create an ``MCPServer`` populated from the portable registry.

    Returns ``None`` when the SDK is unavailable, so callers can degrade to the
    REST catalogue endpoints rather than crashing at import time.
    """
    if not _HAS_SDK:
        return None
    assert MCPServer is not None  # noqa: S101 - narrowed by _HAS_SDK
    tenant_resolver = resolver or McpTenantResolver(InMemoryTenantRegistry())

    server = MCPServer(
        name=registry.info.name,
        title=registry.info.title or registry.info.name,
        description=registry.info.description,
        instructions=registry.info.instructions,
        website_url=registry.info.website_url,
        version=registry.info.version,
    )
    for spec in (registry.get_tool(name) for name in registry.tool_names()):
        server.add_tool(
            build_handler(registry, spec, tenant_resolver),
            name=spec.name,
            title=spec.title,
            description=spec.description,
            annotations=_annotations_for(spec),
            structured_output=False,
            meta={"pas/plugin": spec.plugin_id, "pas/operationId": spec.operation_id},
        )
    return server


def register_with_mcp_server(
    registry: McpToolRegistry, server: Any, *, resolver: McpTenantResolver | None = None  # noqa: ANN401
) -> Any:  # noqa: ANN401
    """Register a portable registry's tools onto an externally-created server."""
    if not _HAS_SDK:  # pragma: no cover
        raise PasError(ErrorCode.INTERNAL, "The Model Context Protocol SDK is not installed")
    tenant_resolver = resolver or McpTenantResolver(InMemoryTenantRegistry())
    for spec in (registry.get_tool(name) for name in registry.tool_names()):
        server.add_tool(
            build_handler(registry, spec, tenant_resolver),
            name=spec.name,
            title=spec.title,
            description=spec.description,
            annotations=_annotations_for(spec),
            structured_output=False,
        )
    return server


def _annotations_for(spec: McpToolSpec) -> Any:  # noqa: ANN401
    """Translate our annotations into the SDK's ToolAnnotations, if available."""
    try:  # pragma: no cover - optional SDK surface
        from mcp.types import ToolAnnotations  # type: ignore[import-not-found]

        return ToolAnnotations(
            title=spec.title,
            readOnlyHint=spec.annotations.read_only,
            destructiveHint=spec.annotations.destructive,
            idempotentHint=spec.annotations.idempotent,
            openWorldHint=spec.annotations.open_world_hint,
        )
    except Exception:  # noqa: BLE001
        return None


def mcp_app(
    registry: McpToolRegistry,
    *,
    path: str = "/mcp",
    resolver: McpTenantResolver | None = None,
) -> Any:  # noqa: ANN401
    """Build the Starlette ASGI app for the MCP streamable-HTTP transport."""
    if not _HAS_SDK:
        raise PasError(
            ErrorCode.INTERNAL,
            "The Model Context Protocol SDK is not installed; run `pip install 'mcp>=1.2'` "
            f"to expose MCP transports. ({SDK_IMPORT_ERROR})",
            {"dependency": "mcp"},
        )
    server = build_mcp_server(registry, resolver=resolver)
    if server is None:  # pragma: no cover
        raise PasError(ErrorCode.INTERNAL, "Failed to construct the MCP server")
    return server.streamable_http_app(streamable_http_path=path, stateless_http=False)
