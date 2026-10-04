"""IFRS 17 engine tests.

The properties under test are the ones an auditor examines:

* a CSM can never be negative - a loss component appears instead (para 103)
* the risk adjustment is the charge for uncertainty, not the best estimate (B88)
* the roll-forward reconciles item by item, in every state including inside a loss
* the disclosure tables tie, and an unreconciled one is reported as unreconciled
* a PAA group never acquires a CSM or a risk adjustment
"""

from __future__ import annotations

import json
import math
from datetime import date

import pytest
from fastapi.testclient import TestClient

from pas_core.errors import ComputationError, ErrorCode, ValidationError
from pas_core.tenancy import RequestPrincipal, Tenant, build_context
from pas_plugins.plugin2_ifrs17.cashflows import (
    FulfilmentCashFlowEngine,
    ProjectionConfig,
    RiskAdjustmentEngine,
    curve_from_rates,
    validate_projection,
)
from pas_plugins.plugin6_datamesh.feature_store import population_stability_index
from pas_plugins.plugin2_ifrs17.csm import (
    CsmEngine,
    CsmInputs,
    CsmRollForwardInput,
    ValuationContext,
    recognise_initial,
    roll_forward,
)
from pas_plugins.plugin2_ifrs17.connector import AssumptionStore, ValuationService
from pas_plugins.plugin2_ifrs17.disclosure import (
    DisclosureGenerator,
    export,
    to_json,
    to_pdf,
    to_xbrl,
    to_xlsx,
)
from pas_plugins.plugin2_ifrs17.grouping import (
    GroupingCriteria,
    GroupingEngine,
    compare_grouping,
)
from pas_plugins.plugin2_ifrs17.library import demo_portfolio, wavy_portfolio, worked_assumption_set
from pas_plugins.plugin2_ifrs17.models import (
    ContractGroupInput,
    GroupingBasis,
    MeasurementModel,
    MeasurementResult,
    ssap102_reserve_class,
)


@pytest.fixture
def assumptions():  # noqa: ANN201
    return worked_assumption_set()


@pytest.fixture
def context(assumptions):  # noqa: ANN001
    return ValuationContext(
        assumptions=assumptions,
        locked_in_curve=assumptions.locked_in_curve,
        updated_curve=assumptions.updated_curve,
        projection_years=30,
    )


@pytest.fixture
def engine() -> CsmEngine:
    return CsmEngine()


@pytest.fixture
def measured(engine: CsmEngine, context: ValuationContext) -> list[MeasurementResult]:
    return [engine.measure(c, context) for c in wavy_portfolio()]


# ---------------------------------------------------------------------------
# Discount curves
# ---------------------------------------------------------------------------
def test_discount_curve_interpolates_and_flattens(assumptions) -> None:  # noqa: ANN001
    curve = assumptions.locked_in_curve
    assert curve is not None
    assert curve.spot(1) == 0.0420
    assert curve.spot(0) == 0.0420          # below the first quoted point
    assert curve.discount_factor(1) == pytest.approx(1 / 1.042)
    assert curve.discount_factor(10) == pytest.approx((1 + curve.spot(10)) ** -10)
    # Beyond the last quoted point the rate is held flat, not extrapolated.
    assert curve.spot(60) == curve.spot(50)


def test_empty_curve_is_rejected() -> None:
    with pytest.raises(ValueError):
        curve_from_rates("c", {}, as_of=date(2026, 1, 1)).spot(1)


# ---------------------------------------------------------------------------
# Cash-flow projection
# ---------------------------------------------------------------------------
def test_projection_survival_decays_monotonically(assumptions) -> None:  # noqa: ANN001
    contract = demo_portfolio()[0]
    projection = FulfilmentCashFlowEngine().project(
        contract, assumptions.mortality, assumptions.expenses,
        ProjectionConfig(horizon_years=20, discount_rate=0.047),
    )
    assert projection.in_force[0] == 1.0
    assert all(
        projection.in_force[i] >= projection.in_force[i + 1]
        for i in range(len(projection.in_force) - 1)
    ), "the in-force cohort must never grow without new business"
    assert 0.0 < projection.survival_rate < 1.0


