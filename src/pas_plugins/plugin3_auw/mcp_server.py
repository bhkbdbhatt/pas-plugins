"""MCP tools for the AUW workbench.

Eleven tools, registered on the shared MCP registry so an agent can run an
underwriting file end to end. Two properties are enforced at the tool layer rather
than only at the HTTP layer:

* Every write-shaped tool is **read-only unless confirmed**. Recording an override
  mutates a decision of record, so the tool is annotated destructive and returns a
  dry run until the caller passes `confirm`; the registry's confirmation guard then
  rejects the unconfirmed call. A decline is never recorded without a named human
  actor attached.
* Tenant is passed explicitly and checked against the caller's tenant, so a
  cross-tenant call fails in the tool rather than being trusted from transport.
"""

from __future__ import annotations

from typing import Any

from pas_core.mcp.registry import (
    McpResourceSpec,
    McpServerInfo,
    McpToolAnnotations,
    McpToolRegistry,
    McpToolSpec,
)
from pas_core.tenancy import TenantContext
from pas_plugins.plugin3_auw.extraction import DocumentText
from pas_plugins.plugin3_auw.models import OverrideRequest, Submission
from pas_plugins.plugin3_auw.monitoring import MonitoringEngine
from pas_plugins.plugin3_auw.service import WorkbenchService, default_workbench_service
from pas_plugins.plugin3_auw.settings import Plugin3Settings

PLUGIN_INFO = McpServerInfo(
    name="pas-auw-workbench",
    version=Plugin3Settings().plugin_version,
    title="PAS AI-Powered Accelerated Underwriting Workbench",
    description=(
        "Accelerated underwriting: triage, document extraction, consent-gated enrichment, "
        "explainable ML risk scoring, appetite rules and audited underwriter overrides."
    ),
)


def _service() -> WorkbenchService:
    return default_workbench_service()


def _submission(payload: Any) -> Submission:
    if isinstance(payload, Submission):
        return payload
    if isinstance(payload, dict):
        body = payload.get("submission") or payload
        return Submission.model_validate(body)
    msg = "submission must be an object"
    raise TypeError(msg)


def _check_tenant(ctx: TenantContext, tenant_id: str) -> None:
    """Refuse to act on a submission owned by another tenant.

    A service principal is not exempt: cross-tenant underwriting decisions are
    exactly what tenant isolation exists to prevent.
    """
    if ctx.tenant.tenant_id != tenant_id:
        from pas_core.errors import PasError  # noqa: PLC0415

        msg = f"submission belongs to tenant {tenant_id}, caller is {ctx.tenant.tenant_id}"
        raise PasError("forbidden", msg, {"submissionTenant": tenant_id})


