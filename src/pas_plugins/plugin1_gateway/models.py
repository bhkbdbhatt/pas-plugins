"""Request and response models for every atomic operation.

These are the authoritative contract: the OpenAPI document, the MCP tool input
schemas and the runtime validation all derive from them, so a spec/code drift is
structurally impossible rather than merely discouraged.

Every model carries field descriptions and examples because an AI agent reads the
schema, not the documentation - if a field is ambiguous to the schema, it is
ambiguous to the agent.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Money = Annotated[float, Field(ge=0, description="A non-negative monetary amount.")]
PositiveMoney = Annotated[float, Field(gt=0, description="A strictly positive monetary amount.")]
Currency = Annotated[
    str, Field(pattern=r"^[A-Z]{3}$", description="ISO 4217 currency code, e.g. USD.")
]
IsoDate = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="ISO-8601 date, e.g. 2026-03-31.")]
PolicyNumber = Annotated[
    str,
    Field(
        pattern=r"^[A-Z0-9][A-Z0-9\-/]{2,34}$",
        description="Carrier policy number. Case-insensitive; returned upper-case.",
    ),
]


class OperationModel(BaseModel):
    """Base for every operation payload."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OperationResult(BaseModel):
    """Base for every operation result."""

    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Policy read
# ---------------------------------------------------------------------------
class PolicySummary(OperationResult):
    """Compact policy projection used in list responses and search results."""

    policyId: str = Field(description="Carrier policy number.", examples=["POL1001"])
    productCode: str = Field(description="Product code the policy was issued on.", examples=["TERM20-A"])
    status: str = Field(description="Canonical policy status.", examples=["active"])
    stateOfIssue: str = Field(min_length=2, max_length=2, description="Issuing state code.", examples=["NY"])
    faceAmount: Money = Field(description="Aggregate face amount / accumulation base.")
    currency: Currency = Field(default="USD")
    annualisedPremium: Money = Field(default=0.0, description="Annualised premium in the stated currency.")
    issueDate: IsoDate | None = Field(default=None, description="Date the policy was issued.")
    effectiveDate: IsoDate | None = Field(default=None, description="Date coverage began.")


class PolicyDetail(PolicySummary):
    """Full policy record as returned by the canonical policy retrieval operation."""

    productId: str = Field(description="Internal product identifier.")
    paymentMode: str = Field(default="monthly", examples=["monthly"])
    lob: str = Field(default="Life", description="Line of business label.", examples=["Life", "Annuity"])
    masterGroup: str | None = Field(default=None, description="Master group / employer number, if any.")
    parties: list[dict[str, Any]] = Field(
        default_factory=list, description="Party role assignments (insured, owner, beneficiary, payor)."
    )
    coverages: list[dict[str, Any]] = Field(default_factory=list, description="Coverage and rider schedules.")
    events: list[dict[str, Any]] = Field(default_factory=list, description="Policy lifecycle events.")


class PolicySearchRequest(OperationModel):
    """Search for policies within the caller's tenant."""

    status: str | None = Field(
        default=None,
        description="Optional canonical status filter, e.g. 'active', 'lapsed', 'quoted'.",
        examples=["active"],
    )
    productCode: str | None = Field(default=None, description="Optional product code filter.")
    stateOfIssue: str | None = Field(default=None, min_length=2, max_length=2, description="State filter.")
    updatedSince: IsoDate | None = Field(default=None, description="Only policies changed on or after this date.")
    limit: int = Field(default=25, ge=1, le=200, description="Maximum rows to return.")
    offset: int = Field(default=0, ge=0, description="Rows to skip, for pagination.")

    @field_validator("productCode")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.upper() if value else value

    @field_validator("stateOfIssue")
    @classmethod
    def _state_upper(cls, value: str | None) -> str | None:
        return value.upper() if value else value


class PolicySearchResponse(OperationResult):
    """A page of policy summaries."""

    items: list[PolicySummary] = Field(description="Matching policies.")
    totalCount: int = Field(ge=0, description="Total matches before pagination.")
    limit: int = Field(description="Page size that was applied.")
    offset: int = Field(description="Offset that was applied.")


# ---------------------------------------------------------------------------
# Rating / quote
# ---------------------------------------------------------------------------
class RatingFactors(OperationModel):
    """The factors that produced a premium, so an underwriter can challenge them."""

    baseRatePerThousand: float | None = Field(
        default=None, description="Base rate per 1,000 units of face amount."
    )
    mortalityMultiplier: float | None = Field(default=None, description="Mortality load by age/sex.")
    tobaccoFactor: float | None = Field(default=None, description="Tobacco use load.")
    classWidthMultiplier: float | None = Field(default=None, description="Rate-class width.")
    policyFee: Money | None = Field(default=None, description="Flat policy fee component.")
    riderCharges: Money | None = Field(default=None, description="Total rider premium.")


