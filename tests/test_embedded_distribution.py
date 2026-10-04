"""Tests for Plugin 5 - Embedded Distribution."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pas_core.tenancy import RequestPrincipal, Tenant, build_context

from pas_plugins.plugin5_embedded.engine import (
    DistributionEngine,
    DistributionError,
)
from pas_plugins.plugin5_embedded.examples import catalog, demo_partner, starter_partner
from pas_plugins.plugin5_embedded.mcp_server import build_registry
from pas_plugins.plugin5_embedded.models import (
    ApplicantProfile,
    DistributionProduct,
    KycResult,
    KycStatus,
    PartnerStatus,
    PartnerTier,
    PaymentMethod,
    PolicyStatus,
    ProductEntitlement,
    SuitabilityOutcome,
)
from pas_plugins.plugin5_embedded.settings import Plugin5Settings

TENANT = "demo-carrier"
GOLD = "PARTNER-GOLD-001"
STARTER = "PARTNER-STARTER-002"
TERM = "DIST-TERM-SIMPLE"


@pytest.fixture
def engine() -> DistributionEngine:
    eng = DistributionEngine(Plugin5Settings())
    eng.register_partner(demo_partner())
    eng.register_partner(starter_partner())
    for product in catalog():
        eng.register_product(product)
    return eng


def applicant(**overrides: object) -> ApplicantProfile:
    payload: dict[str, object] = {
        "first_name": "Dana",
        "last_name": "Reed",
        "date_of_birth": date(1988, 4, 12),
        "state_of_residence": "CA",
        "coverage_need": 500_000.0,
        "existing_coverage": 100_000.0,
        "dependents": 2,
        "annual_income": 90_000.0,
    }
    payload.update(overrides)
    return ApplicantProfile.model_validate(payload)


def do_quote(engine: DistributionEngine, product_id: str = TERM, **kwargs: object):
    payload: dict[str, object] = {
        "tenant_id": TENANT,
        "partner_id": GOLD,
        "product_id": product_id,
        "applicant": applicant(),
        "face_amount": 500_000.0,
        "term_years": 20,
    }
    payload.update(kwargs)
    return engine.quote(**payload)


def to_bound_policy(engine: DistributionEngine, **kwargs: object):
    """Run the happy path through to a bound policy."""
    quote = do_quote(engine, **kwargs)
    engine.screen(quote.quote_id, "driversLicense", "DL-1")
    return engine.bind(quote.quote_id)


def ctx(tenant: str = TENANT) -> object:
    return build_context(
        Tenant(tenant_id=tenant, legal_name="Test"),
        RequestPrincipal(subject="partner.tester", tenant_id=tenant, scopes=frozenset({"*"})),
    )


def call(handler, arguments: dict) -> dict:
    return asyncio.run(handler(arguments, ctx()))


@pytest.fixture
def client():
    from pas_plugins.plugin5_embedded.main import app

    with TestClient(app) as test_client:
        from pas_plugins.plugin5_embedded import main

        engine = DistributionEngine(Plugin5Settings())
        for partner in (demo_partner(), starter_partner()):
            engine.register_partner(partner)
        for product in catalog():
            engine.register_product(product)
        main.platform.extra["distribution_engine"] = engine
        yield test_client


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------


def test_wire_form_round_trips():
    definition = catalog()[0]
    from pas_plugins.plugin5_embedded.models import DistributionProduct as DP

    assert DP.model_validate(definition.to_dict()).to_dict() == definition.to_dict()


def test_partner_tier_ordering_is_total():
    assert PartnerTier.PLATINUM.default_commission_bps > PartnerTier.GOLD.default_commission_bps
    assert PartnerTier.GOLD.default_commission_bps > PartnerTier.STARTER.default_commission_bps
    assert PartnerTier.PLATINUM.settlement_days < PartnerTier.STARTER.settlement_days


def test_only_an_active_partner_may_sell():
    assert PartnerStatus.ACTIVE.may_sell
    assert not PartnerStatus.SUSPENDED.may_sell
    assert not PartnerStatus.PENDING.may_sell


def test_only_a_clear_kyc_permits_binding():
    assert KycStatus.CLEAR.permits_bind
    for status in (KycStatus.NOT_STARTED, KycStatus.REVIEW, KycStatus.FAILED, KycStatus.EXPIRED):
        assert not status.permits_bind


def test_product_needs_rates_to_be_quotable():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="cannot be quoted"):
        DistributionProduct(
            product_id="PROD-X",
            name="Empty rates",
            carrier="Carrier Co",
            category="term",
            min_face_amount=1_000.0,
            max_face_amount=100_000.0,
            rate_per_thousand_by_age={},
        )


def test_entitlement_respects_its_effective_window():
    entitlement = ProductEntitlement(
        product_id="PROD-X",
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 6, 30),
    )
    assert entitlement.is_active_on(date(2026, 3, 1))
    assert not entitlement.is_active_on(date(2025, 12, 31))
    assert not entitlement.is_active_on(date(2026, 7, 1))


# --------------------------------------------------------------------------
# catalog and entitlement
# --------------------------------------------------------------------------


def test_catalog_filters_by_partner_entitlement(engine: DistributionEngine):
    gold = {p.product_id for p in engine.list_catalog(GOLD, state="CA")}
    assert gold == {"DIST-TERM-SIMPLE", "DIST-WHOLE-001", "DIST-ANNUITY-001"}

    starter = {p.product_id for p in engine.list_catalog(STARTER, state="CA")}
    assert starter == {TERM}, "a starter partner must not see a product needing silver"


def test_catalog_filters_by_consumer_jurisdiction(engine: DistributionEngine):
    assert engine.list_catalog(GOLD, state="MT") == []


def test_catalog_excludes_withdrawn_products(engine: DistributionEngine):
    from pas_plugins.plugin5_embedded.models import DistributionProductStatus

    engine.products[TERM].status = DistributionProductStatus.WITHDRAWN
    assert TERM not in {p.product_id for p in engine.list_catalog(GOLD, state="CA")}
    assert TERM in {p.product_id for p in engine.list_catalog(GOLD, state="CA", include_withdrawn=True)}


def test_a_suspended_partner_sees_nothing(engine: DistributionEngine):
    partner = engine.get_partner(STARTER)
    assert partner is not None
    partner.status = PartnerStatus.SUSPENDED
    with pytest.raises(DistributionError, match="may not sell"):
        engine.list_catalog(STARTER, state="CA")


def test_entitlement_face_cap_limits_the_catalog(engine: DistributionEngine):
    """A product whose minimum face exceeds the partner's ceiling cannot be sold."""
    partner = engine.get_partner(GOLD)
    assert partner is not None
    partner.entitlements[1].max_face_amount = 40_000.0
    ids = {p.product_id for p in engine.list_catalog(GOLD, state="CA")}
    assert "DIST-WHOLE-001" not in ids


