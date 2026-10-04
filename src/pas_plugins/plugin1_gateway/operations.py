"""The atomic operation catalogue.

This module is the heart of plugin 1.  It takes the way carriers actually expose
their Policy Administration Systems - a handful of monolithic endpoints with a
``processType`` discriminator, or a SOAP service with dozens of loosely related
operations - and decomposes them into atomic, single-purpose operations.

The decomposition rules applied throughout:

* **One intent per operation.** ``policy.lapse`` does one thing. It does not also
  reinstate, change coverage, and recalculate premium.
* **No magic flags.** Every input is a declared, named parameter. There is no
  ``mode=07`` and no positional "operation code" string.
* **Complete schemas.** Every operation declares a full request and response
  JSON Schema plus worked examples.
* **A published error set.** Each operation declares exactly which catalogue
  codes it can return, so a client (or an agent) knows when to retry.
* **A declared side.** ``read`` / ``write`` / ``compute`` determines whether an
  idempotency key is required and whether the audit event is a read or a change.

Naming convention: ``<domain>.<noun>.<verb>``, e.g. ``policy.beneficiary.update``.
"""

from __future__ import annotations

from pas_core.acord.transaction import TransactionTypeCode
from pas_core.errors import ErrorCode
from pas_core.pas.base import (
    AtomicOperation,
    OpenApiParam,
    OperationRegistry,
    OperationSide,
    build_operation,
)
from pas_plugins.plugin1_gateway import models


def _policy_id_param() -> OpenApiParam:
    return OpenApiParam(
        name="policyId",
        location="path",
        type_="string",
        required=True,
        description="Carrier policy number. Case-insensitive; returned upper-case.",
        example="POL1001",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9\-/]{2,34}$",
    )


POLICY_TAG = "policy"
QUOTE_TAG = "quote"
SERVICING_TAG = "servicing"
BENEFICIARY_TAG = "beneficiary"
PRODUCT_TAG = "product"
APPLICATION_TAG = "application"
PARTY_TAG = "party"


def build_catalogue() -> OperationRegistry:
    """Assemble the full life & annuity atomic operation catalogue."""
    registry = OperationRegistry()
    registry.register_all(
        [
            _policy_get(),
            _policy_search(),
            _policy_status_get(),
            _premium_calculate(),
            _quote_create(),
            _quote_get(),
            _quote_list(),
            _policy_bind(),
            _policy_lapse(),
            _policy_reinstate(),
            _policy_surrender(),
            _policy_terminate(),
            _beneficiary_update(),
            _beneficiary_get(),
            _policy_cash_value(),
            _policy_loan_request(),
            _policy_events_get(),
            _product_get(),
            _product_search(),
            _application_submit(),
            _application_get(),
        ]
    )
    return registry


# ---------------------------------------------------------------------------
# Policy read
# ---------------------------------------------------------------------------
def _policy_get() -> AtomicOperation:
    return build_operation(
        operation_id="policy.get",
        summary="Retrieve a policy",
        intent="Read the current state of one policy by policy number",
        method="GET",
        path="/insurance/v1/policies/{policyId}",
        tags=(POLICY_TAG,),
        side=OperationSide.READ,
        description=(
            "Returns the full canonical policy record. This is the single read that an "
            "agent should use to understand a policy; it never mutates anything and is "
            "safe to call repeatedly."
        ),
        parameters=(_policy_id_param(),),
        response=models.PolicyDetail,
        errors=(ErrorCode.NOT_FOUND, ErrorCode.PAS_OPERATION_UNAVAILABLE),
        required_scopes=("policy:read",),
        rate_limit_policy="tenant-default",
        slo_ms=1000,
    )


def _policy_search() -> AtomicOperation:
    return build_operation(
        operation_id="policy.search",
        summary="Search policies",
        intent="List policies matching a filter, for reconciliation and reporting",
        method="POST",
        path="/insurance/v1/policies/search",
        tags=(POLICY_TAG,),
        side=OperationSide.READ,
        description=(
            "Paginated search across the caller's tenant. Filters are explicit and "
            "combinable; omitted filters match everything."
        ),
        request=models.PolicySearchRequest,
        response=models.PolicySearchResponse,
        errors=(ErrorCode.VALIDATION_FAILED,),
        required_scopes=("policy:read",),
        slo_ms=2000,
    )


