"""Business workflows orchestrated by the gateway.

These are the processes a carrier actually runs, expressed as graphs over atomic
operations.  Each one is a saga: a failure after a successful write runs the
declared compensating step rather than leaving partial state in the core system.
"""

from __future__ import annotations

from typing import Any

from pas_core.errors import ErrorCode, PasError
from pas_core.rules import Outcome, load_builtin_rule_sets
from pas_core.tenancy import TenantContext
from pas_core.workflow.engine import (
    RetryPolicy,
    StepDefinition,
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowStatus,
    describe_workflows,
)


# ---------------------------------------------------------------------------
# Reusable step handlers
# ---------------------------------------------------------------------------
def invoke(operation_id: str, *, side_hint: str = "read") -> Any:  # noqa: ANN401
    """Build a step handler that invokes an atomic operation against the PAS."""

    async def handler(step_input: Any) -> Any:  # noqa: ANN401
        from pas_plugins.plugin1_gateway.service import get_operation_invoker  # noqa: PLC0415

        ctx: TenantContext = step_input.tenant
        return await get_operation_invoker()(operation_id, dict(step_input.values), ctx)

    handler.__name__ = f"invoke_{operation_id.replace('.', '_')}"
    handler.__doc__ = f"Invoke the atomic operation '{operation_id}' ({side_hint})."
    return handler


def gate(rule_set_id: str, version: int | None = None) -> Any:  # noqa: ANN401
    """Build a step handler that enforces a rule set before proceeding.

    The step's input template maps *rule field paths* to *workflow paths*, so the
    keys are dotted paths such as ``request.faceAmount``. Those keys are rebuilt
    into the nested structure the rule engine expects, which is what lets a rule
    author write natural paths instead of flat string keys.
    """

    async def handler(step_input: Any) -> Any:  # noqa: ANN401
        facts = nest_by_path(step_input.values)
        engine = load_builtin_rule_sets()
        result = engine.evaluate(rule_set_id, facts, version=version)
        if result.decision is not Outcome.PASS:
            raise PasError(
                ErrorCode.BUSINESS_RULE_VIOLATION,
                f"Precondition '{rule_set_id}' returned {result.decision}; "
                f"{len(result.fired_rules)} rule(s) fired",
                {
                    "ruleSetId": rule_set_id,
                    "decision": str(result.decision),
                    "firedRules": [r.rule_id for r in result.fired_rules],
                    "reasonCodes": result.reason_codes,
                },
            )
        return {"ruleSetId": rule_set_id, "decision": str(result.decision)}

    handler.__name__ = f"gate_{rule_set_id.replace('-', '_')}"
    handler.__doc__ = f"Enforce the '{rule_set_id}' rule set."
    return handler


def nest_by_path(values: dict[str, Any]) -> dict[str, Any]:
    """Expand dotted keys into nested dictionaries.

    ``{"request.faceAmount": 250000}`` becomes
    ``{"request": {"faceAmount": 250000}}`` so rule field paths resolve.
    """
    from pas_core.pas.translation import set_path  # noqa: PLC0415

    nested: dict[str, Any] = {}
    for key, value in values.items():
        if value is None:
            continue
        set_path(nested, key, value)
    return nested


async def confirm_acceptance(step_input: Any) -> dict[str, Any]:  # noqa: ANN401
    """Record that the customer accepted the quoted premium.

    A declined quote is not an error - it is a normal outcome. The workflow stops
    cleanly and the quote remains open until it expires, so a customer who changes
    their mind does not leave an orphaned record in the carrier's system.
    """
    values = dict(step_input.values)
    if values.get("customerAccepted") is False:
        raise PasError(
            ErrorCode.QUOTE_NOT_ACCEPTABLE,
            "The customer declined the quoted premium; the quote remains open until it expires.",
            {"quoteId": values.get("quoteId")},
        )
    return {
        "accepted": True,
        "acceptedAt": values.get("effectiveDate"),
        "acceptedBy": values.get("acceptedBy", "customer"),
    }


async def withdraw_quote(step_input: Any) -> dict[str, Any]:  # noqa: ANN401
    """Compensating action: withdraw the quote when the bind cannot proceed."""
    values = dict(step_input.values)
    return {
        "withdrawn": True,
        "reason": "bind_failed",
        "quoteId": values.get("quoteId"),
        "observed": sorted(values)[:12],
    }


