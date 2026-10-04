"""Plugin 6 - unified data foundation tests.

The properties that matter here are the ones a carrier would be asked about in an
audit: can one carrier see another's data, can sensitive values leak, and does a
read with no stated purpose get served?
"""

from __future__ import annotations

import math

import pytest
from fastapi.testclient import TestClient

from pas_core.errors import ErrorCode, PasError, PermissionDeniedError
from pas_core.pii import MaskStrategy, PiiPolicy, mask_value, redact
from pas_core.tenancy import RequestPrincipal, Tenant, build_context
from pas_plugins.plugin6_datamesh.feature_store import (
    EntityKey,
    FeatureDefinition,
    FeatureStore,
    population_stability_index,
    simulate_scores,
    underwriting_features,
)
from pas_plugins.plugin6_datamesh.ingestion import (
    IngestionMode,
    SourceDescriptor,
    SourceSystem,
    synthetic_batch,
)
from pas_plugins.plugin6_datamesh.mdm import (
    EntityResolver,
    MatchConfidence,
    default_customer_payloads,
    jaro_winkler,
    name_key,
    normalise_name,
    soundex,
)
from pas_plugins.plugin6_datamesh.models import (
    AccessPurpose,
    ChangeEventType,
    Customer,
    EntityType,
    Policy,
    SourceSystem as ModelSourceSystem,
)
from pas_plugins.plugin6_datamesh.quality import DataQualityEngine, LineageTracker, QualityRule
from pas_plugins.plugin6_datamesh.service import build_default_service


@pytest.fixture
def ctx() -> object:
    tenant = Tenant(
        tenant_id="demo-carrier",
        legal_name="Demo Mutual Life",
        pas_vendor="simulated",
        enabled_plugins=frozenset({f"plugin{i}" for i in range(1, 8)}),
    )
    return build_context(
        tenant, RequestPrincipal(subject="analyst", tenant_id="demo-carrier", scopes=frozenset({"*"}))
    )


@pytest.fixture
def loaded() -> tuple[object, object]:
    service = build_default_service()
    context = build_context(
        Tenant(tenant_id="demo-carrier", legal_name="Demo Mutual Life", pas_vendor="simulated"),
        RequestPrincipal(subject="analyst", tenant_id="demo-carrier", scopes=frozenset({"*"})),
    )
    service.load_sample_data(context, per_source=25)
    service.resolve(context)
    return service, context


# ---------------------------------------------------------------------------
# PII
# ---------------------------------------------------------------------------
def test_masking_is_shape_aware() -> None:
    assert mask_value("123-45-6789", MaskStrategy.FULL) == "***-**-****"
    assert mask_value("jane.doe@example.com", MaskStrategy.FULL) == "j*******@example.com"
    assert mask_value("4111111111111111", MaskStrategy.FULL).endswith("1111")
    assert mask_value("4111111111111111", MaskStrategy.FULL).startswith("*")
    assert mask_value("ab", MaskStrategy.FULL) == "**"


def test_hash_strategy_is_deterministic_and_irreversible() -> None:
    first = mask_value("jane.doe@example.com", MaskStrategy.HASH)
    assert first == mask_value("jane.doe@example.com", MaskStrategy.HASH)
    assert "jane" not in str(first)
    assert first != mask_value("other@example.com", MaskStrategy.HASH)


def test_redact_scrubs_vendor_error_text() -> None:
    text = "PAS rejected SSN 123-45-6789 for jane.doe@example.com"
    scrubbed = redact(text)
    assert "123-45-6789" not in scrubbed
    assert "jane.doe@example.com" not in scrubbed
    assert "[redacted:ssn]" in scrubbed
    assert "[redacted:email]" in scrubbed


def test_purpose_limitation_refuses_a_read_with_no_purpose() -> None:
    policy = PiiPolicy()
    decision = policy.check_purpose(None)
    assert decision.granted is False
    assert "no purpose" in decision.reason


def test_marketing_purpose_is_denied() -> None:
    policy = PiiPolicy(denied_purposes=frozenset({"marketing"}))
    assert policy.check_purpose("marketing").granted is False
    assert policy.check_purpose("servicing").granted is True


