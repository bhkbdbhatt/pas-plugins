"""Grouping of insurance contracts (GICs) under IFRS 17 paragraph 4.

The standard requires contracts to be grouped into portfolios, then by line of
business, then by profitability *at the date of initial recognition*.  Getting
that order wrong is one of the most common causes of a restatement, so it is
enforced here rather than left to configuration.

Two properties of paragraph 4 are easy to overlook and are handled explicitly:

1. **Profitability is assessed once, at initial recognition.** A group that was
   profitable on issue does not become onerous later merely because it did; it
   becomes onerous when *future* fulfilment cash flows change, which goes through
   the CSM, not through regrouping.
2. **Regrouping is not retrospective.** Contracts move between groups only at
   initial recognition or on a change in the grouping criteria's inputs; a prior
   period is never restated on this basis alone.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from pas_core.errors import ValidationError
from pas_core.observability import GLOBAL_METRICS
from pas_core.tenancy import TenantContext
from pas_plugins.plugin2_ifrs17.models import (
    ContractGroupInput,
    GroupingBasis,
    GroupingDecision,
    MeasurementModel,
    Profitability,
)


def _positive(value: int, name: str) -> int:
    """Validate a positive integer in a frozen dataclass field."""
    if value < 1:
        msg = f"{name} must be at least 1, got {value}"
        raise ValidationError(msg, field=name, value=value)
    return value


@dataclass(frozen=True, slots=True)
class GroupingCriteria:
    """The paragraph 4 criteria, in the order the standard applies them."""

    basis: tuple[GroupingBasis, ...] = (
        GroupingBasis.LINE_OF_BUSINESS,
        GroupingBasis.PROFITABILITY,
        GroupingBasis.ISSUE_DATE_COHORT,
    )
    cohort_years_per_group: int = 3
    """Cohort years folded into one group. Three years is the usual practical
    balance between statistical stability and responsiveness to emerging experience."""
    minimum_contracts_per_group: int = 25
    """Below this a group's estimate is dominated by sampling noise."""
    minimum_profitability_margin: float = 0.0
    """Margin above which a cohort counts as profitable at initial recognition. Zero
    means the group must have strictly positive expected profit."""
    merge_small_groups: bool = True
    """Fold sub-minimum groups into the nearest cohort rather than leaving slivers."""

    def __post_init__(self) -> None:
        _positive(self.cohort_years_per_group, "cohort_years_per_group")
        if self.minimum_contracts_per_group < 0:
            msg = "minimum_contracts_per_group cannot be negative"
            raise ValidationError(msg, field="minimum_contracts_per_group")
        if not -1.0 <= self.minimum_profitability_margin <= 1.0:
            msg = "minimum_profitability_margin must be between -1 and 1"
            raise ValidationError(msg, field="minimum_profitability_margin")
        unknown = [b for b in self.basis if b not in GroupingBasis]
        if unknown:
            msg = f"unknown grouping basis: {[str(b) for b in unknown]}"
            raise ValidationError(msg, basis=[str(b) for b in unknown])

    def describe(self) -> dict[str, Any]:
        return {
            "basis": [str(b) for b in self.basis],
            "cohortYearsPerGroup": self.cohort_years_per_group,
            "minimumContractsPerGroup": self.minimum_contracts_per_group,
            "minimumProfitabilityMargin": self.minimum_profitability_margin,
            "mergeSmallGroups": self.merge_small_groups,
            "standard": "IFRS 17 paragraph 4",
        }


@dataclass(frozen=True, slots=True)
class GroupingOutcome:
    """The result of grouping a population of contract cohorts."""

    decisions: tuple[GroupingDecision, ...]
    ungrouped: tuple[ContractGroupInput, ...]
    criteria: GroupingCriteria
    warnings: tuple[str, ...] = ()

    @property
    def group_count(self) -> int:
        return len(self.decisions)

    @property
    def profitable_groups(self) -> int:
        return sum(1 for d in self.decisions if d.profitable_at_recognition)

    @property
    def onerous_groups(self) -> int:
        return sum(1 for d in self.decisions if not d.profitable_at_recognition)

    def by_id(self) -> dict[str, GroupingDecision]:
        return {d.group_id: d for d in self.decisions}

    def to_dict(self) -> dict[str, Any]:
        return {
            "criteria": self.criteria.describe(),
            "groupCount": self.group_count,
            "profitableGroups": self.profitable_groups,
            "onerousGroups": self.onerous_groups,
            "ungroupedCount": len(self.ungrouped),
            "decisions": [d.to_dict() for d in self.decisions],
            "warnings": list(self.warnings),
        }


