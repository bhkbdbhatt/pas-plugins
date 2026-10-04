"""Canonical entity model for the unified data layer.

Every source - PAS, claims, billing, CRM, external enrichment - is mapped into one
of four entity types.  Choosing ACORD NGDS as the canonical vocabulary (rather than
inventing a house style) is what makes the unified layer legible to carriers whose
teams already speak it, and what lets plugin 1 exchange the same records with a
carrier's own systems.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EntityType(StrEnum):
    """The four canonical entity types in the unified layer."""

    CUSTOMER = "customer"
    POLICY = "policy"
    CLAIM = "claim"
    PREMIUM = "premium"


class SourceSystem(StrEnum):
    """Systems the mesh ingests from."""

    PAS = "pas"
    CLAIMS = "claims"
    BILLING = "billing"
    CRM = "crm"
    EXTERNAL_ENRICHMENT = "externalEnrichment"
    AGENCY = "agency"
    MANUAL = "manual"


class RecordQuality(StrEnum):
    GOLDEN = "golden"
    SILVER = "silver"
    BRONZE = "bronze"

    @property
    def rank(self) -> int:
        return {"bronze": 0, "silver": 1, "golden": 2}[str(self.value)]


class PiiClass(StrEnum):
    """Data-sensitivity classification driving masking and access control.

    The classification is data, not code, so a carrier's compliance officer can
    re-classify a field without a release - and so the same record can be
    ``PUBLIC`` to a service account and ``PHI`` to a human reader.
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    PII = "pii"
    PHI = "phi"
    FINANCIAL = "financial"
    PCI = "pci"

    @property
    def requires_unmask_scope(self) -> bool:
        return self in {PiiClass.PII, PiiClass.PHI, PiiClass.FINANCIAL, PiiClass.PCI}


class ChangeEventType(StrEnum):
    """Why a record changed - the basis for incremental CDC-style ingestion."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    SNAPSHOT = "snapshot"


class AccessPurpose(StrEnum):
    """Purpose limitation, as required by HIPAA and by good data practice.

    Every read of a golden record must state why.  A read with no purpose is
    refused rather than served, because "somebody asked" is not an audit answer.
    """

    UNDERWRITING = "underwriting"
    SERVICING = "servicing"
    CLAIMS = "claims"
    ACTUARIAL = "actuarial"
    REPORTING = "reporting"
    REGULATORY = "regulatory"
    MARKETING = "marketing"
    SUPPORT = "support"


class BaseEntity(BaseModel):
    """Common envelope shared by every unified entity record."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # Fields that must be populated for a record to be published as gold. This is
    # declared rather than inferred: completeness measured against "every field
    # the model happens to define" would score a good record badly simply because
    # the carrier's CRM has no ``middle_name`` column, and a quality metric people
    # ignore is worse than no metric at all.
    GOLD_FIELDS: ClassVar[tuple[str, ...]] = ()

    surrogate_key: str = Field(
        min_length=8, max_length=64,
        description="Stable internal identifier for the golden record.",
    )
    tenant_id: str = Field(min_length=3, max_length=64, description="Owning carrier.")
    entity_type: EntityType
    natural_key: str = Field(
        min_length=1, max_length=128,
        description="Business key from the source system, used for reconciliation.",
    )
    source_system: SourceSystem
    source_record_id: str = Field(min_length=1, max_length=128)
    attributes: dict[str, Any] = Field(
        default_factory=dict,
        description="Entity-specific fields, validated against the entity schema.",
    )
    quality_tier: RecordQuality = RecordQuality.BRONZE
    quality_score: float = Field(default=0.0, ge=0.0, le=1.0)
    pii_classes: dict[str, PiiClass] = Field(
        default_factory=dict,
        description="Field path -> sensitivity class. Drives masking and access control.",
    )
    lineage: list[dict[str, Any]] = Field(
        default_factory=list,
        description="OpenLineage-shaped provenance for every field that contributed.",
    )
    effective_from: datetime = Field(default_factory=lambda: datetime.now(UTC))
    effective_to: datetime | None = None
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    consent: dict[str, bool] = Field(
        default_factory=dict,
        description="Per-purpose consent flags. Refused purposes are not enriched or served.",
    )

    @property
    def is_current(self) -> bool:
        return self.effective_to is None

    def gold_completeness(self) -> tuple[float, list[str]]:
        """Fraction of :attr:`GOLD_FIELDS` populated, plus the ones that are missing."""
        if not self.GOLD_FIELDS:
            return 1.0, []
        missing = [
            name for name in self.GOLD_FIELDS
            if getattr(self, name, None) in (None, "", [], {})
        ]
        return round((len(self.GOLD_FIELDS) - len(missing)) / len(self.GOLD_FIELDS), 4), missing

    def attribute(self, name: str, default: Any = None) -> Any:  # noqa: ANN401
        return self.attributes.get(name, default)

    def sensitivity_of(self, *paths: str) -> PiiClass:
        """Highest-sensitivity class across the given field paths."""
        order = [PiiClass.PCI, PiiClass.PHI, PiiClass.FINANCIAL, PiiClass.PII,
                 PiiClass.INTERNAL, PiiClass.PUBLIC]
        classes = [self.pii_classes.get(p, PiiClass.INTERNAL) for p in paths]
        return min(classes, key=order.index)

    def masked(self, paths: tuple[str, ...] = ()) -> "BaseEntity":
        """Return a copy with every sensitive field value masked."""
        from pas_core.pii import mask_record  # noqa: PLC0415

        return mask_record(self, paths=paths)

    def to_summary(self) -> dict[str, Any]:
        """Compact projection for list endpoints and MCP tool results."""
        return {
            "surrogateKey": self.surrogate_key,
            "entityType": str(self.entity_type),
            "naturalKey": self.natural_key,
            "sourceSystem": str(self.source_system),
            "qualityTier": str(self.quality_tier),
            "qualityScore": round(self.quality_score, 4),
        }


