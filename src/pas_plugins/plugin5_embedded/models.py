"""Distribution domain models.

The vocabulary is a distribution manager's: partner, licence, lead, quote, bind,
payment intent, commission statement. Two ideas run through the file and are worth
stating up front.

**A quote is a promise with an expiry.** `expires_at` is not decoration - a premium
computed against a rate table is only defensible for as long as that table is, and
an expired quote that is still bindable is a mispricing exposure.

**A payment intent is not a payment.** `PaymentIntent` records that money was
*asked* for; `Payment` records that it arrived. The distinction is what lets a
reconciliation show "requested but never received" without inspecting a bank
statement.
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


class PartnerTier(StrEnum):
    """Partner commercial tier. Tier drives commission rate and service level."""

    PLATINUM = "platinum"
    GOLD = "gold"
    SILVER = "silver"
    STARTER = "starter"

    @property
    def default_commission_bps(self) -> int:
        """Default commission in basis points of first-year premium."""
        return {"platinum": 1800, "gold": 1400, "silver": 1100, "starter": 800}[str(self.value)]

    @property
    def settlement_days(self) -> int:
        """Days to settle commission after the free-look period clears."""
        return {"platinum": 7, "gold": 14, "silver": 21, "starter": 30}[str(self.value)]


class PartnerStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    TERMINATED = "terminated"

    @property
    def may_sell(self) -> bool:
        return self is PartnerStatus.ACTIVE


class SalesChannel(StrEnum):
    BANCASSURANCE = "bancassurance"
    BROKER = "broker"
    AGGREGATOR = "aggregator"
    DIGITAL_AGENCY = "digitalAgency"
    EMBEDDED_FINANCIALS = "embeddedFinancials"
    AFFINITY = "affinity"


class DistributionProductStatus(StrEnum):
    DRAFT = "draft"
    LISTED = "listed"
    WITHDRAWN = "withdrawn"

    @property
    def is_orderable(self) -> bool:
        return self is DistributionProductStatus.LISTED


class DistributionProductCategory(StrEnum):
    TERM = "term"
    WHOLE = "whole"
    UNIVERSAL_LIFE = "universalLife"
    IUL = "indexedUniversalLife"
    ANNUITY = "annuity"
    DISABILITY = "disability"
    CRITICAL_ILLNESS = "criticalIllness"
    SIMPLE_TERM = "simpleTerm"


class QuoteStatus(StrEnum):
    DRAFT = "draft"
    ISSUED = "issued"
    CONVERTED = "converted"
    EXPIRED = "expired"
    CANCELLED = "cancelled"

    @property
    def is_bindable(self) -> bool:
        return self is QuoteStatus.ISSUED


class ApplicantGender(StrEnum):
    FEMALE = "female"
    MALE = "male"
    OTHER = "other"
    UNDISCLOSED = "undisclosed"


class SmokingStatus(StrEnum):
    NEVER = "never"
    FORMER = "former"
    CURRENT = "current"
    OCCASIONAL = "occasional"


class KycStatus(StrEnum):
    NOT_STARTED = "notStarted"
    IN_PROGRESS = "inProgress"
    CLEAR = "clear"
    REVIEW = "review"
    FAILED = "failed"
    EXPIRED = "expired"

    @property
    def permits_bind(self) -> bool:
        return self is KycStatus.CLEAR


class SanctionsResult(StrEnum):
    NO_MATCH = "noMatch"
    POTENTIAL_MATCH = "potentialMatch"
    CONFIRMED_MATCH = "confirmedMatch"


class PepResult(StrEnum):
    NONE = "none"
    POTENTIAL = "potential"
    CONFIRMED = "confirmed"


class PaymentMethod(StrEnum):
    CARD = "card"
    ACH = "ach"
    BANK_TRANSFER = "bankTransfer"
    PAYROLL_DEDUCTION = "payrollDeduction"
    HRA = "hsa"

    @property
    def supports_recurring(self) -> bool:
        """Whether a premium can be collected on a schedule from this method."""
        return self in {PaymentMethod.CARD, PaymentMethod.ACH, PaymentMethod.PAYROLL_DEDUCTION}


class PaymentStatus(StrEnum):
    REQUIRES_ACTION = "requiresAction"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partiallyRefunded"

    @property
    def is_settled(self) -> bool:
        return self in {PaymentStatus.CAPTURED, PaymentStatus.PARTIALLY_REFUNDED}


class PolicyStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    LAPSED = "lapsed"
    CANCELLED = "cancelled"
    PENDING_FREE_LOOK = "pendingFreeLook"

    @property
    def is_in_force(self) -> bool:
        return self in {PolicyStatus.ACTIVE, PolicyStatus.PENDING_FREE_LOOK}


class CommissionStatus(StrEnum):
    ACCRUED = "accrued"
    PAYABLE = "payable"
    PAID = "paid"
    CLAWED_BACK = "clawedBack"


class SuitabilityOutcome(StrEnum):
    SUITABLE = "suitable"
    NOT_SUITABLE = "notSuitable"
    NEEDS_REVIEW = "needsReview"


class Channel(StrEnum):
    PARTNER_API = "partnerApi"
    PARTNER_PORTAL = "partnerPortal"
    INTERNAL = "internal"


class LeadStatus(StrEnum):
    NEW = "new"
    CONTACTED = "contacted"
    QUOTED = "quoted"
    CONVERTED = "converted"
    LOST = "lost"


class Party(Model):
    """An individual or organisation that can hold a licence."""

    party_id: str = Field(min_length=2, max_length=64)
    legal_name: str = Field(min_length=2, max_length=200)
    country: str = Field(default="US", min_length=2, max_length=3)
    tax_id_last4: str = Field(default="", pattern=r"^\d{0,4}$")
    date_of_birth: date | None = None
    email: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=40)

    @field_validator("country")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @property
    def is_adult(self) -> bool:
        if not self.date_of_birth:
            return False
        today = date.today()
        age = today.year - self.date_of_birth.year - (
            (today.month, today.day) < (self.date_of_birth.month, self.date_of_birth.day)
        )
        return age >= 18

    @property
    def age(self) -> int | None:
        if not self.date_of_birth:
            return None
        today = date.today()
        return today.year - self.date_of_birth.year - (
            (today.month, today.day) < (self.date_of_birth.month, self.date_of_birth.day)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "partyId": self.party_id,
            "legalName": self.legal_name,
            "country": self.country,
            "taxIdLast4": self.tax_id_last4,
            "dateOfBirth": self.date_of_birth.isoformat() if self.date_of_birth else None,
            "email": self.email,
            "phone": self.phone,
        }


class ApplicantProfile(Model):
    """A prospective policyholder, as the partner captured them."""

    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    date_of_birth: date
    gender: ApplicantGender = ApplicantGender.UNDISCLOSED
    smoking_status: SmokingStatus = SmokingStatus.NEVER
    state_of_residence: str = Field(min_length=2, max_length=2)
    country_of_residence: str = Field(default="US", min_length=2, max_length=3)
    occupation: str | None = Field(default=None, max_length=120)
    annual_income: float = Field(default=0.0, ge=0.0)
    coverage_need: float = Field(
        default=0.0, ge=0.0,
        description="The consumer's own stated need, used to test suitability rather than to price.",
    )
    existing_coverage: float = Field(default=0.0, ge=0.0)
    dependents: int = Field(default=0, ge=0, le=30)
    consented_to_credit_check: bool = Field(default=False)
    consented_to_marketing: bool = Field(default=False)

    @field_validator("state_of_residence", mode="before")
    @classmethod
    def _upper_state(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("country_of_residence")
    @classmethod
    def _upper_country(cls, value: str) -> str:
        return value.upper()

    @property
    def age(self) -> int:
        today = date.today()
        return today.year - self.date_of_birth.year - (
            (today.month, today.day) < (self.date_of_birth.month, self.date_of_birth.day)
        )

    @property
    def is_smoker(self) -> bool:
        return self.smoking_status in {SmokingStatus.CURRENT, SmokingStatus.OCCASIONAL}

    @property
    def coverage_gap(self) -> float:
        """How far the requested coverage sits from the consumer's stated need."""
        return max(0.0, self.coverage_need - self.existing_coverage)

    def to_dict(self) -> dict[str, Any]:
        return {
            "firstName": self.first_name,
            "lastName": self.last_name,
            "dateOfBirth": self.date_of_birth.isoformat(),
            "gender": str(self.gender),
            "smokingStatus": str(self.smoking_status),
            "stateOfResidence": self.state_of_residence,
            "countryOfResidence": self.country_of_residence,
            "occupation": self.occupation,
            "annualIncome": self.annual_income,
            "coverageNeed": self.coverage_need,
            "existingCoverage": self.existing_coverage,
            "dependents": self.dependents,
            "consentedToCreditCheck": self.consented_to_credit_check,
            "consentedToMarketing": self.consented_to_marketing,
        }


