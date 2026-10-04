"""Product definition domain models.

Everything a product author writes is one of these models. The vocabulary is a
product manager's, not an actuary's where it can be helped: a *cover* is the thing
sold, a *benefit* is what it pays, a *charge* is what it costs, and a *rating
table* decides the price.

Two conventions run through the file:

* **Bands are inclusive of their lower bound and exclusive of their upper**, with
  the final band closed at the top. That makes a rating table partition a range
  with no gaps and no overlaps, which is what the guardrails check for.
* **Money is decimal and explicit.** Rates are per-thousand-of-face, charges are
  absolute amounts. Mixing the two silently is the classic configuration bug, so
  the units are in the field names.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pas_core.pas.vendors import to_camel


class Model(BaseModel):
    """Base model: snake_case in Python, camelCase on the wire, strict on input."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        alias_generator=to_camel,
        populate_by_name=True,
    )


class ProductStatus(StrEnum):
    """Lifecycle. Only DRAFT is editable; PUBLISHED is immutable."""

    DRAFT = "draft"
    PUBLISHED = "published"
    RETIRED = "retired"

    @property
    def is_editable(self) -> bool:
        return self is ProductStatus.DRAFT


class ProductCategory(StrEnum):
    TERM = "term"
    WHOLE = "whole"
    UNIVERSAL_LIFE = "universalLife"
    INDEX = "index"
    ANNUITY = "annuity"
    IUL = "indexedUniversalLife"
    FINAL_EXPENSE = "finalExpense"
    ACCIDENT = "accident"
    DISABILITY = "disability"


class BenefitCategory(StrEnum):
    DEATH = "death"
    MATURITY = "maturity"
    SURRENDER = "surrender"
    RIDERS = "riders"
    GUARANTEED_INCOME = "guaranteedIncome"


class ChargeFrequency(StrEnum):
    """How often a charge is taken. Monthly is the carrier default."""

    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"
    SINGLE = "single"

    @property
    def per_year(self) -> float:
        return {"monthly": 12.0, "quarterly": 4.0, "annual": 1.0, "single": 1.0}[str(self.value)]


class RuleSeverity(StrEnum):
    """Guardrail and compliance severities.

    BLOCKER stops publication, WARNING is surfaced but allows it, INFO is recorded.
    """

    BLOCKER = "blocker"
    WARNING = "warning"
    INFO = "info"

    @property
    def blocks_publish(self) -> bool:
        return self is RuleSeverity.BLOCKER


class ViolationCategory(StrEnum):
    STRUCTURAL = "structural"
    RATING = "rating"
    COMPLIANCE = "compliance"
    ECONOMICS = "economics"
    CONSISTENCY = "consistency"


class AgeBand(Model):
    """An inclusive-lower, exclusive-upper age band. The top band may be closed."""

    min_age: int = Field(ge=0, le=120, description="Inclusive lower bound.")
    max_age: int | None = Field(
        default=None, ge=1, le=121, description="Exclusive upper bound; None means open-ended."
    )
    label: str = ""

    def contains(self, age: int) -> bool:
        if age < self.min_age:
            return False
        return self.max_age is None or age < self.max_age

    @property
    def is_open(self) -> bool:
        return self.max_age is None

    @property
    def span(self) -> str:
        return f"{self.min_age}-{self.max_age if self.max_age is not None else '+'}"

    @model_validator(mode="after")
    def _ordered(self) -> AgeBand:
        if self.max_age is not None and self.max_age <= self.min_age:
            msg = f"age band {self.min_age}-{self.max_age} is empty"
            raise ValueError(msg)
        return self

    def to_dict(self) -> dict[str, Any]:
        # Only declared fields. `label`, `span` and `openEnded` are derived, and
        # emitting them would make to_dict() output fail to revalidate against
        # the model - which the API and MCP round-trip tests depend on.
        return {"minAge": self.min_age, "maxAge": self.max_age, "label": self.label}