_OBJECT = {"type": "object", "additionalProperties": True}


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def build_registry() -> McpToolRegistry:
    """Construct the registry of AUW MCP tools."""
    registry = McpToolRegistry(PLUGIN_INFO)

    async def submit_application(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        service = _service()
        submission = _submission(arguments)
        _check_tenant(ctx, submission.tenant_id)
        record = service.decide(submission)
        return {
            "decision": record.to_dict(),
            "triage": record.complexity.to_dict() if record.complexity else None,
        }

    async def triage(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        return {"triage": _service().triage(_submission(arguments)).to_dict()}

    async def extract(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        service = _service()
        documents = [
            DocumentText(
                document_id=str(doc.get("documentId", f"doc{index}")),
                text=str(doc.get("text", "")),
                pages=[str(page) for page in doc.get("pages", [])] or None,
            )
            for index, doc in enumerate(arguments.get("documents") or [])
        ]
        fields = service.extract(_submission(arguments), documents)
        return {"fields": [field.model_dump(mode="json", by_alias=True) for field in fields]}

    async def enrich(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        return {"enrichment": _service().enrich(_submission(arguments)).to_dict()}

    async def score(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        service = _service()
        parsed = _submission(arguments)
        signals = service.enrich(parsed).signals
        both = service.scoring.score_both(parsed, signals)
        return {
            "champion": both["champion"].to_dict(),
            "challenger": both["challenger"].to_dict(),
            "agreement": service.scoring.agreement(parsed, signals),
        }

    async def explain(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        record = _service().get_decision(str(arguments.get("decisionId", "")))
        if record is None:
            return {"error": "decisionNotFound", "decisionId": arguments.get("decisionId")}
        return {
            "decision": str(record.decision),
            "explanation": record.message,
            "reasonCodes": record.reason_codes,
            "rulesFired": [r.rule_id for r in record.rule_outcomes if r.fired],
            "topFactors": [c.to_dict() for c in record.model_contributions[:5]],
            "explainable": record.explainable,
        }

    async def override(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        service = _service()
        decision_id = str(arguments.get("decisionId", ""))
        record = service.get_decision(decision_id)
        if record is None:
            return {"error": "decisionNotFound", "decisionId": decision_id}
        payload = OverrideRequest.model_validate(
            {"decision": arguments.get("decision"), "reason": arguments.get("reason")}
        )
        actor = str(arguments.get("actor") or ctx.principal.subject)
        updated = service.override(decision_id, payload, actor)
        return {"decision": updated.to_dict()}

    async def list_rules(_: dict[str, Any], __: TenantContext) -> dict[str, Any]:
        ruleset = _service().rules.ruleset
        return {
            "version": ruleset.version,
            "count": len(ruleset.rules),
            "rules": [
                {
                    "ruleId": rule.rule_id,
                    "name": rule.name,
                    "severity": rule.severity,
                    "reasonCode": rule.reason_code,
                    "outcome": rule.outcome,
                }
                for rule in ruleset.rules
            ],
        }

    async def model_health(_: dict[str, Any], __: TenantContext) -> dict[str, Any]:
        service = _service()
        ensemble = service.scoring.champion.ensemble
        logistic = service.scoring.challenger.fitted
        return {
            "champion": {
                "version": service.scoring.champion.version,
                "trainRocAuc": ensemble.train_roc_auc,
                "stumps": len(ensemble.stumps),
            },
            "challenger": {
                "version": service.scoring.challenger.version,
                "trainRocAuc": logistic.train_roc_auc,
                "trainAccuracy": logistic.train_accuracy,
            },
            "championLeads": ensemble.train_roc_auc >= logistic.train_roc_auc,
        }

    async def monitoring(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        service = _service()
        engine = MonitoringEngine(service.settings, service.scoring)
        submissions = list(service.submissions.values())
        signals = {s.submission_id: service.enrich(s).signals for s in submissions}
        return {"report": engine.report(submissions, signals).to_dict()}

    async def queue(_: dict[str, Any], __: TenantContext) -> dict[str, Any]:
        service = _service()
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

    _submission_prop = {"submission": {**_OBJECT, "description": "The underwriting submission."}}

    specs = [
        McpToolSpec(
            name="auw_submit_application",
            title="Submit Application",
            description=(
                "Submit a life application for triage and decision. Returns the routing "
                "tier, the decision, and the reason codes that support it."
            ),
            input_schema=_schema(_submission_prop, ["submission"]),
            handler=submit_application,
            required_scopes=("uw:submit",),
            annotations=McpToolAnnotations(read_only=False, destructive=False, idempotent=False),
            plugin_id="plugin3",
            operation_id="auw_submit_application",
            tags=("underwriting", "decision"),
        ),
        McpToolSpec(
            name="auw_triage_submission",
            title="Triage Submission",
            description="Score submission complexity and return the routing tier without deciding.",
            input_schema=_schema(_submission_prop, ["submission"]),
            handler=triage,
            required_scopes=("uw:read",),
            plugin_id="plugin3",
            operation_id="auw_triage_submission",
            tags=("underwriting", "triage"),
        ),
        McpToolSpec(
            name="auw_extract_documents",
            title="Extract Document Fields",
            description=(
                "Extract structured fields from submission document text. Every field carries "
                "its source document, page and confidence."
            ),
            input_schema=_schema(
                {
                    **_submission_prop,
                    "documents": {
                        "type": "array",
                        "description": "Documents with documentId and text (optionally pages).",
                        "items": {
                            "type": "object",
                            "properties": {
                                "documentId": {"type": "string"},
                                "text": {"type": "string"},
                                "pages": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["documentId", "text"],
                        },
                    },
                },
                ["submission", "documents"],
            ),
            handler=extract,
            required_scopes=("uw:read",),
            plugin_id="plugin3",
            operation_id="auw_extract_documents",
            tags=("underwriting", "extraction"),
        ),
        McpToolSpec(
            name="auw_enrich_submission",
            title="Enrich Submission",
            description=(
                "Resolve consent-gated external signals (MIB, pharmacy, screening). Reports "
                "consent gaps explicitly rather than assuming a negative result."
            ),
            input_schema=_schema(_submission_prop, ["submission"]),
            handler=enrich,
            required_scopes=("uw:read",),
            plugin_id="plugin3",
            operation_id="auw_enrich_submission",
            tags=("underwriting", "enrichment"),
        ),
        McpToolSpec(
            name="auw_score_risk",
            title="Score Risk",
            description=(
                "Score risk with the champion model and the logistic challenger, returning "
                "exact feature attributions for each."
            ),
            input_schema=_schema(_submission_prop, ["submission"]),
            handler=score,
            required_scopes=("uw:read",),
            plugin_id="plugin3",
            operation_id="auw_score_risk",
            tags=("underwriting", "ml"),
        ),
        McpToolSpec(
            name="auw_explain_decision",
            title="Explain Decision",
            description=(
                "Explain a recorded decision in narrative form, suitable for an adverse "
                "notice. Lists the rules considered, the rules that fired, and the top "
                "risk factors."
            ),
            input_schema=_schema(
                {"decisionId": {"type": "string", "description": "The decision to explain."}},
                ["decisionId"],
            ),
            handler=explain,
            required_scopes=("uw:read",),
            plugin_id="plugin3",
            operation_id="auw_explain_decision",
            tags=("underwriting", "explainability"),
        ),
        McpToolSpec(
            name="auw_override_decision",
            title="Override Decision",
            description=(
                "Record an underwriter override of an automated decision. Destructive: the "
                "caller must pass confirm=true and a substantive reason. The prior outcome "
                "is always retained."
            ),
            input_schema=_schema(
                {
                    "decisionId": {"type": "string"},
                    "decision": {"type": "string", "enum": ["accept", "refer", "decline"]},
                    "reason": {"type": "string", "minLength": 20},
                    "actor": {"type": "string"},
                    "confirm": {"type": "boolean", "default": False},
                },
                ["decisionId", "decision", "reason", "confirm"],
            ),
            handler=override,
            required_scopes=("uw:override",),
            annotations=McpToolAnnotations(read_only=False, destructive=True, idempotent=False),
            plugin_id="plugin3",
            operation_id="auw_override_decision",
            tags=("underwriting", "human-in-the-loop"),
        ),
        McpToolSpec(
            name="auw_list_appetite_rules",
            title="List Appetite Rules",
            description="List the active appetite ruleset with severities and reason codes.",
            input_schema=_schema({}, []),
            handler=list_rules,
            required_scopes=("uw:read",),
            plugin_id="plugin3",
            operation_id="auw_list_appetite_rules",
            tags=("underwriting", "rules"),
        ),
        McpToolSpec(
            name="auw_model_health",
            title="Model Health",
            description=(
                "Report champion and challenger training metrics plus whether the champion "
                "currently leads on discrimination."
            ),
            input_schema=_schema({}, []),
            handler=model_health,
            required_scopes=("uw:read",),
            plugin_id="plugin3",
            operation_id="auw_model_health",
            tags=("underwriting", "ml", "monitoring"),
        ),
        McpToolSpec(
            name="auw_monitoring_report",
            title="Monitoring Report",
            description=(
                "Compute feature drift, calibration and the promotion recommendation over "
                "the submissions seen by this workbench."
            ),
            input_schema=_schema({}, []),
            handler=monitoring,
            required_scopes=("uw:read",),
            plugin_id="plugin3",
            operation_id="auw_monitoring_report",
            tags=("underwriting", "monitoring"),
        ),
        McpToolSpec(
            name="auw_underwriting_queue",
            title="Underwriting Queue",
            description=(
                "List submissions awaiting human underwriter review, oldest first, with the "
                "reason each was routed out of the automated path."
            ),
            input_schema=_schema({}, []),
            handler=queue,
            required_scopes=("uw:read",),
            plugin_id="plugin3",
            operation_id="auw_underwriting_queue",
            tags=("underwriting", "workflow"),
        ),
    ]
    for spec in specs:
        registry.register_tool(spec)

    async def rules_resource(_: TenantContext) -> dict[str, Any]:
        ruleset = _service().rules.ruleset
        return {
            "version": ruleset.version,
            "rules": [
                {
                    "ruleId": rule.rule_id,
                    "name": rule.name,
                    "severity": rule.severity,
                    "reasonCode": rule.reason_code,
                }
                for rule in ruleset.rules
            ],
        }

    registry.register_resource(
        McpResourceSpec(
            uri_template="pas://auw/appetite/rules",
            name="Appetite ruleset",
            title="Active appetite rules",
            description="The active appetite rules with severity and reason code.",
            mime_type="application/json",
            handler=rules_resource,
            plugin_id="plugin3",
            tags=("underwriting", "rules"),
        )
    )

    async def model_resource(_: TenantContext) -> dict[str, Any]:
        return await model_health({}, None)  # type: ignore[arg-type]

    registry.register_resource(
        McpResourceSpec(
            uri_template="pas://auw/model/summary",
            name="Model summary",
            title="Champion and challenger metrics",
            description="Champion and challenger versions and training metrics.",
            mime_type="application/json",
            handler=model_resource,
            plugin_id="plugin3",
            tags=("underwriting", "ml"),
        )
    )

    return registry


__all__ = ["PLUGIN_INFO", "build_registry"]