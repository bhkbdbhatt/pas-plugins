"""Ingestion: batch and streaming sources into the unified record model.

Three ingestion shapes are supported, all landing in the same bronze layer:

* **Batch** - a pull over plugin 1's atomic operations, paginated. The simplest
  option and the one most carriers can implement without touching their core.
* **CDC** - a change stream (Kafka/Debezium, or a vendor webhook) delivering
  inserts, updates and deletes as they happen.
* **Webhook** - HTTP push from a source system, with HMAC verification.

The bronze/silver/gold medallion layering follows the Lakehouse convention:

======  ==========================================================================
Bronze  Raw source records, unmodified, with full provenance. Never deleted.
Silver  Conformed to the canonical entity model and validated.
Gold    Deduplicated, survivorship-resolved golden records with a quality score.
======  ==========================================================================

Only gold records are served by the unified data API. That is deliberate: serving
bronze to an AI agent is how a carrier ends up with three "different" Jane Publics
and no idea why.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from pas_core.errors import ErrorCode, PasError, ValidationError
from pas_core.observability import GLOBAL_METRICS, Timer, correlation_scope
from pas_core.tenancy import TenantContext, maybe_current_context
from pas_plugins.plugin6_datamesh.models import (
    BaseEntity,
    ChangeEventType,
    EntityType,
    RecordQuality,
    SourceSystem,
    model_for,
)

SOURCE_KEY_FIELDS: dict[EntityType, tuple[str, ...]] = {
    EntityType.CUSTOMER: ("policyholder_ssn", "ssn", "date_of_birth", "last_name"),
    EntityType.POLICY: ("policy_number",),
    EntityType.CLAIM: ("claim_number",),
    EntityType.PREMIUM: ("policy_number", "due_date", "premium_type"),
}


class IngestionMode(StrEnum):
    BATCH = "batch"
    CDC = "cdc"
    WEBHOOK = "webhook"


@dataclass(frozen=True, slots=True)
class SourceDescriptor:
    """A configured ingestion source.

    ``connector`` names how to reach it: ``pas.atomic`` pulls through plugin 1,
    ``kafka`` subscribes to a topic, ``webhook`` waits for pushes, ``csv`` reads a
    file.  Anything not built in can be plugged in via ``fetch``.
    """

    source_id: str
    name: str
    entity_type: EntityType
    mode: IngestionMode
    system: SourceSystem
    primary_keys: tuple[str, ...]
    operation_id: str | None = None
    """Plugin 1 atomic operation to pull through for a batch source."""
    topic: str | None = None
    """Kafka/Debezium topic for a CDC source."""
    path: str | None = None
    """Filesystem path for a file source."""
    schedule: str | None = None
    """Cron expression for a scheduled batch source."""
    enabled: bool = True
    fetch: Callable[..., Any] | None = None
    """Optional custom reader. Receives a context dict and returns an iterable of
    raw payloads; use it to onboard a source the built-ins do not cover without
    forking the ingestion service."""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.primary_keys:
            msg = f"source '{self.source_id}' must declare at least one primary key"
            raise ValidationError(msg, sourceId=self.source_id)
        if self.mode is IngestionMode.BATCH and not self.operation_id and self.fetch is None:
            msg = (
                f"batch source '{self.source_id}' needs an operation_id to pull through "
                "plugin 1, or a fetch callable"
            )
            raise ValidationError(msg, sourceId=self.source_id)
        if self.mode is IngestionMode.CDC and not self.topic:
            msg = f"cdc source '{self.source_id}' needs a topic"
            raise ValidationError(msg, sourceId=self.source_id)


@dataclass(slots=True)
class IngestionRecord:
    """One raw record with its provenance, before conformance."""

    tenant_id: str
    source_id: str
    entity_type: EntityType
    system: SourceSystem
    source_record_id: str
    payload: dict[str, Any]
    change_type: ChangeEventType = ChangeEventType.SNAPSHOT
    ingested_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    correlation_id: str = ""
    row_hash: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)

    def compute_row_hash(self) -> str:
        payload = self.payload
        return hashlib.sha256(
            "|".join(f"{k}={payload[k]}" for k in sorted(payload) if k != "_metadata").encode()
        ).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenantId": self.tenant_id,
            "sourceId": self.source_id,
            "entityType": str(self.entity_type),
            "sourceSystem": str(self.system),
            "sourceRecordId": self.source_record_id,
            "changeType": str(self.change_type),
            "ingestedAt": self.ingested_at.isoformat(),
            "correlationId": self.correlation_id,
            "rowHash": self.row_hash,
            "payload": self.payload,
        }


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """Outcome of one ingestion run."""

    run_id: str
    source_id: str
    tenant_id: str
    mode: str
    read: int = 0
    accepted: int = 0
    rejected: int = 0
    deleted: int = 0
    duplicates: int = 0
    duration_ms: float = 0.0
    errors: tuple[dict[str, Any], ...] = ()
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def success_rate(self) -> float:
        return (self.accepted / self.read) if self.read else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "sourceId": self.source_id,
            "tenantId": self.tenant_id,
            "mode": self.mode,
            "read": self.read,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "deleted": self.deleted,
            "duplicates": self.duplicates,
            "successRate": round(self.success_rate, 4),
            "durationMs": round(self.duration_ms, 2),
            "ok": self.ok,
            "errors": list(self.errors[:20]),
            "startedAt": self.started_at.isoformat(),
        }


class BronzeStore:
    """The immutable raw landing layer.

    Append-only by contract: a raw record is never updated or deleted in place, so
    a later disagreement about what the source said can always be resolved.
    """

    def __init__(self) -> None:
        self._records: dict[str, list[IngestionRecord]] = {}

    def append(self, record: IngestionRecord) -> IngestionRecord:
        object.__setattr__(record, "row_hash", record.compute_row_hash())
        self._records.setdefault(record.tenant_id, []).append(record)
        return record

    def for_source(self, tenant_id: str, source_id: str) -> list[IngestionRecord]:
        return [r for r in self._records.get(tenant_id, []) if r.source_id == source_id]

    def latest_for(self, tenant_id: str, entity_type: EntityType) -> dict[str, IngestionRecord]:
        """Most recent record per source-record id - the basis for conformance."""
        latest: dict[str, IngestionRecord] = {}
        for record in self._records.get(tenant_id, []):
            if record.entity_type is not entity_type:
                continue
            existing = latest.get(record.source_record_id)
            if existing is None or record.ingested_at >= existing.ingested_at:
                latest[record.source_record_id] = record
        return latest

    def count(self, tenant_id: str | None = None) -> int:
        if tenant_id is None:
            return sum(len(rows) for rows in self._records.values())
        return len(self._records.get(tenant_id, []))

    def purge_before(self, tenant_id: str, cutoff: datetime) -> int:
        """Retention enforcement. Bronze is append-only but not retained forever."""
        rows = self._records.get(tenant_id, [])
        before = len(rows)
        self._records[tenant_id] = [r for r in rows if r.ingested_at >= cutoff]
        return before - len(self._records[tenant_id])


class SilverStore:
    """Conformed, schema-validated entities awaiting entity resolution."""

    def __init__(self) -> None:
        self._entities: dict[str, dict[str, BaseEntity]] = {}

    def put(self, entity: BaseEntity) -> BaseEntity:
        self._entities.setdefault(entity.tenant_id, {})[entity.natural_key] = entity
        return entity

    def get(self, tenant_id: str, natural_key: str) -> BaseEntity | None:
        return self._entities.get(tenant_id, {}).get(natural_key)

    def all(self, tenant_id: str, entity_type: EntityType | None = None) -> list[BaseEntity]:
        rows = self._entities.get(tenant_id, {}).values()
        return [e for e in rows if entity_type is None or e.entity_type is entity_type]

    def count(self, tenant_id: str) -> int:
        return len(self._entities.get(tenant_id, {}))

    def clear(self, tenant_id: str, source_id: str) -> int:
        """Drop records from a source that has been superseded by a snapshot."""
        rows = self._entities.get(tenant_id, {})
        doomed = [k for k, v in rows.items() if v.attributes.get("_source_id") == source_id]
        for key in doomed:
            rows.pop(key, None)
        return len(doomed)


class SourceConformer:
    """Maps a raw source payload onto the canonical entity model.

    Field mapping is data (``field_map``), which is the difference between
    onboarding a new source system in an afternoon and a multi-week project.  Any
    field not named in the map is preserved under ``attributes`` so nothing is
    silently dropped - losing a field you did not know about is how data-quality
    incidents start.
    """

    def __init__(self, entity_type: EntityType) -> None:
        self.entity_type = entity_type
        self.model = model_for(entity_type)

    def conform(
        self,
        record: IngestionRecord,
        *,
        field_map: Mapping[str, str] | None = None,
        tenant_id: str | None = None,
    ) -> BaseEntity:
        """Validate and construct a canonical entity from a raw record."""
        mapping = dict(field_map or {})
        mapped: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        for raw_key, raw_value in record.payload.items():
            if raw_key.startswith("_"):
                extra[raw_key[1:]] = raw_value
                continue
            target = mapping.get(raw_key, raw_key)
            if target in self.model.model_fields:
                mapped[target] = raw_value
            else:
                extra[raw_key] = raw_value

        mapped.setdefault("surrogate_key", _surrogate_key(record.tenant_id, record.entity_type, record.source_record_id))
        mapped.setdefault("tenant_id", tenant_id or record.tenant_id)
        mapped.setdefault("entity_type", self.entity_type)
        mapped.setdefault("natural_key", str(record.source_record_id))
        mapped.setdefault("source_system", record.system)
        mapped.setdefault("source_record_id", record.source_record_id)
        mapped.setdefault("quality_tier", RecordQuality.SILVER)
        mapped.setdefault(
            "lineage",
            [
                {
                    "dataset": record.source_id,
                    "field": "__all__",
                    "sourceSystem": str(record.system),
                    "sourceRecordId": record.source_record_id,
                    "rowHash": record.row_hash,
                    "ingestedAt": record.ingested_at.isoformat(),
                }
            ],
        )
        mapped["attributes"] = {**mapped.get("attributes", {}), **extra, "_source_id": record.source_id}

        try:
            entity = self.model.model_validate(mapped)
        except Exception as exc:  # noqa: BLE001
            raise PasError(
                ErrorCode.VALIDATION_FAILED,
                f"Source '{record.source_id}' record '{record.source_record_id}' does not "
                f"conform to the canonical {self.entity_type} model",
                {
                    "sourceId": record.source_id,
                    "sourceRecordId": record.source_record_id,
                    "entityType": str(self.entity_type),
                    "error": str(exc)[:500],
                },
            ) from exc
        entity.attributes.setdefault("_source_id", record.source_id)
        return entity


def _surrogate_key(tenant_id: str, entity_type: EntityType, source_record_id: str) -> str:
    """Deterministic surrogate key so re-ingestion is idempotent."""
    digest = hashlib.sha256(f"{tenant_id}:{entity_type}:{source_record_id}".encode()).hexdigest()
    return f"{entity_type[:3].upper()}{digest[:25]}"


class IngestionService:
    """Coordinates reads from sources, conformance, and bronze/silver writes."""

    def __init__(self, *, max_batch: int = 10_000) -> None:
        self.bronze = BronzeStore()
        self.silver = SilverStore()
        self._sources: dict[str, SourceDescriptor] = {}
        self._conformers: dict[EntityType, SourceConformer] = {}
        self._field_maps: dict[str, dict[str, str]] = {}
        self._max_batch = max_batch

    # -- source registration ------------------------------------------------
    def register_source(self, descriptor: SourceDescriptor) -> SourceDescriptor:
        self._sources[descriptor.source_id] = descriptor
        self._conformers.setdefault(
            descriptor.entity_type, SourceConformer(descriptor.entity_type)
        )
        return descriptor

    def set_field_map(self, source_id: str, field_map: dict[str, str]) -> None:
        """Declare how a source's field names map onto the canonical model."""
        self._field_maps[source_id] = dict(field_map)

    def sources(self) -> list[SourceDescriptor]:
        return sorted(self._sources.values(), key=lambda s: s.source_id)

    def source(self, source_id: str) -> SourceDescriptor:
        try:
            return self._sources[source_id]
        except KeyError as exc:
            raise PasError(
                ErrorCode.NOT_FOUND,
                f"Ingestion source '{source_id}' is not registered",
                {"sourceId": source_id, "registered": sorted(self._sources)},
            ) from exc

    # -- ingestion ----------------------------------------------------------
    async def ingest_batch(
        self, source_id: str, ctx: TenantContext, *, limit: int | None = None
    ) -> IngestionResult:
        """Pull every record from a batch source and conform it.

        Uses plugin 1's atomic operation, so the same tenant isolation, rate
        limiting, translation rules and audit trail apply as for a direct API call.
        """
        descriptor = self.source(source_id)
        if descriptor.mode is not IngestionMode.BATCH:
            msg = f"source '{source_id}' is a {descriptor.mode} source, not batch"
            raise PasError(ErrorCode.VALIDATION_FAILED, msg, {"sourceId": source_id})
        started = Timer()
        run_id = uuid.uuid4().hex
        with correlation_scope(ctx.correlation_id, ctx.tenant_id):
            started.__enter__()
            raw_records: list[dict[str, Any]] = []
            if descriptor.fetch is not None:
                raw_records = list(await descriptor.fetch({"sourceId": source_id, "tenantId": ctx.tenant_id}))
            elif descriptor.operation_id:
                raw_records = await self._pull_via_gateway(descriptor, ctx, limit)
            return self._materialise(
                descriptor, ctx, raw_records, run_id, started.elapsed, IngestionMode.BATCH
            )

    async def _pull_via_gateway(
        self, descriptor: SourceDescriptor, ctx: TenantContext, limit: int | None
    ) -> list[dict[str, Any]]:
        from pas_plugins.plugin1_gateway.service import invoke_operation  # noqa: PLC0415

        assert descriptor.operation_id is not None  # noqa: S101 - validated in __post_init__
        page_size = min(limit or self._max_batch, self._max_batch)
        rows: list[dict[str, Any]] = []
        offset = 0
        while len(rows) < page_size:
            response = await invoke_operation(
                descriptor.operation_id,
                {"limit": min(200, page_size - len(rows)), "offset": offset},
                ctx,
            )
            items = response.get("items") or []
            if not items:
                break
            rows.extend(items)
            offset += len(items)
            if len(items) < min(200, page_size - len(rows) + len(items)):
                break
        return rows[:page_size]

    def ingest_change(
        self,
        source_id: str,
        ctx: TenantContext | None,
        payload: dict[str, Any],
        *,
        change_type: ChangeEventType = ChangeEventType.UPDATE,
        source_record_id: str | None = None,
    ) -> IngestionResult:
        """Apply one CDC event. Synchronous by design - it is called per message."""
        descriptor = self.source(source_id)
        context = ctx or maybe_current_context() or _fallback_context()
        record = IngestionRecord(
            tenant_id=context.tenant_id,
            source_id=source_id,
            entity_type=descriptor.entity_type,
            system=descriptor.system,
            source_record_id=source_record_id
            or str(payload.get("id") or payload.get(descriptor.primary_keys[0]) or uuid.uuid4().hex),
            payload=dict(payload),
            change_type=change_type,
            correlation_id=context.correlation_id,
        )
        timer = Timer()
        with correlation_scope(context.correlation_id, context.tenant_id):
            return self._materialise(
                descriptor, context, [record.payload], uuid.uuid4().hex, timer.elapsed,
                IngestionMode.CDC, prebuilt=[record], change_type=change_type,
            )

    def _materialise(
        self,
        descriptor: SourceDescriptor,
        ctx: TenantContext,
        payloads: Iterable[Mapping[str, Any]],
        run_id: str,
        duration_ms: float,
        mode: IngestionMode,
        *,
        prebuilt: list[IngestionRecord] | None = None,
        change_type: ChangeEventType = ChangeEventType.SNAPSHOT,
    ) -> IngestionResult:
        """Bronze the raw payload, conform it into silver, and record the outcome."""
        accepted = rejected = duplicates = deleted = 0
        errors: list[dict[str, Any]] = []
        records = prebuilt if prebuilt is not None else [
            IngestionRecord(
                tenant_id=ctx.tenant_id,
                source_id=descriptor.source_id,
                entity_type=descriptor.entity_type,
                system=descriptor.system,
                source_record_id=str(payload.get("id") or payload.get(descriptor.primary_keys[0]) or uuid.uuid4().hex),
                payload=dict(payload),
                correlation_id=ctx.correlation_id,
            )
            for payload in payloads
        ]

        conformer = self._conformers[descriptor.entity_type]
        field_map = self._field_maps.get(descriptor.source_id, {})

        for record in records:
            self.bronze.append(record)
            if change_type is ChangeEventType.DELETE:
                self.silver.get(record.tenant_id, record.source_record_id) and self.silver._entities.get(  # noqa: SLF001
                    record.tenant_id, {}
                ).pop(record.source_record_id, None)
                deleted += 1
                continue
            try:
                entity = conformer.conform(record, field_map=field_map)
            except PasError as exc:
                rejected += 1
                if len(errors) < 50:
                    errors.append(exc.detail or {"message": exc.message})
                continue
            existing = self.silver.get(record.tenant_id, record.source_record_id)
            if existing is not None and existing.lineage and record.row_hash == _last_row_hash(existing):
                duplicates += 1
                continue
            self.silver.put(entity)
            accepted += 1

        result = IngestionResult(
            run_id=run_id,
            source_id=descriptor.source_id,
            tenant_id=ctx.tenant_id,
            mode=str(mode),
            read=len(records),
            accepted=accepted,
            rejected=rejected,
            deleted=deleted,
            duplicates=duplicates,
            duration_ms=duration_ms,
            errors=tuple(errors),
        )
        GLOBAL_METRICS.increment(
            "datamesh_ingestion_records_total",
            source=descriptor.source_id,
            outcome="accepted" if result.ok else "partial",
        )
        GLOBAL_METRICS.observe(
            "datamesh_ingestion_duration_ms", duration_ms, source=descriptor.source_id
        )
        return result

    def stats(self, ctx: TenantContext) -> dict[str, Any]:
        return {
            "tenantId": ctx.tenant_id,
            "sourceCount": len(self._sources),
            "sources": [
                {
                    "sourceId": s.source_id,
                    "name": s.name,
                    "entityType": str(s.entity_type),
                    "mode": str(s.mode),
                    "system": str(s.system),
                    "enabled": s.enabled,
                    "operationId": s.operation_id,
                    "topic": s.topic,
                    "schedule": s.schedule,
                }
                for s in self._sources.values()
            ],
            "bronzeRecords": self.bronze.count(ctx.tenant_id),
            "silverEntities": self.silver.count(ctx.tenant_id),
        }


