"""Feature store for AI: versioned definitions, online serving and drift monitoring.

Plugin 3's underwriting models are only as trustworthy as the features behind
them, so the store here takes the Feast-style discipline: a feature is a *declared,
versioned, owned* artifact with a TTL and an offline/online split, not a column
somebody added to a query three weeks ago.

What it deliberately does not hide:

* every materialised value records the version and the source that produced it
* drift is computed as Population Stability Index against the training baseline,
  so a change in a feature's distribution raises an alert rather than quietly
  degrading a model's accuracy
"""

from __future__ import annotations

import math
import statistics
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pas_core.errors import NotFoundError, ValidationError
from pas_core.observability import DRIFT_SCORE, GLOBAL_METRICS
from pas_core.tenancy import TenantContext


class ValueType(StrEnum):
    FLOAT = "float"
    INT = "int"
    STRING = "string"
    BOOL = "bool"
    LIST = "list"


class Materialisation(StrEnum):
    OFFLINE = "offline"
    ONLINE = "online"
    BOTH = "both"


@dataclass(frozen=True, slots=True)
class FeatureDefinition:
    """A declared, owned, versioned feature.

    ``source`` names where the value comes from so lineage is automatic; ``ttl``
    bounds staleness in online serving, which is what stops a model scoring a
    policy against a lapsed-months-old risk profile.
    """

    name: str
    entity: str
    value_type: ValueType = ValueType.FLOAT
    description: str = ""
    owner: str = "unknown"
    version: int = 1
    ttl_seconds: int = 900
    tags: tuple[str, ...] = ()
    source: dict[str, Any] = field(default_factory=dict)
    transform: str | None = None
    nullable: bool = True
    monitoring_enabled: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            msg = f"feature name '{self.name}' must be alphanumeric with underscores"
            raise ValidationError(msg, feature=self.name)

    def materialisation(self) -> Materialisation:
        if self.ttl_seconds and self.ttl_seconds <= 86_400:
            return Materialisation.BOTH
        return Materialisation.OFFLINE

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "entity": self.entity,
            "valueType": str(self.value_type),
            "description": self.description,
            "owner": self.owner,
            "version": self.version,
            "ttlSeconds": self.ttl_seconds,
            "tags": list(self.tags),
            "source": self.source,
            "transform": self.transform,
            "nullable": self.nullable,
            "monitoringEnabled": self.monitoring_enabled,
            "materialisation": str(self.materialisation()),
            "createdAt": self.created_at.isoformat(),
        }


@dataclass(slots=True)
class FeatureValue:
    """One materialised feature value with its provenance and expiry."""

    feature_name: str
    version: int
    entity_key: str
    value: Any  # noqa: ANN401
    event_timestamp: datetime
    materialisation: Materialisation = Materialisation.BOTH
    source: dict[str, Any] = field(default_factory=dict)
    expires_at: datetime | None = None

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or datetime.now(UTC)) > self.expires_at

    def age_seconds(self, now: datetime | None = None) -> float:
        return ((now or datetime.now(UTC)) - self.event_timestamp).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        return {
            "featureName": self.feature_name,
            "version": self.version,
            "entityKey": self.entity_key,
            "value": self.value,
            "eventTimestamp": self.event_timestamp.isoformat(),
            "materialisation": str(self.materialisation),
            "expiresAt": self.expires_at.isoformat() if self.expires_at else None,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class EntityKey:
    """Identifies the entity a feature value belongs to."""

    entity: str
    key: str

    def to_dict(self) -> dict[str, Any]:
        return {"entity": self.entity, "key": self.key}

    def __str__(self) -> str:
        return f"{self.entity}:{self.key}"


@dataclass(slots=True)
class FeatureVector:
    """A materialised set of features for one entity, as the model receives it."""

    entity: EntityKey
    values: dict[str, Any] = field(default_factory=dict)
    versions: dict[str, int] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def completeness(self) -> float:
        total = len(self.values) + len(self.missing)
        return round(len(self.values) / total, 4) if total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity.to_dict(),
            "values": self.values,
            "versions": self.versions,
            "missingFeatures": self.missing,
            "staleFeatures": self.stale,
            "completeness": self.completeness(),
            "retrievedAt": self.retrieved_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class DriftReport:
    """Population Stability Index for one feature against its baseline."""

    feature_name: str
    version: int
    psi: float
    baseline_count: int
    current_count: int
    buckets: tuple[dict[str, Any], ...] = ()
    alert: bool = False
    severity: str = "ok"
    computed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "featureName": self.feature_name,
            "version": self.version,
            "psi": round(self.psi, 4),
            "baselineCount": self.baseline_count,
            "currentCount": self.current_count,
            "alert": self.alert,
            "severity": self.severity,
            "buckets": list(self.buckets),
            "computedAt": self.computed_at.isoformat(),
        }