class RateBand(Model):
    """A rate per thousand of face amount for one age band and one rating class."""

    age_band: AgeBand
    rate_per_thousand: float = Field(
        ge=0.0, le=1000.0, description="Base annual premium per $1,000 of face amount."
    )
    policy_fee: float = Field(default=0.0, ge=0.0, description="Fixed annual policy fee in currency.")

    def premium_for(self, face_amount: float, annual: bool = True) -> float:
        """Annual premium for a face amount, optionally amortised to monthly.

        Monthly is computed by dividing by twelve rather than by applying a
        rate-of-interest factor, because a product definition should not embed a
        pricing assumption the author has to remember to state.
        """
        annual_premium = face_amount / 1000.0 * self.rate_per_thousand + self.policy_fee
        return annual_premium if annual else annual_premium / 12.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ageBand": self.age_band.to_dict(),
            "ratePerThousand": self.rate_per_thousand,
            "policyFee": self.policy_fee,
        }


class RatingFactor(Model):
    """A multiplicative or additive adjustment applied to a base rate.

    `direction` is explicit because "factor" without one is ambiguous, and a sign
    error in a rating factor is invisible until it reaches a quote.
    """

    class Direction(StrEnum):
        MULTIPLY = "multiply"
        ADD_BP = "addBp"

    factor_id: str = Field(min_length=2, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    direction: Direction = Direction.MULTIPLY
    value: float = Field(
        description="Multiplier (1.0 = no change) or basis-point offset applied to the base rate."
    )
    applies_to: list[str] = Field(
        default_factory=list, description="Factor ids or explicit conditions this depends on."
    )

    @field_validator("applies_to")
    @classmethod
    def _no_self_reference(cls, value: list[str]) -> list[str]:
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            "factorId": self.factor_id,
            "name": self.name,
            "direction": str(self.direction),
            "value": self.value,
            "appliesTo": self.applies_to,
        }


class RatingTable(Model):
    """The complete rating basis for a cover: bands, factors and cross-references."""

    table_id: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    bands: list[RateBand] = Field(min_length=1)
    factors: list[RatingFactor] = Field(default_factory=list)
    cross_reference: dict[str, str] = Field(
        default_factory=dict,
        description="Rating class name to the factor id that modifies it.",
    )
    currency: str = Field(default="USD", min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    def band_for(self, age: int) -> RateBand | None:
        for band in self.bands:
            if band.age_band.contains(age):
                return band
        return None

    @property
    def age_bands(self) -> list[AgeBand]:
        return [band.age_band for band in self.bands]

    @property
    def covers(self) -> tuple[int, int | None]:
        """The issue-age range this table can price."""
        lows = [band.age_band.min_age for band in self.bands]
        highs = [band.age_band.max_age for band in self.bands if band.age_band.max_age is not None]
        return min(lows), (max(highs) if highs else None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tableId": self.table_id,
            "name": self.name,
            "currency": self.currency,
            # Bands carry their rates, not just their spans. A rate change is the
            # whole reason a version exists, and a diff or a content hash that
            # cannot see a rate change is worse than useless.
            "bands": [band.to_dict() for band in self.bands],
            "factors": [f.to_dict() for f in self.factors],
            "crossReference": dict(self.cross_reference),
        }

    def summary(self) -> dict[str, Any]:
        """A read-only view for listings, with derived fields included."""
        low, high = self.covers
        return {
            **self.to_dict(),
            "bandCount": len(self.bands),
            "ageBands": [band.age_band.to_dict() | {"span": band.age_band.span} for band in self.bands],
            "issueAgeRange": {"min": low, "max": high},
        }


class Benefit(Model):
    """A payable benefit under a cover."""

    benefit_id: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    category: BenefitCategory
    trigger: str = Field(min_length=1, max_length=200, description="When this benefit pays.")
    amount_mode: str = Field(default="faceAmount", max_length=40)
    """How the amount is determined: faceAmount, percentOfFace, schedule, fixed."""
    amount_value: float | None = Field(default=None, ge=0.0)
    guarantee_period_years: int | None = Field(default=None, ge=0, le=100)
    description: str = ""

    @model_validator(mode="after")
    def _percent_needs_a_value(self) -> Benefit:
        if self.amount_mode == "percentOfFace" and self.amount_value is None:
            msg = f"benefit '{self.name}' is a percentage of face but has no amount"
            raise ValueError(msg)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "benefitId": self.benefit_id,
            "name": self.name,
            "category": str(self.category),
            "trigger": self.trigger,
            "amountMode": self.amount_mode,
            "amountValue": self.amount_value,
            "guaranteePeriodYears": self.guarantee_period_years,
            "description": self.description,
        }