def _last_row_hash(entity: BaseEntity) -> str:
    for entry in entity.lineage:
        if entry.get("rowHash"):
            return str(entry["rowHash"])
    return ""


def _fallback_context() -> TenantContext:
    """Context for a CDC event that arrives outside an HTTP request."""
    from pas_core.tenancy import SYSTEM_PRINCIPAL, Tenant, build_context  # noqa: PLC0415

    return build_context(Tenant(tenant_id="system", legal_name="System"), SYSTEM_PRINCIPAL)


def demo_sources() -> list[SourceDescriptor]:
    """The sources a carrier typically has on day one."""
    return [
        SourceDescriptor(
            source_id="pas-policies",
            name="PAS policy master",
            entity_type=EntityType.POLICY,
            mode=IngestionMode.BATCH,
            system=SourceSystem.PAS,
            primary_keys=("policy_number",),
            operation_id="policy.search",
            schedule="0 2 * * *",
            metadata={"notes": "Nightly full extract via plugin 1 policy.search."},
        ),
        SourceDescriptor(
            source_id="pas-customers",
            name="PAS policyholder master",
            entity_type=EntityType.CUSTOMER,
            mode=IngestionMode.BATCH,
            system=SourceSystem.PAS,
            primary_keys=("ssn",),
            operation_id="policy.search",
            schedule="0 2 * * *",
        ),
        SourceDescriptor(
            source_id="claims-stream",
            name="Claims system change stream",
            entity_type=EntityType.CLAIM,
            mode=IngestionMode.CDC,
            system=SourceSystem.CLAIMS,
            primary_keys=("claim_number",),
            topic="pas.claims.changes",
            metadata={"notes": "Kafka + Debezium topic carrying claim status changes."},
        ),
        SourceDescriptor(
            source_id="billing-premiums",
            name="Billing premium ledger",
            entity_type=EntityType.PREMIUM,
            mode=IngestionMode.CDC,
            system=SourceSystem.BILLING,
            primary_keys=("policy_number", "due_date"),
            topic="pas.billing.premiums",
        ),
        SourceDescriptor(
            source_id="crm-enrichment",
            name="CRM enrichment push",
            entity_type=EntityType.CUSTOMER,
            mode=IngestionMode.WEBHOOK,
            system=SourceSystem.CRM,
            primary_keys=("email",),
            metadata={"notes": "Signed webhook pushes preferred language and tags."},
        ),
    ]


