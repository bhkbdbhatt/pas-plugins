"""Appetite rules and the carrier's decision posture.

Two things decide a case, and both are the carrier's to set:

* **Appetite rules** - declarative, versioned, auditable statements of what the
  carrier will and will not write. Every rule has an id, a severity and a reason
  code, because a declined applicant is entitled to know which rule declined them.
* **The decision posture** - which of those rule outcomes may be executed by
  software and which must be signed by a human.

The distinction that matters most: a rule can *determine* an outcome without
*deciding* it. "Face amount above $5m" is a fact. Whether that fact stops the
application, routes it to a medical underwriter, or merely sets an override class
is the carrier's decision, and the engine keeps those two things apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Callable

from pas_plugins.plugin3_auw.complexity import ComplexityEngine
from pas_plugins.plugin3_auw.models import (
    Avocation,
    Build,
    ComplexityTier,
    Decision,
    ParamedExamStatus,
    RuleOutcomeLine,
    Submission,
)


@dataclass(frozen=True, slots=True)
class AppetiteRule:
    """One versioned appetite statement."""

    rule_id: str
    name: str
    severity: str
    reason_code: str
    message: str
    outcome: str
    predicate: Callable[[Submission], bool]
    product_codes: tuple[str, ...] | None = None
    version: str = "1.0.0"
    enabled: bool = True

    def applies_to(self, submission: Submission) -> bool:
        if not self.enabled:
            return False
        if self.product_codes is None:
            return True
        return submission.request.product_code.upper() in {
            code.upper() for code in self.product_codes
        }

    def evaluate(self, submission: Submission) -> RuleOutcomeLine:
        if not self.applies_to(submission):
            # A rule scoped to other products is reported as considered-and-skipped
            # rather than dropped, so the decision file shows the whole ruleset.
            return RuleOutcomeLine(
                rule_id=self.rule_id,
                name=self.name,
                outcome=self.outcome,
                fired=False,
                severity=self.severity,
                applicable=False,
            )
        fired = bool(self.predicate(submission))
        return RuleOutcomeLine(
            rule_id=self.rule_id,
            name=self.name,
            outcome=self.outcome,
            fired=fired,
            severity=self.severity,
            reason=f"{self.reason_code}: {self.message}" if fired else "",
        )


class AppetiteRuleSet:
    """An immutable, versioned collection of appetite rules."""

    def __init__(self, version: str, rules: list[AppetiteRule]) -> None:
        self.version = version
        self._rules = list(rules)
        self.published_at = datetime.now(UTC)

    @property
    def rules(self) -> list[AppetiteRule]:
        return list(self._rules)

    def evaluate(self, submission: Submission) -> list[RuleOutcomeLine]:
        """Evaluate every rule in the set, returning one line per rule.

        Non-applicable rules are not dropped - they come back with
        `applicable=False` - so the decision record shows the full ruleset that
        was considered rather than only the part that could have fired.
        """
        return [rule.evaluate(submission) for rule in self._rules]

    @property
    def rule_ids(self) -> list[str]:
        return [rule.rule_id for rule in self._rules]


def default_ruleset() -> AppetiteRuleSet:
    """A carrier-neutral default appetite set.

    These defaults are deliberately conservative and are meant to be edited by a
    carrier, not shipped as gospel. Every one carries a reason code so the decision
    can be explained to an applicant.
    """
    rules = [
        AppetiteRule(
            rule_id="APP-001",
            name="Absolute face amount ceiling",
            severity="decline",
            reason_code="FACE_AMOUNT_EXCEEDS_LIMITS",
            message="Requested face amount is above the carrier's absolute issue limit.",
            outcome="decline",
            predicate=lambda s: s.request.face_amount > 5_000_000,
        ),
        AppetiteRule(
            rule_id="APP-002",
            name="Jurisdiction eligibility",
            severity="refer",
            reason_code="JURISDICTION_NOT_LICENSED",
            message="The applicant resides in a state where the product is not licensed.",
            outcome="refer",
            predicate=lambda s: s.applicant.state_of_residence in _NON_LICENSED_STATES,
        ),
        AppetiteRule(
            rule_id="APP-003",
            name="Contestable MIB outcome",
            severity="decline",
            reason_code="MIB_DECISION_CONTESTABLE",
            message="MIB returned a contestable or upheld adverse decision.",
            outcome="decline",
            predicate=lambda s: bool(s.mib and s.mib.review_status.is_decline_trigger),
        ),
        AppetiteRule(
            rule_id="APP-004",
            name="Controlled substance signal",
            severity="refer",
            reason_code="CONTROLLED_SUBSTANCE_SIGNAL",
            message="A controlled-substance signal was returned by pharmacy enrichment.",
            outcome="refer",
            predicate=lambda s: bool(s.prescription and s.prescription.controlled_substance_count > 0),
        ),
        AppetiteRule(
            rule_id="APP-005",
            name="Hazardous avocation",
            severity="refer",
            reason_code="AVOCATION_HAZARD_CLASS_5",
            message="Occupational hazard class requires underwriter review.",
            outcome="refer",
            predicate=lambda s: s.applicant.avocation is Avocation.CLASS_5,
        ),
        AppetiteRule(
            rule_id="APP-006",
            name="Poor build",
            severity="refer",
            reason_code="BUILD_BELOW_STANDARD",
            message="Build is poor on the application rating chart.",
            outcome="refer",
            predicate=lambda s: s.applicant.build is Build.POOR,
        ),
        AppetiteRule(
            rule_id="APP-007",
            name="Tobacco above age band",
            severity="refer",
            reason_code="TOBACCO_AGE_BAND",
            message="Tobacco use with an age band that requires a tobacco class.",
            outcome="refer",
            predicate=lambda s: s.applicant.tobacco_use and s.applicant.age >= 45,
        ),
        AppetiteRule(
            rule_id="APP-008",
            name="Sanctions or PEP match",
            severity="refer",
            reason_code="SANCTIONS_OR_PEP_MATCH",
            message="Sanctions or politically-exposed-person screening requires compliance review.",
            outcome="refer",
            predicate=lambda s: s.screening.sanctions_match or s.screening.pep_match,
        ),
        AppetiteRule(
            rule_id="APP-009",
            name="Substandard BMI",
            severity="refer",
            reason_code="BMI_ABOVE_40",
            message="BMI is above the automatic issue threshold.",
            outcome="refer",
            predicate=lambda s: (s.applicant.bmi or 0) > 40.0,
        ),
        AppetiteRule(
            rule_id="APP-010",
            name="Felony conviction",
            severity="refer",
            reason_code="FELONY_CONVICTION_REVIEW",
            message="A felony conviction requires manual underwriting review.",
            outcome="refer",
            predicate=lambda s: s.applicant.felony_conviction,
        ),
        AppetiteRule(
            rule_id="APP-011",
            name="Recent bankruptcy",
            severity="refer",
            reason_code="FINANCIAL_INSTABILITY",
            message="Recent bankruptcy requires financial underwriter review.",
            outcome="refer",
            predicate=lambda s: 0 < s.applicant.bankruptcy_last_years <= 2,
        ),
        AppetiteRule(
            rule_id="APP-012",
            name="Child rider restriction",
            severity="decline",
            reason_code="MINOR_NOT_ELIGIBLE_FOR_THIS_PRODUCT",
            message="Applicants under 18 are not eligible for this product.",
            outcome="decline",
            predicate=lambda s: s.applicant.age < 18,
            product_codes=("TERM", "WHOLE"),
        ),
        AppetiteRule(
            rule_id="APP-013",
            name="Paramed exam outstanding above limit",
            severity="refer",
            reason_code="PARAMED_EXAM_OUTSTANDING",
            message="A paramed exam is required and has not been completed.",
            outcome="refer",
            predicate=lambda s: bool(s.medical and s.medical.paramed_exam_status is ParamedExamStatus.REQUIRED),
        ),
        AppetiteRule(
            rule_id="APP-014",
            name="Expedited issue limit",
            severity="decline",
            reason_code="EXPEDITED_ABOVE_LIMIT",
            message="Expedited issue is not available above the expedited face amount limit.",
            outcome="decline",
            predicate=lambda s: s.request.expedited and s.request.face_amount > 1_000_000,
        ),
        AppetiteRule(
            rule_id="APP-015",
            name="Replacement suitability",
            severity="refer",
            reason_code="REPLACEMENT_SUITABILITY_REVIEW",
            message="A replacement requires suitability review of the existing contract.",
            outcome="refer",
            predicate=lambda s: s.request.is_replacement,
        ),
        AppetiteRule(
            rule_id="APP-016",
            name="Elderly applicant",
            severity="specialist",
            reason_code="AGE_REQUIRES_MEDICAL_REVIEW",
            message="Applicants aged 70 or over require medical underwriting.",
            outcome="refer",
            predicate=lambda s: s.applicant.age >= 70,
        ),
    ]
    return AppetiteRuleSet(version="1.0.0", rules=rules)


def _non_licensed_states() -> frozenset[str]:
    """States where the default appetite set assumes no active license.

    A real carrier supplies its own licensing footprint; this default keeps the
    demo deterministic and makes the extension point explicit.
    """
    return frozenset({"AK", "HI", "ID", "MT", "WY"})


_NON_LICENSED_STATES = _non_licensed_states()


@dataclass(slots=True)
class RuleEngine:
    """Evaluates appetite rules and combines them with model output.

    The precedence order is fixed and documented, because "why was this approved
    when a decline rule fired?" is a question that has to have an answer:

    1. A `decline` rule that fired stops the case.
    2. Any `refer` or `specialist` rule that fired routes it to a human.
    3. Only then may the model decide automatically, and only outside the
       threshold boundaries.
    """

    ruleset: AppetiteRuleSet = field(default_factory=default_ruleset)
    complexity: ComplexityEngine = field(default_factory=ComplexityEngine)
    decline_threshold: float = 0.25
    fast_track_threshold: float = 0.65
    auto_decide_threshold: float = 0.90
    allow_auto_decline: bool = False

    def evaluate(self, submission: Submission) -> tuple[Decision, list[str], str, list[RuleOutcomeLine]]:
        """Return (decision, reason codes, message, rule outcomes)."""
        outcomes = self.ruleset.evaluate(submission)
        fired = [line for line in outcomes if line.fired]
        reasons = [line.reason.split(":", 1)[0] for line in fired]
        complexity = self.complexity.score(submission)

        declines = [line for line in fired if line.severity == "decline"]
        if declines:
            return (
                Decision.DECLINE,
                reasons,
                declines[0].reason,
                outcomes,
            )

        if complexity.tier.requires_human:
            code = f"COMPLEXITY_{complexity.tier.name.upper()}"
            return (
                Decision.REFER,
                [*reasons, code],
                _human_message(complexity.tier, fired),
                outcomes,
            )

        if not fired:
            # An accept is an outcome too: record why nothing objected, so the
            # decision file shows a positive basis rather than a blank one.
            return (
                Decision.ACCEPT,
                ["APPETITE_CLEAR"],
                "Every appetite rule was evaluated and none fired.",
                outcomes,
            )

        return Decision.ACCEPT, reasons, "No appetite rule blocked issue.", outcomes


def _human_message(tier: ComplexityTier, fired: list[RuleOutcomeLine]) -> str:
    if fired:
        first = fired[0]
        return f"{first.reason.partition(':')[2].strip()} Routed to {tier} review."
    return {
        ComplexityTier.REFER: "Complexity score requires an underwriter.",
        ComplexityTier.SPECIALIST: "Complexity score requires a medical or financial underwriter.",
    }.get(tier, "Routed for review.")


__all__ = [
    "AppetiteRule",
    "AppetiteRuleSet",
    "RuleEngine",
    "default_ruleset",
]