def test_terminated_partner_cannot_be_reonboarded(engine: DistributionEngine):
    partner = engine.get_partner(STARTER)
    assert partner is not None
    partner.status = PartnerStatus.TERMINATED
    with pytest.raises(DistributionError, match="terminated"):
        engine.register_partner(partner)


# --------------------------------------------------------------------------
# quoting
# --------------------------------------------------------------------------


def test_quote_prices_from_the_age_band(engine: DistributionEngine):
    quote = do_quote(engine)
    assert quote.base_rate_per_thousand == 0.85
    assert quote.annual_premium == pytest.approx(425.0)
    assert quote.monthly_premium == pytest.approx(425.0 / 12.0, abs=0.01)


def test_quote_names_the_partner_commission(engine: DistributionEngine):
    quote = do_quote(engine)
    assert quote.commission_bps == 1400
    assert quote.annual_commission == pytest.approx(425.0 * 0.14, abs=0.01)
    commission_lines = [line for line in quote.line_items if line.kind == "commission"]
    assert commission_lines


def test_older_applicant_prices_higher(engine: DistributionEngine):
    young = do_quote(engine, applicant=applicant(date_of_birth=date(2000, 1, 1)))
    old = do_quote(engine, applicant=applicant(date_of_birth=date(1960, 1, 1)))
    assert old.annual_premium > young.annual_premium


def test_tobacco_loading_is_applied_and_shown(engine: DistributionEngine):
    quote = do_quote(engine, applicant=applicant(smoking_status="current"))
    assert quote.annual_premium > 425.0
    assert any(line.kind == "factor" for line in quote.line_items)