class ProductEntitlement(Model):
    """A partner's permission to sell one product, in one place, on one tier."""

    product_id: str = Field(min_length=2, max_length=60)
    states: list[str] = Field(default_factory=list, description="Empty means all entitled states.")
    min_tier: PartnerTier = PartnerTier.STARTER
    commission_override_bps: int | None = Field(
        default=None, ge=0, le=10_000,
        description="Replaces the tier default when set.",
    )
    max_face_amount: float | None = Field(default=None, gt=0.0)
    effective_from: date | None = None
    effective_to: date | None = None

    @field_validator("states")
    @classmethod
    def _upper_states(cls, value: list[str]) -> list[str]:
        return sorted({s.strip().upper() for s in value})

    def is_active_on(self, when: date) -> bool:
        """Whether the entitlement is in force on a given date."""
        if self.effective_from and when < self.effective_from:
            return False
        if self.effective_to and when > self.effective_to:
            return False
        return True

    def covers_state(self, state: str) -> bool:
        return not self.states or state.upper() in self.states

    def commission_bps(self, tier: PartnerTier) -> int:
        return self.commission_override_bps or tier.default_commission_bps

    def to_dict(self) -> dict[str, Any]:
        return {
            "productId": self.product_id,
            "states": self.states,
            "minTier": str(self.min_tier),
            "commissionOverrideBps": self.commission_override_bps,
            "maxFaceAmount": self.max_face_amount,
            "effectiveFrom": self.effective_from.isoformat() if self.effective_from else None,
            "effectiveTo": self.effective_to.isoformat() if self.effective_to else None,
        }


