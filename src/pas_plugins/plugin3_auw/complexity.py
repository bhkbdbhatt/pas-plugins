"""Submission triage: complexity scoring and routing.

Triage is where an AUW tool earns its keep. A carrier's cost is not really the
scoring - it is the 40 minutes an underwriter spends on a case that a rule could
have cleared. So complexity scoring is not "how hard is this to score", it is
"how much human judgement does this need, and who is qualified to give it".

The score is a transparent weighted sum. It is deliberately not a model: a
carrier must be able to argue with the routing, and an opaque second model
sitting in front of the underwriting model is one more thing to validate.
"""

from __future__ import annotations

from pas_plugins.plugin3_auw.models import ComplexityScore, ComplexityTier, Submission

# Weights are expressed as points out of 100. They were calibrated against the
# demo corpus and are exposed so a carrier can re-weight them per product.
_BASE_POINTS = 0.0
_MAX_SCORE = 100.0

_FACTOR_WEIGHTS: dict[str, float] = {
    "faceAmountBand": 18.0,
    "ageBand": 14.0,
    "avocationHazard": 16.0,
    "tobacco": 20.0,
    "build": 12.0,
    "medicalEvidence": 22.0,
    "prescriptionActivity": 14.0,
    "financialComplexity": 12.0,
    "controlFlags": 10.0,
    "replacement": 8.0,
    "missingEvidence": 10.0,
}

_FACE_BAND_POINTS = {"under100k": 0.0, "100k-500k": 0.4, "500k-1m": 0.7, "1m-5m": 0.9, "over5m": 1.0}
_AGE_BAND_POINTS = {"18-25": 0.5, "26-35": 0.2, "36-45": 0.2, "46-55": 0.5, "56-65": 0.9, "66+": 1.0}
_BUILD_POINTS = {"excellent": 0.0, "good": 0.1, "average": 0.4, "belowAverage": 0.8, "poor": 1.0}
_BMI_BAND_POINTS = {"underweight": 0.5, "healthy": 0.0, "overweight": 0.4, "obese1": 0.8, "obese2": 1.0, "obese3": 1.0, "unknown": 0.3}


class ComplexityEngine:
    """Scores how much human judgement a submission needs."""

    def __init__(self, weights: dict[str, float] | None = None) -> None:
        self._weights = dict(_FACTOR_WEIGHTS)
        if weights:
            self._weights.update(weights)

    def score(self, submission: Submission) -> ComplexityScore:
        """Return the complexity score and routing tier for a submission."""
        applicant = submission.applicant
        factors: dict[str, float] = {}

        factors["faceAmountBand"] = _round(_FACE_BAND_POINTS[submission.face_amount_band])
        factors["ageBand"] = _round(_AGE_BAND_POINTS[submission.age_band])
        factors["avocationHazard"] = _round(applicant.avocation.hazard_multiplier / 2.6)
        factors["tobacco"] = 1.0 if applicant.tobacco_use else 0.0
        factors["build"] = _round(max(_BUILD_POINTS[applicant.build.value], _BMI_BAND_POINTS[applicant.bmi_band]))
        factors["medicalEvidence"] = _round(self._medical_points(submission))
        factors["prescriptionActivity"] = _round(self._prescription_points(submission))
        factors["financialComplexity"] = _round(self._financial_points(submission))
        factors["controlFlags"] = _round(min(len(submission.control_flags) / 3.0, 1.0))
        factors["replacement"] = 1.0 if submission.request.is_replacement else 0.0
        factors["missingEvidence"] = _round(self._missing_evidence_points(submission))

        score = _BASE_POINTS + sum(
            self._weights[name] * value for name, value in factors.items() if name in self._weights
        )
        score = max(0.0, min(_MAX_SCORE, score))
        tier = self._tier(submission, score)
        return ComplexityScore(
            tier=tier,
            score=round(score, 2),
            factors=factors,
            control_flags=submission.control_flags,
            estimated_minutes_for_human=round(self._effort_minutes(tier, score), 1),
        )

    def _medical_points(self, submission: Submission) -> float:
        medical = submission.medical
        if not medical:
            # No medical evidence is only a routing cost when evidence is actually
            # expected. A 30-year-old applying for $250k does not owe a paramed exam,
            # and treating the absence as complexity would send every young case to a
            # human.
            if submission.request.face_amount < 1_000_000 and submission.applicant.age < 60:
                return 0.0
            return 0.3
        points = 0.0
        if medical.paramed_exam_status.value == "required":
            points += 0.5
        if medical.paramed_exam_status.value == "failed":
            points += 0.8
        if medical.attending_physician_statement:
            points += 0.3
        points += min(len(medical.diagnosed_conditions) * 0.15, 0.6)
        points += min(len(medical.lab_abnormalities) * 0.1, 0.3)
        return min(points, 1.0)

    def _prescription_points(self, submission: Submission) -> float:
        prescription = submission.prescription
        if not prescription:
            return 0.0
        points = min(prescription.script_count_12m / 12.0, 0.5)
        points += min(len(prescription.active_conditions) * 0.2, 0.6)
        points += min(prescription.controlled_substance_count * 0.25, 0.5)
        if prescription.refill_adherence < 0.7:
            points += 0.3
        return min(points, 1.0)

    def _financial_points(self, submission: Submission) -> float:
        applicant = submission.applicant
        points = 0.0
        if applicant.annual_income > 0 and submission.request.face_amount > applicant.annual_income * 20:
            points += 0.5
        if applicant.bankruptcy_last_years > 0:
            points += 0.4
        if applicant.net_worth < submission.request.face_amount * 0.5:
            points += 0.3
        if applicant.felony_conviction:
            points += 0.5
        if submission.request.is_replacement:
            points += 0.2
        return min(points, 1.0)

    def _missing_evidence_points(self, submission: Submission) -> float:
        """Missing evidence is a routing signal, not a risk signal.

        A case with no paramed exam on file may be perfectly standard, but somebody
        has to go and get it. That is workflow, and it belongs in the triage score.
        """
        points = 0.0
        kinds = {doc.kind for doc in submission.documents}
        if "application" not in kinds:
            points += 0.5
        if submission.request.face_amount >= 1_000_000 and "paramedExam" not in kinds:
            points += 0.4
        # Only a genuine consent/lookup gap counts. A consented MIB lookup that came
        # back clean is complete evidence, not missing evidence.
        if submission.mib is None and not submission.mib_consent:
            points += 0.2
        if submission.request.is_replacement and "illustration" not in kinds:
            points += 0.3
        return min(points, 1.0)

    def _tier(self, submission: Submission, score: float) -> ComplexityTier:
        applicant = submission.applicant
        if applicant.age >= 70 or applicant.family_history_early_death:
            return ComplexityTier.SPECIALIST
        if submission.screening.sanctions_match or submission.screening.pep_match:
            return ComplexityTier.SPECIALIST
        if score < 25:
            return ComplexityTier.AUTO
        if score < 50:
            return ComplexityTier.FAST_TRACK
        return ComplexityTier.REFER

    @staticmethod
    def _effort_minutes(tier: ComplexityTier, score: float) -> float:
        base = {"auto": 0.0, "fastTrack": 6.0, "refer": 28.0, "specialist": 55.0}[str(tier.value)]
        return base * (0.75 + score / 200.0)

    @property
    def weights(self) -> dict[str, float]:
        return dict(self._weights)


def _round(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 4)


def triage(submission: Submission, engine: ComplexityEngine | None = None) -> ComplexityScore:
    """Convenience wrapper for one-off triage."""
    return (engine or ComplexityEngine()).score(submission)


__all__ = ["ComplexityEngine", "triage"]