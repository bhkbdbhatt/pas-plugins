"""External data enrichment: MIB, prescription, health signals.

Every source here is consent-gated, and the gate is enforced in code rather than
documented and hoped for. A missing consent record means the signal is *absent*,
not "assumed negative" - assuming no MIB record exists when consent was never
obtained would be a fair-decisioning violation, so it produces an explicit
`consentMissing` signal that routes the case to a human.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from pas_plugins.plugin3_auw.models import (
    MedicalRecord,
    MibRecord,
    MibStatus,
    ParamedExamStatus,
    PrescriptionRecord,
    ScreeningRecord,
    Submission,
)


@dataclass(slots=True)
class EnrichmentResult:
    """What enrichment produced, and what it was not allowed to produce."""

    mib: MibRecord | None = None
    prescription: PrescriptionRecord | None = None
    medical: MedicalRecord | None = None
    screening: ScreeningRecord | None = None
    sources_used: list[str] = field(default_factory=list)
    signals: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    enriched_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def consent_gaps(self) -> list[str]:
        gaps: list[str] = []
        if self.mib is None:
            gaps.append("mibConsentMissing")
        if self.prescription is None:
            gaps.append("prescriptionConsentMissing")
        return gaps

    def to_dict(self) -> dict[str, object]:
        return {
            "mib": self.mib.model_dump(mode="json", by_alias=True) if self.mib else None,
            "prescription": (
                self.prescription.model_dump(mode="json", by_alias=True) if self.prescription else None
            ),
            "medical": self.medical.model_dump(mode="json", by_alias=True) if self.medical else None,
            "screening": self.screening.model_dump(mode="json", by_alias=True) if self.screening else None,
            "sourcesUsed": self.sources_used,
            "signals": {k: round(v, 4) for k, v in self.signals.items()},
            "warnings": self.warnings,
            "consentGaps": self.consent_gaps,
            "enrichedAt": self.enriched_at.isoformat(),
        }


class EnrichmentEngine:
    """Resolves external signals for a submission.

    The deterministic simulator below stands in for the licensed data vendors a
    carrier actually buys (MIB, pharmacy benefit, APS). It produces stable,
    explainable output for a given submission so the workbench is demonstrable and
    testable end to end, and the vendor call sites are isolated so a real adapter
    replaces them one method at a time.
    """

    def __init__(self, *, mib_enabled: bool = True, pharmacy_enabled: bool = True) -> None:
        self._mib_enabled = mib_enabled
        self._pharmacy_enabled = pharmacy_enabled

    def enrich(self, submission: Submission) -> EnrichmentResult:
        result = EnrichmentResult()
        if self._mib_enabled:
            result.mib = self._lookup_mib(submission)
            if result.mib:
                result.sources_used.append("mib")
        if self._pharmacy_enabled:
            result.prescription = self._lookup_prescription(submission)
            if result.prescription:
                result.sources_used.append("pharmacyBenefit")
        result.medical = self._build_medical(submission)
        result.screening = self._screen(submission)
        result.signals = self._derive_signals(submission, result)
        result.warnings = self._warnings(submission, result)
        return result

    def _lookup_mib(self, submission: Submission) -> MibRecord | None:
        if submission.mib is not None:
            return submission.mib
        if not submission.mib_consent:
            return None
        roll = _stable_unit(submission.submission_id, submission.applicant.state_of_residence, "mib")
        if roll > 0.94:
            status = MibStatus.CONTESTED
        elif roll > 0.86:
            status = MibStatus.UPHELD
        elif roll > 0.76:
            status = MibStatus.STANDARD_RATES
        elif roll > 0.64:
            status = MibStatus.INSUREABLE
        else:
            status = MibStatus.CLEAR
        subjects: list[str] = []
        if status.is_decline_trigger:
            subjects = _mib_subjects(roll)
        return MibRecord(
            review_status=status,
            lookup_date=date.today(),
            consent_obtained=True,
            subjects=subjects,
        )

    def _lookup_prescription(self, submission: Submission) -> PrescriptionRecord | None:
        if submission.prescription is not None:
            return submission.prescription
        if not submission.prescription_consent:
            return None
        applicant = submission.applicant
        roll = _stable_unit(submission.submission_id, submission.submission_id, "rx")
        script_count = 0
        conditions: list[str] = []
        if applicant.tobacco_use:
            conditions.append("respiratory")
        if (applicant.bmi or 0) >= 32:
            conditions.append("metabolic")
        if applicant.age >= 55:
            conditions.append("cardiovascularRisk")
        if roll > 0.6:
            script_count = int(roll * 9)
        if roll > 0.92:
            script_count += 2
        controlled = 1 if roll > 0.965 else 0
        adherence = round(max(0.4, 1.0 - roll * 0.5), 2)
        return PrescriptionRecord(
            active_conditions=sorted(set(conditions)),
            script_count_12m=script_count,
            controlled_substance_count=controlled,
            refill_adherence=adherence,
            consent_obtained=True,
            source="pharmacyBenefitSimulator",
        )

    def _build_medical(self, submission: Submission) -> MedicalRecord:
        if submission.medical is not None:
            return submission.medical
        applicant = submission.applicant
        status = ParamedExamStatus.NONE
        if submission.request.face_amount >= 1_000_000 or applicant.age >= 60:
            status = ParamedExamStatus.REQUIRED
        elif submission.request.expedited:
            status = ParamedExamStatus.WAIVED
        abnormalities: list[str] = []
        bmi = applicant.bmi
        if bmi is not None and bmi >= 30:
            abnormalities.append("elevatedBmi")
        if applicant.tobacco_use:
            abnormalities.append("tobaccoUseDocumented")
        conditions: list[str] = []
        if applicant.age >= 60:
            conditions.append("ageRelatedReview")
        if applicant.family_history_early_death:
            conditions.append("familyHistory")
        return MedicalRecord(
            paramed_exam_status=status,
            attending_physician_statement=False,
            lab_abnormalities=abnormalities,
            diagnosed_conditions=conditions,
            bmi_from_visit=bmi,
        )

    def _screen(self, submission: Submission) -> ScreeningRecord:
        base = submission.screening
        sanctions = base.sanctions_match or _stable_unit(submission.submission_id, "ofac", "sanctions") > 0.985
        pep = base.pep_match or _stable_unit(submission.submission_id, "pep", "pep") > 0.99
        media = base.adverse_media_count
        if _stable_unit(submission.submission_id, "media", "media") > 0.75:
            media += 1
        indicators = list(base.fraud_indicators)
        if sanctions:
            indicators.append("sanctionsListMatch")
        if pep:
            indicators.append("politicallyExposedPerson")
        return ScreeningRecord(
            sanctions_match=sanctions,
            pep_match=pep,
            adverse_media_count=media,
            fraud_indicators=sorted(set(indicators)),
            screened_at=datetime.now(UTC),
        )

    @staticmethod
    def _derive_signals(submission: Submission, result: EnrichmentResult) -> dict[str, float]:
        """Collapse enrichment into the normalised features the model consumes."""
        applicant = submission.applicant
        signals: dict[str, float] = {
            "mibAdverse": 1.0 if (result.mib and result.mib.review_status.is_decline_trigger) else 0.0,
            "mibElevated": 1.0 if (result.mib and result.mib.review_status in {MibStatus.STANDARD_RATES, MibStatus.INSUREABLE}) else 0.0,
            "rxScriptVolume": min((result.prescription.script_count_12m if result.prescription else 0) / 12.0, 1.0),
            "rxPoorAdherence": 1.0 if (result.prescription and result.prescription.refill_adherence < 0.7) else 0.0,
            "paramedOutstanding": 1.0
            if (result.medical and result.medical.paramed_exam_status is ParamedExamStatus.REQUIRED)
            else 0.0,
            "sanctionsMatch": 1.0 if (result.screening and result.screening.sanctions_match) else 0.0,
            "pepMatch": 1.0 if (result.screening and result.screening.pep_match) else 0.0,
            "tobacco": 1.0 if applicant.tobacco_use else 0.0,
            "bmi": _clip((applicant.bmi or 24.0) / 45.0),
            "hazard": _clip(applicant.avocation.hazard_multiplier / 2.6),
        }
        return signals

    @staticmethod
    def _warnings(submission: Submission, result: EnrichmentResult) -> list[str]:
        warnings: list[str] = []
        if not submission.mib_consent and submission.mib is None:
            warnings.append("mibLookupSkippedNoConsent")
        if not submission.prescription_consent and submission.prescription is None:
            warnings.append("prescriptionLookupSkippedNoConsent")
        if result.screening and result.screening.sanctions_match:
            warnings.append("sanctionsMatchRequiresComplianceReview")
        if result.mib and result.mib.review_status.is_decline_trigger:
            warnings.append("mibDecisionContestable")
        return warnings


def _stable_unit(*parts: str) -> float:
    """A deterministic float in [0, 1) derived from the given parts.

    Determinism matters more than realism here: the same submission must produce
    the same enrichment on every run, or the workbench's explanations stop being
    reproducible and cannot be tested.
    """
    digest = hashlib.sha256("|".join(parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def _clip(value: float) -> float:
    return max(0.0, min(1.0, value))


def _mib_subjects(roll: float) -> list[str]:
    """Pick a deterministic, non-empty subset of MIB adverse subjects."""
    candidates = ["build", "avocation", "financial", "drivingRecord", "health", "alcohol"]
    count = 1 + int(roll * 100) % 3
    offset = int(roll * 1000) % len(candidates)
    rotated = candidates[offset:] + candidates[:offset]
    return sorted(rotated[:count])


__all__ = ["EnrichmentEngine", "EnrichmentResult"]