class Partner(Model):
    """A distribution partner: an agency, a bank, an aggregator, an app."""

    partner_id: str = Field(min_length=2, max_length=64)
    legal_name: str = Field(min_length=2, max_length=200)
    display_name: str = Field(min_length=2, max_length=160)
    status: PartnerStatus = PartnerStatus.PENDING
    tier: PartnerTier = PartnerTier.STARTER
    channel: SalesChannel = SalesChannel.BROKER
    country: str = Field(default="US", min_length=2, max_length=3)
    annual_quota_policies: int = Field(default=1_000, ge=0)
    entitlements: list[ProductEntitlement] = Field(default_factory=list)
    payout_account_masked: str = Field(default="", max_length=40)
    contact_email: str | None = Field(default=None, max_length=200)
    onboarded_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("country")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    def to_dict(self) -> dict[str, Any]:
        return {
            "partnerId": self.partner_id,
            "legalName": self.legal_name,
            "displayName": self.display_name,
            "status": str(self.status),
            "tier": str(self.tier),
            "channel": str(self.channel),
            "country": self.country,
            "annualQuotaPolicies": self.annual_quota_policies,
            "entitlements": [e.to_dict() for e in self.entitlements],
            "payoutAccountMasked": self.payout_account_masked,
            "contactEmail": self.contact_email,
            "onboardedAt": self.onboarded_at.isoformat() if self.onboarded_at else None,
            "createdAt": self.created_at.isoformat(),
        }