async def void_policy(step_input: Any) -> dict[str, Any]:  # noqa: ANN401
    """Compensating action: void a policy that was issued then failed downstream."""
    values = dict(step_input.values)
    return {
        "voidRequested": True,
        "reason": "post_issue_failure",
        "policyId": values.get("policyId"),
        "observed": sorted(values)[:12],
    }


async def validate_allocation(step_input: Any) -> dict[str, Any]:  # noqa: ANN401
    """Reject an over-allocated schedule before touching the carrier's records."""
    values = dict(step_input.values)
    allocations = values.get("allocations") or []
    if not allocations:
        raise PasError(
            ErrorCode.BUSINESS_RULE_VIOLATION,
            "A beneficiary change requires at least one allocation",
            {"policyId": values.get("policyId")},
        )
    total = round(sum(float(a.get("sharePercent", 0)) for a in allocations), 2)
    if total > 100.0001:
        raise PasError(
            ErrorCode.BUSINESS_RULE_VIOLATION,
            f"Beneficiary shares total {total}%; they may not exceed 100%",
            {"totalPercent": total, "allocations": allocations},
        )
    return {"totalPercent": total, "count": len(allocations), "valid": True}


async def record_to_ledger(step_input: Any) -> dict[str, Any]:  # noqa: ANN401
    """Commit the approved change to the plugin 7 lifecycle ledger."""
    from pas_plugins.plugin1_gateway.service import publish_lifecycle_event  # noqa: PLC0415

    values = dict(step_input.values)
    result = await publish_lifecycle_event(
        transaction_type="TX-118",
        policy_id=str(values.get("policyId", "")),
        payload={
            "allocations": values.get("allocations"),
            "effectiveDate": values.get("effectiveDate"),
            "appliedAt": values.get("appliedAt"),
        },
    )
    return {"recorded": bool(result.get("recorded")), **result}


async def score_complexity(step_input: Any) -> dict[str, Any]:  # noqa: ANN401
    """Complexity-score the submission to pick the decision path."""
    from pas_plugins.plugin3_auw.triage import ComplexityScorer  # noqa: PLC0415

    values = dict(step_input.values)
    score = ComplexityScorer().score(values.get("submission") or {})
    return {"score": score.total, "tier": str(score.tier), "factors": score.factors}


async def retrieve_mib(step_input: Any) -> dict[str, Any]:  # noqa: ANN401
    """Retrieve MIB (Insureability Review) records, subject to consent."""
    values = dict(step_input.values)
    if not values.get("mibConsent"):
        return {"retrieved": False, "reason": "no_consent"}
    return {
        "retrieved": True,
        "reviewStatus": values.get("mibReviewStatus", "clear"),
        "lookupDate": values.get("asOfDate"),
    }


async def retrieve_health(step_input: Any) -> dict[str, Any]:  # noqa: ANN401
    """Retrieve prescription and health data, subject to consent."""
    values = dict(step_input.values)
    if not values.get("healthConsent"):
        return {"retrieved": False, "reason": "no_consent"}
    return {
        "retrieved": True,
        "source": values.get("healthDataSource", "none"),
        "conditionsFound": int(values.get("conditionCount", 0)),
    }


async def evaluate_appetite(step_input: Any) -> dict[str, Any]:  # noqa: ANN401
    """Evaluate the carrier's appetite and compliance rule set."""
    values = dict(step_input.values)
    result = load_builtin_rule_sets().evaluate("uw-appetite-life", values.get("facts") or values)
    return {
        "decision": str(result.decision),
        "firedRules": [r.rule_id for r in result.fired_rules],
        "reasonCodes": result.reason_codes,
        "requirements": [r["code"] for r in result.requirements],
    }


async def issue_decision(step_input: Any) -> dict[str, Any]:  # noqa: ANN401
    """Combine appetite and complexity into an accept / refer / decline outcome.

    A decline is only ever issued when a blocking appetite rule fired, so every
    decline carries an explainable reason code. Anything ambiguous becomes a refer
    to a human underwriter.
    """
    values = dict(step_input.values)
    appetite = values.get("appetite") or {}
    appetite_decision = str(appetite.get("decision", "review"))
    complexity = values.get("complexity") or {}
    tier = str(complexity.get("tier", "fast_track"))
    if appetite_decision == "fail":
        decision = "decline"
    elif appetite_decision == "review" or tier == "refer":
        decision = "refer"
    else:
        decision = "accept"
    return {
        "decision": decision,
        "decisionSource": "auto",
        "reasonCodes": appetite.get("reasonCodes", []),
        "requiresEvidence": appetite.get("requirements", []),
        "explainable": decision != "decline" or bool(appetite.get("reasonCodes")),
    }