def _policy_status_get() -> AtomicOperation:
    return build_operation(
        operation_id="policy.status.get",
        summary="Read policy status",
        intent="Check only the current status of a policy",
        method="GET",
        path="/insurance/v1/policies/{policyId}/status",
        tags=(POLICY_TAG,),
        side=OperationSide.READ,
        description=(
            "A cheap status probe. Use this instead of `policy.get` when the status is the "
            "only thing needed - it is materially faster on a loaded PAS."
        ),
        parameters=(_policy_id_param(),),
        response=models.PolicyStatusResponse,
        errors=(ErrorCode.NOT_FOUND,),
        required_scopes=("policy:read",),
        slo_ms=400,
    )


def _policy_events_get() -> AtomicOperation:
    return build_operation(
        operation_id="policy.history.get",
        summary="Read policy event history",
        intent="See every lifecycle event recorded against a policy",
        method="GET",
        path="/insurance/v1/policies/{policyId}/events",
        tags=(POLICY_TAG, "history"),
        side=OperationSide.READ,
        description=(
            "Ordered lifecycle events: issue, changes, premium status changes, lapse, "
            "reinstatement, surrender. Correlates with the plugin 7 ledger history."
        ),
        parameters=(
            _policy_id_param(),
            OpenApiParam(
                name="since",
                location="query",
                type_="string",
                description="ISO-8601 date; only events on or after this date are returned.",
                example="2026-01-01",
            ),
        ),
        response=models.OperationResult,
        errors=(ErrorCode.NOT_FOUND,),
        required_scopes=("policy:read",),
        slo_ms=1500,
    )


# ---------------------------------------------------------------------------
# Rating and quoting
# ---------------------------------------------------------------------------
def _premium_calculate() -> AtomicOperation:
    return build_operation(
        operation_id="policy.premium.calculate",
        summary="Calculate a premium",
        intent="Price a hypothetical policy without creating anything",
        method="POST",
        path="/insurance/v1/policies/premium/calculate",
        tags=(QUOTE_TAG, "rating"),
        side=OperationSide.COMPUTE,
        description=(
            "Pure rating computation. Nothing is written to the carrier's records, so it is "
            "safe for an agent to call speculatively while exploring options. Returns the "
            "rating-factor breakdown so an underwriter can challenge individual components."
        ),
        request=models.PremiumCalculationRequest,
        response=models.PremiumCalculationResponse,
        errors=(ErrorCode.VALIDATION_FAILED, ErrorCode.PAS_UNAVAILABLE),
        required_scopes=("policy:rate",),
        rate_limit_policy="embed-quote",
        slo_ms=800,
    )


def _quote_create() -> AtomicOperation:
    return build_operation(
        operation_id="policy.quote.create",
        summary="Create a quote",
        intent="Persist a price that can later be converted into a policy",
        method="POST",
        path="/insurance/v1/quotes",
        tags=(QUOTE_TAG,),
        side=OperationSide.WRITE,
        description=(
            "Creates a bindable quote with a validity window. Re-sending the same "
            "Idempotency-Key returns the original quote rather than creating a duplicate. "
            "An accepted quote is the only legal input to `policy.bind`."
        ),
        request=models.QuoteRequest,
        response=models.Quote,
        errors=(ErrorCode.VALIDATION_FAILED, ErrorCode.QUOTE_NOT_ACCEPTABLE, ErrorCode.CONFLICT),
        required_scopes=("policy:write",),
        acord_tx_type=TransactionTypeCode.TX101_LIFE_APPLICATION_SUBMISSION,
        rate_limit_policy="embed-quote",
        slo_ms=900,
    )


def _quote_get() -> AtomicOperation:
    return build_operation(
        operation_id="policy.quote.get",
        summary="Retrieve a quote",
        intent="Read back a previously created quote",
        method="GET",
        path="/insurance/v1/quotes/{quoteId}",
        tags=(QUOTE_TAG,),
        side=OperationSide.READ,
        parameters=(
            OpenApiParam(
                name="quoteId",
                location="path",
                type_="string",
                required=True,
                description="Quote identifier returned by the create-quote operation.",
                example="QT7F3A9B21C",
            ),
        ),
        response=models.Quote,
        errors=(ErrorCode.NOT_FOUND,),
        required_scopes=("policy:read",),
        slo_ms=400,
    )