class DistributionProduct(Model):
    """A product as the distribution channel sees it.

    Deliberately narrower than a product configuration: a partner needs to render
    a card and price it, not see the rating table internals.
    """

    product_id: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=160)
    carrier: str = Field(min_length=1, max_length=120)
    category: DistributionProductCategory
    status: DistributionProductStatus = DistributionProductStatus.LISTED
    min_age: int = Field(default=18, ge=0, le=120)
    max_age: int = Field(default=80, ge=0, le=120)
    min_face_amount: float = Field(default=1_000.0, gt=0.0)
    max_face_amount: float = Field(ge=0.0)
    term_options_years: list[int] = Field(default_factory=list)
    rate_per_thousand_by_age: dict[int, float] = Field(
        default_factory=dict, description="Attained age to annual rate per $1,000 of face."
    )
    base_commission_bps: int = Field(default=1_000, ge=0, le=10_000)
    states: list[str] = Field(default_factory=list)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    underwriting_class: str = Field(default="standard", max_length=40)
    instant_decision: bool = Field(default=False)
    requires_paramed_exam: bool = Field(default=False)
    marketing_summary: str = ""
    tags: list[str] = Field(default_factory=list)

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @field_validator("states")
    @classmethod
    def _upper_states(cls, value: list[str]) -> list[str]:
        return sorted({s.strip().upper() for s in value})

    @model_validator(mode="after")
    def _consistent(self) -> DistributionProduct:
        if self.max_age <= self.min_age:
            msg = f"product '{self.name}' has a max age at or below its minimum"
            raise ValueError(msg)
        if self.max_face_amount <= self.min_face_amount:
            msg = f"product '{self.name}' has a max face amount at or below its minimum"
            raise ValueError(msg)
        if not self.rate_per_thousand_by_age:
            msg = f"product '{self.name}' has no rates, so it cannot be quoted"
            raise ValueError(msg)
        return self

    @property
    def is_instant_decision_eligible(self) -> bool:
        """Whether the product can be bound without human underwriting."""
        return self.instant_decision and not self.requires_paramed_exam

    def age_band_for(self, age: int) -> tuple[int, float] | None:
        """Return (band age, rate) for an attained age, or None if out of range."""
        if age < self.min_age or age > self.max_age:
            return None
        ages = sorted(self.rate_per_thousand_by_age)
        chosen = ages[0]
        for band_age in ages:
            if band_age <= age:
                chosen = band_age
            else:
                break
        return chosen, self.rate_per_thousand_by_age[chosen]

    def covers_state(self, state: str) -> bool:
        return not self.states or state.upper() in self.states

    def to_dict(self) -> dict[str, Any]:
        return {
            "productId": self.product_id,
            "name": self.name,
            "carrier": self.carrier,
            "category": str(self.category),
            "status": str(self.status),
            "minAge": self.min_age,
            "maxAge": self.max_age,
            "minFaceAmount": self.min_face_amount,
            "maxFaceAmount": self.max_face_amount,
            "termOptionsYears": self.term_options_years,
            "ratePerThousandByAge": {str(k): v for k, v in sorted(self.rate_per_thousand_by_age.items())},
            "baseCommissionBps": self.base_commission_bps,
            "states": self.states,
            "currency": self.currency,
            "underwritingClass": self.underwriting_class,
            "instantDecision": self.instant_decision,
            "requiresParamedExam": self.requires_paramed_exam,
            "marketingSummary": self.marketing_summary,
            "tags": self.tags,
        }


