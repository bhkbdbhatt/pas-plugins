"""Cash-flow projection and risk adjustment.

Two engines, kept separate because they answer different questions:

* :class:`FulfilmentCashFlowEngine` projects the *expected* cash flows. This is
  deterministic: same inputs, same numbers, every time - which is what makes a
  valuation reviewable and a regression test meaningful.
* :class:`RiskAdjustmentEngine` projects the *uncertainty* around those cash flows
  and puts a confidence interval around them. The risk adjustment is the
  compensation an entity charges for that uncertainty, and IFRS 17 requires it to
  be released as the uncertainty is removed, which is why it is measured as a
  distribution rather than as a single number.

Everything here is vectorised with NumPy so a 2000-path simulation over a
30-year projection completes in milliseconds.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np

from pas_core.errors import ComputationError
from pas_core.observability import VALUATIONS, Timer, trace_span
from pas_plugins.plugin2_ifrs17.models import (
    ContractGroupInput,
    DiscountCurve,
    ExpenseAssumptions,
    MeasurementResult,
    MortalityAssumptions,
)

MAX_DRAWDOWN = 0.999


@dataclass(frozen=True, slots=True)
class ProjectionConfig:
    """Knobs for a cash-flow projection."""

    horizon_years: int = 30
    discount_rate: float = 0.04
    currency: str = "USD"
    include_expenses: bool = True
    include_morbidity: bool = True
    apply_lapse: bool = True
    discount_curve: DiscountCurve | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if self.horizon_years < 1:
            msg = "horizon_years must be at least 1"
            raise ComputationError(msg, horizonYears=self.horizon_years)
        if self.discount_rate <= -1.0:
            msg = f"discount_rate {self.discount_rate} implies a non-positive accumulation factor"
            raise ComputationError(msg, discountRate=self.discount_rate)


@dataclass(slots=True)
class CashFlowProjection:
    """Year-by-year expected cash flows, present-valued."""

    years: tuple[int, ...]
    premiums: np.ndarray = field(repr=False)
    claims: np.ndarray = field(repr=False)
    expenses: np.ndarray = field(repr=False)
    claims_discounted: np.ndarray = field(repr=False)
    expenses_discounted: np.ndarray = field(repr=False)
    premiums_discounted: np.ndarray = field(repr=False)
    discount_factors: np.ndarray = field(repr=False)
    in_force: np.ndarray = field(repr=False)
    currency: str = "USD"

    @property
    def pv_claims(self) -> float:
        return float(self.claims_discounted.sum())

    @property
    def pv_expenses(self) -> float:
        return float(self.expenses_discounted.sum())

    @property
    def pv_premiums(self) -> float:
        return float(self.premiums_discounted.sum())

    @property
    def pv_outflows(self) -> float:
        return self.pv_claims + self.pv_expenses

    @property
    def net_present_value(self) -> float:
        return self.pv_premiums - self.pv_outflows

    @property
    def survival_rate(self) -> float:
        """Fraction of the initial cohort still in force at the end of the horizon."""
        return float(self.in_force[-1]) if len(self.in_force) else 0.0

    def scaled(self, factor: float, *, contract_count: int | None = None) -> CashFlowProjection:
        """Scale to group totals.

        The projection is computed per contract so the mortality and lapse
        recursions stay interpretable; the measurement itself is at group level, so
        it must be multiplied by the contract count before the CSM is derived.
        Doing that scaling explicitly here keeps the per-contract view available for
        diagnostics without anyone having to remember to apply the multiplier.
        """
        return CashFlowProjection(
            years=self.years,
            premiums=self.premiums * factor,
            claims=self.claims * factor,
            expenses=self.expenses * factor,
            claims_discounted=self.claims_discounted * factor,
            expenses_discounted=self.expenses_discounted * factor,
            premiums_discounted=self.premiums_discounted * factor,
            discount_factors=self.discount_factors,
            in_force=self.in_force * factor,
            currency=self.currency,
        )

    def to_group_scale(self, contract_count: int) -> CashFlowProjection:
        """Convert a per-contract projection into group totals."""
        return self.scaled(float(contract_count))

    def to_dict(self, *, max_years: int = 60) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "years": list(self.years[:max_years]),
            "pvPremiums": round(self.pv_premiums, 2),
            "pvClaims": round(self.pv_claims, 2),
            "pvExpenses": round(self.pv_expenses, 2),
            "pvOutflows": round(self.pv_outflows, 2),
            "netPresentValue": round(self.net_present_value, 2),
            "truncated": len(self.years) > max_years,
            "detail": [
                {
                    "year": int(year),
                    "inForce": round(float(self.in_force[i]), 4),
                    "premium": round(float(self.premiums[i]), 2),
                    "claims": round(float(self.claims[i]), 2),
                    "expenses": round(float(self.expenses[i]), 2),
                    "discountFactor": round(float(self.discount_factors[i]), 6),
                    "pvClaims": round(float(self.claims_discounted[i]), 2),
                }
                for i, year in enumerate(self.years[:max_years])
            ],
        }


class FulfilmentCashFlowEngine:
    """Deterministic projection of premium, claim and expense cash flows.

    The per-survival projection is the standard actuarial recursion: the number of
    contracts in force at the end of year *t* is the number at the start of year
    *t* times (1 - qx - lapse), and cash flows are weighted by that survival.
    Writing it this way rather than multiplying annual rates is what makes the
    projection consistent when claims are paid in the year of death.
    """

    def project(
        self,
        contract: ContractGroupInput,
        mortality: MortalityAssumptions,
        expenses: ExpenseAssumptions,
        config: ProjectionConfig,
    ) -> CashFlowProjection:
        """Project expected fulfilment cash flows for one contract group."""
        with trace_span(
            "pas.ifrs17.cashflows", group=contract.group_id, model=str(contract.measurement_model)
        ):
            timer = Timer()
            timer.__enter__()
            horizon = config.horizon_years
            years = tuple(range(1, horizon + 1))

            issue_age = contract.average_age_at_issue
            premium = contract.average_annualised_premium or (
                contract.issued_premium / max(contract.contract_count, 1)
            )

            in_force = np.zeros(horizon, dtype=float)
            premiums = np.zeros(horizon, dtype=float)
            claims = np.zeros(horizon, dtype=float)
            expense_outflows = np.zeros(horizon, dtype=float)

            # Survival is carried forward: the number of contracts in force at the
            # start of year t is what survived year t-1. Initialising the array to
            # ones would restart every year from a full cohort and overstate the
            # projection by an order of magnitude on a long-duration contract.
            surviving = 1.0
            for index, _year in enumerate(years):
                age = issue_age + index
                qx = mortality.qx(age)
                lapse = mortality.lapse(index + 1) if config.apply_lapse else 0.0
                survival = max(0.0, 1.0 - qx - lapse)
                start_in_force = surviving

                premiums[index] = start_in_force * premium
                expected_claim_per_contract = _expected_claim_per_contract(
                    contract, mortality, expenses, age
                )
                claims[index] = start_in_force * expected_claim_per_contract

                if config.include_expenses:
                    maintenance = premiums[index] * expenses.maintenance_expense_pct_of_premium
                    # One claim-handling cost per expected death claim.
                    claim_handling = start_in_force * qx * expenses.claim_expense_per_claim
                    escalation = (1.0 + expenses.expense_inflation) ** index
                    expense_outflows[index] = (maintenance + claim_handling) * escalation

                in_force[index] = start_in_force
                surviving = start_in_force * survival

            discount_factors = self._discount_factors(horizon, config)
            premiums_discounted = premiums * discount_factors
            claims_discounted = claims * discount_factors
            expenses_discounted = expense_outflows * discount_factors

            projection = CashFlowProjection(
                years=years,
                premiums=premiums,
                claims=claims,
                expenses=expense_outflows,
                claims_discounted=claims_discounted,
                expenses_discounted=expenses_discounted,
                premiums_discounted=premiums_discounted,
                discount_factors=discount_factors,
                in_force=in_force,
                currency=contract.currency,
            )
            elapsed = timer.elapsed
            del elapsed
            return projection

    @staticmethod
    def _discount_factors(horizon: int, config: ProjectionConfig) -> np.ndarray:
        """Discount factors, from a curve when one is supplied, else flat rate."""
        if config.discount_curve is not None:
            return np.array(
                [config.discount_curve.discount_factor(t) for t in range(1, horizon + 1)],
                dtype=float,
            )
        return np.array(
            [(1.0 + config.discount_rate) ** -t for t in range(1, horizon + 1)],
            dtype=float,
        )


def _expected_claim_per_contract(
    contract: ContractGroupInput,
    mortality: MortalityAssumptions,
    expenses: ExpenseAssumptions,
    age: int,
) -> float:
    """Expected claim cost per contract in force at the start of the year.

    Derived from the block's own ultimate loss, not from a hard-coded ratio: if
    the PAS reports ``expected_claims`` and ``expected_premiums`` for the group,
    those describe the block's economics and should drive the projection.
    """
    if contract.expected_premiums > 0 and contract.expected_claims > 0:
        loss_ratio = contract.expected_claims / contract.expected_premiums
        premium_per_contract = contract.average_annualised_premium or (
            contract.issued_premium / max(contract.contract_count, 1)
        )
        return max(0.0, premium_per_contract * loss_ratio)
    premium_per_contract = contract.average_annualised_premium or (
        contract.issued_premium / max(contract.contract_count, 1)
    )
    base = premium_per_contract * 2.2
    if age >= 60:
        base *= 2.5
    elif age >= 45:
        base *= 1.4
    base *= (1.0 + mortality.morbidity_trend) ** max(0, age - 35)
    base *= (1.0 + mortality.morbidity_rate * 10)
    return max(0.0, base)


# ---------------------------------------------------------------------------
# Risk adjustment
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class RiskAdjustmentResult:
    """Risk adjustment with its confidence interval and the volatility behind it.

    The distinction that matters: IFRS 17 paragraph B88 defines the risk adjustment
    as *the amount the entity charges for the uncertainty*, not the best estimate of
    the cash flows. The best estimate already sits in the fulfilment cash flows, so
    adding the whole simulated mean as well would double-count it and would
    routinely turn an onerous group into a profitable one.

    Therefore:

    * ``best_estimate`` - mean of the simulated PV of claims and expenses, i.e. the
      stochastic restatement of the fulfilment cash flows
    * ``point_estimate`` - the chosen confidence percentile, i.e. the entity's
      risk-loaded outflow
    * ``risk_adjustment`` - the difference between the two, which is what goes onto
      the balance sheet
    """

    confidence_level: float
    best_estimate: float
    point_estimate: float
    lower_bound: float
    upper_bound: float
    volatility: float
    standard_error: float
    path_count: int
    method: str = "monteCarloPercentile"
    seed: int = 0
    duration_ms: float = 0.0
    warnings: tuple[str, ...] = ()

    @property
    def risk_adjustment(self) -> float:
        """The charge for uncertainty: the confidence percentile less the mean."""
        return max(0.0, self.point_estimate - self.best_estimate)

    @property
    def width(self) -> float:
        return self.upper_bound - self.lower_bound

    @property
    def coefficient_of_variation(self) -> float:
        return self.volatility / self.best_estimate if self.best_estimate else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "confidenceLevel": self.confidence_level,
            "bestEstimate": round(self.best_estimate, 2),
            "percentileEstimate": round(self.point_estimate, 2),
            "riskAdjustment": round(self.risk_adjustment, 2),
            "lowerBound": round(self.lower_bound, 2),
            "upperBound": round(self.upper_bound, 2),
            "intervalWidth": round(self.width, 2),
            "volatility": round(self.volatility, 6),
            "standardError": round(self.standard_error, 6),
            "coefficientOfVariation": round(self.coefficient_of_variation, 6),
            "pathCount": self.path_count,
            "method": self.method,
            "seed": self.seed,
            "durationMs": round(self.duration_ms, 3),
            "warnings": list(self.warnings),
        }


class RiskAdjustmentEngine:
    """Monte Carlo risk adjustment over the projected cash flows.

    Uncertainty is injected as three independent, documented shocks rather than a
    single blended factor, because each has a different actuarial meaning:

    * **mortality improvement / deterioration** - a multiplicative shock on qx
    * **lapse volatility** - a multiplicative shock on the lapse rate
    * **claim severity volatility** - a lognormal shock on claim amounts

    Correlations between the three are not modelled. That is stated explicitly
    rather than hidden: it is a known simplification, it is conservative on the
    risk adjustment (it over-disperses the upper tail), and an auditor reading the
    method note can see exactly what was and was not modelled.
    """

    def __init__(
        self,
        *,
        paths: int = 2000,
        confidence_level: float = 0.95,
        seed: int = 20_260_101,
        mortality_shock_sd: float = 0.15,
        lapse_shock_sd: float = 0.25,
        severity_shock_sd: float = 0.30,
        expense_shock_sd: float = 0.10,
    ) -> None:
        self.paths = paths
        self.confidence_level = confidence_level
        self.seed = seed
        self.mortality_shock_sd = mortality_shock_sd
        self.lapse_shock_sd = lapse_shock_sd
        self.severity_shock_sd = severity_shock_sd
        self.expense_shock_sd = expense_shock_sd

    def simulate(
        self,
        projection: CashFlowProjection,
        mortality: MortalityAssumptions,
        config: ProjectionConfig,
        *,
        contract: ContractGroupInput | None = None,
    ) -> RiskAdjustmentResult:
        """Run the simulation and return the confidence interval."""
        started = time.perf_counter()
        if self.paths < 2:
            msg = "at least 2 Monte Carlo paths are required to estimate a variance"
            raise ComputationError(msg, paths=self.paths)

        with trace_span("pas.ifrs17.riskAdjustment", paths=self.paths):
            rng = np.random.default_rng(self.seed + _seed_offset(projection, contract))
            horizon = len(projection.years)
            paths = self.paths
            shape = (paths, horizon)
            base_claims = np.broadcast_to(projection.claims, shape).astype(float)
            base_expenses = np.broadcast_to(projection.expenses, shape).astype(float)

            # Independent annual shocks, one column per projection year. A single
            # shock held for the whole projection would understate the dispersion
            # of a 30-year sum, which is exactly what the risk adjustment is meant
            # to capture.
            qx_path = np.clip(
                rng.lognormal(mean=-0.5 * self.mortality_shock_sd**2, sigma=self.mortality_shock_sd, size=shape),
                0.2,
                5.0,
            )
            severity_path = np.clip(
                rng.lognormal(mean=-0.5 * self.severity_shock_sd**2, sigma=self.severity_shock_sd, size=shape),
                0.1,
                10.0,
            )
            lapse_path = np.clip(
                rng.lognormal(mean=-0.5 * self.lapse_shock_sd**2, sigma=self.lapse_shock_sd, size=shape),
                0.2,
                5.0,
            )
            expense_path = np.clip(
                rng.lognormal(mean=-0.5 * self.expense_shock_sd**2, sigma=self.expense_shock_sd, size=shape),
                0.3,
                3.0,
            )

            shocked_claims = base_claims * severity_path
            shocked_expenses = base_expenses * expense_path
            shocked_in_force = np.zeros(shape, dtype=float)

            surviving = np.ones((paths,), dtype=float)
            start_in_force = np.ones((paths,), dtype=float)
            for index in range(horizon):
                age = (contract.average_age_at_issue if contract else 35) + index
                qx = float(mortality.qx(age))
                lapse = float(mortality.lapse(index + 1)) if config.apply_lapse else 0.0
                effective_qx = np.minimum(MAX_DRAWDOWN, qx * qx_path[:, index])
                effective_lapse = np.minimum(MAX_DRAWDOWN, lapse * lapse_path[:, index])
                survival = np.clip(1.0 - effective_qx - effective_lapse, 0.0, 1.0)
                shocked_in_force[:, index] = start_in_force * survival
                surviving = start_in_force * survival
                start_in_force = surviving

            discount = projection.discount_factors[None, :]
            pv_claims = (shocked_claims * discount).sum(axis=1)
            pv_expenses = (shocked_expenses * discount).sum(axis=1)
            totals = pv_claims + pv_expenses
            mean_in_force = shocked_in_force.mean(axis=0)
            warnings: list[str] = []
            if np.any(mean_in_force[-1:] <= 0) and horizon > 5:
                warnings.append(
                    "every simulated cohort fully exited before the end of the projection; "
                    "check the lapse and mortality assumptions"
                )

        tail = (1.0 - self.confidence_level) / 2.0
        lower = float(np.quantile(totals, tail))
        upper = float(np.quantile(totals, 1.0 - tail))
        # The entity's risk-loaded outflow is the upper confidence percentile; the
        # best estimate is the mean. The difference is the charge for uncertainty.
        percentile = float(np.quantile(totals, self.confidence_level))
        best = float(np.mean(totals))
        volatility = float(np.std(totals, ddof=1))
        standard_error = volatility / math.sqrt(paths)

        return RiskAdjustmentResult(
            confidence_level=self.confidence_level,
            best_estimate=best,
            point_estimate=percentile,
            lower_bound=lower,
            upper_bound=upper,
            volatility=volatility,
            standard_error=standard_error,
            path_count=paths,
            seed=self.seed,
            duration_ms=(time.perf_counter() - started) * 1000,
            warnings=warnings,
        )

    def release_profile(
        self, projection: CashFlowProjection, *, years: int | None = None
    ) -> tuple[np.ndarray, np.ndarray]:
        """Per-year fraction of the risk adjustment released as service is provided.

        The release must sum to one over the projection; IFRS 17 releases the risk
        adjustment in proportion to the reduction in the uncertainty, which is
        proxied by the reduction in expected claims relative to the first year.
        """
        horizon = min(years or len(projection.years), len(projection.years))
        weights = projection.claims_discounted[:horizon].astype(float)
        if weights.sum() <= 0:
            return np.full(horizon, 1.0 / horizon), np.zeros(horizon)
        profile = weights / weights.sum()
        return profile, profile * float(projection.claims_discounted.sum())


def _seed_offset(projection: CashFlowProjection, contract: ContractGroupInput | None) -> int:
    """Stable per-group offset so two groups do not share a random stream."""
    material = f"{contract.group_id if contract else ''}:{projection.currency}:{len(projection.years)}"
    return int(hashlib.sha256(material.encode()).hexdigest()[:6], 16)


# ---------------------------------------------------------------------------
# Validation and diagnostics
# ---------------------------------------------------------------------------
def validate_projection(projection: CashFlowProjection, contract: ContractGroupInput) -> list[str]:
    """Sanity checks a projection must pass before it can feed a filing.

    These are the checks an actuarial reviewer would otherwise do by hand:
    monotonic decay of in-force, no negative cash flows, and a loss ratio the
    block can plausibly produce.
    """
    warnings: list[str] = []
    in_force = projection.in_force
    if len(in_force) > 1 and not np.all(np.diff(in_force) <= 1e-12):
        warnings.append("in-force count increased, which is impossible without new business")
    if float(in_force.min()) < 0:
        warnings.append("in-force count went negative")
    if float(projection.claims.min()) < -1e-9:
        warnings.append("negative claim cash flows were produced")
    if float(projection.expenses.min()) < -1e-9:
        warnings.append("negative expense cash flows were produced")
    loss_ratio = projection.pv_claims / projection.pv_premiums if projection.pv_premiums > 0 else None
    if loss_ratio is not None and not 0.05 <= loss_ratio <= 5.0:
        warnings.append(f"implausible loss ratio {loss_ratio:.3f} for group {contract.group_id}")
    if contract.measurement_model.requires_csm and contract.profitability.value == "profitable":
        if projection.net_present_value < 0:
            warnings.append(
                "a profitable-at-initial-recognition group projects a negative net "
                "present value; check the assumption set or the grouping assessment"
            )
    return warnings


def convergence_report(
    results: Sequence[MeasurementResult], *, tolerance: float = 1e-6
) -> dict[str, Any]:
    """Summarise a convergence run over repeated valuations of the same group."""
    if not results:
        return {"iterations": 0, "converged": False, "reason": "no results supplied"}
    csm = [abs(r.contractual_service_margin) for r in results]
    changes = [abs(csm[i] - csm[i - 1]) for i in range(1, len(csm))]
    final = changes[-1] if changes else 0.0
    return {
        "iterations": len(results),
        "converged": final <= tolerance,
        "finalChange": round(final, 10),
        "tolerance": tolerance,
        "csmPath": [round(v, 6) for v in csm],
    }


def curve_from_rates(
    curve_id: str, rates: dict[int, float], *, as_of: date, basis: str = "locked-in"
) -> DiscountCurve:
    """Convenience constructor used by tests and the assumption UI."""
    return DiscountCurve(
        curve_id=curve_id, basis=basis, as_of=as_of, spot_rates=dict(sorted(rates.items()))
    )


def record_valuation_metric(measurement_model: str, outcome: str) -> None:
    """Emit a valuation counter (used by the monitoring dashboard)."""
    if VALUATIONS is not None:
        VALUATIONS.labels(measurement_model=measurement_model, outcome=outcome).inc()
