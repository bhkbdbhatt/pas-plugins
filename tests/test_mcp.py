"""MCP layer tests.

Verifies the properties that make MCP safe to expose a carrier's core system to
an AI agent: tenant binding, scope enforcement, destructive-tool confirmation,
error mapping and deterministic discovery.
"""

from __future__ import annotations

import json

import pytest

from pas_core.errors import ErrorCode, PasError
from pas_core.mcp.registry import McpToolCall, McpToolRegistry, forbid_destructive_without_confirmation
from pas_core.mcp.spec import McpPromptSpec, McpServerInfo, McpToolAnnotations, McpToolSpec
from pas_core.tenancy import RequestPrincipal, Tenant


ALL_SCOPES = ["*"]


ALL_SCOPES = ["*"]


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(tenant_id="demo-carrier", legal_name="Demo Mutual Life", pas_vendor="simulated")


@pytest.fixture
def gateway_registry():
    from pas_plugins.plugin1_gateway.mcp_server import build_registry
    from pas_plugins.plugin1_gateway.operations import CATALOGUE

    return build_registry(CATALOGUE)


def test_tool_names_are_snake_case(gateway_registry: McpToolRegistry) -> None:
    import re

    for name in gateway_registry.tool_names():
        assert re.fullmatch(r"[a-z][a-z0-9_]*", name), name


def test_every_operation_has_a_tool(gateway_registry: McpToolRegistry) -> None:
    from pas_plugins.plugin1_gateway.operations import CATALOGUE  # noqa: PLC0415

    names = set(gateway_registry.tool_names())
    for operation in CATALOGUE.list():
        expected = operation.operation_id.replace(".", "_").replace("-", "_")
        expected = __import__("re").sub(r"(?<!^)(?=[A-Z])", "_", expected).lower()
        assert expected in names, f"{operation.operation_id} has no MCP tool"


def test_write_tools_are_marked_destructive(gateway_registry: McpToolRegistry) -> None:
    bind = gateway_registry.get_tool("policy_bind")
    assert bind.annotations.read_only is False
    assert bind.annotations.destructive is True
    assert "confirm" in bind.input_schema["properties"]

    read = gateway_registry.get_tool("policy_get")
    assert read.annotations.read_only is True
    assert read.annotations.destructive is False


def test_write_tool_requires_confirmation(gateway_registry: McpToolRegistry, tenant: Tenant) -> None:
    import asyncio

    refused = asyncio.run(
        gateway_registry.invoke(
            McpToolCall(name="policy_bind", arguments={"quoteId": "QT1", "effectiveDate": "2026-06-01"}),
            tenant=tenant,
        principal_scopes=ALL_SCOPES,
        )
    )
    assert refused.is_error
    assert refused.payload["code"] == ErrorCode.PERMISSION_DENIED.value

    allowed = asyncio.run(
        gateway_registry.invoke(
            McpToolCall(
                name="policy_bind",
                arguments={"quoteId": "QT1", "effectiveDate": "2026-06-01", "confirm": True},
            ),
            tenant=tenant,
        principal_scopes=ALL_SCOPES,
        )
    )
    assert allowed.ok, allowed.payload
    assert allowed.payload["policyId"]


def test_compute_tool_is_side_effect_free(gateway_registry: McpToolRegistry, tenant: Tenant) -> None:
    import asyncio

    result = asyncio.run(
        gateway_registry.invoke(
            McpToolCall(
                name="policy_premium_calculate",
                arguments={
                    "productCode": "TERM20-A",
                    "faceAmount": 250_000,
                    "issueAge": 45,
                    "stateOfIssue": "NY",
                },
            ),
            tenant=tenant,
        principal_scopes=ALL_SCOPES,
        )
    )
    assert result.ok, result.payload
    assert result.payload["annualisedPremium"] > 0
    assert result.annotations.read_only is True


def test_argument_validation_rejects_bad_payload(gateway_registry: McpToolRegistry, tenant: Tenant) -> None:
    import asyncio

    result = asyncio.run(
        gateway_registry.invoke(
            McpToolCall(name="policy_premium_calculate", arguments={"faceAmount": 250_000}),
            tenant=tenant,
        principal_scopes=ALL_SCOPES,
        )
    )
    assert result.is_error
    assert result.payload["code"] == ErrorCode.VALIDATION_FAILED.value
    assert result.payload["retryable"] is False


def test_scope_enforcement(gateway_registry: McpToolRegistry, tenant: Tenant) -> None:
    import asyncio

    result = asyncio.run(
        gateway_registry.invoke(
            McpToolCall(name="policy_get", arguments={"policyId": "SIMPOL000001"}),
            tenant=tenant,
            principal_scopes=["product:read"],
        )
    )
    assert result.is_error
    assert result.payload["code"] == ErrorCode.SCOPE_INSUFFICIENT.value
    assert "policy:read" in result.payload["errors"]["required"]


def test_unknown_tool_lists_alternatives(tenant: Tenant) -> None:
    import asyncio

    registry = McpToolRegistry(McpServerInfo(name="t", version="1"))

    async def handler(_: dict, __: object) -> dict:
        return {}

    registry.register_tool(
        McpToolSpec(
            name="only_tool",
            title="Only",
            description="d",
            input_schema={"type": "object", "properties": {}},
            handler=handler,
        )
    )
    result = asyncio.run(registry.invoke(McpToolCall(name="nope", arguments={}), tenant=tenant, principal_scopes=ALL_SCOPES))
    assert result.is_error
    assert result.payload["errors"]["availableTools"] == ["only_tool"]


