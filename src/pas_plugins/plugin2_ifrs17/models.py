"""Domain models for the IFRS 17 engine.

The vocabulary follows IFRS 17 itself rather than a house style, because an
actuary reviewing a filing needs the same words the standard uses:

* **Fulfilment cash flows** - the cash flows expected to arise from the remaining
  service, split into premium cash flows (asset) and claims and expenses (liability)
* **Contractual service margin (CSM)** - the asset or liability representing the
  unearned profit, released to profit or loss as service is provided
* **Risk adjustment** - the compensation for the uncertainty inherent in the cash
  flows, released as uncertainty is removed
* **Loss component** - the CSM becomes a loss component when fulfilment cash
  flows exceed the carrying amount of the asset group
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pas_core.pas.vendors import to_camel


class MeasurementModel(StrEnum):
    """The three measurement models in IFRS 17."""

    GMM = "gmm"
    """General Measurement Model - the default for everything except short-duration
    contracts and direct participating contracts."""
    VFA = "vfa"
    """Variable Fee Approach - for contracts with direct participating features,
    where the entity's share of the underlying items drives the fee."""
    PAA = "paa"
    """Premium Allocation Approach - simplified for short-duration contracts
    (typically under one year)."""

    @property
    def display_name(self) -> str:
        return {
            "gmm": "General Measurement Model",
            "vfa": "Variable Fee Approach",
            "paa": "Premium Allocation Approach",
        }[str(self.value)]

    @property
    def requires_csm(self) -> bool:
        """PAA does not compute a CSM; it allocates premiums to services received."""
        return self is not MeasurementModel.PAA

    @property
    def requires_risk_adjustment(self) -> bool:
        return self is not MeasurementModel.PAA


class ProfitOrLossComponent(StrEnum):
    """IFRS 17 disaggregated profit-or-loss presentation lines."""

    INSURANCE_REVENUE = "insuranceRevenue"
    INSURANCE_SERVICE_EXPENSE = "insuranceServiceExpense"
    INSURANCE_SERVICE_RESULT = "insuranceServiceResult"
    INSURANCE_FINANCE_INCOME_EXPENSE = "insuranceFinanceIncomeExpense"
    INSURANCE_FINANCE_RESULT = "insuranceFinanceResult"
    RISK_ADJUSTMENT_RELEASE = "riskAdjustmentRelease"
    CSM_RELEASE = "csmRelease"
    LOSS_COMPONENT_REVERSAL = "lossComponentReversal"


class CoverageUnit(StrEnum):
    """Distinct services within a group, which is what service is provided against.

    Getting this wrong is the single most common cause of a restatement, because
    CSM allocation follows it.
    """

    INSURANCE = "insurance"
    INVESTMENT_RIDING = "investmentRiding"
    INVESTMENT_RETURN = "investmentReturn"
    PROTECTION = "protection"
    ANNUITY = "annuity"


class Profitability(StrEnum):
    """IFRS 17 paragraph 4 profitability assessment used for grouping."""

    PROFITABLE = "profitable"
    ON_COST = "onCost"
    LOSS = "loss"


class GroupingBasis(StrEnum):
    """IFRS 17 paragraph 4 grouping criteria, in the order they are applied."""

    PORTFOLIO = "portfolio"
    LINE_OF_BUSINESS = "lineOfBusiness"
    PROFITABILITY = "profitability"
    ISSUE_DATE_COHORT = "issueDateCohort"

    @property
    def rank(self) -> int:
        return {
            "lineOfBusiness": 0,
            "portfolio": 1,
            "issueDateCohort": 2,
            "profitability": 3,
        }[str(self.value)]


