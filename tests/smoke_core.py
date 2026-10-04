"""Smoke checks for the shared core library.

Run with:  .venv/Scripts/python -m tests.smoke_core
These are quick developer assertions, not the test suite (see ``tests/``).
"""

from __future__ import annotations

import sys

from pas_core.acord import mapping, models, transaction
from pas_core.acord.schema import bundled_schema, validate_payload
from pas_core.pas.base import AtomicOperation, SimulatedPasAdapter, OperationRegistry
from pas_core.pas.translation import get_engine
from pas_core.tenancy import InMemoryTenantRegistry, RequestPrincipal, build_context


def check_acord() -> None:
    codes = transaction.registry_as_json()["transactions"]
    print(f"ACORD registry entries : {len(codes)}")
    policy = models.Policy(
        policy_id="POL1001",
        product_id="PROD001",
        product_code="TERM20-A",
        status=models.PolicyStatus.ACTIVE,
        issue_date=models.date(2026, 1, 1),
        effective_date=models.date(2026, 2, 1),
        face_amount=250_000,
        annualised_premium=1_920.55,
        state_of_issue="NY",
        coverages=[
            models.Coverage(coverage_id="COV1", coverage_type=models.CoverageType.TERM, face_amount=250_000)
        ],
    )
    ctx = transaction.TransactionContext(
        sender_id="PASPLUGINS", receiver_id="LIFEPLUS", correlation_id="abc123"
    )
    envelope = mapping.to_acord_envelope(policy, ctx)
    print(f"envelope tx type       : {envelope.transaction_type_code}")
    print(f"envelope content keys  : {sorted(envelope.content)[:6]}")
    round_trip = mapping.parse_policy_content(envelope.content)
    assert round_trip.policy_id == policy.policy_id
    validate_payload("TransactionEnvelope", envelope.to_dict())
    print(f"bundled schema defs    : {len(bundled_schema()['$defs'])}")


def check_translation() -> None:
    engine = get_engine()
    print(f"translation profiles   : {engine.vendors()}")
    result = engine.from_vendor(
        "majesco-lifeplus",
        "policy.get",
        {
            "policyNumber": "LP123",
            "policyStatus": "InForce",
            "issueDate": "01/15/2021",
            "faceAmount": "$250,000.00",
        },
    )
    print(f"majesco from_vendor     : {result.payload}")
    outbound = engine.to_vendor("oracle-oipa", "policy.get", {"policyId": "OA9", "asOfDate": "2026-03-31"})
    print(f"oipa to_vendor          : {outbound.payload}")


def check_simulated_pas() -> None:
    import asyncio

    from pas_core.tenancy import Tenant

    registry = InMemoryTenantRegistry()
    tenant = registry.register(
        Tenant(
            tenant_id="demo-carrier",
            legal_name="Demo Mutual Life",
            pas_vendor="simulated",
        )
    )
    principal = RequestPrincipal(subject="dev", tenant_id="demo-carrier", scopes=frozenset({"*"}))
    ctx = build_context(tenant, principal)

    op = AtomicOperation(
        operation_id="policy.quote.calculate",
        summary="Calculate a premium",
        intent="price a policy",
        method="POST",
        path="/insurance/v1/policies/premium/calculate",
    )
    adapter = SimulatedPasAdapter()
    result = asyncio.run(adapter.execute(op, {"faceAmount": 250_000, "issueAge": 45}, ctx))
    print(f"simulated quote        : {result['annualisedPremium']} / {result['periodicPremium']}")
    ops = OperationRegistry([op])
    print(f"operation registry     : {ops.ids()}")


def main() -> int:
    check_acord()
    check_translation()
    check_simulated_pas()
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
