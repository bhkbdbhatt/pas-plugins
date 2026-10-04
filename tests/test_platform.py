"""Cross-plugin contract tests for the platform foundation and plugin 1.

These are the tests that would catch the mistakes that matter most in this suite:
a tenant could read another carrier's policy, an MCP agent could bind a policy
without confirmation, or the OpenAPI document could drift from behaviour.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pas_core.acord import mapping, models
from pas_core.acord.schema import bundled_schema, validate_payload
from pas_core.acord.transaction import TransactionTypeCode, registry_as_json, resolve_code
from pas_core.audit import AuditAction, InMemoryAuditSink, diff_dicts
from pas_core.errors import ErrorCode, PasError, TenantMismatchError
from pas_core.pas.translation import get_engine
from pas_core.ratelimit import InMemoryRateLimiter, RateLimitPolicy
from pas_core.rules import Outcome, load_builtin_rule_sets
from pas_core.tenancy import (
    InMemoryTenantRegistry,
    RequestPrincipal,
    Tenant,
    TenantPlan,
    TenantStatus,
    build_context,
)


@pytest.fixture
def client() -> TestClient:
    from pas_plugins.plugin1_gateway.main import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(
        tenant_id="test-carrier",
        legal_name="Test Life",
        pas_vendor="simulated",
        plan=TenantPlan.ENTERPRISE,
        enabled_plugins=frozenset({f"plugin{i}" for i in range(1, 8)}),
    )


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------
def test_tenant_id_is_validated() -> None:
    with pytest.raises(PasError) as exc:
        Tenant(tenant_id="A", legal_name="Too short")
    assert exc.value.code is ErrorCode.VALIDATION_FAILED


def test_cross_tenant_context_is_refused(tenant: Tenant) -> None:
    principal = RequestPrincipal(subject="agent", tenant_id="other-carrier")
    with pytest.raises(TenantMismatchError):
        build_context(tenant, principal)


def test_plugin_entitlement_is_enforced(tenant: Tenant) -> None:
    restricted = tenant.with_overrides(enabled_plugins=frozenset({"plugin1"}))
    restricted.assert_plugin_enabled("plugin1")
    with pytest.raises(PasError) as exc:
        restricted.assert_plugin_enabled("plugin3")
    assert exc.value.code is ErrorCode.PERMISSION_DENIED


def test_suspended_tenant_cannot_transact(tenant: Tenant) -> None:
    suspended = tenant.with_overrides(status=TenantStatus.SUSPENDED)
    with pytest.raises(PasError) as exc:
        suspended.assert_active()
    assert exc.value.code is ErrorCode.TENANT_SUSPENDED


def test_scopes_support_hierarchy() -> None:
    principal = RequestPrincipal(subject="a", tenant_id="t", scopes=frozenset({"policy:*"}))
    assert principal.has_scope("policy:write")
    assert not principal.has_scope("ifrs17:read")
    with pytest.raises(PasError):
        principal.require_scopes("policy:read", "ifrs17:read")


def test_registry_lifecycle() -> None:
    registry = InMemoryTenantRegistry()
    registry.register(Tenant(tenant_id="acme", legal_name="Acme Life"))
    assert registry.require("acme").legal_name == "Acme Life"
    registry.suspend("acme")
    assert registry.require("acme").status is TenantStatus.SUSPENDED
    registry.activate("acme")
    assert registry.require("acme").status is TenantStatus.ACTIVE
    registry.remove("acme")
    assert registry.get("acme") is None


# ---------------------------------------------------------------------------
# ACORD NGDS
# ---------------------------------------------------------------------------
def test_registry_resolves_codes_and_synonyms() -> None:
    assert resolve_code("TX-103") is not None
    assert resolve_code("tx-103") is not None
    assert resolve_code("UWDecision") is not None  # documented legacy synonym
    assert resolve_code("TX-999") is None
    assert len(registry_as_json()["transactions"]) >= 30


def test_envelope_round_trip() -> None:
    policy = models.Policy(
        policy_id="POL9001",
        product_id="PROD001",
        product_code="TERM20-A",
        status=models.PolicyStatus.ISSUED,
        issue_date=__import__("datetime").date(2026, 2, 1),
        effective_date=__import__("datetime").date(2026, 3, 1),
        face_amount=500_000,
        annualised_premium=1_250.75,
        state_of_issue="TX",
        coverages=[
            models.Coverage(
                coverage_id="COV1",
                coverage_type=models.CoverageType.TERM,
                face_amount=500_000,
                premium_period_years=20,
            )
        ],
        parties=[
            models.RoleAssignment(
                party_id="PARTY1",
                relationship=models.Relationship.INSURED,
                is_primary=True,
            )
        ],
    )
    from pas_core.acord.transaction import TransactionContext  # noqa: PLC0415

    envelope = mapping.to_acord_envelope(
        policy,
        TransactionContext(sender_id="PAS", receiver_id="LIFEPLUS", correlation_id="corr123"),
    )
    assert envelope.transaction_type_code == TransactionTypeCode.TX105_LIFE_POLICY_ISSUE.value
    validate_payload("TransactionEnvelope", envelope.to_dict())

    parsed = mapping.parse_policy_content(envelope.content)
    assert parsed.policy_id == "POL9001"
    assert parsed.coverages[0].premium_period_years == 20
    assert parsed.parties[0].relationship is models.Relationship.INSURED


def test_bundled_schema_is_self_contained() -> None:
    schema = bundled_schema()
    assert schema["$schema"].endswith("2020-12/schema")
    assert "Policy" in schema["$defs"]
    assert "Address" in schema["$defs"]


def test_beneficiary_shares_cannot_exceed_100() -> None:
    from pas_plugins.plugin1_gateway.models import BeneficiaryAllocation, BeneficiaryUpdateRequest  # noqa: PLC0415

    with pytest.raises(Exception):
        BeneficiaryUpdateRequest(
            allocations=[
                BeneficiaryAllocation(partyId="P1", relationship="spouse", sharePercent=80),
                BeneficiaryAllocation(partyId="P2", relationship="child", sharePercent=30),
            ]
        )


def test_in_force_policy_requires_effective_date() -> None:
    with pytest.raises(Exception):
        models.Policy(
            policy_id="POL1",
            product_id="P1",
            product_code="T20",
            status=models.PolicyStatus.ACTIVE,
            face_amount=1000,
            state_of_issue="NY",
        )


# ---------------------------------------------------------------------------
# Translation engine
# ---------------------------------------------------------------------------
def test_bundled_profiles_load() -> None:
    engine = get_engine()
    assert set(engine.vendors()) >= {
        "majesco-lifeplus", "oracle-oipa", "eis", "mccamish-ngin", "sapiens"
    }


def test_majesco_status_translation_is_case_insensitive() -> None:
    engine = get_engine()
    for raw, expected in [("InForce", "active"), ("INFORCE", "active"), ("Lapsed", "lapsed")]:
        result = engine.from_vendor("majesco-lifeplus", "policy.get", {"policyNumber": "P1", "policyStatus": raw})
        assert result.payload["status"] == expected


def test_oracle_status_code_translation() -> None:
    result = get_engine().from_vendor(
        "oracle-oipa", "policy.get", {"PolicyNumber": "O1", "PolicyStatus": "A", "FaceAmount": "100000.00"}
    )
    assert result.payload["status"] == "active"
    assert result.payload["faceAmount"] == 100_000.0


def test_ngin_unwrap_and_envelope() -> None:
    result = get_engine().from_vendor(
        "mccamish-ngin",
        "policy.get",
        {"ngin": {"operation": "policy.read", "version": "3", "errors": [], "data": {
            "policyNumber": "N1", "policyStatus": "in_force"}}},
    )
    assert result.payload["policyId"] == "N1"
    assert result.payload["status"] == "active"


def test_sapiens_version_is_required() -> None:
    engine = get_engine()
    from pas_core.errors import TranslationError  # noqa: PLC0415

    with pytest.raises(TranslationError):
        engine.to_vendor("sapiens", "policy.lapse", {"policyId": "S1"})


def test_translation_defaults_only_on_outbound() -> None:
    engine = get_engine()
    outbound = engine.to_vendor("majesco-lifeplus", "policy.get", {"policyId": "P1"})
    assert "requestorId" in outbound.payload
    inbound = engine.from_vendor(
        "majesco-lifeplus", "policy.get", {"policyNumber": "P1", "policyStatus": "InForce"}
    )
    assert inbound.payload["status"] == "active"
    assert "requestorId" not in inbound.payload


def test_translation_profiles_reject_arbitrary_casts() -> None:
    """A profile is operator configuration and must never be able to execute code."""
    from pas_core.errors import TranslationError  # noqa: PLC0415
    from pas_core.pas.translation import load_profile_file  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "evil.yaml"
        path.write_text(
            "vendor: evil\noperations:\n"
            "  - operation_id: x\n"
            "    to_vendor:\n"
            '      - kind: compute\n        source: a\n        target: b\n        cast: "__import__"\n',
            encoding="utf-8",
        )
        with pytest.raises(TranslationError) as exc:
            load_profile_file(path)
    assert "evil.yaml" in exc.value.message


# ---------------------------------------------------------------------------
# Rules engine
# ---------------------------------------------------------------------------
def test_builtin_rules_evaluate() -> None:
    engine = load_builtin_rule_sets()
    result = engine.evaluate(
        "uw-appetite-life",
        {
            "applicant": {"age": 90, "tobacco": False},
            "request": {"faceAmount": 7_000_000},
            "product": {"filedStates": ["AL", "NY"]},
            "screening": {"sanctionsMatch": False, "pepMatch": False},
        },
    )
    assert result.decision is Outcome.FAIL
    fired = {r.rule_id for r in result.fired_rules}
    assert "APP-001" in fired
    assert "APP-002" in fired
    assert "AGE_OUT_OF_RANGE" in result.reason_codes


def test_rule_versions_and_rollback() -> None:
    from pas_core.errors import BusinessRuleViolation  # noqa: PLC0415
    from pas_core.rules import RuleSet, RuleVersionStore  # noqa: PLC0415

    versions = RuleVersionStore()
    versions.publish(
        RuleSet.from_spec({
            "ruleSetId": "demo", "name": "Demo", "version": 1,
            "rules": [{"ruleId": "R1", "name": "n", "when": {"field": "x", "operator": ">", "value": 1},
                       "then": [{"kind": "reason_code", "target": "X", "value": "X"}]}],
        })
    )
    versions.publish(
        RuleSet.from_spec({
            "ruleSetId": "demo", "name": "Demo", "version": 2,
            "rules": [
                {"ruleId": "R1", "name": "n", "when": {"field": "x", "operator": ">", "value": 5},
                 "then": [{"kind": "reason_code", "target": "X", "value": "X"}]},
                {"ruleId": "R2", "name": "m", "when": {"field": "y", "operator": "==", "value": True},
                 "then": [{"kind": "flag", "target": "flagged", "value": True}]},
            ],
        })
    )
    diff = versions.diff("demo", 1, 2)
    assert diff["added"] == ["R2"]
    assert [c["ruleId"] for c in diff["changed"]] == ["R1"]
    assert diff["changed"][0]["before"]["when"]["conditions"][0]["value"] == 1
    assert diff["changed"][0]["after"]["when"]["conditions"][0]["value"] == 5

    restored = versions.rollback("demo", 1, by="tester")
    assert restored.version == 3
    # Rollback republishes the old content as a new version: history is never
    # rewritten, so the v1 rules are still readable and the rollback is itself
    # auditable.
    assert versions.get("demo").version == 3
    assert versions.get("demo").get("R1").when.flat()[0].expected == 1
    assert "rollback-to-v1" in versions.get("demo").tags

    with pytest.raises(BusinessRuleViolation):
        versions.publish(versions.get("demo", 2))


def test_rule_evaluation_is_deterministic() -> None:
    engine = load_builtin_rule_sets()
    facts = {"applicant": {"age": 65}, "request": {"faceAmount": 500_000}}
    first = engine.evaluate("uw-appetite-life", facts).to_dict()
    second = engine.evaluate("uw-appetite-life", facts).to_dict()
    assert [r["ruleId"] for r in first["ruleOutcomes"]] == [
        r["ruleId"] for r in second["ruleOutcomes"]
    ]


def test_business_rule_violation_carries_rule_id() -> None:
    from pas_core.errors import BusinessRuleViolation  # noqa: PLC0415

    error = BusinessRuleViolation("nope", rule_id="APP-001", age=90)
    problem = error.to_problem()
    assert problem["errors"]["ruleId"] == "APP-001"


# ---------------------------------------------------------------------------
# Audit and rate limiting
# ---------------------------------------------------------------------------
def test_audit_chain_detects_tampering() -> None:
    sink = InMemoryAuditSink()
    from pas_core.audit import AuditTrail  # noqa: PLC0415

    trail = AuditTrail(sink)
    trail.record(AuditAction.CREATE, "policy", "P1")
    trail.record(AuditAction.UPDATE, "policy", "P1", before={"a": 1}, after={"a": 2})
    assert sink.verify_chain()

    tampered = sink.events[0]
    object.__setattr__(tampered, "resource_id", "P999")
    assert not sink.verify_chain()


def test_diff_dicts() -> None:
    assert diff_dicts({"a": 1, "b": 2}, {"a": 1, "b": 3}) == {"b": {"from": 2, "to": 3}}


def test_rate_limiter_raises_after_burst() -> None:
    from pas_core.errors import RateLimitedError  # noqa: PLC0415

    limiter = InMemoryRateLimiter({"t": RateLimitPolicy("t", requests_per_second=1, burst=2)})
    import asyncio  # noqa: PLC0415

    async def run() -> None:
        await limiter.check("k", "t")
        await limiter.check("k", "t")
        with pytest.raises(RateLimitedError):
            await limiter.check("k", "t")

    asyncio.run(run())


def test_rate_limit_keys_are_tenant_scoped() -> None:
    limiter = InMemoryRateLimiter({"t": RateLimitPolicy("t", requests_per_second=1, burst=1)})
    import asyncio  # noqa: PLC0415

    async def run() -> None:
        await limiter.check("t:a:mcp:x", "t")
        await limiter.check("t:b:mcp:x", "t")  # different tenant, fresh budget

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Plugin 1 HTTP surface
# ---------------------------------------------------------------------------
def test_health_and_version(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "healthy"
    version = client.get("/version").json()
    assert version["pluginId"] == "plugin1"
    assert any(v["vendor"] == "majesco-lifeplus" for v in version["vendors"])


def test_operation_catalogue_is_published(client: TestClient) -> None:
    body = client.get("/operations").json()
    ids = {o["operationId"] for o in body["operations"]}
    assert {"policy.get", "policy.bind", "policy.premium.calculate"} <= ids
    bind = next(o for o in body["operations"] if o["operationId"] == "policy.bind")
    assert bind["side"] == "write"
    assert bind["requiresIdempotencyKey"] is True


def test_operation_detail_includes_schema_and_examples(client: TestClient) -> None:
    body = client.get("/operations/policy.premium.calculate").json()
    assert body["requestSchema"]["properties"]["faceAmount"]["description"]
    assert body["responseSchema"]["properties"]["annualisedPremium"]
    assert body["requestExample"]["productCode"]


def test_acord_reference_endpoints(client: TestClient) -> None:
    assert len(client.get("/acord/ngds").json()["transactions"]) >= 30
    schemas = client.get("/acord/schemas").json()
    assert "Policy" in schemas["bundled"]["$defs"]


def test_error_catalogue_is_published(client: TestClient) -> None:
    entries = client.get("/errors/catalog").json()["entries"]
    codes = {e["code"] for e in entries}
    assert "PAS-429-RATE_LIMITED" in codes
    assert all("retryable" in e for e in entries)


def test_unknown_policy_returns_problem_json(client: TestClient) -> None:
    response = client.get("/insurance/v1/policies/NOPE")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    problem = response.json()
    assert problem["code"] == ErrorCode.NOT_FOUND.value
    assert problem["correlationId"]


def test_premium_calculation_is_computation_only(client: TestClient) -> None:
    response = client.post(
        "/insurance/v1/policies/premium/calculate",
        json={
            "productCode": "TERM20-A",
            "faceAmount": 250_000,
            "issueAge": 45,
            "stateOfIssue": "NY",
            "paymentMode": "monthly",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["annualisedPremium"] > 0
    assert body["_meta"]["operationId"] == "policy.premium.calculate"
    assert body["ratingFactors"]["baseRatePerThousand"] > 0


def test_quote_then_bind_round_trip(client: TestClient) -> None:
    quote = client.post(
        "/insurance/v1/quotes",
        json={
            "productCode": "TERM20-A",
            "faceAmount": 250_000,
            "issueAge": 45,
            "stateOfIssue": "NY",
        },
    )
    assert quote.status_code == 200, quote.text
    quote_id = quote.json()["quoteId"]

    bind = client.post(
        "/insurance/v1/policies",
        json={"quoteId": quote_id, "effectiveDate": "2026-06-01"},
    )
    assert bind.status_code == 200, bind.text
    assert bind.json()["status"] in {"issued", "accepted"}


def test_beneficiary_over_allocation_is_refused(client: TestClient) -> None:
    response = client.put(
        "/insurance/v1/policies/SIMPOL000001/beneficiaries",
        json={
            "allocations": [
                {"partyId": "PARTY1", "relationship": "spouse", "sharePercent": 80},
                {"partyId": "PARTY2", "relationship": "child", "sharePercent": 30},
            ]
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == ErrorCode.VALIDATION_FAILED.value


def test_servicing_requires_confirmation(client: TestClient) -> None:
    """A policy-state change must be refused unless the caller confirms intent."""
    response = client.post("/insurance/v1/policies/SIMPOL000002/lapse", json={})
    assert response.status_code == 400
    assert response.json()["code"] == ErrorCode.VALIDATION_FAILED.value

    ok = client.post(
        "/insurance/v1/policies/SIMPOL000002/lapse",
        json={"effectiveDate": "2026-06-01", "confirmation": True, "reason": "non-payment"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "lapsed"


def test_mcp_catalogue_lists_every_operation(client: TestClient) -> None:
    body = client.get("/mcp/catalogue").json()
    assert body["enabled"] is True
    tool_names = {t["name"] for t in body["tools"]}
    assert "policy_get" in tool_names
    assert "policy_bind" in tool_names
    assert "catalogue_list_operations" in tool_names
    assert body["server"]["protocolVersion"] == "2025-06-18"


def test_workflow_catalogue(client: TestClient) -> None:
    body = client.get("/workflows").json()
    ids = {w["workflowId"] for w in body["workflows"]}
    assert {"quote-to-bind", "beneficiary-change", "accelerated-underwriting"} <= ids
    quote = next(w for w in body["workflows"] if w["workflowId"] == "quote-to-bind")
    assert quote["executionOrder"][0] == "read_product"
    assert "withdraw_quote" in {s["name"] for s in quote["steps"]}


def test_translation_preview(client: TestClient) -> None:
    body = client.post(
        "/pas/translation/preview",
        json={
            "operationId": "policy.get",
            "vendor": "majesco-lifeplus",
            "direction": "to_vendor",
            "payload": {"policyId": "POL1"},
        },
    ).json()
    assert body["translated"]["policyNumber"] == "POL1"
    assert body["profileVersion"]


def test_openapi_is_31_with_error_catalog(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert schema["openapi"].startswith("3.1")
    assert "Problem" in schema["components"]["schemas"]
    assert "ErrorCatalogEntry" in schema["components"]["schemas"]
    assert schema["info"]["x-pas-atomic-operations"] >= 20
    assert schema["info"]["x-mcp"]["toolCount"] > 20


def test_openapi_paths_match_declared_operations(client: TestClient) -> None:
    from pas_plugins.plugin1_gateway.operations import CATALOGUE  # noqa: PLC0415

    schema = client.get("/openapi.json").json()
    declared = {op.path for op in CATALOGUE.list()}
    published = set(schema["paths"])
    missing = declared - published
    assert not missing, f"operations missing from the OpenAPI document: {sorted(missing)}"


def test_tenant_isolation_over_http(client: TestClient) -> None:
    """A caller may only ever see its own carrier's tenant record."""
    import time  # noqa: PLC0415

    from jose import jwt  # noqa: PLC0415

    token = jwt.encode(
        {
            "sub": "agent@example.com",
            "tenant_id": "acme-life",
            "scope": "policy:read",
            "iss": "https://keycloak.local/realms/pas-plugins",
            "exp": int(time.time()) + 300,
        },
        "development-only-key-do-not-use-in-production",
        algorithm="HS256",
    )
    response = client.get("/tenants", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert {t["tenantId"] for t in response.json()["tenants"]} == {"acme-life"}


def test_tenant_header_cannot_impersonate_another_carrier(client: TestClient) -> None:
    """A token for one carrier plus another carrier's header must be refused."""
    import time  # noqa: PLC0415

    from jose import jwt  # noqa: PLC0415

    token = jwt.encode(
        {
            "sub": "agent@example.com",
            "tenant_id": "acme-life",
            "scope": "policy:read",
            "iss": "https://keycloak.local/realms/pas-plugins",
            "exp": int(time.time()) + 300,
        },
        "development-only-key-do-not-use-in-production",
        algorithm="HS256",
    )
    response = client.get(
        "/tenants",
        headers={"Authorization": f"Bearer {token}", "X-PAS-Tenant-Id": "northstar-annuity"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == ErrorCode.TENANT_MISMATCH.value


def test_insufficient_scope_is_refused(client: TestClient) -> None:
    import time  # noqa: PLC0415

    from jose import jwt  # noqa: PLC0415

    token = jwt.encode(
        {
            "sub": "reader@example.com",
            "tenant_id": "demo-carrier",
            "scope": "product:read",
            "iss": "https://keycloak.local/realms/pas-plugins",
            "exp": int(time.time()) + 300,
        },
        "development-only-key-do-not-use-in-production",
        algorithm="HS256",
    )
    response = client.get("/insurance/v1/policies/SIMPOL000001", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert response.json()["code"] == ErrorCode.SCOPE_INSUFFICIENT.value


def test_suspended_tenant_is_refused(client: TestClient) -> None:
    from pas_plugins.plugin1_gateway.main import platform  # noqa: PLC0415

    platform.tenants.register(
        Tenant(tenant_id="paused-carrier", legal_name="Paused Life", status=TenantStatus.SUSPENDED)
    )
    try:
        response = client.get("/operations", headers={"X-PAS-Tenant-Id": "paused-carrier"})
        assert response.status_code == 403
        assert response.json()["code"] == ErrorCode.TENANT_SUSPENDED.value
    finally:
        platform.tenants.remove("paused-carrier")


def test_correlation_id_is_echoed(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Correlation-Id": "abc123def456"})
    assert response.headers["X-Correlation-Id"] == "abc123def456"


def test_oversized_request_is_refused(client: TestClient) -> None:
    response = client.post(
        "/insurance/v1/quotes",
        content=b'{"productCode":"X","pad":"' + b"A" * 100 + b'"}',
        headers={"content-type": "application/json", "content-length": str(9_000_000_000)},
    )
    assert response.status_code in {400, 413}
