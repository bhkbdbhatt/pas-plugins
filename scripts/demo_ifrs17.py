"""Script: exercise the IFRS 17 engine end to end and print the key figures.

Run with::

    .venv/Scripts/python -m scripts.demo_ifrs17
"""

from __future__ import annotations

import json
from datetime import date

from pas_plugins.plugin2_ifrs17.cashflows import (
    FulfilmentCashFlowEngine,
    ProjectionConfig,
    RiskAdjustmentEngine,
    curve_from_rates,
)
from pas_plugins.plugin2_ifrs17.csm import CsmEngine, CsmInputs, ValuationContext, recognise_initial, roll_forward, CsmRollForwardInput
from pas_plugins.plugin2_ifrs17.models import (
    AssumptionVersion,
    ContractGroupInput,
    ExpenseAssumptions,
    MeasurementModel,
    MortalityAssumptions,
    Profitability,
    net_balance_sheet,
)


def build_assumptions() -> AssumptionVersion:
    mortality = MortalityAssumptions(
        version="v1",
        effective_from=date(2026, 1, 1),
        qx_by_age={30: 0.0010, 35: 0.0012, 40: 0.0019, 45: 0.0030, 50: 0.0048, 55: 0.0075, 60: 0.0112},
        lapse_by_tenure={1: 0.085, 2: 0.06, 3: 0.05, 5: 0.04, 10: 0.035},
    )
    expenses = ExpenseAssumptions(version="v1")
    curve = curve_from_rates(
        "USD-LI-2026",
        {1: 0.0420, 5: 0.0450, 10: 0.0470, 20: 0.0480, 30: 0.0475},
        as_of=date(2026, 3, 31),
    )
    return AssumptionVersion(
        version="v1",
        label="2026 annual valuation",
        effective_from=date(2026, 1, 1),
        mortality=mortality,
        expenses=expenses,
        locked_in_curve=curve,
        updated_curve=curve,
        status="approved",
    )


def build_contract(**overrides) -> ContractGroupInput:
    base = {
        "group_id": "GIC-LIFE-2024-TERM",
        "period": "2026",
        "reporting_date": date(2026, 3, 31),
        "measurement_model": MeasurementModel.GMM,
        "product_line": "term",
        "cohort_year": 2024,
        "profitability": Profitability.PROFITABLE,
        "contract_count": 1000,
        "issued_premium": 2_400_000,
        "expected_premiums": 3_100_000,
        "expected_claims": 1_900_000,
        "expected_expenses": 300_000,
        "average_age_at_issue": 38,
        "average_term_years": 20,
        "average_annualised_premium": 2400,
        "issue_date": date(2025, 6, 1),
        "locked_in_rate": 0.047,
        "data_as_of": date(2026, 3, 31),
    }
    base.update(overrides)
    return ContractGroupInput(**base)


def main() -> int:
    assumptions = build_assumptions()
    context = ValuationContext(
        assumptions=assumptions,
        locked_in_curve=assumptions.locked_in_curve,
        updated_curve=assumptions.updated_curve,
        projection_years=30,
    )

    print("=" * 78)
    print("IFRS 17 engine - worked example")
    print("=" * 78)

    # --- deterministic projection -----------------------------------------
    contract = build_contract()
    projection = FulfilmentCashFlowEngine().project(
        contract,
        assumptions.mortality,
        assumptions.expenses,
        ProjectionConfig(horizon_years=30, discount_rate=0.047,
                         discount_curve=assumptions.locked_in_curve),
    )
    print("\nFulfilment cash flows (30-year projection)")
    print(f"  PV premiums            {projection.pv_premiums:>18,.2f}")
    print(f"  PV claims              {projection.pv_claims:>18,.2f}")
    print(f"  PV expenses            {projection.pv_expenses:>18,.2f}")
    print(f"  Net present value      {projection.net_present_value:>18,.2f}")
    print(f"  Survival after year 30 {projection.survival_rate:>18,.4f}")
    print(f"  Group PV premiums       {projection.to_group_scale(1000).pv_premiums:>18,.2f}")

    # --- risk adjustment ---------------------------------------------------
    ra = RiskAdjustmentEngine(paths=2000, seed=20260101).simulate(
        projection, assumptions.mortality,
        ProjectionConfig(horizon_years=30, discount_rate=0.047,
                         discount_curve=assumptions.locked_in_curve),
        contract=contract,
    )
    print("\nRisk adjustment (2000-path Monte Carlo, 95% percentile)")
    print(f"  Point estimate         {ra.point_estimate:>18,.2f}")
    print(f"  95% interval           [{ra.lower_bound:,.2f}, {ra.upper_bound:,.2f}]")
    print(f"  Coefficient of variation{ra.coefficient_of_variation:>19.4f}")
    print(f"  Elapsed                {ra.duration_ms:>18.2f} ms")

    # --- initial recognition ----------------------------------------------
    for label, model in (
        ("GMM", MeasurementModel.GMM),
        ("VFA", MeasurementModel.VFA),
        ("PAA", MeasurementModel.PAA),
    ):
        result = CsmEngine().measure(build_contract(measurement_model=model), context)
        print(f"\n{model.display_name} ({label})")
        print(f"  Fulfilment cash flows  {result.fulfilment_cash_flows:>18,.2f}")
        print(f"  Risk adjustment       {result.risk_adjustment:>18,.2f}")
        print(f"  CSM                    {result.contractual_service_margin:>18,.2f}")
        print(f"  Loss component         {result.loss_component:>18,.2f}")
        print(f"  Balance sheet          {net_balance_sheet(result)}")
        if result.warnings:
            print(f"  Warnings               {result.warnings}")

    # --- the loss component rule ------------------------------------------
    onerous = CsmInputs(
        pv_future_premiums=100_000, pv_future_claims_and_expenses=140_000, risk_adjustment=4_000
    )
    loss = recognise_initial(onerous, MeasurementModel.GMM)
    print("\nOnerous group (IFRS 17 para 103)")
    print(f"  {json.dumps(loss.to_dict(), indent=2)}")

    # --- roll-forward ------------------------------------------------------
    movement = roll_forward(
        CsmRollForwardInput(
            opening_csm=800_000, locked_in_rate=0.047, csm_release=90_000,
            experience_adjustments=-15_000,
        )
    )
    print("\nCSM roll-forward")
    print(f"  {json.dumps(movement.to_dict(), indent=2)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