def population_stability_index(
    baseline: Sequence[float], current: Sequence[float], buckets: int = 10
) -> tuple[float, tuple[dict[str, Any], ...]]:
    """PSI with quantile-based bin edges.

    PSI is used because it is the metric carriers' model-risk teams already use and
    because a fixed-width binning silently hides drift in a heavy-tailed feature
    such as face amount.
    """
    if not baseline or not current:
        return 0.0, ()
    edges = _quantile_edges(baseline, buckets)
    baseline_counts = _histogram(baseline, edges)
    current_counts = _histogram(current, edges)

    total_psi = 0.0
    detail: list[dict[str, Any]] = []
    # ``edges`` has buckets+1 entries, so the pairings are intentionally unequal
    # length - do not use strict=True here.
    for index, (lower, upper) in enumerate(zip(edges, edges[1:], strict=False)):
        b = baseline_counts[index] / len(baseline)
        c = current_counts[index] / len(current)
        # A zero cell makes the raw ratio infinite; a small epsilon keeps PSI finite
        # and still reports a large shift.
        b_adj = max(b, 1e-6)
        c_adj = max(c, 1e-6)
        contribution = (c_adj - b_adj) * math.log(c_adj / b_adj)
        total_psi += contribution
        detail.append({
            "lower": round(lower, 6),
            "upper": round(upper, 6),
            "baselineShare": round(b, 6),
            "currentShare": round(c, 6),
            "contribution": round(contribution, 6),
        })
    return total_psi, tuple(detail)


def _quantile_edges(values: Sequence[float], buckets: int) -> list[float]:
    ordered = sorted(values)
    edges = [-math.inf]
    for i in range(1, buckets):
        position = (i / buckets) * (len(ordered) - 1)
        low = math.floor(position)
        high = math.ceil(position)
        weight = position - low
        edges.append(ordered[low] * (1 - weight) + ordered[high] * weight)
    edges.append(math.inf)
    return edges


def _histogram(values: Sequence[float], edges: Sequence[float]) -> list[int]:
    counts = [0] * (len(edges) - 1)
    for value in values:
        for index in range(len(edges) - 1):
            if edges[index] <= value < edges[index + 1]:
                counts[index] += 1
                break
        else:  # the final edge is +inf so this is unreachable, but be explicit
            counts[-1] += 1
    return counts


def _same_definition(left: FeatureDefinition, right: FeatureDefinition) -> bool:
    """Whether two feature definitions declare identical content.

    `created_at` is excluded deliberately. It is a wall-clock stamp, so comparing
    it would make "re-registering an identical definition is a no-op" true only
    within a single clock tick - a guarantee that holds in a fast unit test and
    fails intermittently in production, which is the worst way for it to fail.
    """
    volatile = ("createdAt", "created_at")
    left_content = {k: v for k, v in left.to_dict().items() if k not in volatile}
    right_content = {k: v for k, v in right.to_dict().items() if k not in volatile}
    return left_content == right_content


