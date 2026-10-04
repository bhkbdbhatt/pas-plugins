"""Plugin 6 HTTP API.

Paths are namespaced under ``/data`` so this plugin can be mounted alongside the
others without collisions, and every handler declares a purpose so the PII gate is
unavoidable rather than advisory.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from pas_core.app import Platform
from pas_core.errors import ErrorCode, PasError
from pas_core.observability import GLOBAL_METRICS
from pas_core.tenancy import TenantContext
from pas_plugins.plugin6_datamesh.models import EntityType
from pas_plugins.plugin6_datamesh.service import DataMeshService, survivorship_summary

router = APIRouter(prefix="/data", tags=["data-mesh"])


def build_router(platform: Platform, service: DataMeshService) -> APIRouter:
    """Attach the platform-bound routes."""
    from pas_core.app import context_dependency  # noqa: PLC0415

    dependency = context_dependency(platform)

    @router.get("/catalog", summary="Unified data catalogue", operation_id="data.catalog")
    async def catalogue(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """Entities, sources, features, quality, lineage and the PII policy."""
        ctx.principal.require_scopes("data:read")
        service.seed_features(ctx)
        return service.catalog(ctx)

    # --- ingestion --------------------------------------------------------
    @router.get("/sources", summary="List ingestion sources", operation_id="data.listSources")
    async def list_sources(
        request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """What is ingested, from where, how often, and on which entity type."""
        ctx.principal.require_scopes("data:read")
        return service.ingestion.stats(ctx)

    @router.post("/sources/{source_id}/ingest", summary="Ingest a batch source", operation_id="data.ingest")
    async def ingest_source(
        source_id: str,
        request: Request,
        ctx: TenantContext = Depends(dependency),
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Pull a batch source into the unified layer (bronze -> silver)."""
        ctx.principal.require_scopes("data:write")
        return (await service.ingest(source_id, ctx, limit=limit)).to_dict()

    @router.post("/changes", summary="Apply a change-data-capture event", operation_id="data.applyChange")
    async def apply_change(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """Apply one CDC or webhook change to the unified layer.

        Idempotent on ``id``: re-delivering the same change does not duplicate the
        record, which is what makes at-least-once delivery safe.
        """
        ctx.principal.require_scopes("data:write")
        body = await _json_body(request)
        source_id = str(body.get("sourceId") or "")
        if not source_id:
            raise PasError(ErrorCode.VALIDATION_FAILED, "sourceId is required")
        payload = dict(body.get("payload") or {})
        return service.apply_change(
            source_id, ctx, payload, change_type=str(body.get("changeType", "update"))
        ).to_dict()

    @router.post("/sample-data", summary="Load deterministic sample data", operation_id="data.loadSample")
    async def load_sample(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """Load reproducible sample data through every batch source (evaluation only)."""
        ctx.principal.require_scopes("data:write")
        body = await _json_body(request)
        results = service.load_sample_data(ctx, per_source=int(body.get("perSource", 25)))
        return {"results": [r.to_dict() for r in results]}

    # --- resolution and quality ------------------------------------------
    @router.post("/resolve", summary="Resolve entities into golden records", operation_id="data.resolve")
    async def resolve(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """Run entity resolution and survivorship, then score the result."""
        ctx.principal.require_scopes("data:write")
        body = await _json_body(request)
        return service.resolve(ctx, quality_floor=body.get("qualityFloor"))

    @router.get("/quality", summary="Data quality report", operation_id="data.qualityReport")
    async def quality_report(
        request: Request, dataset: str = "unified-gold", ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Quality score, grade, per-dimension breakdown and failing rules.

        This is the endpoint an AI agent should call before answering any
        quantitative question from the unified layer.
        """
        ctx.principal.require_scopes("data:read")
        return service.quality_report(ctx, dataset=dataset).to_dict()

    @router.get("/quality/rules", summary="Quality rule catalogue", operation_id="data.qualityRules")
    async def quality_rules(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """Every registered quality rule, by entity type and dimension."""
        ctx.principal.require_scopes("data:read")
        return {
            "qualityFloor": service._quality_floor,  # noqa: SLF001 - published configuration
            "entityTypes": {
                str(entity_type): [r.to_dict() for r in service.quality.rules(entity_type)]
                for entity_type in EntityType
            },
        }

    @router.get("/mdm/stats", summary="Master data statistics", operation_id="data.mdmStats")
    async def mdm_stats(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """Golden record counts, survivorship ratio and the active policy."""
        ctx.principal.require_scopes("data:read")
        return service.golden.stats(ctx)

    @router.get("/mdm/survivorship", summary="Survivorship decisions", operation_id="data.survivorship")
    async def survivorship(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """Which fields disagreed across sources and which source won."""
        ctx.principal.require_scopes("data:read")
        return survivorship_summary(service.golden.build(service.ingestion.silver.all(ctx.tenant_id), ctx))

    # --- reads ------------------------------------------------------------
    @router.get("/search", summary="Search golden records", operation_id="data.search")
    async def search(
        request: Request,
        purpose: str,
        entity_type: str | None = None,
        query: str | None = None,
        limit: int = 25,
        offset: int = 0,
        unmask: bool = False,
        ctx: TenantContext = Depends(dependency),
    ) -> dict[str, Any]:
        """Search reconciled records. Returns summaries; sensitive values stay masked
        unless the caller holds the unmasking scope."""
        ctx.principal.require_scopes("data:read")
        return service.search(
            ctx,
            entity_type=EntityType(entity_type) if entity_type else None,
            query=query,
            limit=limit,
            offset=offset,
            purpose=purpose,
            unmask=unmask,
        )

    @router.get("/entities/{surrogate_key}", summary="Read a golden record", operation_id="data.getEntity")
    async def get_entity(
        surrogate_key: str,
        request: Request,
        purpose: str,
        unmask: bool = False,
        ctx: TenantContext = Depends(dependency),
    ) -> dict[str, Any]:
        """Read one record with its lineage and the list of fields that were masked."""
        ctx.principal.require_scopes("data:read")
        entity, masked = service.read_entity(ctx, surrogate_key, purpose=purpose, unmask=unmask)
        return {
            **entity.model_dump(mode="json"),
            "maskedFields": masked,
            "sources": sorted({str(e.get("sourceSystem")) for e in entity.lineage}),
        }

    @router.get(
        "/customers/{customer_id}/360",
        summary="Customer 360",
        operation_id="data.customer360",
    )
    async def customer_360(
        customer_id: str,
        request: Request,
        purpose: str,
        unmask: bool = False,
        ctx: TenantContext = Depends(dependency),
    ) -> dict[str, Any]:
        """The customer, their policies, their claims and their premium history - one call.

        This is the endpoint the specification describes and the reason an AI agent
        can be useful to a service agent.
        """
        ctx.principal.require_scopes("data:read")
        view = service.customer_360(ctx, customer_id, purpose=purpose, unmask=unmask)
        return {
            **view.to_summary(),
            "customer": view.customer.model_dump(mode="json"),
            "policies": [p.model_dump(mode="json") for p in view.policies],
            "claims": [c.model_dump(mode="json") for c in view.claims],
            "premiums": [x.model_dump(mode="json") for x in view.premiums],
        }

    # --- lineage ----------------------------------------------------------
    @router.get("/lineage", summary="Lineage graph", operation_id="data.lineage")
    async def lineage(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """Source-to-gold lineage as a node/edge graph for the management UI."""
        ctx.principal.require_scopes("data:read")
        return service.lineage.graph(ctx)

    @router.get(
        "/lineage/impact",
        summary="Downstream impact of a dataset",
        operation_id="data.lineageImpact",
    )
    async def lineage_impact(
        request: Request, dataset: str, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Which downstream datasets are affected when this one is wrong."""
        ctx.principal.require_scopes("data:read")
        return service.lineage.impact(ctx, dataset)

    @router.get("/lineage/events", summary="OpenLineage run events", operation_id="data.lineageEvents")
    async def lineage_events(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """Raw OpenLineage-shaped run events, ready for an existing catalogue."""
        ctx.principal.require_scopes("data:read")
        return {"events": service.lineage.events(ctx)}

    # --- features ---------------------------------------------------------
    @router.get("/features", summary="Feature definitions", operation_id="data.listFeatures")
    async def list_features(
        request: Request, tag: str | None = None, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Every declared feature with owner, TTL, source and materialisation mode."""
        ctx.principal.require_scopes("data:read")
        service.seed_features(ctx)
        return {"features": [f.to_dict() for f in service.features.definitions(ctx=ctx, tag=tag)]}

    @router.post(
        "/features/materialize",
        summary="Materialise feature values",
        operation_id="data.materializeFeatures",
    )
    async def materialize(
        request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Write feature values for an entity, recording version, source and expiry."""
        ctx.principal.require_scopes("data:write")
        body = await _json_body(request)
        entity = service.entity_key_for(
            ctx, EntityType(body.get("entityType", "submission")), str(body["entityKey"])
        )
        return service.materialize_features(ctx, entity, dict(body.get("values") or {}))

    @router.get("/features/vector", summary="Fetch a feature vector", operation_id="data.featureVector")
    async def feature_vector(
        request: Request,
        entity_key: str,
        entity_type: str = "submission",
        features: str | None = None,
        ctx: TenantContext = Depends(dependency),
    ) -> dict[str, Any]:
        """Current feature values for online inference, with TTL enforcement."""
        ctx.principal.require_scopes("data:read")
        entity = service.entity_key_for(ctx, EntityType(entity_type), entity_key)
        names = features.split(",") if features else None
        return service.feature_vector(ctx, entity, names)

    @router.post("/features/drift", summary="Measure feature drift", operation_id="data.featureDrift")
    async def feature_drift(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """Population Stability Index for a feature against its training baseline."""
        ctx.principal.require_scopes("data:read")
        body = await _json_body(request)
        report = service.features.compute_drift(
            str(body["featureName"]), [float(v) for v in body.get("currentValues", [])], ctx
        )
        return report.to_dict()

    @router.get(
        "/features/retraining",
        summary="Retraining recommendation",
        operation_id="data.retrainingTrigger",
    )
    async def retraining(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """Which features have drifted enough to warrant retraining a model."""
        ctx.principal.require_scopes("data:read")
        return service.features.retraining_trigger(ctx)

    # --- governance -------------------------------------------------------
    @router.get("/governance/pii", summary="PII policy", operation_id="data.piiPolicy")
    async def pii_policy(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """Masking strategy per data class, allowed purposes and the unmasking scope."""
        ctx.principal.require_scopes("data:read")
        from pas_core.pii import DEFAULT_REGISTRY, STRATEGY_BY_CLASS  # noqa: PLC0415

        return {
            "mode": service.pii_policy.mode,
            "unmaskScope": service.pii_policy.unmask_scope,
            "callerMayUnmask": service.pii_policy.may_unmask(ctx.principal.scopes),
            "requirePurpose": service.pii_policy.require_purpose,
            "allowedPurposes": sorted(service.pii_policy.allowed_purposes),
            "deniedPurposes": sorted(service.pii_policy.denied_purposes),
            "strategies": {k: str(v) for k, v in STRATEGY_BY_CLASS.items()},
            "classifications": DEFAULT_REGISTRY.as_json()["classifications"],
        }

    @router.get("/governance/metrics", summary="Data mesh metrics", operation_id="data.metrics")
    async def metrics(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """Operational metrics for the data mesh: records, quality and drift."""
        ctx.principal.require_scopes("data:read")
        return {
            "ingestion": service.ingestion.stats(ctx),
            "mdm": service.golden.stats(ctx),
            "features": service.features.stats(ctx),
            "quality": [
                {"dataset": r.dataset, "score": round(r.score, 4), "grade": r.grade}
                for r in service.quality.reports(ctx)
            ],
            "retraining": service.features.retraining_trigger(ctx),
            "runtime": GLOBAL_METRICS.snapshot(),
        }

    return router


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}
