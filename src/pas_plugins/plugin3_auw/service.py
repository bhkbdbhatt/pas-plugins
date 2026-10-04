"""The workbench: the single call that turns a submission into a decision.

This is the module a carrier integrates against. It runs the funnel in order and
stops at the first point where a human is required:

    intake -> triage -> extract -> enrich -> score -> appetite rules -> decide

Every stage records what it did on the decision record, so the audit trail is the
by-product of doing the work rather than something assembled afterwards.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

from pas_plugins.plugin3_auw.complexity import ComplexityEngine
from pas_plugins.plugin3_auw.enrichment import EnrichmentEngine, EnrichmentResult
from pas_plugins.plugin3_auw.extraction import DocumentText, RuleBasedExtractor
from pas_plugins.plugin3_auw.models import (
    Class,
    ComplexityScore,
    Decision,
    DecisionRecord,
    DecisionSource,
    OverrideRequest,
    ParamedExamStatus,
    RiskScore,
    Submission,
)
from pas_plugins.plugin3_auw.rules import AppetiteRuleSet, RuleEngine, default_ruleset
from pas_plugins.plugin3_auw.scoring import ScoringEngine
from pas_plugins.plugin3_auw.settings import Plugin3Settings


class WorkbenchService:
    """Orchestrates triage, extraction, enrichment, scoring and rules."""

    def __init__(
        self,
        settings: Plugin3Settings | None = None,
        *,
        scoring: ScoringEngine | None = None,
        enrichment: EnrichmentEngine | None = None,
        rules: RuleEngine | None = None,
        ruleset: AppetiteRuleSet | None = None,
        complexity: ComplexityEngine | None = None,
    ) -> None:
        self._settings = settings or Plugin3Settings()
        self._scoring = scoring or ScoringEngine(self._settings)
        self._enrichment = enrichment or EnrichmentEngine()
        self._rules = rules or RuleEngine(
            ruleset=ruleset or default_ruleset(),
            decline_threshold=self._settings.decline_threshold,
            fast_track_threshold=self._settings.fast_track_threshold,
            auto_decide_threshold=self._settings.auto_decide_threshold,
            allow_auto_decline=self._settings.allow_auto_decline,
        )
        self._complexity = complexity or self._rules.complexity
        self._extractor = RuleBasedExtractor()
        self._decisions: dict[str, DecisionRecord] = {}
        self._submissions: dict[str, Submission] = {}

    # -- funnel ------------------------------------------------------------

    def triage(self, submission: Submission) -> ComplexityScore:
        return self._complexity.score(submission)

    def extract(
        self, submission: Submission, texts: list[DocumentText] | None = None
    ) -> list:
        """Extract fields from documents and reconcile them with declarations."""
        if not texts:
            return []
        extracted = self._extractor.extract(submission, texts)
        return self._extractor.merge_with_declarations(extracted, submission)

    def enrich(self, submission: Submission) -> EnrichmentResult:
        return self._enrichment.enrich(submission)

    def decide(self, submission: Submission) -> DecisionRecord:
        """Run the full funnel and produce an explainable decision."""
        started = time.perf_counter()
        complexity = self.triage(submission)
        enrichment = self.enrich(submission)
        risk = self._scoring.score(submission, enrichment.signals)

        decision, reasons, message, outcomes = self._rules.evaluate(submission)

        source = DecisionSource.AUTO_RULE
        escalate = self._scoring.should_escalate(risk, enrichment.signals, submission)

        if decision is Decision.ACCEPT and not escalate and risk.score < self._settings.decline_threshold:
            decision = Decision.DECLINE
            reasons = [*reasons, "MODEL_RISK_ABOVE_THRESHOLD"]
            message = "Model risk score is above the auto-decline threshold."
            if not self._settings.allow_auto_decline:
                decision = Decision.REFER
                reasons.append("AUTO_DECLINE_DISABLED")
                message = "Model risk is elevated; declines require underwriter sign-off."
                source = DecisionSource.AUTO_MODEL
        elif decision is Decision.ACCEPT and not escalate:
            source = DecisionSource.AUTO_MODEL

        if escalation_to_refer(decision, complexity, enrichment):
            decision = Decision.REFER
            reasons = [*reasons, "CONSENT_GAP"]
            message = f"{message} Consent missing: {', '.join(enrichment.consent_gaps)}."
            source = DecisionSource.AUTO_RULE

        assigned = self._assign_class(submission, risk, decision)
        record = DecisionRecord(
            decision_id=f"DEC{uuid.uuid4().hex[:16]}",
            submission_id=submission.submission_id,
            tenant_id=submission.tenant_id,
            decision=decision,
            decision_source=source,
            risk_score=risk.score,
            model_version=risk.model_version,
            reason_codes=sorted(set(reasons)),
            message=message,
            requirements=self._requirements(submission, enrichment, decision),
            assigned_class=assigned,
            rule_outcomes=outcomes,
            model_contributions=risk.top_drivers,
            complexity=complexity,
            decided_by=f"workbench:{self._settings.plugin_id}",
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )
        if self._settings.require_explanation and not record.explainable:
            record.decision = Decision.REFER
            record.reason_codes = sorted({*record.reason_codes, "EXPLANATION_UNAVAILABLE"})
            record.message = (
                f"{record.message} Routed to a human: the automated basis was not sufficient "
                "to explain this decision."
            )
        self._decisions[record.decision_id] = record
        self._submissions[submission.submission_id] = submission
        return record

    def override(self, decision_id: str, request: OverrideRequest, actor: str) -> DecisionRecord:
        """Record an underwriter override. Never silently replaces the model output."""
        record = self._decisions.get(decision_id)
        if record is None:
            msg = f"unknown decision {decision_id}"
            raise KeyError(msg)
        record.override = {
            "fromDecision": str(record.decision),
            "toDecision": str(request.decision),
            "reason": request.reason,
            "actor": actor,
            "at": datetime.now(UTC).isoformat(),
        }
        record.decision = request.decision
        record.decision_source = (
            DecisionSource.SUPERVISOR_OVERRIDE if request.follow_up_at else DecisionSource.UNDERWRITER
        )
        if request.assigned_class:
            record.assigned_class = request.assigned_class
        record.reason_codes = sorted({*record.reason_codes, "UNDERWRITER_OVERRIDE"})
        record.message = f"Overridden by {actor}: {request.reason}"
        record.decided_by = actor
        return record

    # -- lookups -----------------------------------------------------------

    def get_decision(self, decision_id: str) -> DecisionRecord | None:
        return self._decisions.get(decision_id)

    def get_submission(self, submission_id: str) -> Submission | None:
        return self._submissions.get(submission_id)

    def decisions_for_submission(self, submission_id: str) -> list[DecisionRecord]:
        return [d for d in self._decisions.values() if d.submission_id == submission_id]

    @property
    def settings(self) -> Plugin3Settings:
        return self._settings

    @property
    def scoring(self) -> ScoringEngine:
        return self._scoring

    @property
    def rules(self) -> RuleEngine:
        return self._rules

    @property
    def submissions(self) -> dict[str, Submission]:
        """The submissions this workbench instance has decided on."""
        return dict(self._submissions)

    @property
    def decisions(self) -> dict[str, DecisionRecord]:
        """Every decision this workbench instance has recorded."""
        return dict(self._decisions)

    # -- internals ---------------------------------------------------------

    def _assign_class(
        self, submission: Submission, risk: RiskScore, decision: Decision
    ) -> Class | None:
        if decision is not Decision.ACCEPT:
            return None
        if risk.score <= self._settings.auto_decide_threshold:
            return Class(code="PREFERRED", basis_points=0, rationale="Standard preferred class.")
        return Class(
            code="STANDARD",
            basis_points=0,
            rationale="Standard class; risk score inside appetite.",
        )

    @staticmethod
    def _requirements(
        submission: Submission, enrichment: EnrichmentResult, decision: Decision
    ) -> list[dict[str, object]]:
        """Outstanding evidence needed before the case can be issued."""
        requirements: list[dict[str, object]] = []
        if (
            enrichment.medical
            and enrichment.medical.paramed_exam_status is ParamedExamStatus.REQUIRED
        ):
            requirements.append(
                {"type": "paramedExam", "blocking": True, "reason": "Required above the issue-age/face limit."}
            )
        if submission.request.is_replacement:
            requirements.append(
                {"type": "replacementSuitability", "blocking": True, "reason": "Existing contract review."}
            )
        if not submission.mib_consent:
            requirements.append(
                {"type": "mibConsent", "blocking": False, "reason": "MIB lookup not consented."}
            )
        if decision is Decision.REFER and not requirements:
            requirements.append(
                {"type": "underwriterReview", "blocking": True, "reason": "Routed by complexity or appetite."}
            )
        return requirements


def escalation_to_refer(decision: Decision, complexity: ComplexityScore, enrichment: EnrichmentResult) -> bool:
    """Whether a consent gap must block an automated accept."""
    return bool(
        enrichment.consent_gaps
        and decision is Decision.ACCEPT
        and complexity.tier.value in {"auto", "fastTrack"}
    )


def _default_rules_for(settings: Plugin3Settings) -> AppetiteRuleSet:
    return default_ruleset()


_default_service: WorkbenchService | None = None


def default_workbench_service() -> WorkbenchService:
    """Process-wide workbench. Scoring is trained once and cached."""
    global _default_service
    if _default_service is None:
        _default_service = WorkbenchService(Plugin3Settings())
    return _default_service


__all__ = ["WorkbenchService", "default_workbench_service"]