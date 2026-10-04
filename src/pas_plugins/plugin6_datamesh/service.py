"""Plugin 6 service layer.

Composes the ingestion, MDM, quality, lineage and feature-store components into
the operations the API and MCP tools expose, including the customer 360 view and
the PII/purpose gate that guards every read.
"""

from __future__ import annotations

import logging
from typing import Any

from pas_core.errors import ErrorCode, NotFoundError, PasError, PermissionDeniedError
from pas_core.observability import GLOBAL_METRICS
from pas_core.pii import PiiPolicy, masked_fields_for, mask_record
from pas_core.tenancy import TenantContext
from pas_plugins.plugin6_datamesh.feature_store import (
    EntityKey,
    FeatureDefinition,
    FeatureStore,
    seed_feature_store,
)
from pas_plugins.plugin6_datamesh.ingestion import (
    IngestionResult,
    IngestionService,
    SourceDescriptor,
    demo_sources,
    ingest_synthetic,
)
from pas_plugins.plugin6_datamesh.mdm import (
    EntityResolver,
    GoldenRecordBuilder,
    SurvivorshipResult,
)
from pas_plugins.plugin6_datamesh.models import (
    AccessPurpose,
    BaseEntity,
    Claim,
    Customer,
    Customer360,
    EntityType,
    Policy,
    Premium,
)
from pas_plugins.plugin6_datamesh.quality import DataQualityEngine, LineageTracker, QualityReport

logger = logging.getLogger("pas_plugins.plugin6.service")