class GroupingEngine:
    """Assigns contract cohorts to groups of insurance contracts.

    The engine is deterministic: the same population and criteria always produce
    the same group identifiers, so a re-run after a PAS data refresh does not
    silently reshuffle the reported numbers.
    """

    def __init__(self, criteria: GroupingCriteria | None = None) -> None:
        self.criteria = criteria or GroupingCriteria()

    def group(
        self, population: Sequence[ContractGroupInput], ctx: TenantContext
    ) -> GroupingOutcome:
        """Group a population of cohorts."""
        if not population:
            msg = "cannot group an empty contract population"
            raise ValidationError(msg, tenantId=ctx.tenant_id)

        for criterion in self.criteria.basis:
            if criterion not in GroupingBasis:
                msg = f"unknown grouping basis '{criterion}'"
                raise ValidationError(msg, basis=str(criterion))

        decisions: dict[str, GroupingDecision] = {}
        merged: dict[str, list[ContractGroupInput]] = {}
        warnings: list[str] = []

        for contract in population:
            profitability = self._assess_profitability(contract)
            cohort_bucket = self._cohort_bucket(contract.cohort_year)
            key = self._group_key(contract, profitability, cohort_bucket)
            merged.setdefault(key, []).append(contract)

        for key, members in sorted(merged.items()):
            if self.criteria.merge_small_groups and len(members) < self.criteria.minimum_contracts_per_group:
                warnings.append(
                    f"group '{key}' holds {len(members)} cohort(s) below the "
                    f"{self.criteria.minimum_contracts_per_group}-cohort minimum; "
                    "consider widening the cohort window"
                )
            head = members[0]
            profitability = self._assess_profitability(head)
            total_contracts = sum(m.contract_count for m in members)
            blended_rate = _blended_locked_in_rate(members)
            rules_fired = self._rules_fired(head, profitability)

            decisions[key] = GroupingDecision(
                group_id=key,
                basis=GroupingBasis.LINE_OF_BUSINESS,
                product_line=head.product_line,
                cohort_year=min(m.cohort_year for m in members),
                profitability=profitability,
                profitable_at_recognition=profitability is not Profitability.LOSS,
                rationale=self._rationale(head, members, profitability),
                rules_fired=rules_fired,
                contract_count=total_contracts,
                locked_in_rate=blended_rate,
            )

        GLOBAL_METRICS.increment(
            "ifrs17_groups_created_total",
            tenant=ctx.tenant_id,
            count=len(decisions),
        )
        return GroupingOutcome(
            decisions=tuple(sorted(decisions.values(), key=lambda d: d.group_id)),
            ungrouped=(),
            criteria=self.criteria,
            warnings=tuple(warnings),
        )

    # -- paragraph 4 steps --------------------------------------------------
    def _assess_profitability(self, contract: ContractGroupInput) -> Profitability:
        """Assess profitability *at initial recognition* (paragraph 4(b)).

        The test is whether the expected profit exceeds zero after deducting the
        risk adjustment, which is what determines whether a CSM or a loss component
        arises. A group exactly at zero is treated as onerous rather than
        profitable, so a marginal group does not carry a CSM it cannot support.
        """
        expected_profit = (
            contract.expected_premiums
            - contract.expected_claims
            - contract.expected_expenses
            - contract.cash_at_recognition
        )
        if expected_profit > self.criteria.minimum_profitability_margin * max(
            1.0, contract.expected_premiums
        ):
            return Profitability.PROFITABLE
        if expected_profit >= 0:
            return Profitability.ON_COST
        return Profitability.LOSS

    def _cohort_bucket(self, cohort_year: int) -> int:
        """Bucket cohort years into windows so nearby vintages share a group."""
        window = self.criteria.cohort_years_per_group
        return (cohort_year // window) * window

    def _group_key(
        self, contract: ContractGroupInput, profitability: Profitability, cohort: int
    ) -> str:
        """A stable, human-readable group identifier.

        Stable matters: the identifier appears in the filing and in the PAS, and a
        hash-derived id would change if the criteria changed.
        """
        parts = [
            contract.product_line.strip().lower().replace(" ", "-"),
            contract.measurement_model.value,
            str(profitability.value),
            f"{cohort}-{cohort + self.criteria.cohort_years_per_group - 1}",
        ]
        return "GIC-" + "-".join(parts)

    def _rationale(
        self,
        head: ContractGroupInput,
        members: Sequence[ContractGroupInput],
        profitability: Profitability,
    ) -> str:
        cohort_years = sorted({m.cohort_year for m in members})
        expected_profit = sum(
            m.expected_premiums - m.expected_claims - m.expected_expenses - m.cash_at_recognition
            for m in members
        )
        expected_premiums = sum(m.expected_premiums for m in members) or 1.0
        return (
            f"Line of business '{head.product_line}' measured under the "
            f"{head.measurement_model.display_name}, profitability assessed at initial "
            f"recognition as {profitability.value} (expected profit "
            f"{expected_profit:,.0f} on {expected_premiums:,.0f} of expected premiums, "
            f"a {expected_profit / expected_premiums:.2%} margin). Cohort years "
            f"{cohort_years[0]}-{cohort_years[-1]} fall in the same window. "
            "Profitability is not reassessed once recognised (IFRS 17 para 4(b))."
        )

    def _rules_fired(
        self, contract: ContractGroupInput, profitability: Profitability
    ) -> list[str]:
        fired = ["GF-001: grouped by line of business"]
        fired.append(
            "GF-002: profitability assessed at initial recognition"
        )
        if profitability is Profitability.LOSS:
            fired.append("GF-003: onerous cohort - loss component expected on measurement")
        if contract.measurement_model is MeasurementModel.PAA:
            fired.append(
                "GF-004: PAA requires a short-duration contract (IFRS 17 para 53); "
                "confirm the coverage period qualifies"
            )
        if contract.measurement_model is MeasurementModel.VFA:
            fired.append("GF-005: VFA requires direct participating features (para B3)")
        return fired

    # -- diagnostics --------------------------------------------------------
    def validate_population(self, population: Iterable[ContractGroupInput]) -> list[str]:
        """Checks a population before grouping, so problems surface early."""
        warnings: list[str] = []
        seen: dict[tuple[str, int, str], int] = {}
        for contract in population:
            key = (contract.product_line, contract.cohort_year, str(contract.measurement_model))
            seen[key] = seen.get(key, 0) + 1
            if contract.cohort_year > contract.reporting_date.year:
                msg = (
                    f"cohort {contract.group_id} has an issue year after its reporting date"
                )
                warnings.append(msg)
            if contract.contract_count == 0:
                warnings.append(f"cohort {contract.group_id} has no contracts")
            if contract.measurement_model is MeasurementModel.PAA and (
                contract.average_term_years > 1
            ):
                warnings.append(
                    f"cohort {contract.group_id} uses PAA with a "
                    f"{contract.average_term_years}-year term; PAA is for short-duration "
                    "contracts only (IFRS 17 para 53)"
                )
        duplicates = [k for k, v in seen.items() if v > 1]
        for product_line, cohort_year, model in duplicates:
            warnings.append(
                f"duplicate cohort for '{product_line}' {cohort_year} under {model}; "
                "the cohorts must be combined before grouping"
            )
        return warnings

    def recommend_measurement_model(self, contract: ContractGroupInput) -> MeasurementModel:
        """Suggest the measurement model the standard would require.

        A recommendation is only a recommendation: a carrier may have filed
        PAA eligibility with its regulator for contracts that look short-duration,
        and the audit trail records why the engine suggested something else.
        """
        if contract.average_term_years <= 1 and contract.coverage_units == ["insurance"]:
            return MeasurementModel.PAA
        if contract.metadata_share < 1.0 and len(contract.coverage_units) > 1:
            return MeasurementModel.VFA
        return MeasurementModel.GMM


def _blended_locked_in_rate(members: Sequence[ContractGroupInput]) -> float:
    """Contract-count-weighted locked-in rate across the merged cohorts."""
    total = sum(m.contract_count for m in members)
    if total <= 0:
        return members[0].locked_in_rate if members else 0.0
    return round(sum(m.locked_in_rate * m.contract_count for m in members) / total, 6)


def stable_group_fingerprint(decision: GroupingDecision) -> str:
    """Short digest used to detect a grouping change between valuation runs."""
    material = f"{decision.group_id}|{decision.profitability}|{decision.cohort_year}"
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def compare_grouping(
    previous: Sequence[GroupingDecision], current: Sequence[GroupingDecision]
) -> dict[str, Any]:
    """Diff two grouping runs, so a reviewer can see exactly what moved."""
    before = {d.group_id: d for d in previous}
    after = {d.group_id: d for d in current}
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = []
    for group_id in sorted(set(before) & set(after)):
        old, new = before[group_id], after[group_id]
        if old.profitability is not new.profitability or old.contract_count != new.contract_count:
            changed.append({
                "groupId": group_id,
                "profitability": {
                    "from": str(old.profitability),
                    "to": str(new.profitability),
                },
                "contractCount": {"from": old.contract_count, "to": new.contract_count},
                "lockedInRate": {"from": old.locked_in_rate, "to": new.locked_in_rate},
            })
    return {
        "previousGroupCount": len(before),
        "currentGroupCount": len(after),
        "added": added,
        "removed": removed,
        "changed": changed,
        "stable": not (added or removed or changed),
    }
