"""Underwriting domain models.

The vocabulary here is the one an underwriter actually uses: face amount, band,
build, avocation, MIB status, paramed exam, and the three decisions - accept,
refer, decline. Anything that would not survive being read aloud in an
underwriting meeting does not belong in these models.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pas_core.pas.vendors import to_camel


class Model(BaseModel):
    """Base for underwriting models: snake_case in Python, camelCase on the wire."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        alias_generator=to_camel,
        populate_by_name=True,
    )


class Decision(StrEnum):
    """The three outcomes. Every decision path must land on one of these."""

    ACCEPT = "accept"
    REFER = "refer"
    DECLINE = "decline"
    PENDING = "pending"

    @property
    def is_final(self) -> bool:
        return self in {Decision.ACCEPT, Decision.DECLINE}


class DecisionSource(StrEnum):
    """Where a decision came from - audited, never mixed."""

    AUTO_RULE = "autoRule"
    AUTO_MODEL = "autoModel"
    UNDERWRITER = "underwriter"
    SUPERVISOR_OVERRIDE = "supervisorOverride"
    CARRIER_RULES = "carrierRules"


class ComplexityTier(StrEnum):
    """Where a submission lands in the triage funnel."""

    AUTO = "auto"
    """Simple enough to decide automatically."""
    FAST_TRACK = "fastTrack"
    """Straightforward; an agent can issue without underwriter review."""
    REFER = "refer"
    """Needs a human underwriter."""
    SPECIALIST = "specialist"
    """Medical or financial underwriter required."""

    @property
    def requires_human(self) -> bool:
        return self in {ComplexityTier.REFER, ComplexityTier.SPECIALIST}


class Build(StrEnum):
    """Physical build, a standard field on life applications."""

    EXCELLENT = "excellent"
    GOOD = "good"
    AVERAGE = "average"
    BELOW_AVERAGE = "belowAverage"
    POOR = "poor"


class Avocation(StrEnum):
    """Occupational hazard class."""

    CLASS_1 = "class1"
    """Office, professional - lowest hazard."""
    CLASS_2 = "class2"
    CLASS_3 = "class3"
    CLASS_4 = "class4"
    CLASS_5 = "class5"
    """Manual, hazardous - highest."""

    @property
    def hazard_multiplier(self) -> float:
        return {"class1": 1.0, "class2": 1.15, "class3": 1.4, "class4": 1.85, "class5": 2.6}[str(self.value)]


class MibStatus(StrEnum):
    """MIB Insureability Review outcome."""

    CLEAR = "clear"
    INSUREABLE = "insureable"
    STANDARD_RATES = "standardRates"
    CONTESTED = "contested"
    UPHELD = "upheld"
    NOT_FOUND = "notFound"

    @property
    def is_decline_trigger(self) -> bool:
        return self in {MibStatus.CONTESTED, MibStatus.UPHELD}


class ParamedExamStatus(StrEnum):
    NONE = "none"
    REQUIRED = "required"
    COMPLETE = "complete"
    FAILED = "failed"
    WAIVED = "waived"


class FieldProvenance(StrEnum):
    """How an extracted field came to be believed.

    Provenance is not decoration: a field an underwriter typed is treated
    differently from a field a document-extraction model guessed at low
    confidence, and the workbench shows that difference.
    """

    APPLICANT_DECLARED = "applicantDeclared"
    AGENT_ENTERED = "agentEntered"
    DOCUMENT_EXTRACTED = "documentExtracted"
    ENRICHMENT_SOURCE = "enrichmentSource"
    PAS_RECORD = "pasRecord"
    DERIVED = "derived"
    MANUAL_OVERRIDE = "manualOverride"


class Class(Model):  # noqa: N801 - insurance domain term
    """A rating or underwriting class assigned to a submission."""

    code: str = Field(min_length=1, max_length=20, description="Class code, e.g. 'A'.")
    basis_points: int = Field(
        default=0, ge=-5000, le=5000,
        description="Rate allowance or load in basis points (100bp = 1%).",
    )
    source: DecisionSource = DecisionSource.AUTO_MODEL
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "basisPoints": self.basis_points,
            "source": str(self.source), "rationale": self.rationale,
        }


