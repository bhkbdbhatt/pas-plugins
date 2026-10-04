"""MCP tool registry and invocation pipeline.

The registry is the enforcement point for an MCP session.  Invoking a tool always
goes through :meth:`McpToolRegistry.invoke`, which:

1. resolves the tenant bound to the session,
2. checks the caller's OAuth scopes against the tool's declared scopes,
3. applies the tenant's MCP rate limit,
4. validates the arguments against the tool's declared JSON Schema,
5. runs the handler inside a correlation/tenant context,
6. writes an audit event and emits metrics,
7. maps any catalogue error into a structured MCP error result.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

from pas_core.acord.transaction import registry_as_json
from pas_core.audit import AuditAction, AuditOutcome, get_audit_trail
from pas_core.errors import (
    ErrorCode,
    PasError,
    PermissionDeniedError,
    ScopeInsufficientError,
    ValidationError,
)
from pas_core.mcp.spec import (
    McpPromptSpec,
    McpResourceSpec,
    McpServerInfo,
    McpToolAnnotations,
    McpToolSpec,
)
from pas_core.observability import MCP_TOOL_CALLS, GLOBAL_METRICS
from pas_core.ratelimit import InMemoryRateLimiter
from pas_core.tenancy import (
    RequestPrincipal,
    Tenant,
    TenantContext,
    build_context,
    reset_tenant_context,
    set_tenant_context,
)


@dataclass(frozen=True, slots=True)
class McpToolCall:
    """An inbound tool invocation."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    principal_subject: str = "mcp-agent"
    scopes: frozenset[str] = field(default_factory=frozenset)
    correlation_id: str = ""
    request_id: str = ""


@dataclass(frozen=True, slots=True)
class McpToolResult:
    """The result of invoking a tool.

    Mirrors the MCP content model: a list of text/structured content blocks plus
    ``isError``.  Callers should branch on ``result.ok`` and inspect
    ``result.payload["code"]`` rather than parsing prose.
    """

    tool: str
    ok: bool
    payload: dict[str, Any]
    duration_ms: float = 0.0
    is_error: bool = False
    annotations: McpToolAnnotations | None = None

    def to_wire(self) -> dict[str, Any]:
        blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": json.dumps(self.payload, indent=2, default=str),
            }
        ]
        if self.payload:
            blocks.append({"type": "resource", "resource": {
                "uri": f"pas://tool-result/{self.tool}",
                "mimeType": "application/json",
                "text": json.dumps(self.payload, default=str),
            }})
        return {
            "content": blocks,
            "isError": self.is_error,
            "structuredContent": self.payload,
        }

    def summary(self) -> str:
        if self.ok:
            keys = ", ".join(sorted(self.payload)[:5])
            return f"{self.tool} ok in {self.duration_ms:.1f}ms ({keys})"
        return f"{self.tool} failed in {self.duration_ms:.1f}ms: {self.payload.get('code')}"


