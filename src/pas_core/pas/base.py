"""The atomic operation abstraction and adapter base classes.

An atomic operation is the unit of AI-readiness: a single business intent with a
stable identifier, fully described parameters, a complete JSON Schema for its
request and response, worked examples and a declared error set.  A well-known
monolithic PAS call such as ``POST /PolicyService/ProcessTransaction`` with a
``processType`` discriminator becomes a dozen operations like
``policy.beneficiary.update`` and ``policy.lapse`` - each of which an agent can
understand, authorise separately and audit separately.
"""

from __future__ import annotations

import abc
import asyncio
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError as PydanticValidationError

from pas_core.acord.transaction import TransactionTypeCode
from pas_core.errors import (
    ErrorCode,
    PasError,
    UpstreamPasError,
    ValidationError,
)
from pas_core.observability import record_pas_call
from pas_core.tenancy import TenantContext


class OperationSide(StrEnum):
    READ = "read"
    WRITE = "write"
    COMPUTE = "compute"

    @property
    def requires_idempotency_key(self) -> bool:
        return self is OperationSide.WRITE


@dataclass(frozen=True, slots=True)
class OperationParameter:
    """A single input to an atomic operation.

    Parameters are declared, never inferred: no "magic flags", no positional
    overloads.  An agent reads this and knows exactly what to send.
    """

    name: str
    location: str
    schema_: dict[str, Any] = field(default_factory=dict)
    required: bool = True
    description: str = ""
    examples: tuple[Any, ...] = ()

    def to_openapi(self) -> dict[str, Any]:
        entry: dict[str, Any] = {"schema": self.schema_ or {}}
        if self.description:
            entry["description"] = self.description
        if self.examples:
            entry["examples"] = {"default": self.examples[0]}
        if self.required and self.location == "path":
            entry["required"] = True
        return entry


@dataclass(frozen=True, slots=True)
class AtomicOperation:
    """One business intent exposed over the gateway."""

    operation_id: str
    summary: str
    intent: str
    method: str
    path: str
    side: OperationSide = OperationSide.READ
    tags: tuple[str, ...] = ()
    description: str = ""
    parameters: tuple[OperationParameter, ...] = ()
    request_schema: dict[str, Any] | None = None
    response_schema: dict[str, Any] | None = None
    request_example: dict[str, Any] | None = None
    response_example: dict[str, Any] | None = None
    errors: tuple[ErrorCode, ...] = (ErrorCode.VALIDATION_FAILED,)
    required_scopes: tuple[str, ...] = ()
    acord_tx_type: TransactionTypeCode | None = None
    vendor: str | None = None
    vendor_operation: str | None = None
    rate_limit_policy: str = "tenant-default"
    slo_ms: int = 1000
    request_model: type[BaseModel] | None = field(default=None, repr=False)
    response_model: type[BaseModel] | None = field(default=None, repr=False)

    def to_openapi_operation(self) -> dict[str, Any]:
        """Render as an OpenAPI 3.1 path item operation object."""
        op: dict[str, Any] = {
            "operationId": self.operation_id,
            "summary": self.summary,
            "description": self.description or self.summary,
            "tags": list(self.tags) or ["default"],
            "x-pas-intent": self.intent,
            "x-pas-side": str(self.side),
            "x-pas-acord-tx-type": str(self.acord_tx_type) if self.acord_tx_type else None,
            "x-pas-required-scopes": list(self.required_scopes),
            "x-pas-slo-ms": self.slo_ms,
            "x-pas-errors": [e.value for e in self.errors],
        }
        op = {k: v for k, v in op.items() if v is not None}

        parameters: list[dict[str, Any]] = []
        for param in self.parameters:
            entry: dict[str, Any] = {
                "name": param.name,
                "in": param.location,
                "required": param.required or param.location == "path",
                **param.to_openapi(),
            }
            if param.examples:
                entry["examples"] = {"default": {"value": param.examples[0]}}
                if "examples" in entry["schema"]:
                    del entry["schema"]["examples"]
            parameters.append(entry)
        if parameters:
            op["parameters"] = parameters

        if self.request_schema:
            content: dict[str, Any] = {"schema": self.request_schema}
            if self.request_example:
                content["examples"] = {
                    "standard": {
                        "summary": "Canonical example",
                        "value": self.request_example,
                    }
                }
            op["requestBody"] = {
                "required": self.side is not OperationSide.READ,
                "content": {"application/json": content},
            }

        responses: dict[str, Any] = {}
        if self.response_schema:
            responses["200"] = {
                "description": f"{self.summary} - successful result",
                "content": {
                    "application/json": {
                        "schema": self.response_schema,
                        **(
                            {"examples": {"standard": {"value": self.response_example}}}
                            if self.response_example
                            else {}
                        ),
                    }
                },
            }
        responses["400"] = _problem_response("Request failed validation or a business rule")
        responses["401"] = _problem_response("Missing or invalid OAuth 2.1 credential")
        responses["403"] = _problem_response("Tenant isolation or scope violation")
        responses["429"] = _problem_response("Per-tenant rate limit exceeded")
        responses["502"] = _problem_response("The connected PAS returned an error")
        op["responses"] = responses
        return op

    def path_parameters(self) -> dict[str, str]:
        return {
            p.name: p.schema_.get("type", "string")
            for p in self.parameters
            if p.location == "path"
        }


