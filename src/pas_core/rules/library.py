"""Bundled rule sets: underwriting appetite, product eligibility, workflow gates.

These are starting points a carrier actuary edits - not legal or actuarial advice.
Every rule carries a description stating its intent so the reviewer UI and the
audit trail show *why* a decision was made.
"""

from __future__ import annotations

from typing import Any

from pas_core.rules.engine import RuleEngine, RuleSet

APPEtite_RULES: list[dict[str, Any]] = [
    {
        "ruleId": "APP-001",
        "name": "Issue age within product limits",
        "description": "Decline-to-refer outside the filed issue-age range for the product.",
        "when": {"mode": "any", "conditions": [
            {"field": "applicant.age", "operator": "<", "value": 18},
            {"field": "applicant.age", "operator": ">", "value": 80},
        ]},
        "then": [{"kind": "reason_code", "target": "AGE_OUT_OF_RANGE",
                  "value": "AGE_OUT_OF_RANGE",
                  "message": "Issue age is outside the filed range for this product."}],
        "severity": "blocking",
        "outcome": "fail",
        "tags": ["appetite", "age"],
    },
    {
        "ruleId": "APP-002",
        "name": "Face amount within underwriting authority",
        "description": "Amounts above the automated authority must go to an underwriter.",
        "when": {"field": "request.faceAmount", "operator": ">", "value": 5_000_000},
        "then": [
            {"kind": "flag", "target": "manualReviewRequired", "value": True},
            {"kind": "reason_code", "target": "ABOVE_AUTHORITY", "value": "ABOVE_AUTHORITY",
             "message": "Face amount exceeds automated underwriting authority."},
        ],
        "severity": "warning",
        "outcome": "review",
        "tags": ["appetite", "authority"],
    },
    {
        "ruleId": "APP-003",
        "name": "Tobacco surcharge review",
        "description": "Tobacco applicants always receive a manual review at face amounts over 1M.",
        "when": {"mode": "all", "conditions": [
            {"field": "applicant.tobacco", "operator": "==", "value": True},
            {"field": "request.faceAmount", "operator": ">", "value": 1_000_000},
        ]},
        "then": [
            {"kind": "reason_code", "target": "TOBACCO_REVIEW", "value": "TOBACCO_REVIEW",
             "message": "Tobacco use above 1M face requires underwriter review."},
        ],
        "severity": "warning",
        "outcome": "review",
        "tags": ["appetite", "tobacco"],
    },
    {
        "ruleId": "APP-004",
        "name": "Contested EIG rejection",
        "description": "Decline when an application contests an existing Insureability Review.",
        "when": {"field": "mig.reviewStatus", "operator": "in", "value": ["contested", "upheld"]},
        "then": [
            {"kind": "reason_code", "target": "EIG_CONTESTED", "value": "EIG_CONTESTED",
             "message": "Applicant contested an existing Insureability Review (MIB)."},
        ],
        "severity": "blocking",
        "outcome": "fail",
        "tags": ["appetite", "mib"],
    },
    {
        "ruleId": "APP-005",
        "name": "Missing paramed exam within 30 days",
        "description": "Require a paramed exam before a decision can be issued.",
        "when": {"field": "medical.paramedExamRequired", "operator": "==", "value": True},
        "then": [
            {"kind": "require", "target": "PARAMED_EXAM", "value": True,
             "message": "Paramed examination result is outstanding."},
            {"kind": "reason_code", "target": "MEDICAL_OUTSTANDING", "value": "MEDICAL_OUTSTANDING",
             "message": "Decision blocked pending medical evidence."},
        ],
        "severity": "blocking",
        "outcome": "review",
        "tags": ["medical"],
    },
    {
        "ruleId": "APP-006",
        "name": "State eligibility",
        "description": "Refer applications written in a state where the product is not filed.",
        "when": {"field": "product.filedStates", "operator": "contains", "value": "XX"},
        "then": [
            {"kind": "reason_code", "target": "NOT_FILED_IN_STATE", "value": "NOT_FILED_IN_STATE",
             "message": "Product is not filed in the applicant state."},
        ],
        "severity": "blocking",
        "outcome": "fail",
        "tags": ["compliance", "state-filing"],
    },
    {
        "ruleId": "APP-007",
        "name": "Sanctions and PEP screening",
        "description": "Hard stop for sanctions or politically-exposed-person matches.",
        "when": {"mode": "any", "conditions": [
            {"field": "screening.sanctionsMatch", "operator": "==", "value": True},
            {"field": "screening.pepMatch", "operator": "==", "value": True},
        ]},
        "then": [
            {"kind": "reason_code", "target": "SANCTIONS_MATCH", "value": "SANCTIONS_MATCH",
             "message": "Sanctions or PEP screening match - escalate to compliance."},
        ],
        "severity": "blocking",
        "outcome": "fail",
        "tags": ["compliance", "aml"],
    },
]