def test_every_call_is_audited(gateway_registry: McpToolRegistry, tenant: Tenant) -> None:
    import asyncio

    before = len(gateway_registry.invocations)
    asyncio.run(
        gateway_registry.invoke(
            McpToolCall(name="acord_list_transaction_codes", arguments={}), tenant=tenant,
            principal_scopes=ALL_SCOPES
        )
    )
    entries = gateway_registry.invocations[before:]
    assert len(entries) == 1
    assert entries[0]["tool"] == "acord_list_transaction_codes"
    assert entries[0]["tenantId"] == "demo-carrier"


def test_catalogue_is_agent_discoverable(gateway_registry: McpToolRegistry) -> None:
    catalogue = gateway_registry.catalogue()
    assert catalogue["server"]["protocolVersion"] == "2025-06-18"
    assert catalogue["tools"]
    tool = next(t for t in catalogue["tools"] if t["name"] == "policy_premium_calculate")
    assert tool["readOnly"] is True
    assert "faceAmount" in tool["inputSchema"]["properties"]
    assert tool["examples"]
    assert catalogue["acordNgds"]["transactions"]


def test_resources_use_pas_scheme(gateway_registry: McpToolRegistry) -> None:
    for resource in gateway_registry.list_resources():
        assert resource["uriTemplate"].startswith("pas://")


def test_resource_template_resolution(gateway_registry: McpToolRegistry) -> None:
    spec = gateway_registry.resolve_resource("pas://policies/POL1234")
    assert spec.uri_template == "pas://policies/{policyId}"
    params = McpToolRegistry.extract_template_params(
        "pas://policies/POL1234", "pas://policies/{policyId}"
    )
    assert params == {"policyId": "POL1234"}


def test_prompt_rendering() -> None:
    prompt = McpPromptSpec(
        name="demo_prompt",
        title="Demo",
        description="d",
        template="Hello {name}, policy {policyId}",
        arguments=({"name": "name"}, {"name": "policyId"}),
    )
    assert prompt.render({"name": "Jane", "policyId": "POL1"}) == "Hello Jane, policy POL1"
    assert "{policyId}" in prompt.render({"name": "Jane"})


def test_destructive_guard_is_centralised() -> None:
    destructive = McpToolSpec(
        name="destructive_op",
        title="D",
        description="d",
        input_schema={"type": "object", "properties": {}},
        annotations=McpToolAnnotations(read_only=False, destructive=True, idempotent=False),
    )
    with pytest.raises(PasError):
        forbid_destructive_without_confirmation(destructive, {})
    forbid_destructive_without_confirmation(destructive, {"confirm": True})


def test_tool_result_wire_format(gateway_registry: McpToolRegistry, tenant: Tenant) -> None:
    import asyncio

    result = asyncio.run(
        gateway_registry.invoke(
            McpToolCall(name="pas_describe_vendor", arguments={}), tenant=tenant,
            principal_scopes=ALL_SCOPES
        )
    )
    wire = result.to_wire()
    assert wire["isError"] is False
    assert wire["structuredContent"]["connectedVendor"] == "simulated"
    assert json.loads(wire["content"][0]["text"])["connectedVendor"] == "simulated"


def test_tool_registries_are_isolated_between_plugins() -> None:
    """Two plugins can register the same tool name without colliding."""
    from pas_plugins.plugin1_gateway.mcp_server import build_registry as build_gateway  # noqa: PLC0415
    from pas_plugins.plugin1_gateway.operations import CATALOGUE  # noqa: PLC0415

    first = build_gateway(CATALOGUE)
    second = McpToolRegistry(McpServerInfo(name="other", version="1"))

    async def handler(_: dict, __: object) -> dict:
        return {}

    second.register_tool(
        McpToolSpec(
            name="policy_get",
            title="Other",
            description="d",
            input_schema={"type": "object", "properties": {}},
            handler=handler,
        )
    )
    assert first.get_tool("policy_get").title != second.get_tool("policy_get").title


def test_tenant_is_bound_from_the_token_not_the_arguments(tenant: Tenant) -> None:
    """An agent cannot address another carrier by naming it in the arguments."""
    import asyncio

    registry = McpToolRegistry(McpServerInfo(name="t", version="1"))
    seen: dict[str, str] = {}

    async def handler(arguments: dict, ctx: object) -> dict:
        seen["tenant"] = ctx.tenant_id  # type: ignore[attr-defined]
        seen["claimed"] = arguments.get("tenantId", "<absent>")
        return {"ok": True}

    registry.register_tool(
        McpToolSpec(
            name="read_something",
            title="R",
            description="d",
            input_schema={
                "type": "object",
                "properties": {"tenantId": {"type": "string"}},
                "additionalProperties": False,
            },
            handler=handler,
        )
    )
    asyncio.run(
        registry.invoke(
            McpToolCall(name="read_something", arguments={"tenantId": "victim-carrier"}),
            tenant=tenant,
        principal_scopes=ALL_SCOPES,
        )
    )
    assert seen["tenant"] == "demo-carrier"


def test_principal_scopes_are_hierarchical() -> None:
    principal = RequestPrincipal(subject="a", tenant_id="t", scopes=frozenset({"policy:*"}))
    assert principal.has_scope("policy:write")
    assert not principal.has_scope("claim:write")