def _quote_list() -> AtomicOperation:
    return build_operation(
        operation_id="policy.quote.list",
        summary="List recent quotes",
        intent="Find quotes created recently, for conversion reporting",
        method="GET",
        path="/insurance/v1/quotes",
        tags=(QUOTE_TAG,),
        side=OperationSide.READ,
        parameters=(
            OpenApiParam(
                name="status",
                location="query",
                type_="string",
                description="Filter by quote status: quoted, accepted, expired, converted.",
                example="quoted",
                enum=["quoted", "accepted", "expired", "converted", "withdrawn"],
            ),
            OpenApiParam(
                name="limit",
                location="query",
                type_="integer",
                description="Maximum rows to return.",
                example=25,
                minimum=1,
            ),
        ),
        response=models.OperationResult,
        errors=(),
        required_scopes=("policy:read",),
        slo_ms=800,
    )


# ---------------------------------------------------------------------------
# Issue and bind
# ---------------------------------------------------------------------------
def _policy_bind() -> AtomicOperation:
    return build_operation(
        operation_id="policy.bind",
        summary="Bind a policy from a quote",
        intent="Convert an accepted quote into an issued, in-force policy",
        method="POST",
        path="/insurance/v1/policies",
        tags=(POLICY_TAG, QUOTE_TAG, "issue"),
        side=OperationSide.WRITE,
        description=(
            "Issues a policy. Requires an accepted quote; the workflow in plugin 1 validates "
            "eligibility, underwriting status and funding before calling this operation. "
            "Always send an Idempotency-Key: a retried bind against a live PAS would "
            "otherwise create a second policy."
        ),
        request=models.BindRequest,
        response=models.BindResponse,
        errors=(
            ErrorCode.VALIDATION_FAILED,
            ErrorCode.QUOTE_NOT_ACCEPTABLE,
            ErrorCode.BUSINESS_RULE_VIOLATION,
            ErrorCode.PAS_UNAVAILABLE,
            ErrorCode.IDEMPOTENCY_REPLAY,
        ),
        required_scopes=("policy:write",),
        acord_tx_type=TransactionTypeCode.TX105_LIFE_POLICY_ISSUE,
        rate_limit_policy="embed-bind",
        slo_ms=2000,
    )


# ---------------------------------------------------------------------------
# Servicing
# ---------------------------------------------------------------------------
def _servicing_operation(
    *,
    operation_id: str,
    summary: str,
    intent: str,
    path_suffix: str,
    status: str,
    tx_type: TransactionTypeCode,
    errors: tuple[ErrorCode, ...],
) -> AtomicOperation:
    return build_operation(
        operation_id=operation_id,
        summary=summary,
        intent=intent,
        method="POST",
        path=f"/insurance/v1/policies/{{policyId}}/{path_suffix}",
        tags=(SERVICING_TAG, POLICY_TAG),
        side=OperationSide.WRITE,
        description=(
            f"Moves the policy to '{status}'. The request must carry confirmation=true: "
            "policy-state changes are irreversible in most cores, and an agent must state "
            "its intent explicitly rather than firing a mutation by side effect."
        ),
        parameters=(_policy_id_param(),),
        request=models.ServicingRequest,
        response=models.PolicyStatusChangeResponse,
        errors=errors,
        required_scopes=("policy:write",),
        acord_tx_type=tx_type,
        rate_limit_policy="tenant-default",
        slo_ms=1500,
    )


def _policy_lapse() -> AtomicOperation:
    return _servicing_operation(
        operation_id="policy.lapse",
        summary="Lapse a policy",
        intent="Move an in-force policy to lapsed status",
        path_suffix="lapse",
        status="lapsed",
        tx_type=TransactionTypeCode.TX120_LIFE_POLICY_LAPSE_REINSTATEMENT,
        errors=(ErrorCode.NOT_FOUND, ErrorCode.BUSINESS_RULE_VIOLATION, ErrorCode.VERSION_CONFLICT),
    )


def _policy_reinstate() -> AtomicOperation:
    return _servicing_operation(
        operation_id="policy.reinstate",
        summary="Reinstate a policy",
        intent="Return a lapsed policy to active status",
        path_suffix="reinstate",
        status="active",
        tx_type=TransactionTypeCode.TX120_LIFE_POLICY_LAPSE_REINSTATEMENT,
        errors=(ErrorCode.NOT_FOUND, ErrorCode.BUSINESS_RULE_VIOLATION),
    )


def _policy_surrender() -> AtomicOperation:
    return _servicing_operation(
        operation_id="policy.surrender",
        summary="Surrender a policy",
        intent="Surrender an in-force policy for its cash surrender value",
        path_suffix="surrender",
        status="surrendered",
        tx_type=TransactionTypeCode.TX106_LIFE_POLICY_TRANSACTION,
        errors=(
            ErrorCode.NOT_FOUND,
            ErrorCode.BUSINESS_RULE_VIOLATION,
            ErrorCode.VERSION_CONFLICT,
            ErrorCode.PAS_UNAVAILABLE,
        ),
    )