class Lead(Model):
    """A prospect a partner has engaged, before it becomes a quote."""

    lead_id: str = Field(min_length=2, max_length=64)
    partner_id: str = Field(min_length=2, max_length=64)
    applicant: ApplicantProfile
    state: str = Field(min_length=2, max_length=2)
    product_id: str | None = Field(default=None, max_length=60)
    status: LeadStatus = LeadStatus.NEW
    source_channel: Channel = Channel.PARTNER_API
    utm_source: str | None = Field(default=None, max_length=120)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("state", mode="before")
    @classmethod
    def _upper(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @property
    def is_convertible(self) -> bool:
        return self.status in {LeadStatus.NEW, LeadStatus.CONTACTED, LeadStatus.QUOTED}

    def to_dict(self) -> dict[str, Any]:
        return {
            "leadId": self.lead_id,
            "partnerId": self.partner_id,
            "applicant": self.applicant.to_dict(),
            "state": self.state,
            "productId": self.product_id,
            "status": str(self.status),
            "sourceChannel": str(self.source_channel),
            "utmSource": self.utm_source,
            "createdAt": self.created_at.isoformat(),
        }


class SuitabilityAssessment(Model):
    """Whether the cover suits this consumer's stated need.

    This exists because the cheapest distribution is the one that sells the wrong
    product: a consumer with no dependents and no income protection does not need
    a large whole life policy, and selling them one is a complaint waiting to
    happen and, in most markets, a conduct breach.
    """

    outcome: SuitabilityOutcome
    reasons: list[str] = Field(default_factory=list)
    coverage_need: float = 0.0
    coverage_requested: float = 0.0
    assessed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    assessed_by: str = Field(default="system")

    @property
    def over_insured_ratio(self) -> float:
        """Requested coverage as a multiple of stated need. 1.0 is proportionate."""
        if self.coverage_need <= 0:
            return 0.0
        return round(self.coverage_requested / self.coverage_need, 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": str(self.outcome),
            "reasons": self.reasons,
            "coverageNeed": self.coverage_need,
            "coverageRequested": self.coverage_requested,
            "overInsuredRatio": self.over_insured_ratio,
            "assessedAt": self.assessed_at.isoformat(),
            "assessedBy": self.assessed_by,
        }


class KycResult(Model):
    """The outcome of identity and sanctions screening."""

    status: KycStatus = KycStatus.NOT_STARTED
    sanctions_result: SanctionsResult = SanctionsResult.NO_MATCH
    pep_result: PepResult = PepResult.NONE
    adverse_media_count: int = Field(default=0, ge=0)
    identity_verified: bool = False
    document_type: str | None = Field(default=None, max_length=40)
    document_reference: str | None = Field(default=None, max_length=120)
    consent_recorded: bool = False
    screened_at: datetime | None = None
    findings: list[str] = Field(default_factory=list)

    @property
    def requires_manual_review(self) -> bool:
        return self.status in {KycStatus.REVIEW, KycStatus.FAILED} or self.sanctions_result in {
            SanctionsResult.POTENTIAL_MATCH,
            SanctionsResult.CONFIRMED_MATCH,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "sanctionsResult": str(self.sanctions_result),
            "pepResult": str(self.pep_result),
            "adverseMediaCount": self.adverse_media_count,
            "identityVerified": self.identity_verified,
            "documentType": self.document_type,
            "documentReference": self.document_reference,
            "consentRecorded": self.consent_recorded,
            "screenedAt": self.screened_at.isoformat() if self.screened_at else None,
            "requiresManualReview": self.requires_manual_review,
            "findings": self.findings,
        }


class QuoteLineItem(Model):
    """One component of a quoted premium."""

    label: str
    amount: float
    kind: str = Field(default="premium")

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "amount": round(self.amount, 4), "kind": self.kind}


