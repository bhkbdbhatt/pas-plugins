"""Canonical ACORD NGDS entity models for life and annuity business.

These are the shared vocabulary types: the gateway's atomic operations, the AUW
workbench submissions, the embedded quote/bind pipeline and the IFRS 17 engine
all speak these structures, so a plugin never has to re-derive "what is a face
amount" or "what states are a policy allowed to be in".
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NGDS_PATTERN = r"^[A-Z0-9][A-Z0-9\-_/]{1,34}[A-Z0-9]$"


class PolicyStatus(StrEnum):
    """Policy lifecycle states shared by the PAS, MCP tools and the ledger."""

    QUOTED = "quoted"
    SUBMITTED = "submitted"
    UNDERWRITING = "underwriting"
    REFERRED = "referred"
    DECLINED = "declined"
    ACCEPTED = "accepted"
    ISSUED = "issued"
    ACTIVE = "active"
    LAPSED = "lapsed"
    SURRENDERED = "surrendered"
    TERMINATED = "terminated"
    EXPIRED = "expired"
    PAID_UP = "paid_up"

    @property
    def is_terminal(self) -> bool:
        return self in {
            PolicyStatus.DECLINED,
            PolicyStatus.SURRENDERED,
            PolicyStatus.TERMINATED,
            PolicyStatus.EXPIRED,
        }

    @property
    def is_inforce(self) -> bool:
        return self in {
            PolicyStatus.ACTIVE,
            PolicyStatus.LAPSED,
            PolicyStatus.PAID_UP,
        }


class Gender(StrEnum):
    MALE = "M"
    FEMALE = "F"
    UNKNOWN = "U"
    NOT_SPECIFIED = "N"


class Relationship(StrEnum):
    """Role a party plays with respect to a contract."""

    APPLICANT = "applicant"
    PROPOSED_INSURED = "proposedInsured"
    INSURED = "insured"
    OWNER = "owner"
    BENEFICIARY = "beneficiary"
    CONTINGENT_BENEFICIARY = "contingentBeneficiary"
    PAYOR = "payor"
    ANNUITANT = "annuitant"
    APPLICANT_PAYOR = "applicantPayor"
    AGENT = "agent"
    CONSENTING_PARTY = "consentingParty"


class CoverageType(StrEnum):
    """Life and annuity coverage categories used for rating and reporting."""

    TERM = "term"
    WHOLE_LIFE = "wholeLife"
    UNIVERSAL_LIFE = "universalLife"
    INDEXED_UNIVERSAL_LIFE = "indexedUniversalLife"
    VARIABLE_UNIVERSAL_LIFE = "variableUniversalLife"
    FIXED_INDEXED_ANNUITY = "fixedIndexedAnnuity"
    VARIABLE_ANNUITY = "variableAnnuity"
    IMMEDIATE_ANNUITY = "immediateAnnuity"
    DEFERRED_ANNUITY = "deferredAnnuity"
    LONG_TERM_CARE = "longTermCare"
    GUARANTEED_LIVING_BENEFIT = "guaranteedLivingBenefit"


class ValuationMethod(StrEnum):
    """Valuation basis for a block of business."""

    MODIFIED_ACTUARIAL = "modifiedActuarial"
    NET_LEVEL_PREMIUM = "netLevelPremium"
    CASH_VALUE = "cashValue"


class PaymentMode(StrEnum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    SEMI_ANNUAL = "semiAnnual"
    ANNUAL = "annual"
    SINGLE = "single"


class Address(BaseModel):
    """NGDS address block. ISO 3166-2 subdivision codes are required for US."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    line1: str = Field(min_length=1, max_length=100)
    line2: str | None = Field(default=None, max_length=100)
    city: str = Field(min_length=1, max_length=60)
    state: str = Field(
        min_length=2,
        max_length=2,
        description="US state or territory code, e.g. 'NY'.",
    )
    postal_code: str = Field(pattern=r"^\d{5}(-\d{4})?$", description="US ZIP / ZIP+4")
    country: str = Field(default="US", pattern=r"^[A-Z]{2}$")

    @field_validator("state", "country")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    def as_lines(self) -> list[str]:
        return [line for line in (self.line1, self.line2, f"{self.city}, {self.state} {self.postal_code}") if line]

    @property
    def is_po_box(self) -> bool:
        return self.line1.upper().startswith(("PO BOX", "P.O. BOX"))