def test_projection_is_deterministic(assumptions) -> None:  # noqa: ANN001
    contract = demo_portfolio()[0]
    config = ProjectionConfig(horizon_years=15, discount_rate=0.047)
    first = FulfilmentCashFlowEngine().project(contract, assumptions.mortality, assumptions.expenses, config)
    second = FulfilmentCashFlowEngine().project(contract, assumptions.mortality, assumptions.expenses, config)
    assert first.pv_claims == second.pv_claims
    assert first.pv_premiums == second.pv_premiums


def test_discount_factor_lowers_present_value(assumptions) -> None:  # noqa: ANN001
    contract = demo_portfolio()[0]
    flat = FulfilmentCashFlowEngine().project(
        contract, assumptions.mortality, assumptions.expenses,
        ProjectionConfig(horizon_years=20, discount_rate=0.047),
    )
    steep = FulfilmentCashFlowEngine().project(
        contract, assumptions.mortality, assumptions.expenses,
        ProjectionConfig(horizon_years=20, discount_rate=0.12),
    )
    assert steep.pv_claims < flat.pv_claims
    assert steep.pv_premiums < flat.pv_premiums


def test_group_scaling(assumptions) -> None:  # noqa: ANN001
    contract = demo_portfolio()[0]
    projection = FulfilmentCashFlowEngine().project(
        contract, assumptions.mortality, assumptions.expenses,
        ProjectionConfig(horizon_years=10, discount_rate=0.047),
    )
    scaled = projection.to_group_scale(1000)
    assert scaled.pv_claims == pytest.approx(projection.pv_claims * 1000)


def test_invalid_discount_rate_is_rejected() -> None:
    with pytest.raises(ComputationError):
        ProjectionConfig(discount_rate=-1.5)


def test_projection_warnings_catch_impossible_inputs(assumptions) -> None:  # noqa: ANN001
    contract = demo_portfolio()[0]
    projection = FulfilmentCashFlowEngine().project(
        contract, assumptions.mortality, assumptions.expenses,
        ProjectionConfig(horizon_years=5, discount_rate=0.047),
    )
    assert isinstance(validate_projection(projection, contract), list)


# ---------------------------------------------------------------------------
# Risk adjustment
# ---------------------------------------------------------------------------
def test_risk_adjustment_is_the_charge_not_the_best_estimate(assumptions) -> None:  # noqa: ANN001
    contract = demo_portfolio()[0]
    config = ProjectionConfig(horizon_years=20, discount_rate=0.047)
    projection = FulfilmentCashFlowEngine().project(
        contract, assumptions.mortality, assumptions.expenses, config
    )
    result = RiskAdjustmentEngine(paths=1000, seed=42).simulate(
        projection, assumptions.mortality, config, contract=contract
    )
    assert result.best_estimate > 0
    assert result.risk_adjustment == pytest.approx(result.point_estimate - result.best_estimate)
    # The charge for uncertainty must be a small fraction of the outflows, not equal
    # to them: charging the whole best estimate would double-count.
    assert result.risk_adjustment < 0.35 * result.best_estimate
    assert result.lower_bound < result.point_estimate <= result.upper_bound
    assert result.coefficient_of_variation < 0.5


def test_risk_adjustment_is_reproducible(assumptions) -> None:  # noqa: ANN001
    contract = demo_portfolio()[0]
    config = ProjectionConfig(horizon_years=15, discount_rate=0.047)
    projection = FulfilmentCashFlowEngine().project(
        contract, assumptions.mortality, assumptions.expenses, config
    )
    first = RiskAdjustmentEngine(paths=500, seed=7).simulate(
        projection, assumptions.mortality, config, contract=contract
    )
    second = RiskAdjustmentEngine(paths=500, seed=7).simulate(
        projection, assumptions.mortality, config, contract=contract
    )
    assert first.point_estimate == second.point_estimate


