"""MCP tools and resources for the gateway.

Every atomic operation is exposed as an MCP tool so an agent can discover and call
it with no bespoke integration.  The mapping is deliberate and mechanical:

======================  =========================================================
Canonical concept        MCP projection
======================  =========================================================
operation id             tool name (``policy.get`` -> ``policy_get``)
intent + summary         tool title + description
request schema           tool ``inputSchema``
required scopes          required OAuth scopes (enforced before invocation)
write operations         ``readOnlyHint: false``; confirm flag required
ACORD transaction type    advertised in the tool's ``_meta`` and the catalogue
======================  =========================================================

Plus the suite's cross-cutting tools, which no single operation provides:
``catalogue_list_operations``, ``workflow_run``, ``workflow_status``,
``acord_list_transaction_codes`` and ``translation_preview``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pas_core.acord.transaction import registry_as_json
from pas_core.errors import ErrorCode, PasError
from pas_core.mcp.registry import McpToolRegistry, standard_catalogue_tool
from pas_core.mcp.spec import (
    McpPromptSpec,
    McpResourceSpec,
    McpServerInfo,
    McpToolAnnotations,
    McpToolSpec,
)
from pas_core.pas.base import OperationRegistry, OperationSide
from pas_core.pas.registry import describe_vendor
from pas_core.pas.translation import get_engine
from pas_core.tenancy import TenantContext
from pas_plugins.plugin1_gateway.config import PLUGIN_INFO
from pas_plugins.plugin1_gateway.settings import Plugin1Settings

SETTINGS = Plugin1Settings()


def build_registry(operations: OperationRegistry) -> McpToolRegistry:
    """Build the gateway's MCP registry from the atomic operation catalogue."""
    registry = McpToolRegistry(
        McpServerInfo(
            name=PLUGIN_INFO.mcp_server_name,
            version=SETTINGS.plugin_version,
            title=PLUGIN_INFO.display_name,
            description=(
                "Atomic Policy Administration operations for Life and Annuity business, "
                "translated live from the carrier's own core system."
            ),
            instructions=PLUGIN_INFO.mcp_instructions,
        )
    )

    for operation in operations.list():
        registry.register_tool(_tool_for(operation))
        registry.register_resource(_resource_for(operation))

    _register_platform_tools(registry, operations)
    standard_catalogue_tool(registry)
    return registry


def _tool_name(operation_id: str) -> str:
    """``policy.cashValue.get`` -> ``policy_cash_value_get``.

    MCP tool names must be snake_case: they become identifiers in the agent's own
    runtime, and several agent frameworks reject camelCase.
    """
    return re.sub(r"(?<!^)(?=[A-Z])", "_", operation_id.replace(".", "_").replace("-", "_")).lower()