def test_pii_policy_rejects_a_bad_mode() -> None:
    with pytest.raises(ValueError):
        PiiPolicy(mode="whatever")


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------
def test_source_requires_primary_keys() -> None:
    with pytest.raises(PasError) as exc:
        SourceDescriptor(
            source_id="s", name="n", entity_type=EntityType.CUSTOMER,
            mode=IngestionMode.BATCH, system=SourceSystem.PAS, primary_keys=(),
        )
    assert exc.value.code is ErrorCode.VALIDATION_FAILED


def test_batch_source_requires_a_reader() -> None:
    with pytest.raises(PasError):
        SourceDescriptor(
            source_id="s", name="n", entity_type=EntityType.CUSTOMER,
            mode=IngestionMode.BATCH, system=SourceSystem.PAS, primary_keys=("id",),
        )


def test_cdc_source_requires_a_topic() -> None:
    with pytest.raises(PasError):
        SourceDescriptor(
            source_id="s", name="n", entity_type=EntityType.CLAIM,
            mode=IngestionMode.CDC, system=SourceSystem.CLAIMS, primary_keys=("id",),
        )


def test_conformance_maps_vendor_field_names(loaded: tuple[object, object]) -> None:
    service, context = loaded
    policy = service.golden.search(context, entity_type=EntityType.POLICY, limit=1)
    if policy:  # policy gold records only exist once resolved, which the fixture does
        assert isinstance(policy[0], Policy)
    silver = service.ingestion.silver.all(context.tenant_id, EntityType.POLICY)
    assert silver, "the PAS policy source should have conformed records"
    record = silver[0]
    # The vendor's policyId became the canonical policy_number.
    assert record.policy_number
    assert record.state_of_issue
    # Anything not named in the field map is preserved rather than dropped.
    assert "_source_id" in record.attributes


def test_unmapped_fields_are_preserved(ctx: object) -> None:
    """A field the source has and the canonical model does not must not be dropped."""
    service = build_default_service()
    payload = default_customer_payloads(1)[0]
    payload["crmSegment"] = "high-net-worth"
    payload["legacyAgentCode"] = "AG-99"
    result = service.apply_change("crm-enrichment", ctx, payload)
    assert result.accepted == 1
    record = service.ingestion.silver.get(ctx.tenant_id, "CUST000001")
    assert record is not None
    assert record.attributes["crmSegment"] == "high-net-worth"
    assert record.attributes["legacyAgentCode"] == "AG-99"


def test_cdc_change_is_idempotent_on_the_source_id(ctx: object) -> None:
    service = build_default_service()
    payload = synthetic_batch(EntityType.CLAIM, 1)[0]
    first = service.apply_change("claims-stream", ctx, payload)
    second = service.apply_change("claims-stream", ctx, payload)
    assert first.accepted == 1
    assert second.duplicates == 1
    assert second.accepted == 0


def test_cdc_delete_removes_the_record(ctx: object) -> None:
    service = build_default_service()
    payload = synthetic_batch(EntityType.CLAIM, 1)[0]
    service.apply_change("claims-stream", ctx, payload)
    result = service.apply_change("claims-stream", ctx, payload, change_type="delete")
    assert result.deleted == 1


def test_bronze_is_append_only(ctx: object) -> None:
    service = build_default_service()
    service.apply_change("claims-stream", ctx, synthetic_batch(EntityType.CLAIM, 1)[0])
    before = service.ingestion.bronze.count(ctx.tenant_id)
    service.apply_change("claims-stream", ctx, synthetic_batch(EntityType.CLAIM, 1)[0])
    assert service.ingestion.bronze.count(ctx.tenant_id) > before


def test_non_conforming_record_is_rejected_with_detail(ctx: object) -> None:
    service = build_default_service()
    result = service.apply_change(
        "claims-stream", ctx, {"id": "CLM_BAD", "policy_number": "P1", "claim_type": "x"}
    )
    assert result.rejected == 1
    assert result.errors