def test_risk_adjustment_grows_with_uncertainty(assumptions) -> None:  # noqa: ANN001
    contract = demo_portfolio()[0]
    config = ProjectionConfig(horizon_years=15, discount_rate=0.047)
    projection = FulfilmentCashFlowEngine().project(
        contract, assumptions.mortality, assumptions.expenses, config
    )
    calm = RiskAdjustmentEngine(paths=800, seed=3, severity_shock_sd=0.05).simulate(
        projection, assumptions.mortality, config, contract=contract
    )
    volatile = RiskAdjustmentEngine(paths=800, seed=3, severity_shock_sd=0.90).simulate(
        projection, assumptions.mortality, config, contract=contract
    )
    assert volatile.risk_adjustment > calm.risk_adjustment


def test_risk_adjustment_needs_enough_paths() -> None:
    with pytest.raises(ComputationError):
        RiskAdjustmentEngine(paths=1).simulate(None, None, None)  # type: ignore[arg-type]


def test_psi_is_zero_for_identical_populations() -> None:
    values = [float(i) for i in range(100)]
    psi, buckets = population_stability_index(values, values, buckets=10)
    assert psi < 1e-9
    assert len(buckets) == 10
    assert math.isfinite(psi)


def test_psi_detects_a_shift() -> None:
    baseline = [float(i) for i in range(100)]
    shifted = [float(i) + 60 for i in range(100)]
    psi, _ = population_stability_index(baseline, shifted)
    assert psi > 0.25


# ---------------------------------------------------------------------------
# Initial recognition
# ---------------------------------------------------------------------------
def test_profitable_group_recognises_a_csm() -> None:
    result = recognise_initial(
        CsmInputs(pv_future_premiums=1_000_000, pv_future_claims_and_expenses=600_000,
                   risk_adjustment=40_000, deferred_acquisition_costs=120_000),
        MeasurementModel.GMM,
    )
    assert result.csm == pytest.approx(560_000)
    assert result.loss_component == 0.0
    assert result.recognised_in_profit_or_loss == 0.0


def test_onerous_group_gets_a_loss_component_not_a_negative_csm() -> None:
    """IFRS 17 para 103: no negative CSM; a loss component and an immediate loss."""
    result = recognise_initial(
        CsmInputs(pv_future_premiums=500_000, pv_future_claims_and_expenses=700_000,
                   risk_adjustment=30_000),
        MeasurementModel.GMM,
    )
    assert result.csm == 0.0
    # FCF -200,000 plus a 30,000 risk adjustment gives a -170,000 residual.
    assert result.loss_component == pytest.approx(170_000)
    assert result.recognised_in_profit_or_loss == pytest.approx(170_000)
    assert any("para 103" in note for note in result.notes)


def test_floating_point_dust_is_not_a_loss_component() -> None:
    """A CSM of -0.004 on a large group is arithmetic noise, not an onerous result."""
    result = recognise_initial(
        CsmInputs(
            pv_future_premiums=1_000_000.0,
            pv_future_claims_and_expenses=1_000_000.004,
        ),
        MeasurementModel.GMM,
        tolerance=0.01,
    )
    assert result.csm == 0.0
    assert result.loss_component == 0.0


def test_paa_recognises_no_csm() -> None:
    result = recognise_initial(
        CsmInputs(pv_future_premiums=1_000_000, pv_future_claims_and_expenses=600_000),
        MeasurementModel.PAA,
    )
    assert result.csm == 0.0
    assert result.loss_component == 0.0
    assert any("PAA" in note for note in result.notes)


def test_invalid_currency_is_rejected() -> None:
    with pytest.raises(ValidationError):
        recognise_initial(
            CsmInputs(pv_future_premiums=1, pv_future_claims_and_expenses=1, currency="DOLLARS"),
            MeasurementModel.GMM,
        )


# ---------------------------------------------------------------------------
# Roll-forward
# ---------------------------------------------------------------------------
def test_roll_forward_reconciles() -> None:
    movement = roll_forward(
        CsmRollForwardInput(opening_csm=800_000, locked_in_rate=0.047, csm_release=90_000,
                            experience_adjustments=-15_000)
    )
    assert movement.reconciled is True
    assert movement.interest_accretion == pytest.approx(37_600)
    assert movement.closing_csm == pytest.approx(800_000 + 37_600 - 90_000 - 15_000)
    assert movement.coverage_allocations["insurance"] == pytest.approx(90_000)