class Customer(BaseEntity):
    """A natural person known to the carrier.

    The canonical customer is the party, not the policyholder: an insured who is
    also the owner and the payor is one customer with three roles.
    """

    GOLD_FIELDS: ClassVar[tuple[str, ...]] = (
        "first_name", "last_name", "date_of_birth", "address_line1",
        "address_city", "address_state", "address_postal_code",
    )

    entity_type: EntityType = EntityType.CUSTOMER
    first_name: str = Field(min_length=1, max_length=60)
    last_name: str = Field(min_length=1, max_length=60)
    middle_name: str | None = Field(default=None, max_length=60)
    date_of_birth: date
    ssn_last4: str | None = Field(default=None, pattern=r"^\d{4}$")
    gender: str = Field(default="U", pattern=r"^[MFUX]$")
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=20)
    address_line1: str | None = Field(default=None, max_length=100)
    address_city: str | None = Field(default=None, max_length=60)
    address_state: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    address_postal_code: str | None = Field(default=None, pattern=r"^\d{5}(-\d{4})?$")
    preferred_language: str | None = Field(default=None, pattern=r"^[a-z]{2}(-[A-Z]{2})?$")
    lifetime_premium: float = Field(default=0.0, ge=0.0)
    in_force_policy_count: int = Field(default=0, ge=0)
    customer_since: date | None = None
    tags: list[str] = Field(default_factory=list)

    @property
    def full_name(self) -> str:
        return " ".join(filter(None, [self.first_name, self.middle_name, self.last_name]))

    @property
    def age(self) -> int:
        today = date.today()
        return today.year - self.date_of_birth.year - (
            (today.month, today.day) < (self.date_of_birth.month, self.date_of_birth.day)
        )

    @model_validator(mode="after")
    def _classify_sensitivity(self) -> Customer:
        classes = dict(self.pii_classes)
        classes.setdefault("first_name", PiiClass.PII)
        classes.setdefault("last_name", PiiClass.PII)
        classes.setdefault("date_of_birth", PiiClass.PII)
        classes.setdefault("ssn_last4", PiiClass.PII)
        classes.setdefault("email", PiiClass.PII)
        classes.setdefault("phone", PiiClass.PII)
        classes.setdefault("address_line1", PiiClass.PII)
        classes.setdefault("address_city", PiiClass.PII)
        classes.setdefault("address_state", PiiClass.PII)
        classes.setdefault("address_postal_code", PiiClass.PII)
        classes.setdefault("lifetime_premium", PiiClass.FINANCIAL)
        classes.setdefault("in_force_policy_count", PiiClass.FINANCIAL)
        object.__setattr__(self, "pii_classes", classes)
        return self

    @field_validator("date_of_birth")
    @classmethod
    def _not_in_future(cls, value: date) -> date:
        if value > date.today():
            msg = "date_of_birth cannot be in the future"
            raise ValueError(msg)
        return value

    def to_summary(self) -> dict[str, Any]:
        return {
            "surrogateKey": self.surrogate_key,
            "entityType": str(self.entity_type),
            "naturalKey": self.natural_key,
            "name": f"{self.first_name[:1]}. {self.last_name}",
            "age": self.age,
            "inForcePolicyCount": self.in_force_policy_count,
            "lifetimePremium": round(self.lifetime_premium, 2),
            "qualityTier": str(self.quality_tier),
            "sourceSystems": [self.source_system],
        }