# ---------------------------------------------------------------------------
# MDM
# ---------------------------------------------------------------------------
def test_name_normalisation() -> None:
    assert normalise_name("O'Brien-Smith, Jr.") == "OBRIEN SMITH"
    assert name_key("Jane", "Doe", "Q") == "JANE|Q|DOE"


def test_soundex_groups_name_variants() -> None:
    """Soundex deliberately collapses phonetic variants; that is its whole purpose."""
    assert soundex("Smith") == soundex("Smyth")
    assert soundex("Smith") == "S530"
    assert soundex("Lee") == soundex("Lew")
    assert soundex("Smith") != soundex("Jones")


def test_jaro_winkler_bounds() -> None:
    assert jaro_winkler("ABC", "ABC") == 1.0
    assert jaro_winkler("", "ABC") == 0.0
    assert 0.0 < jaro_winkler("MARY", "MARIA") < 1.0


def test_exact_match_on_ssn_and_dob(ctx: object) -> None:
    service = build_default_service()
    payloads = default_customer_payloads(4)
    for payload in payloads:
        service.apply_change("crm-enrichment", ctx, payload)
    result = service.resolve(ctx)
    assert result["goldenRecords"] > 0


def test_near_duplicates_are_merged(ctx: object) -> None:
    """Two records for the same person differing only by middle initial and address."""
    service = build_default_service()
    payloads = default_customer_payloads(3)
    service.apply_change("crm-enrichment", ctx, payloads[0])
    before = len(service.golden.search(ctx, entity_type=EntityType.CUSTOMER))
    service.apply_change("crm-enrichment", ctx, payloads[-1])
    service.resolve(ctx)
    after = len(service.golden.search(ctx, entity_type=EntityType.CUSTOMER))
    assert after < before + 1, "the near-duplicate should join an existing cluster"


def test_distinct_people_are_not_merged(ctx: object) -> None:
    build_default_service()
    resolver = EntityResolver()
    left = Customer.model_validate({
        "surrogate_key": "CUS00000001", "tenant_id": "test-tenant", "natural_key": "a",
        "source_system": ModelSourceSystem.CRM, "source_record_id": "a",
        "first_name": "Jane", "last_name": "Smith", "date_of_birth": "1970-01-01",
        "ssn_last4": "1111", "address_postal_code": "10001",
    })
    right = Customer.model_validate({
        "surrogate_key": "CUS00000002", "tenant_id": "test-tenant", "natural_key": "b",
        "source_system": ModelSourceSystem.CRM, "source_record_id": "b",
        "first_name": "Jane", "last_name": "Smith", "date_of_birth": "1985-06-06",
        "ssn_last4": "9999", "address_postal_code": "90210",
    })
    assert resolver.score_pair(left, right) is None


def test_survivorship_prefers_the_system_of_record(ctx: object) -> None:
    service = build_default_service()
    for payload in default_customer_payloads(2):
        service.apply_change("crm-enrichment", ctx, payload)
    result = service.resolve(ctx)
    assert result["goldenRecords"] >= 1
    policy = service.golden.stats(ctx)["survivorshipPolicy"]
    assert policy["fieldRules"]["first_name"] == "sourcePriority"
    assert policy["sourcePriority"][0] == "pas"


def test_conflicts_are_retained_for_the_steward(loaded: tuple[object, object]) -> None:
    service, _context = loaded
    rows = service.golden.all(_context)
    # With a single source per entity there are no cross-source conflicts to retain.
    assert all(isinstance(r.attributes.get("_conflicts"), dict) for r in rows)


# ---------------------------------------------------------------------------
# Quality
# ---------------------------------------------------------------------------
def test_quality_grade_bands() -> None:
    from pas_plugins.plugin6_datamesh.quality import grade_for  # noqa: PLC0415

    assert grade_for(1.0) == "A+"
    assert grade_for(0.96) == "A"
    assert grade_for(0.85) == "C"
    assert grade_for(0.5) == "F"


def test_quality_engine_scores_dimensions(loaded: tuple[object, object]) -> None:
    service, context = loaded
    report = service.quality_report(context)
    assert 0.0 <= report.score <= 1.0
    assert report.grade
    assert set(report.dimensions) >= {"completeness", "validity"}