def test_quote_is_itemised_and_reconciles(engine: DistributionEngine):
    """Base plus factor must equal the premium, or the itemisation is decoration."""
    quote = do_quote(engine, applicant=applicant(smoking_status="current"))
    base = next(line for line in quote.line_items if line.kind == "basePremium")
    factor = next(line for line in quote.line_items if line.kind == "factor")
    assert base.amount + factor.amount == pytest.approx(quote.annual_premium, abs=0.01)


def test_quote_carries_risk_disclosures(engine: DistributionEngine):
    quote = do_quote(engine)
    assert quote.disclosed_risks
    assert any("lapses" in risk for risk in quote.disclosed_risks)


def test_whole_life_quote_discloses_surrender_charges(engine: DistributionEngine):
    quote = do_quote(
        engine,
        product_id="DIST-WHOLE-001",
        face_amount=400_000.0,
        term_years=None,
        applicant=applicant(coverage_need=400_000.0, existing_coverage=0.0),
    )
    assert any("surrender" in risk.lower() for risk in quote.disclosed_risks)


def test_unlicensed_partner_cannot_quote(engine: DistributionEngine):
    with pytest.raises(DistributionError, match="not licensed"):
        do_quote(engine, partner_id=STARTER, product_id="DIST-WHOLE-001")


def test_product_not_offered_in_state_is_refused(engine: DistributionEngine):
    with pytest.raises(DistributionError, match="not offered"):
        do_quote(engine, applicant=applicant(state_of_residence="MT"))


def test_face_amount_outside_the_range_is_refused(engine: DistributionEngine):
    with pytest.raises(DistributionError, match="outside the range"):
        do_quote(engine, face_amount=5_000.0)
    with pytest.raises(DistributionError, match="outside the range"):
        do_quote(engine, face_amount=5_000_000.0)


def test_unoffered_term_is_refused(engine: DistributionEngine):
    with pytest.raises(DistributionError, match="not offered"):
        do_quote(engine, term_years=15)


def test_age_outside_issue_range_is_refused(engine: DistributionEngine):
    with pytest.raises(DistributionError, match="outside the issue range"):
        do_quote(engine, applicant=applicant(date_of_birth=date(2015, 1, 1)))


def test_quote_expires_and_stops_being_bindable(engine: DistributionEngine):
    quote = do_quote(engine)
    assert quote.is_bindable
    quote.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert quote.is_expired
    assert not quote.is_bindable


# --------------------------------------------------------------------------
# suitability
# --------------------------------------------------------------------------


def test_proportionate_cover_is_suitable(engine: DistributionEngine):
    quote = do_quote(engine)
    assert quote.suitability is not None
    assert quote.suitability.outcome is SuitabilityOutcome.SUITABLE


def test_materially_over_insured_cover_is_flagged(engine: DistributionEngine):
    """Selling 4x a stated need is a conduct problem even at the right price."""
    quote = do_quote(
        engine, face_amount=500_000.0, applicant=applicant(coverage_need=100_000.0)
    )
    assert quote.suitability is not None
    assert quote.suitability.outcome is SuitabilityOutcome.NOT_SUITABLE
    assert quote.suitability.over_insured_ratio == 5.0


def test_moderately_over_insured_cover_needs_review(engine: DistributionEngine):
    quote = do_quote(
        engine, face_amount=300_000.0, applicant=applicant(coverage_need=100_000.0)
    )
    assert quote.suitability is not None
    assert quote.suitability.outcome is SuitabilityOutcome.NEEDS_REVIEW


def test_no_stated_need_needs_review(engine: DistributionEngine):
    quote = do_quote(engine, applicant=applicant(coverage_need=0.0))
    assert quote.suitability is not None
    assert quote.suitability.outcome is SuitabilityOutcome.NEEDS_REVIEW
    assert "noStatedNeed" in quote.suitability.reasons


def test_whole_life_for_a_consumer_with_no_dependents_needs_review(engine: DistributionEngine):
    quote = do_quote(
        engine,
        product_id="DIST-WHOLE-001",
        face_amount=300_000.0,
        term_years=None,
        applicant=applicant(
            coverage_need=300_000.0, existing_coverage=0.0, dependents=0, annual_income=18_000.0
        ),
    )
    assert quote.suitability is not None
    assert quote.suitability.outcome is SuitabilityOutcome.NEEDS_REVIEW