def _tool_for(operation: Any) -> McpToolSpec:  # noqa: ANN401
    """Project an atomic operation into an MCP tool."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for parameter in operation.parameters:
        if parameter.location == "path":
            properties[parameter.name] = {
                **(parameter.schema_ or {"type": "string"}),
                "description": parameter.description,
            }
            required.append(parameter.name)
    if operation.request_schema:
        properties.update(operation.request_schema.get("properties", {}))
        required.extend(operation.request_schema.get("required", []))

    if operation.side is OperationSide.WRITE:
        properties["confirm"] = {
            "type": "boolean",
            "default": False,
            "description": (
                "Must be true. Destructive and state-changing tools require explicit "
                "confirmation so an agent cannot mutate a policy as a side effect."
            ),
        }

    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
    # Path and body parameters are mutually exclusive in the HTTP API; accepting
    # both here would create an ambiguity the schema cannot express.
    input_schema["description"] = (
        f"{operation.summary}. Intent: {operation.intent}. "
        f"Endpoint: {operation.method} {operation.path}."
    )

    return McpToolSpec(
        name=_tool_name(operation.operation_id),
        title=operation.summary,
        description=(
            f"{operation.summary}. Intent: {operation.intent}. "
            f"HTTP equivalent: {operation.method} {operation.path}. "
            f"ACORD transaction: {operation.acord_tx_type or 'n/a'}. "
            f"Target latency: {operation.slo_ms}ms."
        ),
        input_schema=input_schema,
        handler=_handler_for(operation),
        output_schema=operation.response_schema,
        required_scopes=operation.required_scopes,
        annotations=McpToolAnnotations(
            read_only=operation.side is not OperationSide.WRITE,
            destructive=operation.side is OperationSide.WRITE,
            idempotent=operation.side is OperationSide.READ,
        ),
        operation_id=operation.operation_id,
        plugin_id=PLUGIN_INFO.plugin_id,
        tags=operation.tags,
        examples=(operation.request_example,) if operation.request_example else (),
        error_codes=tuple(e.value for e in operation.errors),
        rate_limit_policy=operation.rate_limit_policy,
    )


def _handler_for(operation: Any) -> Any:  # noqa: ANN401
    """Build the async handler that routes an MCP tool call to the PAS."""
    from pas_core.pas.registry import get_adapter  # noqa: PLC0415
    from pas_core.pas.translation import get_engine  # noqa: PLC0415

    async def handler(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        if operation.side is OperationSide.WRITE and arguments.get("confirm") is not True:
            from pas_core.errors import PermissionDeniedError  # noqa: PLC0415

            raise PermissionDeniedError(
                f"Tool '{operation.operation_id}' changes state and requires \"confirm\": true",
                tool=operation.operation_id,
                hint="Re-issue the call with confirm=true once the change has been approved.",
            )

        payload = {k: v for k, v in arguments.items() if k != "confirm"}
        vendor = ctx.effective_pas_vendor
        engine = get_engine()
        outbound = engine.to_vendor(vendor, operation.operation_id, payload)
        adapter = get_adapter(vendor)
        adapter_payload = adapter.translate_request(operation, outbound.payload)
        raw = await adapter.execute(operation, adapter_payload, ctx, path_params=payload)
        normalised = adapter.translate_response(operation, raw)
        inbound = engine.from_vendor(vendor, operation.operation_id, normalised)
        result = inbound.payload if inbound.ok else normalised
        result.setdefault("_meta", {})
        result["_meta"] = {
            "operationId": operation.operation_id,
            "vendor": vendor,
            "acordTxType": str(operation.acord_tx_type) if operation.acord_tx_type else None,
            "correlationId": ctx.correlation_id,
        }
        return result

    handler.__name__ = _tool_name(operation.operation_id)
    handler.__doc__ = operation.summary
    return handler


def _resource_for(operation: Any) -> McpResourceSpec:  # noqa: ANN401
    """Expose the same data as an MCP resource for read-only contexts."""
    is_read = operation.side is OperationSide.READ
    # The canonical REST path is reused as the MCP URI so an agent (or a human
    # reading the catalogue) can map one onto the other without a lookup table.
    uri = (
        "pas://" + operation.path.strip("/").removeprefix("insurance/v1/").removeprefix("insurance/v1")
        if is_read
        else f"pas://operations/{operation.operation_id}"
    )

    async def read_handler(params: dict[str, str], ctx: TenantContext) -> dict[str, Any]:
        from pas_core.pas.registry import get_adapter  # noqa: PLC0415

        adapter = get_adapter(ctx.effective_pas_vendor)
        return await adapter.execute(operation, dict(params), ctx, path_params=params)

    return McpResourceSpec(
        uri_template=uri,
        name=_tool_name(operation.operation_id),
        title=operation.summary,
        description=f"{operation.summary} (read-only resource view).",
        mime_type="application/json",
        handler=read_handler,
        plugin_id=PLUGIN_INFO.plugin_id,
        tags=operation.tags,
    )


def _register_platform_tools(registry: McpToolRegistry, operations: OperationRegistry) -> None:
    """Register the gateway's own tools, which cut across operations."""

    async def list_vendors(_: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        """Describe the carrier's connected PAS: transport, API style, quirks."""
        from pas_core.pas.registry import available_vendors  # noqa: PLC0415

        return {
            "connectedVendor": ctx.effective_pas_vendor,
            "vendors": [describe_vendor(v["vendor"]) for v in available_vendors()],
        }

    registry.register_tool(
        McpToolSpec(
            name="pas_describe_vendor",
            title="Describe the connected Policy Administration System",
            description=(
                "Report which PAS vendor this tenant is connected to and what that vendor's "
                "integration requires: transport, API style, whether it supports idempotency, "
                "and any known quirks that affect how results should be interpreted."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=list_vendors,
            annotations=McpToolAnnotations(read_only=True, idempotent=True),
            plugin_id=PLUGIN_INFO.plugin_id,
            tags=("discovery", "pas"),
            examples=({},),
            error_codes=(ErrorCode.PAS_OPERATION_UNAVAILABLE.value,),
        )
    )

    async def list_acord(_: dict[str, Any], __: TenantContext) -> dict[str, Any]:
        """List the ACORD NGDS transaction codes this gateway can emit."""
        return registry_as_json()

    registry.register_tool(
        McpToolSpec(
            name="acord_list_transaction_codes",
            title="List ACORD NGDS transaction codes",
            description=(
                "Return the ACORD Next-Generation Data Standards transaction code registry for "
                "Life and Annuity business, including which code each gateway operation emits "
                "and its legacy vendor synonyms. Use this to map an agent's intent onto the "
                "carrier's expected transaction type."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=list_acord,
            annotations=McpToolAnnotations(read_only=True, idempotent=True),
            plugin_id=PLUGIN_INFO.plugin_id,
            tags=("discovery", "acord"),
            examples=({},),
            error_codes=(),
        )
    )

    async def preview_translation(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        """Show how a payload would be rewritten for the carrier's vendor dialect."""
        vendor = str(arguments.get("vendor") or ctx.effective_pas_vendor)
        operation_id = str(arguments["operationId"])
        payload = dict(arguments.get("payload") or {})
        direction = str(arguments.get("direction") or "to_vendor")
        engine = get_engine()
        if direction == "from_vendor":
            result = engine.from_vendor(vendor, operation_id, payload)
        else:
            result = engine.to_vendor(vendor, operation_id, payload)
        profile = engine.profile(vendor)
        return {
            "vendor": vendor,
            "operationId": operation_id,
            "direction": direction,
            "profileVersion": profile.version if profile else None,
            "translated": result.payload,
            "trace": result.as_trace(),
        }

    registry.register_tool(
        McpToolSpec(
            name="translation_preview",
            title="Preview a vendor translation",
            description=(
                "Show exactly how the gateway would rewrite a payload for the carrier's PAS "
                "vendor dialect, together with the rule trace. Useful when debugging an "
                "integration or explaining to a carrier why a payload looks different."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "operationId": {
                        "type": "string",
                        "description": "The atomic operation whose rules should be applied.",
                        "examples": ["policy.get"],
                    },
                    "vendor": {
                        "type": "string",
                        "description": "Vendor id. Defaults to this tenant's connected PAS.",
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["to_vendor", "from_vendor"],
                        "default": "to_vendor",
                        "description": "Translate towards the vendor, or back to canonical.",
                    },
                    "payload": {
                        "type": "object",
                        "description": "The payload to translate.",
                        "default": {},
                    },
                },
                "required": ["operationId"],
                "additionalProperties": False,
            },
            handler=preview_translation,
            annotations=McpToolAnnotations(read_only=True, idempotent=True),
            plugin_id=PLUGIN_INFO.plugin_id,
            tags=("translation", "pas"),
            examples=({"operationId": "policy.get", "payload": {"policyId": "POL1001"}}),
            error_codes=(ErrorCode.TRANSLATION_FAILED.value,),
        )
    )

    # --- workflows -------------------------------------------------------
    from pas_plugins.plugin1_gateway.workflows import WORKFLOWS  # noqa: PLC0415

    async def run_workflow(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        """Execute a business workflow such as quote-to-bind or beneficiary change."""
        from pas_plugins.plugin1_gateway.service import get_workflow_engine  # noqa: PLC0415

        workflow_id = str(arguments["workflowId"])
        definition = next(
            (w for w in WORKFLOWS if w.workflow_id == workflow_id), None
        )
        if definition is None:
            raise PasError(
                ErrorCode.NOT_FOUND,
                f"Unknown workflow '{workflow_id}'",
                {"workflowId": workflow_id, "available": [w.workflow_id for w in WORKFLOWS]},
            )
        engine = get_workflow_engine()
        execution = await engine.start(definition, ctx, dict(arguments.get("input") or {}))
        return execution.to_dict()

    registry.register_tool(
        McpToolSpec(
            name="workflow_run",
            title="Run a business workflow",
            description=(
                "Compose atomic operations into a carrier business process. Available "
                "workflows include quote-to-bind, accelerated underwriting and beneficiary "
                "change with multi-signature approval. The workflow validates preconditions, "
                "retries transient failures and compensates partial work."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workflowId": {
                        "type": "string",
                        "description": "Which workflow to run.",
                        "examples": ["quote-to-bind", "beneficiary-change"],
                    },
                    "input": {
                        "type": "object",
                        "description": "Workflow input; the shape depends on the workflow.",
                        "default": {},
                    },
                },
                "required": ["workflowId"],
                "additionalProperties": False,
            },
            handler=run_workflow,
            output_schema=None,
            required_scopes=("workflow:write",),
            annotations=McpToolAnnotations(read_only=False, destructive=False, idempotent=False),
            plugin_id=PLUGIN_INFO.plugin_id,
            tags=("workflow", "orchestration"),
            examples=({"workflowId": "quote-to-bind", "input": {"productCode": "TERM20-A"}}),
            error_codes=(ErrorCode.NOT_FOUND.value, ErrorCode.INTERNAL.value),
            rate_limit_policy="tenant-default",
        )
    )

    async def workflow_status(arguments: dict[str, Any], __: TenantContext) -> dict[str, Any]:
        """Read the execution trace of a workflow run."""
        from pas_plugins.plugin1_gateway.service import get_workflow_engine  # noqa: PLC0415

        execution = await get_workflow_engine().get(str(arguments["executionId"]))
        if execution is None:
            raise PasError(
                ErrorCode.NOT_FOUND,
                f"Workflow execution '{arguments['executionId']}' was not found",
                {"executionId": arguments["executionId"]},
            )
        return execution.to_dict()

    registry.register_tool(
        McpToolSpec(
            name="workflow_status",
            title="Read a workflow execution",
            description=(
                "Return the full step-by-step trace of a workflow run: which steps ran, their "
                "status, duration, outputs and any errors. Use this to diagnose a workflow that "
                "did not complete."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "executionId": {"type": "string", "description": "Execution id from workflow_run."},
                },
                "required": ["executionId"],
                "additionalProperties": False,
            },
            handler=workflow_status,
            annotations=McpToolAnnotations(read_only=True, idempotent=True),
            plugin_id=PLUGIN_INFO.plugin_id,
            tags=("workflow", "orchestration"),
            examples=({"executionId": "a1b2c3"}),
            error_codes=(ErrorCode.NOT_FOUND.value,),
        )
    )

    async def list_workflows(_: dict[str, Any], __: TenantContext) -> dict[str, Any]:
        """Describe every workflow the gateway can orchestrate."""
        from pas_plugins.plugin1_gateway.workflows import describe  # noqa: PLC0415

        return describe(WORKFLOWS)

    registry.register_tool(
        McpToolSpec(
            name="workflow_list",
            title="List available workflows",
            description=(
                "List every business workflow the gateway can orchestrate, with its steps, "
                "execution order, dependencies and compensating actions."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=list_workflows,
            annotations=McpToolAnnotations(read_only=True, idempotent=True),
            plugin_id=PLUGIN_INFO.plugin_id,
            tags=("workflow", "discovery"),
            examples=({},),
            error_codes=(),
        )
    )

    # --- resources and prompts ------------------------------------------
    registry.register_resource(
        McpResourceSpec(
            uri_template="pas://vendors",
            name="pas_vendors",
            title="Supported PAS vendors",
            description="The vendor capability matrix: transport, API style and known quirks.",
            handler=lambda params, ctx: _vendors_resource(ctx),
            plugin_id=PLUGIN_INFO.plugin_id,
            tags=("discovery", "pas"),
        )
    )
    registry.register_resource(
        McpResourceSpec(
            uri_template="pas://acord/transaction-codes",
            name="acord_transaction_codes",
            title="ACORD NGDS transaction codes",
            description="The full transaction type code registry for Life and Annuity business.",
            handler=lambda params, ctx: _acord_resource(),
            plugin_id=PLUGIN_INFO.plugin_id,
            tags=("discovery", "acord"),
        )
    )
    registry.register_resource(
        McpResourceSpec(
            uri_template="pas://workflows",
            name="workflow_catalogue",
            title="Workflow catalogue",
            description="Every orchestrable business process with its step graph.",
            handler=lambda params, ctx: _workflows_resource(),
            plugin_id=PLUGIN_INFO.plugin_id,
            tags=("workflow",),
        )
    )
    registry.register_prompt(
        McpPromptSpec(
            name="underwrite_and_issue",
            title="Underwrite and issue a life application",
            description=(
                "A step-by-step playbook for taking an applicant from intake to an in-force "
                "policy, using only the atomic tools this server exposes."
            ),
            template=(
                "Underwrite and issue a life policy for the following applicant.\n\n"
                "Applicant: {applicant}\n"
                "Product: {productCode}\n"
                "Requested face amount: {faceAmount}\n"
                "State of issue: {stateOfIssue}\n\n"
                "Follow these steps and stop if any step fails:\n"
                "1. Read the product definition with `product_get` and confirm the applicant "
                "is within the filed issue-age and face-amount bounds.\n"
                "2. Compute an indicative premium with `policy_premium_calculate`.\n"
                "3. If the indicative premium is acceptable, create a quote with "
                "`policy_quote_create` and tell the user the quote id and expiry.\n"
                "4. If the applicant asked to proceed, call `policy_bind` with confirm=true. "
                "Never call policy_bind without a fresh, accepted quote.\n"
                "5. Report the policy number, effective date and first premium due.\n\n"
                "Rules you must not break:\n"
                "- Never issue a policy the product is not filed for in the applicant's state.\n"
                "- Never infer a beneficiary allocation; ask the user.\n"
                "- If a required medical requirement is outstanding, do not issue - explain "
                "what is needed instead."
            ),
            arguments=(
                {"name": "applicant", "description": "Applicant details", "required": True},
                {"name": "productCode", "description": "Product code", "required": True},
                {"name": "faceAmount", "description": "Requested face amount", "required": True},
                {"name": "stateOfIssue", "description": "Two-letter state code", "required": True},
            ),
            plugin_id=PLUGIN_INFO.plugin_id,
        )
    )
    registry.register_prompt(
        McpPromptSpec(
            name="beneficiary_change_explainer",
            title="Explain a beneficiary change before making it",
            description=(
                "Guides the user through the consequences of a beneficiary change, including "
                "tax implications and multi-signature requirements, before the change is made."
            ),
            template=(
                "The policyholder wants to change the beneficiaries on policy {policyId}.\n\n"
                "Requested change:\n{allocations}\n\n"
                "Before calling `policy_beneficiary_update`:\n"
                "1. Confirm the current schedule with `policy_beneficiary_list` and show the "
                "difference to the user.\n"
                "2. Explain that shares must not exceed 100 percent, and that any existing "
                "allocation not replaced by this call reverts to the default beneficiary.\n"
                "3. Explain that with multi-signature approval every allocated party must "
                "counter-sign before the change takes effect.\n"
                "4. Ask for explicit confirmation, then call the tool with confirm=true."
            ),
            arguments=(
                {"name": "policyId", "description": "Policy number", "required": True},
                {"name": "allocations", "description": "Requested beneficiary allocations", "required": True},
            ),
            plugin_id=PLUGIN_INFO.plugin_id,
        )
    )


async def _vendors_resource(ctx: TenantContext) -> dict[str, Any]:
    from pas_core.pas.registry import available_vendors  # noqa: PLC0415

    return {
        "connectedVendor": ctx.effective_pas_vendor,
        "vendors": [describe_vendor(v["vendor"]) for v in available_vendors()],
    }


async def _acord_resource() -> dict[str, Any]:
    return registry_as_json()


async def _workflows_resource() -> dict[str, Any]:
    from pas_plugins.plugin1_gateway.workflows import WORKFLOWS, describe  # noqa: PLC0415

    return describe(WORKFLOWS)


def catalogue_document(operations: OperationRegistry) -> str:
    """The discovery document as canonical JSON (used by ``GET /mcp/catalogue``)."""
    registry = build_registry(operations)
    return json.dumps(registry.catalogue(), indent=2, default=str)