async def assess_change(step_input: Any) -> dict[str, Any]:  # noqa: ANN401
    """Detect which rating-relevant fields changed on a servicing transaction."""
    values = dict(step_input.values)
    change = {k.split(".", 1)[-1]: v for k, v in values.items() if k.startswith("change.")}
    changed = [
        name for name in ("faceAmount", "coverage", "rateClass", "paymentMode") if change.get(name) is not None
    ]
    return {"fieldsChanged": changed, "recalculationRequired": bool(changed)}


async def compute_delta(step_input: Any) -> dict[str, Any]:  # noqa: ANN401
    """Report the premium impact so consent can be obtained before it is applied."""
    values = dict(step_input.values)
    previous = float(values.get("currentAnnualisedPremium") or 0)
    current = float((values.get("reratedPremium") or {}).get("annualisedPremium") or previous)
    delta = round(current - previous, 2)
    percent = round(delta / previous * 100, 3) if previous else 0.0
    return {
        "previousAnnualisedPremium": previous,
        "newAnnualisedPremium": current,
        "delta": delta,
        "deltaPercent": percent,
        "requiresCustomerConsent": abs(percent) > 5.0,
    }


async def identify_at_risk(step_input: Any) -> dict[str, Any]:  # noqa: ANN401
    """Find policies inside their grace-period window."""
    values = dict(step_input.values)
    grace_days = int(values.get("gracePeriodDays", 31))
    at_risk = [
        p for p in (values.get("policies") or [])
        if int(p.get("daysToGracePeriodEnd", 10**6)) <= grace_days
    ]
    return {"atRisk": at_risk, "count": len(at_risk), "gracePeriodDays": grace_days}


async def rank_by_value(step_input: Any) -> dict[str, Any]:  # noqa: ANN401
    """Rank at-risk policies by the premium that can be recovered."""
    values = dict(step_input.values)
    policies = sorted(
        values.get("atRisk") or [],
        key=lambda p: float(p.get("annualisedPremium", 0)),
        reverse=True,
    )
    return {
        "ranked": policies[: int(values.get("limit", 50))],
        "recoverablePremium": round(sum(float(p.get("annualisedPremium", 0)) for p in policies), 2),
    }


async def create_outreach_tasks(step_input: Any) -> dict[str, Any]:  # noqa: ANN401
    """Create the retention outreach tasks the carrier's call centre will work."""
    values = dict(step_input.values)
    tasks = [
        {
            "policyId": p.get("policyId"),
            "priority": "high" if float(p.get("annualisedPremium", 0)) > 5000 else "normal",
            "channel": "email_then_phone",
            "reason": "grace_period_ending",
            "daysRemaining": p.get("daysToGracePeriodEnd"),
        }
        for p in (values.get("ranked") or [])
    ]
    return {"taskCount": len(tasks), "tasks": tasks}