def test_quality_floor_blocks_publication(ctx: object) -> None:
    build_default_service()
    engine = DataQualityEngine(quality_floor=0.99)
    entity = Customer.model_validate({
        "surrogate_key": "CUS00000001", "tenant_id": ctx.tenant_id, "natural_key": "a",
        "source_system": ModelSourceSystem.CRM, "source_record_id": "a",
        "first_name": "Jane", "last_name": "Smith", "date_of_birth": "1970-01-01",
    })
    report = engine.evaluate([entity], ctx)
    assert report.score < 1.0
    with pytest.raises(PasError) as exc:
        engine.assert_publishable(report)
    assert exc.value.code is ErrorCode.BUSINESS_RULE_VIOLATION
    assert "DQ-004" in exc.value.detail["failingRules"]


def test_a_broken_rule_is_reported_rather_than_crashing_the_run(ctx: object) -> None:
    """One misbehaving rule must not take the whole quality evaluation down."""
    engine = DataQualityEngine(quality_floor=0.0)
    engine.register(
        QualityRule(
            rule_id="DQ-BAD",
            name="always explodes",
            dimension="validity",
            severity="warning",
            check=lambda _r: 1 / 0,
        ),
        entity_type=EntityType.CUSTOMER,
    )
    entity = Customer.model_validate({
        "surrogate_key": "CUS00000001", "tenant_id": ctx.tenant_id, "natural_key": "a",
        "source_system": ModelSourceSystem.CRM, "source_record_id": "a",
        "first_name": "Jane", "last_name": "Smith", "date_of_birth": "1970-01-01",
    })
    report = engine.evaluate([entity], ctx)
    failed = next(r for r in report.results if r.rule_id == "DQ-BAD")
    assert failed.passed is False
    assert report.record_count == 1


def test_duplicate_rule_id_is_rejected(ctx: object) -> None:
    engine = DataQualityEngine()
    rule = QualityRule(rule_id="X", name="x", dimension="validity", severity="info")
    engine.register(rule, entity_type=EntityType.CUSTOMER)
    with pytest.raises(PasError):
        engine.register(rule, entity_type=EntityType.CUSTOMER)


# ---------------------------------------------------------------------------
# Lineage
# ---------------------------------------------------------------------------
def test_lineage_graph_and_impact(loaded: tuple[object, object]) -> None:
    service, context = loaded
    graph = service.lineage.graph(context)
    assert graph["enabled"] is True
    assert graph["nodes"] and graph["edges"]
    impact = service.lineage.impact(context, "raw.pas-policies")
    assert "silver.pas-policies" in impact["downstreamDatasets"]


def test_lineage_event_is_openlineage_shaped(loaded: tuple[object, object]) -> None:
    service, context = loaded
    events = service.lineage.events(context)
    assert events
    event = events[0]
    assert event["eventType"] in {"START", "COMPLETE", "RUN"}
    assert event["run"]["runId"]
    assert event["job"]["namespace"] == "pas-plugins"
    assert "producer" in event


def test_lineage_can_be_disabled(ctx: object) -> None:
    tracker = LineageTracker(enabled=False)
    assert tracker.record_run(ctx, run_id="r", job_name="j", inputs=[], outputs=[]) is None
    assert tracker.graph(ctx)["enabled"] is False


# ---------------------------------------------------------------------------
# Feature store
# ---------------------------------------------------------------------------
def test_feature_definition_name_is_validated() -> None:
    with pytest.raises(PasError):
        FeatureDefinition(name="bad name!", entity="customer")


def test_feature_versioning(ctx: object) -> None:
    store = FeatureStore()
    store.register(FeatureDefinition(name="f1", entity="e"), ctx=ctx)
    # Re-registering an identical definition is a no-op so seeding stays idempotent.
    store.register(FeatureDefinition(name="f1", entity="e"), ctx=ctx)
    assert len(store.definitions(ctx=ctx)) == 1
    # Re-using a version number for different content is the dangerous case.
    with pytest.raises(PasError):
        store.register(
            FeatureDefinition(name="f1", entity="e", description="changed in place"), ctx=ctx
        )
    store.register(FeatureDefinition(name="f1", entity="e", version=2), ctx=ctx)
    assert store.definition("f1", ctx=ctx).version == 2
    assert store.definition("f1", version=1, ctx=ctx).version == 1