# --------------------------------------------------------------------------
# screening and binding gates
# --------------------------------------------------------------------------


def test_binding_without_kyc_is_refused(engine: DistributionEngine):
    quote = do_quote(engine)
    with pytest.raises(DistributionError, match="screening must precede binding"):
        engine.bind(quote.quote_id)


def test_binding_after_a_clear_screen_succeeds(engine: DistributionEngine):
    quote = do_quote(engine)
    engine.screen(quote.quote_id, "dl", "DL-1")
    policy = engine.bind(quote.quote_id)
    assert policy.status is PolicyStatus.PENDING_FREE_LOOK
    assert policy.free_look_expires is not None
    assert policy.free_look_expires > date.today()


def test_binding_an_unsuitable_quote_is_refused(engine: DistributionEngine):
    quote = do_quote(engine, applicant=applicant(coverage_need=100_000.0))
    engine.screen(quote.quote_id, "dl", "DL-1")
    with pytest.raises(DistributionError, match="not suitable"):
        engine.bind(quote.quote_id)


def test_binding_an_expired_quote_is_refused(engine: DistributionEngine):
    quote = do_quote(engine)
    engine.screen(quote.quote_id, "dl", "DL-1")
    quote.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(DistributionError, match="expired"):
        engine.bind(quote.quote_id)


def test_binding_twice_is_refused(engine: DistributionEngine):
    quote = do_quote(engine)
    engine.screen(quote.quote_id, "dl", "DL-1")
    engine.bind(quote.quote_id)
    with pytest.raises(DistributionError, match="cannot be bound"):
        engine.bind(quote.quote_id)


def test_kyc_result_is_deterministic_per_quote(engine: DistributionEngine):
    """Re-screening the same quote must reach the same verdict.

    Only the wall-clock stamp may differ; the decision itself must not, or the
    same consumer would clear on one call and land in review on the next.
    """
    first = do_quote(engine)
    second = do_quote(engine)
    a = engine.screen(first.quote_id, "dl", "DL-1").to_dict()
    b = engine.screen(first.quote_id, "dl", "DL-1").to_dict()
    for payload in (a, b):
        payload.pop("screenedAt")
    assert a == b
    assert engine.screen(second.quote_id, "dl", "DL-1") is not None


def test_a_review_screening_blocks_binding(engine: DistributionEngine):
    """A screening that needs review must stop the bind, not warn about it."""
    quote = do_quote(engine, applicant=applicant(last_name="Case22"))
    result = engine.screen(quote.quote_id, "dl", "DL-1")
    assert result.status is KycStatus.REVIEW
    assert result.requires_manual_review
    with pytest.raises(DistributionError, match="does not permit binding"):
        engine.bind(quote.quote_id)


def test_quota_is_enforced(engine: DistributionEngine):
    partner = engine.get_partner(STARTER)
    assert partner is not None
    partner.annual_quota_policies = 1
    to_bound_policy(engine, partner_id=STARTER)
    with pytest.raises(DistributionError, match="annual quota"):
        to_bound_policy(engine, partner_id=STARTER)


def test_commission_is_not_payable_inside_free_look(engine: DistributionEngine):
    policy = to_bound_policy(engine)
    assert policy.in_free_look
    assert not policy.commission_payable


def test_commission_becomes_payable_once_free_look_clears(engine: DistributionEngine):
    policy = to_bound_policy(engine)
    policy.free_look_expires = date.today() - timedelta(days=1)
    assert policy.commission_payable
    engine.activate(policy.policy_id)
    assert policy.status is PolicyStatus.ACTIVE


def test_cancelling_inside_free_look_claws_back_commission(engine: DistributionEngine):
    policy = to_bound_policy(engine)
    engine.cancel(policy.policy_id, "consumer changed their mind")
    assert policy.status is PolicyStatus.CANCELLED
    statement = engine.commission_statement(
        TENANT, GOLD, date.today() - timedelta(days=1), date.today()
    )
    assert statement.entries[0].status.value == "clawedBack"
    assert statement.gross_commission == 0.0


