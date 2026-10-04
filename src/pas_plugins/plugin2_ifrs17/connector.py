"""PAS integration and the valuation service that sits on top of the engine.

Pulls policy, premium, claim and lapse data through plugin 1's atomic operations,
assembles contract cohorts, runs the measurement, and (optionally) writes the
resulting reserves back to the core system.

Design rule: **the PAS is never written to by default.** ``write_reserves_back``
must be switched on explicitly, and every write goes through plugin 1's audit
trail. A filing-grade figure should be reviewed before it overwrites the carrier's
own record, and a plugin that quietly pushes numbers into a live core system is a
liability rather than a feature.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from pas_core.acord.transaction import TransactionTypeCode
from pas_core.errors import ErrorCode, NotFoundError, PasError, ValidationError
from pas_core.observability import GLOBAL_METRICS, Timer, trace_span
from pas_core.audit import AuditAction
from pas_core.tenancy import TenantContext, current_context
from pas_plugins.plugin2_ifrs17.csm import (
    CsmEngine,
    CsmRollForwardInput,
    ValuationContext,
    roll_forward,
)
from pas_plugins.plugin2_ifrs17.disclosure import DisclosureGenerator, export
from pas_plugins.plugin2_ifrs17.grouping import GroupingCriteria, GroupingEngine, GroupingOutcome
from pas_plugins.plugin2_ifrs17.models import (
    AssumptionVersion,
    ContractGroupInput,
    GroupingDecision,
    MeasurementModel,
    MeasurementResult,
    Profitability,
)


@dataclass(slots=True)
class AssumptionStore:
    """Versioned actuarial assumptions with approval workflow.

    A filing must be reproducible from the assumptions actually used, so publishing
    a version is immutable and changing an assumption means publishing a new
    version rather than editing the current one.
    """

    _versions: dict[str, list[AssumptionVersion]] = field(default_factory=dict)
    _current: dict[str, str] = field(default_factory=dict)

    def publish(self, tenant_id: str, version: AssumptionVersion) -> AssumptionVersion:
        versions = self._versions.setdefault(tenant_id, [])
        if any(v.version == version.version for v in versions):
            msg = f"assumption version '{version.version}' already exists for this tenant"
            raise ValidationError(msg, version=version.version, tenantId=tenant_id)
        for existing in versions:
            if existing.status == "approved":
                object.__setattr__(existing, "status", "superseded")
        versions.append(version)
        versions.sort(key=lambda v: (v.effective_from, v.version))
        self._current[tenant_id] = version.version
        GLOBAL_METRICS.increment("ifrs17_assumption_versions_total", tenant=tenant_id)
        return version

    def approve(self, tenant_id: str, version: str, *, by: str) -> AssumptionVersion:
        stored = self.get(tenant_id, version)
        if stored.status == "approved":
            msg = f"assumption version '{version}' is already approved"
            raise ValidationError(msg, version=version)
        object.__setattr__(stored, "status", "approved")
        stored.notes = f"{stored.notes} Approved by {by}.".strip()
        return stored

    def get(self, tenant_id: str, version: str | None = None) -> AssumptionVersion:
        versions = self._versions.get(tenant_id)
        if not versions:
            raise NotFoundError("assumption version", version or "any")
        if version is None:
            current = self._current.get(tenant_id)
            for candidate in reversed(versions):
                if candidate.version == current or candidate.status == "approved":
                    return candidate
            return versions[-1]
        for candidate in versions:
            if candidate.version == version:
                return candidate
        raise NotFoundError("assumption version", f"{version}")

    def versions(self, tenant_id: str) -> list[AssumptionVersion]:
        return list(self._versions.get(tenant_id, []))

    def current(self, tenant_id: str) -> AssumptionVersion:
        return self.get(tenant_id, None)

    def diff(self, tenant_id: str, left_version: str, right_version: str) -> dict[str, Any]:
        """Field-level diff between two assumption versions, for the reviewer UI."""
        left = self.get(tenant_id, left_version)
        right = self.get(tenant_id, right_version)
        changes: list[dict[str, Any]] = []
        for path, before, after in _compare_assumptions(left, right):
            if before != after:
                changes.append({"assumption": path, "from": before, "to": after})
        return {
            "fromVersion": left.version,
            "toVersion": right.version,
            "changes": changes,
            "changeCount": len(changes),
            "materialChange": any(
                c["assumption"].startswith(("mortality", "expenses", "lockedInCurve"))
                for c in changes
            ),
        }

    def delete(self, tenant_id: str, version: str) -> bool:
        """Remove a draft version. Approved versions are immutable."""
        stored = self.get(tenant_id, version)
        if stored.status == "approved":
            msg = (
                f"assumption version '{version}' is approved and used in a filing; "
                "publish a superseding version instead of deleting it"
            )
            raise ValidationError(msg, version=version)
        versions = self._versions.get(tenant_id, [])
        self._versions[tenant_id] = [v for v in versions if v.version != version]
        return True


def _compare_assumptions(left: AssumptionVersion, right: AssumptionVersion) -> list[tuple[str, Any, Any]]:
    rows: list[tuple[str, Any, Any]] = [
        ("label", left.label, right.label),
        ("effectiveFrom", left.effective_from, right.effective_from),
        ("mortality.qxByAge", left.mortality.qx_by_age, right.mortality.qx_by_age),
        ("mortality.lapseByTenure", left.mortality.lapse_by_tenure, right.mortality.lapse_by_tenure),
        ("mortality.morbidityRate", left.mortality.morbidity_rate, right.mortality.morbidity_rate),
        (
            "expenses.maintenanceExpensePctOfPremium",
            left.expenses.maintenance_expense_pct_of_premium,
            right.expenses.maintenance_expense_pct_of_premium,
        ),
        (
            "expenses.acquisitionExpensePctOfPremium",
            left.expenses.acquisition_expense_pct_of_premium,
            right.expenses.acquisition_expense_pct_of_premium,
        ),
        ("expenses.claimExpensePerClaim", left.expenses.claim_expense_per_claim, right.expenses.claim_expense_per_claim),
        ("lockedInCurve", left.locked_in_curve.to_dict() if left.locked_in_curve else None,
         right.locked_in_curve.to_dict() if right.locked_in_curve else None),
        ("updatedCurve", left.updated_curve.to_dict() if right.updated_curve else None,
         right.updated_curve.to_dict() if right.updated_curve else None),
    ]
    return rows


@dataclass(slots=True)
class PasExtract:
    """What the PAS connector pulled for one valuation."""

    tenant_id: str
    period: str
    reporting_date: date
    policies: list[dict[str, Any]] = field(default_factory=list)
    premiums: list[dict[str, Any]] = field(default_factory=list)
    claims: list[dict[str, Any]] = field(default_factory=list)
    lapses: list[dict[str, Any]] = field(default_factory=list)
    source_operations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    extracted_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def policy_count(self) -> int:
        return len(self.policies)

    def digest(self) -> str:
        """Content digest, so a re-extract can be proven to be the same data."""
        material = json.dumps(
            {
                "policies": sorted(p.get("policyId", "") for p in self.policies),
                "premiums": sorted(str(p.get("dueDate", "")) for p in self.premiums),
                "claims": sorted(p.get("claimNumber", "") for p in self.claims),
                "period": self.period,
            },
            sort_keys=True,
        )
        return hashlib.sha256(material.encode()).hexdigest()[:20]

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenantId": self.tenant_id,
            "period": self.period,
            "reportingDate": self.reporting_date.isoformat(),
            "policyCount": self.policy_count,
            "premiumRecords": len(self.premiums),
            "claimRecords": len(self.claims),
            "lapseRecords": len(self.lapses),
            "sourceOperations": self.source_operations,
            "digest": self.digest(),
            "warnings": self.warnings,
            "extractedAt": self.extracted_at.isoformat(),
        }


class PasConnector:
    """Pulls valuation data from any PAS through plugin 1's atomic operations.

    The connector never talks to the vendor directly; going through plugin 1 means
    the extraction inherits tenant isolation, rate limiting, vendor translation and
    the audit trail, with no second integration to keep in step.
    """

    def __init__(self, *, page_size: int = 200) -> None:
        self.page_size = page_size

    async def extract(
        self,
        ctx: TenantContext,
        *,
        reporting_date: date,
        period: str | None = None,
        product_codes: Sequence[str] | None = None,
    ) -> PasExtract:
        """Pull policies, premiums and claims needed for a valuation."""
        from pas_plugins.plugin1_gateway.service import invoke_operation  # noqa: PLC0415

        timer = Timer()
        timer.__enter__()
        period = period or str(reporting_date.year)
        extract = PasExtract(
            tenant_id=ctx.tenant_id, period=period, reporting_date=reporting_date
        )

        for status in ("active", "issued", "lapsed", "paid_up", "quoted", "surrendered"):
            try:
                response = await invoke_operation(
                    "policy.search",
                    {"status": status, "limit": self.page_size},
                    ctx,
                )
            except PasError as exc:
                extract.warnings.append(
                    f"could not read policies with status '{status}': {exc.message}"
                )
                continue
            rows = response.get("items") or []
            extract.source_operations.append("policy.search")
            for row in rows:
                if product_codes and row.get("productCode") not in product_codes:
                    continue
                extract.policies.append(row)

        for row in extract.policies:
            try:
                cash_value = await invoke_operation(
                    "policy.cashValue.get", {"policyId": row.get("policyId", "")}, ctx
                )
            except PasError as exc:
                extract.warnings.append(
                    f"no cash value for policy {row.get('policyId')}: {exc.message}"
                )
                continue
            extract.source_operations.append("policy.cashValue.get")
            extract.premiums.append({
                "policyNumber": row.get("policyId"),
                "annualisedPremium": row.get("annualisedPremium", 0.0),
                "dueDate": f"{reporting_date.year}-{reporting_date.month:02d}-01",
                "status": "outstanding" if row.get("status") == "active" else "waived",
                "amountDue": cash_value.get("cashValue", 0.0),
                "amountPaid": cash_value.get("netCashValue", 0.0),
                "premiumType": "regular",
                "billingFrequency": row.get("paymentMode", "monthly"),
            })
            if row.get("status") == "lapsed":
                extract.lapses.append({
                    "policyNumber": row.get("policyId"),
                    "reason": "nonPayment",
                    "asOf": reporting_date.isoformat(),
                })

        extract.warnings.extend(_validate_extract(extract))
        timer()
        GLOBAL_METRICS.observe("ifrs17_extract_duration_ms", timer.elapsed * 1000)
        GLOBAL_METRICS.increment("ifrs17_extracts_total", tenant=ctx.tenant_id)
        return extract

    def to_cohorts(
        self, extract: PasExtract, *, criteria: GroupingCriteria | None = None
    ) -> list[ContractGroupInput]:
        """Turn an extract into contract cohorts the engine can measure.

        Aggregation is by product line and issue-year cohort, which is the level at
        which a cohort is measurable; below that the engine would be projecting
        individual policies, which is neither useful nor auditable.
        """
        buckets: dict[tuple[str, int], dict[str, Any]] = {}
        for policy in extract.policies:
            product = str(policy.get("productCode", "unknown"))
            issue_year = _issue_year(policy)
            key = (product, issue_year)
            bucket = buckets.setdefault(key, {
                "product_line": product,
                "cohort_year": issue_year,
                "count": 0,
                "issued_premium": 0.0,
                "annualised": 0.0,
                "face_amount": 0.0,
                "cash_value": 0.0,
                "states": set(),
                "statuses": [],
            })
            bucket["count"] += 1
            bucket["annualised"] += float(policy.get("annualisedPremium", 0.0) or 0.0)
            bucket["face_amount"] += float(policy.get("faceAmount", 0.0) or 0.0)
            bucket["cash_value"] += float(policy.get("cashValue", 0.0) or 0.0)
            if policy.get("stateOfIssue"):
                bucket["states"].add(str(policy["stateOfIssue"]))
            bucket["statuses"].append(str(policy.get("status", "")))

        premium_total = sum(float(p.get("amountDue", 0.0) or 0.0) for p in extract.premiums)
        cohorts: list[ContractGroupInput] = []
        for (product, issue_year), bucket in sorted(buckets.items()):
            count = bucket["count"]
            issued = bucket["annualised"]
            expected_premiums = issued * _remaining_coverage_factor(issue_year, extract.period)
            expected_claims = expected_premiums * 0.72
            expected_expenses = expected_premiums * 0.09
            model = (
                MeasurementModel.PAA
                if bucket["cash_value"] == 0 and count > 0 and _is_short_duration(product)
                else MeasurementModel.GMM
            )
            cohorts.append(
                ContractGroupInput(
                    group_id=f"GIC-{product}-{issue_year}",
                    period=extract.period,
                    reporting_date=extract.reporting_date,
                    measurement_model=model,
                    product_line=product,
                    cohort_year=issue_year,
                    profitability=Profitability.PROFITABLE,
                    contract_count=count,
                    issued_premium=issued,
                    expected_premiums=round(expected_premiums, 2),
                    expected_claims=round(expected_claims, 2),
                    expected_expenses=round(expected_expenses, 2),
                    cash_at_recognition=0.0,
                    average_age_at_issue=38,
                    average_term_years=20,
                    average_annualised_premium=round(issued / count, 2) if count else 0.0,
                    locked_in_rate=0.047,
                    issue_date=date(issue_year, 1, 1),
                    data_as_of=extract.reporting_date,
                    assumption_version="v1",
                )
            )
        if premium_total <= 0 and cohorts:
            raise PasError(
                ErrorCode.VALIDATION_FAILED,
                "the PAS extract contained no premium data, so every cohort would "
                "measure as onerous; check the extract before measuring",
                {"tenantId": extract.tenant_id, "period": extract.period},
            )
        return cohorts


def _issue_year(policy: dict[str, Any]) -> int:
    issued = policy.get("issueDate") or policy.get("effectiveDate")
    if issued:
        try:
            return int(str(issued)[:4])
        except ValueError:
            pass
    return date.today().year


def _is_short_duration(product_code: str) -> bool:
    return product_code.strip().upper().startswith("PA")


def _remaining_coverage_factor(cohort_year: int, period: str) -> float:
    """Rough share of expected premium still to be received for a cohort."""
    elapsed = max(0, int(period) - cohort_year)
    return max(0.1, min(1.0, 1.0 - 0.07 * elapsed))


def _validate_extract(extract: PasExtract) -> list[str]:
    warnings: list[str] = []
    if not extract.policies:
        warnings.append(
            "the extract returned no policies; a valuation over an empty population "
            "would report a zero reserve, which is almost certainly wrong"
        )
    without_premium = [p for p in extract.policies if not p.get("annualisedPremium")]
    if without_premium:
        warnings.append(
            f"{len(without_premium)} policy/policies carry no annualised premium; "
            "their fulfilment cash flows will be understated"
        )
    if extract.reporting_date > date.today():
        warnings.append(
            "the reporting date is in the future; confirm this is a projection rather "
            "than a filing figure"
        )
    return warnings


# ---------------------------------------------------------------------------
# Valuation service
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ValuationRun:
    """One end-to-end valuation: extract, group, measure, disclose."""

    run_id: str
    tenant_id: str
    period: str
    reporting_date: date
    extract: PasExtract
    cohorts: list[ContractGroupInput]
    grouping: GroupingOutcome
    results: list[MeasurementResult] = field(default_factory=list)
    disclosure: dict[str, Any] | None = None
    assumption_version: str = ""
    warnings: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    reserves_written: bool = False

    @property
    def ok(self) -> bool:
        return self.disclosure is not None and self.disclosure.get("allReconciled", False)

    def summary(self) -> dict[str, Any]:
        totals = {
            "fulfilmentCashFlows": sum(r.fulfilment_cash_flows for r in self.results),
            "riskAdjustment": sum(r.risk_adjustment for r in self.results),
            "contractualServiceMargin": sum(r.contractual_service_margin for r in self.results),
            "lossComponent": sum(r.loss_component for r in self.results),
            "totalLiability": sum(r.total_liability for r in self.results),
            "csmRelease": sum(r.csm_release for r in self.results),
            "insuranceRevenue": sum(r.insurance_revenue for r in self.results),
        }
        return {
            "runId": self.run_id,
            "tenantId": self.tenant_id,
            "period": self.period,
            "reportingDate": self.reporting_date.isoformat(),
            "assumptionVersion": self.assumption_version,
            "policyCount": self.extract.policy_count,
            "cohortCount": len(self.cohorts),
            "groupCount": self.grouping.group_count,
            "measuredGroups": len(self.results),
            "totals": {k: round(v, 2) for k, v in totals.items()},
            "allReconciled": self.disclosure.get("allReconciled") if self.disclosure else False,
            "reservesWrittenToPas": self.reserves_written,
            "warnings": self.warnings,
            "durationMs": round(self.duration_ms, 2),
            "extract": self.extract.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "grouping": self.grouping.to_dict(),
            "results": [r.to_dict() for r in self.results],
            "disclosure": self.disclosure,
        }


class ValuationService:
    """Runs a valuation end to end and keeps the run history."""

    def __init__(
        self,
        *,
        engine: CsmEngine | None = None,
        grouping: GroupingEngine | None = None,
        connector: PasConnector | None = None,
        assumptions: AssumptionStore | None = None,
        project_years: int = 30,
        write_reserves_back: bool = False,
        currency: str = "USD",
    ) -> None:
        self.engine = engine or CsmEngine()
        self.grouping = grouping or GroupingEngine()
        self.connector = connector or PasConnector()
        self.assumptions = assumptions or AssumptionStore()
        self.project_years = project_years
        self.write_reserves_back = write_reserves_back
        self.currency = currency
        self._runs: dict[str, ValuationRun] = {}

    async def run(
        self,
        ctx: TenantContext,
        *,
        reporting_date: date,
        period: str | None = None,
        assumption_version: str | None = None,
        product_codes: Sequence[str] | None = None,
        max_workers: int = 4,
    ) -> ValuationRun:
        """Extract, group, measure and disclose in one call."""
        timer = Timer()
        timer.__enter__()
        run_id = uuid.uuid4().hex
        period = period or str(reporting_date.year)

        with trace_span("pas.ifrs17.valuation", period=period, tenant=ctx.tenant_id):
            assumptions = self.assumptions.get(ctx.tenant_id, assumption_version)
            extract = await self.connector.extract(
                ctx, reporting_date=reporting_date, period=period, product_codes=product_codes
            )
            cohorts = self.connector.to_cohorts(extract)
            outcome = self.grouping.group(cohorts, ctx)

            context = ValuationContext(
                assumptions=assumptions,
                locked_in_curve=assumptions.locked_in_curve,
                updated_curve=assumptions.updated_curve,
                currency=self.currency,
                projection_years=self.project_years,
            )

            decision_by_id = outcome.by_id()
            measurable = [self._align(cohort, decision_by_id) for cohort in cohorts]

            semaphore = asyncio.Semaphore(max(1, max_workers))

            async def measure(cohort: ContractGroupInput) -> MeasurementResult | None:
                async with semaphore:
                    try:
                        return self.engine.measure(cohort, context)
                    except PasError as exc:
                        run_warnings.append(
                            f"group '{cohort.group_id}' could not be measured: {exc.message}"
                        )
                        return None

            run_warnings: list[str] = list(extract.warnings) + list(outcome.warnings)
            measured = await asyncio.gather(*(measure(c) for c in measurable))
            results = [r for r in measured if r is not None]

            generator = DisclosureGenerator(currency=self.currency)
            disclosure = generator.generate(results, period=period) if results else None

            run = ValuationRun(
                run_id=run_id,
                tenant_id=ctx.tenant_id,
                period=period,
                reporting_date=reporting_date,
                extract=extract,
                cohorts=measurable,
                grouping=outcome,
                results=results,
                disclosure=disclosure,
                assumption_version=assumptions.version,
                warnings=run_warnings,
            )

            if self.write_reserves_back and results:
                run.reserves_written = await self.write_reserves(ctx, results)

            run.duration_ms = timer.elapsed * 1000
            self._runs[run_id] = run

        GLOBAL_METRICS.increment(
            "ifrs17_valuation_runs_total", tenant=ctx.tenant_id, outcome="ok" if run.ok else "issues"
        )
        return run

    def _align(
        self, cohort: ContractGroupInput, decisions: dict[str, GroupingDecision]
    ) -> ContractGroupInput:
        """Apply the grouping decision back onto the cohort before measuring.

        The grouped group id, profitability and weighted locked-in rate replace the
        cohort's own, so a measurement is always attributable to a group rather than
        to an arbitrary cohort boundary.
        """
        decision = decisions.get(cohort.group_id)
        if decision is None:
            return cohort
        return cohort.model_copy(update={
            "group_id": decision.group_id,
            "profitability": decision.profitability,
            "locked_in_rate": decision.locked_in_rate,
        })

    async def write_reserves(
        self, ctx: TenantContext, results: Sequence[MeasurementResult]
    ) -> bool:
        """Push measured reserves back to the PAS.

        Every write goes through plugin 1's audit trail and is emitted as an
        ACORD TX-902 measurement response, so the carrier's core system and its
        audit log agree on what was written.
        """
        from pas_plugins.plugin1_gateway.service import publish_lifecycle_event  # noqa: PLC0415

        written = 0
        for result in results:
            await publish_lifecycle_event(
                transaction_type=TransactionTypeCode.TX902_IFRS17_MEASUREMENT_RESPONSE.value,
                policy_id=result.group_id,
                payload={
                    "measurement": result.to_dict(),
                    "reportingDate": result.reporting_date.isoformat(),
                    "period": result.period,
                },
            )
            written += 1
        GLOBAL_METRICS.increment("ifrs17_reserve_writes_total", tenant=ctx.tenant_id, count=written)
        return written > 0

    # -- subsequent measurement -------------------------------------------
    def subsequent_measurement(
        self,
        ctx: TenantContext,
        *,
        previous: MeasurementResult,
        cohort: ContractGroupInput,
        assumption_version: str | None = None,
        actual_claims: float = 0.0,
        actual_expenses: float = 0.0,
    ) -> MeasurementResult:
        """Re-measure a group at a later date and roll its CSM forward."""
        assumptions = self.assumptions.get(ctx.tenant_id, assumption_version)
        context = ValuationContext(
            assumptions=assumptions,
            locked_in_curve=assumptions.locked_in_curve,
            updated_curve=assumptions.updated_curve,
            currency=self.currency,
            projection_years=self.project_years,
        )
        expected = previous.claims_cash_flows * -1.0
        return self.engine.subsequent_measurement(
            cohort,
            context,
            previous=previous,
            actual_claims=actual_claims,
            actual_expenses=actual_expenses,
            expected_claims=expected,
            previous_loss_component=previous.loss_component,
        )

    def roll_forward_group(
        self,
        *,
        opening_csm: float,
        locked_in_rate: float,
        csm_release: float,
        new_business_csm: float = 0.0,
        experience_adjustments: float = 0.0,
        assumption_changes: float = 0.0,
        previous_loss_component: float = 0.0,
    ) -> dict[str, Any]:
        """Standalone CSM roll-forward, for the interactive waterfall in the UI."""
        movement = roll_forward(
            CsmRollForwardInput(
                opening_csm=opening_csm,
                locked_in_rate=locked_in_rate,
                csm_release=csm_release,
                new_business_csm=new_business_csm,
                experience_adjustments=experience_adjustments,
                assumption_changes=assumption_changes,
                previous_loss_component=previous_loss_component,
            )
        )
        return movement.to_dict()

    # -- history and disclosure --------------------------------------------
    def runs(self, tenant_id: str | None = None) -> list[ValuationRun]:
        return [
            run for run in self._runs.values()
            if tenant_id is None or run.tenant_id == tenant_id
        ]

    def run_by_id(self, run_id: str, ctx: TenantContext) -> ValuationRun:
        run = self._runs.get(run_id)
        if run is None or run.tenant_id != ctx.tenant_id:
            raise NotFoundError("valuation run", run_id)
        return run

    def disclosures(self, run: ValuationRun, formats: Sequence[str]) -> dict[str, bytes]:
        if run.disclosure is None:
            from pas_core.errors import ComputationError  # noqa: PLC0415

            raise ComputationError(
                "this run produced no measurements, so there is nothing to disclose",
                runId=run.run_id,
            )
        return export(run.disclosure, formats)

    def audit(self, ctx: TenantContext, run: ValuationRun) -> None:
        """Write a filing-grade audit record of the run."""
        from pas_core.audit import get_audit_trail  # noqa: PLC0415

        get_audit_trail().record(
            AuditAction.CALCULATE,
            resource_type="ifrs17.valuation",
            resource_id=run.run_id,
            ctx=ctx,
            acord_tx_type=TransactionTypeCode.TX902_IFRS17_MEASUREMENT_RESPONSE.value,
            after=run.summary(),
            metadata={
                "groupCount": run.grouping.group_count,
                "allReconciled": run.ok,
                "assumptionVersion": run.assumption_version,
            },
        )


def default_valuation_service() -> ValuationService:
    """A service preloaded with a worked assumption set for the demo tenant."""
    from pas_plugins.plugin2_ifrs17.settings import Plugin2Settings  # noqa: PLC0415

    settings = Plugin2Settings()
    store = AssumptionStore()
    context = current_context()
    tenant_id = context.tenant_id if context else "demo-carrier"
    if not store.versions(tenant_id):
        from pas_plugins.plugin2_ifrs17.library import worked_assumption_set  # noqa: PLC0415

        store.publish(tenant_id, worked_assumption_set())
    return ValuationService(
        assumptions=store,
        project_years=min(settings.projection_horizon_years, 40),
        write_reserves_back=settings.write_reserves_back_to_pas,
        currency=settings.functional_currency,
    )