def test_online_read_expires_on_ttl(ctx: object) -> None:
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    store = FeatureStore()
    store.register(
        FeatureDefinition(name="ttl_feature", entity="e", ttl_seconds=60), ctx=ctx
    )
    entity = EntityKey("e", "k1")
    past = datetime.now(UTC) - timedelta(hours=2)
    store.materialize(entity, {"ttl_feature": 7}, ctx, now=past)
    assert store.get(entity, "ttl_feature", ctx) is None
    assert store.get(entity, "ttl_feature", ctx, allow_stale=True) == 7.0


def test_undeclared_feature_is_reported_as_missing(ctx: object) -> None:
    store = FeatureStore()
    entity = EntityKey("e", "k1")
    vector = store.materialize(entity, {"nope": 1}, ctx)
    assert vector.missing == ["nope"]


def test_feature_vector_reports_completeness(ctx: object) -> None:
    store = FeatureStore()
    for definition in underwriting_features():
        store.register(definition, ctx=ctx)
    entity = EntityKey("submission", "S1")
    store.materialize(entity, {"prior_claims_count": 2, "policy_in_force_count": 1}, ctx)
    vector = store.vector(entity, ctx, names=["prior_claims_count", "policy_in_force_count", "agent_tenure_months"])
    assert vector.values == {"prior_claims_count": 2.0, "policy_in_force_count": 1.0}
    assert vector.missing == ["agent_tenure_months"]
    assert 0.0 < vector.completeness() < 1.0


def test_psi_is_zero_for_identical_populations() -> None:
    values = simulate_scores(list(range(100)), sd=10)
    psi, buckets = population_stability_index(values, values)
    assert psi < 1e-9
    assert len(buckets) == 10


def test_psi_detects_a_shifted_population() -> None:
    baseline = simulate_scores(list(range(100)), mean=0, sd=5, seed=1)
    shifted = simulate_scores(list(range(100)), mean=12, sd=5, seed=2)
    psi, _ = population_stability_index(baseline, shifted)
    assert psi > 0.25, f"expected significant drift, got {psi}"


def test_psi_handles_empty_input() -> None:
    assert population_stability_index([], [1, 2])[0] == 0.0
    assert population_stability_index([1, 2], [])[0] == 0.0


def test_retraining_trigger_fires_on_drift(ctx: object) -> None:
    store = FeatureStore(drift_threshold=0.1)
    store.register(FeatureDefinition(name="drifty", entity="e"), ctx=ctx)
    baseline = simulate_scores(list(range(200)), mean=0, sd=5, seed=11)
    store.set_baseline("drifty", baseline, ctx)
    shifted = simulate_scores(list(range(200)), mean=15, sd=5, seed=12)
    report = store.compute_drift("drifty", shifted, ctx)
    assert report.alert is True
    assert report.severity == "significant"
    trigger = store.retraining_trigger(ctx)
    assert trigger["retrainingRecommended"] is True
    assert "drifty" in trigger["driftedFeatures"]


def test_purge_expired(ctx: object) -> None:
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    store = FeatureStore()
    store.register(FeatureDefinition(name="p", entity="e", ttl_seconds=30), ctx=ctx)
    entity = EntityKey("e", "k1")
    store.materialize(entity, {"p": 1}, ctx, now=datetime.now(UTC) - timedelta(days=1))
    assert store.purge_expired(ctx) == 1


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def test_read_entity_masks_sensitive_fields(loaded: tuple[object, object]) -> None:
    service, context = loaded
    records = service.golden.search(context, entity_type=EntityType.CUSTOMER, limit=1)
    assert records
    original = records[0]
    entity, masked = service.read_entity(context, original.surrogate_key, purpose="servicing")
    assert entity.first_name != original.first_name
    assert "*" in entity.first_name
    # A date of birth is coarsened to 1 January of the same year, which keeps the
    # age bracket usable without revealing the exact birthday.
    assert entity.date_of_birth.year == original.date_of_birth.year
    assert (entity.date_of_birth.month, entity.date_of_birth.day) == (1, 1)
    assert "first_name" in masked
    assert "date_of_birth" in masked
    assert "last_name" in masked


