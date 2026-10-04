"""Tests for Plugin 4 - Low-Code Product Configuration."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from pas_core.tenancy import RequestPrincipal, Tenant, build_context

from pas_plugins.plugin4_productconfig.compliance import ComplianceEngine, GuardrailEngine
from pas_plugins.plugin4_productconfig.diffing import diff_definitions
from pas_plugins.plugin4_productconfig.examples import demo_definition, draft_with_low_rates
from pas_plugins.plugin4_productconfig.mcp_server import build_registry
from pas_plugins.plugin4_productconfig.models import (
    AgeBand,
    Charge,
    ChargeFrequency,
    ComplianceRule,
    Cover,
    EligibilityRule,
    ProductDefinition,
    QuoteRequest,
    RateBand,
    RatingTable,
    RuleSeverity,
    SimulationProfile,
)
from pas_plugins.plugin4_productconfig.rating import RatingEngine, RatingError
from pas_plugins.plugin4_productconfig.service import (
    ProductConfigError,
    ProductConfigService,
)

TENANT = "demo-carrier"
PRODUCT = "PROD-TERM20"
COVER = "cv-term-20"


def _live_service(client: TestClient) -> ProductConfigService:
    """The service instance the app is currently wired to."""
    from pas_plugins.plugin4_productconfig import main

    live = main.platform.extra.get("product_service")
    assert live is not None
    return live


@pytest.fixture
def service() -> ProductConfigService:
    return ProductConfigService()


def quote_request(**overrides: object) -> QuoteRequest:
    payload: dict[str, object] = {
        "tenant_id": TENANT,
        "product_id": PRODUCT,
        "cover_id": COVER,
        "age": 35,
        "face_amount": 500_000.0,
        "term_years": 20,
        "state": "CA",
    }
    payload.update(overrides)
    return QuoteRequest.model_validate(payload)


def ctx(tenant: str = TENANT) -> object:
    return build_context(
        Tenant(tenant_id=tenant, legal_name="Test"),
        RequestPrincipal(subject="pm.tester", tenant_id=tenant, scopes=frozenset({"*"})),
    )


def call(handler, arguments: dict) -> dict:
    return asyncio.run(handler(arguments, ctx()))


@pytest.fixture
def client():
    from pas_plugins.plugin4_productconfig.main import app

    from pas_plugins.plugin4_productconfig import main

    with TestClient(app) as test_client:
        # Give each test an isolated service. The module-level instance is a
        # process singleton, correct in production and wrong in tests: without
        # this reset, a product published in one test stays published in the next.
        main.platform.extra["product_service"] = ProductConfigService()
        yield test_client


# --------------------------------------------------------------------------
# model integrity
# --------------------------------------------------------------------------


def test_definition_round_trips_through_its_wire_form():
    """to_dict() output must revalidate. The API and MCP round-trip on it."""
    definition = demo_definition()
    reparsed = ProductDefinition.model_validate(definition.to_dict())
    assert reparsed.to_dict() == definition.to_dict()


def test_low_rate_variant_also_round_trips():
    definition = draft_with_low_rates()
    assert ProductDefinition.model_validate(definition.to_dict()).to_dict() == definition.to_dict()


def test_age_bands_are_inclusive_lower_exclusive_upper():
    band = AgeBand(min_age=30, max_age=45)
    assert band.contains(30)
    assert not band.contains(45)


def test_open_ended_age_band_contains_everything_above():
    band = AgeBand(min_age=60, max_age=None)
    assert band.is_open
    assert band.contains(60)
    assert band.contains(120)


def test_empty_age_band_is_rejected():
    with pytest.raises(ValueError, match="empty"):
        AgeBand(min_age=40, max_age=40)


def test_percentage_benefit_requires_an_amount():
    from pas_plugins.plugin4_productconfig.models import Benefit, BenefitCategory

    with pytest.raises(ValueError, match="no amount"):
        Benefit(
            benefit_id="b1",
            name="Waiver",
            category=BenefitCategory.RIDERS,
            trigger="Disability.",
            amount_mode="percentOfFace",
        )


def test_negative_charge_is_rejected():
    """A payout is a benefit. A negative charge is a bug or a workaround."""
    with pytest.raises(ValueError, match="negative amount"):
        Charge(charge_id="c1", name="Bad", amount=-5.0)


def test_cover_rejects_an_inverted_face_amount_range():
    with pytest.raises(ValueError, match="max face amount"):
        Cover(
            cover_id="c1",
            name="Broken",
            min_face_amount=1_000_000.0,
            max_face_amount=500_000.0,
            rating_table_id="t1",
        )


def test_cover_rejects_duplicate_term_options():
    with pytest.raises(ValueError, match="repeats a term"):
        Cover(
            cover_id="c1",
            name="Broken",
            min_face_amount=1_000.0,
            max_face_amount=100_000.0,
            rating_table_id="t1",
            term_options_years=[20, 20],
        )


def test_between_rule_needs_a_ordered_pair():
    with pytest.raises(ValueError, match="two-element"):
        EligibilityRule(
            rule_id="r1",
            name="Range",
            field="age",
            operator="between",
            value=40,
            reason_code="RANGE_TOO_LONG",
            message="A range check needs two bounds.",
        )
    with pytest.raises(ValueError, match="backwards"):
        EligibilityRule(
            rule_id="r2",
            name="Range",
            field="age",
            operator="between",
            value=[60, 30],
            reason_code="RANGE_REVERSED",
            message="A range must run low to high.",
        )


def test_set_operator_requires_a_list():
    with pytest.raises(ValueError, match="needs a list"):
        EligibilityRule(
            rule_id="r1",
            name="Membership",
            field="state",
            operator="in",
            value="CA",
            reason_code="NOT_A_LIST",
            message="A membership test needs a list.",
        )


def test_cover_must_reference_a_rating_table_the_product_defines():
    with pytest.raises(ValueError, match="does not define"):
        ProductDefinition(
            product_id="P1",
            name="Broken product",
            covers=[
                Cover(
                    cover_id="c1",
                    name="Broken cover",
                    min_face_amount=1_000.0,
                    max_face_amount=1_000_000.0,
                    rating_table_id="missing",
                )
            ],
            rating_tables=[
                RatingTable(table_id="t1", name="T", bands=[RateBand(age_band=AgeBand(min_age=0, max_age=100), rate_per_thousand=1.0)])
            ],
        )


def test_charge_frequency_annualises_correctly():
    assert Charge(charge_id="c", name="C", frequency=ChargeFrequency.MONTHLY, amount=10.0).annualised() == 120.0
    assert Charge(charge_id="c", name="C", frequency=ChargeFrequency.ANNUAL, amount=90.0).annualised() == 90.0
    assert Charge(charge_id="c", name="C", frequency=ChargeFrequency.QUARTERLY, amount=30.0).annualised() == 120.0


# --------------------------------------------------------------------------
# rating engine
# --------------------------------------------------------------------------


def test_base_premium_is_rate_per_thousand_plus_policy_fee():
    definition = demo_definition()
    quote = RatingEngine().quote(
        definition.covers[0],
        definition.rating_tables[0],
        quote_request(face_amount=500_000.0),
    )
    # 500 / 1000 x 1.15 + 90 policy fee
    assert quote.annual_premium == pytest.approx(665.0)
    assert quote.rating_band == "30-45"


def test_monthly_is_one_twelfth_of_annual():
    definition = demo_definition()
    quote = RatingEngine().quote(
        definition.covers[0], definition.rating_tables[0], quote_request()
    )
    assert quote.monthly_premium == pytest.approx(quote.annual_premium / 12.0)


def test_older_ages_price_higher():
    definition = demo_definition()
    engine = RatingEngine()
    young = engine.quote(definition.covers[0], definition.rating_tables[0], quote_request(age=25))
    old = engine.quote(definition.covers[0], definition.rating_tables[0], quote_request(age=65))
    assert old.annual_premium > young.annual_premium


def test_multiplicative_factor_is_itemised_and_applied():
    definition = demo_definition()
    quote = RatingEngine().quote(
        definition.covers[0],
        definition.rating_tables[0],
        quote_request(risk_factors={"tobacco": 1.0}),
    )
    assert quote.annual_premium == pytest.approx(665.0 * 1.75)
    adjustments = [line for line in quote.lines if line.kind == "factorAdjustment"]
    assert adjustments
    assert any("Tobacco" in line.label for line in adjustments)


def test_factors_compose_and_each_line_reports_its_own_delta():
    """Two multipliers must both be reflected, and the itemisation must add up."""
    definition = demo_definition()
    quote = RatingEngine().quote(
        definition.covers[0],
        definition.rating_tables[0],
        quote_request(risk_factors={"tobacco": 1.0, "preferred": 1.0}),
    )
    assert quote.annual_premium == pytest.approx(665.0 * 1.75 * 0.90)
    base = next(line for line in quote.lines if line.kind == "basePremium")
    factors = [line for line in quote.lines if line.kind == "factorAdjustment"]
    charges = [line for line in quote.lines if line.kind == "charge"]
    assert base.amount + sum(f.amount for f in factors) == pytest.approx(quote.annual_premium)
    assert sum(c.amount for c in charges) == pytest.approx(quote.annual_charges)


def test_basis_point_factor_applies_after_multipliers():
    definition = demo_definition()
    quote = RatingEngine().quote(
        definition.covers[0],
        definition.rating_tables[0],
        quote_request(risk_factors={"residentFee": 1.0}),
    )
    assert quote.annual_premium == pytest.approx(665.0 * 0.985)


def test_unknown_factor_is_reported_and_does_not_change_the_price():
    definition = demo_definition()
    quote = RatingEngine().quote(
        definition.covers[0],
        definition.rating_tables[0],
        quote_request(risk_factors={"notARealFactor": 2.0}),
    )
    assert quote.annual_premium == pytest.approx(665.0)
    assert any("Unknown factor" in line.label for line in quote.lines)


def test_out_of_range_requests_yield_an_ineligible_decision_not_an_error():
    """An underwriter needs a recordable decision, not a stack trace."""
    definition = demo_definition()
    engine = RatingEngine()

    too_small = engine.quote(
        definition.covers[0], definition.rating_tables[0], quote_request(face_amount=100.0)
    )
    assert too_small.eligibility == "ineligible"
    assert "FACE_BELOW_MINIMUM" in too_small.reason_codes
    assert too_small.annual_premium == 0.0

    too_large = engine.quote(
        definition.covers[0], definition.rating_tables[0], quote_request(face_amount=50_000_000.0)
    )
    assert "FACE_ABOVE_MAXIMUM" in too_large.reason_codes

    wrong_term = engine.quote(
        definition.covers[0], definition.rating_tables[0], quote_request(term_years=15)
    )
    assert "TERM_NOT_OFFERED" in wrong_term.reason_codes

    too_old = engine.quote(
        definition.covers[0], definition.rating_tables[0], quote_request(age=85)
    )
    assert "ABOVE_ISSUE_AGE" in too_old.reason_codes


def test_missing_band_is_refused_rather_than_defaulted():
    """A gap in the bands must fail loudly. A silent fallback prices the wrong risk."""
    table = RatingTable(
        table_id="t",
        name="Sparse",
        bands=[RateBand(age_band=AgeBand(min_age=0, max_age=30), rate_per_thousand=1.0)],
    )
    cover = Cover(
        cover_id="c",
        name="C",
        min_face_amount=1_000.0,
        max_face_amount=1_000_000.0,
        rating_table_id="t",
    )
    with pytest.raises(RatingError, match="no band for age"):
        RatingEngine().quote(cover, table, quote_request(age=50))


def test_eligibility_rule_on_the_failing_condition():
    """'Under 18' is `lt 18`. A rule written the other way fires on every applicant."""
    definition = demo_definition()
    engine = RatingEngine()
    adult = engine.quote(definition.covers[0], definition.rating_tables[0], quote_request(age=35, state="CA"))
    minor = engine.quote(definition.covers[0], definition.rating_tables[0], quote_request(age=16, state="CA"))
    unlicensed = engine.quote(definition.covers[0], definition.rating_tables[0], quote_request(age=35, state="MT"))

    assert adult.eligibility == "eligible"
    assert adult.reason_codes == []
    assert minor.eligibility == "ineligible"
    assert "BELOW_ISSUE_AGE" in minor.reason_codes
    assert unlicensed.eligibility == "ineligible"
    assert "STATE_NOT_LICENSED" in unlicensed.reason_codes


def test_a_refer_rule_alone_yields_refer_not_ineligible():
    definition = demo_definition()
    quote = RatingEngine().quote(
        definition.covers[0],
        definition.rating_tables[0],
        quote_request(face_amount=6_000_000.0, age=40),
    )
    assert quote.eligibility == "refer"
    assert "LARGE_FACE_REVIEW" in quote.reason_codes
    # A referred request is still priced; the underwriter needs the number.
    assert quote.annual_premium > 0


def test_rule_referencing_an_unresolvable_field_does_not_fire():
    """A misconfigured rule must not silently make a case ineligible."""
    cover = Cover(
        cover_id="c",
        name="C",
        min_face_amount=1_000.0,
        max_face_amount=1_000_000.0,
        rating_table_id="t",
        eligibility=[
            EligibilityRule(
                rule_id="r",
                name="Bogus",
                field="applicant.maritalStatus",
                operator="eq",
                value="M",
                reason_code="BOGUS",
                message="m",
            )
        ],
    )
    table = RatingTable(
        table_id="t",
        name="T",
        bands=[RateBand(age_band=AgeBand(min_age=0, max_age=120), rate_per_thousand=1.0)],
    )
    quote = RatingEngine().quote(cover, table, quote_request())
    assert quote.eligibility == "eligible"


# --------------------------------------------------------------------------
# guardrails
# --------------------------------------------------------------------------


def test_demo_definition_passes_guardrails():
    report = GuardrailEngine().evaluate(demo_definition())
    assert report.can_publish
    assert not report.blockers


def test_overlapping_age_bands_block():
    definition = demo_definition()
    definition.rating_tables[0].bands[1].age_band.min_age = 25
    report = GuardrailEngine().evaluate(definition)
    assert any(v.rule_id == "GR-102" for v in report.blockers)


def test_a_gap_between_age_bands_blocks():
    definition = demo_definition()
    definition.rating_tables[0].bands[1].age_band.min_age = 32
    report = GuardrailEngine().evaluate(definition)
    assert any(v.rule_id == "GR-103" for v in report.blockers)
    gap = next(v for v in report.blockers if v.rule_id == "GR-103")
    assert "30" in gap.message and "32" in gap.message


def test_open_ended_band_must_come_last():
    definition = demo_definition()
    definition.rating_tables[0].bands[0].age_band.max_age = None
    report = GuardrailEngine().evaluate(definition)
    assert any(v.rule_id == "GR-101" for v in report.blockers)


def test_cross_reference_to_an_unknown_factor_blocks():
    definition = demo_definition()
    definition.rating_tables[0].cross_reference["ghostClass"] = "noSuchFactor"
    report = GuardrailEngine().evaluate(definition)
    assert any(v.rule_id == "GR-105" for v in report.blockers)


def test_negative_margin_blocks():
    report = GuardrailEngine().evaluate(draft_with_low_rates())
    assert any(v.rule_id == "GR-501" for v in report.blockers)
    assert not report.can_publish


def test_negative_margin_can_be_allowed_but_is_still_reported():
    engine = GuardrailEngine(allow_negative_margin=True)
    report = engine.evaluate(draft_with_low_rates())
    assert report.can_publish


def test_cover_without_benefits_blocks():
    definition = demo_definition()
    definition.covers[0].benefits = []
    report = GuardrailEngine().evaluate(definition)
    assert any(v.rule_id == "GR-401" for v in report.blockers)


def test_cover_without_charges_warns():
    definition = demo_definition()
    definition.covers[0].charges = []
    report = GuardrailEngine().evaluate(definition)
    assert any(v.rule_id == "GR-403" for v in report.warnings)


def test_eligibility_rule_on_an_unknown_field_blocks():
    """It can never fire, so the product would quietly mis-price. That is a blocker."""
    definition = demo_definition()
    definition.covers[0].eligibility.append(
        EligibilityRule(
            rule_id="elig-bogus",
            name="Bogus",
            field="applicant.maritalStatus",
            operator="eq",
            value="M",
            reason_code="BOGUS",
            message="m",
        )
    )
    report = GuardrailEngine().evaluate(definition)
    assert any(v.rule_id == "GR-302" for v in report.blockers)


def test_duplicate_reason_codes_warn():
    definition = demo_definition()
    duplicate = definition.covers[0].eligibility[0].model_copy(update={"rule_id": "elig-dup"})
    definition.covers[0].eligibility.append(duplicate)
    report = GuardrailEngine().evaluate(definition)
    assert any(v.rule_id == "GR-301" for v in report.warnings)


def test_issue_age_range_exceeding_the_table_blocks():
    definition = demo_definition()
    definition.covers[0].max_issue_age = 85
    report = GuardrailEngine().evaluate(definition)
    assert any(v.rule_id == "GR-202" for v in report.blockers)


def test_product_with_no_states_blocks():
    definition = demo_definition()
    definition.states = []
    report = GuardrailEngine().evaluate(definition)
    assert any(v.rule_id == "GR-001" for v in report.blockers)


def test_every_violation_carries_a_remediation():
    """A guardrail that says only 'invalid' makes a product author guess."""
    report = GuardrailEngine().evaluate(draft_with_low_rates())
    assert report.violations
    assert all(v.remediation for v in report.violations)


# --------------------------------------------------------------------------
# compliance
# --------------------------------------------------------------------------


def test_state_scoped_face_cap_blocks_an_over_limit_quote():
    definition = demo_definition()
    violation = ComplianceEngine().check_quote(definition, quote_request(age=64, face_amount=6_000_000.0, state="CA"))
    assert any(v.rule_id == "comp-face-cap" for v in violation)


def test_state_scoped_rule_does_not_apply_elsewhere():
    definition = demo_definition()
    violations = ComplianceEngine().check_quote(
        definition, quote_request(age=64, face_amount=6_000_000.0, state="TX")
    )
    assert not any(v.rule_id == "comp-face-cap" for v in violations)


def test_rate_to_value_violation_is_carried():
    definition = demo_definition()
    violations = ComplianceEngine().check_quote(definition, quote_request(age=64, state="TX"))
    assert any(v.rule_id == "comp-rate-to-value" for v in violations)


def test_unrecognised_compliance_kind_warns_rather_than_passes_silently():
    definition = demo_definition()
    definition.compliance_rules.append(
        ComplianceRule(
            rule_id="comp-weird",
            name="Unsupported",
            kind="somethingElse",
            message="m",
        )
    )
    violations = ComplianceEngine().check_quote(definition, quote_request())
    weak = next(v for v in violations if v.rule_id == "comp-weird")
    assert weak.severity is RuleSeverity.WARNING


def test_compliance_violation_makes_the_quote_ineligible(service: ProductConfigService):
    service.save_draft(demo_definition())
    service.publish(PRODUCT, "pm")
    quote = service.quote(quote_request(age=64, face_amount=6_000_000.0, state="CA"))
    assert quote.eligibility == "ineligible"
    assert "comp-face-cap" in quote.reason_codes


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------


def test_publish_produces_a_content_hash(service: ProductConfigService):
    service.save_draft(demo_definition())
    published = service.publish(PRODUCT, "pm.jane")
    assert published.version == 1
    assert len(published.content_hash) == 32
    assert published.published_by == "pm.jane"


def test_content_hash_covers_rates_not_bookkeeping(service: ProductConfigService):
    """The hash identifies product content, not the moment it was saved."""
    service.save_draft(demo_definition())
    hash_a = service.publish(PRODUCT, "pm").content_hash

    revised = demo_definition()
    revised.rating_tables[0].bands[0].rate_per_thousand = 0.95
    service.save_draft(revised)
    hash_b = service.publish(PRODUCT, "pm").content_hash
    assert hash_a != hash_b


def test_publishing_unchanged_content_is_refused(service: ProductConfigService):
    """A version that changes nothing is noise in the audit trail."""
    service.save_draft(demo_definition())
    service.publish(PRODUCT, "pm")
    service.save_draft(demo_definition())
    with pytest.raises(ProductConfigError, match="identical content"):
        service.publish(PRODUCT, "pm")


def test_publish_blocks_while_a_blocker_stands(service: ProductConfigService):
    service.save_draft(draft_with_low_rates())
    with pytest.raises(ProductConfigError, match="blockers stand"):
        service.publish(PRODUCT, "pm")


def test_save_draft_refuses_an_edit_based_on_a_stale_version(service: ProductConfigService):
    """An author must reload before editing, or their change silently drops a rate change."""
    service.save_draft(demo_definition())
    service.publish(PRODUCT, "pm")

    # Someone else publishes version 2 while this author still holds version 1.
    other = demo_definition()
    other.rating_tables[0].bands[0].rate_per_thousand = 0.95
    service._published[PRODUCT][-1].version = 2  # simulate a concurrent publication

    stale = demo_definition()
    stale.rating_tables[0].bands[3].rate_per_thousand = 9.99
    with pytest.raises(ProductConfigError, match="behind published version"):
        service.save_draft(stale)


def test_versions_increment_and_are_listed(service: ProductConfigService):
    service.save_draft(demo_definition())
    service.publish(PRODUCT, "pm")
    revised = demo_definition()
    revised.version = 2
    revised.rating_tables[0].bands[0].rate_per_thousand = 0.95
    service.save_draft(revised)
    service.publish(PRODUCT, "pm")
    assert [v.version for v in service.list_published(PRODUCT)] == [1, 2]


def test_retire_marks_status_but_preserves_content(service: ProductConfigService):
    service.save_draft(demo_definition())
    published = service.publish(PRODUCT, "pm")
    original_hash = published.content_hash
    retired = service.retire(PRODUCT, 1)
    assert retired.status.value == "retired"
    assert retired.content_hash == original_hash


def test_retiring_an_unknown_version_raises(service: ProductConfigService):
    service.save_draft(demo_definition())
    service.publish(PRODUCT, "pm")
    with pytest.raises(ProductConfigError):
        service.retire(PRODUCT, 99)


def test_publishing_without_a_draft_raises(service: ProductConfigService):
    with pytest.raises(ProductConfigError, match="no draft"):
        service.publish("PROD-NOPE", "pm")


def test_quoting_an_unpublished_product_raises(service: ProductConfigService):
    service.save_draft(demo_definition())
    with pytest.raises(ProductConfigError, match="no published version"):
        service.quote(quote_request())


# --------------------------------------------------------------------------
# diffing
# --------------------------------------------------------------------------


def test_rate_change_shows_up_in_the_diff_and_is_material():
    before = demo_definition()
    after = demo_definition()
    after.rating_tables[0].bands[1].rate_per_thousand = 1.35
    diff = diff_definitions(before, after)
    assert diff.is_material
    rate_entries = [e for e in diff.entries if "ratePerThousand" in e.path]
    assert rate_entries
    assert rate_entries[0].before == 1.15
    assert rate_entries[0].after == 1.35


def test_description_only_change_is_not_material():
    """A new version is not automatically a new rate filing."""
    before = demo_definition()
    after = demo_definition()
    after.description = "Reworded."
    diff = diff_definitions(before, after)
    assert not diff.is_material


def test_diff_ignores_the_version_number():
    before = demo_definition()
    after = demo_definition()
    after.version = 7
    assert diff_definitions(before, after).entries == []


def test_diff_reports_added_and_removed_states():
    before = demo_definition()
    after = demo_definition()
    after.states = [*before.states, "WA"]
    diff = diff_definitions(before, after)
    assert any(e.change == "added" and e.path.startswith("states") for e in diff.entries)


def test_diff_matches_list_elements_by_id_not_position():
    before = demo_definition()
    after = demo_definition()
    after.covers[0].benefits = list(reversed(after.covers[0].benefits))
    diff = diff_definitions(before, after)
    # Reordering is not a change to the elements themselves.
    assert not any("benefits.ben-" in e.path and e.change == "added" for e in diff.entries)


def test_diff_reports_a_removed_compliance_rule():
    before = demo_definition()
    after = demo_definition()
    after.compliance_rules = after.compliance_rules[:2]
    diff = diff_definitions(before, after)
    removed = [e for e in diff.entries if e.change == "removed"]
    assert removed
    assert any("comp-rate-to-value" in str(entry.before) for entry in removed)


def test_diff_service_raises_for_an_unknown_version(service: ProductConfigService):
    service.save_draft(demo_definition())
    service.publish(PRODUCT, "pm")
    with pytest.raises(ProductConfigError):
        service.diff(PRODUCT, 1, 2)


# --------------------------------------------------------------------------
# simulation
# --------------------------------------------------------------------------


def test_simulation_reports_economics(service: ProductConfigService):
    result = service.simulate(demo_definition(), SimulationProfile(min_age=20, max_age=65), policies=200)
    assert result.policies == 200
    assert result.average_annual_premium > 0
    assert result.gross_written_premium > 0
    assert result.is_viable


def test_simulation_states_its_benefit_cost_assumption(service: ProductConfigService):
    """A simulation must not look like an actuarial estimate."""
    result = service.simulate(demo_definition(), policies=50)
    assert any("assumed" in note.lower() for note in result.notes)


def test_simulation_is_deterministic_for_a_seed(service: ProductConfigService):
    profile = SimulationProfile(min_age=20, max_age=65)
    first = service.simulate(demo_definition(), profile, policies=100, seed=99).to_dict()
    second = service.simulate(demo_definition(), profile, policies=100, seed=99).to_dict()
    first.pop("simulatedAt")
    second.pop("simulatedAt")
    assert first == second


def test_simulation_flags_an_unpriceable_profile(service: ProductConfigService):
    """Applicants outside the configured range must be counted, not dropped."""
    profile = SimulationProfile(min_age=0, max_age=5)
    result = service.simulate(demo_definition(), profile, policies=60)
    assert result.unpriceable == 60
    assert not result.is_viable


def test_simulation_reports_a_loss_making_product(service: ProductConfigService):
    result = service.simulate(draft_with_low_rates(), SimulationProfile(min_age=20, max_age=60), policies=150)
    assert result.loss_ratio > 1.0
    assert not result.is_viable


# --------------------------------------------------------------------------
# MCP surface
# --------------------------------------------------------------------------


def test_registry_exposes_twelve_tools(service: ProductConfigService):
    registry = build_registry(service)
    assert len(registry.tool_names()) == 12
    assert "products_publish" in registry.tool_names()


def test_publish_tool_is_destructive_and_quoting_is_not():
    registry = build_registry(ProductConfigService())
    assert registry.get_tool("products_publish").annotations.destructive is True
    assert registry.get_tool("products_publish").annotations.read_only is False
    assert registry.get_tool("products_quote").annotations.read_only is True


def test_drafting_needs_no_confirmation_because_it_is_a_scratchpad():
    spec = build_registry(ProductConfigService()).get_tool("products_save_draft")
    assert spec.annotations.destructive is False
    assert spec.annotations.read_only is False


def test_tool_lifecycle_end_to_end(service: ProductConfigService):
    registry = build_registry(service)
    definition = demo_definition()

    saved = call(registry.get_tool("products_save_draft").handler, {"definition": definition.to_dict()})
    assert saved["draft"]["productId"] == PRODUCT

    validated = call(registry.get_tool("products_validate").handler, {"productId": PRODUCT})
    assert validated["report"]["canPublish"] is True

    simulated = call(registry.get_tool("products_simulate").handler, {"productId": PRODUCT, "policies": 60})
    assert simulated["simulation"]["policies"] == 60

    published = call(registry.get_tool("products_publish").handler, {"productId": PRODUCT, "confirm": True})
    assert published["published"]["version"] == 1

    versions = call(registry.get_tool("products_list_versions").handler, {"productId": PRODUCT})
    assert versions["count"] == 1


def test_publish_tool_reports_a_blocker_rather_than_raising(service: ProductConfigService):
    registry = build_registry(service)
    call(
        registry.get_tool("products_save_draft").handler,
        {"definition": draft_with_low_rates().to_dict()},
    )
    result = call(registry.get_tool("products_publish").handler, {"productId": PRODUCT, "confirm": True})
    assert result["error"] == "cannotPublish"
    assert "GR-501" in result["detail"]


def test_unknown_draft_is_reported_cleanly(service: ProductConfigService):
    registry = build_registry(service)
    assert call(registry.get_tool("products_get_draft").handler, {"productId": "PROD-NONE"})[
        "error"
    ] == "draftNotFound"


def test_quote_tool_takes_the_tenant_from_the_context(service: ProductConfigService):
    """A caller-supplied tenantId must not let them price another carrier's product."""
    registry = build_registry(service)
    call(registry.get_tool("products_save_draft").handler, {"definition": demo_definition().to_dict()})
    call(registry.get_tool("products_publish").handler, {"productId": PRODUCT, "confirm": True})
    result = call(
        registry.get_tool("products_quote").handler,
        {
            "productId": PRODUCT,
            "coverId": COVER,
            "age": 35,
            "faceAmount": 500_000,
            "termYears": 20,
            "state": "CA",
            "tenantId": "someone-else",
        },
    )
    assert result["quote"]["tenantId"] == TENANT


