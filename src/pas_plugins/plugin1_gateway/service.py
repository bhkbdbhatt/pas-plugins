"""Plugin 1 service layer.

Holds the singletons that the routers, the MCP tools and the workflows share:
the operation catalogue, the PAS adapter resolution, the workflow engine and the
lifecycle event bridge to plugin 7.
"""

from __future__ import annotations

import logging
from typing import Any

from pas_core.errors import ErrorCode, PasError
from pas_core.pas.base import OperationRegistry
from pas_core.pas.registry import get_adapter
from pas_core.pas.translation import get_engine
from pas_core.tenancy import TenantContext
from pas_core.workflow.engine import LocalWorkflowEngine, WorkflowEngine
from pas_plugins.plugin1_gateway.operations import CATALOGUE

logger = logging.getLogger("pas_plugins.plugin1.service")

_WORKFLOW_ENGINE: WorkflowEngine | None = None
_INVOKER: Any = None
_LIFECYCLE_SINK: Any = None


def get_operations() -> OperationRegistry:
    """The atomic operation catalogue."""
    return CATALOGUE


def set_workflow_engine(engine: WorkflowEngine) -> WorkflowEngine:
    global _WORKFLOW_ENGINE  # noqa: PLW0603
    _WORKFLOW_ENGINE = engine
    return engine


def get_workflow_engine() -> WorkflowEngine:
    """The workflow engine, defaulting to the local deterministic engine."""
    global _WORKFLOW_ENGINE  # noqa: PLW0603
    if _WORKFLOW_ENGINE is None:
        _WORKFLOW_ENGINE = LocalWorkflowEngine(operation_invoker=invoke_operation)
        from pas_plugins.plugin1_gateway.workflows import WORKFLOWS  # noqa: PLC0415

        _WORKFLOW_ENGINE.register_all(WORKFLOWS)  # type: ignore[union-attr]
    return _WORKFLOW_ENGINE


async def invoke_operation(
    operation_id: str, payload: dict[str, Any], ctx: TenantContext
) -> dict[str, Any]:
    """Invoke one atomic operation, applying the translation rules.

    Shared by the REST router, the MCP tools and the workflow engine so all three
    behave identically - including audit, rate limiting and vendor translation.
    """
    operation = CATALOGUE.get(operation_id)
    vendor = ctx.effective_pas_vendor
    adapter = get_adapter(vendor)
    if not adapter.supports(operation_id):
        raise PasError(
            ErrorCode.PAS_OPERATION_UNAVAILABLE,
            f"PAS vendor '{vendor}' does not expose operation '{operation_id}'",
            {"vendor": vendor, "operationId": operation_id},
        )
    engine = get_engine()
    outbound = engine.to_vendor(vendor, operation_id, payload)
    if not outbound.ok:
        raise PasError(
            ErrorCode.TRANSLATION_FAILED,
            f"Request translation failed for vendor '{vendor}'",
            {"vendor": vendor, "operationId": operation_id, **outbound.as_trace()},
        )
    raw = await adapter.execute(
        operation, adapter.translate_request(operation, outbound.payload), ctx, path_params=payload
    )
    normalised = adapter.translate_response(operation, raw)
    inbound = engine.from_vendor(vendor, operation_id, normalised)
    return inbound.payload if inbound.ok else normalised


def set_operation_invoker(invoker: Any) -> None:  # noqa: ANN401
    global _INVOKER  # noqa: PLW0603
    _INVOKER = invoker


def get_operation_invoker() -> Any:  # noqa: ANN401
    return _INVOKER or invoke_operation


def set_lifecycle_sink(sink: Any) -> None:  # noqa: ANN401
    """Register the plugin 7 bridge used by the beneficiary-change workflow."""
    global _LIFECYCLE_SINK  # noqa: PLW0603
    _LIFECYCLE_SINK = sink


async def publish_lifecycle_event(
    *, transaction_type: str, policy_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Commit a policy state change to the lifecycle ledger.

    When plugin 7 is deployed the event is written to the permissioned ledger.
    Without it, the event is logged and returned so the calling workflow still
    completes - plugin 1 must not become unusable because an optional companion
    is not deployed, but the absence is explicit in the result.
    """
    if _LIFECYCLE_SINK is not None:
        return await _LIFECYCLE_SINK(transaction_type=transaction_type, policy_id=policy_id, payload=payload)
    logger.info(
        "lifecycle event not committed: plugin 7 bridge is not configured",
        extra={"transactionType": transaction_type, "policyId": policy_id},
    )
    return {
        "recorded": False,
        "reason": "plugin7_bridge_not_configured",
        "transactionType": transaction_type,
        "policyId": policy_id,
    }


def reset() -> None:
    """Clear module state (tests only)."""
    global _WORKFLOW_ENGINE, _INVOKER, _LIFECYCLE_SINK  # noqa: PLW0603
    _WORKFLOW_ENGINE = None
    _INVOKER = None
    _LIFECYCLE_SINK = None