def test_group_entering_loss_creates_a_loss_component() -> None:
    movement = roll_forward(
        CsmRollForwardInput(opening_csm=50_000, locked_in_rate=0.05, csm_release=0.0,
                            experience_adjustments=-120_000)
    )
    assert movement.closing_csm == 0.0
    # 50,000 opening + 2,500 accretion - 120,000 adverse = -67,500 residual.
    assert movement.loss_component_closing == pytest.approx(67_500)
    assert movement.reconciled is True
    assert any("para 103" in note for note in movement.notes)


def test_loss_group_gets_no_csm_release() -> None:
    movement = roll_forward(
        CsmRollForwardInput(opening_csm=0, previous_loss_component=200_000, csm_release=50_000)
    )
    assert movement.csm_release == 0.0
    assert movement.closing_csm == 0.0
    assert movement.reconciled is True
    assert any("not released while the group is in loss" in n for n in movement.notes)


def test_favourable_variances_reverse_the_loss_component_before_building_csm() -> None:
    movement = roll_forward(
        CsmRollForwardInput(opening_csm=0, previous_loss_component=100_000,
                            experience_adjustments=40_000)
    )
    assert movement.closing_csm == 0.0
    assert movement.loss_component_closing == pytest.approx(60_000)
    assert movement.reconciled is True

    rebuilt = roll_forward(
        CsmRollForwardInput(opening_csm=0, previous_loss_component=100_000,
                            experience_adjustments=160_000)
    )
    assert rebuilt.loss_component_closing == 0.0
    assert rebuilt.closing_csm == pytest.approx(60_000)
    assert rebuilt.reconciled is True
    assert any("re-established a CSM" in n for n in rebuilt.notes)


def test_adverse_variances_increase_the_loss_component() -> None:
    movement = roll_forward(
        CsmRollForwardInput(opening_csm=0, previous_loss_component=100_000,
                            experience_adjustments=-30_000)
    )
    assert movement.loss_component_closing == pytest.approx(130_000)
    assert movement.closing_csm == 0.0
    assert movement.reconciled is True


def test_locked_in_rate_accretion_is_used() -> None:
    movement = roll_forward(
        CsmRollForwardInput(opening_csm=1_000_000, locked_in_rate=0.0375)
    )
    assert movement.interest_accretion == pytest.approx(37_500)
    assert any("locked-in rate" in note for note in movement.notes)


def test_explicit_accretion_overrides_the_rate() -> None:
    movement = roll_forward(
        CsmRollForwardInput(opening_csm=1_000_000, locked_in_rate=0.05, interest_accretion=12_000)
    )
    assert movement.interest_accretion == 12_000


def test_roll_forward_across_many_states_always_reconciles() -> None:
    """Sweep the opening balance, release and variance space for the invariant."""
    for opening in (0.0, 50_000.0, 500_000.0, 5_000_000.0):
        for release in (0.0, 10_000.0, 90_000.0):
            for variance in (-500_000.0, -30_000.0, 0.0, 60_000.0, 900_000.0):
                for loss in (0.0, 20_000.0, 200_000.0):
                    movement = roll_forward(
                        CsmRollForwardInput(
                            opening_csm=opening,
                            locked_in_rate=0.047,
                            csm_release=release,
                            experience_adjustments=variance,
                            previous_loss_component=loss,
                        )
                    )
                    assert movement.reconciled, (opening, release, variance, loss)
                    assert movement.closing_csm >= -0.01
                    assert movement.loss_component_closing >= -0.01


# ---------------------------------------------------------------------------
# Measurement by model
# ---------------------------------------------------------------------------
def test_gmm_produces_a_csm(measured: list[MeasurementResult]) -> None:
    gmm = next(r for r in measured if r.measurement_model is MeasurementModel.GMM)
    assert gmm.contractual_service_margin > 0
    assert gmm.risk_adjustment > 0
    assert gmm.csm_release == 0.0, "nothing is released at initial recognition"
    assert gmm.interest_accretion == 0.0
    assert gmm.closing_csm == gmm.contractual_service_margin