class Policy(BaseEntity):
    """A contract, from whichever system currently holds it."""

    GOLD_FIELDS: ClassVar[tuple[str, ...]] = (
        "policy_number", "customer_key", "product_code", "status", "state_of_issue",
        "issue_date", "effective_date", "face_amount", "annualised_premium",
    )

    entity_type: EntityType = EntityType.POLICY
    policy_number: str = Field(min_length=3, max_length=64, description="Carrier policy number.")
    customer_key: str = Field(min_length=8, max_length=64, description="Golden customer surrogate key.")
    product_code: str = Field(min_length=1, max_length=64)
    status: str = Field(min_length=2, max_length=32)
    lob: str = Field(default="Life", max_length=40)
    state_of_issue: str = Field(pattern=r"^[A-Z]{2}$")
    issue_date: date | None = None
    effective_date: date | None = None
    expiration_date: date | None = None
    face_amount: float = Field(default=0.0, ge=0.0)
    annualised_premium: float = Field(default=0.0, ge=0.0)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    payment_mode: str | None = Field(default=None, max_length=20)
    agent_id: str | None = Field(default=None, max_length=64)
    coverages: list[dict[str, Any]] = Field(default_factory=list)
    beneficiaries: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _classify_sensitivity(self) -> Policy:
        classes = dict(self.pii_classes)
        classes.setdefault("policy_number", PiiClass.FINANCIAL)
        classes.setdefault("face_amount", PiiClass.FINANCIAL)
        classes.setdefault("annualised_premium", PiiClass.FINANCIAL)
        classes.setdefault("beneficiaries", PiiClass.PII)
        object.__setattr__(self, "pii_classes", classes)
        return self

    def to_summary(self) -> dict[str, Any]:
        return {
            "surrogateKey": self.surrogate_key,
            "entityType": str(self.entity_type),
            "naturalKey": self.natural_key,
            "policyNumber": self.policy_number,
            "productCode": self.product_code,
            "status": self.status,
            "customerKey": self.customer_key,
            "annualisedPremium": round(self.annualised_premium, 2),
            "stateOfIssue": self.state_of_issue,
            "sourceSystems": [self.source_system],
        }


class Claim(BaseEntity):
    """A claim, normalised across the claims system and any supplementary feeds."""

    GOLD_FIELDS: ClassVar[tuple[str, ...]] = (
        "claim_number", "policy_number", "claim_type", "status",
        "report_date", "claim_amount",
    )

    entity_type: EntityType = EntityType.CLAIM
    claim_number: str = Field(min_length=3, max_length=64)
    policy_number: str = Field(min_length=3, max_length=64)
    customer_key: str | None = Field(default=None, max_length=64)
    claim_type: str = Field(min_length=2, max_length=40, description="death, disability, hospital, ...")
    status: str = Field(min_length=2, max_length=32)
    loss_date: date | None = None
    report_date: date | None = None
    settlement_date: date | None = None
    claim_amount: float = Field(default=0.0, ge=0.0)
    paid_amount: float = Field(default=0.0, ge=0.0)
    reserve_amount: float = Field(default=0.0, ge=0.0)
    adjuster_id: str | None = Field(default=None, max_length=64)
    denial_reason: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _paid_not_above_claim(self) -> Claim:
        if self.paid_amount > self.claim_amount > 0:
            msg = f"paid_amount ({self.paid_amount}) exceeds claim_amount ({self.claim_amount})"
            raise ValueError(msg)
        return self

    def to_summary(self) -> dict[str, Any]:
        return {
            "surrogateKey": self.surrogate_key,
            "entityType": str(self.entity_type),
            "naturalKey": self.natural_key,
            "claimNumber": self.claim_number,
            "policyNumber": self.policy_number,
            "claimType": self.claim_type,
            "status": self.status,
            "claimAmount": round(self.claim_amount, 2),
            "sourceSystems": [self.source_system],
        }


