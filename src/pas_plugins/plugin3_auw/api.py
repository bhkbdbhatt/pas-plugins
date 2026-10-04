"""Plugin 3 HTTP API: submission intake, triage, decisions and monitoring."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from pas_core.app import Platform, context_dependency
from pas_core.tenancy import TenantContext
from pas_plugins.plugin3_auw.extraction import DocumentText
from pas_plugins.plugin3_auw.models import OverrideRequest, Submission
from pas_plugins.plugin3_auw.monitoring import MonitoringEngine
from pas_plugins.plugin3_auw.service import WorkbenchService


def build_router(platform: Platform, service: WorkbenchService) -> APIRouter:
    """Attach the platform-bound routes."""
    router = APIRouter(prefix="/auw", tags=["auw-workbench"])
    dependency = context_dependency(platform)

    def _check_tenant(ctx: TenantContext, tenant_id: str) -> None:
        """Refuse to act on a submission owned by another tenant.

        A service principal is not exempt. Cross-tenant underwriting decisions
        are exactly the kind of thing tenant isolation exists to prevent, so the
        check runs for every caller regardless of role.
        """
        if ctx.tenant.tenant_id != tenant_id:
            msg = "submission belongs to another tenant"
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=msg)

    async def _body(request: Request) -> dict[str, Any]:
        payload = await request.json()
        if not isinstance(payload, dict):
            msg = "request body must be a JSON object"
            raise TypeError(msg)
        return payload

    # --- funnel ------------------------------------------------------------

    @router.post(
        "/submissions",
        status_code=status.HTTP_201_CREATED,
        summary="Submit an application for triage and decision",
        operation_id="auw.createSubmission",
    )
    async def create_submission(
        request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Accept a submission, run the funnel, and return the decision with its basis."""
        ctx.principal.require_scopes("uw:submit")
        submission = Submission.model_validate(await _body(request))
        _check_tenant(ctx, submission.tenant_id)
        decision = service.decide(submission)
        return {
            "submission": submission.to_dict(),
            "decision": decision.to_dict(),
            "triage": service.triage(submission).to_dict(),
        }

    @router.post(
        "/submissions/{submission_id}/triage",
        summary="Score complexity without deciding",
        operation_id="auw.triage",
    )
    async def triage_submission(
        submission_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Complexity scoring only. Safe to call repeatedly; no decision is recorded."""
        ctx.principal.require_scopes("uw:read")
        submission = Submission.model_validate(await _body(request))
        _check_tenant(ctx, submission.tenant_id)
        if submission.submission_id != submission_id:
            msg = "body submissionId does not match the path"
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)
        return {"triage": service.triage(submission).to_dict()}

    @router.post(
        "/submissions/{submission_id}/extract",
        summary="Extract fields from documents with provenance",
        operation_id="auw.extract",
    )
    async def extract_submission(
        submission_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Extract fields from supplied document text, with per-field provenance.

        Accepts either a bare submission or an envelope of
        `{"submission": ..., "documents": [...]}`; the envelope is optional so
        the same endpoint serves a single-document call and a batch.
        """
        ctx.principal.require_scopes("uw:read")
        body = await _body(request)
        payload = body.get("submission") or body
        documents_in = body.get("documents") or payload.get("documents") or []
        submission_payload = {k: v for k, v in payload.items() if k != "documents"}
        submission = Submission.model_validate(submission_payload)
        _check_tenant(ctx, submission.tenant_id)
        documents = [
            DocumentText(
                document_id=str(doc.get("documentId", f"doc{index}")),
                text=str(doc.get("text", "")),
                pages=[str(page) for page in doc.get("pages", [])] or None,
            )
            for index, doc in enumerate(documents_in)
        ]
        fields = service.extract(submission, documents)
        return {
            "submissionId": submission_id,
            "fields": [field.model_dump(mode="json", by_alias=True) for field in fields],
        }

    @router.post(
        "/submissions/{submission_id}/enrich",
        summary="Resolve consent-gated external signals",
        operation_id="auw.enrich",
    )
    async def enrich_submission(
        submission_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Consent-gated enrichment. Consent gaps are reported, never assumed away."""
        ctx.principal.require_scopes("uw:read")
        submission = Submission.model_validate(await _body(request))
        _check_tenant(ctx, submission.tenant_id)
        return {"submissionId": submission_id, "enrichment": service.enrich(submission).to_dict()}

    @router.post(
        "/submissions/{submission_id}/score",
        summary="Score risk with exact attributions",
        operation_id="auw.score",
    )
    async def score_submission(
        submission_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Champion and challenger scores with feature attributions for each."""
        ctx.principal.require_scopes("uw:read")
        submission = Submission.model_validate(await _body(request))
        _check_tenant(ctx, submission.tenant_id)
        signals = service.enrich(submission).signals
        both = service.scoring.score_both(submission, signals)
        return {
            "submissionId": submission_id,
            "champion": both["champion"].to_dict(),
            "challenger": both["challenger"].to_dict(),
            "agreement": service.scoring.agreement(submission, signals),
        }

    @router.post(
        "/submissions/{submission_id}/decision",
        summary="Produce an explainable decision",
        operation_id="auw.decide",
    )
    async def decide_submission(
        submission_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Run the full funnel and return the decision with its complete basis."""
        ctx.principal.require_scopes("uw:submit")
        submission = Submission.model_validate(await _body(request))
        _check_tenant(ctx, submission.tenant_id)
        return {"decision": service.decide(submission).to_dict()}

    # --- decisions ---------------------------------------------------------

    @router.get("/decisions", summary="Decisions recorded for a submission", operation_id="auw.listDecisions")
    async def list_decisions(
        request: Request,
        ctx: TenantContext = Depends(dependency),
        submission_id: str = Query(min_length=6, alias="submissionId"),
    ) -> dict[str, Any]:
        """Every decision recorded for one submission, oldest first."""
        ctx.principal.require_scopes("uw:read")
        records = service.decisions_for_submission(submission_id)
        return {
            "submissionId": submission_id,
            "count": len(records),
            "decisions": [record.to_dict() for record in records],
        }

    @router.get("/decisions/{decision_id}", summary="Retrieve a decision", operation_id="auw.getDecision")
    async def get_decision(
        decision_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Fetch a decision record by id, including any override."""
        ctx.principal.require_scopes("uw:read")
        record = service.get_decision(decision_id)
        if record is None:
            msg = f"decision {decision_id} not found"
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        return {"decision": record.to_dict()}

    @router.post(
        "/decisions/{decision_id}/override",
        summary="Record an underwriter override",
        operation_id="auw.override",
    )
    async def override_decision(
        decision_id: str,
        payload: OverrideRequest,
        request: Request,
        ctx: TenantContext = Depends(dependency),
    ) -> dict[str, Any]:
        """Overrule an automated decision. The prior outcome is always retained."""
        ctx.principal.require_scopes("uw:override")
        try:
            record = service.override(decision_id, payload, ctx.principal.subject)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"decision {decision_id} not found"
            ) from exc
        return {"decision": record.to_dict()}

    @router.get(
        "/decisions/{decision_id}/explain",
        summary="Human-readable decision explanation",
        operation_id="auw.explain",
    )
    async def explain_decision(
        decision_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """A narrative explanation suitable for an applicant-facing adverse notice."""
        ctx.principal.require_scopes("uw:read")
        record = service.get_decision(decision_id)
        if record is None:
            msg = f"decision {decision_id} not found"
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        return {
            "decisionId": decision_id,
            "decision": str(record.decision),
            "explanation": record.message,
            "reasonCodes": record.reason_codes,
            "topFactors": [
                {"factor": c.feature, "direction": c.direction, "impact": round(c.shap_value, 6)}
                for c in record.model_contributions[:5]
            ],
            "rulesConsidered": len(record.rule_outcomes),
            "rulesFired": [r.rule_id for r in record.rule_outcomes if r.fired],
            "explainable": record.explainable,
        }

    # --- governance --------------------------------------------------------

    @router.get("/ruleset", summary="The active appetite ruleset", operation_id="auw.ruleset")
    async def get_ruleset(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """Every appetite rule, its severity, and the reason code it emits."""
        ctx.principal.require_scopes("uw:read")
        ruleset = service.rules.ruleset
        return {
            "version": ruleset.version,
            "publishedAt": ruleset.published_at.isoformat(),
            "count": len(ruleset.rules),
            "rules": [
                {
                    "ruleId": rule.rule_id,
                    "name": rule.name,
                    "severity": rule.severity,
                    "outcome": rule.outcome,
                    "reasonCode": rule.reason_code,
                    "message": rule.message,
                    "productCodes": list(rule.product_codes) if rule.product_codes else None,
                    "version": rule.version,
                }
                for rule in ruleset.rules
            ],
        }

    @router.get("/monitoring/report", summary="Model monitoring report", operation_id="auw.monitoringReport")
    async def monitoring_report(
        request: Request,
        ctx: TenantContext = Depends(dependency),
        days: int = Query(default=30, ge=1, le=365),
    ) -> dict[str, Any]:
        """Drift, calibration and the champion/challenger promotion recommendation."""
        ctx.principal.require_scopes("uw:read")
        engine = MonitoringEngine(service.settings, service.scoring)
        submissions = list(service.submissions.values())
        signals = {s.submission_id: service.enrich(s).signals for s in submissions}
        return {"windowDays": days, "report": engine.report(submissions, signals).to_dict()}

    @router.get(
        "/underwriting-queue",
        summary="Cases awaiting human underwriter review",
        operation_id="auw.queue",
    )
    async def underwriting_queue(
        request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Referred cases with the reason each left the automated path."""
        ctx.principal.require_scopes("uw:read")
        waiting = [r for r in service.decisions.values() if str(r.decision) == "refer"]
        waiting.sort(key=lambda r: r.decided_at)
        return {
            "count": len(waiting),
            "items": [
                {
                    "submissionId": r.submission_id,
                    "decisionId": r.decision_id,
                    "reasonCodes": r.reason_codes,
                    "complexityTier": r.complexity.tier.value if r.complexity else None,
                    "estimatedMinutes": (
                        r.complexity.estimated_minutes_for_human if r.complexity else None
                    ),
                }
                for r in waiting
            ],
        }

    @router.get("/health/detailed", summary="Model and rule inventory", operation_id="auw.modelInventory")
    async def health_detailed(
        request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """What is loaded: model versions, training metrics and the ruleset version."""
        ctx.principal.require_scopes("uw:read")
        ensemble = service.scoring.champion.ensemble
        logistic = service.scoring.challenger.fitted
        return {
            "status": "ok",
            "plugin": service.settings.plugin_id,
            "champion": {
                "version": service.scoring.champion.version,
                "algorithm": service.scoring.champion.algorithm,
                "trainRocAuc": ensemble.train_roc_auc,
                "stumps": len(ensemble.stumps),
                "trainSamples": ensemble.train_samples,
                "baseRate": ensemble.base_rate,
            },
            "challenger": {
                "version": service.scoring.challenger.version,
                "algorithm": service.scoring.challenger.algorithm,
                "trainRocAuc": logistic.train_roc_auc,
                "trainAccuracy": logistic.train_accuracy,
            },
            "ruleset": {"version": service.rules.ruleset.version, "rules": len(service.rules.ruleset.rules)},
            "thresholds": {
                "autoDecide": service.settings.auto_decide_threshold,
                "fastTrack": service.settings.fast_track_threshold,
                "decline": service.settings.decline_threshold,
                "allowAutoDecline": service.settings.allow_auto_decline,
            },
        }

    return router


__all__ = ["build_router"]