class Charge(Model):
    """A cost deducted from premium or from the policy value."""

    charge_id: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    frequency: ChargeFrequency = ChargeFrequency.MONTHLY
    amount: float = Field(description="Currency amount per occurrence. Must not be negative.")
    deducted_from: str = Field(default="premium", max_length=40)
    """premium, policyValue, or fund."""

    @model_validator(mode="after")
    def _non_negative(self) -> Charge:
        if self.amount < 0:
            msg = f"charge '{self.name}' has a negative amount; use a benefit for payouts"
            raise ValueError(msg)
        return self

    def annualised(self) -> float:
        """Annual cost of this charge, treating single charges as one year."""
        return self.amount * ChargeFrequency(self.frequency).per_year

    def to_dict(self) -> dict[str, Any]:
        # No derived `annualised` here: to_dict() output is re-sent to
        # model_validate() by the API and the MCP round-trip tests, and a derived
        # field would come back as an unexpected extra. Consumers that want the
        # annualised figure call annualised().
        return {
            "chargeId": self.charge_id,
            "name": self.name,
            "frequency": str(self.frequency),
            "amount": self.amount,
            "deductedFrom": self.deducted_from,
        }


class EligibilityRule(Model):
    """A condition a proposed insured must satisfy to be considered.

    Rules are declarative and data-evaluated: the engine supports a small, fixed set
    of operators. That is a deliberate limitation - a product author cannot inject
    arbitrary code into the pricing or eligibility path.
    """

    class Operator(StrEnum):
        EQ = "eq"
        NE = "ne"
        GT = "gt"
        GTE = "gte"
        LT = "lt"
        LTE = "lte"
        IN = "in"
        NOT_IN = "notIn"
        BETWEEN = "between"

    class Effect(StrEnum):
        ELIGIBLE = "eligible"
        INELIGIBLE = "ineligible"
        REFER = "refer"

    rule_id: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    field: str = Field(min_length=1, max_length=60, description="Dotted applicant field path.")
    operator: Operator
    value: Any = None
    effect: Effect = Effect.INELIGIBLE
    reason_code: str = Field(min_length=1, max_length=60)
    message: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _operator_needs_operands(self) -> EligibilityRule:
        needs_two = self.operator in {self.Operator.BETWEEN}
        if needs_two and not (isinstance(self.value, (list, tuple)) and len(self.value) == 2):
            msg = f"rule '{self.name}' uses 'between' and needs a two-element value"
            raise ValueError(msg)
        if self.operator in {self.Operator.IN, self.Operator.NOT_IN} and not isinstance(self.value, (list, tuple)):
            msg = f"rule '{self.name}' uses a set operator and needs a list value"
            raise ValueError(msg)
        if needs_two:
            low, high = self.value
            if low > high:
                msg = f"rule '{self.name}' has a between range that runs backwards"
                raise ValueError(msg)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "ruleId": self.rule_id,
            "name": self.name,
            "field": self.field,
            "operator": str(self.operator),
            "value": self.value,
            "effect": str(self.effect),
            "reasonCode": self.reason_code,
            "message": self.message,
        }