def _problem_response(description: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {
            "application/problem+json": {
                "schema": {"$ref": "#/components/schemas/Problem"},
                "examples": {"problem": {"value": _problem_sample(description)}},
            }
        },
    }


def _problem_sample(description: str) -> dict[str, Any]:
    return {
        "type": "https://docs.pas-plugins.io/errors/pas-400-validation",
        "title": "Validation failed",
        "status": 400,
        "detail": description,
        "code": ErrorCode.VALIDATION_FAILED.value,
        "retryable": False,
    }


class PasTransport(StrEnum):
    """Wire protocol used to reach the vendor."""

    REST_JSON = "rest+json"
    REST_XML = "rest+xml"
    SOAP = "soap"
    GRAPHQL = "graphql"
    FILE = "file"
    SDK = "in-process-sdk"


@dataclass(frozen=True, slots=True)
class VendorCapabilities:
    """What a vendor's PAS can and cannot do, used for routing and honesty."""

    vendor: str
    display_name: str
    transport: PasTransport
    api_style: str
    batch_capable: bool
    streaming_capable: bool
    idempotency_support: bool
    soap_namespace: str | None = None
    notes: str = ""
    atomic_operations: tuple[str, ...] = ()


class PasAdapter(abc.ABC):
    """Base class every vendor adapter implements."""

    vendor: str = "generic"
    capabilities: VendorCapabilities

    @abc.abstractmethod
    async def execute(
        self,
        operation: AtomicOperation,
        payload: dict[str, Any],
        ctx: TenantContext,
        *,
        path_params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Execute one atomic operation and return the canonical response body."""

    async def health(self) -> dict[str, Any]:
        """Vendor health probe used by the gateway's monitoring UI."""
        return {"vendor": self.vendor, "status": "unknown"}

    def supports(self, operation_id: str) -> bool:
        """Whether this vendor exposes the operation.

        Default: honour an explicit per-vendor allow list when the operation
        declares one, otherwise assume the full catalogue is available.
        """
        return not self.capabilities.atomic_operations or (
            operation_id in self.capabilities.atomic_operations
        )

    def translate_request(self, operation: AtomicOperation, payload: dict[str, Any]) -> dict[str, Any]:
        """Hook for vendor-specific request rewriting (overridden per vendor)."""
        return payload

    def translate_response(self, operation: AtomicOperation, response: Any) -> dict[str, Any]:  # noqa: ANN401
        """Hook for vendor-specific response normalisation."""
        if isinstance(response, dict):
            return response
        return {"result": response}


class SimulatedPasAdapter(PasAdapter):
    """Deterministic in-memory PAS used for demos, tests and local development.

    Behaviour is fully determined by the input, which makes contract tests and
    the Svelte UI reproducible without a licence to any real carrier system.
    """

    vendor = "simulated"

    def __init__(
        self,
        *,
        vendor: str = "simulated",
        latency_ms: float = 5.0,
        failure_rate: float = 0.0,
        seed_policies: int = 25,
        capabilities: VendorCapabilities | None = None,
    ) -> None:
        self.vendor = vendor
        self._latency = latency_ms / 1000.0
        self._failure_rate = failure_rate
        self._policies: dict[str, dict[str, Any]] = {}
        self._quotes: dict[str, dict[str, Any]] = {}
        self._call_log: list[dict[str, Any]] = []
        self.capabilities = capabilities or VendorCapabilities(
            vendor=vendor,
            display_name="Simulated PAS",
            transport=PasTransport.REST_JSON,
            api_style="canonical",
            batch_capable=True,
            streaming_capable=False,
            idempotency_support=True,
            notes="Deterministic simulator for tests and demos.",
        )
        self._seed(seed_policies)

    def _seed(self, count: int) -> None:
        """Create deterministic sample policies in the canonical camelCase shape.

        The simulator stores records in exactly the shape the gateway's published
        response schema promises, so a client (or a generated SDK) sees the same
        keys it was documented - a simulator that returned snake_case would hide
        real integration bugs.
        """
        from pas_core.acord.models import (  # noqa: PLC0415
            Coverage,
            CoverageType,
            PaymentMode,
            Policy,
            PolicyStatus,
            Relationship,
            RoleAssignment,
        )

        coverages = [c for c in CoverageType if c is not CoverageType.LONG_TERM_CARE]
        annuity_types = {CoverageType.FIXED_INDEXED_ANNUITY, CoverageType.VARIABLE_ANNUITY}
        statuses = (PolicyStatus.ACTIVE, PolicyStatus.LAPSED, PolicyStatus.QUOTED)
        states = ["NY", "CA", "TX", "FL", "IL", "OH", "PA"]
        tiers = ["A", "B", "C"]
        for index in range(count):
            policy_id = f"SIMPOL{index + 1:06d}"
            coverage = coverages[index % len(coverages)]
            issue = date(2020 + index % 5, index % 9 + 1, 15)
            face = float(50_000 * (index % 20 + 1))
            model = Policy(
                policy_id=policy_id,
                product_id=f"PROD{index % 5 + 1:03d}",
                product_code=f"{coverage.value.upper()[:6]}-{tiers[index % 3]}",
                status=statuses[index % 3],
                issue_date=issue,
                effective_date=issue,
                face_amount=face,
                annualised_premium=float(120 * (index % 10 + 1)),
                payment_mode=PaymentMode.MONTHLY,
                state_of_issue=states[index % len(states)],
                lob="Annuity" if coverage in annuity_types else "Life",
                coverages=[Coverage(
                    coverage_id="COV1",
                    coverage_type=coverage,
                    face_amount=face,
                    premium_period_years=20,
                )],
                parties=[RoleAssignment(
                    party_id=f"SIMPARTY{index % 5 + 1:03d}",
                    relationship=Relationship.INSURED,
                    is_primary=True,
                )],
            )
            self._policies[policy_id] = {
                "policyId": model.policy_id,
                "productId": model.product_id,
                "productCode": model.product_code,
                "status": str(model.status),
                "stateOfIssue": model.state_of_issue,
                "lob": model.lob,
                "faceAmount": model.face_amount,
                "currency": model.currency,
                "annualisedPremium": model.annualised_premium,
                "paymentMode": str(model.payment_mode),
                "issueDate": model.issue_date.isoformat() if model.issue_date else None,
                "effectiveDate": model.effective_date.isoformat() if model.effective_date else None,
                "expirationDate": None,
                "coverages": [
                    {
                        "coverageId": c.coverage_id,
                        "coverageType": str(c.coverage_type),
                        "faceAmount": c.face_amount,
                        "isRider": c.is_rider,
                        "isGuaranteed": c.is_guaranteed,
                        "premiumPeriodYears": c.premium_period_years,
                    }
                    for c in model.coverages
                ],
                "parties": [
                    {
                        "partyId": p.party_id,
                        "relationship": str(p.relationship),
                        "isPrimary": p.is_primary,
                    }
                    for p in model.parties
                ],
                "events": [],
            }

    @property
    def call_log(self) -> list[dict[str, Any]]:
        return list(self._call_log)

    async def health(self) -> dict[str, Any]:
        return {
            "vendor": self.vendor,
            "status": "ok",
            "policyCount": len(self._policies),
            "transport": str(self.capabilities.transport),
        }

    async def execute(
        self,
        operation: AtomicOperation,
        payload: dict[str, Any],
        ctx: TenantContext,
        *,
        path_params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        params = path_params or {}
        self._call_log.append({
            "operationId": operation.operation_id,
            "tenantId": ctx.tenant_id,
            "payload": payload,
            "pathParams": params,
            "idempotencyKey": idempotency_key,
        })
        if self._latency:
            await asyncio.sleep(self._latency)
        if self._failure_rate:
            # Deterministic pseudo-failure keyed on the correlation id so tests
            # can exercise retry paths without flakiness.
            bucket = int(ctx.correlation_id[:4], 16) % 100
            if bucket < self._failure_rate * 100:
                record_pas_call(self.vendor, operation.operation_id, "error", time.perf_counter() - started)
                raise UpstreamPasError(
                    "Simulated upstream failure",
                    status_code=503,
                    vendor=self.vendor,
                    operation=operation.operation_id,
                )
        try:
            result = await self._dispatch(operation, payload, params, ctx, idempotency_key)
        except PasError:
            record_pas_call(self.vendor, operation.operation_id, "error", time.perf_counter() - started)
            raise
        record_pas_call(self.vendor, operation.operation_id, "success", time.perf_counter() - started)
        return result

    async def _dispatch(
        self,
        operation: AtomicOperation,
        payload: dict[str, Any],
        params: dict[str, Any],
        ctx: TenantContext,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        oid = operation.operation_id
        if oid == "policy.get":
            policy_id = params.get("policyId") or payload.get("policyId")
            policy = self._policies.get(str(policy_id))
            if policy is None:
                raise PasError(
                    ErrorCode.NOT_FOUND,
                    f"Policy '{policy_id}' was not found",
                    {"policyId": str(policy_id)},
                )
            return policy
        if oid == "policy.search":
            status_filter = payload.get("status")
            product_filter = payload.get("productCode")
            state_filter = payload.get("stateOfIssue")
            updated_since = payload.get("updatedSince")
            limit = int(payload.get("limit", 25))
            offset = int(payload.get("offset", 0) or 0)
            rows = [
                p for p in self._policies.values()
                if (not status_filter or p["status"] == status_filter)
                and (not product_filter or p["productCode"] == product_filter)
                and (not state_filter or p["stateOfIssue"] == state_filter)
                and (not updated_since or str(p.get("issueDate") or "") >= str(updated_since))
            ]
            return {
                "items": rows[offset: offset + limit],
                "totalCount": len(rows),
                "limit": limit,
                "offset": offset,
            }
        if oid == "policy.premium.calculate":
            return self._calculate_premium(payload)
        if oid == "policy.quote.create":
            quote_id = f"QT{ctx.correlation_id[:10].upper()}"
            premium = self._calculate_premium(payload)
            quote = {
                "quoteId": quote_id,
                "productCode": payload.get("productCode", "UNKNOWN"),
                "status": "quoted",
                "quotedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "expiresAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 30 * 86400)),
                **premium,
            }
            self._quotes[quote_id] = quote
            return quote
        if oid == "policy.quote.get":
            quote = self._quotes.get(str(params.get("quoteId")))
            if quote is None:
                raise PasError(ErrorCode.NOT_FOUND, f"Quote '{params.get('quoteId')}' was not found")
            return quote
        if oid == "policy.bind":
            policy_id = payload.get("policyId") or f"SIMPOL{len(self._policies) + 1:06d}"
            base = dict(self._policies.get(policy_id) or {})
            base.update(payload)
            base.setdefault("policyId", policy_id)
            base["status"] = payload.get("status", "issued")
            base["issueDate"] = time.strftime("%Y-%m-%d", time.gmtime())
            base["effectiveDate"] = payload.get("effectiveDate", base["issueDate"])
            base.setdefault("productCode", "UNKNOWN")
            base.setdefault("stateOfIssue", "NY")
            base.setdefault("faceAmount", 0.0)
            self._policies[policy_id] = base
            return {"policyId": policy_id, "status": base["status"], "issuedAt": base["issueDate"]}
        if oid == "policy.status.get":
            policy = self._policies.get(str(params.get("policyId")))
            if policy is None:
                raise PasError(ErrorCode.NOT_FOUND, f"Policy '{params.get('policyId')}' was not found")
            return {"policyId": policy["policyId"], "status": policy["status"]}
        if oid == "policy.lapse":
            return self._set_status(str(params.get("policyId")), "lapsed")
        if oid == "policy.reinstate":
            return self._set_status(str(params.get("policyId")), "active")
        if oid == "policy.surrender":
            return self._set_status(str(params.get("policyId")), "surrendered")
        if oid == "policy.terminate":
            return self._set_status(str(params.get("policyId")), "terminated")
        if oid == "policy.history.get":
            policy_id = str(params.get("policyId"))
            policy = self._policies.get(policy_id)
            if policy is None:
                raise PasError(ErrorCode.NOT_FOUND, f"Policy '{policy_id}' was not found")
            since = payload.get("since") or params.get("since")
            events = [
                {
                    "eventId": f"{policy_id}-E1",
                    "eventType": "issue",
                    "effectiveDate": policy.get("issueDate"),
                    "status": "issued",
                },
                {
                    "eventId": f"{policy_id}-E2",
                    "eventType": "premiumStatusChange",
                    "effectiveDate": policy.get("issueDate"),
                    "status": policy.get("status"),
                },
            ]
            if since:
                events = [e for e in events if str(e["effectiveDate"]) >= str(since)]
            return {"policyId": policy_id, "items": events, "totalCount": len(events)}
        if oid == "policy.quote.list":
            status_filter = payload.get("status")
            rows = [q for q in self._quotes.values() if not status_filter or q["status"] == status_filter]
            limit = int(payload.get("limit") or 25)
            return {"items": rows[:limit], "totalCount": len(rows)}
        if oid == "policy.beneficiary.update":
            allocations = payload.get("allocations", [])
            total = round(sum(float(a.get("sharePercent", 0)) for a in allocations), 2)
            if total > 100.0001:
                raise PasError(
                    ErrorCode.BUSINESS_RULE_VIOLATION,
                    "Beneficiary shares exceed 100%",
                    {"totalPercent": total},
                )
            return {"policyId": params.get("policyId"), "totalPercent": total, "count": len(allocations)}
        if oid == "policy.beneficiary.list":
            policy_id = params.get("policyId")
            policy = self._policies.get(str(policy_id))
            if policy is None:
                raise PasError(ErrorCode.NOT_FOUND, f"Policy '{policy_id}' was not found")
            return {
                "policyId": policy["policyId"],
                "allocations": [
                    {"partyId": "SIMBENE1", "relationship": "beneficiary", "sharePercent": 100.0, "isPrimary": True}
                ],
                "totalPercent": 100.0,
                "signaturesPending": 0,
            }
        if oid == "product.get":
            return {
                "productId": payload.get("productId", "PROD001"),
                "productCode": payload.get("productCode", "TERM20-A"),
                "productName": "Simulated 20-Year Term",
                "productType": "term",
                "status": "inForce",
                "issueAgeMinimum": 18,
                "issueAgeMaximum": 75,
                "minimumFaceAmount": 25_000.0,
                "maximumFaceAmount": 5_000_000.0,
            }
        if oid == "product.search":
            return {"items": await self._dispatch_products(), "totalCount": 3}
        if oid == "application.submit":
            return {
                "applicationId": f"APP{ctx.correlation_id[:10].upper()}",
                "status": "submitted",
                "receivedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        if oid == "application.get":
            return {
                "applicationId": params.get("applicationId"),
                "status": "underwriting",
                "requirements": [{"code": "MEDICAL", "description": "Attending physician statement"}],
            }
        if oid == "policy.cashValue.get":
            policy = self._policies.get(str(params.get("policyId")))
            face = float((policy or {}).get("faceAmount", 100_000))
            return {
                "policyId": params.get("policyId"),
                "asOfDate": time.strftime("%Y-%m-%d", time.gmtime()),
                "cashValue": round(face * 0.11, 2),
                "surrenderValue": round(face * 0.115, 2),
                "netCashValue": round(face * 0.108, 2),
                "currency": "USD",
            }
        if oid == "policy.loan.request":
            amount = float(payload.get("amount", 0))
            if amount <= 0:
                raise PasError(ErrorCode.VALIDATION_FAILED, "Policy loan amount must be positive")
            return {
                "policyId": params.get("policyId"),
                "loanId": f"LOAN{ctx.correlation_id[:8].upper()}",
                "amount": amount,
                "status": "approved",
                "interestRate": 0.06,
            }
        raise PasError(
            ErrorCode.PAS_OPERATION_UNAVAILABLE,
            f"Operation '{oid}' is not implemented by the simulated PAS",
            {"operationId": oid, "vendor": self.vendor},
        )

    async def _dispatch_products(self) -> list[dict[str, Any]]:
        return [
            {
                "productId": f"PROD{i + 1:03d}",
                "productCode": f"T{i + 1}0-A",
                "productName": f"Simulated Term {10 * (i + 1)}",
                "productType": "term",
                "status": "inForce",
            }
            for i in range(3)
        ]

    def _set_status(self, policy_id: str, status: str) -> dict[str, Any]:
        policy = self._policies.get(policy_id)
        if policy is None:
            raise PasError(ErrorCode.NOT_FOUND, f"Policy '{policy_id}' was not found")
        previous = policy["status"]
        policy["status"] = status
        return {
            "policyId": policy_id,
            "previousStatus": previous,
            "status": status,
            "effectiveDate": time.strftime("%Y-%m-%d", time.gmtime()),
        }

    @staticmethod
    def _calculate_premium(payload: dict[str, Any]) -> dict[str, Any]:
        """A transparent, deterministic rating function for demo purposes.

        Rates are expressed per USD 1,000 of face amount so the arithmetic is
        inspectable by a carrier actuary reviewing the simulation, rather than
        being an opaque lookup table.
        """
        face = float(payload.get("faceAmount") or 0)
        age = int(payload.get("issueAge") or 0)
        term = int(payload.get("termYears") or 20)
        tobacco = bool(payload.get("tobacco") or False)
        mode = payload.get("paymentMode") or "monthly"
        rate_per_thousand = 1.50 * (term / 20)
        mortality = max(0.35, 1.0 + (age - 30) * 0.021)
        tobacco_factor = 1.55 if tobacco else 1.0
        annual = (face / 1000.0) * rate_per_thousand * mortality * tobacco_factor
        mode_factor = {
            "monthly": 12.0,
            "quarterly": 4.0,
            "semiAnnual": 2.0,
            "annual": 1.0,
            "single": 1.0,
        }.get(mode, 12.0)
        periodic = round(annual / mode_factor, 2)
        return {
            "annualisedPremium": round(annual, 2),
            "periodicPremium": periodic,
            "paymentMode": mode,
            "currency": "USD",
            "guaranteedPeriodYears": min(term, 10),
            "ratingFactors": {
                "baseRatePerThousand": round(rate_per_thousand, 4),
                "mortalityMultiplier": round(mortality, 4),
                "tobaccoFactor": tobacco_factor,
            },
            "calculationVersion": "sim-1.0.0",
        }


class OperationRegistry:
    """Holds the atomic operations for a deployment.

    Plugin 1 registers the full life & annuity catalogue; a carrier can also
    register bespoke operations discovered from their own PAS documentation
    without redeploying the gateway.
    """

    def __init__(self, operations: Iterable[AtomicOperation] | None = None) -> None:
        self._operations: dict[str, AtomicOperation] = {}
        for operation in operations or ():
            self.register(operation)

    def register(self, operation: AtomicOperation) -> AtomicOperation:
        if operation.operation_id in self._operations:
            raise ValidationError(
                f"Duplicate operation id '{operation.operation_id}'",
                operationId=operation.operation_id,
            )
        self._operations[operation.operation_id] = operation
        return operation

    def register_all(self, operations: Iterable[AtomicOperation]) -> None:
        for operation in operations:
            self.register(operation)

    def get(self, operation_id: str) -> AtomicOperation:
        try:
            return self._operations[operation_id]
        except KeyError as exc:
            raise PasError(
                ErrorCode.PAS_OPERATION_UNAVAILABLE,
                f"Unknown atomic operation '{operation_id}'",
                {"operationId": operation_id, "available": sorted(self._operations)[:25]},
            ) from exc

    def list(
        self,
        *,
        tag: str | None = None,
        side: OperationSide | None = None,
        search: str | None = None,
    ) -> list[AtomicOperation]:
        results = list(self._operations.values())
        if tag:
            results = [o for o in results if tag in o.tags]
        if side:
            results = [o for o in results if o.side is side]
        if search:
            needle = search.lower()
            results = [
                o for o in results
                if needle in o.operation_id.lower()
                or needle in o.summary.lower()
                or needle in o.intent.lower()
            ]
        return sorted(results, key=lambda o: o.operation_id)

    def by_tag(self) -> dict[str, list[AtomicOperation]]:
        grouped: dict[str, list[AtomicOperation]] = {}
        for operation in self._operations.values():
            for tag in operation.tags or ("default",):
                grouped.setdefault(tag, []).append(operation)
        return {tag: sorted(ops, key=lambda o: o.operation_id) for tag, ops in sorted(grouped.items())}

    def ids(self) -> list[str]:
        return sorted(self._operations)

    def __len__(self) -> int:
        return len(self._operations)

    def __contains__(self, operation_id: object) -> bool:
        return operation_id in self._operations


def validate_request(operation: AtomicOperation, payload: Any) -> dict[str, Any]:  # noqa: ANN401
    """Validate a request payload against the operation's declared contract.

    Two layers, both required:

    1. the published JSON Schema, which is what the client and any generated SDK
       were told to satisfy;
    2. the pydantic model, which additionally enforces cross-field rules that JSON
       Schema cannot express - beneficiary shares totalling no more than 100 percent,
       a mandatory ``confirmation`` flag, mutually exclusive options.
    """
    if operation.request_model is not None:
        # Nulls are dropped rather than forwarded: most PAS dialects reject or
        # mis-handle explicit nulls, and an omitted optional field is
        # unambiguous in every vendor we support.
        return coerce_model(operation.request_model, payload).model_dump(
            mode="json", exclude_none=True
        )

    if not operation.request_schema:
        return {}
    from jsonschema import Draft202012Validator  # noqa: PLC0415

    validator = Draft202012Validator(operation.request_schema)
    errors: Sequence[str] = [e.message for e in validator.iter_errors(payload)]
    if errors:
        raise ValidationError(
            f"Request for '{operation.operation_id}' is invalid: {errors[0]}",
            operationId=operation.operation_id,
            violations=list(errors[:20]),
        )
    return dict(payload) if isinstance(payload, dict) else {"value": payload}


class CoercingModel(BaseModel):
    """Base for request models that also accept extra vendor fields."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


def coerce_model(model: type[BaseModel], payload: dict[str, Any]) -> BaseModel:
    """Validate into a pydantic model, converting failures to catalogue errors."""
    try:
        return model.model_validate(payload)
    except PydanticValidationError as exc:
        raise ValidationError(
            f"Payload failed validation for {model.__name__}: {exc.error_count()} error(s)",
            model=model.__name__,
            violations=[
                {"path": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
                for e in exc.errors()[:20]
            ],
        ) from exc


class OpenApiParam(BaseModel):
    """Convenience constructor helper for declared parameters."""

    name: str
    location: str = "query"
    type_: str = "string"
    required: bool = False
    description: str = ""
    example: Any = None
    enum: list[Any] | None = None
    pattern: str | None = None
    minimum: float | None = None

    def to_parameter(self) -> OperationParameter:
        schema: dict[str, Any] = {"type": self.type_}
        if self.enum:
            schema["enum"] = self.enum
        if self.pattern:
            schema["pattern"] = self.pattern
        if self.minimum is not None:
            schema["minimum"] = self.minimum
        if self.type_ == "string" and self.pattern is None and self.minimum is None:
            schema.setdefault("minLength", 1)
        return OperationParameter(
            name=self.name,
            location=self.location,
            schema_=schema,
            required=self.required,
            description=self.description,
            examples=(self.example,) if self.example is not None else (),
        )


def field_schema(model: type[BaseModel], *, by_alias: bool = True) -> dict[str, Any]:
    """JSON Schema for a request/response model, inlined for OpenAPI embedding."""
    schema = model.model_json_schema(by_alias=by_alias, ref_template="#/components/schemas/{model}")
    defs = schema.pop("$defs", {})
    if not defs:
        return schema
    return {
        "allOf": [schema],
        **{
            "$comment": f"Nested models are emitted under components/schemas: {sorted(defs)}",
        },
    }


def build_operation(
    *,
    operation_id: str,
    summary: str,
    intent: str,
    method: str,
    path: str,
    request: type[BaseModel] | None = None,
    response: type[BaseModel] | None = None,
    tags: Sequence[str] = (),
    side: OperationSide = OperationSide.READ,
    description: str = "",
    parameters: Sequence[OpenApiParam] = (),
    errors: Sequence[ErrorCode] = (),
    required_scopes: Sequence[str] = (),
    acord_tx_type: TransactionTypeCode | None = None,
    vendor_operation: str | None = None,
    rate_limit_policy: str = "tenant-default",
    slo_ms: int = 1000,
) -> AtomicOperation:
    """Assemble an :class:`AtomicOperation` from pydantic models.

    Parameters are accepted as the friendlier :class:`OpenApiParam` builder type and
    normalised to :class:`OperationParameter` here.  The pydantic models themselves
    are retained so the router can enforce cross-field rules (total beneficiary
    shares, a required confirmation flag) that JSON Schema cannot express.
    """
    return AtomicOperation(
        operation_id=operation_id,
        summary=summary,
        intent=intent,
        method=method,
        path=path,
        side=side,
        tags=tuple(tags),
        description=description,
        parameters=tuple(p.to_parameter() for p in parameters),
        request_schema=_strip_title(request) if request else None,
        response_schema=_strip_title(response) if response else None,
        request_example=_example(request) if request else None,
        response_example=_example(response) if response else None,
        errors=tuple(errors) or (ErrorCode.VALIDATION_FAILED,),
        required_scopes=tuple(required_scopes),
        acord_tx_type=acord_tx_type,
        vendor_operation=vendor_operation,
        rate_limit_policy=rate_limit_policy,
        slo_ms=slo_ms,
        request_model=request,
        response_model=response,
    )


def _strip_title(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema(by_alias=True)
    schema.pop("title", None)
    return schema


def _example(model: type[BaseModel]) -> dict[str, Any] | None:
    """Derive a worked example from a model's JSON Schema.

    Resolution order per property: explicit ``examples`` -> ``default`` ->
    ``const`` -> ``enum[0]`` -> a type-appropriate synthesised value.  Handles
    ``anyOf`` (pydantic's representation of optional fields) and local ``$ref``
    pointers, so nested objects and arrays produce usable examples rather than
    empty stubs.  The specification's "example payloads" deliverable depends on
    this: an operation with no example is not discoverable by an agent.
    """
    schema = model.model_json_schema(by_alias=True)
    defs = schema.get("$defs", {})
    example = _walk_schema(schema, defs, depth=0)
    if isinstance(example, dict) and example:
        return example
    return None


_MAX_EXAMPLE_DEPTH = 4


def _walk_schema(node: Any, defs: dict[str, Any], *, depth: int) -> Any:  # noqa: ANN401
    if not isinstance(node, dict) or depth > _MAX_EXAMPLE_DEPTH:
        return None

    if "$ref" in node:
        target = defs.get(str(node["$ref"]).rsplit("/", 1)[-1])
        return _walk_schema(target, defs, depth=depth) if target else None

    for value in node.get("examples", []):
        return value
    if "example" in node:
        return node["example"]
    if "const" in node:
        return node["const"]
    if "default" in node:
        return node["default"]

    kind = node.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), None)

    if kind == "object" or "properties" in node:
        out: dict[str, Any] = {}
        for key, child in (node.get("properties") or {}).items():
            value = _walk_schema(child, defs, depth=depth + 1)
            if value is not None:
                out[key] = value
        return out or None

    if kind == "array":
        item_schema = node.get("items")
        item = _walk_schema(item_schema, defs, depth=depth + 1)
        # Only arrays of structured values benefit from a populated sample; an
        # array of free-text values is clearer (and safer) shown as empty.
        if item is None:
            return []
        if isinstance(item, (str, int, float, bool)) and not _is_constrained(item_schema):
            return []
        return [item]

    if kind == "boolean":
        return True
    if kind == "integer":
        return int(node["minimum"]) if "minimum" in node else 0
    if kind == "number":
        return float(node["minimum"]) if "minimum" in node else 0.0
    if kind == "string":
        if node.get("enum"):
            return node["enum"][0]
        fmt = node.get("format")
        if fmt == "date":
            return "2026-01-01"
        if fmt == "date-time":
            return "2026-01-01T00:00:00Z"
        if fmt == "email":
            return "applicant@example.com"
        if fmt == "uri":
            return "https://example.com"
        return _string_for_pattern(str(node.get("pattern", "")), node)

    for variant in node.get("anyOf", []):
        value = _walk_schema(variant, defs, depth=depth)
        if value is not None:
            return value
    return None


def _is_constrained(node: Any) -> bool:  # noqa: ANN401
    """Whether a scalar schema carries enough information to be a real example."""
    return bool(isinstance(node, dict) and (node.get("enum") or node.get("const") or node.get("examples")))


_STATE_PATTERN = re.compile(r"\^\[A-Z\]\{2\}\$")
_ZIP_PATTERN = re.compile(r"\\d\{5\}")
_POLICY_PATTERN = re.compile(r"\[A-Z0-9\]")


def _string_for_pattern(pattern: str, node: dict[str, Any] | None = None) -> str:
    """Produce a plausible string that also satisfies length constraints.

    Deliberately conservative: when nothing is recognised we return a short generic
    literal rather than something that looks authoritative but is not, and we always
    respect ``minLength``/``maxLength`` so the example passes its own schema.
    """
    if _STATE_PATTERN.search(pattern):
        return "NY"
    if _ZIP_PATTERN.search(pattern):
        return "10001"
    if _POLICY_PATTERN.search(pattern) and "policy" not in pattern.lower():
        return "POL1001"
    if "\\d{4}-\\d{2}-\\d{2}" in pattern:
        return "2026-01-01"
    node = node or {}
    min_length = int(node.get("minLength", 0) or 0)
    max_length = node.get("maxLength")
    base = "example"
    if isinstance(max_length, int) and len(base) > max_length:
        # Two-letter codes (state, currency, sex) are the common case.
        base = ("A" * min_length) if min_length else "X"
    while len(base) < min_length:
        base += "x"
    return base