def _policy_terminate() -> AtomicOperation:
    return _servicing_operation(
        operation_id="policy.terminate",
        summary="Terminate a policy",
        intent="Terminate a policy without a cash payout, e.g. on underwriting grounds",
        path_suffix="terminate",
        status="terminated",
        tx_type=TransactionTypeCode.TX106_LIFE_POLICY_TRANSACTION,
        errors=(ErrorCode.NOT_FOUND, ErrorCode.PERMISSION_DENIED, ErrorCode.BUSINESS_RULE_VIOLATION),
    )


# ---------------------------------------------------------------------------
# Beneficiary
# ---------------------------------------------------------------------------
def _beneficiary_update() -> AtomicOperation:
    return build_operation(
        operation_id="policy.beneficiary.update",
        summary="Replace the beneficiary schedule",
        intent="Change who receives the death benefit",
        method="PUT",
        path="/insurance/v1/policies/{policyId}/beneficiaries",
        tags=(BENEFICIARY_TAG, POLICY_TAG),
        side=OperationSide.WRITE,
        description=(
            "Replaces the whole beneficiary schedule (PUT semantics - not a delta). Shares "
            "may total less than 100 percent, in which case the remainder stays with the "
            "existing default beneficiary; they may never exceed 100 percent. With "
            "requireAllSignatures the change is staged pending counter-signature and is "
            "committed to the plugin 7 ledger once every party has signed."
        ),
        parameters=(_policy_id_param(),),
        request=models.BeneficiaryUpdateRequest,
        response=models.BeneficiaryUpdateResponse,
        errors=(
            ErrorCode.NOT_FOUND,
            ErrorCode.BUSINESS_RULE_VIOLATION,
            ErrorCode.PERMISSION_DENIED,
        ),
        required_scopes=("policy:write", "beneficiary:write"),
        acord_tx_type=TransactionTypeCode.TX118_LIFE_POLICY_CHANGE_REQUEST,
        slo_ms=1800,
    )


def _beneficiary_get() -> AtomicOperation:
    return build_operation(
        operation_id="policy.beneficiary.list",
        summary="List beneficiaries",
        intent="See the current beneficiary schedule and signature state",
        method="GET",
        path="/insurance/v1/policies/{policyId}/beneficiaries",
        tags=(BENEFICIARY_TAG, POLICY_TAG),
        side=OperationSide.READ,
        parameters=(_policy_id_param(),),
        response=models.OperationResult,
        errors=(ErrorCode.NOT_FOUND,),
        required_scopes=("policy:read",),
        slo_ms=700,
    )


# ---------------------------------------------------------------------------
# Cash value and loans
# ---------------------------------------------------------------------------
def _policy_cash_value() -> AtomicOperation:
    return build_operation(
        operation_id="policy.cashValue.get",
        summary="Project cash value",
        intent="Read cash, surrender and net cash values for a policy",
        method="POST",
        path="/insurance/v1/policies/{policyId}/cash-value",
        tags=(POLICY_TAG, "financial"),
        side=OperationSide.COMPUTE,
        description=(
            "Returns the three figures a surrender conversation needs, and can project "
            "them forward. netCashValue is cashValue less any outstanding policy loan."
        ),
        parameters=(_policy_id_param(),),
        request=models.CashValueRequest,
        response=models.CashValueResponse,
        errors=(ErrorCode.NOT_FOUND, ErrorCode.VALIDATION_FAILED),
        required_scopes=("policy:read",),
        slo_ms=1200,
    )


def _policy_loan_request() -> AtomicOperation:
    return build_operation(
        operation_id="policy.loan.request",
        summary="Request a policy loan",
        intent="Borrow against a policy's cash value",
        method="POST",
        path="/insurance/v1/policies/{policyId}/loans",
        tags=(POLICY_TAG, "financial"),
        side=OperationSide.WRITE,
        description=(
            "Advances cash against the policy's surrender value. The PAS enforces the "
            "maximum loan percentage and the free-look restriction on new policies."
        ),
        parameters=(_policy_id_param(),),
        request=models.PolicyLoanRequest,
        response=models.PolicyLoanResponse,
        errors=(
            ErrorCode.VALIDATION_FAILED,
            ErrorCode.NOT_FOUND,
            ErrorCode.BUSINESS_RULE_VIOLATION,
        ),
        required_scopes=("policy:write",),
        acord_tx_type=TransactionTypeCode.TX116_LIFE_POLICY_LOAN_TRANSACTION,
        slo_ms=1600,
    )