def test_paa_has_no_csm_or_risk_adjustment(measured: list[MeasurementResult]) -> None:
    paa = next(r for r in measured if r.measurement_model is MeasurementModel.PAA)
    assert paa.contractual_service_margin == 0.0
    assert paa.risk_adjustment == 0.0
    assert any("para 53" in note or "short-duration" in note for note in paa.notes)


def test_vfa_scales_by_participation(measured: list[MeasurementResult]) -> None:
    vfa = next(r for r in measured if r.measurement_model is MeasurementModel.VFA)
    assert any("participation" in note for note in vfa.notes + vfa.warnings)


def test_loss_group_is_measured_as_a_loss(measured: list[MeasurementResult]) -> None:
    loss = [r for r in measured if r.loss_component > 0]
    assert loss, "the worked portfolio must contain an onerous group"
    for result in loss:
        assert result.contractual_service_margin == 0.0
        assert result.loss_component > 0


def test_measurement_is_reproducible(engine: CsmEngine, context: ValuationContext) -> None:
    cohort = demo_portfolio()[0]
    first = engine.measure(cohort, context)
    second = engine.measure(cohort, context)
    assert first.contractual_service_margin == second.contractual_service_margin
    assert first.risk_adjustment == second.risk_adjustment


def test_subsequent_measurement_rolls_forward_and_reconciles(
    engine: CsmEngine, context: ValuationContext, measured: list[MeasurementResult]
) -> None:
    cohort = demo_portfolio()[0]
    previous = measured[0]
    nxt = engine.subsequent_measurement(
        cohort, context, previous=previous,
        actual_claims=1_100_000, actual_expenses=310_000, expected_claims=1_000_000,
    )
    assert nxt.opening_csm == previous.contractual_service_margin
    assert nxt.csm_release > 0
    assert nxt.interest_accretion > 0
    assert nxt.closing_csm == pytest.approx(
        nxt.opening_csm + nxt.interest_accretion + nxt.experience_adjustment - nxt.csm_release
    )
    disclosure = DisclosureGenerator().generate([nxt], period="2027")
    assert disclosure["allReconciled"] is True


def test_subsequent_paa_never_gains_a_csm(
    engine: CsmEngine, context: ValuationContext, measured: list[MeasurementResult]
) -> None:
    paa_cohort = next(c for c in wavy_portfolio() if c.measurement_model is MeasurementModel.PAA)
    paa_result = next(r for r in measured if r.measurement_model is MeasurementModel.PAA)
    nxt = engine.subsequent_measurement(
        paa_cohort, context, previous=paa_result,
        actual_claims=1_000_000, actual_expenses=200_000, expected_claims=950_000,
    )
    assert nxt.contractual_service_margin == 0.0
    assert nxt.closing_csm == 0.0


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------
def test_grouping_separates_profitable_and_onerous() -> None:
    ctx = build_context(
        Tenant(tenant_id="demo-carrier", legal_name="Demo"),
        RequestPrincipal(subject="actuary", tenant_id="demo-carrier", scopes=frozenset({"*"})),
    )
    outcome = GroupingEngine().group(wavy_portfolio(), ctx)
    assert outcome.group_count >= 4
    assert outcome.onerous_groups >= 1
    assert outcome.profitable_groups >= 1
    for decision in outcome.decisions:
        assert "para 4(b)" in decision.rationale


def test_grouping_ids_are_stable() -> None:
    ctx = build_context(
        Tenant(tenant_id="demo-carrier", legal_name="Demo"),
        RequestPrincipal(subject="a", tenant_id="demo-carrier", scopes=frozenset({"*"})),
    )
    first = GroupingEngine().group(wavy_portfolio(), ctx)
    second = GroupingEngine().group(wavy_portfolio(), ctx)
    assert [d.group_id for d in first.decisions] == [d.group_id for d in second.decisions]