class PremiumCalculationRequest(OperationModel):
    """Price a hypothetical policy. Pure computation: nothing is written."""

    productCode: str = Field(min_length=1, max_length=30, description="Product code to rate.", examples=["TERM20-A"])
    faceAmount: PositiveMoney = Field(description="Face amount / accumulation base to rate.", examples=[250000])
    issueAge: int = Field(ge=0, le=120, description="Issue age of the insured in years.", examples=[45])
    sex: Literal["M", "F", "U"] = Field(default="U", description="Sex for mortality-rated products.")
    tobacco: bool = Field(default=False, description="Tobacco use in the last 12 months.")
    termYears: int | None = Field(default=None, ge=1, le=80, description="Term in years, if the product is term.")
    paymentMode: Literal["monthly", "quarterly", "semiAnnual", "annual", "single"] = Field(
        default="monthly", description="Premium payment frequency."
    )
    stateOfIssue: str = Field(min_length=2, max_length=2, examples=["NY"], description="State where the policy would be issued.")
    ratingClass: str | None = Field(default=None, description="Requested rate class, if known.")
    asOfDate: IsoDate | None = Field(default=None, description="Effective date of the rating.")
    riders: list[str] = Field(default_factory=list, description="Rider codes to include in the rating.")

    @field_validator("stateOfIssue")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @field_validator("productCode")
    @classmethod
    def _product_upper(cls, value: str) -> str:
        return value.upper()


class PremiumCalculationResponse(OperationResult):
    """The calculated premium with its full rating-factor breakdown."""

    annualisedPremium: PositiveMoney = Field(description="Annualised premium.")
    periodicPremium: Money = Field(description="Premium per the requested payment frequency.")
    paymentMode: str = Field(description="Payment frequency the periodic premium is quoted for.")
    currency: Currency = Field(default="USD")
    guaranteedPeriodYears: int | None = Field(default=None, description="Level premium guarantee period.")
    ratingFactors: RatingFactors = Field(default_factory=RatingFactors)
    calculationVersion: str = Field(description="Version of the rating logic that produced this result.")


class QuoteRequest(OperationModel):
    """Create a persistable quote that a bind can reference."""

    productCode: str = Field(min_length=1, max_length=30, examples=["TERM20-A"])
    faceAmount: PositiveMoney = Field(description="Face amount to quote.", examples=[250000])
    issueAge: int = Field(ge=0, le=120, examples=[45])
    sex: Literal["M", "F", "U"] = "U"
    tobacco: bool = False
    termYears: int | None = Field(default=None, ge=1, le=80)
    paymentMode: Literal["monthly", "quarterly", "semiAnnual", "annual", "single"] = "monthly"
    stateOfIssue: str = Field(min_length=2, max_length=2, description="Issuing state.", examples=["NY"])
    applicant: dict[str, Any] | None = Field(default=None, description="Applicant details as an NGDS party object.")
    riders: list[str] = Field(default_factory=list)
    asOfDate: IsoDate | None = None
    quoteValidDays: int = Field(default=30, ge=1, le=180, description="How long the quote remains bindable.")

    @field_validator("stateOfIssue", "productCode")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()


class Quote(OperationResult):
    """A persisted quote."""

    quoteId: str = Field(description="Quote identifier to pass to the bind operation.", examples=["QT7F3A9B21C"])
    productCode: str
    status: Literal["quoted", "accepted", "expired", "converted", "withdrawn"]
    quotedAt: str = Field(description="ISO-8601 timestamp of when the quote was created.")
    expiresAt: str = Field(description="ISO-8601 timestamp after which the quote cannot be bound.")
    annualisedPremium: Money = 0.0
    periodicPremium: Money = 0.0
    currency: Currency = "USD"
    paymentMode: str = "monthly"
    faceAmount: Money = 0.0
    quoteValidDays: int = 30


class BindRequest(OperationModel):
    """Convert a quote into an issued policy."""

    quoteId: str = Field(min_length=1, description="Quote to bind.", examples=["QT7F3A9B21C"])
    policyId: str | None = Field(
        default=None,
        description="Requested policy number. Omit to let the PAS allocate one.",
        pattern=r"^[A-Z0-9][A-Z0-9\-/]{2,34}$",
    )
    effectiveDate: IsoDate = Field(description="Date coverage should begin (ISO-8601).", examples=["2026-06-01"])
    payor: Literal["applicant", "owner", "insured", "thirdParty"] = "applicant"
    firstPremiumAmount: Money | None = Field(
        default=None, description="Initial premium to collect with the bind. Omit to bill per schedule."
    )
    deliveryMethod: Literal["electronic", "paper", "agent"] = "electronic"
    riders: list[str] = Field(default_factory=list)
    beneficiaryAllocations: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Beneficiary shares; must total 100 percent when supplied.",
    )
    requireSignedIllustration: bool = Field(
        default=False, description="Refuse the bind unless a signed illustration is on file."
    )

    @field_validator("policyId")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.upper() if value else value

    @model_validator(mode="after")
    def _allocations_total(self) -> BindRequest:
        if self.beneficiaryAllocations:
            total = round(
                sum(float(a.get("sharePercent", 0)) for a in self.beneficiaryAllocations), 2
            )
            if total > 100.0001:
                msg = f"Beneficiary shares total {total}%, which exceeds 100%"
                raise ValueError(msg)
        return self


