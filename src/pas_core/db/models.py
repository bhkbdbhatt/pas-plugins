"""Tenant-isolated async persistence.

Every tenant-owned table carries ``tenant_id`` as the leading column of its primary
key, and every repository method takes the tenant id explicitly.  That combination
makes cross-tenant reads a *query error* rather than a forgotten ``WHERE``
clause, which is the difference between a bug and a reportable incident.

The suite runs on PostgreSQL in production and on SQLite+aiosqlite for tests and
demos, from the same SQLAlchemy models.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from pas_core.errors import ErrorCode, NotFoundError, PasError


class Base(DeclarativeBase):
    """Declarative base for every ORM model in the suite."""


def _pk(prefix: str) -> Mapped[str]:
    return mapped_column(String(64), primary_key=True, default=lambda: f"{prefix}_{uuid.uuid4().hex[:20]}")


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )


# ---------------------------------------------------------------------------
# Control plane
# ---------------------------------------------------------------------------
class TenantRecord(Base, TimestampMixin):
    """Persisted carrier tenant."""

    __tablename__ = "tenants"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    legal_name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="active")
    plan: Mapped[str] = mapped_column(String(32), default="starter")
    pas_vendor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data_region: Mapped[str] = mapped_column(String(32), default="us-east-1")
    enabled_plugins: Mapped[list[str]] = mapped_column(JSON, default=list)
    limits: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


# ---------------------------------------------------------------------------
# Plugin 1 - gateway
# ---------------------------------------------------------------------------
class OperationInvocation(Base, TimestampMixin):
    """One atomic operation execution against a PAS."""

    __tablename__ = "operation_invocations"
    __table_args__ = (
        Index("ix_operation_invocations_tenant_time", "tenant_id", "created_at"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_invocation_idempotency"),
    )

    invocation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    vendor: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    actor: Mapped[str] = mapped_column(String(128), default="unknown")
    status: Mapped[str] = mapped_column(String(32), default="success")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    translation_trace: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class WorkflowExecutionRecord(Base, TimestampMixin):
    """Durable workflow execution state."""

    __tablename__ = "workflow_executions"
    __table_args__ = (Index("ix_workflow_executions_tenant", "tenant_id", "workflow_id"),)

    execution_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workflow_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="running")
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    outputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)


# ---------------------------------------------------------------------------
# Plugin 2 - IFRS 17
# ---------------------------------------------------------------------------
class ContractGroupRecord(Base, TimestampMixin):
    """A group of insurance contracts (IFRS 17 GIC)."""

    __tablename__ = "contract_groups"
    __table_args__ = (
        UniqueConstraint("tenant_id", "group_id", name="uq_contract_group"),
        Index("ix_contract_groups_tenant_period", "tenant_id", "period"),
    )

    group_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    period: Mapped[str] = mapped_column(String(16), nullable=False)
    measurement_model: Mapped[str] = mapped_column(String(32), default="gmm")
    product_line: Mapped[str] = mapped_column(String(64))
    cohort_year: Mapped[int] = mapped_column(Integer)
    profitability: Mapped[str] = mapped_column(String(32), default="unknown")
    locked_in_rate: Mapped[float] = mapped_column(Float, default=0.0)
    contract_count: Mapped[int] = mapped_column(Integer, default=0)
    assumptions_version: Mapped[str] = mapped_column(String(64), default="v1")
    grouping_rationale: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class MeasurementRecord(Base, TimestampMixin):
    """A measurement result for a contract group at a reporting date."""

    __tablename__ = "measurements"
    __table_args__ = (
        UniqueConstraint("tenant_id", "group_id", "reporting_date", name="uq_measurement"),
        Index("ix_measurements_tenant_date", "tenant_id", "reporting_date"),
    )

    measurement_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    group_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reporting_date: Mapped[str] = mapped_column(String(16), nullable=False)
    measurement_model: Mapped[str] = mapped_column(String(32))
    fulfilment_cash_flows: Mapped[float] = mapped_column(Float, default=0.0)
    risk_adjustment: Mapped[float] = mapped_column(Float, default=0.0)
    contractual_service_margin: Mapped[float] = mapped_column(Float, default=0.0)
    loss_component: Mapped[float] = mapped_column(Float, default=0.0)
    total_liability: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    inputs_digest: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AssumptionVersion(Base, TimestampMixin):
    """Versioned actuarial assumptions."""

    __tablename__ = "assumption_versions"
    __table_args__ = (UniqueConstraint("tenant_id", "version", name="uq_assumption_version"),)

    assumption_version_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(128))
    effective_from: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), default="draft")
    assumptions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(128), default="system")


# ---------------------------------------------------------------------------
# Plugin 3 - underwriting
# ---------------------------------------------------------------------------
class SubmissionRecord(Base, TimestampMixin):
    """An underwriting submission."""

    __tablename__ = "submissions"
    __table_args__ = (
        Index("ix_submissions_tenant_status", "tenant_id", "status"),
        UniqueConstraint("tenant_id", "submission_id", name="uq_submission"),
    )

    submission_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    product_code: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="submitted")
    complexity_score: Mapped[float] = mapped_column(Float, default=0.0)
    complexity_tier: Mapped[str] = mapped_column(String(32), default="fast_track")
    risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    extracted_fields: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enrichment: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    facts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    channel: Mapped[str] = mapped_column(String(64), default="api")


class DecisionRecord(Base, TimestampMixin):
    """An underwriting decision with its full explanation."""

    __tablename__ = "decisions"
    __table_args__ = (Index("ix_decisions_tenant_submission", "tenant_id", "submission_id"),)

    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    submission_id: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(32))
    decision_source: Mapped[str] = mapped_column(String(32), default="auto")
    risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    rule_outcomes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    shap_values: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    overridden_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)


class ModelRegistryRecord(Base, TimestampMixin):
    """Trained model versions and their monitoring state."""

    __tablename__ = "model_registry"
    __table_args__ = (UniqueConstraint("tenant_id", "model_name", "version", name="uq_model_version"),)

    model_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm: Mapped[str] = mapped_column(String(64))
    stage: Mapped[str] = mapped_column(String(32), default="production")
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    feature_names: Mapped[list[str]] = mapped_column(JSON, default=list)
    trained_at: Mapped[str] = mapped_column(String(32))
    artifact_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    drift_psi: Mapped[float] = mapped_column(Float, default=0.0)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)


# ---------------------------------------------------------------------------
# Plugin 4 - product configuration
# ---------------------------------------------------------------------------
class ProductRecord(Base, TimestampMixin):
    """A product definition with versioned configuration."""

    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("tenant_id", "product_code", "version", name="uq_product_version"),
        Index("ix_products_tenant_status", "tenant_id", "status"),
    )

    product_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    product_code: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="draft")
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    acord_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    compliance_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(128), default="system")
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    deployed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class DeploymentRecord(Base, TimestampMixin):
    """Product configuration deployment to a PAS."""

    __tablename__ = "deployments"
    __table_args__ = (Index("ix_deployments_tenant_product", "tenant_id", "product_code"),)

    deployment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    product_code: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer)
    target_env: Mapped[str] = mapped_column(String(32), default="staging")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    requested_by: Mapped[str] = mapped_column(String(128))
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


# ---------------------------------------------------------------------------
# Plugin 5 - embedded distribution
# ---------------------------------------------------------------------------
class PartnerRecord(Base, TimestampMixin):
    """A distribution partner with white-label configuration."""

    __tablename__ = "partners"
    __table_args__ = (UniqueConstraint("tenant_id", "partner_id", name="uq_partner"),)

    partner_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="active")
    brand_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    allowed_products: Mapped[list[str]] = mapped_column(JSON, default=list)
    revenue_share_bps: Mapped[int] = mapped_column(Integer, default=0)
    rate_limit_rps: Mapped[int] = mapped_column(Integer, default=20)
    settlement_cycle: Mapped[str] = mapped_column(String(32), default="monthly")


class TransactionRecord(Base, TimestampMixin):
    """A quote/bind transaction for revenue share and reporting."""

    __tablename__ = "transactions"
    __table_args__ = (Index("ix_transactions_tenant_partner", "tenant_id", "partner_id"),)

    transaction_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    partner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    transaction_type: Mapped[str] = mapped_column(String(32))
    product_code: Mapped[str] = mapped_column(String(64))
    policy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    premium_amount: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    revenue_share_amount: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(32), default="completed")
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    settlement_period: Mapped[str | None] = mapped_column(String(16), nullable=True)
    settled_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class SettlementRecord(Base, TimestampMixin):
    """A settlement batch paid to a partner."""

    __tablename__ = "settlements"

    settlement_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    partner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    period: Mapped[str] = mapped_column(String(16))
    transaction_count: Mapped[int] = mapped_column(Integer, default=0)
    gross_premium: Mapped[float] = mapped_column(Float, default=0.0)
    revenue_share: Mapped[float] = mapped_column(Float, default=0.0)
    adjustments: Mapped[float] = mapped_column(Float, default=0.0)
    net_payable: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    status: Mapped[str] = mapped_column(String(32), default="pending")


# ---------------------------------------------------------------------------
# Plugin 6 - data mesh
# ---------------------------------------------------------------------------
class EntityRecord(Base, TimestampMixin):
    """A master data entity (customer, policy, product) in the unified layer."""

    __tablename__ = "mdm_entities"
    __table_args__ = (
        UniqueConstraint("tenant_id", "entity_type", "surrogate_key", name="uq_mdm_entity"),
        Index("ix_mdm_entities_tenant_type", "tenant_id", "entity_type"),
    )

    surrogate_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32))
    natural_key: Mapped[str] = mapped_column(String(128))
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    quality_score: Mapped[float] = mapped_column(Float, default=1.0)
    lineage: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    is_golden: Mapped[bool] = mapped_column(Boolean, default=True)


class DataQualityResult(Base, TimestampMixin):
    """Outcome of a data quality rule evaluation."""

    __tablename__ = "data_quality_results"
    __table_args__ = (Index("ix_dq_tenant_dataset", "tenant_id", "dataset"),)

    result_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset: Mapped[str] = mapped_column(String(128))
    rule_id: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(32), default="warning")
    passed: Mapped[bool] = mapped_column(Boolean, default=True)
    records_checked: Mapped[int] = mapped_column(Integer, default=0)
    records_failed: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    sample_failures: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)


class FeatureDefinitionRecord(Base, TimestampMixin):
    """A Feast-style feature definition with online/offline materialisation."""

    __tablename__ = "feature_definitions"
    __table_args__ = (UniqueConstraint("tenant_id", "feature_name", "version", name="uq_feature"),)

    feature_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    entity: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    value_type: Mapped[str] = mapped_column(String(32), default="float")
    owner: Mapped[str] = mapped_column(String(128), default="unknown")
    ttl_seconds: Mapped[int] = mapped_column(Integer, default=900)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    source: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    transform: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Plugin 7 - blockchain
# ---------------------------------------------------------------------------
class LedgerBlockRecord(Base, TimestampMixin):
    """A block written to the policy lifecycle ledger."""

    __tablename__ = "ledger_blocks"
    __table_args__ = (Index("ix_ledger_tenant_height", "tenant_id", "height"),)

    block_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    height: Mapped[int] = mapped_column(Integer, default=0)
    channel: Mapped[str] = mapped_column(String(64))
    transaction_id: Mapped[str] = mapped_column(String(64))
    transaction_type: Mapped[str] = mapped_column(String(64))
    policy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    endorsements: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    committed_at: Mapped[str] = mapped_column(String(32))


# ---------------------------------------------------------------------------
# Cross-plugin: audit persistence
# ---------------------------------------------------------------------------
class AuditEventRecord(Base):
    """Persisted audit event (mirrors :class:`pas_core.audit.AuditEvent`)."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_tenant_time", "tenant_id", "occurred_at"),
        UniqueConstraint("tenant_id", "sequence", name="uq_audit_sequence"),
    )

    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(128))
    actor_type: Mapped[str] = mapped_column(String(32))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    acord_tx_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    changes: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    previous_hash: Mapped[str] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64), index=True)