def test_resources_use_the_platform_uri_scheme(service: ProductConfigService):
    resources = build_registry(service).list_resources()
    assert all(r["uriTemplate"].startswith("pas://") for r in resources)


def test_example_definition_is_returned_and_valid(service: ProductConfigService):
    registry = build_registry(service)
    result = call(registry.get_tool("products_get_example_definition").handler, {})
    reparsed = ProductDefinition.model_validate(result["definition"])
    assert reparsed.product_id == PRODUCT
    assert len(reparsed.rating_tables[0].bands) == 4


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------


def test_health_and_meta(client: TestClient):
    assert client.get("/health").json()["status"] == "healthy"
    meta = client.get("/products/meta").json()
    assert "eligibilityOperators" in meta
    assert "rateToValue" in meta["complianceKinds"]


def test_save_validate_publish_flow(client: TestClient):
    assert client.post("/products/drafts", json=demo_definition().to_dict()).status_code == 201
    report = client.post(f"/products/drafts/{PRODUCT}/validate", json={}).json()["report"]
    assert report["canPublish"] is True
    published = client.post(f"/products/{PRODUCT}/publish", json={})
    assert published.status_code == 201
    assert published.json()["published"]["contentHash"]


def test_publishing_a_blocked_product_is_a_conflict(client: TestClient):
    client.post("/products/drafts", json=draft_with_low_rates().to_dict())
    response = client.post(f"/products/{PRODUCT}/publish", json={})
    assert response.status_code == 409