class Applicant(Model):
    """The proposed insured, as the workbench understands them."""

    age: int = Field(ge=0, le=120, description="Attained age in years.")
    sex: str = Field(default="U", pattern=r"^[MFUX]$")
    tobacco_use: bool = Field(default=False, description="Tobacco or nicotine in the last 12 months.")
    tobacco_type: str | None = Field(default=None, max_length=20)
    build: Build = Build.AVERAGE
    avocation: Avocation = Avocation.CLASS_2
    height_inches: int | None = Field(default=None, ge=36, le=84)
    weight_pounds: int | None = Field(default=None, ge=60, le=600)
    state_of_residence: str = Field(pattern=r"^[A-Z]{2}$", examples=["NY"])
    marital_status: str | None = Field(default=None, max_length=20)
    dependents: int = Field(default=0, ge=0, le=30)
    annual_income: float = Field(default=0.0, ge=0.0)
    net_worth: float = Field(default=0.0, ge=0.0)
    prior_policy_count: int = Field(default=0, ge=0)
    bankruptcy_last_years: int = Field(default=0, ge=0, le=30)
    family_history_early_death: bool = Field(default=False)
    felony_conviction: bool = Field(default=False)
    hazardous_sports: bool = Field(default=False)

    @property
    def bmi(self) -> float | None:
        if not self.height_inches or not self.weight_pounds:
            return None
        return round(703 * self.weight_pounds / (self.height_inches ** 2), 1)

    @property
    def bmi_band(self) -> str:
        value = self.bmi
        if value is None:
            return "unknown"
        if value < 18.5:
            return "underweight"
        if value < 25:
            return "healthy"
        if value < 30:
            return "overweight"
        if value < 35:
            return "obese1"
        if value < 40:
            return "obese2"
        return "obese3"

    @field_validator("state_of_residence", mode="before")
    @classmethod
    def _upper(cls, value: Any) -> Any:
        """Normalise before the pattern check so 'ny' is accepted as 'NY'."""
        return value.strip().upper() if isinstance(value, str) else value


class Request(Model):
    """What is being asked for."""

    product_code: str = Field(min_length=1, max_length=30, examples=["TERM20-A"])
    face_amount: float = Field(ge=0.0, examples=[250_000])
    term_years: int | None = Field(default=None, ge=1, le=80)
    payment_mode: str = Field(default="monthly", max_length=20)
    coverage_type: str = Field(default="term", max_length=40)
    expedited: bool = Field(default=False)
    is_replacement: bool = Field(default=False)
    existing_carrier: str | None = Field(default=None, max_length=60)

    @model_validator(mode="after")
    def _replacement_requires_existing(self) -> Request:
        if self.is_replacement and not self.existing_carrier:
            msg = "a replacement application must name the existing carrier"
            raise ValueError(msg)
        return self


class MibRecord(Model):
    """An MIB Insureability Review result. Consent-gated."""

    review_status: MibStatus = MibStatus.CLEAR
    lookup_date: date | None = None
    consent_obtained: bool = Field(
        default=False, description="Whether the applicant consented to an MIB lookup."
    )
    subjects: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consent_required(self) -> MibRecord:
        if self.review_status is not MibStatus.NOT_FOUND and not self.consent_obtained:
            msg = "an MIB result cannot be recorded without the applicant's consent"
            raise ValueError(msg)
        return self


class PrescriptionRecord(Model):
    """Aggregated prescription signals - deliberately aggregate, never a drug list."""

    active_conditions: list[str] = Field(
        default_factory=list, description="Condition categories, not individual drugs."
    )
    script_count_12m: int = Field(default=0, ge=0)
    controlled_substance_count: int = Field(default=0, ge=0)
    refill_adherence: float = Field(default=1.0, ge=0.0, le=1.0)
    consent_obtained: bool = False
    source: str = "unknown"


class MedicalRecord(Model):
    """Medical evidence summary."""

    paramed_exam_status: ParamedExamStatus = ParamedExamStatus.NONE
    attending_physician_statement: bool = False
    lab_abnormalities: list[str] = Field(default_factory=list)
    diagnosed_conditions: list[str] = Field(default_factory=list)
    bmi_from_visit: float | None = None
    notes: list[str] = Field(default_factory=list)


class ScreeningRecord(Model):
    """Sanctions, PEP and fraud screening."""

    sanctions_match: bool = False
    pep_match: bool = False
    adverse_media_count: int = Field(default=0, ge=0)
    fraud_indicators: list[str] = Field(default_factory=list)
    screened_at: datetime | None = None