ELIGIBILITY_RULES: list[dict[str, Any]] = [
    {
        "ruleId": "ELG-001",
        "name": "Minimum issue age",
        "description": "Products may define a higher minimum issue age than 18.",
        "when": {"field": "applicant.age", "operator": "<", "value": 18},
        "then": [{"kind": "reason_code", "target": "BELOW_MIN_AGE", "value": "BELOW_MIN_AGE",
                  "message": "Applicant is below the product minimum issue age."}],
        "severity": "blocking",
        "outcome": "fail",
        "tags": ["eligibility"],
    },
    {
        "ruleId": "ELG-002",
        "name": "Minimum face amount",
        "description": "Face amount must meet the product minimum.",
        "when": {"field": "request.faceAmount", "operator": "<", "value": 25_000},
        "then": [{"kind": "reason_code", "target": "BELOW_MIN_FACE", "value": "BELOW_MIN_FACE",
                  "message": "Requested face amount is below the product minimum."}],
        "severity": "blocking",
        "outcome": "fail",
        "tags": ["eligibility", "face-amount"],
    },
    {
        "ruleId": "ELG-003",
        "name": "Maximum face amount",
        "description": "Face amount must not exceed the filed maximum.",
        "when": {"field": "request.faceAmount", "operator": ">", "value": 20_000_000},
        "then": [{"kind": "reason_code", "target": "ABOVE_MAX_FACE", "value": "ABOVE_MAX_FACE",
                  "message": "Requested face amount exceeds the filed maximum."}],
        "severity": "blocking",
        "outcome": "fail",
        "tags": ["eligibility", "face-amount"],
    },
    {
        "ruleId": "ELG-004",
        "name": "Beneficiary completeness",
        "description": "Primary beneficiary designation is required before issue.",
        "when": {"field": "beneficiaries.totalSharePercent", "operator": "<", "value": 100},
        "then": [
            {"kind": "require", "target": "BENEFICIARY_DESIGNATION", "value": True,
             "message": "Beneficiary shares must total 100%."},
        ],
        "severity": "blocking",
        "outcome": "review",
        "tags": ["beneficiary", "compliance"],
    },
    {
        "ruleId": "ELG-005",
        "name": "Illustration acknowledgement",
        "description": "Life and annuity illustrations require a signed acknowledgement.",
        "when": {"field": "illustrationRequired", "operator": "==", "value": True},
        "then": [
            {"kind": "require", "target": "ILLUSTRATION_ACK", "value": True,
             "message": "Signed illustration acknowledgement is required."},
        ],
        "severity": "blocking",
        "outcome": "review",
        "tags": ["illustration", "compliance"],
    },
]