class ComplianceRule(Model):
    """A regulatory constraint on a product, scoped to jurisdictions."""

    rule_id: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    citation: str = Field(default="", max_length=200, description="Statutory or regulatory reference.")
    kind: str = Field(
        min_length=2, max_length=40,
        description="maxFaceAmount, minIssueAge, maxIssueAge, rateToValue, stateEligibility, surrenderCharge...",
    )
    states: list[str] = Field(default_factory=list, description="Empty means all states.")
    threshold: float | None = Field(default=None, ge=0.0)
    severity: RuleSeverity = RuleSeverity.BLOCKER
    message: str = Field(min_length=1, max_length=200)

    @field_validator("states")
    @classmethod
    def _upper_states(cls, value: list[str]) -> list[str]:
        return sorted({s.strip().upper() for s in value})

    def applies_in(self, state: str) -> bool:
        return not self.states or state.upper() in self.states

    def to_dict(self) -> dict[str, Any]:
        return {
            "ruleId": self.rule_id,
            "name": self.name,
            "citation": self.citation,
            "kind": self.kind,
            "states": self.states,
            "threshold": self.threshold,
            "severity": str(self.severity),
            "message": self.message,
        }


class Cover(Model):
    """One thing the product can sell."""

    cover_id: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    category: ProductCategory = ProductCategory.TERM
    min_face_amount: float = Field(default=1_000.0, gt=0.0)
    max_face_amount: float = Field(ge=0.0)
    min_issue_age: int = Field(default=0, ge=0, le=120)
    max_issue_age: int = Field(default=80, ge=0, le=120)
    term_options_years: list[int] = Field(default_factory=list)
    rating_table_id: str = Field(min_length=1, max_length=40)
    benefits: list[Benefit] = Field(default_factory=list)
    charges: list[Charge] = Field(default_factory=list)
    eligibility: list[EligibilityRule] = Field(default_factory=list)
    currency: str = Field(default="USD", min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _consistent(self) -> Cover:
        if self.max_face_amount <= self.min_face_amount:
            msg = f"cover '{self.name}' has a max face amount at or below its minimum"
            raise ValueError(msg)
        if self.max_issue_age <= self.min_issue_age:
            msg = f"cover '{self.name}' has a max issue age at or below its minimum"
            raise ValueError(msg)
        if any(term <= 0 for term in self.term_options_years):
            msg = f"cover '{self.name}' has a non-positive term option"
            raise ValueError(msg)
        if len(set(self.term_options_years)) != len(self.term_options_years):
            msg = f"cover '{self.name}' repeats a term option"
            raise ValueError(msg)
        return self

    @property
    def annual_charges(self) -> float:
        return sum(charge.annualised() for charge in self.charges)

    @property
    def is_fixed_term(self) -> bool:
        return bool(self.term_options_years)

    def to_dict(self) -> dict[str, Any]:
        return {
            "coverId": self.cover_id,
            "name": self.name,
            "category": str(self.category),
            "minFaceAmount": self.min_face_amount,
            "maxFaceAmount": self.max_face_amount,
            "minIssueAge": self.min_issue_age,
            "maxIssueAge": self.max_issue_age,
            "termOptionsYears": self.term_options_years,
            "ratingTableId": self.rating_table_id,
            "benefits": [b.to_dict() for b in self.benefits],
            "charges": [c.to_dict() for c in self.charges],
            "eligibility": [e.to_dict() for e in self.eligibility],
            "currency": self.currency,
        }


class ProductDefinition(Model):
    """A complete, self-contained product definition.

    This is the document a product author maintains. It carries its own rating
    tables rather than referencing them by id alone, so a published version is
    self-contained and can be priced years later without looking anything up.
    """

    product_id: str = Field(min_length=2, max_length=60)
    name: str = Field(min_length=2, max_length=160)
    version: int = Field(default=1, ge=1)
    status: ProductStatus = ProductStatus.DRAFT
    category: ProductCategory = ProductCategory.TERM
    description: str = ""
    currency: str = Field(default="USD", min_length=3, max_length=3)
    covers: list[Cover] = Field(min_length=1)
    rating_tables: list[RatingTable] = Field(min_length=1)
    compliance_rules: list[ComplianceRule] = Field(default_factory=list)
    target_margin_bps: int = Field(
        default=800, ge=-5000, le=5000, description="Desired gross margin in basis points."
    )
    states: list[str] = Field(
        default_factory=list, description="States the product is offered in. Empty means none."
    )
    author: str = Field(default="unknown", max_length=128)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    effective_date: date | None = None
    tags: list[str] = Field(default_factory=list)

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, value: str) -> str:
        return value.upper()

    @field_validator("states")
    @classmethod
    def _upper_states(cls, value: list[str]) -> list[str]:
        return sorted({s.strip().upper() for s in value})

    @model_validator(mode="after")
    def _referential_integrity(self) -> ProductDefinition:
        table_ids = {table.table_id for table in self.rating_tables}
        if len(table_ids) != len(self.rating_tables):
            msg = "duplicate rating table ids in this product"
            raise ValueError(msg)
        for cover in self.covers:
            if cover.rating_table_id not in table_ids:
                msg = (
                    f"cover '{cover.name}' references rating table "
                    f"'{cover.rating_table_id}', which this product does not define"
                )
                raise ValueError(msg)
        cover_ids = [cover.cover_id for cover in self.covers]
        if len(set(cover_ids)) != len(cover_ids):
            msg = "duplicate cover ids in this product"
            raise ValueError(msg)
        return self

    def table(self, table_id: str) -> RatingTable | None:
        return next((t for t in self.rating_tables if t.table_id == table_id), None)

    def cover(self, cover_id: str) -> Cover | None:
        return next((c for c in self.covers if c.cover_id == cover_id), None)

    @property
    def is_publishable(self) -> bool:
        """Structural bar for publication, independent of compliance and economics."""
        return bool(self.covers) and bool(self.rating_tables) and bool(self.states)

    def to_dict(self) -> dict[str, Any]:
        return {
            "productId": self.product_id,
            "name": self.name,
            "version": self.version,
            "status": str(self.status),
            "category": str(self.category),
            "description": self.description,
            "currency": self.currency,
            "covers": [c.to_dict() for c in self.covers],
            "ratingTables": [t.to_dict() for t in self.rating_tables],
            "complianceRules": [r.to_dict() for r in self.compliance_rules],
            "targetMarginBps": self.target_margin_bps,
            "states": self.states,
            "author": self.author,
            "createdAt": self.created_at.isoformat(),
            "updatedAt": self.updated_at.isoformat(),
            "effectiveDate": self.effective_date.isoformat() if self.effective_date else None,
            "tags": self.tags,
        }