class DataMeshService:
    """The plugin's application service.

    Deliberately one object rather than five globals: the interactions between
    ingestion, resolution, quality gating and the feature store are the whole
    value proposition, so they belong in one place.
    """

    def __init__(
        self,
        *,
        pii_policy: PiiPolicy | None = None,
        match_threshold: float = 0.86,
        quality_floor: float = 0.95,
        default_feature_ttl: int = 900,
        drift_threshold: float = 0.20,
        lineage_enabled: bool = True,
    ) -> None:
        self.ingestion = IngestionService()
        self.resolver = EntityResolver(threshold=match_threshold, pii_policy=pii_policy)
        self.golden = GoldenRecordBuilder(self.resolver)
        self.quality = DataQualityEngine(quality_floor=quality_floor)
        self.quality.register_defaults()
        self.lineage = LineageTracker(enabled=lineage_enabled)
        self.features = FeatureStore(default_ttl=default_feature_ttl, drift_threshold=drift_threshold)
        self.pii_policy = pii_policy or PiiPolicy()
        self._quality_floor = quality_floor

    # -- bootstrap ---------------------------------------------------------
    def register_demo_sources(self) -> list[SourceDescriptor]:
        """Register the sources a carrier typically has, with field maps."""
        registered: list[SourceDescriptor] = []
        for descriptor in demo_sources():
            registered.append(self.ingestion.register_source(descriptor))
        # Field maps are configuration, not code: onboarding a vendor's differently
        # named columns is a settings change.
        self.ingestion.set_field_map("pas-policies", {
            "policyId": "policy_number",
            "productCode": "product_code",
            "stateOfIssue": "state_of_issue",
            "faceAmount": "face_amount",
            "annualisedPremium": "annualised_premium",
            "issueDate": "issue_date",
            "effectiveDate": "effective_date",
            "expirationDate": "expiration_date",
            "customerId": "customer_key",
            "lob": "lob",
            "paymentMode": "payment_mode",
            "status": "status",
        })
        self.ingestion.set_field_map("pas-customers", {
            "id": "natural_key",
        })
        self.ingestion.set_field_map("claims-stream", {
            "id": "claim_number",
            "policyId": "policy_number",
            "claimType": "claim_type",
            "claimAmount": "claim_amount",
            "paidAmount": "paid_amount",
            "reserveAmount": "reserve_amount",
            "reportDate": "report_date",
            "lossDate": "loss_date",
        })
        self.ingestion.set_field_map("billing-premiums", {
            "id": "natural_key",
            "policyId": "policy_number",
            "dueDate": "due_date",
            "paidDate": "paid_date",
            "amountDue": "amount_due",
            "amountPaid": "amount_paid",
            "premiumType": "premium_type",
            "billingFrequency": "billing_frequency",
            "commissionAmount": "commission_amount",
        })
        self.ingestion.set_field_map("crm-enrichment", {
            "id": "natural_key",
        })
        return registered

    def seed_features(self, ctx: TenantContext) -> list[FeatureDefinition]:
        return seed_feature_store(self.features, ctx).definitions(ctx=ctx)

    # -- ingestion ---------------------------------------------------------
    async def ingest(self, source_id: str, ctx: TenantContext, *, limit: int | None = None) -> IngestionResult:
        """Ingest a batch source and record lineage."""
        result = await self.ingestion.ingest_batch(source_id, ctx, limit=limit)
        self.lineage.record_run(
            ctx,
            run_id=result.run_id,
            job_name=f"ingest:{source_id}",
            inputs=[{"namespace": "pas", "name": f"raw.{source_id}"}],
            outputs=[{"namespace": "pas", "name": f"silver.{result.source_id}"}],
        )
        return result

    def load_sample_data(self, ctx: TenantContext, *, per_source: int = 25) -> list[IngestionResult]:
        """Load deterministic sample data through every batch source (demos/tests)."""
        results: list[IngestionResult] = []
        for descriptor in self.ingestion.sources():
            if descriptor.mode.value != "batch":
                continue
            result = ingest_synthetic(self.ingestion, descriptor.source_id, ctx, per_source)
            results.append(result)
            # Lineage is recorded per source rather than as a wildcard so impact
            # analysis can answer "what breaks if this source is wrong?".
            self.lineage.record_run(
                ctx,
                run_id=result.run_id,
                job_name=f"ingest:{descriptor.source_id}",
                inputs=[{"namespace": "pas", "name": f"raw.{descriptor.source_id}"}],
                outputs=[{"namespace": "pas", "name": f"silver.{descriptor.source_id}"}],
            )
        return results

    def apply_change(
        self,
        source_id: str,
        ctx: TenantContext,
        payload: dict[str, Any],
        *,
        change_type: str = "update",
    ) -> IngestionResult:
        from pas_plugins.plugin6_datamesh.models import ChangeEventType  # noqa: PLC0415

        return self.ingestion.ingest_change(
            source_id, ctx, payload, change_type=ChangeEventType(change_type)
        )

    # -- resolution and quality --------------------------------------------
    def resolve(self, ctx: TenantContext, *, quality_floor: float | None = None) -> dict[str, Any]:
        """Resolve silver entities into golden records and score their quality."""
        floor = quality_floor if quality_floor is not None else self._quality_floor
        silver = [
            entity for entity in self.ingestion.silver.all(ctx.tenant_id)
            if entity.entity_type is EntityType.CUSTOMER
        ]
        results = self.golden.build(silver, ctx, quality_floor=floor)
        report = self.quality.evaluate(self.golden.all(ctx), ctx, dataset="unified-gold")
        self.lineage.record_run(
            ctx,
            run_id="resolve",
            job_name="mdm:resolve-and-survive",
            inputs=[{"namespace": "pas", "name": "silver.*"}],
            outputs=[{"namespace": "pas", "name": "gold.*"}],
        )
        GLOBAL_METRICS.increment("datamesh_resolutions_total", tenant=ctx.tenant_id, count=len(results))
        return {
            "tenantId": ctx.tenant_id,
            "clustersCreated": len(results),
            "mergedRecords": sum(1 for r in results if len(r.entity.attributes.get("_merged_members", [])) > 1),
            "goldenRecords": len(self.golden.all(ctx)),
            "quality": report.to_dict(),
            "promotable": report.score >= floor and not report.blocking,
            "qualityFloor": floor,
        }

    def quality_report(self, ctx: TenantContext, dataset: str = "unified-gold") -> QualityReport:
        records = self.golden.all(ctx)
        return self.quality.evaluate(records, ctx, dataset=dataset)

    # -- reads -------------------------------------------------------------
    def authorize_read(
        self,
        ctx: TenantContext,
        *,
        purpose: str | None,
        entity_type: EntityType,
        entity_key: str,
    ) -> dict[str, Any]:
        """Purpose limitation plus masking decision for one read.

        Returns the decision so the caller can include it in the response - a
        client that silently received masked data would have no way to know.
        """
        try:
            AccessPurpose(purpose) if purpose else None
        except ValueError as exc:
            raise PasError(
                ErrorCode.VALIDATION_FAILED,
                f"'{purpose}' is not a recognised access purpose",
                {
                    "purpose": purpose,
                    "allowedPurposes": [str(p) for p in AccessPurpose],
                },
            ) from exc
        decision = self.pii_policy.check_purpose(purpose)
        if not decision.granted:
            raise PermissionDeniedError(
                f"access refused: {decision.reason}",
                purpose=purpose,
                entityType=str(entity_type),
                entityKey=entity_key,
            )
        return {"granted": True, "purpose": purpose}

    def read_entity(
        self,
        ctx: TenantContext,
        surrogate_key: str,
        *,
        purpose: str | None = None,
        unmask: bool = False,
    ) -> tuple[BaseEntity, list[str]]:
        """Read one golden record, applying purpose limitation and masking."""
        entity = self.golden.get(ctx, surrogate_key)
        if entity is None:
            entity = self.golden.find_by_natural_key(ctx, surrogate_key)
        if entity is None:
            raise NotFoundError("golden record", surrogate_key)
        self.authorize_read(
            ctx, purpose=purpose, entity_type=entity.entity_type, entity_key=surrogate_key
        )
        may_unmask = unmask and self.pii_policy.may_unmask(ctx.principal.scopes)
        # When the caller is cleared to see the real values, nothing was masked -
        # reporting masked fields would be misleading in the audit trail.
        masked_fields = [] if may_unmask else masked_fields_for(entity)
        result = entity if may_unmask else mask_record(entity)
        self.pii_policy.log_access(
            tenant_id=ctx.tenant_id,
            actor=ctx.principal.subject,
            entity_type=str(entity.entity_type),
            entity_key=surrogate_key,
            fields=sorted(result.model_fields),
            purpose=purpose,
            masked_count=0 if may_unmask else len(masked_fields),
        )
        GLOBAL_METRICS.increment("datamesh_entity_reads_total", entityType=str(entity.entity_type))
        return result, masked_fields

    def customer_360(
        self,
        ctx: TenantContext,
        customer: Customer | str,
        *,
        purpose: str | None = None,
        unmask: bool = False,
    ) -> Customer360:
        """The single-call view: customer, policies, claims and premiums.

        This is the shape the specification calls for and the reason an AI agent
        can be useful to a service agent: one call, one coherent picture, with the
        masking decision stated rather than implied.
        """
        record = customer
        if isinstance(customer, str):
            found = self.golden.get(ctx, customer) or self.golden.find_by_natural_key(ctx, customer)
            if not isinstance(found, Customer):
                raise NotFoundError("customer", customer)
            record = found
        self.authorize_read(
            ctx, purpose=purpose, entity_type=EntityType.CUSTOMER, entity_key=record.surrogate_key
        )

        policies = [p for p in self.golden.all(ctx) if isinstance(p, Policy)]
        claims = [c for c in self.golden.all(ctx) if isinstance(c, Claim)]
        premiums = [x for x in self.golden.all(ctx) if isinstance(x, Premium)]

        related_policies = [p for p in policies if _belongs_to(p, record)]
        policy_numbers = {p.policy_number for p in related_policies}
        related_claims = [c for c in claims if c.policy_number in policy_numbers]
        related_premiums = [x for x in premiums if x.policy_number in policy_numbers]

        may_unmask = unmask and self.pii_policy.may_unmask(ctx.principal.scopes)
        projected_customer = record if may_unmask else mask_record(record)
        masked_fields = [] if may_unmask else masked_fields_for(record)
        present_domains = sum(
            [
                bool(record.email or record.phone),
                bool(related_policies),
                bool(related_claims),
                bool(related_premiums),
            ]
        )
        return Customer360(
            customer=projected_customer,
            policies=related_policies,
            claims=related_claims,
            premiums=related_premiums,
            source_systems=sorted({record.source_system}, key=str),
            completeness=present_domains / 4,
            masked_fields=masked_fields,
        )

    def search(
        self,
        ctx: TenantContext,
        *,
        entity_type: EntityType | None = None,
        query: str | None = None,
        limit: int = 25,
        offset: int = 0,
        purpose: str | None = None,
        unmask: bool = False,
    ) -> dict[str, Any]:
        """Search golden records, returning summaries rather than full records."""
        self.authorize_read(
            ctx, purpose=purpose, entity_type=entity_type or EntityType.CUSTOMER, entity_key="*"
        )
        rows = self.golden.search(ctx, entity_type=entity_type, query=query, limit=limit, offset=offset)
        may_unmask = unmask and self.pii_policy.may_unmask(ctx.principal.scopes)
        items: list[dict[str, Any]] = []
        for row in rows:
            summary = row.to_summary()
            summary["qualityScore"] = row.quality_score
            if not may_unmask:
                summary["name"] = _mask_name(summary.get("name", ""))
            items.append(summary)
        self.pii_policy.log_access(
            tenant_id=ctx.tenant_id,
            actor=ctx.principal.subject,
            entity_type=str(entity_type or "all"),
            entity_key="search",
            fields=["*"],
            purpose=purpose,
            masked_count=0 if may_unmask else len(items),
        )
        return {
            "tenantId": ctx.tenant_id,
            "count": len(items),
            "limit": limit,
            "offset": offset,
            "masked": not may_unmask,
            "items": items,
        }

    # -- features ----------------------------------------------------------
    def materialize_features(
        self, ctx: TenantContext, entity: EntityKey, values: dict[str, Any]
    ) -> dict[str, Any]:
        vector = self.features.materialize(entity, values, ctx)
        return vector.to_dict()

    def feature_vector(
        self, ctx: TenantContext, entity: EntityKey, names: list[str] | None = None
    ) -> dict[str, Any]:
        return self.features.vector(entity, ctx, names=names).to_dict()

    def register_feature(
        self, ctx: TenantContext, definition: FeatureDefinition
    ) -> FeatureDefinition:
        return self.features.register(definition, ctx=ctx)

    # -- reporting ---------------------------------------------------------
    def catalog(self, ctx: TenantContext) -> dict[str, Any]:
        """The data mesh catalogue: entities, sources, features and quality."""
        return {
            "tenantId": ctx.tenant_id,
            "entities": [
                {
                    "entityType": str(e),
                    "model": model.__name__,
                    "goldenRecords": sum(
                        1 for g in self.golden.all(ctx) if g.entity_type is e
                    ),
                }
                for e, model in (
                    (EntityType.CUSTOMER, Customer),
                    (EntityType.POLICY, Policy),
                    (EntityType.CLAIM, Claim),
                    (EntityType.PREMIUM, Premium),
                )
            ],
            "sources": self.ingestion.stats(ctx)["sources"],
            "features": [f.to_dict() for f in self.features.definitions(ctx=ctx)],
            "quality": [r.to_dict() for r in self.quality.reports(ctx)],
            "lineage": self.lineage.graph(ctx),
            "pii": self.pii_policy.audit_enabled,
            "mdm": self.golden.stats(ctx),
            "stats": self.ingestion.stats(ctx),
        }