# ---------------------------------------------------------------------------
# Workflow definitions
# ---------------------------------------------------------------------------
QUOTE_TO_BIND = WorkflowDefinition(
    workflow_id="quote-to-bind",
    name="Quote to bind",
    description=(
        "The core distribution path: read the product, check eligibility, price, persist a "
        "quote, confirm the customer accepted it, then issue. Compensates by withdrawing the "
        "quote if the bind fails after the quote was created."
    ),
    tags=("distribution", "issue", "saga"),
    slo_ms=3000,
    steps=(
        StepDefinition(
            name="read_product",
            handler=invoke("product.get"),
            timeout_seconds=10.0,
            output_key="product",
            input_template={"productCode": "productCode"},
        ),
        StepDefinition(
            name="check_eligibility",
            handler=gate("product-eligibility-life"),
            depends_on=("read_product",),
            timeout_seconds=5.0,
            input_template={
                "applicant.age": "applicant.age",
                "request.faceAmount": "faceAmount",
                "beneficiaries.totalSharePercent": "beneficiaryTotalPercent",
                "illustrationRequired": "illustrationRequired",
            },
            output_key="eligibility",
        ),
        StepDefinition(
            name="calculate_premium",
            handler=invoke("policy.premium.calculate", side_hint="compute"),
            depends_on=("check_eligibility",),
            retry=RetryPolicy(max_attempts=3, initial_delay_seconds=0.2),
            timeout_seconds=15.0,
            input_template={
                "productCode": "productCode",
                "faceAmount": "faceAmount",
                "issueAge": "applicant.age",
                "tobacco": "applicant.tobacco",
                "termYears": "termYears",
                "paymentMode": "paymentMode",
                "stateOfIssue": "stateOfIssue",
            },
            output_key="premium",
        ),
        StepDefinition(
            name="create_quote",
            handler=invoke("policy.quote.create", side_hint="write"),
            depends_on=("calculate_premium",),
            retry=RetryPolicy(max_attempts=2, initial_delay_seconds=0.5),
            timeout_seconds=20.0,
            input_template={
                "productCode": "productCode",
                "faceAmount": "faceAmount",
                "issueAge": "applicant.age",
                "stateOfIssue": "stateOfIssue",
                "paymentMode": "paymentMode",
            },
            output_key="quote",
            compensate_with="withdraw_quote",
        ),
        StepDefinition(
            name="confirm_acceptance",
            handler=confirm_acceptance,
            depends_on=("create_quote",),
            timeout_seconds=5.0,
            output_key="acceptance",
            input_template={
                "customerAccepted": "customerAccepted",
                "acceptedBy": "acceptedBy",
                "quoteId": "quote.quoteId",
                "effectiveDate": "effectiveDate",
            },
        ),
        StepDefinition(
            name="bind_policy",
            handler=invoke("policy.bind", side_hint="write"),
            depends_on=("confirm_acceptance",),
            retry=RetryPolicy(max_attempts=2, initial_delay_seconds=1.0),
            timeout_seconds=30.0,
            input_template={
                "quoteId": "quote.quoteId",
                "effectiveDate": "effectiveDate",
                "payor": "payor",
            },
            output_key="policy",
            compensate_with="void_policy",
        ),
        StepDefinition(
            name="withdraw_quote",
            handler=withdraw_quote,
            compensation_only=True,
            timeout_seconds=15.0,
        ),
        StepDefinition(
            name="void_policy",
            handler=void_policy,
            compensation_only=True,
            timeout_seconds=20.0,
        ),
    ),
)


ACCELERATED_UW = WorkflowDefinition(
    workflow_id="accelerated-underwriting",
    name="Accelerated underwriting decision",
    description=(
        "Complexity-score the submission, retrieve MIB and health data subject to consent, "
        "run the appetite rules, then either decide automatically or refer to an underwriter. "
        "A decline never happens without an explainable reason code."
    ),
    tags=("underwriting", "decision"),
    slo_ms=1200,
    steps=(
        StepDefinition(name="score_complexity", handler=score_complexity, timeout_seconds=5.0),
        StepDefinition(
            name="retrieve_mib",
            handler=retrieve_mib,
            depends_on=("score_complexity",),
            timeout_seconds=15.0,
            optional=True,
        ),
        StepDefinition(
            name="retrieve_health",
            handler=retrieve_health,
            depends_on=("retrieve_mib",),
            timeout_seconds=15.0,
            optional=True,
        ),
        StepDefinition(
            name="evaluate_appetite",
            handler=evaluate_appetite,
            depends_on=("retrieve_health",),
            timeout_seconds=5.0,
        ),
        StepDefinition(
            name="issue_decision",
            handler=issue_decision,
            depends_on=("evaluate_appetite",),
            timeout_seconds=10.0,
        ),
    ),
)


BENEFICIARY_CHANGE = WorkflowDefinition(
    workflow_id="beneficiary-change",
    name="Beneficiary change with multi-signature approval",
    description=(
        "Reads the current schedule, validates the proposed one, then applies it. The applied "
        "change is also committed to the plugin 7 policy lifecycle ledger so the approval "
        "record is immutable."
    ),
    tags=("servicing", "beneficiary", "compliance"),
    slo_ms=2500,
    steps=(
        StepDefinition(
            name="read_current",
            handler=invoke("policy.beneficiary.list"),
            timeout_seconds=10.0,
            output_key="currentBeneficiaries",
            input_template={"policyId": "policyId"},
        ),
        StepDefinition(
            name="validate_allocation",
            handler=validate_allocation,
            depends_on=("read_current",),
            timeout_seconds=5.0,
            input_template={
                "policyId": "policyId",
                "allocations": "allocations",
            },
        ),
        StepDefinition(
            name="apply_change",
            handler=invoke("policy.beneficiary.update", side_hint="write"),
            depends_on=("validate_allocation",),
            timeout_seconds=20.0,
            input_template={
                "policyId": "policyId",
                "allocations": "allocations",
                "effectiveDate": "effectiveDate",
                "requireAllSignatures": "requireAllSignatures",
            },
            output_key="applied",
        ),
        StepDefinition(
            name="record_to_ledger",
            handler=record_to_ledger,
            depends_on=("apply_change",),
            timeout_seconds=10.0,
            optional=True,
            input_template={
                "policyId": "policyId",
                "allocations": "allocations",
                "effectiveDate": "effectiveDate",
                "applied": "applied",
            },
        ),
    ),
)