# --------------------------------------------------------------------------
# payments
# --------------------------------------------------------------------------


def test_payment_requires_a_bound_policy_first(engine: DistributionEngine):
    quote = do_quote(engine)
    with pytest.raises(DistributionError, match="payment follows a bound policy"):
        engine.create_payment_intent(quote.quote_id)


def test_intent_and_payment_are_separate_records(engine: DistributionEngine):
    policy = to_bound_policy(engine)
    intent = engine.create_payment_intent(policy.quote_id, PaymentMethod.ACH)
    assert intent.status.value == "requiresAction"
    assert engine.get_payment(intent.intent_id) is None

    payment = engine.capture_payment(intent.intent_id)
    assert payment.status.is_settled
    assert payment.net_amount == pytest.approx(intent.amount)


def test_a_failed_capture_is_recorded_as_an_attempt(engine: DistributionEngine):
    policy = to_bound_policy(engine)
    intent = engine.create_payment_intent(policy.quote_id)
    with pytest.raises(DistributionError, match="failed"):
        engine.capture_payment(intent.intent_id, success=False)
    assert engine.get_intent(intent.intent_id).attempts == 1


def test_capturing_more_than_the_intent_is_refused(engine: DistributionEngine):
    policy = to_bound_policy(engine)
    intent = engine.create_payment_intent(policy.quote_id)
    with pytest.raises(DistributionError, match="exceeds the intent"):
        engine.capture_payment(intent.intent_id, amount=intent.amount * 2)


def test_reconciliation_shows_outstanding_premium(engine: DistributionEngine):
    engine.screen(do_quote(engine).quote_id, "dl", "DL-1")
    quote = do_quote(engine)
    engine.screen(quote.quote_id, "dl", "DL-2")
    engine.bind(quote.quote_id)
    rows = {r.quote_id: r for r in engine.reconcile(GOLD)}
    assert rows[quote.quote_id].outstanding == pytest.approx(quote.annual_premium)
    assert rows[quote.quote_id].state == "bound"


# --------------------------------------------------------------------------
# commission
# --------------------------------------------------------------------------


def test_statement_accrues_rather_than_pays_inside_free_look(engine: DistributionEngine):
    policy = to_bound_policy(engine)
    statement = engine.commission_statement(
        TENANT, GOLD, date.today().replace(day=1), date.today()
    )
    assert statement.gross_commission == pytest.approx(policy.annual_commission())
    assert statement.payable_now == 0.0
    assert statement.pending == pytest.approx(policy.annual_commission())


def test_statement_pays_once_the_period_has_settled(engine: DistributionEngine):
    policy = to_bound_policy(engine)
    policy.free_look_expires = date.today() - timedelta(days=1)
    engine.activate(policy.policy_id)
    statement = engine.commission_statement(
        TENANT, GOLD, date.today().replace(day=1), date.today()
    )
    assert statement.payable_now == pytest.approx(policy.annual_commission())


def test_statement_splits_by_product(engine: DistributionEngine):
    to_bound_policy(engine, product_id=TERM)
    statement = engine.commission_statement(
        TENANT, GOLD, date.today().replace(day=1), date.today()
    )
    assert TERM in statement.by_product()


def test_statement_respects_the_partner_tier_settlement_window(engine: DistributionEngine):
    to_bound_policy(engine)
    statement = engine.commission_statement(
        TENANT, GOLD, date.today().replace(day=1), date.today()
    )
    expected = date.today() + timedelta(days=PartnerTier.GOLD.settlement_days)
    assert statement.settlement_due == expected


def test_statement_for_an_unknown_partner_raises(engine: DistributionEngine):
    with pytest.raises(DistributionError, match="unknown partner"):
        engine.commission_statement(TENANT, "PARTNER-NONE", date.today(), date.today())


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------


def test_the_flow_emits_an_event_per_stage(engine: DistributionEngine):
    policy = to_bound_policy(engine)
    intent = engine.create_payment_intent(policy.quote_id)
    engine.capture_payment(intent.intent_id)
    types = [event.event_type for event in engine.events(GOLD)]
    for expected in (
        "quote.issued",
        "kyc.screened",
        "policy.bound",
        "payment.intent_created",
        "payment.captured",
    ):
        assert expected in types