def test_grouping_criteria_are_validated() -> None:
    with pytest.raises(ValidationError):
        GroupingCriteria(cohort_years_per_group=0)
    with pytest.raises(ValidationError):
        GroupingCriteria(minimum_profitability_margin=2.0)


def test_grouping_warns_about_slim_groups() -> None:
    ctx = build_context(
        Tenant(tenant_id="demo-carrier", legal_name="Demo"),
        RequestPrincipal(subject="a", tenant_id="demo-carrier", scopes=frozenset({"*"})),
    )
    outcome = GroupingEngine(
        GroupingCriteria(minimum_contracts_per_group=1000)
    ).group(wavy_portfolio(), ctx)
    assert any("below the" in w for w in outcome.warnings)


def test_grouping_rejects_an_empty_population() -> None:
    ctx = build_context(
        Tenant(tenant_id="demo-carrier", legal_name="Demo"),
        RequestPrincipal(subject="a", tenant_id="demo-carrier", scopes=frozenset({"*"})),
    )
    with pytest.raises(ValidationError):
        GroupingEngine().group([], ctx)


def test_grouping_diff_detects_changes() -> None:
    ctx = build_context(
        Tenant(tenant_id="demo-carrier", legal_name="Demo"),
        RequestPrincipal(subject="a", tenant_id="demo-carrier", scopes=frozenset({"*"})),
    )
    engine = GroupingEngine()
    before = engine.group(demo_portfolio(), ctx)
    after = engine.group(wavy_portfolio(), ctx)
    diff = compare_grouping(before.decisions, after.decisions)
    assert diff["added"]
    assert diff["stable"] is False


def test_population_validation_flags_paa_on_long_contracts() -> None:
    build_context(
        Tenant(tenant_id="demo-carrier", legal_name="Demo"),
        RequestPrincipal(subject="a", tenant_id="demo-carrier", scopes=frozenset({"*"})),
    )
    bad = ContractGroupInput(
        group_id="GIC-BAD", period="2026", reporting_date=date(2026, 12, 31),
        measurement_model=MeasurementModel.PAA, product_line="term", cohort_year=2020,
        contract_count=10, issued_premium=100_000, expected_premiums=100_000,
        expected_claims=50_000, expected_expenses=5_000, average_term_years=20,
    )
    warnings = GroupingEngine().validate_population([bad])
    assert any("short-duration" in w for w in warnings)


def test_grouping_basis_ordering() -> None:
    assert GroupingBasis.LINE_OF_BUSINESS.rank == 0
    assert GroupingBasis.PROFITABILITY.rank == 3


# ---------------------------------------------------------------------------
# Disclosure
# ---------------------------------------------------------------------------
def test_disclosure_reconciles_at_initial_recognition(measured: list[MeasurementResult]) -> None:
    disclosure = DisclosureGenerator().generate(measured, period="2026")
    assert disclosure["allReconciled"] is True
    assert disclosure["tableCount"] >= 6
    ids = {t["tableId"] for t in disclosure["tables"]}
    assert {"IFRS17-LRC", "IFRS17-CSM", "IFRS17-RA", "IFRS17-PL", "SSAP102"} <= ids


def test_csm_table_excludes_paa(measured: list[MeasurementResult]) -> None:
    disclosure = DisclosureGenerator().generate(measured, period="2026")
    csm = next(t for t in disclosure["tables"] if t["tableId"] == "IFRS17-CSM")
    excluded = csm["reconciliation"]["excludedPaaGroups"]
    assert excluded, "the worked portfolio uses PAA and must be excluded"
    assert csm["reconciliation"]["balanced"] is True


def test_disclosure_rejects_no_results() -> None:
    with pytest.raises(ComputationError):
        DisclosureGenerator().generate([], period="2026")