class McpToolRegistry:
    """Holds a plugin's tools, resources and prompts, and invokes them safely."""

    def __init__(
        self,
        info: McpServerInfo,
        *,
        limiter: InMemoryRateLimiter | None = None,
        validate_arguments: bool = True,
    ) -> None:
        self.info = info
        self._tools: dict[str, McpToolSpec] = {}
        self._resources: list[McpResourceSpec] = []
        self._prompts: list[McpPromptSpec] = []
        self.limiter = limiter or InMemoryRateLimiter()
        self.validate_arguments = validate_arguments
        self._invocations: list[dict[str, Any]] = []

    # -- registration ------------------------------------------------------
    def register_tool(self, spec: McpToolSpec) -> McpToolSpec:
        if spec.name in self._tools:
            msg = f"MCP tool '{spec.name}' is already registered"
            raise ValidationError(msg, tool=spec.name, plugin=self.info.name)
        self._tools[spec.name] = spec
        return spec

    def register_tools(self, specs: Iterable[McpToolSpec]) -> None:
        for spec in specs:
            self.register_tool(spec)

    def register_resource(self, spec: McpResourceSpec) -> McpResourceSpec:
        self._resources.append(spec)
        return spec

    def register_prompt(self, spec: McpPromptSpec) -> McpPromptSpec:
        self._prompts.append(spec)
        return spec

    def tool(
        self,
        name: str,
        *,
        title: str = "",
        description: str,
        input_schema: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        required_scopes: tuple[str, ...] = (),
        read_only: bool = True,
        destructive: bool = False,
        idempotent: bool = True,
        plugin_id: str = "pas-core",
        operation_id: str | None = None,
        tags: tuple[str, ...] = (),
        examples: tuple[dict[str, Any], ...] = (),
        error_codes: tuple[str, ...] = (),
        rate_limit_policy: str = "mcp-tool",
    ) -> Any:  # noqa: ANN401 - decorator factory
        """Decorator that registers the wrapped coroutine as an MCP tool."""
        import functools  # noqa: PLC0415

        def decorator(func: Any) -> Any:  # noqa: ANN401
            schema = input_schema or _infer_schema(func)
            spec = McpToolSpec(
                name=name,
                title=title or name.replace("_", " ").title(),
                description=(func.__doc__ or description).strip(),
                input_schema=schema,
                handler=func,
                output_schema=output_schema,
                required_scopes=required_scopes,
                annotations=McpToolAnnotations(
                    read_only=read_only, destructive=destructive, idempotent=idempotent
                ),
                operation_id=operation_id,
                plugin_id=plugin_id,
                tags=tags,
                examples=examples,
                error_codes=error_codes,
                rate_limit_policy=rate_limit_policy,
            )
            self.register_tool(spec)

            @functools.wraps(func)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
                return await func(*args, **kwargs)

            wrapper.mcp_tool = spec  # type: ignore[attr-defined]
            return wrapper

        return decorator

    # -- discovery ---------------------------------------------------------
    def get_tool(self, name: str) -> McpToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise PasError(
                ErrorCode.NOT_FOUND,
                f"MCP tool '{name}' is not available on server '{self.info.name}'",
                {
                    "tool": name,
                    "server": self.info.name,
                    "availableTools": self.tool_names()[:30],
                },
            ) from exc

    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    def list_tools(self) -> list[dict[str, Any]]:
        return [self._tools[name].to_wire() for name in sorted(self._tools)]

    def list_resources(self) -> list[dict[str, Any]]:
        return [r.to_wire() for r in self._resources]

    def list_prompts(self) -> list[dict[str, Any]]:
        return [p.to_wire() for p in self._prompts]

    def get_prompt(self, name: str) -> McpPromptSpec:
        for prompt in self._prompts:
            if prompt.name == name:
                return prompt
        raise PasError(ErrorCode.NOT_FOUND, f"MCP prompt '{name}' is not available")

    def resolve_resource(self, uri: str) -> McpResourceSpec:
        """Match a concrete URI against the registered templates."""
        for resource in self._resources:
            if resource.uri_template == uri:
                return resource
        for resource in self._resources:
            if not resource.is_template:
                continue
            pattern = _template_to_regex(resource.uri_template)
            if pattern and pattern.match(uri):
                return resource
        raise PasError(
            ErrorCode.NOT_FOUND,
            f"No MCP resource matches '{uri}'",
            {"uri": uri, "available": [r.uri_template for r in self._resources]},
        )

    @staticmethod
    def extract_template_params(uri: str, template: str) -> dict[str, str]:
        """Pull ``{name}`` values out of a concrete URI."""
        template_parts = template.split("/")
        uri_parts = uri.split("/")
        params: dict[str, str] = {}
        for template_part, uri_part in zip(template_parts, uri_parts, strict=False):
            if template_part.startswith("{") and template_part.endswith("}"):
                params[template_part[1:-1]] = uri_part
        return params

    def catalogue(self) -> dict[str, Any]:
        """Full discovery document: tools, resources, prompts and ACORD reference."""
        return {
            "server": self.info.to_dict(),
            "tools": [self._tools[name].to_schema_dict() for name in sorted(self._tools)],
            "resources": [r.to_schema_dict() for r in self._resources],
            "prompts": [p.to_schema_dict() for p in self._prompts],
            "acordNgds": registry_as_json(),
        }

    # -- invocation --------------------------------------------------------
    async def invoke(
        self,
        call: McpToolCall,
        *,
        tenant: Tenant,
        principal_scopes: Iterable[str] | None = None,
    ) -> McpToolResult:
        """Execute a tool with full tenant isolation, scope checks and auditing."""
        started = time.perf_counter()
        tool_name = call.name
        scopes = frozenset(principal_scopes if principal_scopes is not None else call.scopes)
        tenant_context = build_context(
            tenant,
            _mcp_principal(call, scopes, tenant),
            correlation_id=call.correlation_id or None,
        )

        try:
            spec = self.get_tool(tool_name)
            self._check_scopes(spec, scopes)
            await self.limiter.check(
                tenant_context.cache_key("mcp", tool_name), spec.rate_limit_policy
            )
            if self.validate_arguments:
                self._validate_arguments(spec, call.arguments)
            token = set_tenant_context(tenant_context)
            try:
                if spec.handler is None:
                    raise PasError(
                        ErrorCode.INTERNAL,
                        f"MCP tool '{tool_name}' has no handler bound",
                        {"tool": tool_name},
                    )
                raw = await spec.handler(call.arguments, tenant_context)
                payload = raw if isinstance(raw, dict) else {"result": raw}
            finally:
                reset_tenant_context(token)
        except PasError as exc:
            duration = (time.perf_counter() - started) * 1000
            self._record(tool_name, exc.code.value, duration, tenant_context.tenant_id, exc.detail)
            return McpToolResult(
                tool=tool_name,
                ok=False,
                is_error=True,
                payload=exc.to_problem(instance=f"pas://tool-result/{tool_name}"),
                duration_ms=duration,
            )
        except Exception as exc:  # noqa: BLE001
            duration = (time.perf_counter() - started) * 1000
            wrapped = PasError(
                ErrorCode.INTERNAL,
                f"MCP tool '{tool_name}' raised an unhandled error: {exc.__class__.__name__}",
                {"tool": tool_name},
                context="mcp",
            )
            self._record(tool_name, ErrorCode.INTERNAL.value, duration, tenant_context.tenant_id, {})
            return McpToolResult(
                tool=tool_name, ok=False, is_error=True,
                payload=wrapped.to_problem(), duration_ms=duration,
            )

        duration = (time.perf_counter() - started) * 1000
        self._record(tool_name, "ok", duration, tenant_context.tenant_id, {})
        return McpToolResult(
            tool=tool_name,
            ok=True,
            is_error=False,
            payload=payload,
            duration_ms=duration,
            annotations=spec.annotations,
        )

    def _check_scopes(self, spec: McpToolSpec, granted: frozenset[str]) -> None:
        if not spec.required_scopes:
            return
        missing = [
            scope for scope in spec.required_scopes
            if scope not in granted and "*" not in granted
        ]
        if missing:
            raise ScopeInsufficientError(missing, sorted(granted))

    @staticmethod
    def _validate_arguments(spec: McpToolSpec, arguments: Mapping[str, Any]) -> None:
        if not spec.input_schema:
            return
        validator = Draft202012Validator(spec.input_schema)
        errors = sorted(validator.iter_errors(dict(arguments)), key=lambda e: list(e.absolute_path))
        if errors:
            first = errors[0]
            raise ValidationError(
                f"Arguments for tool '{spec.name}' are invalid: {first.message}",
                tool=spec.name,
                path="/".join(str(p) for p in first.absolute_path),
                expectedSchema=spec.input_schema.get("properties", {}),
                violations=[
                    {
                        "path": "/".join(str(p) for p in e.absolute_path),
                        "message": e.message,
                    }
                    for e in errors[:10]
                ],
            )

    def _record(
        self,
        tool: str,
        outcome: str,
        duration_ms: float,
        tenant_id: str,
        detail: Mapping[str, Any],
    ) -> None:
        self._invocations.append({
            "tool": tool, "outcome": outcome, "durationMs": round(duration_ms, 3),
            "tenantId": tenant_id, "at": time.time(),
        })
        if MCP_TOOL_CALLS is not None:
            MCP_TOOL_CALLS.labels(plugin=self.info.name, tool=tool, outcome=outcome).inc()
        GLOBAL_METRICS.increment("mcp_tool_calls_total", plugin=self.info.name, outcome=outcome)
        GLOBAL_METRICS.observe("mcp_tool_duration_ms", duration_ms, plugin=self.info.name, tool=tool)
        get_audit_trail().record(
            AuditAction.MCP_TOOL_CALL,
            resource_type="mcp.tool",
            resource_id=tool,
            outcome=AuditOutcome.SUCCESS if outcome == "ok" else AuditOutcome.FAILURE,
            actor="mcp-agent",
            metadata={
                "server": self.info.name,
                "durationMs": round(duration_ms, 3),
                "tenantId": tenant_id,
                **dict(detail),
            },
        )

    @property
    def invocations(self) -> list[dict[str, Any]]:
        return list(self._invocations)

    def stats(self) -> dict[str, Any]:
        by_tool: dict[str, dict[str, Any]] = {}
        for entry in self._invocations:
            bucket = by_tool.setdefault(entry["tool"], {"calls": 0, "errors": 0, "totalMs": 0.0})
            bucket["calls"] += 1
            bucket["errors"] += 1 if entry["outcome"] != "ok" else 0
            bucket["totalMs"] += entry["durationMs"]
        for bucket in by_tool.values():
            bucket["avgMs"] = round(bucket["totalMs"] / bucket["calls"], 3) if bucket["calls"] else 0.0
        return {
            "server": self.info.name,
            "toolCount": len(self._tools),
            "resourceCount": len(self._resources),
            "promptCount": len(self._prompts),
            "invocationCount": len(self._invocations),
            "byTool": by_tool,
        }