class ExtractedField(Model):
    """One field read out of a document, with its provenance and confidence."""

    field: str = Field(min_length=1, description="Dotted path of the field.")
    value: Any = None
    confidence: float = Field(ge=0.0, le=1.0)
    provenance: FieldProvenance = FieldProvenance.DOCUMENT_EXTRACTED
    source_document: str | None = Field(default=None, max_length=200)
    page: int | None = Field(default=None, ge=1)
    bounding_box: tuple[float, float, float, float] | None = Field(
        default=None, description="(x0, y0, x1, y1) in page coordinates."
    )

    @model_validator(mode="after")
    def _extraction_needs_a_source(self) -> ExtractedField:
        if self.provenance is FieldProvenance.DOCUMENT_EXTRACTED and not self.source_document:
            msg = f"field '{self.field}' is marked document-extracted but names no document"
            raise ValueError(msg)
        return self


class DocumentRef(Model):
    """A submitted document."""

    document_id: str = Field(min_length=1, max_length=64)
    kind: str = Field(
        min_length=2, max_length=40,
        description="application, illustration, paramedExam, MIB, photoId, ...",
    )
    uri: str | None = Field(default=None, max_length=512)
    mime_type: str | None = Field(default=None, max_length=80)
    page_count: int = Field(default=1, ge=1, le=500)
    uploaded_at: datetime | None = None


class Submission(Model):
    """An underwriting submission as the workbench receives it."""

    submission_id: str = Field(min_length=6, max_length=64, examples=["SUB20260001"])
    tenant_id: str = Field(min_length=3, max_length=64)
    applicant: Applicant
    request: Request
    channel: str = Field(default="api", max_length=40)
    agent_id: str | None = Field(default=None, max_length=64)
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    documents: list[DocumentRef] = Field(default_factory=list)
    mib_consent: bool = Field(
        default=False, description="Applicant consented to an MIB Insureability Review lookup."
    )
    prescription_consent: bool = Field(
        default=False, description="Applicant consented to pharmacy/prescription enrichment."
    )
    mib: MibRecord | None = None
    prescription: PrescriptionRecord | None = None
    medical: MedicalRecord | None = None
    screening: ScreeningRecord = Field(default_factory=ScreeningRecord)
    submitted_by: str = Field(default="unknown", max_length=128)

    @property
    def age_band(self) -> str:
        age = self.applicant.age
        if age < 26:
            return "18-25"
        if age < 36:
            return "26-35"
        if age < 46:
            return "36-45"
        if age < 56:
            return "46-55"
        if age < 66:
            return "56-65"
        return "66+"

    @property
    def face_amount_band(self) -> str:
        amount = self.request.face_amount
        if amount < 100_000:
            return "under100k"
        if amount < 500_000:
            return "100k-500k"
        if amount < 1_000_000:
            return "500k-1m"
        if amount < 5_000_000:
            return "1m-5m"
        return "over5m"

    @property
    def control_flags(self) -> list[str]:
        """Signals that require a human regardless of the model's score.

        A contestable decision - a decline, a sanctions match, a replacement on an
        existing policy - is never left to an automated path.
        """
        flags: list[str] = []
        if self.mib and self.mib.review_status.is_decline_trigger:
            flags.append("mibContested")
        if self.screening.sanctions_match:
            flags.append("sanctionsMatch")
        if self.screening.pep_match:
            flags.append("pepMatch")
        if self.applicant.family_history_early_death:
            flags.append("familyHistoryEarlyDeath")
        if self.request.is_replacement:
            flags.append("replacement")
        if self.request.expedited:
            flags.append("expedited")
        if self.medical and self.medical.paramed_exam_status is ParamedExamStatus.REQUIRED:
            flags.append("paramedExamOutstanding")
        return flags

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class ComplexityScore(Model):
    """Why a submission was routed where it was."""

    tier: ComplexityTier
    score: float = Field(ge=0.0, le=100.0)
    factors: dict[str, float] = Field(
        default_factory=dict, description="Named contributions to the score."
    )
    control_flags: list[str] = Field(default_factory=list)
    estimated_minutes_for_human: float = Field(default=0.0, ge=0.0)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class ScoreContribution(Model):
    """One feature's contribution to a score, in SHAP units."""

    feature: str
    value: float
    shap_value: float = Field(description="Signed contribution to the model output.")
    rank: int = Field(ge=1)

    @property
    def direction(self) -> str:
        if self.shap_value > 0:
            return "increasesRisk"
        if self.shap_value < 0:
            return "decreasesRisk"
        return "neutral"

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "value": self.value,
            "shapValue": round(self.shap_value, 6),
            "rank": self.rank,
            "direction": self.direction,
        }