def _belongs_to(policy: Policy, customer: Customer) -> bool:
    """Whether a policy relates to a customer, via surrogate key or natural key."""
    if policy.customer_key and policy.customer_key == customer.surrogate_key:
        return True
    return policy.customer_key == customer.natural_key


def _mask_name(name: str) -> str:
    parts = name.split(" ")
    if len(parts) < 2:
        return "***"
    return f"{parts[0][:1]}. {'*' * (len(parts[1]) - 1)}{parts[1][-1:]}"


_SERVICE: DataMeshService | None = None


def get_service() -> DataMeshService:
    """Process-wide service instance."""
    global _SERVICE  # noqa: PLW0603
    if _SERVICE is None:
        from pas_plugins.plugin6_datamesh.settings import Plugin6Settings  # noqa: PLC0415

        settings = Plugin6Settings()
        from pas_core.pii import build_policy  # noqa: PLC0415

        _SERVICE = DataMeshService(
            pii_policy=build_policy(settings),
            match_threshold=settings.golden_record_match_threshold,
            quality_floor=settings.quality_gate_threshold,
            default_feature_ttl=settings.feature_ttl_seconds,
            drift_threshold=0.20,
            lineage_enabled=settings.lineage_enabled,
        )
        _SERVICE.register_demo_sources()
    return _SERVICE