def test_events_can_be_marked_delivered(engine: DistributionEngine):
    do_quote(engine)
    event = engine.events(GOLD)[0]
    delivered = engine.deliver(event.event_id)
    assert delivered.delivered
    assert delivered.delivered_at is not None


# --------------------------------------------------------------------------
# MCP surface
# --------------------------------------------------------------------------


def test_registry_exposes_fourteen_tools(engine: DistributionEngine):
    registry = build_registry(engine)
    assert len(registry.tool_names()) == 14
    assert "distribution_bind_policy" in registry.tool_names()


def test_binding_is_destructive_and_quoting_is_not(engine: DistributionEngine):
    registry = build_registry(engine)
    assert registry.get_tool("distribution_bind_policy").annotations.destructive is True
    assert registry.get_tool("distribution_quote").annotations.read_only is True


def test_screening_is_read_only_but_scope_protected(engine: DistributionEngine):
    spec = build_registry(engine).get_tool("distribution_screen_kyc")
    assert spec.annotations.read_only is True
    assert "distribution:kyc" in spec.required_scopes


def test_mcp_flow_reaches_a_bound_policy(engine: DistributionEngine):
    registry = build_registry(engine)
    applicant_args = {
        "partnerId": GOLD,
        "productId": TERM,
        "faceAmount": 500_000,
        "termYears": 20,
        "applicant": applicant().to_dict(),
    }
    quote = call(registry.get_tool("distribution_quote").handler, applicant_args)
    assert quote["quote"]["annualPremium"] == 425.0
    quote_id = quote["quote"]["quoteId"]

    screened = call(
        registry.get_tool("distribution_screen_kyc").handler,
        {"quoteId": quote_id, "documentType": "dl", "documentReference": "DL-1"},
    )
    assert "kyc" in screened

    bound = call(
        registry.get_tool("distribution_bind_policy").handler,
        {"quoteId": quote_id, "confirm": True},
    )
    assert bound["policy"]["status"] == "pendingFreeLook"


def test_mcp_quote_reports_a_licence_refusal_rather_than_raising(engine: DistributionEngine):
    registry = build_registry(engine)
    result = call(
        registry.get_tool("distribution_quote").handler,
        {
            "partnerId": STARTER,
            "productId": "DIST-WHOLE-001",
            "faceAmount": 500_000,
            "applicant": applicant().to_dict(),
        },
    )
    assert result["error"] == "cannotQuote"
    assert "not licensed" in result["detail"]


def test_mcp_suitability_tool_is_available_without_a_quote(engine: DistributionEngine):
    registry = build_registry(engine)
    result = call(
        registry.get_tool("distribution_assess_suitability").handler,
        {
            "productId": TERM,
            "faceAmount": 500_000,
            "applicant": applicant(coverage_need=100_000).to_dict(),
        },
    )
    assert result["suitability"]["outcome"] == "notSuitable"


def test_mcp_catalog_respects_entitlement(engine: DistributionEngine):
    registry = build_registry(engine)
    result = call(
        registry.get_tool("distribution_list_catalog").handler,
        {"partnerId": STARTER, "state": "CA"},
    )
    assert [p["productId"] for p in result["products"]] == [TERM]


def test_mcp_capabilities_state_the_gates(engine: DistributionEngine):
    registry = build_registry(engine)
    result = call(registry.get_tool("distribution_get_capabilities").handler, {})
    assert result["gates"] == ["suitability", "kyc", "bind", "pay", "commission"]


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------


def test_health_and_meta(client: TestClient):
    assert client.get("/health").json()["status"] == "healthy"
    meta = client.get("/distribution/meta").json()
    assert meta["requireKycBeforeBind"] is True
    assert meta["gates"][0] == "suitability"


def test_catalog_endpoint_filters_by_entitlement(client: TestClient):
    body = client.get("/distribution/catalog", params={"partnerId": GOLD, "state": "CA"}).json()
    assert body["count"] == 3
    assert client.get("/distribution/catalog", params={"partnerId": GOLD, "state": "MT"}).json()["count"] == 0


def test_partner_endpoint(client: TestClient):
    body = client.get(f"/distribution/partners/{GOLD}").json()["partner"]
    assert body["tier"] == "gold"
    assert body["entitlements"]
    assert client.get("/distribution/partners/PARTNER-NONE").status_code == 404