class Quote(Model):
    """A partner-specific quote, including what the partner earns."""

    quote_id: str = Field(min_length=4, max_length=64)
    tenant_id: str = Field(min_length=2, max_length=64)
    partner_id: str = Field(min_length=2, max_length=64)
    lead_id: str | None = Field(default=None, max_length=64)
    product_id: str
    applicant_age: int = Field(ge=0, le=120)
    applicant_key: str = Field(
        default="",
        max_length=200,
        description=(
            "Stable identity of the applicant, used for deterministic screening. "
            "Carried on the quote because a quote is about a specific person: without "
            "it, screening could not repeatably return the same answer for them."
        ),
    )
    face_amount: float = Field(gt=0.0)
    term_years: int | None = Field(default=None, ge=1, le=80)
    state: str = Field(min_length=2, max_length=2)
    status: QuoteStatus = QuoteStatus.ISSUED
    currency: str = Field(default="USD", min_length=3, max_length=3)
    base_rate_per_thousand: float = 0.0
    annual_premium: float = 0.0
    monthly_premium: float = 0.0
    annual_commission: float = 0.0
    commission_bps: int = 0
    payment_frequency: str = Field(default="monthly", max_length=20)
    line_items: list[QuoteLineItem] = Field(default_factory=list)
    suitability: SuitabilityAssessment | None = None
    disclosed_risks: list[str] = Field(default_factory=list)
    quoted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(UTC) >= self.expires_at

    @property
    def is_bindable(self) -> bool:
        """An expired quote is not bindable, whatever its status field says."""
        return self.status.is_bindable and not self.is_expired

    def to_dict(self) -> dict[str, Any]:
        return {
            "quoteId": self.quote_id,
            "tenantId": self.tenant_id,
            "partnerId": self.partner_id,
            "leadId": self.lead_id,
            "productId": self.product_id,
            "applicantAge": self.applicant_age,
            "applicantKey": self.applicant_key,
            "faceAmount": self.face_amount,
            "termYears": self.term_years,
            "state": self.state,
            "status": str(self.status),
            "currency": self.currency,
            "baseRatePerThousand": self.base_rate_per_thousand,
            "annualPremium": round(self.annual_premium, 2),
            "monthlyPremium": round(self.monthly_premium, 2),
            "annualCommission": round(self.annual_commission, 2),
            "commissionBps": self.commission_bps,
            "paymentFrequency": self.payment_frequency,
            "lineItems": [line.to_dict() for line in self.line_items],
            "suitability": self.suitability.to_dict() if self.suitability else None,
            "disclosedRisks": self.disclosed_risks,
            "quotedAt": self.quoted_at.isoformat(),
            "expiresAt": self.expires_at.isoformat() if self.expires_at else None,
            "isBindable": self.is_bindable,
        }