def reset_service() -> None:
    """Clear the service singleton (tests only)."""
    global _SERVICE  # noqa: PLW0603
    _SERVICE = None


def build_default_service(
    *, pii_policy: PiiPolicy | None = None, match_threshold: float = 0.86
) -> DataMeshService:
    """A fresh service with the demo sources registered."""
    service = DataMeshService(pii_policy=pii_policy, match_threshold=match_threshold)
    service.register_demo_sources()
    return service


def entity_key_for(ctx: TenantContext, entity_type: EntityType, key: str) -> EntityKey:
    """Standard feature-store entity key for an entity type."""
    return EntityKey(entity=str(entity_type), key=key)


def require_quality(service: DataMeshService, ctx: TenantContext) -> QualityReport:
    """Evaluate and gate; used by the publish endpoint."""
    report = service.quality_report(ctx)
    return service.quality.assert_publishable(report)


def survivorship_summary(results: list[SurvivorshipResult]) -> dict[str, Any]:
    """Condensed view of what survivorship decided, for the UI."""
    conflicts = sum(len(r.conflicts) for r in results)
    return {
        "entities": len(results),
        "conflictedFields": conflicts,
        "recordsWithConflicts": sum(1 for r in results if r.conflicts),
        "samples": [
            {"surrogateKey": r.entity.surrogate_key, **r.to_dict()}
            for r in results[:10] if r.conflicts
        ],
    }