def test_quote_endpoint(client: TestClient):
    response = client.post(
        "/distribution/quotes",
        json={
            "partnerId": GOLD,
            "productId": TERM,
            "applicant": applicant().to_dict(),
            "faceAmount": 500_000,
            "termYears": 20,
        },
    )
    assert response.status_code == 200
    quote = response.json()["quote"]
    assert quote["annualPremium"] == 425.0
    assert quote["commissionBps"] == 1400


def test_quote_endpoint_refuses_an_unlicensed_partner_with_403(client: TestClient):
    """A licensing refusal is a permission problem, not a malformed request."""
    response = client.post(
        "/distribution/quotes",
        json={
            "partnerId": STARTER,
            "productId": "DIST-WHOLE-001",
            "applicant": applicant().to_dict(),
            "faceAmount": 500_000,
        },
    )
    assert response.status_code == 403


def test_lead_capture_endpoint(client: TestClient):
    response = client.post(
        "/distribution/leads", json={"partnerId": GOLD, "applicant": applicant().to_dict()}
    )
    assert response.status_code == 201
    assert response.json()["lead"]["partnerId"] == GOLD


def test_binding_and_payment_endpoints(client: TestClient):
    quote = client.post(
        "/distribution/quotes",
        json={
            "partnerId": GOLD,
            "productId": TERM,
            "applicant": applicant().to_dict(),
            "faceAmount": 500_000,
            "termYears": 20,
        },
    ).json()["quote"]
    quote_id = quote["quoteId"]

    assert client.post(f"/distribution/quotes/{quote_id}/bind").status_code == 422
    assert client.post(f"/distribution/quotes/{quote_id}/kyc", json={}).status_code == 200

    bound = client.post(f"/distribution/quotes/{quote_id}/bind")
    assert bound.status_code == 201
    policy = bound.json()["policy"]
    assert policy["commissionPayable"] is False

    intent = client.post(f"/distribution/quotes/{quote_id}/payment-intent", json={"method": "ach"})
    assert intent.status_code == 201
    captured = client.post(
        f"/distribution/intents/{intent.json()['intent']['intentId']}/capture", json={"success": True}
    )
    assert captured.status_code == 201
    assert client.get(f"/distribution/policies/{policy['policyId']}").status_code == 200


def test_commission_statement_endpoint(client: TestClient):
    quote = client.post(
        "/distribution/quotes",
        json={
            "partnerId": GOLD,
            "productId": TERM,
            "applicant": applicant().to_dict(),
            "faceAmount": 500_000,
            "termYears": 20,
        },
    ).json()["quote"]
    client.post(f"/distribution/quotes/{quote['quoteId']}/kyc", json={})
    client.post(f"/distribution/quotes/{quote['quoteId']}/bind")

    today = date.today()
    response = client.get(
        "/distribution/commissions/statement",
        params={
            "partnerId": GOLD,
            "periodStart": str(today.replace(day=1)),
            "periodEnd": str(today),
        },
    )
    assert response.status_code == 200
    statement = response.json()["statement"]
    assert statement["grossCommission"] > 0
    assert statement["payableNow"] == 0.0


def test_reconciliation_and_events_endpoints(client: TestClient):
    client.post(
        "/distribution/quotes",
        json={
            "partnerId": GOLD,
            "productId": TERM,
            "applicant": applicant().to_dict(),
            "faceAmount": 500_000,
            "termYears": 20,
        },
    ).json()["quote"]
    reconciliation = client.get("/distribution/reconciliation", params={"partnerId": GOLD}).json()
    assert reconciliation["rowCount"] >= 1
    assert client.get("/distribution/events", params={"partnerId": GOLD}).json()["count"] >= 1


def test_openapi_documents_the_distribution_surface(client: TestClient):
    spec = client.get("/openapi.json").json()
    paths = [p for p in spec["paths"] if p.startswith("/distribution")]
    assert len(paths) >= 12
    assert spec["openapi"].startswith("3.1")


def test_clear_kyc_example_is_consistent():
    from pas_plugins.plugin5_embedded.examples import clear_kyc

    result = clear_kyc()
    assert isinstance(result, KycResult)
    assert result.status.permits_bind