def synthetic_batch(entity_type: EntityType, count: int, *, tenant_id: str = "demo-carrier") -> list[dict[str, Any]]:
    """Generate deterministic sample payloads for demos and load tests."""
    states = ["NY", "CA", "TX", "FL", "IL"]
    out: list[dict[str, Any]] = []
    for index in range(count):
        surname = ["Smith", "Jones", "Patel", "Garcia", "Chen", "Okafor", "Novak"][index % 7]
        given = ["Jane", "Alex", "Maria", "Wei", "Omar", "Priya", "Lars"][index % 7]
        base = {
            "id": f"{entity_type.upper()}{index + 1:06d}",
            "first_name": given,
            "last_name": surname,
            "date_of_birth": date(1955 + index % 40, index % 12 + 1, index % 27 + 1).isoformat(),
            "gender": "MF"[index % 2],
            "ssn_last4": f"{index % 9000 + 1000:04d}",
            "email": f"{given.lower()}.{surname.lower()}{index}@example.com",
            "phone": f"+1555{index % 10000000:07d}",
            "address_line1": f"{index + 1} Main Street",
            "address_city": "New York",
            "address_state": states[index % len(states)],
            "address_postal_code": f"{10001 + index % 800:05d}",
        }
        if entity_type is EntityType.POLICY:
            base.update({
                "policy_number": f"{entity_type.value.upper()}{index + 1:06d}",
                "customer_key": f"CUS{index % 25 + 1:06d}",
                "product_code": ["TERM20-A", "IUL-01", "FIA-B"][index % 3],
                "status": ["active", "active", "lapsed", "issued"][index % 4],
                "lob": "Life",
                "state_of_issue": states[index % len(states)],
                "issue_date": date(2015 + index % 10, index % 12 + 1, 1).isoformat(),
                "effective_date": date(2015 + index % 10, index % 12 + 1, 15).isoformat(),
                "face_amount": float(50_000 * (index % 20 + 1)),
                "annualised_premium": float(120 * (index % 10 + 1)),
            })
        elif entity_type is EntityType.CLAIM:
            base.update({
                "claim_number": f"CLM{index + 1:06d}",
                "policy_number": f"POLICY{index + 1:06d}",
                "claim_type": ["death", "hospital", "disability"][index % 3],
                "status": ["open", "settled", "denied"][index % 3],
                "claim_amount": float(1_000 * (index % 50 + 1)),
                "paid_amount": float(500 * (index % 50)) if index % 3 else 0.0,
                "report_date": date(2024 + index % 2, index % 12 + 1, 1).isoformat(),
            })
        elif entity_type is EntityType.PREMIUM:
            base.update({
                "policy_number": f"POLICY{index + 1:06d}",
                "due_date": date(2026, (index % 12) + 1, 1).isoformat(),
                "amount_due": float(120 * (index % 10 + 1)),
                "amount_paid": float(120 * (index % 10 + 1)) if index % 3 else 0.0,
                "status": "paid" if index % 3 else "outstanding",
                "billing_frequency": "monthly",
                "commission_amount": round(120 * (index % 10 + 1) * 0.1, 2),
            })
        else:
            base.update({
                "lifetime_premium": float(1_000 * (index % 40)),
                "in_force_policy_count": index % 5,
            })
        out.append(base)
    return out