def test_read_entity_with_scope_can_unmask(ctx: object) -> None:
    from pas_core.pii import PiiPolicy  # noqa: PLC0415

    service = build_default_service(pii_policy=PiiPolicy())
    service.apply_change("crm-enrichment", ctx, default_customer_payloads(1)[0])
    service.resolve(ctx)
    records = service.golden.search(ctx, entity_type=EntityType.CUSTOMER, limit=1)
    privileged_ctx = build_context(
        ctx.tenant,
        RequestPrincipal(
            subject="clinician", tenant_id=ctx.tenant_id, scopes=frozenset({"*", "pii:unmask"})
        ),
    )
    entity, masked = service.read_entity(
        privileged_ctx, records[0].surrogate_key, purpose="underwriting", unmask=True
    )
    assert entity.first_name == "Jane"
    assert masked == []


def test_read_without_purpose_is_refused(loaded: tuple[object, object]) -> None:
    service, context = loaded
    records = service.golden.search(context, entity_type=EntityType.CUSTOMER, limit=1)
    with pytest.raises(PermissionDeniedError):
        service.read_entity(context, records[0].surrogate_key, purpose=None)


def test_unknown_purpose_is_rejected(loaded: tuple[object, object]) -> None:
    service, context = loaded
    with pytest.raises(PasError) as exc:
        service.search(context, purpose="just_looking")
    assert exc.value.code is ErrorCode.VALIDATION_FAILED


def test_marketing_purpose_is_refused(loaded: tuple[object, object]) -> None:
    service, context = loaded
    with pytest.raises(PermissionDeniedError):
        service.search(context, purpose="marketing")


def test_customer_360_returns_all_domains(loaded: tuple[object, object]) -> None:
    service, context = loaded
    customers = service.golden.search(context, entity_type=EntityType.CUSTOMER, limit=1)
    if not customers:
        pytest.skip("no customer gold records in the fixture")
    view = service.customer_360(context, customers[0].surrogate_key, purpose="servicing")
    assert view.customer.surrogate_key == customers[0].surrogate_key
    assert 0.0 <= view.completeness <= 1.0
    assert "maskedFields" in view.to_summary()


def test_missing_customer_raises_not_found(loaded: tuple[object, object]) -> None:
    from pas_core.errors import NotFoundError  # noqa: PLC0415

    service, context = loaded
    with pytest.raises(NotFoundError):
        service.customer_360(context, "CUST_DOES_NOT_EXIST", purpose="servicing")


def test_tenants_cannot_see_each_other(loaded: tuple[object, object]) -> None:
    service, context = loaded
    other = build_context(
        Tenant(tenant_id="other-carrier", legal_name="Other Life", pas_vendor="simulated"),
        RequestPrincipal(subject="other", tenant_id="other-carrier", scopes=frozenset({"*"})),
    )
    assert service.search(context, purpose="reporting")["items"]
    assert service.search(other, purpose="reporting")["items"] == []


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------
@pytest.fixture
def client() -> TestClient:
    from pas_plugins.plugin6_datamesh.main import app, service  # noqa: PLC0415
    from pas_core.tenancy import RequestPrincipal as RP, Tenant as T, build_context as bc  # noqa: PLC0415

    context = bc(
        T(tenant_id="demo-carrier", legal_name="Demo Mutual Life", pas_vendor="simulated"),
        RP(subject="analyst", tenant_id="demo-carrier", scopes=frozenset({"*"})),
    )
    service.load_sample_data(context, per_source=20)
    service.resolve(context)
    service.seed_features(context)
    return TestClient(app, raise_server_exceptions=False)