class Violation(Model):
    """A guardrail or compliance finding against a definition."""

    rule_id: str
    title: str
    severity: RuleSeverity
    category: ViolationCategory
    message: str
    location: str = Field(default="", description="Dotted path to the offending element.")
    remediation: str = ""

    @property
    def blocks_publish(self) -> bool:
        return self.severity.blocks_publish

    def to_dict(self) -> dict[str, Any]:
        return {
            "ruleId": self.rule_id,
            "title": self.title,
            "severity": str(self.severity),
            "category": str(self.category),
            "message": self.message,
            "location": self.location,
            "remediation": self.remediation,
            "blocksPublish": self.blocks_publish,
        }


class GuardrailReport(Model):
    """The full finding set for one definition."""

    product_id: str
    version: int
    violations: list[Violation] = Field(default_factory=list)
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def blockers(self) -> list[Violation]:
        return [v for v in self.violations if v.severity is RuleSeverity.BLOCKER]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity is RuleSeverity.WARNING]

    @property
    def can_publish(self) -> bool:
        return not self.blockers

    def by_category(self, category: ViolationCategory) -> list[Violation]:
        return [v for v in self.violations if v.category is category]

    def to_dict(self) -> dict[str, Any]:
        return {
            "productId": self.product_id,
            "version": self.version,
            "canPublish": self.can_publish,
            "blockerCount": len(self.blockers),
            "warningCount": len(self.warnings),
            "violations": [v.to_dict() for v in self.violations],
            "evaluatedAt": self.evaluated_at.isoformat(),
        }