def source_payload_key(descriptor: SourceDescriptor, payload: Mapping[str, Any]) -> str:
    """Derive the source record id from a source's declared primary keys."""
    for key in descriptor.primary_keys:
        if key in payload:
            return str(payload[key])
    return str(payload.get("id") or uuid.uuid4().hex)


def ingest_synthetic(
    service: IngestionService, source_id: str, ctx: TenantContext, count: int = 25
) -> IngestionResult:
    """Load deterministic sample data through a registered source (demos/tests)."""
    descriptor = service.source(source_id)
    payloads = synthetic_batch(descriptor.entity_type, count, tenant_id=ctx.tenant_id)
    timer = Timer()
    timer.__enter__()
    with correlation_scope(ctx.correlation_id, ctx.tenant_id):
        return service._materialise(  # noqa: SLF001 - intentional internal use
            descriptor, ctx, payloads, uuid.uuid4().hex, timer.elapsed, IngestionMode.BATCH
        )


def ingestion_timestamp() -> float:
    """Wall-clock seconds, exposed for time-partition naming."""
    return time.time()


def partition_for(entity_type: EntityType, when: datetime | None = None) -> str:
    """Iceberg-style partition for an entity, e.g. ``entity=policy/date=2026-03-31``."""
    moment = when or datetime.now(UTC)
    return f"entity={entity_type.value}/date={moment.date().isoformat()}"