class PaymentIntent(Model):
    """A request to collect money against a quote."""

    intent_id: str = Field(min_length=4, max_length=64)
    quote_id: str
    partner_id: str
    amount: float = Field(gt=0.0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    method: PaymentMethod = PaymentMethod.ACH
    status: PaymentStatus = PaymentStatus.REQUIRES_ACTION
    frequency: str = Field(default="monthly", max_length=20)
    attempts: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @property
    def is_expired(self) -> bool:
        return bool(self.expires_at and datetime.now(UTC) >= self.expires_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "intentId": self.intent_id,
            "quoteId": self.quote_id,
            "partnerId": self.partner_id,
            "amount": round(self.amount, 2),
            "currency": self.currency,
            "method": str(self.method),
            "status": str(self.status),
            "frequency": self.frequency,
            "attempts": self.attempts,
            "createdAt": self.created_at.isoformat(),
            "expiresAt": self.expires_at.isoformat() if self.expires_at else None,
            "isExpired": self.is_expired,
        }


class Payment(Model):
    """Money that actually moved."""

    payment_id: str = Field(min_length=4, max_length=64)
    intent_id: str
    quote_id: str
    partner_id: str
    amount: float = Field(gt=0.0)
    refunded_amount: float = Field(default=0.0, ge=0.0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    method: PaymentMethod = PaymentMethod.ACH
    status: PaymentStatus = PaymentStatus.CAPTURED
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    settlement_reference: str | None = Field(default=None, max_length=120)

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _refund_within_amount(self) -> Payment:
        if self.refunded_amount > self.amount:
            msg = f"refund of {self.refunded_amount} exceeds the payment of {self.amount}"
            raise ValueError(msg)
        return self

    @property
    def net_amount(self) -> float:
        return round(self.amount - self.refunded_amount, 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "paymentId": self.payment_id,
            "intentId": self.intent_id,
            "quoteId": self.quote_id,
            "partnerId": self.partner_id,
            "amount": round(self.amount, 2),
            "refundedAmount": round(self.refunded_amount, 2),
            "netAmount": self.net_amount,
            "currency": self.currency,
            "method": str(self.method),
            "status": str(self.status),
            "capturedAt": self.captured_at.isoformat(),
            "settlementReference": self.settlement_reference,
        }


class Policy(Model):
    """A bound policy. Issuing is a carrier-system act; this records it."""

    policy_id: str = Field(min_length=4, max_length=64)
    quote_id: str
    partner_id: str
    product_id: str
    holder_name: str = Field(min_length=2, max_length=160)
    face_amount: float = Field(gt=0.0)
    annual_premium: float = Field(gt=0.0)
    commission_bps: int = 0
    status: PolicyStatus = PolicyStatus.PENDING
    effective_date: date | None = None
    free_look_expires: date | None = None
    bound_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kyc_status: KycStatus = KycStatus.NOT_STARTED
    suitability_outcome: SuitabilityOutcome = SuitabilityOutcome.NEEDS_REVIEW
    carrier_policy_reference: str | None = Field(default=None, max_length=120)

    @property
    def in_free_look(self) -> bool:
        if not self.free_look_expires:
            return False
        return date.today() <= self.free_look_expires

    @property
    def commission_payable(self) -> bool:
        """Commission is only payable once the free-look period has cleared.

        A policy cancelled inside free look must not have earned commission, which
        is why this is a property of the policy rather than of the statement.
        """
        return self.status.is_in_force and not self.in_free_look

    def annual_commission(self) -> float:
        return round(self.annual_premium * self.commission_bps / 10_000.0, 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "policyId": self.policy_id,
            "quoteId": self.quote_id,
            "partnerId": self.partner_id,
            "productId": self.product_id,
            "holderName": self.holder_name,
            "faceAmount": self.face_amount,
            "annualPremium": round(self.annual_premium, 2),
            "commissionBps": self.commission_bps,
            "annualCommission": self.annual_commission(),
            "status": str(self.status),
            "effectiveDate": self.effective_date.isoformat() if self.effective_date else None,
            "freeLookExpires": self.free_look_expires.isoformat() if self.free_look_expires else None,
            "boundAt": self.bound_at.isoformat(),
            "kycStatus": str(self.kyc_status),
            "suitabilityOutcome": str(self.suitability_outcome),
            "inFreeLook": self.in_free_look,
            "commissionPayable": self.commission_payable,
            "carrierPolicyReference": self.carrier_policy_reference,
        }


class CommissionEntry(Model):
    """One line of a commission statement."""

    entry_id: str
    policy_id: str
    product_id: str
    written_on: date
    annual_premium: float
    commission_bps: int
    amount: float
    status: CommissionStatus = CommissionStatus.ACCRUED
    clawback_reason: str | None = Field(default=None, max_length=200)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entryId": self.entry_id,
            "policyId": self.policy_id,
            "productId": self.product_id,
            "writtenOn": self.written_on.isoformat(),
            "annualPremium": round(self.annual_premium, 2),
            "commissionBps": self.commission_bps,
            "amount": round(self.amount, 2),
            "status": str(self.status),
            "clawbackReason": self.clawback_reason,
        }


class CommissionStatement(Model):
    """What a partner has earned over a period, and what is payable now."""

    statement_id: str = Field(min_length=4, max_length=64)
    tenant_id: str
    partner_id: str
    period_start: date
    period_end: date
    currency: str = Field(default="USD", min_length=3, max_length=3)
    entries: list[CommissionEntry] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    settlement_due: date | None = None

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @property
    def gross_commission(self) -> float:
        return round(sum(e.amount for e in self.entries if e.status is not CommissionStatus.CLAWED_BACK), 2)

    @property
    def clawback_total(self) -> float:
        return round(sum(e.amount for e in self.entries if e.status is CommissionStatus.CLAWED_BACK), 2)

    @property
    def payable_now(self) -> float:
        return round(
            sum(
                e.amount
                for e in self.entries
                if e.status in {CommissionStatus.PAYABLE, CommissionStatus.PAID}
            ),
            2,
        )

    @property
    def pending(self) -> float:
        return round(sum(e.amount for e in self.entries if e.status is CommissionStatus.ACCRUED), 2)

    def by_product(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for entry in self.entries:
            totals[entry.product_id] = round(
                totals.get(entry.product_id, 0.0) + entry.amount, 2
            )
        return totals

    def to_dict(self) -> dict[str, Any]:
        return {
            "statementId": self.statement_id,
            "tenantId": self.tenant_id,
            "partnerId": self.partner_id,
            "periodStart": self.period_start.isoformat(),
            "periodEnd": self.period_end.isoformat(),
            "currency": self.currency,
            "entryCount": len(self.entries),
            "grossCommission": self.gross_commission,
            "clawbackTotal": self.clawback_total,
            "payableNow": self.payable_now,
            "pending": self.pending,
            "byProduct": self.by_product(),
            "entries": [e.to_dict() for e in self.entries],
            "generatedAt": self.generated_at.isoformat(),
            "settlementDue": self.settlement_due.isoformat() if self.settlement_due else None,
        }


class WebhookEvent(Model):
    """An event a partner is notified about."""

    event_id: str = Field(min_length=4, max_length=64)
    event_type: str = Field(min_length=2, max_length=60)
    partner_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    delivered: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    delivered_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "eventId": self.event_id,
            "eventType": self.event_type,
            "partnerId": self.partner_id,
            "payload": self.payload,
            "delivered": self.delivered,
            "createdAt": self.created_at.isoformat(),
            "deliveredAt": self.delivered_at.isoformat() if self.delivered_at else None,
        }


class ReconciliationRow(Model):
    """One line of a partner reconciliation between quote and money."""

    quote_id: str
    product_id: str
    premium: float
    collected: float
    outstanding: float
    state: str = Field(default="open", max_length=20)

    def to_dict(self) -> dict[str, Any]:
        return {
            "quoteId": self.quote_id,
            "productId": self.product_id,
            "premium": round(self.premium, 2),
            "collected": round(self.collected, 2),
            "outstanding": round(self.outstanding, 2),
            "state": self.state,
        }


__all__ = [
    "ApplicantGender",
    "ApplicantProfile",
    "Channel",
    "CommissionEntry",
    "CommissionStatement",
    "CommissionStatus",
    "DistributionProduct",
    "DistributionProductCategory",
    "DistributionProductStatus",
    "KycResult",
    "KycStatus",
    "Lead",
    "LeadStatus",
    "Model",
    "Partner",
    "PartnerStatus",
    "PartnerTier",
    "Party",
    "Payment",
    "PaymentIntent",
    "PaymentMethod",
    "PaymentStatus",
    "PepResult",
    "Policy",
    "PolicyStatus",
    "ProductEntitlement",
    "Quote",
    "QuoteLineItem",
    "QuoteStatus",
    "ReconciliationRow",
    "SalesChannel",
    "SanctionsResult",
    "SmokingStatus",
    "SuitabilityAssessment",
    "SuitabilityOutcome",
    "WebhookEvent",
]