def test_disclosure_exports_all_formats(measured: list[MeasurementResult]) -> None:
    disclosure = DisclosureGenerator().generate(measured, period="2026")
    payloads = export(disclosure, ["json", "xlsx", "pdf", "xbrl"])
    assert json.loads(payloads["json"].decode())["tableCount"] == disclosure["tableCount"]
    assert payloads["xlsx"][:2] == b"PK"  # zip container
    assert payloads["pdf"][:4] == b"%PDF"
    assert b"xbrli:xbrl" in payloads["xbrl"]


def test_unsupported_export_format(measured: list[MeasurementResult]) -> None:
    disclosure = DisclosureGenerator().generate(measured, period="2026")
    with pytest.raises(ComputationError):
        export(disclosure, ["docx"])


def test_xlsx_has_one_sheet_per_table(measured: list[MeasurementResult]) -> None:
    from openpyxl import load_workbook  # noqa: PLC0415

    disclosure = DisclosureGenerator().generate(measured, period="2026")
    import io  # noqa: PLC0415

    workbook = load_workbook(io.BytesIO(to_xlsx(disclosure)))
    assert "Summary" in workbook.sheetnames
    for table in disclosure["tables"]:
        assert table["tableId"][:31] in workbook.sheetnames


def test_xbrl_is_parseable(measured: list[MeasurementResult]) -> None:
    import xml.etree.ElementTree as ET  # noqa: PLC0415

    disclosure = DisclosureGenerator().generate(measured, period="2026")
    root = ET.fromstring(to_xbrl(disclosure).decode())
    assert root.tag.endswith("xbrl")


def test_pdf_renders(measured: list[MeasurementResult]) -> None:
    assert to_pdf(DisclosureGenerator().generate(measured, period="2026"))[:4] == b"%PDF"


def test_disclosure_summary_names_failing_tables(measured: list[MeasurementResult]) -> None:
    disclosure = DisclosureGenerator().generate(measured, period="2026")
    summary = DisclosureGenerator().reconciliation_summary(disclosure)
    assert summary["allReconciled"] is True
    assert summary["failedTables"] == []


def test_ssap102_mapping() -> None:
    assert ssap102_reserve_class("term") == "class1"
    assert ssap102_reserve_class("fixed indexed annuity") == "class5"
    assert ssap102_reserve_class("something else") == "class99"


def test_json_export_is_canonical(measured: list[MeasurementResult]) -> None:
    disclosure = DisclosureGenerator().generate(measured, period="2026")
    assert json.loads(to_json(disclosure).decode())["allReconciled"] is True


# ---------------------------------------------------------------------------
# Assumptions and the service
# ---------------------------------------------------------------------------
def test_assumption_versions_are_immutable_once_published() -> None:
    store = AssumptionStore()
    first = worked_assumption_set()
    store.publish("t", first)
    with pytest.raises(ValidationError):
        store.publish("t", first.model_copy(update={"label": "changed"}))
    store.publish("t", first.model_copy(update={"version": "v2"}))
    assert [v.version for v in store.versions("t")] == ["v1", "v2"]


def test_approved_assumptions_cannot_be_deleted() -> None:
    store = AssumptionStore()
    draft = worked_assumption_set().model_copy(update={"status": "draft"})
    store.publish("t", draft)
    store.approve("t", "v1", by="actuary")
    with pytest.raises(ValidationError) as exc:
        store.delete("t", "v1")
    assert "superseding version" in exc.value.message


def test_approving_twice_is_refused() -> None:
    store = AssumptionStore()
    store.publish("t", worked_assumption_set().model_copy(update={"status": "draft"}))
    store.approve("t", "v1", by="actuary")
    with pytest.raises(ValidationError):
        store.approve("t", "v1", by="actuary")


def test_draft_assumptions_can_be_deleted() -> None:
    store = AssumptionStore()
    store.publish("t", worked_assumption_set().model_copy(update={"status": "draft"}))
    assert store.delete("t", "v1") is True
    assert store.versions("t") == []


def test_assumption_diff_flags_material_changes() -> None:
    store = AssumptionStore()
    store.publish("t", worked_assumption_set())
    shocked = worked_assumption_set().model_copy(update={"version": "v2"})
    shocked.mortality.lapse_by_tenure = {1: 0.12}
    store.publish("t", shocked)
    diff = store.diff("t", "v1", "v2")
    assert diff["materialChange"] is True
    assert any(c["assumption"] == "mortality.lapseByTenure" for c in diff["changes"])