class BindResponse(OperationResult):
    """Result of a bind."""

    policyId: str = Field(description="The issued policy number.")
    status: Literal["accepted", "issued", "pending", "declined"]
    issuedAt: IsoDate | None = None
    effectiveDate: IsoDate | None = None
    annualisedPremium: Money = 0.0
    firstPremiumAmount: Money = 0.0
    currency: Currency = "USD"


# ---------------------------------------------------------------------------
# Policy status and servicing
# ---------------------------------------------------------------------------
class PolicyStatusResponse(OperationResult):
    """The current canonical status of a policy."""

    policyId: str
    status: str = Field(description="Canonical status: active, lapsed, paid_up, surrendered, ...")
    asOfDate: IsoDate = Field(description="Effective date of the status read.")
    statusChangedDate: IsoDate | None = None


class ServicingRequest(OperationModel):
    """Shared payload for lapse, reinstate, surrender and terminate."""

    effectiveDate: IsoDate | None = Field(default=None, examples=["2026-06-01"], description="Effective date of the change.")
    reason: str | None = Field(default=None, max_length=200, description="Free-text reason for the change.")
    reasonCode: str | None = Field(
        default=None, description="Vendor reason code when the carrier requires one."
    )
    confirmation: bool = Field(
        default=False,
        examples=[True],
        description="Must be true. Acknowledges the caller intends a policy-state change.",
    )

    @model_validator(mode="after")
    def _require_confirmation(self) -> ServicingRequest:
        if not self.confirmation:
            msg = "Set confirmation=true to execute a policy-state change"
            raise ValueError(msg)
        return self


class PolicyStatusChangeResponse(OperationResult):
    """Result of a policy-state change."""

    policyId: str
    previousStatus: str | None = None
    status: str = Field(description="Status after the change.")
    effectiveDate: IsoDate | None = None
    reference: str | None = Field(default=None, description="Vendor reference for the transaction.")


# ---------------------------------------------------------------------------
# Beneficiary
# ---------------------------------------------------------------------------
class BeneficiaryAllocation(OperationModel):
    """One beneficiary share."""

    partyId: str = Field(min_length=3, examples=["PARTY001"], description="Party identifier of the beneficiary.")
    relationship: Literal[
        "beneficiary", "contingentBeneficiary", "spouse", "child", "parent", "trust", "estate"
    ] = Field(description="Relationship to the insured.")
    sharePercent: float = Field(ge=0, le=100, examples=[60.0], description="Share of the death benefit.")
    isPrimary: bool = Field(default=False, description="Whether this is a primary beneficiary.")

    @model_validator(mode="after")
    def _primary_implies_share(self) -> BeneficiaryAllocation:
        if self.isPrimary and self.sharePercent <= 0:
            msg = "a primary beneficiary must have a share greater than zero"
            raise ValueError(msg)
        return self


class BeneficiaryUpdateRequest(OperationModel):
    """Replace the beneficiary schedule on an in-force policy."""

    allocations: list[BeneficiaryAllocation] = Field(
        min_length=1, description="Complete replacement schedule, not a delta."
    )
    effectiveDate: IsoDate | None = None
    requireAllSignatures: bool = Field(
        default=True,
        description="Require every allocated party to counter-sign before the change applies.",
    )
    reason: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _shares_total_100(self) -> BeneficiaryUpdateRequest:
        total = round(sum(a.sharePercent for a in self.allocations), 2)
        if total > 100.0001:
            msg = f"Beneficiary shares total {total}%; they may not exceed 100%"
            raise ValueError(msg)
        if len(self.allocations) > 10:
            msg = "A policy may have at most 10 beneficiaries"
            raise ValueError(msg)
        return self


class BeneficiaryUpdateResponse(OperationResult):
    """Result of a beneficiary change."""

    policyId: str
    totalPercent: float = Field(description="Total allocated percentage.")
    count: int = Field(description="Number of beneficiaries allocated.")
    effectiveDate: IsoDate | None = None
    signaturesPending: int = Field(default=0, ge=0, description="Allocations still awaiting a signature.")