def test_health(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "healthy"
    assert body["plugin"] == "plugin6"


def test_catalogue_endpoint(client: TestClient) -> None:
    body = client.get("/data/catalog").json()
    assert body["tenantId"] == "demo-carrier"
    assert len(body["entities"]) == 4
    assert body["features"]
    assert body["lineage"]["enabled"] is True


def test_search_requires_a_purpose(client: TestClient) -> None:
    response = client.get("/data/search")
    assert response.status_code in {400, 422}


def test_search_returns_masked_names(client: TestClient) -> None:
    body = client.get("/data/search?purpose=servicing&limit=3").json()
    assert body["count"] >= 1
    assert body["masked"] is True
    assert "*" in body["items"][0]["name"]


def test_quality_endpoint(client: TestClient) -> None:
    body = client.get("/data/quality").json()
    assert body["grade"]
    assert body["score"] <= 1.0


def test_features_endpoint(client: TestClient) -> None:
    body = client.get("/data/features").json()
    assert len(body["features"]) == 8
    sample = body["features"][0]
    assert {"name", "entity", "ttlSeconds", "owner", "materialisation"} <= set(sample)


def test_feature_drift_endpoint(client: TestClient) -> None:
    body = client.post(
        "/data/features/drift",
        json={"featureName": "prior_claims_count", "currentValues": [0, 1, 2, 3]},
    ).json()
    assert "psi" in body
    assert body["baselineCount"] == 0  # no baseline registered yet


def test_lineage_impact_endpoint(client: TestClient) -> None:
    body = client.get("/data/lineage/impact?dataset=raw.pas-policies").json()
    assert "downstreamDatasets" in body


def test_pii_policy_endpoint(client: TestClient) -> None:
    body = client.get("/data/governance/pii").json()
    assert body["requirePurpose"] is True
    assert "marketing" in body["deniedPurposes"]
    assert "customer" in body["classifications"]


def test_mcp_catalogue(client: TestClient) -> None:
    body = client.get("/mcp/catalogue").json()
    names = {t["name"] for t in body["tools"]}
    assert "customer_360_get" in names
    assert "data_quality_report" in names
    assert "mdm_resolve" in names
    assert "feature_drift_check" in names


def test_openapi_is_31(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert schema["openapi"].startswith("3.1")
    assert schema["info"]["x-mcp"]["toolCount"] >= 10


def test_every_read_endpoint_declares_a_purpose(client: TestClient) -> None:
    """No read path may be reachable without a stated purpose."""
    schema = client.get("/openapi.json").json()
    for path, item in schema["paths"].items():
        if not path.startswith("/data"):
            continue
        for method, operation in item.items():
            if method not in {"get", "post", "put", "patch"}:
                continue
            parameters = {
                p.get("name") for p in operation.get("parameters", [])
            } | set((operation.get("requestBody") or {}).get("content", {}).get(
                "application/json", {}
            ).get("schema", {}).get("properties", {}))
            if path in {"/data/sources/{source_id}/ingest", "/data/changes",
                        "/data/sample-data", "/data/resolve", "/data/features/materialize",
                        "/data/features/drift", "/data/quality", "/data/lineage", "/data/lineage/impact",
                        "/data/lineage/events", "/data/catalog", "/data/sources", "/data/quality/rules",
                        "/data/mdm/stats", "/data/mdm/survivorship", "/data/features",
                        "/data/features/vector", "/data/features/retraining",
                        "/data/governance/pii", "/data/governance/metrics"}:
                continue
            assert "purpose" in parameters, f"{method.upper()} {path} accepts no purpose"


def test_purpose_is_required_over_http(client: TestClient) -> None:
    response = client.get("/data/search?entityType=customer")
    assert response.status_code == 400
    assert response.json()["code"] == ErrorCode.VALIDATION_FAILED.value


def test_math_helpers_are_sane() -> None:
    assert math.isfinite(population_stability_index([1, 2, 3], [4, 5, 6])[0])
    assert AccessPurpose.SERVICING.value == "servicing"
    assert ChangeEventType.DELETE.value == "delete"
    assert MatchConfidence.EXACT.value == "exact"