class QuoteRequest(Model):
    """A rating enquiry against a configured product."""

    tenant_id: str = Field(min_length=3, max_length=64)
    product_id: str = Field(min_length=3, max_length=60)
    cover_id: str = Field(min_length=2, max_length=40)
    age: int = Field(ge=0, le=120)
    face_amount: float = Field(gt=0.0)
    term_years: int | None = Field(default=None, ge=1, le=80)
    risk_factors: dict[str, float] = Field(
        default_factory=dict, description="Rating factor id to multiplier."
    )
    state: str = Field(default="CA", min_length=2, max_length=2)
    tobacco: bool = False
    health_class: str | None = Field(default=None, max_length=20)

    @field_validator("state", mode="before")
    @classmethod
    def _upper_state(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _term_when_fixed(self) -> QuoteRequest:
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenantId": self.tenant_id,
            "productId": self.product_id,
            "coverId": self.cover_id,
            "age": self.age,
            "faceAmount": self.face_amount,
            "termYears": self.term_years,
            "riskFactors": self.risk_factors,
            "state": self.state,
            "tobacco": self.tobacco,
            "healthClass": self.health_class,
        }


class QuoteLine(Model):
    """One explained component of a premium."""

    label: str
    amount: float
    kind: str = Field(default="charge", description="basePremium, factorAdjustment, charge, fee.")
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "amount": round(self.amount, 4),
            "kind": self.kind,
            "detail": self.detail,
        }


class Quote(Model):
    """A priced quote, fully itemised and reproducible."""

    tenant_id: str
    product_id: str
    product_version: int = Field(default=0, description="Set by the service once a version is resolved.")
    cover_id: str
    annual_premium: float
    monthly_premium: float
    face_amount: float
    age: int
    currency: str
    base_rate_per_thousand: float
    rating_band: str
    lines: list[QuoteLine] = Field(default_factory=list)
    annual_charges: float = 0.0
    margin_bps: int = 0
    eligibility: str = Field(default="eligible")
    reason_codes: list[str] = Field(default_factory=list)
    quoted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None

    @property
    def total_annual_cost(self) -> float:
        return self.annual_premium + self.annual_charges

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenantId": self.tenant_id,
            "productId": self.product_id,
            "productVersion": self.product_version,
            "coverId": self.cover_id,
            "annualPremium": round(self.annual_premium, 2),
            "monthlyPremium": round(self.monthly_premium, 2),
            "faceAmount": self.face_amount,
            "age": self.age,
            "currency": self.currency,
            "baseRatePerThousand": self.base_rate_per_thousand,
            "ratingBand": self.rating_band,
            "lines": [line.to_dict() for line in self.lines],
            "annualCharges": round(self.annual_charges, 2),
            "totalAnnualCost": round(self.total_annual_cost, 2),
            "marginBps": self.margin_bps,
            "eligibility": self.eligibility,
            "reasonCodes": self.reason_codes,
            "quotedAt": self.quoted_at.isoformat(),
            "expiresAt": self.expires_at.isoformat() if self.expires_at else None,
        }


