"""Plugin 1 HTTP API: workflows, PAS diagnostics and MCP helpers.

The atomic operations themselves are mounted automatically by the app factory
from :mod:`pas_plugins.plugin1_gateway.operations`; this module adds the
orchestration and diagnostics surface.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from pas_core.app import Platform
from pas_core.errors import ErrorCode, PasError
from pas_core.observability import GLOBAL_METRICS
from pas_core.pas.registry import describe_vendor, get_adapter
from pas_core.pas.translation import get_engine
from pas_core.tenancy import TenantContext
from pas_plugins.plugin1_gateway import operations as catalogue_module
from pas_plugins.plugin1_gateway.config import PLUGIN_INFO
from pas_plugins.plugin1_gateway.service import get_workflow_engine
from pas_plugins.plugin1_gateway.workflows import WORKFLOWS, describe

router = APIRouter(tags=["orchestration"])


def build_router(platform: Platform) -> APIRouter:
    """Attach the platform-bound routes."""
    dependency = _dependency(platform)

    @router.get(
        "/workflows",
        summary="List orchestrable workflows",
        operation_id="workflow.list",
    )
    async def list_workflows(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """Every business workflow the gateway can orchestrate, with its step graph."""
        return {
            "tenantId": ctx.tenant_id,
            "engine": get_workflow_engine().__class__.__name__,
            **describe(WORKFLOWS),
            "summary": {
                "count": len(WORKFLOWS),
                "totalSteps": sum(len(w.steps) for w in WORKFLOWS),
            },
        }

    @router.get(
        "/workflows/{workflow_id}",
        summary="Workflow detail",
        operation_id="workflow.get",
    )
    async def get_workflow(
        workflow_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """One workflow's steps, execution order and compensating actions."""
        ctx.principal.require_scopes("policy:read")
        definition = next((w for w in WORKFLOWS if w.workflow_id == workflow_id), None)
        if definition is None:
            raise PasError(
                ErrorCode.NOT_FOUND,
                f"Unknown workflow '{workflow_id}'",
                {"workflowId": workflow_id, "available": [w.workflow_id for w in WORKFLOWS]},
            )
        return definition.to_dict()

    @router.post(
        "/workflows/{workflow_id}/executions",
        summary="Start a workflow execution",
        operation_id="workflow.start",
    )
    async def start_workflow(
        workflow_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Start a workflow and return the execution with its full step trace."""
        ctx.principal.require_scopes("workflow:write")
        definition = next((w for w in WORKFLOWS if w.workflow_id == workflow_id), None)
        if definition is None:
            raise PasError(
                ErrorCode.NOT_FOUND,
                f"Unknown workflow '{workflow_id}'",
                {"workflowId": workflow_id, "available": [w.workflow_id for w in WORKFLOWS]},
            )
        body: dict[str, Any] = await _json_body(request)
        execution = await get_workflow_engine().start(definition, ctx, body.get("input") or {})
        return execution.to_dict()

    @router.get(
        "/workflows/executions/{execution_id}",
        summary="Read a workflow execution",
        operation_id="workflow.execution",
    )
    async def get_execution(
        execution_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """The step-by-step trace of one execution, for diagnosing a failure."""
        ctx.principal.require_scopes("policy:read")
        execution = await get_workflow_engine().get(execution_id)
        if execution is None or execution.tenant_id != ctx.tenant_id:
            raise PasError(
                ErrorCode.NOT_FOUND,
                f"Workflow execution '{execution_id}' was not found",
                {"executionId": execution_id},
            )
        return execution.to_dict()

    @router.get(
        "/orchestration/stats",
        summary="Workflow engine statistics",
        operation_id="workflow.stats",
    )
    async def orchestration_stats(
        request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        engine = get_workflow_engine()
        stats = engine.stats() if hasattr(engine, "stats") else {}
        executions = engine.executions(tenant_id=ctx.tenant_id) if hasattr(engine, "executions") else []
        return {
            "engine": engine.__class__.__name__,
            **stats,
            "recentExecutions": [e.to_dict() for e in executions[-10:]],
            "sloBreaches": sum(
                1 for e in executions if e.duration_ms > next(
                    (w.slo_ms for w in WORKFLOWS if w.workflow_id == e.workflow_id), 3000
                )
            ),
        }

    # --- PAS diagnostics -------------------------------------------------
    @router.get("/pas/health", summary="PAS connectivity", operation_id="pas.tenantHealth")
    async def pas_health(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """Probe the tenant's connected PAS and report what this gateway can do with it."""
        vendor = ctx.effective_pas_vendor
        adapter = get_adapter(vendor)
        health = await adapter.health()
        return {
            "tenantId": ctx.tenant_id,
            "vendor": describe_vendor(vendor),
            "health": health,
            "availableOperations": [
                o.operation_id for o in catalogue_module.CATALOGUE.list()
                if adapter.supports(o.operation_id)
            ],
            "unavailableOperations": [
                o.operation_id for o in catalogue_module.CATALOGUE.list()
                if not adapter.supports(o.operation_id)
            ],
        }

    @router.post(
        "/pas/translation/preview",
        summary="Preview a vendor translation",
        operation_id="pas.translationPreview",
    )
    async def translation_preview(
        request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Show how a payload would be rewritten for the carrier's vendor dialect."""
        body: dict[str, Any] = await _json_body(request)
        operation_id = str(body.get("operationId") or "")
        if not operation_id:
            raise PasError(ErrorCode.VALIDATION_FAILED, "operationId is required")
        vendor = str(body.get("vendor") or ctx.effective_pas_vendor)
        direction = str(body.get("direction") or "to_vendor")
        engine = get_engine()
        payload = dict(body.get("payload") or {})
        result = (
            engine.from_vendor(vendor, operation_id, payload)
            if direction == "from_vendor"
            else engine.to_vendor(vendor, operation_id, payload)
        )
        profile = engine.profile(vendor)
        return {
            "vendor": vendor,
            "operationId": operation_id,
            "direction": direction,
            "profileVersion": profile.version if profile else None,
            "translated": result.payload,
            "trace": result.as_trace(),
        }

    # --- MCP helpers -----------------------------------------------------
    @router.get("/mcp/tools", summary="MCP tool catalogue", operation_id="mcp.listTools")
    async def mcp_tools(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """The exact tool list an MCP client would discover, as JSON."""
        registry = platform.mcp_registry
        if registry is None:
            return {"enabled": False, "tools": []}
        return {
            "enabled": True,
            "server": registry.info.to_dict(),
            "transport": platform.settings.mcp_transport,  # type: ignore[attr-defined]
            "path": "/mcp",
            "tools": registry.list_tools(),
            "resources": registry.list_resources(),
            "prompts": registry.list_prompts(),
            "stats": registry.stats(),
        }

    @router.get("/plugin", summary="Plugin metadata", operation_id="gateway.pluginInfo")
    async def plugin_info(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """Identity, problem statement and commercial model for this plugin."""
        return {
            **PLUGIN_INFO.to_dict(),
            "catalogue": catalogue_module.catalogue_summary(),
            "workflowCount": len(WORKFLOWS),
            "pasVendor": ctx.effective_pas_vendor,
            "metrics": GLOBAL_METRICS.snapshot(),
        }

    return router


def _dependency(platform: Platform) -> Any:  # noqa: ANN401
    from pas_core.app import context_dependency  # noqa: PLC0415

    return context_dependency(platform)


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}