def _mcp_principal(call: McpToolCall, scopes: frozenset[str], tenant: Tenant) -> RequestPrincipal:
    """The principal behind an MCP tool call.

    The tenant always comes from the session binding resolved by the transport
    layer, never from the tool arguments - that is what stops an agent from
    addressing another carrier.
    """
    return RequestPrincipal(
        subject=call.principal_subject or "mcp-agent",
        tenant_id=tenant.tenant_id,
        scopes=scopes,
        client_id=call.request_id or None,
        auth_method="mcp",
    )


def _template_to_regex(template: str) -> Any:  # noqa: ANN401
    import re  # noqa: PLC0415

    if "{" not in template:
        return None
    pattern = re.escape(template)
    pattern = re.sub(r"\\\{[^}]+\\\}", r"[^/]+", pattern)
    return re.compile(f"^{pattern}$")


def _infer_schema(func: Any) -> dict[str, Any]:  # noqa: ANN401
    """Derive a JSON Schema from a handler's type hints where possible.

    Falls back to a permissive object schema.  Being permissive is a deliberate
    trade-off: a wrong *narrow* schema silently blocks a valid agent call, while a
    permissive schema merely means the handler validates. Every handler in the
    suite also validates with pydantic.
    """
    from typing import get_type_hints  # noqa: PLC0415

    try:
        hints = get_type_hints(func)
    except Exception:  # noqa: BLE001
        return {"type": "object", "additionalProperties": True}
    if "arguments" not in hints:
        return {"type": "object", "additionalProperties": True}
    try:
        schema = _schema_from_model(hints["arguments"])
    except Exception:  # noqa: BLE001
        return {"type": "object", "additionalProperties": True}
    return schema