WORKFLOW_GATES: list[dict[str, Any]] = [
    {
        "ruleId": "WF-001",
        "name": "Bind requires an accepted quote",
        "description": "A policy cannot be issued unless a quote exists and is in accepted status.",
        "when": {"mode": "any", "conditions": [
            {"field": "quote", "operator": "exists", "value": False},
            {"field": "quote.status", "operator": "not_in", "value": ["accepted", "converted"]},
        ]},
        "then": [{"kind": "reason_code", "target": "NO_ACCEPTED_QUOTE", "value": "NO_ACCEPTED_QUOTE",
                  "message": "Binding requires an accepted quote."}],
        "severity": "blocking",
        "outcome": "fail",
        "tags": ["workflow", "bind"],
    },
    {
        "ruleId": "WF-002",
        "name": "Bind requires a resolved underwriting decision",
        "description": "Refer or pending decisions block issuance.",
        "when": {"field": "underwriting.decision", "operator": "in",
                 "value": ["refer", "pending", "deferred"]},
        "then": [{"kind": "reason_code", "target": "UW_NOT_RESOLVED", "value": "UW_NOT_RESOLVED",
                  "message": "Underwriting decision must be resolved before bind."}],
        "severity": "blocking",
        "outcome": "fail",
        "tags": ["workflow", "underwriting"],
    },
    {
        "ruleId": "WF-003",
        "name": "Payment method required for bind",
        "description": "An initial premium must be funded before the policy is issued.",
        "when": {"field": "payment.method", "operator": "is_null", "value": True},
        "then": [
            {"kind": "reason_code", "target": "PAYMENT_REQUIRED", "value": "PAYMENT_REQUIRED",
             "message": "A payment method is required to bind this policy."},
        ],
        "severity": "blocking",
        "outcome": "fail",
        "tags": ["workflow", "premium"],
    },
    {
        "ruleId": "WF-004",
        "name": "Recalculate premium when face amount changes",
        "description": "A face-amount change invalidates any cached quote.",
        "when": {"mode": "all", "conditions": [
            {"field": "change.faceAmountChanged", "operator": "==", "value": True},
            {"field": "quote.status", "operator": "==", "value": "accepted"},
        ]},
        "then": [
            {"kind": "flag", "target": "recalculatePremium", "value": True},
            {"kind": "reason_code", "target": "QUOTE_STALE", "value": "QUOTE_STALE",
             "message": "Face amount changed - the accepted quote is no longer valid."},
        ],
        "severity": "blocking",
        "outcome": "review",
        "tags": ["workflow", "change"],
    },
]

BUILTIN_RULE_SETS: tuple[dict[str, Any], ...] = (
    {
        "ruleSetId": "uw-appetite-life",
        "name": "Life underwriting appetite",
        "version": 1,
        "domain": "underwriting",
        "description": "Carrier appetite and compliance gates for individual life applications.",
        "tags": ["appetite", "life", "auworkbench"],
        "rules": APPEtite_RULES,
    },
    {
        "ruleSetId": "product-eligibility-life",
        "name": "Life product eligibility",
        "version": 1,
        "domain": "eligibility",
        "description": "Product-level eligibility and issue requirements for life products.",
        "tags": ["eligibility", "product-config"],
        "rules": ELIGIBILITY_RULES,
    },
    {
        "ruleSetId": "workflow-gates",
        "name": "Workflow preconditions",
        "version": 1,
        "domain": "workflow",
        "description": "Preconditions enforced before a workflow step may execute.",
        "tags": ["workflow", "orchestration"],
        "rules": WORKFLOW_GATES,
    },
)


def load_builtin_rule_sets(engine: RuleEngine | None = None) -> RuleEngine:
    """Publish the bundled rule sets and return a ready-to-use engine.

    Idempotent: calling it repeatedly returns the same published versions rather
    than re-publishing, so it is safe to call from a request path.
    """
    target = engine or RuleEngine()
    for spec in BUILTIN_RULE_SETS:
        if target.store.versions(str(spec["ruleSetId"])):
            continue
        target.store.publish(RuleSet.from_spec(spec))
    return target


def default_engine() -> RuleEngine:
    """A process-wide engine preloaded with the bundled rule sets."""
    return load_builtin_rule_sets()