class SimulationProfile(Model):
    """An assumed applicant for portfolio simulation."""

    label: str = "standard"
    min_age: int = Field(default=18, ge=0, le=120)
    max_age: int = Field(default=70, ge=0, le=120)
    min_face_amount: float = Field(default=25_000.0, gt=0.0)
    max_face_amount: float = Field(default=1_000_000.0, gt=0.0)
    tobacco_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    state: str = Field(default="CA", min_length=2, max_length=2)
    states: list[str] = Field(default_factory=list)

    @field_validator("state", mode="before")
    @classmethod
    def _upper(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _ordered(self) -> SimulationProfile:
        if self.max_age <= self.min_age:
            msg = f"simulation profile '{self.label}' has an empty age range"
            raise ValueError(msg)
        if self.max_face_amount <= self.min_face_amount:
            msg = f"simulation profile '{self.label}' has an empty face amount range"
            raise ValueError(msg)
        return self


class SimulationResult(Model):
    """The outcome of simulating a definition against a book of business."""

    product_id: str
    version: int
    profile: str
    policies: int
    average_face_amount: float
    average_annual_premium: float
    average_margin_bps: int
    loss_ratio: float = Field(
        default=0.0, ge=0.0,
        description="Charges plus assumed benefits over premium. Exceeds 1 means a loss product.",
    )
    unpriceable: int = Field(default=0, ge=0)
    gross_written_premium: float = 0.0
    profit: float = 0.0
    notes: list[str] = Field(default_factory=list)
    simulated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_viable(self) -> bool:
        return self.average_margin_bps > 0 and self.unpriceable == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "productId": self.product_id,
            "version": self.version,
            "profile": self.profile,
            "policies": self.policies,
            "averageFaceAmount": round(self.average_face_amount, 2),
            "averageAnnualPremium": round(self.average_annual_premium, 2),
            "averageMarginBps": self.average_margin_bps,
            "lossRatio": round(self.loss_ratio, 4),
            "unpriceable": self.unpriceable,
            "grossWrittenPremium": round(self.gross_written_premium, 2),
            "profit": round(self.profit, 2),
            "viable": self.is_viable,
            "notes": self.notes,
            "simulatedAt": self.simulated_at.isoformat(),
        }


class VersionDiffEntry(Model):
    """One difference between two product versions."""

    path: str = Field(description="Dotted path, e.g. covers.cv-term.ratingTables.tbl-1.bands.2.ratePerThousand.")
    change: str = Field(pattern=r"^(added|removed|changed)$")
    before: Any = None
    after: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "change": self.change, "before": self.before, "after": self.after}


class VersionDiff(Model):
    """The full difference between two versions of one product."""

    product_id: str
    from_version: int
    to_version: int
    entries: list[VersionDiffEntry] = Field(default_factory=list)

    @property
    def is_material(self) -> bool:
        """Whether the change touches pricing or eligibility.

        A version bump that only changes the description is still a new version,
        but it is not worth a filing conversation.
        """
        return any(
            any(token in entry.path for token in ("ratingTables", "covers", "complianceRules", "states"))
            for entry in self.entries
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "productId": self.product_id,
            "fromVersion": self.from_version,
            "toVersion": self.to_version,
            "isMaterial": self.is_material,
            "entryCount": len(self.entries),
            "entries": [e.to_dict() for e in self.entries],
        }


class PublishedVersion(Model):
    """An immutable published version, with the hash that identifies it."""

    product_id: str
    version: int
    status: ProductStatus
    content_hash: str
    published_at: datetime
    published_by: str
    guardrail_summary: dict[str, Any] = Field(default_factory=dict)
    definition: ProductDefinition | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "productId": self.product_id,
            "version": self.version,
            "status": str(self.status),
            "contentHash": self.content_hash,
            "publishedAt": self.published_at.isoformat(),
            "publishedBy": self.published_by,
            "guardrailSummary": self.guardrail_summary,
            "definition": self.definition.to_dict() if self.definition else None,
        }


__all__ = [
    "AgeBand",
    "Benefit",
    "BenefitCategory",
    "Charge",
    "ChargeFrequency",
    "ComplianceRule",
    "Cover",
    "EligibilityRule",
    "GuardrailReport",
    "ProductCategory",
    "ProductDefinition",
    "ProductStatus",
    "PublishedVersion",
    "Quote",
    "QuoteLine",
    "QuoteRequest",
    "RateBand",
    "RatingFactor",
    "RatingTable",
    "RuleSeverity",
    "SimulationProfile",
    "SimulationResult",
    "VersionDiff",
    "VersionDiffEntry",
    "Violation",
    "ViolationCategory",
]