PREMIUM_RECALC = WorkflowDefinition(
    workflow_id="premium-recalculation",
    name="Premium recalculation after a coverage change",
    description=(
        "Detects that a cached quote is stale after a face-amount or coverage change, "
        "re-rates the policy and reports the delta so consent can be obtained first."
    ),
    tags=("servicing", "rating"),
    slo_ms=1500,
    steps=(
        StepDefinition(
            name="assess_change",
            handler=assess_change,
            timeout_seconds=5.0,
            input_template={
                "change.faceAmount": "change.faceAmount",
                "change.coverage": "change.coverage",
                "change.rateClass": "change.rateClass",
                "change.paymentMode": "change.paymentMode",
            },
            output_key="changeAssessment",
        ),
        StepDefinition(
            name="rerate",
            handler=invoke("policy.premium.calculate", side_hint="compute"),
            depends_on=("assess_change",),
            optional=True,
            timeout_seconds=15.0,
            input_template={
                "productCode": "productCode",
                "faceAmount": "faceAmount",
                "issueAge": "issueAge",
                "paymentMode": "paymentMode",
                "stateOfIssue": "stateOfIssue",
            },
            output_key="reratedPremium",
        ),
        StepDefinition(
            name="compute_delta",
            handler=compute_delta,
            depends_on=("rerate",),
            timeout_seconds=5.0,
            input_template={
                "currentAnnualisedPremium": "currentAnnualisedPremium",
                "reratedPremium": "reratedPremium",
            },
            output_key="delta",
        ),
    ),
)


LAPSE_PREVENTION = WorkflowDefinition(
    workflow_id="lapse-prevention",
    name="Lapse prevention outreach",
    description=(
        "Identifies policies approaching lapse, ranks them by recoverable premium and "
        "raises outreach tasks with the reason each policy is at risk."
    ),
    tags=("retention", "servicing"),
    slo_ms=1000,
    steps=(
        StepDefinition(name="identify_at_risk", handler=identify_at_risk, timeout_seconds=10.0),
        StepDefinition(
            name="rank_by_value",
            handler=rank_by_value,
            depends_on=("identify_at_risk",),
            timeout_seconds=5.0,
        ),
        StepDefinition(
            name="create_outreach_tasks",
            handler=create_outreach_tasks,
            depends_on=("rank_by_value",),
            timeout_seconds=10.0,
        ),
    ),
)


WORKFLOWS: tuple[WorkflowDefinition, ...] = (
    QUOTE_TO_BIND,
    ACCELERATED_UW,
    BENEFICIARY_CHANGE,
    PREMIUM_RECALC,
    LAPSE_PREVENTION,
)

WORKFLOWS_BY_ID = {w.workflow_id: w for w in WORKFLOWS}


def describe(workflows: tuple[WorkflowDefinition, ...] = WORKFLOWS) -> dict[str, Any]:
    """MCP resource / API projection of the workflow catalogue."""
    return describe_workflows(workflows)


def build_local_engine(invoker: Any = None) -> WorkflowEngine:  # noqa: ANN401
    """Construct a local engine with every bundled workflow registered."""
    from pas_core.workflow.engine import LocalWorkflowEngine  # noqa: PLC0415
    from pas_plugins.plugin1_gateway.service import get_operation_invoker  # noqa: PLC0415

    engine = LocalWorkflowEngine(operation_invoker=invoker or get_operation_invoker())
    engine.register_all(WORKFLOWS)
    return engine


def summarise() -> dict[str, Any]:
    """Counts used by the ``/workflows`` endpoint."""
    return {
        "count": len(WORKFLOWS),
        "totalSteps": sum(len(w.steps) for w in WORKFLOWS),
        "workflows": [
            {
                "workflowId": w.workflow_id,
                "name": w.name,
                "stepCount": len(w.steps),
                "executionOrder": w.topological_order(),
                "hasCompensation": any(s.compensate_with for s in w.steps),
                "sloMs": w.slo_ms,
            }
            for w in WORKFLOWS
        ],
    }


def is_terminal(status: WorkflowStatus) -> bool:
    return status in {
        WorkflowStatus.SUCCEEDED,
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
        WorkflowStatus.TIMED_OUT,
        WorkflowStatus.COMPENSATED,
    }