def test_unknown_draft_is_404(client: TestClient):
    assert client.get("/products/drafts/PROD-NOPE").status_code == 404


def test_quote_endpoint_ignores_a_caller_supplied_tenant(client: TestClient):
    client.post("/products/drafts", json=demo_definition().to_dict())
    client.post(f"/products/{PRODUCT}/publish", json={})
    response = client.post(
        "/products/quote",
        json={
            "productId": PRODUCT,
            "coverId": COVER,
            "age": 35,
            "faceAmount": 500_000,
            "termYears": 20,
            "state": "CA",
            "tenantId": "someone-else",
        },
    )
    assert response.status_code == 200
    assert response.json()["quote"]["tenantId"] != "someone-else"


def test_out_of_range_quote_returns_an_ineligible_decision(client: TestClient):
    """A face amount below the minimum is a decline of the request, not a server error."""
    client.post("/products/drafts", json=demo_definition().to_dict())
    client.post(f"/products/{PRODUCT}/publish", json={})
    response = client.post(
        "/products/quote",
        json={"productId": PRODUCT, "coverId": COVER, "age": 35, "faceAmount": 10, "termYears": 20, "state": "CA"},
    )
    assert response.status_code == 200
    quote = response.json()["quote"]
    assert quote["eligibility"] == "ineligible"
    assert "FACE_BELOW_MINIMUM" in quote["reasonCodes"]