class Premium(BaseEntity):
    """A premium transaction - the record IFRS 17 and revenue share both need."""

    GOLD_FIELDS: ClassVar[tuple[str, ...]] = (
        "policy_number", "due_date", "premium_type", "amount_due", "status",
    )

    entity_type: EntityType = EntityType.PREMIUM
    policy_number: str = Field(min_length=3, max_length=64)
    customer_key: str | None = Field(default=None, max_length=64)
    due_date: date
    paid_date: date | None = None
    premium_type: str = Field(
        default="regular", pattern=r"^(initial|regular|additional|reinstatement|surrender|valuation)$"
    )
    amount_due: float = Field(default=0.0, ge=0.0)
    amount_paid: float = Field(default=0.0, ge=0.0)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    status: str = Field(default="outstanding", pattern=r"^(outstanding|paid|waived|failed|refunded)$")
    commission_amount: float = Field(default=0.0, ge=0.0)
    billing_frequency: str | None = Field(default=None, max_length=20)

    @property
    def days_late(self) -> int:
        if self.status == "paid" or self.paid_date:
            return 0
        return max(0, (date.today() - self.due_date).days)

    def to_summary(self) -> dict[str, Any]:
        return {
            "surrogateKey": self.surrogate_key,
            "entityType": str(self.entity_type),
            "naturalKey": self.natural_key,
            "policyNumber": self.policy_number,
            "dueDate": self.due_date.isoformat(),
            "amountDue": round(self.amount_due, 2),
            "status": self.status,
            "daysLate": self.days_late,
        }


ENTITY_MODELS: dict[EntityType, type[BaseEntity]] = {
    EntityType.CUSTOMER: Customer,
    EntityType.POLICY: Policy,
    EntityType.CLAIM: Claim,
    EntityType.PREMIUM: Premium,
}


def model_for(entity_type: EntityType | str) -> type[BaseEntity]:
    """Look up the canonical model for an entity type."""
    try:
        return ENTITY_MODELS[EntityType(entity_type)]
    except (KeyError, ValueError) as exc:
        from pas_core.errors import ValidationError  # noqa: PLC0415

        raise ValidationError(
            f"Unknown entity type '{entity_type}'",
            entityType=str(entity_type),
            supported=[str(e) for e in ENTITY_MODELS],
        ) from exc


class Customer360(BaseModel):
    """The single-call view an agent or an AI assistant needs.

    This is the shape the specification calls for: "AI agents can query customer
    360, policy history, claims and billing in one call".
    """

    model_config = ConfigDict(extra="forbid")

    customer: Customer
    policies: list[Policy] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    premiums: list[Premium] = Field(default_factory=list)
    source_systems: list[SourceSystem] = Field(default_factory=list)
    completeness: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description="Fraction of expected domains present (contact, policies, claims, billing).",
    )
    masked_fields: list[str] = Field(
        default_factory=list,
        description="Which fields were masked for this caller, and why.",
    )
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def in_force_policies(self) -> list[Policy]:
        return [p for p in self.policies if p.status in {"active", "issued", "paid_up"}]

    @property
    def total_in_force_premium(self) -> float:
        return round(sum(p.annualised_premium for p in self.in_force_policies), 2)

    def to_summary(self) -> dict[str, Any]:
        return {
            "customer": self.customer.to_summary(),
            "policyCount": len(self.policies),
            "inForcePolicyCount": len(self.in_force_policies),
            "claimCount": len(self.claims),
            "premiumRecordCount": len(self.premiums),
            "totalInForcePremium": self.total_in_force_premium,
            "sourceSystems": [str(s) for s in self.source_systems],
            "completeness": round(self.completeness, 4),
            "maskedFields": self.masked_fields,
        }