class Contact(BaseModel):
    """Contact point per NGDS party model."""

    model_config = ConfigDict(extra="forbid")

    telephone: str | None = Field(default=None, pattern=r"^\+?[0-9]{10,15}$")
    email: str | None = Field(default=None, max_length=254)
    preferred_channel: str | None = Field(default=None, max_length=20)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        if value and ("@" not in value or value.startswith("@") or value.endswith("@")):
            msg = f"'{value}' is not a valid email address"
            raise ValueError(msg)
        return value.lower() if value else value


class Person(BaseModel):
    """An individual party. ``ssn_last4`` only - full SSNs never transit this suite."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    party_id: str = Field(pattern=NGDS_PATTERN, description="Carrier-assigned party identifier.")
    first_name: str = Field(min_length=1, max_length=60)
    last_name: str = Field(min_length=1, max_length=60)
    middle_name: str | None = Field(default=None, max_length=60)
    suffix: str | None = Field(default=None, max_length=10)
    date_of_birth: date
    gender: Gender = Gender.UNKNOWN
    ssn_last4: str | None = Field(default=None, pattern=r"^\d{4}$")
    address: Address | None = None
    contact: Contact | None = None
    tobacco_use: bool | None = Field(
        default=None, description="Required for mortality-rated life products."
    )
    preferred_language: str | None = Field(default=None, pattern=r"^[a-z]{2}(-[A-Z]{2})?$")

    @model_validator(mode="after")
    def _check_age(self) -> Person:
        today = date.today()
        years = today.year - self.date_of_birth.year - (
            (today.month, today.day) < (self.date_of_birth.month, self.date_of_birth.day)
        )
        if years > 120:
            msg = f"date_of_birth implies an implausible age of {years}"
            raise ValueError(msg)
        if years < 0:
            msg = "date_of_birth is in the future"
            raise ValueError(msg)
        return self

    @property
    def age(self) -> int:
        today = date.today()
        return today.year - self.date_of_birth.year - (
            (today.month, today.day) < (self.date_of_birth.month, self.date_of_birth.day)
        )

    @property
    def full_name(self) -> str:
        return " ".join(filter(None, [self.first_name, self.middle_name, self.last_name]))

    def display_name(self, *, mask: bool = False) -> str:
        """``Jane Q. Public`` or a masked form for redacted surfaces."""
        if not mask:
            return self.full_name
        initial = f"{self.first_name[:1]}." if self.first_name else ""
        return f"{initial} {self.last_name}".strip()


class RoleAssignment(BaseModel):
    """Binds a party to a contract in a specific role."""

    model_config = ConfigDict(extra="forbid")

    party_id: str = Field(pattern=NGDS_PATTERN)
    relationship: Relationship
    share_percent: float | None = Field(default=None, ge=0, le=100)
    effective_date: date | None = None
    term_years: int | None = Field(default=None, ge=0, le=100)
    benefit_percent: float | None = Field(default=None, ge=0, le=100)
    is_primary: bool = False


class Coverage(BaseModel):
    """A single coverage/rider on a policy."""

    model_config = ConfigDict(extra="forbid")

    coverage_id: str = Field(pattern=NGDS_PATTERN)
    coverage_type: CoverageType
    face_amount: float | None = Field(default=None, ge=0)
    benefit_period_years: int | None = Field(default=None, ge=0, le=80)
    benefit_period_months: int | None = Field(default=None, ge=0, le=11)
    premium_period_years: int | None = Field(default=None, ge=0, le=80)
    premium_to_age: int | None = Field(default=None, ge=0, le=120)
    is_rider: bool = False
    is_guaranteed: bool = True
    waiting_period_months: int | None = Field(default=None, ge=0, le=60)
    rate_class: str | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def _check_periods(self) -> Coverage:
        if self.benefit_period_months is not None and self.benefit_period_months > 11:
            msg = "benefit_period_months must be 0-11"
            raise ValueError(msg)
        if self.is_guaranteed and not self.coverage_type:
            msg = "guaranteed coverages require a coverage_type"
            raise ValueError(msg)
        return self


class LifeEvent(BaseModel):
    """A policy or coverage level event (issue, change, lapse, surrender...)."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(pattern=NGDS_PATTERN)
    event_type: str = Field(min_length=2, max_length=40)
    effective_date: date
    status: PolicyStatus = PolicyStatus.ACTIVE
    amount: float | None = Field(default=None, ge=0)
    reason: str | None = Field(default=None, max_length=200)
    source_system: str | None = Field(default=None, max_length=60)


