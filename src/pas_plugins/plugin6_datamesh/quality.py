"""Data quality rules, scoring and lineage.

Two things a carrier genuinely needs and rarely has:

* **A quality score per dataset**, so an AI agent can be told how much to trust a
  record instead of presenting a five-day-old partial extract as fact.
* **Lineage**, so that when a number turns out to be wrong, someone can find out
  which source record produced it and when.

Quality rules are data, so a data steward can add one without a release; lineage
follows the OpenLineage shape so it drops into an existing catalogue.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pas_core.errors import ValidationError
from pas_core.observability import GLOBAL_METRICS
from pas_core.tenancy import TenantContext
from pas_plugins.plugin6_datamesh.models import BaseEntity, EntityType


class QualityDimension(StrEnum):
    """The standard data-quality dimensions, as carriers report them."""

    COMPLETENESS = "completeness"
    ACCURACY = "accuracy"
    CONSISTENCY = "consistency"
    TIMELINESS = "timeliness"
    UNIQUENESS = "uniqueness"
    VALIDITY = "validity"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {"info": 0, "warning": 1, "error": 2, "critical": 3}[str(self.value)]


@dataclass(frozen=True, slots=True)
class QualityRule:
    """One declarative data-quality check.

    ``check`` receives the record and returns ``True`` when the record passes.
    Rules are pure predicates on a single record unless ``scope`` is ``dataset``,
    in which case the check receives the whole collection.
    """

    rule_id: str
    name: str
    dimension: QualityDimension
    severity: Severity
    description: str = ""
    fields: tuple[str, ...] = ()
    check: Callable[[Any], bool] = field(default=lambda _record: True, repr=False)
    scope: str = "record"
    remediation: str = ""
    weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ruleId": self.rule_id,
            "name": self.name,
            "dimension": str(self.dimension),
            "severity": str(self.severity),
            "description": self.description,
            "fields": list(self.fields),
            "scope": self.scope,
            "remediation": self.remediation,
            "weight": self.weight,
        }


def _present(value: Any) -> bool:  # noqa: ANN401
    return value not in (None, "", [], {})


DEFAULT_QUALITY_RULES: tuple[QualityRule, ...] = (
    QualityRule(
        rule_id="DQ-001",
        name="Required fields are populated",
        dimension=QualityDimension.COMPLETENESS,
        severity=Severity.ERROR,
        description="Every business-critical field must carry a value.",
        fields=("natural_key", "source_system", "source_record_id"),
        check=lambda r: all(_present(getattr(r, f, None)) for f in ("natural_key", "source_record_id")),
    ),
    QualityRule(
        rule_id="DQ-002",
        name="Date of birth is plausible",
        dimension=QualityDimension.VALIDITY,
        severity=Severity.WARNING,
        description="A customer must be between 0 and 120 years old.",
        fields=("date_of_birth",),
        check=lambda r: _age_ok(getattr(r, "date_of_birth", None)),
    ),
    QualityRule(
        rule_id="DQ-003",
        name="State code is valid",
        dimension=QualityDimension.VALIDITY,
        severity=Severity.WARNING,
        description="US state or territory code.",
        fields=("state_of_issue", "address_state"),
        check=lambda r: _state_ok(r),
    ),
    QualityRule(
        rule_id="DQ-004",
        name="Contact details are present",
        dimension=QualityDimension.COMPLETENESS,
        severity=Severity.INFO,
        description="At least one contact channel (email or phone) should exist.",
        fields=("email", "phone"),
        check=lambda r: _contact_ok(r),
    ),
    QualityRule(
        rule_id="DQ-005",
        name="Amounts are non-negative",
        dimension=QualityDimension.VALIDITY,
        severity=Severity.ERROR,
        description="Face amounts, premiums and claim amounts cannot be negative.",
        check=lambda r: _amounts_non_negative(r),
    ),
    QualityRule(
        rule_id="DQ-006",
        name="Effective date is not before issue date",
        dimension=QualityDimension.CONSISTENCY,
        severity=Severity.ERROR,
        description="A contract cannot take effect before it was issued.",
        fields=("issue_date", "effective_date"),
        check=lambda r: _dates_ordered(r),
    ),
    QualityRule(
        rule_id="DQ-007",
        name="Natural keys are unique within a source",
        dimension=QualityDimension.UNIQUENESS,
        severity=Severity.CRITICAL,
        description="A source must not deliver the same business key twice.",
        scope="dataset",
        fields=("natural_key",),
        check=lambda rows: len({r.natural_key for r in rows}) == len(rows),
    ),
    QualityRule(
        rule_id="DQ-008",
        name="Records are fresh",
        dimension=QualityDimension.TIMELINESS,
        severity=Severity.WARNING,
        description="A gold record should have been refreshed within 90 days.",
        scope="dataset",
        check=lambda rows: all(
            (datetime.now(UTC) - r.effective_from).days <= 90 for r in rows
        ),
    ),
    QualityRule(
        rule_id="DQ-009",
        name="Merged records have no unresolved conflicts",
        dimension=QualityDimension.CONSISTENCY,
        severity=Severity.WARNING,
        description=(
            "Survivorship picked a winner for every conflicting field, so the record is "
            "usable - but the conflict is retained so a data steward can chase the source "
            "that disagrees with the system of record."
        ),
        scope="record",
        fields=(),
        check=lambda r: not (r.attributes.get("_conflicts") or []),
        remediation=(
            "Reconcile the disagreeing source with the PAS. Use GET /data/lineage/impact "
            "to find downstream datasets affected."
        ),
    ),
)


def _age_ok(value: Any) -> bool:  # noqa: ANN401
    if value is None:
        return True
    from datetime import date  # noqa: PLC0415

    if not isinstance(value, date):
        return False
    days = (date.today() - value).days
    return 0 <= days <= 120 * 365


US_STATES = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV",
    "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN",
    "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC", "PR", "VI", "GU", "AS", "MP",
})


def _state_ok(record: Any) -> bool:  # noqa: ANN401
    for field_name in ("state_of_issue", "address_state"):
        value = getattr(record, field_name, None)
        if value and str(value).upper() not in US_STATES:
            return False
    return True


def _contact_ok(record: Any) -> bool:  # noqa: ANN401
    if not hasattr(record, "email") and not hasattr(record, "phone"):
        return True  # not a customer record
    return bool(getattr(record, "email", None) or getattr(record, "phone", None))


def _amounts_non_negative(record: Any) -> bool:  # noqa: ANN401
    for field_name in (
        "face_amount", "annualised_premium", "claim_amount", "paid_amount",
        "reserve_amount", "amount_due", "amount_paid", "commission_amount",
        "lifetime_premium",
    ):
        value = getattr(record, field_name, None)
        if value is not None and float(value) < 0:
            return False
    return True


def _dates_ordered(record: Any) -> bool:  # noqa: ANN401
    issue = getattr(record, "issue_date", None)
    effective = getattr(record, "effective_date", None)
    expiration = getattr(record, "expiration_date", None)
    if issue and effective and str(effective) < str(issue):
        return False
    if issue and expiration and str(expiration) <= str(issue):
        return False
    return True


@dataclass(slots=True)
class RuleResult:
    """Outcome of evaluating one quality rule against a dataset."""

    rule_id: str
    name: str
    dimension: str
    severity: str
    passed: bool
    pass_rate: float
    records_checked: int
    records_failed: int
    message: str = ""
    remediation: str = ""
    sample_failures: list[dict[str, Any]] = field(default_factory=list)

    @property
    def weight(self) -> float:
        return {"info": 0.25, "warning": 0.5, "error": 1.0, "critical": 1.5}[self.severity]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ruleId": self.rule_id,
            "name": self.name,
            "dimension": self.dimension,
            "severity": self.severity,
            "passed": self.passed,
            "passRate": round(self.pass_rate, 4),
            "recordsChecked": self.records_checked,
            "recordsFailed": self.records_failed,
            "message": self.message,
            "remediation": self.remediation,
            "sampleFailures": self.sample_failures[:5],
        }


@dataclass(slots=True)
class QualityReport:
    """Aggregated quality picture for one dataset."""

    dataset: str
    tenant_id: str
    entity_type: str
    record_count: int
    score: float
    grade: str
    dimensions: dict[str, float]
    results: list[RuleResult]
    blocking: bool = False
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def failed_rules(self) -> list[RuleResult]:
        return [r for r in self.results if not r.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "tenantId": self.tenant_id,
            "entityType": self.entity_type,
            "recordCount": self.record_count,
            "score": round(self.score, 4),
            "grade": self.grade,
            "dimensions": {k: round(v, 4) for k, v in self.dimensions.items()},
            "blockingIssues": self.blocking,
            "passed": len(self.results) - len(self.failed_rules),
            "failed": len(self.failed_rules),
            "rules": [r.to_dict() for r in self.results],
            "evaluatedAt": self.evaluated_at.isoformat(),
        }


class DataQualityEngine:
    """Evaluates quality rules and produces a score a downstream consumer can gate on."""

    def __init__(self, *, quality_floor: float = 0.95) -> None:
        self._rules: dict[str, list[QualityRule]] = {}
        self._quality_floor = quality_floor
        self._reports: dict[str, list[QualityReport]] = {}

    def register(self, rule: QualityRule, *, entity_type: EntityType | str = "all") -> QualityRule:
        key = str(entity_type)
        self._rules.setdefault(key, [])
        if any(r.rule_id == rule.rule_id for r in self._rules[key]):
            msg = f"quality rule '{rule.rule_id}' is already registered for '{key}'"
            raise ValidationError(msg, ruleId=rule.rule_id, entityType=key)
        self._rules[key].append(rule)
        return rule

    def register_defaults(self) -> list[QualityRule]:
        """Register the bundled rules for every entity type."""
        registered: list[QualityRule] = []
        for entity_type in EntityType:
            for rule in DEFAULT_QUALITY_RULES:
                applicable = _rule_applies(rule, entity_type)
                if applicable:
                    registered.append(self.register(rule, entity_type=entity_type))
        return registered

    def rules(self, entity_type: EntityType | str = "all") -> list[QualityRule]:
        return list(self._rules.get(str(entity_type), []))

    def evaluate(
        self, records: Sequence[BaseEntity], ctx: TenantContext, *, dataset: str | None = None
    ) -> QualityReport:
        """Evaluate every applicable rule and produce a scored report."""
        entity_type = records[0].entity_type if records else EntityType.CUSTOMER
        name = dataset or f"{entity_type.value}-gold"
        rules = self._rules.get(str(entity_type)) or self._rules.get("all") or list(DEFAULT_QUALITY_RULES)

        results: list[RuleResult] = []
        for rule in rules:
            results.append(self._evaluate_rule(rule, records))

        dimensions: dict[str, list[tuple[float, float]]] = {}
        for rule, result in zip(rules, results, strict=True):
            dimensions.setdefault(str(rule.dimension), []).append((result.pass_rate, rule.weight))

        dimension_scores = {
            dimension: sum(rate * weight for rate, weight in values) / sum(w for _, w in values)
            for dimension, values in dimensions.items()
            if values
        }
        total_weight = sum(r.weight for r in results)
        score = (
            sum(r.pass_rate * r.weight for r in results) / total_weight if total_weight else 0.0
        )
        blocking = any(
            r.severity in {"error", "critical"} and not r.passed for r in results
        )

        report = QualityReport(
            dataset=name,
            tenant_id=ctx.tenant_id,
            entity_type=str(entity_type),
            record_count=len(records),
            score=score,
            grade=grade_for(score),
            dimensions=dimension_scores,
            results=results,
            blocking=blocking,
        )
        self._reports.setdefault(ctx.tenant_id, []).append(report)
        GLOBAL_METRICS.set_gauge(
            "datamesh_quality_score", score, dataset=name, tenant=ctx.tenant_id
        )
        GLOBAL_METRICS.increment(
            "datamesh_quality_evaluations_total",
            dataset=name,
            outcome="blocking" if blocking else "pass",
        )
        return report

    def _evaluate_rule(self, rule: QualityRule, records: Sequence[BaseEntity]) -> RuleResult:
        checked = len(records)
        failures: list[dict[str, Any]] = []
        if rule.scope == "dataset":
            try:
                passed = bool(rule.check(list(records))) if records else True
            except Exception:  # noqa: BLE001
                passed = False
            return RuleResult(
                rule_id=rule.rule_id,
                name=rule.name,
                dimension=str(rule.dimension),
                severity=str(rule.severity),
                passed=passed,
                pass_rate=1.0 if passed else 0.0,
                records_checked=checked,
                records_failed=0 if passed else checked,
                message="dataset-level check " + ("passed" if passed else "failed"),
                remediation=rule.remediation,
            )

        failed = 0
        for record in records:
            try:
                ok = bool(rule.check(record))
            except Exception:  # noqa: BLE001 - a broken rule must not stop evaluation
                ok = False
            if not ok:
                failed += 1
                if len(failures) < 10:
                    failures.append({
                        "naturalKey": record.natural_key,
                        "sourceSystem": str(record.source_system),
                        "fields": [
                            f for f in rule.fields
                            if not _present(getattr(record, f, None))
                        ] or list(rule.fields),
                    })
        pass_rate = (checked - failed) / checked if checked else 1.0
        return RuleResult(
            rule_id=rule.rule_id,
            name=rule.name,
            dimension=str(rule.dimension),
            severity=str(rule.severity),
            passed=failed == 0,
            pass_rate=pass_rate,
            records_checked=checked,
            records_failed=failed,
            message=f"{failed} of {checked} record(s) failed" if failed else "all records passed",
            remediation=rule.remediation,
            sample_failures=failures,
        )

    def report(self, ctx: TenantContext, dataset: str) -> QualityReport | None:
        reports = self._reports.get(ctx.tenant_id, [])
        return next((r for r in reversed(reports) if r.dataset == dataset), None)

    def reports(self, ctx: TenantContext) -> list[QualityReport]:
        return list(self._reports.get(ctx.tenant_id, []))

    def assert_publishable(self, report: QualityReport) -> QualityReport:
        """Block promotion to gold when quality is below the floor."""
        if report.score < self._quality_floor or report.blocking:
            failed = ", ".join(r.rule_id for r in report.failed_rules[:6])
            from pas_core.errors import BusinessRuleViolation  # noqa: PLC0415

            raise BusinessRuleViolation(
                f"dataset '{report.dataset}' scored {report.score:.3f} "
                f"(floor {self._quality_floor:.3f}); failing rules: {failed}",
                dataset=report.dataset,
                score=round(report.score, 4),
                qualityFloor=self._quality_floor,
                failingRules=[r.rule_id for r in report.failed_rules],
            )
        return report


def _rule_applies(rule: QualityRule, entity_type: EntityType) -> bool:
    """Whether a bundled rule is meaningful for an entity type."""
    if rule.scope in {"dataset", "cluster"}:
        return True
    if rule.rule_id == "DQ-002":
        return entity_type is EntityType.CUSTOMER
    if rule.rule_id == "DQ-003":
        return entity_type in {EntityType.CUSTOMER, EntityType.POLICY}
    if rule.rule_id == "DQ-004":
        return entity_type is EntityType.CUSTOMER
    if rule.rule_id == "DQ-006":
        return entity_type is EntityType.POLICY
    return True


def grade_for(score: float) -> str:
    """Letter grade used on the data-mesh dashboard."""
    if score >= 0.98:
        return "A+"
    if score >= 0.95:
        return "A"
    if score >= 0.90:
        return "B"
    if score >= 0.80:
        return "C"
    if score >= 0.70:
        return "D"
    return "F"


# ---------------------------------------------------------------------------
# Lineage
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class LineageEvent:
    """An OpenLineage-shaped run event."""

    run_id: str
    event_type: str
    event_time: datetime
    inputs: tuple[dict[str, Any], ...] = ()
    outputs: tuple[dict[str, Any], ...] = ()
    job: dict[str, Any] = field(default_factory=dict)
    tenant_id: str = ""

    def to_json(self) -> dict[str, Any]:
        """Serialise to the OpenLineage RunEvent shape."""

        def facet(namespace: str, name: str, value: Any) -> dict[str, Any]:
            return {
                "_producer": "https://github.com/OpenLineage/OpenLineage",
                "_schemaURL": f"https://openlineage.io/spec/facets/2-0-1/{namespace}.json",
                name: value,
            }

        return {
            "eventType": self.event_type,
            "eventTime": self.event_time.isoformat(),
            "run": {
                "runId": self.run_id,
                "facets": {
                    "nominalTime": facet("nominalTime", "nominalTime", self.event_time.isoformat()),
                    "custom": {"pas": {"tenantId": self.tenant_id}},
                },
            },
            "job": {
                "name": self.job.get("name", "pas-data-mesh"),
                "namespace": self.job.get("namespace", "pas-plugins"),
                "facets": {},
            },
            "inputs": [
                {"namespace": i.get("namespace", "pas"), "name": i["name"], "facets": {}}
                for i in self.inputs
            ],
            "outputs": [
                {"namespace": o.get("namespace", "pas"), "name": o["name"], "facets": {}}
                for o in self.outputs
            ],
            "producer": "https://github.com/pas-plugins/data-mesh",
        }


class LineageTracker:
    """Builds and serves the lineage graph from sources to golden records."""

    def __init__(self, *, enabled: bool = True) -> None:
        self._events: dict[str, list[LineageEvent]] = {}
        self._enabled = enabled

    def record_run(
        self,
        ctx: TenantContext,
        *,
        run_id: str,
        job_name: str,
        inputs: list[dict[str, Any]],
        outputs: list[dict[str, Any]],
        event_type: str = "START",
    ) -> LineageEvent | None:
        if not self._enabled:
            return None
        event = LineageEvent(
            run_id=run_id,
            event_type=event_type,
            event_time=datetime.now(UTC),
            inputs=tuple(inputs),
            outputs=tuple(outputs),
            job={"name": job_name, "namespace": "pas-plugins"},
            tenant_id=ctx.tenant_id,
        )
        self._events.setdefault(ctx.tenant_id, []).append(event)
        GLOBAL_METRICS.increment("datamesh_lineage_events_total", job=job_name)
        return event

    def graph(self, ctx: TenantContext) -> dict[str, Any]:
        """Nodes and edges, ready for the Svelte lineage visualisation."""
        if not self._enabled:
            return {"enabled": False, "nodes": [], "edges": []}
        nodes: dict[str, dict[str, Any]] = {}
        edges: dict[tuple[str, str], dict[str, Any]] = {}
        for event in self._events.get(ctx.tenant_id, []):
            for source in (*event.inputs, *event.outputs):
                name = source["name"]
                nodes.setdefault(name, {
                    "id": name,
                    "type": "output" if source in event.outputs else "input",
                    "namespace": source.get("namespace", "pas"),
                    "runs": 0,
                })
                nodes[name]["runs"] += 1
            for source in event.inputs:
                for target in event.outputs:
                    edges[(source["name"], target["name"])] = {
                        "from": source["name"],
                        "to": target["name"],
                        "job": event.job.get("name"),
                        "runCount": edges.get((source["name"], target["name"]), {}).get("runCount", 0) + 1,
                    }
        return {
            "enabled": True,
            "nodes": sorted(nodes.values(), key=lambda n: n["id"]),
            "edges": sorted(edges.values(), key=lambda e: (e["from"], e["to"])),
            "eventCount": len(self._events.get(ctx.tenant_id, [])),
        }

    def impact(self, ctx: TenantContext, dataset: str) -> dict[str, Any]:
        """Which downstream datasets are affected when ``dataset`` is wrong."""
        graph = self.graph(ctx)
        affected: set[str] = set()
        frontier = {dataset}
        while frontier:
            node = frontier.pop()
            for edge in graph["edges"]:  # type: ignore[index]
                if edge["from"] == node and edge["to"] not in affected:
                    affected.add(edge["to"])
                    frontier.add(edge["to"])
        return {
            "dataset": dataset,
            "downstreamDatasets": sorted(affected),
            "impactCount": len(affected),
            "openLineageEvents": [
                e.to_json() for e in self._events.get(ctx.tenant_id, [])[-20:]
            ],
        }

    def events(self, ctx: TenantContext) -> list[dict[str, Any]]:
        return [e.to_json() for e in self._events.get(ctx.tenant_id, [])]


def quality_dimensions_summary(report: QualityReport) -> dict[str, Any]:
    """Per-dimension breakdown for the dashboard radar chart."""
    return {
        "dataset": report.dataset,
        "grade": report.grade,
        "overall": round(report.score, 4),
        "dimensions": [
            {
                "dimension": dimension,
                "score": round(score, 4),
                "grade": grade_for(score),
            }
            for dimension, score in sorted(report.dimensions.items())
        ],
        "recommendations": [
            {"ruleId": r.rule_id, "remediation": r.remediation or r.message}
            for r in report.failed_rules
        ],
    }


def pass_rate_for(results: Iterable[RuleResult]) -> float:
    rows = list(results)
    if not rows:
        return 1.0
    return sum(r.pass_rate for r in rows) / len(rows)


def confidence_score(ratio: float) -> float:
    """Map a pass ratio onto a 0-1 confidence for downstream consumers."""
    if ratio >= 0:
        return 1.0 - math.exp(-4.0 * ratio) if ratio < 1 else 1.0
    return 0.0