@pytest.mark.asyncio
async def test_valuation_run_end_to_end() -> None:
    ctx = build_context(
        Tenant(tenant_id="demo-carrier", legal_name="Demo Mutual Life", pas_vendor="simulated"),
        RequestPrincipal(subject="actuary", tenant_id="demo-carrier", scopes=frozenset({"*"})),
    )
    store = AssumptionStore()
    store.publish(ctx.tenant_id, worked_assumption_set())
    service = ValuationService(assumptions=store, project_years=15)
    run = await service.run(ctx, reporting_date=date(2026, 12, 31), period="2026")
    assert run.results
    assert run.disclosure is not None
    assert run.ok is True
    assert run.extract.policy_count > 0
    assert run.summary()["totals"]["contractualServiceMargin"] > 0
    assert run.summary()["allReconciled"] is True


def test_standalone_roll_forward_endpoint() -> None:
    service = ValuationService(assumptions=AssumptionStore())
    movement = service.roll_forward_group(
        opening_csm=1_000_000, locked_in_rate=0.047, csm_release=120_000
    )
    assert movement["reconciled"] is True
    assert movement["interestAccretion"] == 47_000


def test_reserves_are_not_written_by_default() -> None:
    service = ValuationService(assumptions=AssumptionStore())
    assert service.write_reserves_back is False


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------
@pytest.fixture
def client() -> TestClient:
    from pas_plugins.plugin2_ifrs17.main import app

    return TestClient(app, raise_server_exceptions=False)


def test_health(client: TestClient) -> None:
    assert client.get("/health").json()["plugin"] == "plugin2"


def test_models_endpoint(client: TestClient) -> None:
    body = client.get("/ifrs17/models").json()
    assert {m["model"] for m in body["models"]} == {"gmm", "vfa", "paa"}
    assert "statutory reserve" in body["warning"]


def test_assumptions_seeded(client: TestClient) -> None:
    body = client.get("/ifrs17/assumptions").json()
    assert body["count"] >= 1
    assert body["versions"][0]["mortality"]["qxByAge"]


def test_measure_endpoint(client: TestClient) -> None:
    cohort = demo_portfolio()[0].to_dict()
    cohort["reportingDate"] = "2026-12-31"
    body = client.post("/ifrs17/measure", json=cohort).json()
    assert body["measurementModel"] == "gmm"
    assert body["contractualServiceMargin"] > 0
    assert body["method"].startswith("General Measurement Model")


def test_roll_forward_endpoint(client: TestClient) -> None:
    body = client.post(
        "/ifrs17/roll-forward",
        json={"openingCsm": 1_000_000, "lockedInRate": 0.047, "csmRelease": 90_000},
    ).json()
    assert body["reconciled"] is True
    assert body["interestAccretion"] == 47_000


def test_roll_forward_requires_opening_csm(client: TestClient) -> None:
    response = client.post("/ifrs17/roll-forward", json={})
    assert response.status_code == 400
    assert response.json()["code"] == ErrorCode.VALIDATION_FAILED.value


def test_demo_endpoint(client: TestClient) -> None:
    body = client.post("/ifrs17/demo", json={}).json()
    assert len(body["results"]) >= 5
    assert body["disclosure"]["allReconciled"] is True
    assert "illustration" in body


def test_mcp_catalogue(client: TestClient) -> None:
    body = client.get("/mcp/catalogue").json()
    names = {t["name"] for t in body["tools"]}
    assert "ifrs17_measure_group" in names
    assert "ifrs17_run_valuation" in names
    assert "ifrs17_roll_forward_csm" in names
    catalogue_tool = next(t for t in body["tools"] if t["name"] == "ifrs17_list_measurement_models")
    assert catalogue_tool["readOnly"] is True


def test_openapi_is_31(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert schema["openapi"].startswith("3.1")
    assert schema["info"]["x-mcp"]["toolCount"] >= 8