class FeatureStore:
    """Versioned feature definitions with online and offline materialisation."""

    def __init__(self, *, default_ttl: int = 900, drift_threshold: float = 0.20) -> None:
        self._definitions: dict[str, list[FeatureDefinition]] = {}
        self._values: dict[str, dict[tuple[str, str], FeatureValue]] = {}
        self._baselines: dict[str, list[float]] = {}
        self._drift: dict[str, list[DriftReport]] = {}
        self._default_ttl = default_ttl
        self._drift_threshold = drift_threshold

    # -- definitions -------------------------------------------------------
    def register(self, definition: FeatureDefinition, *, ctx: TenantContext | None = None) -> FeatureDefinition:
        """Register a feature version.

        Re-registering an identical definition is a no-op, so seeding is idempotent
        and a re-run on every request cannot fail. Registering the *same version*
        with different content is an error: two definitions claiming version 1 is
        exactly the ambiguity that makes a feature store untrustworthy.
        """
        tenant = ctx.tenant_id if ctx else "system"
        versions = self._definitions.setdefault(f"{tenant}:{definition.name}", [])
        existing = next((d for d in versions if d.version == definition.version), None)
        if existing is not None:
            if _same_definition(existing, definition):
                return existing
            msg = (
                f"feature '{definition.name}' version {definition.version} already exists "
                "with different content; publish the change as a new version"
            )
            raise ValidationError(msg, feature=definition.name, version=definition.version)
        versions.append(definition)
        versions.sort(key=lambda d: d.version)
        GLOBAL_METRICS.increment("feature_store_registrations_total", feature=definition.name)
        return definition

    def register_many(
        self, definitions: Iterable[FeatureDefinition], *, ctx: TenantContext | None = None
    ) -> list[FeatureDefinition]:
        return [self.register(d, ctx=ctx) for d in definitions]

    def definition(
        self, name: str, version: int | None = None, *, ctx: TenantContext | None = None
    ) -> FeatureDefinition:
        tenant = ctx.tenant_id if ctx else "system"
        versions = self._definitions.get(f"{tenant}:{name}")
        if not versions:
            raise NotFoundError("feature definition", name)
        if version is None:
            return versions[-1]
        for candidate in versions:
            if candidate.version == version:
                return candidate
        raise NotFoundError("feature version", f"{name}@{version}")

    def definitions(
        self, *, ctx: TenantContext | None = None, tag: str | None = None
    ) -> list[FeatureDefinition]:
        tenant = ctx.tenant_id if ctx else "system"
        rows = [
            versions[-1]
            for key, versions in self._definitions.items()
            if key.startswith(f"{tenant}:")
        ]
        if tag:
            rows = [d for d in rows if tag in d.tags]
        return sorted(rows, key=lambda d: d.name)

    # -- materialisation ---------------------------------------------------
    def materialize(
        self,
        entity: EntityKey,
        values: dict[str, Any],
        ctx: TenantContext,
        *,
        source: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> FeatureVector:
        """Write feature values for an entity, resolving versions and TTLs."""
        moment = now or datetime.now(UTC)
        vector = FeatureVector(entity=entity)
        store = self._values.setdefault(ctx.tenant_id, {})
        for name, value in values.items():
            try:
                definition = self.definition(name, ctx=ctx)
            except NotFoundError:
                vector.missing.append(name)
                continue
            feature_value = FeatureValue(
                feature_name=name,
                version=definition.version,
                entity_key=entity.key,
                value=_cast(value, definition.value_type),
                event_timestamp=moment,
                materialisation=definition.materialisation(),
                source={"definition": definition.to_dict()["source"], **(source or {})},
                expires_at=moment + timedelta(seconds=definition.ttl_seconds),
            )
            store[(entity.entity, f"{entity.key}:{name}")] = feature_value
            vector.values[name] = feature_value.value
            vector.versions[name] = definition.version
        GLOBAL_METRICS.increment("feature_store_materialisations_total", entity=entity.entity)
        GLOBAL_METRICS.observe("feature_store_materialisation_count", float(len(vector.values)))
        return vector

    def get(
        self, entity: EntityKey, name: str, ctx: TenantContext, *, allow_stale: bool = False
    ) -> Any:  # noqa: ANN401
        """Online read with TTL enforcement."""
        stored = self._values.get(ctx.tenant_id, {}).get((entity.entity, f"{entity.key}:{name}"))
        if stored is None:
            return None
        if not allow_stale and stored.is_expired():
            GLOBAL_METRICS.increment("feature_store_expired_reads_total", feature=name)
            return None
        return stored.value

    def vector(self, entity: EntityKey, ctx: TenantContext, *, names: Sequence[str] | None = None) -> FeatureVector:
        """Fetch a whole feature vector for online inference."""
        wanted = list(names) if names else [d.name for d in self.definitions(ctx=ctx, tag="model")]
        vector = FeatureVector(entity=entity)
        store = self._values.get(ctx.tenant_id, {})
        for name in wanted:
            stored = store.get((entity.entity, f"{entity.key}:{name}"))
            if stored is None or stored.is_expired():
                (vector.stale if stored is not None else vector.missing).append(name)
                continue
            vector.values[name] = stored.value
            vector.versions[name] = stored.version
        return vector

    def history(self, entity: EntityKey, name: str, ctx: TenantContext) -> list[FeatureValue]:
        """Offline read: the value trail, for model training and dispute review."""
        stored = [
            v for (kind, key), v in self._values.get(ctx.tenant_id, {}).items()
            if kind == entity.entity and key.startswith(f"{entity.key}:{name}:")
        ]
        if not stored:
            stored = [
                v for (kind, key), v in self._values.get(ctx.tenant_id, {}).items()
                if kind == entity.entity and key == f"{entity.key}:{name}"
            ]
        return sorted(stored, key=lambda v: v.event_timestamp)

    # -- drift -------------------------------------------------------------
    def set_baseline(self, name: str, values: Sequence[float], ctx: TenantContext) -> int:
        """Freeze the training distribution for drift comparison."""
        self._baselines[f"{ctx.tenant_id}:{name}"] = list(values)
        return len(values)

    def compute_drift(
        self, name: str, current: Sequence[float], ctx: TenantContext, *, version: int | None = None
    ) -> DriftReport:
        """PSI between the training baseline and the current population."""
        baseline = self._baselines.get(f"{ctx.tenant_id}:{name}", [])
        definition_version = version
        if definition_version is None:
            try:
                definition_version = self.definition(name, ctx=ctx).version
            except NotFoundError:
                definition_version = 0
        psi, buckets = population_stability_index(baseline, current)
        # Conventional thresholds: <0.10 stable, <0.25 moderate, >=0.25 significant.
        severity = "ok" if psi < 0.10 else ("moderate" if psi < 0.25 else "significant")
        report = DriftReport(
            feature_name=name,
            version=definition_version,
            psi=psi,
            baseline_count=len(baseline),
            current_count=len(current),
            buckets=buckets,
            alert=psi >= self._drift_threshold,
            severity=severity,
        )
        self._drift.setdefault(ctx.tenant_id, []).append(report)
        if DRIFT_SCORE is not None:
            DRIFT_SCORE.labels(feature=name, model=f"v{definition_version}").set(psi)
        GLOBAL_METRICS.set_gauge("feature_drift_psi", psi, feature=name)
        return report

    def drift_reports(self, ctx: TenantContext) -> list[DriftReport]:
        """Latest report per feature."""
        latest: dict[str, DriftReport] = {}
        for report in self._drift.get(ctx.tenant_id, []):
            latest[report.feature_name] = report
        return [latest[name] for name in sorted(latest)]

    def retraining_trigger(self, ctx: TenantContext, *, threshold: float | None = None) -> dict[str, Any]:
        """Which features have drifted enough to warrant retraining."""
        limit = threshold if threshold is not None else self._drift_threshold
        drifted = [r for r in self.drift_reports(ctx) if r.psi >= limit]
        return {
            "tenantId": ctx.tenant_id,
            "threshold": limit,
            "retrainingRecommended": bool(drifted),
            "driftedFeatures": [r.feature_name for r in drifted],
            "reports": [r.to_dict() for r in drifted],
        }

    def stats(self, ctx: TenantContext) -> dict[str, Any]:
        definitions = self.definitions(ctx=ctx)
        materialised = len(self._values.get(ctx.tenant_id, {}))
        return {
            "tenantId": ctx.tenant_id,
            "featureCount": len(definitions),
            "materialisedValues": materialised,
            "entities": len({k for k in self._values.get(ctx.tenant_id, {}) if True}),
            "online": sum(1 for d in definitions if d.ttl_seconds <= 86_400),
            "offline": sum(1 for d in definitions if d.ttl_seconds > 86_400),
            "monitored": sum(1 for d in definitions if d.monitoring_enabled),
            "driftAlerts": sum(1 for r in self.drift_reports(ctx) if r.alert),
        }

    def purge_expired(self, ctx: TenantContext, *, now: datetime | None = None) -> int:
        moment = now or datetime.now(UTC)
        store = self._values.get(ctx.tenant_id, {})
        doomed = [k for k, v in store.items() if v.is_expired(moment)]
        for key in doomed:
            store.pop(key, None)
        return len(doomed)


def _cast(value: Any, value_type: ValueType) -> Any:  # noqa: ANN401
    try:
        if value_type is ValueType.FLOAT:
            return float(value)
        if value_type is ValueType.INT:
            return int(value)
        if value_type is ValueType.BOOL:
            return bool(value)
        if value_type is ValueType.STRING:
            return str(value)
        if value_type is ValueType.LIST:
            return list(value) if isinstance(value, (list, tuple)) else [value]
    except (TypeError, ValueError):
        return None
    return value


# ---------------------------------------------------------------------------
# Bundled features
# ---------------------------------------------------------------------------
def underwriting_features() -> list[FeatureDefinition]:
    """Features consumed by plugin 3's underwriting model."""
    return [
        FeatureDefinition(
            name="applicant_age_band",
            entity="submission",
            value_type=ValueType.STRING,
            description="Age band used for mortality rating: 18-25, 26-35, 36-45, 46-55, 56-65, 66+.",
            owner="actuarial",
            ttl_seconds=86_400,
            tags=("model", "underwriting", "categorical"),
            source={"dataset": "gold.customer", "field": "date_of_birth"},
        ),
        FeatureDefinition(
            name="face_amount_tier",
            entity="submission",
            value_type=ValueType.STRING,
            description="Requested face amount tier, used for authority and complexity routing.",
            owner="underwriting",
            ttl_seconds=86_400,
            tags=("model", "underwriting", "categorical"),
            source={"dataset": "silver.policy", "field": "face_amount"},
        ),
        FeatureDefinition(
            name="prior_claims_count",
            entity="customer",
            value_type=ValueType.INT,
            description="Count of claims in the trailing 36 months.",
            owner="claims",
            ttl_seconds=3_600,
            tags=("model", "underwriting"),
            source={"dataset": "gold.claim", "aggregation": "count(36m)"},
        ),
        FeatureDefinition(
            name="policy_in_force_count",
            entity="customer",
            value_type=ValueType.INT,
            description="Number of in-force policies held by the customer.",
            owner="policy",
            ttl_seconds=3_600,
            tags=("model", "underwriting", "data-mesh"),
            source={"dataset": "gold.policy", "field": "in_force_policy_count"},
        ),
        FeatureDefinition(
            name="mib_review_status_score",
            entity="customer",
            value_type=ValueType.FLOAT,
            description="Encoded MIB (Insureability Review) status: 0 clear, 1 contested, 2 upheld.",
            owner="compliance",
            ttl_seconds=86_400,
            tags=("model", "underwriting", "compliance"),
            source={"dataset": "external.mib", "transform": "ordinal_encode"},
            monitoring_enabled=True,
        ),
        FeatureDefinition(
            name="annualised_premium_total",
            entity="customer",
            value_type=ValueType.FLOAT,
            description="Total annualised premium across in-force policies.",
            owner="policy",
            ttl_seconds=3_600,
            tags=("model", "reporting", "data-mesh"),
            source={"dataset": "gold.policy", "aggregation": "sum(annualised_premium)"},
        ),
        FeatureDefinition(
            name="days_since_last_activity",
            entity="customer",
            value_type=ValueType.INT,
            description="Days since the most recent servicing or payment activity.",
            owner="data-eng",
            ttl_seconds=7_200,
            tags=("model", "retention"),
            source={"dataset": "gold.policy", "transform": "date_diff(now, max(event_date))"},
        ),
        FeatureDefinition(
            name="agent_tenure_months",
            entity="agent",
            value_type=ValueType.INT,
            description="Months the writing agent has been appointed.",
            owner="distribution",
            ttl_seconds=604_800,
            tags=("model", "distribution"),
            source={"dataset": "crm.agent", "field": "appointed_date"},
        ),
    ]


def seed_feature_store(store: FeatureStore, ctx: TenantContext) -> FeatureStore:
    """Register the bundled underwriting features.

    Idempotent: the bundled definitions are only published once per tenant, so this
    is safe to call on every request.
    """
    existing = {d.name for d in store.definitions(ctx=ctx)}
    store.register_many(
        [d for d in underwriting_features() if d.name not in existing], ctx=ctx
    )
    return store


def simulate_scores(values: Sequence[float], *, mean: float = 0.0, sd: float = 1.0, seed: int = 7) -> list[float]:
    """Deterministic pseudo-normal sample used to demonstrate drift monitoring.

    Uses a fixed seed so drift dashboards and their tests are reproducible; a real
    deployment feeds the store from the actual production population.
    """
    import random  # noqa: PLC0415

    rng = random.Random(seed)
    return [mean + sd * rng.gauss(0, 1) for _ in values] if values else []


def population_summary(values: Sequence[float]) -> dict[str, Any]:
    """Descriptive statistics for a feature population."""
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "mean": round(statistics.fmean(values), 6),
        "stdev": round(statistics.pstdev(values), 6) if len(values) > 1 else 0.0,
        "min": round(min(values), 6),
        "p25": round(statistics.quantiles(values, n=4)[0], 6) if len(values) > 3 else round(min(values), 6),
        "median": round(statistics.median(values), 6),
        "p75": round(statistics.quantiles(values, n=4)[2], 6) if len(values) > 3 else round(max(values), 6),
        "max": round(max(values), 6),
    }


def build_entity_key(entity: str, key: str) -> EntityKey:
    """Factory used by the API layer."""
    return EntityKey(entity=entity, key=key)


def now_seconds() -> float:
    """Wall clock helper for TTL maths."""
    return time.time()


def feature_run_id() -> str:
    return uuid.uuid4().hex