# ---------------------------------------------------------------------------
# Cash value and loans
# ---------------------------------------------------------------------------
class CashValueRequest(OperationModel):
    """Projected cash value for a policy."""

    asOfDate: IsoDate | None = Field(default=None, description="Projection date; defaults to today.")
    projectionYears: int | None = Field(default=None, ge=0, le=50, description="Years to project forward.")


class CashValueResponse(OperationResult):
    """Cash, surrender and net cash values."""

    policyId: str
    asOfDate: IsoDate
    cashValue: Money = Field(description="Cash surrender value before any surrender charge.")
    surrenderValue: Money = Field(description="Gross surrender value including the surrender charge benefit.")
    netCashValue: Money = Field(description="Cash value less any outstanding policy loan balance.")
    currency: Currency = "USD"
    projection: list[dict[str, Any]] = Field(
        default_factory=list, description="Yearly projection when projectionYears was requested."
    )


class PolicyLoanRequest(OperationModel):
    """Request a policy loan or advance."""

    amount: PositiveMoney = Field(examples=[25000.0], description="Amount to borrow; must not exceed the available cash value.")
    reason: str | None = Field(default=None, max_length=200)
    repaymentPlan: Literal["interestOnly", "level", "none"] = Field(
        default="interestOnly", description="How the loan will be repaid."
    )


class PolicyLoanResponse(OperationResult):
    """Policy loan decision."""

    policyId: str
    loanId: str
    amount: Money
    status: Literal["approved", "pending", "declined"]
    interestRate: float | None = Field(default=None, description="Contractual loan interest rate.")
    currency: Currency = "USD"


# ---------------------------------------------------------------------------
# Applications (underwriting entry points)
# ---------------------------------------------------------------------------
class ApplicationSubmitRequest(OperationModel):
    """Submit an application for underwriting."""

    productCode: str = Field(min_length=1, max_length=30, examples=["TERM20-A"])
    applicant: dict[str, Any] = Field(
        description="Applicant as an ACORD NGDS party object.",
        examples=[{
            "PartyOccurrenceID": "PARTY001",
            "Person": {
                "FirstName": "Jane",
                "LastName": "Doe",
                "BirthDate": "1985-04-12",
                "GenderCode": "F",
            },
            "Address": {
                "AddressLine1": "1 Main Street",
                "AddressCity": "New York",
                "AddressStateCode": "NY",
                "PostalCode": "10001",
            },
        }],
    )
    proposedInsured: dict[str, Any] | None = Field(default=None, description="Proposed insured, if different.")
    requestedFaceAmount: PositiveMoney = Field(examples=[250000])
    stateOfIssue: str = Field(min_length=2, max_length=2, examples=["NY"])
    agentId: str | None = Field(default=None, description="Capturing agent identifier.")
    distributionChannel: Literal["agent", "direct", "embedded", "broker", "digital"] = "direct"
    attachments: list[dict[str, Any]] = Field(
        default_factory=list, description="Document references (uri, mimeType, kind)."
    )

    @field_validator("stateOfIssue", "productCode")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()


class ApplicationSubmitResponse(OperationResult):
    """Acknowledgement of a submitted application."""

    applicationId: str
    status: str
    receivedAt: str
    outstandingRequirements: list[str] = Field(default_factory=list)


class ApplicationStatusResponse(OperationResult):
    """Current state of an application."""

    applicationId: str
    status: Literal["submitted", "triage", "underwriting", "referred", "approved", "declined", "withdrawn"]
    receivedAt: str | None = None
    requirements: list[dict[str, Any]] = Field(
        default_factory=list, description="Outstanding evidence requirements."
    )
    decision: str | None = Field(default=None)
    reasonCodes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Product catalogue
# ---------------------------------------------------------------------------
class ProductSearchResponse(OperationResult):
    """A page of products."""

    items: list[dict[str, Any]]
    totalCount: int = Field(ge=0)


class ProductDetail(OperationResult):
    """A product definition as exposed through the gateway."""

    productId: str
    productCode: str
    productName: str
    productType: str = Field(description="term, wholeLife, indexedUniversalLife, fixedIndexedAnnuity, ...")
    status: Literal["draft", "inForce", "withdrawn"]
    issueAgeMinimum: int | None = None
    issueAgeMaximum: int | None = None
    minimumFaceAmount: Money | None = None
    maximumFaceAmount: Money | None = None
    premiumModes: list[str] = Field(default_factory=list)
    riders: list[dict[str, Any]] = Field(default_factory=list)
    filedStates: list[str] = Field(default_factory=list)
    version: int = 1


# ---------------------------------------------------------------------------
# Generic
# ---------------------------------------------------------------------------
class HealthResponse(OperationResult):
    """Result of a connectivity probe."""

    vendor: str
    status: str
    policyCount: int | None = None
    transport: str | None = None