class RiskScore(Model):
    """A model's assessment, with the contributions that produced it."""

    score: float = Field(ge=0.0, le=1.0, description="Probability the case is a risk-declined case.")
    model_version: str
    algorithm: str = "gradientBoostedTrees"
    contributions: list[ScoreContribution] = Field(default_factory=list)
    base_value: float = Field(default=0.5, description="Model output at the mean feature vector.")
    computed_in_ms: float = Field(default=0.0)

    @property
    def top_drivers(self) -> list[ScoreContribution]:
        return sorted(self.contributions, key=lambda c: abs(c.shap_value), reverse=True)[:5]

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 6),
            "modelVersion": self.model_version,
            "algorithm": self.algorithm,
            "baseValue": self.base_value,
            "topDrivers": [c.to_dict() for c in self.top_drivers],
            "contributions": [c.to_dict() for c in self.contributions],
            "computedInMs": round(self.computed_in_ms, 3),
        }


class RuleOutcomeLine(Model):
    """One rule's contribution to a decision, quoted verbatim for the file."""

    rule_id: str
    name: str
    outcome: str
    fired: bool
    severity: str
    reason: str = ""
    applicable: bool = Field(
        default=True,
        description="False when the rule is scoped to products this submission is not.",
    )


class DecisionRecord(Model):
    """A underwriting decision and the full basis for it."""

    decision_id: str = Field(min_length=6, max_length=64)
    submission_id: str
    tenant_id: str
    decision: Decision
    decision_source: DecisionSource = DecisionSource.AUTO_MODEL
    risk_score: float | None = Field(default=None, ge=0.0, le=1.0)
    model_version: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    message: str = Field(default="", description="Operator-facing explanation.")
    requirements: list[dict[str, Any]] = Field(
        default_factory=list, description="Outstanding evidence required before issue."
    )
    assigned_class: Class | None = None
    rule_outcomes: list[RuleOutcomeLine] = Field(default_factory=list)
    model_contributions: list[ScoreContribution] = Field(default_factory=list)
    complexity: ComplexityScore | None = None
    override: dict[str, Any] | None = None
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    decided_by: str = "system"
    duration_ms: float = 0.0

    @property
    def explainable(self) -> bool:
        """Whether a decision could be explained to a regulator.

        Every decision needs at least one reason code, and an automated decline
        needs the model's feature contributions as well - a decline is the decision
        a carrier is most likely to be challenged on.
        """
        if not self.reason_codes:
            return False
        if self.decision is Decision.DECLINE and self.decision_source is not DecisionSource.UNDERWRITER:
            return bool(self.model_contributions) or bool(
                any(r.fired for r in self.rule_outcomes)
            )
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "decisionId": self.decision_id,
            "submissionId": self.submission_id,
            "decision": str(self.decision),
            "decisionSource": str(self.decision_source),
            "riskScore": round(self.risk_score, 6) if self.risk_score is not None else None,
            "modelVersion": self.model_version,
            "reasonCodes": self.reason_codes,
            "message": self.message,
            "requirements": self.requirements,
            "assignedClass": self.assigned_class.to_dict() if self.assigned_class else None,
            "ruleOutcomes": [r.model_dump(mode="json", by_alias=True) for r in self.rule_outcomes],
            "topDrivers": [c.to_dict() for c in self.model_contributions[:5]],
            "complexity": self.complexity.to_dict() if self.complexity else None,
            "override": self.override,
            "explainable": self.explainable,
            "decidedAt": self.decided_at.isoformat(),
            "decidedBy": self.decided_by,
            "durationMs": round(self.duration_ms, 3),
        }


class OverrideRequest(Model):
    """An underwriter's override of an automated decision."""

    decision: Decision
    reason: str = Field(min_length=20, max_length=1000, description="Why the model was overruled.")
    assigned_class: Class | None = None
    follow_up_at: datetime | None = None

    @field_validator("reason")
    @classmethod
    def _substantive(cls, value: str) -> str:
        if len(value.split()) < 5:
            msg = "an override reason must be a sentence, not a token"
            raise ValueError(msg)
        return value