def _schema_from_model(annotation: Any) -> dict[str, Any]:  # noqa: ANN401
    from pydantic import BaseModel  # noqa: PLC0415

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        schema = annotation.model_json_schema(by_alias=True)
        schema.pop("title", None)
        defs = schema.pop("$defs", None)
        if defs:
            return {"type": "object", "properties": schema.get("properties", {}), "additionalProperties": False}
        return schema
    return {"type": "object", "additionalProperties": True}


def standard_catalogue_tool(registry: McpToolRegistry) -> McpToolSpec:
    """The ``catalogue_list_operations`` tool every plugin exposes.

    It is the agent's entry point: an agent that calls this first learns the full
    operation inventory, the error catalogue and the ACORD transaction codes
    without guessing.
    """

    async def _handler(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        return {
            "tenant": {
                "tenantId": ctx.tenant_id,
                "pasVendor": ctx.effective_pas_vendor,
            },
            "server": registry.info.to_dict(),
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "readOnly": t.annotations.read_only,
                    "destructive": t.destructive,
                    "inputSchema": t.input_schema,
                    "errorCodes": list(t.error_codes),
                }
                for t in (registry._tools[name] for name in registry.tool_names())  # noqa: SLF001
            ],
            "resources": registry.list_resources(),
            "prompts": registry.list_prompts(),
            "errorCatalog": [
                {"code": e.code.value, "httpStatus": e.http_status, "title": e.title,
                 "retryable": e.retryable}
                for e in _catalog_entries()
            ],
            "acordTransactionCodes": registry_as_json()["transactions"],
        }

    return registry.register_tool(
        McpToolSpec(
            name="catalogue_list_operations",
            title="List available operations",
            description=(
                "List every operation, resource, prompt, error code and ACORD transaction code "
                "this server exposes for the connected carrier. Call this first: it is the "
                "authoritative discovery surface and prevents guessing at tool names."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tag": {
                        "type": "string",
                        "description": "Optional filter to only include tools carrying this tag.",
                    },
                    "includeSchema": {
                        "type": "boolean",
                        "default": True,
                        "description": "Include full JSON Schemas (large). Set false for a summary.",
                    },
                },
                "additionalProperties": False,
            },
            handler=_handler,
            required_scopes=(),
            annotations=McpToolAnnotations(read_only=True, idempotent=True),
            plugin_id=registry.info.name,
            tags=("discovery", "meta"),
            examples=({"includeSchema": False},),
            error_codes=(ErrorCode.NOT_FOUND.value,),
        )
    )


def _catalog_entries() -> list[Any]:
    from pas_core.errors import ERROR_CATALOG  # noqa: PLC0415

    return list(ERROR_CATALOG.values())


def forbid_destructive_without_confirmation(
    tool: McpToolSpec, arguments: Mapping[str, Any]
) -> None:
    """Guard used by destructive tools when the agent has not confirmed intent.

    MCP has no standard confirmation primitive, so the convention is an explicit
    ``confirm: true`` argument on every destructive tool. This function is the
    single place that enforces it, so the rule cannot drift between plugins.
    """
    if not tool.destructive:
        return
    if arguments.get("confirm") is True:
        return
    raise PermissionDeniedError(
        f"Tool '{tool.name}' is destructive and requires \"confirm\": true",
        tool=tool.name,
        hint="Re-issue the call with confirm=true once the user has approved the change.",
    )