# ---------------------------------------------------------------------------
# Product catalogue
# ---------------------------------------------------------------------------
def _product_get() -> AtomicOperation:
    return build_operation(
        operation_id="product.get",
        summary="Retrieve a product definition",
        intent="Read what a product is and what it allows",
        method="GET",
        path="/insurance/v1/products/{productCode}",
        tags=(PRODUCT_TAG,),
        side=OperationSide.READ,
        description=(
            "Issue ages, face-amount bounds, riders and filed states. This is what an agent "
            "should read before quoting a product it has not seen before."
        ),
        parameters=(
            OpenApiParam(
                name="productCode",
                location="path",
                type_="string",
                required=True,
                description="Product code, e.g. TERM20-A.",
                example="TERM20-A",
            ),
        ),
        response=models.ProductDetail,
        errors=(ErrorCode.NOT_FOUND,),
        required_scopes=("product:read",),
        slo_ms=500,
    )


def _product_search() -> AtomicOperation:
    return build_operation(
        operation_id="product.search",
        summary="List available products",
        intent="Discover which products a carrier will currently sell",
        method="GET",
        path="/insurance/v1/products",
        tags=(PRODUCT_TAG,),
        side=OperationSide.READ,
        parameters=(
            OpenApiParam(
                name="productType",
                location="query",
                type_="string",
                description="Filter by product type, e.g. term or fixedIndexedAnnuity.",
                enum=[
                    "term", "wholeLife", "universalLife", "indexedUniversalLife",
                    "variableUniversalLife", "fixedIndexedAnnuity", "variableAnnuity", "longTermCare",
                ],
            ),
            OpenApiParam(name="limit", location="query", type_="integer", description="Page size.", example=25, minimum=1),
        ),
        response=models.ProductSearchResponse,
        errors=(),
        required_scopes=("product:read",),
        slo_ms=600,
    )


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
def _application_submit() -> AtomicOperation:
    return build_operation(
        operation_id="application.submit",
        summary="Submit an application for underwriting",
        intent="Start underwriting on a new application",
        method="POST",
        path="/insurance/v1/applications",
        tags=(APPLICATION_TAG,),
        side=OperationSide.WRITE,
        description=(
            "Submits an application into the carrier's underwriting queue. Plugin 3's "
            "workbench calls this after triage; direct API clients call it when they want "
            "the carrier's own underwriter rather than an automated decision."
        ),
        request=models.ApplicationSubmitRequest,
        response=models.ApplicationSubmitResponse,
        errors=(ErrorCode.VALIDATION_FAILED, ErrorCode.BUSINESS_RULE_VIOLATION, ErrorCode.PAS_UNAVAILABLE),
        required_scopes=("application:write",),
        acord_tx_type=TransactionTypeCode.TX101_LIFE_APPLICATION_SUBMISSION,
        rate_limit_policy="uw-decision",
        slo_ms=1500,
    )


def _application_get() -> AtomicOperation:
    return build_operation(
        operation_id="application.get",
        summary="Retrieve an application",
        intent="Check the underwriting status and outstanding requirements",
        method="GET",
        path="/insurance/v1/applications/{applicationId}",
        tags=(APPLICATION_TAG,),
        side=OperationSide.READ,
        parameters=(
            OpenApiParam(
                name="applicationId",
                location="path",
                type_="string",
                required=True,
                description="Application identifier returned by the submit operation.",
                example="APP7F3A9B21C",
            ),
        ),
        response=models.ApplicationStatusResponse,
        errors=(ErrorCode.NOT_FOUND,),
        required_scopes=("application:read",),
        slo_ms=600,
    )


CATALOGUE = build_catalogue()


def operation_ids() -> list[str]:
    """All operation ids in the catalogue."""
    return CATALOGUE.ids()


def catalogue_summary() -> dict[str, int]:
    """Counts used by the management UI and the OpenAPI extensions."""
    operations = CATALOGUE.list()
    return {
        "total": len(operations),
        "reads": sum(1 for o in operations if o.side is OperationSide.READ),
        "writes": sum(1 for o in operations if o.side is OperationSide.WRITE),
        "compute": sum(1 for o in operations if o.side is OperationSide.COMPUTE),
        "tags": len(CATALOGUE.by_tag()),
    }