# ---------------------------------------------------------------------------
# Engine / session management
# ---------------------------------------------------------------------------
def create_engine(url: str, *, echo: bool = False) -> Any:  # noqa: ANN401
    """Create an async engine, disabling pooling for in-memory SQLite."""
    kwargs: dict[str, Any] = {"echo": echo, "future": True}
    if url.startswith("sqlite"):
        from sqlalchemy.pool import StaticPool  # noqa: PLC0415

        kwargs["poolclass"] = StaticPool
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_async_engine(url, **kwargs)


def create_session_factory(engine: Any) -> async_sessionmaker[AsyncSession]:  # noqa: ANN401
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def create_schema(engine: Any) -> None:  # noqa: ANN401
    """Create all tables. Alembic handles production migrations."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


class PostgresTenantRepository:
    """PostgreSQL-backed tenant registry implementing the same interface as
    :class:`pas_core.tenancy.InMemoryTenantRegistry`."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def register(self, tenant: Any) -> Any:  # noqa: ANN401
        async with self._sessions() as session:
            record = TenantRecord(
                tenant_id=tenant.tenant_id,
                legal_name=tenant.legal_name,
                status=str(tenant.status),
                plan=str(tenant.plan),
                pas_vendor=tenant.pas_vendor,
                data_region=tenant.data_region,
                enabled_plugins=sorted(tenant.enabled_plugins),
                limits={
                    "requests_per_second": tenant.limits.requests_per_second,
                    "requests_per_day": tenant.limits.requests_per_day,
                    "mcp_calls_per_hour": tenant.limits.mcp_calls_per_hour,
                },
                meta=tenant.metadata,
            )
            session.merge(record)
            await session.commit()
        return tenant

    async def get(self, tenant_id: str) -> Any | None:  # noqa: ANN401
        async with self._sessions() as session:
            record = await session.get(TenantRecord, tenant_id)
            return _to_tenant(record) if record else None

    async def require(self, tenant_id: str) -> Any:  # noqa: ANN401
        tenant = await self.get(tenant_id)
        if tenant is None:
            raise PasError(
                ErrorCode.NOT_FOUND,
                f"Unknown tenant '{tenant_id}'",
                {"tenantId": tenant_id},
            )
        return tenant

    async def list(self) -> list[Any]:  # noqa: ANN401
        from sqlalchemy import select  # noqa: PLC0415

        async with self._sessions() as session:
            rows = (await session.execute(select(TenantRecord).order_by(TenantRecord.tenant_id))).scalars()
            return [_to_tenant(r) for r in rows]


def _to_tenant(record: TenantRecord) -> Any:  # noqa: ANN401
    from pas_core.tenancy import Tenant, TenantLimits, TenantPlan, TenantStatus  # noqa: PLC0415

    limits = record.limits or {}
    return Tenant(
        tenant_id=record.tenant_id,
        legal_name=record.legal_name,
        status=TenantStatus(record.status),
        plan=TenantPlan(record.plan),
        pas_vendor=record.pas_vendor,
        data_region=record.data_region,
        enabled_plugins=frozenset(record.enabled_plugins or ()),
        limits=TenantLimits(
            requests_per_second=int(limits.get("requests_per_second", 50)),
            requests_per_day=int(limits.get("requests_per_day", 1_000_000)),
            mcp_calls_per_hour=int(limits.get("mcp_calls_per_hour", 20_000)),
        ),
        metadata=record.meta or {},
    )


def require_found(record: Any, entity: str, identifier: str) -> Any:  # noqa: ANN401
    """Helper that turns a ``None`` lookup into a catalogue NotFound error."""
    if record is None:
        raise NotFoundError(entity, identifier)
    return record
