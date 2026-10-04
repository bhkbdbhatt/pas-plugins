"""Worked example data for the IFRS 17 plugin.

A carrier actuary evaluating the engine wants to see a complete, internally
consistent portfolio rather than one synthetic contract. This module provides:

* :func:`worked_assumption_set` - a full assumption set with curves
* :func:`demo_portfolio` - a cohorted life and annuity portfolio
* :func:`wavy_portfolio` - a portfolio with deliberately mixed profitability, so
  the grouping engine's onerous/profitable split and the loss-component rule are
  both exercised

All figures are illustrative and clearly labelled as such; they are not advice and
must not be used for any filing.
"""

from __future__ import annotations

from datetime import date

from pas_plugins.plugin2_ifrs17.models import (
    AssumptionVersion,
    ContractGroupInput,
    ExpenseAssumptions,
    MeasurementModel,
    MortalityAssumptions,
    Profitability,
)

DEMO_ASSUMPTION_DATE = date(2026, 1, 1)
DEMO_REPORTING_DATE = date(2026, 12, 31)


def worked_assumption_set() -> AssumptionVersion:
    """A complete assumption set with mortality, lapse, expenses and both curves."""
    mortality = MortalityAssumptions(
        model_name="2026 US insured lives best estimate",
        version="v1",
        effective_from=date(2026, 1, 1),
        qx_by_age={
            25: 0.0008, 30: 0.0010, 35: 0.0014, 40: 0.0021, 45: 0.0032, 50: 0.0049,
            55: 0.0075, 60: 0.0112, 65: 0.0165, 70: 0.0242, 75: 0.0351, 80: 0.0502,
        },
        lapse_by_tenure={1: 0.085, 2: 0.062, 3: 0.051, 4: 0.045, 5: 0.041,
                         7: 0.037, 10: 0.034, 15: 0.032, 20: 0.031},
        morbidity_rate=0.012,
        morbidity_trend=0.045,
    )
    expenses = ExpenseAssumptions(
        version="v1",
        maintenance_expense_pct_of_premium=0.021,
        acquisition_expense_pct_of_premium=0.145,
        claim_expense_per_claim=820.0,
        expense_inflation=0.032,
        currency="USD",
    )
    locked_in = _curve("USD-LI-2026-LOCKED-IN", "locked-in", DEMO_ASSUMPTION_DATE)
    updated = _curve("USD-LI-2026-UPDATED", "updated", DEMO_REPORTING_DATE, shift=0.0035)
    return AssumptionVersion(
        version="v1",
        label="2026 annual valuation - illustrative",
        effective_from=DEMO_ASSUMPTION_DATE,
        mortality=mortality,
        expenses=expenses,
        locked_in_curve=locked_in,
        updated_curve=updated,
        created_by="actuarial-demo",
        status="approved",
        notes=(
            "Illustrative assumption set for evaluation. Not derived from any carrier's "
            "experience study and not suitable for a filing."
        ),
    )


def _curve(curve_id: str, basis: str, as_of: date, *, shift: float = 0.0) -> object:
    from pas_plugins.plugin2_ifrs17.cashflows import curve_from_rates  # noqa: PLC0415

    base = {
        1: 0.0420, 2: 0.0425, 3: 0.0430, 5: 0.0445, 7: 0.0455, 10: 0.0465,
        15: 0.0470, 20: 0.0475, 25: 0.0475, 30: 0.0472, 40: 0.0468, 50: 0.0465,
    }
    return curve_from_rates(
        curve_id,
        {term: round(rate + shift, 6) for term, rate in base.items()},
        as_of=as_of,
        basis=basis,
    )


def demo_portfolio() -> list[ContractGroupInput]:
    """A profitable life portfolio - the case that should produce a CSM."""
    return [
        _cohort(
            "GIC-TERM-2024", "term", 2024, MeasurementModel.GMM, Profitability.PROFITABLE,
            contracts=1_250, issued=3_600_000, expected_premiums=4_450_000,
            expected_claims=2_150_000, expected_expenses=510_000, age=38, term=20,
        ),
        _cohort(
            "GIC-WL-2022", "whole life", 2022, MeasurementModel.GMM, Profitability.PROFITABLE,
            contracts=480, issued=2_150_000, expected_premiums=5_900_000,
            expected_claims=4_180_000, expected_expenses=620_000, age=44, term=30,
        ),
        _cohort(
            "GIC-IUL-2025", "indexed universal life", 2025, MeasurementModel.VFA,
            Profitability.PROFITABLE,
            contracts=310, issued=1_850_000, expected_premiums=3_200_000,
            expected_claims=2_310_000, expected_expenses=395_000, age=41, term=20,
            metadata_share=0.78,
        ),
        _cohort(
            "GIC-PA-2026", "fixed indexed annuity", 2026, MeasurementModel.PAA,
            Profitability.PROFITABLE,
            contracts=940, issued=18_600_000, expected_premiums=1_400_000,
            expected_claims=1_250_000, expected_expenses=180_000, age=63, term=1,
        ),
    ]


def wavy_portfolio() -> list[ContractGroupInput]:
    """A portfolio with deliberately mixed profitability.

    Includes cohorts that are marginally profitable, exactly on cost and clearly
    onerous, so the paragraph 4 profitability assessment, the loss-component rule
    and the CSM roll-forward through a loss are all exercised.
    """
    return demo_portfolio() + [
        _cohort(
            "GIC-TERM-2023-MARGINAL", "term", 2023, MeasurementModel.GMM,
            Profitability.ON_COST,
            contracts=180, issued=395_000, expected_premiums=410_000,
            expected_claims=402_000, expected_expenses=44_000, age=57, term=15,
        ),
        _cohort(
            "GIC-WL-2019-LOSS", "whole life", 2019, MeasurementModel.GMM,
            Profitability.LOSS,
            contracts=95, issued=210_000, expected_premiums=180_000,
            expected_claims=265_000, expected_expenses=48_000, age=62, term=25,
        ),
    ]


def _cohort(
    group_id: str,
    product_line: str,
    cohort_year: int,
    model: MeasurementModel,
    profitability: Profitability,
    *,
    contracts: int,
    issued: float,
    expected_premiums: float,
    expected_claims: float,
    expected_expenses: float,
    age: int,
    term: int,
    metadata_share: float = 0.80,
) -> ContractGroupInput:
    return ContractGroupInput(
        group_id=group_id,
        period="2026",
        reporting_date=DEMO_REPORTING_DATE,
        measurement_model=model,
        product_line=product_line,
        cohort_year=cohort_year,
        profitability=profitability,
        contract_count=contracts,
        issued_premium=issued,
        expected_premiums=expected_premiums,
        expected_claims=expected_claims,
        expected_expenses=expected_expenses,
        cash_at_recognition=0.0,
        average_age_at_issue=age,
        average_term_years=term,
        average_annualised_premium=round(issued / contracts, 2) if contracts else 0.0,
        locked_in_rate=0.0465,
        issue_date=date(cohort_year, 3, 1),
        data_as_of=DEMO_REPORTING_DATE,
        assumption_version="v1",
        metadata_share=metadata_share,
    )