def test_versions_diff_and_retire_over_http(client: TestClient):
    client.post("/products/drafts", json=demo_definition().to_dict())
    client.post(f"/products/{PRODUCT}/publish", json={})
    revised = demo_definition()
    revised.version = 2
    revised.rating_tables[0].bands[0].rate_per_thousand = 0.95
    client.post("/products/drafts", json=revised.to_dict())
    client.post(f"/products/{PRODUCT}/publish", json={})

    assert client.get(f"/products/{PRODUCT}/versions").json()["count"] == 2
    assert client.get(f"/products/{PRODUCT}/versions/1").status_code == 200
    diff = client.get(f"/products/{PRODUCT}/versions/1/diff/2").json()["diff"]
    assert diff["isMaterial"] is True
    assert client.post(f"/products/{PRODUCT}/versions/1/retire").status_code == 200


def test_price_matrix_reports_unpriceable_cells(client: TestClient):
    service = _live_service(client)
    service.save_draft(demo_definition())
    service.publish(PRODUCT, "pm")
    response = client.get(
        f"/products/{PRODUCT}/covers/{COVER}/price",
        params={"ages": 3, "minFace": 100_000, "maxFace": 1_000_000},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["rows"]
    assert any(cell.get("error") or cell.get("annualPremium") for row in body["rows"] for cell in row["cells"])


def test_simulation_endpoint(client: TestClient):
    service = _live_service(client)
    service.save_draft(demo_definition())
    response = client.post(f"/products/drafts/{PRODUCT}/simulate", json={"policies": 100})
    assert response.status_code == 200
    assert response.json()["simulation"]["policies"] == 100


def test_openapi_documents_the_product_surface(client: TestClient):
    spec = client.get("/openapi.json").json()
    paths = [p for p in spec["paths"] if p.startswith("/products")]
    assert len(paths) >= 10
    assert spec["openapi"].startswith("3.1")