class Model(BaseModel):
    """Base for every IFRS 17 model.

    Fields are declared in snake_case and exposed in camelCase, matching the rest
    of the suite's wire format, with population by field name still allowed so
    internal call sites stay readable. One convention everywhere is worth more
    than the tidiness of the Python attribute names.
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        alias_generator=to_camel,
        populate_by_name=True,
    )


class MortalityAssumptions(Model):
    """Mortality, morbidity and lapse assumptions by cohort."""

    model_name: str = "assumptionSet"
    version: str = "v1"
    effective_from: date = Field(description="Date from which these assumptions apply.")
    qx_by_age: dict[int, float] = Field(
        default_factory=dict,
        description="Annual mortality probability by attained age, e.g. {\"35\": 0.0012}.",
    )
    lapse_by_tenure: dict[int, float] = Field(
        default_factory=dict,
        description="Annual lapse probability by policy year, e.g. {\"1\": 0.08}.",
    )
    morbidity_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    morbidity_trend: float = Field(default=0.05, ge=-1.0, le=1.0)

    def qx(self, age: int) -> float:
        """Mortality for an age, with a documented default outside the table.

        The default is deliberately conservative (higher than any realistic
        mortality) so that an unbounded cohort cannot produce an optimistic
        liability.
        """
        return float(self.qx_by_age.get(age, min(0.02 + max(0, age - 40) * 0.0015, 0.15)))

    def lapse(self, tenure: int) -> float:
        """Lapse for a tenure year, with a documented default."""
        return float(self.lapse_by_tenure.get(tenure, 0.06))

    def to_dict(self) -> dict[str, Any]:
        return {
            "modelName": self.model_name,
            "version": self.version,
            "effectiveFrom": self.effective_from.isoformat(),
            "qxByAge": {str(k): v for k, v in sorted(self.qx_by_age.items())},
            "lapseByTenure": {str(k): v for k, v in sorted(self.lapse_by_tenure.items())},
            "morbidityRate": self.morbidity_rate,
            "morbidityTrend": self.morbidity_trend,
        }


class ExpenseAssumptions(Model):
    """Maintenance, acquisition and claim-handling expenses."""

    version: str = "v1"
    maintenance_expense_pct_of_premium: float = Field(default=0.02, ge=0.0, le=1.0)
    acquisition_expense_pct_of_premium: float = Field(default=0.15, ge=0.0, le=1.0)
    claim_expense_per_claim: float = Field(default=750.0, ge=0.0)
    expense_inflation: float = Field(default=0.03, ge=-0.5, le=0.5)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")


class DiscountCurve(Model):
    """A discount curve, either locked-in at initial recognition or updated.

    Both must be kept: IFRS 17 requires the locked-in rate for the CSM accretion
    (paragraph B72) and the updated rate for insurance finance income, and
    conflating them is one of the most common reporting errors.
    """

    curve_id: str
    basis: str = Field(default="locked-in", description="locked-in or updated.")
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    as_of: date
    spot_rates: dict[int, float] = Field(
        default_factory=dict,
        description="Annual spot rate by term in years, e.g. {\"1\": 0.042, \"10\": 0.048}.",
    )

    def spot(self, term: int) -> float:
        """Spot rate for a term, linearly interpolated and flat-extrapolated.

        Beyond the last quoted point the rate is held flat rather than extrapolated
        on a curve fit: an invented long-dated rate has no evidential basis and can
        materially distort a 30-year liability.
        """
        if not self.spot_rates:
            msg = f"discount curve '{self.curve_id}' has no rates"
            raise ValueError(msg)
        terms = sorted(self.spot_rates)
        if term <= terms[0]:
            return float(self.spot_rates[terms[0]])
        if term >= terms[-1]:
            return float(self.spot_rates[terms[-1]])
        for low, high in zip(terms, terms[1:], strict=True):
            if low <= term <= high:
                weight = (term - low) / (high - low)
                return float(
                    self.spot_rates[low] * (1 - weight) + self.spot_rates[high] * weight
                )
        return float(self.spot_rates[terms[-1]])

    def discount_factor(self, term: int) -> float:
        """Annual-compounded discount factor to ``term`` years."""
        return (1.0 + self.spot(term)) ** -term

    def to_dict(self) -> dict[str, Any]:
        return {
            "curveId": self.curve_id,
            "basis": self.basis,
            "currency": self.currency,
            "asOf": self.as_of.isoformat(),
            "spotRates": {str(k): v for k, v in sorted(self.spot_rates.items())},
        }


class ContractGroupInput(Model):
    """Input to a measurement run for one group of insurance contracts (GIC).

    This is the boundary between the PAS and the engine: the PAS knows policies,
    premiums and claims; the engine needs a contract-level projection of them.
    """

    group_id: str = Field(min_length=3, max_length=64, description="GIC identifier.")
    period: str = Field(pattern=r"^\d{4}$", description="Reporting period, e.g. '2026'.")
    reporting_date: date = Field(description="Valuation date.")
    measurement_model: MeasurementModel = MeasurementModel.GMM
    product_line: str = Field(min_length=1, max_length=64, description="Line of business.")
    cohort_year: int = Field(ge=1800, le=2200, description="Issue-date cohort year.")
    profitability: Profitability = Profitability.PROFITABLE
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")

    contract_count: int = Field(ge=0)
    issued_premium: float = Field(ge=0.0, description="Premium received on issue.")
    expected_premiums: float = Field(default=0.0, ge=0.0, description="PV of future premium cash inflows.")
    expected_claims: float = Field(default=0.0, ge=0.0, description="PV of future claim cash outflows.")
    expected_expenses: float = Field(default=0.0, ge=0.0, description="PV of future maintenance expenses.")
    cash_at_recognition: float = Field(default=0.0, description="Cash paid or received at initial recognition.")

    average_age_at_issue: int = Field(default=35, ge=0, le=120)
    average_term_years: int = Field(default=20, ge=1, le=80)
    issue_date: date | None = Field(
        default=None,
        description=(
            "Date the cohort's contracts took effect. Required for PAA, where the "
            "unearned premium proportion depends on the elapsed coverage period."
        ),
    )
    average_annualised_premium: float = Field(default=0.0, ge=0.0)
    locked_in_rate: float = Field(default=0.04, ge=-0.2, le=0.5)
    coverage_units: list[CoverageUnit] = Field(default_factory=lambda: [CoverageUnit.INSURANCE])

    assumption_version: str = Field(default="v1", description="Version of the assumptions used.")
    data_as_of: date | None = Field(default=None, description="As-of date of the source data.")
    metadata_share: float = Field(
        default=0.80, ge=0.05, le=1.0,
        description=(
            "The entity's share of the underlying items, used only under VFA. "
            "IFRS 17 paragraph B48 makes the fulfilment cash flows vary with this."
        ),
    )

    @model_validator(mode="after")
    def _cohort_precedes_reporting(self) -> ContractGroupInput:
        if self.cohort_year > self.reporting_date.year:
            msg = (
                f"cohort_year {self.cohort_year} is after the reporting year "
                f"{self.reporting_date.year}"
            )
            raise ValueError(msg)
        return self

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class ProfitOrLossLine(Model):
    """One disaggregated line of the IFRS 17 profit-or-loss presentation."""

    component: ProfitOrLossComponent
    amount: float
    currency: str = "USD"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": str(self.component),
            "amount": round(self.amount, 2),
            "currency": self.currency,
            "note": self.note,
        }


class MeasurementResult(Model):
    """The measured position of one group at one reporting date.

    Carries the full derivation so the number is reproducible: the inputs, the
    assumption versions, the curve identifiers and every intermediate component.
    """

    group_id: str
    period: str
    reporting_date: date
    measurement_model: MeasurementModel
    currency: str = "USD"
    product_line: str = ""
    """Carried through so disclosures and statutory mappings can be produced without
    a second lookup into the cohort table."""
    contract_count: int = 0

    fulfilment_cash_flows: float = Field(
        description="PV(future inflows) - PV(future outflows). A liability when negative."
    )
    premium_cash_flows: float = Field(default=0.0, description="PV of future premium inflows (an asset).")
    claims_cash_flows: float = Field(default=0.0, description="PV of future claims and expenses.")
    risk_adjustment: float = Field(default=0.0, description="Compensation for cash-flow uncertainty.")
    contractual_service_margin: float = Field(
        default=0.0,
        description="Unearned profit. Zero for PAA groups and for loss-making groups.",
    )
    loss_component: float = Field(default=0.0, description="Fulfilment cash flows in excess of the asset.")
    total_liability: float = Field(default=0.0, description="LRC plus LIC, sign-applied.")

    insurance_revenue: float = 0.0
    insurance_service_expense: float = 0.0
    insurance_finance_expense: float = 0.0
    risk_adjustment_release: float = 0.0
    csm_release: float = 0.0
    loss_component_reversal: float = 0.0

    opening_csm: float = 0.0
    new_business_csm: float = 0.0
    interest_accretion: float = 0.0
    experience_adjustment: float = 0.0
    assumption_change: float = 0.0
    loss_component_absorption: float = 0.0
    """Signed adjustment to the CSM movements because they went to a loss component
    instead. Reported as its own CSM roll-forward line so the identity holds on its face."""
    closing_csm: float = 0.0
    opening_lrc: float = 0.0
    """Liability for remaining coverage at the start of the period. Zero at initial
    recognition; carried forward on subsequent measurement so the LRC roll-forward
    has a genuine opening balance."""

    locked_in_curve_id: str = ""
    updated_curve_id: str = ""
    assumption_version: str = "v1"
    method: str = ""
    notes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    duration_ms: float = 0.0

    @property
    def insurance_service_result(self) -> float:
        return self.insurance_revenue - self.insurance_service_expense

    @property
    def insurance_finance_result(self) -> float:
        return -self.insurance_finance_expense

    def to_dict(self) -> dict[str, Any]:
        return {
            "groupId": self.group_id,
            "period": self.period,
            "reportingDate": self.reporting_date.isoformat(),
            "measurementModel": str(self.measurement_model),
            "currency": self.currency,
            "productLine": self.product_line,
            "contractCount": self.contract_count,
            "fulfilmentCashFlows": round(self.fulfilment_cash_flows, 2),
            "premiumCashFlows": round(self.premium_cash_flows, 2),
            "claimsCashFlows": round(self.claims_cash_flows, 2),
            "riskAdjustment": round(self.risk_adjustment, 2),
            "contractualServiceMargin": round(self.contractual_service_margin, 2),
            "lossComponent": round(self.loss_component, 2),
            "totalLiability": round(self.total_liability, 2),
            "profitOrLoss": [
                ProfitOrLossLine(
                    component=ProfitOrLossComponent.INSURANCE_REVENUE,
                    amount=self.insurance_revenue,
                    currency=self.currency,
                    note="Expected claims and expenses released from the liability for "
                         "remaining coverage, net of the CSM release.",
                ).to_dict(),
                ProfitOrLossLine(
                    component=ProfitOrLossComponent.INSURANCE_SERVICE_EXPENSE,
                    amount=self.insurance_service_expense,
                    currency=self.currency,
                ).to_dict(),
                ProfitOrLossLine(
                    component=ProfitOrLossComponent.INSURANCE_SERVICE_RESULT,
                    amount=self.insurance_service_result,
                    currency=self.currency,
                ).to_dict(),
                ProfitOrLossLine(
                    component=ProfitOrLossComponent.INSURANCE_FINANCE_INCOME_EXPENSE,
                    amount=-self.insurance_finance_expense,
                    currency=self.currency,
                    note="Presented net; at initial recognition there is no elapsed period, "
                         "so this is nil.",
                ).to_dict(),
                ProfitOrLossLine(
                    component=ProfitOrLossComponent.RISK_ADJUSTMENT_RELEASE,
                    amount=self.risk_adjustment_release,
                    currency=self.currency,
                ).to_dict(),
                ProfitOrLossLine(
                    component=ProfitOrLossComponent.CSM_RELEASE,
                    amount=self.csm_release,
                    currency=self.currency,
                ).to_dict(),
                ProfitOrLossLine(
                    component=ProfitOrLossComponent.LOSS_COMPONENT_REVERSAL,
                    amount=self.loss_component_reversal,
                    currency=self.currency,
                ).to_dict(),
            ],
            "csmRollForward": {
                "opening": round(self.opening_csm, 2),
                "newBusiness": round(self.new_business_csm, 2),
                "interestAccretion": round(self.interest_accretion, 2),
                "experienceAdjustment": round(self.experience_adjustment, 2),
                "assumptionChange": round(self.assumption_change, 2),
                "lossComponentAbsorption": round(self.loss_component_absorption, 2),
                "csmRelease": round(self.csm_release, 2),
                "closing": round(self.closing_csm, 2),
            },
            "curves": {"lockedIn": self.locked_in_curve_id, "updated": self.updated_curve_id},
            "assumptionVersion": self.assumption_version,
            "method": self.method,
            "notes": self.notes,
            "warnings": self.warnings,
            "durationMs": round(self.duration_ms, 3),
        }


class GroupingDecision(Model):
    """The grouping assessment for one cohort, with the reason recorded."""

    group_id: str
    basis: GroupingBasis
    product_line: str
    cohort_year: int
    profitability: Profitability
    profitable_at_recognition: bool
    rationale: str
    rules_fired: list[str] = Field(default_factory=list)
    contract_count: int = 0
    locked_in_rate: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "groupId": self.group_id,
            "basis": str(self.basis),
            "productLine": self.product_line,
            "cohortYear": self.cohort_year,
            "profitability": str(self.profitability),
            "profitableAtRecognition": self.profitable_at_recognition,
            "rationale": self.rationale,
            "rulesFired": self.rules_fired,
            "contractCount": self.contract_count,
            "lockedInRate": self.locked_in_rate,
        }


class AssumptionVersion(Model):
    """A versioned set of assumptions, with the diff that produced it."""

    version: str
    label: str
    effective_from: date
    mortality: MortalityAssumptions
    expenses: ExpenseAssumptions = Field(default_factory=ExpenseAssumptions)
    locked_in_curve: DiscountCurve | None = None
    updated_curve: DiscountCurve | None = None
    created_by: str = "system"
    status: str = Field(default="draft", description="draft, approved or superseded.")
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "label": self.label,
            "effectiveFrom": self.effective_from.isoformat(),
            "status": self.status,
            "createdBy": self.created_by,
            "mortality": self.mortality.to_dict(),
            "expenses": self.expenses.model_dump(mode="json", by_alias=True),
            "lockedInCurve": self.locked_in_curve.to_dict() if self.locked_in_curve else None,
            "updatedCurve": self.updated_curve.to_dict() if self.updated_curve else None,
            "notes": self.notes,
        }


def liability_for_remaining_coverage(result: MeasurementResult) -> float:
    """LRC as presented: the fulfilment cash flows relating to future service."""
    return result.fulfilment_cash_flows + result.risk_adjustment + result.contractual_service_margin


def liability_for_incurred_claims(result: MeasurementResult) -> float:
    """LIC: fulfilment cash flows for incurred but unsettled claims."""
    return -max(0.0, result.claims_cash_flows) + max(0.0, result.loss_component)


def net_balance_sheet(result: MeasurementResult) -> dict[str, float]:
    """The two-line balance sheet extract a filing needs."""
    lrc = liability_for_remaining_coverage(result)
    lic = liability_for_incurred_claims(result)
    return {
        "liabilityForRemainingCoverage": round(lrc, 2),
        "liabilityForIncurredClaims": round(lic, 2),
        "contractualServiceMargin": round(result.contractual_service_margin, 2),
        "riskAdjustment": round(result.risk_adjustment, 2),
        "netInsuranceContractLiability": round(lrc + lic, 2),
    }


def ssap102_reserve_class(product_line: str) -> str:
    """Map a product line onto a NAIC SSAP 102 / VMAP reserve class.

    SSAP No. 102 is the US statutory source of truth for life reserves; mapping
    IFRS 17 output onto it lets a carrier report both bases from one calculation.
    """
    mapping = {
        "term": "class1",
        "whole life": "class2",
        "universal life": "class3",
        "indexed universal life": "class3",
        "variable universal life": "class3",
        "fixed indexed annuity": "class5",
        "variable annuity": "class5",
        "immediate annuity": "class5",
        "deferred annuity": "class5",
        "long term care": "class9",
        "group life": "class1",
        "credit": "class11",
    }
    return mapping.get(product_line.strip().lower(), "class99")