class Policy(BaseModel):
    """The canonical life/annuity contract record."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    policy_id: str = Field(pattern=NGDS_PATTERN, description="Carrier policy number.")
    product_id: str = Field(pattern=NGDS_PATTERN)
    product_code: str = Field(min_length=1, max_length=30)
    status: PolicyStatus = PolicyStatus.QUOTED
    issue_date: date | None = None
    effective_date: date | None = None
    expiration_date: date | None = None
    face_amount: float = Field(ge=0, description="Sum assured / accumulation value base.")
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    payment_mode: PaymentMode = PaymentMode.MONTHLY
    annualised_premium: float = Field(default=0.0, ge=0)
    valuation_method: ValuationMethod = ValuationMethod.MODIFIED_ACTUARIAL
    parties: list[RoleAssignment] = Field(default_factory=list)
    coverages: list[Coverage] = Field(default_factory=list)
    events: list[LifeEvent] = Field(default_factory=list)
    state_of_issue: str = Field(pattern=r"^[A-Z]{2}$")
    lob: str = Field(default="Life", max_length=40, description="Line of business label.")
    master_group: str | None = Field(default=None, max_length=60)
    total_face_amount: float = Field(default=0.0, ge=0, description="Aggregate face amount.")

    @model_validator(mode="after")
    def _check_dates(self) -> Policy:
        if self.issue_date and self.expiration_date and self.expiration_date <= self.issue_date:
            msg = "expiration_date must be after issue_date"
            raise ValueError(msg)
        if self.status.is_inforce and not self.effective_date:
            msg = f"a policy in status '{self.status}' requires an effective_date"
            raise ValueError(msg)
        if not self.total_face_amount and self.coverages:
            self.total_face_amount = sum(c.face_amount or 0.0 for c in self.coverages)
        return self

    def party_in_role(self, relationship: Relationship) -> RoleAssignment | None:
        for assignment in self.parties:
            if assignment.relationship is relationship:
                return assignment
        return None

    def coverage_of_type(self, coverage_type: CoverageType) -> Coverage | None:
        for coverage in self.coverages:
            if coverage.coverage_type is coverage_type:
                return coverage
        return None

    def to_summary(self) -> dict[str, Any]:
        """Compact projection used in list endpoints and MCP tool results."""
        return {
            "policyId": self.policy_id,
            "productCode": self.product_code,
            "status": str(self.status),
            "stateOfIssue": self.state_of_issue,
            "faceAmount": self.face_amount,
            "currency": self.currency,
            "annualisedPremium": self.annualised_premium,
            "issueDate": self.issue_date.isoformat() if self.issue_date else None,
            "effectiveDate": self.effective_date.isoformat() if self.effective_date else None,
        }


class BeneficiaryAllocation(BaseModel):
    """Beneficiary share with the multi-signature approval state."""

    model_config = ConfigDict(extra="forbid")

    party_id: str = Field(pattern=NGDS_PATTERN)
    relationship: str = Field(min_length=2, max_length=30)
    share_percent: float = Field(ge=0, le=100)
    approved: bool = False
    approved_at: str | None = None
    approval_did: str | None = Field(
        default=None, description="W3C DID of the approving credential holder."
    )

    @model_validator(mode="after")
    def _needs_approval(self) -> BeneficiaryAllocation:
        if self.approved and not self.approved_at:
            msg = "an approved beneficiary allocation requires approved_at"
            raise ValueError(msg)
        return self


def validate_allocations(allocations: list[BeneficiaryAllocation]) -> float:
    """Total beneficiary shares; the caller must assert the 100% rule."""
    return round(sum(a.share_percent for a in allocations), 4)
