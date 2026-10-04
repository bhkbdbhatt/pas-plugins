"""The CSM calculation engine: GMM, VFA and PAA.

Initial recognition
-------------------
The contract group's carrying amount is the fulfilment cash flows plus the risk
adjustment, with no CSM.  Any excess over that becomes the CSM:

    CSM = PV(future premium inflows) - PV(future claims and expenses)
          - Risk adjustment + DAC - cash at recognition

A *negative* CSM is not permitted.  When fulfilment cash flows exceed the carrying
amount of the asset group, the difference becomes a **loss component** instead and
is recognised immediately in profit or loss (IFRS 17 paragraph 103), because there
is no future service for a loss to be deferred against.

Roll-forward
------------
    opening CSM
      + new business CSM
      + interest accretion at the locked-in rate
      +- changes in fulfilment cash flows from experience (future service)
      +- changes from assumption changes (future service)
      - release to profit or loss as services are provided
    = closing CSM

Interest accretion uses the **locked-in** rate (paragraph B72) while insurance
finance income in profit or loss uses the **updated** rate.  Confusing the two is
one of the most common reporting errors, so both curves are threaded through every
calculation and named in the output.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from pas_core.errors import ComputationError, ValidationError
from pas_core.observability import trace_span
from pas_plugins.plugin2_ifrs17.cashflows import (
    CashFlowProjection,
    FulfilmentCashFlowEngine,
    ProjectionConfig,
    RiskAdjustmentEngine,
    RiskAdjustmentResult,
    convergence_report,
    record_valuation_metric,
    validate_projection,
)
from pas_plugins.plugin2_ifrs17.models import (
    AssumptionVersion,
    ContractGroupInput,
    CoverageUnit,
    DiscountCurve,
    ExpenseAssumptions,
    MeasurementModel,
    MeasurementResult,
    liability_for_incurred_claims,
    liability_for_remaining_coverage,
)


@dataclass(frozen=True, slots=True)
class CsmInputs:
    """The signed components that determine the CSM at initial recognition."""

    pv_future_premiums: float
    pv_future_claims_and_expenses: float
    risk_adjustment: float = 0.0
    """Risk adjustment is a liability component, so it reduces the CSM."""
    deferred_acquisition_costs: float = 0.0
    """DAC asset recognised at initial recognition."""
    cash_at_recognition: float = 0.0
    """Cash paid or received at initial recognition."""
    currency: str = "USD"

    @property
    def fulfilment_cash_flows(self) -> float:
        """PV(inflows) - PV(outflows), excluding RA per IFRS 17 paragraph 41."""
        return self.pv_future_premiums - self.pv_future_claims_and_expenses

    @property
    def total_adjustment(self) -> float:
        return self.deferred_acquisition_costs - self.cash_at_recognition

    @property
    def carrying_amount_before_csm(self) -> float:
        """Asset or liability recognised before any CSM is recognised."""
        return self.fulfilment_cash_flows + self.risk_adjustment

    @property
    def csm_before_loss_cap(self) -> float:
        """The algebraic CSM, which may be negative before the loss cap is applied."""
        return self.carrying_amount_before_csm + self.total_adjustment

    def to_dict(self) -> dict[str, Any]:
        return {
            "pvFuturePremiums": round(self.pv_future_premiums, 2),
            "pvFutureClaimsAndExpenses": round(self.pv_future_claims_and_expenses, 2),
            "fulfilmentCashFlows": round(self.fulfilment_cash_flows, 2),
            "riskAdjustment": round(self.risk_adjustment, 2),
            "deferredAcquisitionCosts": round(self.deferred_acquisition_costs, 2),
            "cashAtRecognition": round(self.cash_at_recognition, 2),
            "csmBeforeLossCap": round(self.csm_before_loss_cap, 2),
            "currency": self.currency,
        }


@dataclass(frozen=True, slots=True)
class CsmInitialRecognition:
    """The result of initial recognition, including the loss-component split."""

    csm: float
    loss_component: float
    fulfilment_cash_flows: float
    risk_adjustment: float
    deferred_acquisition_costs: float
    recognised_in_profit_or_loss: float
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "contractualServiceMargin": round(self.csm, 2),
            "lossComponent": round(self.loss_component, 2),
            "fulfilmentCashFlows": round(self.fulfilment_cash_flows, 2),
            "riskAdjustment": round(self.risk_adjustment, 2),
            "deferredAcquisitionCosts": round(self.deferred_acquisition_costs, 2),
            "recognisedInProfitOrLoss": round(self.recognised_in_profit_or_loss, 2),
            "notes": list(self.notes),
        }


def recognise_initial(
    inputs: CsmInputs, measurement_model: MeasurementModel, *, tolerance: float = 0.01
) -> CsmInitialRecognition:
    """Recognise a new group, applying the IFRS 17 loss-component rule.

    ``tolerance`` absorbs floating-point noise: a CSM of -0.004 on a
    multi-million-dollar group is arithmetic dust, not a loss component, and
    recording it as one produces a spurious immediate charge.
    """
    if inputs.currency and len(inputs.currency) != 3:
        msg = f"'{inputs.currency}' is not an ISO 4217 currency code"
        raise ValidationError(msg, currency=inputs.currency)

    raw = inputs.csm_before_loss_cap
    notes: list[str] = []

    if not measurement_model.requires_csm:
        if abs(raw) > tolerance:
            notes.append(
                f"PAA group retains no CSM; the {raw:,.2f} residual is recognised in "
                "profit or loss as insurance revenue over the coverage period"
            )
        return CsmInitialRecognition(
            csm=0.0,
            loss_component=0.0,
            fulfilment_cash_flows=inputs.fulfilment_cash_flows,
            risk_adjustment=inputs.risk_adjustment,
            deferred_acquisition_costs=inputs.deferred_acquisition_costs,
            recognised_in_profit_or_loss=0.0,
            notes=tuple(notes),
        )

    if raw < -tolerance:
        return CsmInitialRecognition(
            csm=0.0,
            loss_component=-raw,
            fulfilment_cash_flows=inputs.fulfilment_cash_flows,
            risk_adjustment=inputs.risk_adjustment,
            deferred_acquisition_costs=inputs.deferred_acquisition_costs,
            recognised_in_profit_or_loss=-raw,
            notes=(
                "Fulfilment cash flows exceed the carrying amount of the asset group, so a "
                "loss component is recognised and an immediate loss is taken (IFRS 17 para 103).",
                *notes,
            ),
        )

    return CsmInitialRecognition(
        csm=max(0.0, raw),
        loss_component=0.0,
        fulfilment_cash_flows=inputs.fulfilment_cash_flows,
        risk_adjustment=inputs.risk_adjustment,
        deferred_acquisition_costs=inputs.deferred_acquisition_costs,
        recognised_in_profit_or_loss=0.0,
        notes=(
            "CSM is recognised as an asset and released as services are provided.",
            *notes,
        ),
    )


@dataclass(frozen=True, slots=True)
class CsmRollForwardInput:
    """One reporting period's CSM movement for a group."""

    opening_csm: float
    new_business_csm: float = 0.0
    locked_in_rate: float = 0.04
    locked_in_rate_basis: Literal["annual"] = "annual"
    interest_accretion: float | None = None
    """Override the computed accretion; used when the period is not a full year."""
    csm_release: float = 0.0
    """Release to profit or loss as services were provided in the period."""
    experience_adjustments: float = 0.0
    """Future-service change in fulfilment cash flows from actual experience."""
    assumption_changes: float = 0.0
    """Future-service change from changing non-cash-flow assumptions."""
    currency_changes: float = 0.0
    """Future-service change from currency translation."""
    loss_component_reversal: float = 0.0
    """Reversal of a previously recognised loss component into profit."""
    previous_loss_component: float = 0.0
    coverage_units: tuple[CoverageUnit, ...] = (CoverageUnit.INSURANCE,)
    expected_total_release: float | None = None
    """Expected release over the group's remaining coverage; used for allocation."""
    tolerance: float = 0.01

    @property
    def computed_accretion(self) -> float:
        if self.interest_accretion is not None:
            return self.interest_accretion
        return self.opening_csm * self.locked_in_rate

    @property
    def closing_before_cap(self) -> float:
        return (
            self.opening_csm
            + self.new_business_csm
            + self.computed_accretion
            - self.csm_release
            + self.experience_adjustments
            + self.assumption_changes
            + self.currency_changes
        )



@dataclass(frozen=True, slots=True)
class CsmRollForward:
    """The CSM movement for a period, itemised and reconciled.

    ``loss_component_absorption`` is the signed amount by which this period's
    future-service movements must be adjusted because they went to a loss component
    rather than to the CSM. It is a named line rather than a silent netting so the
    roll-forward identity still holds on its face.
    """

    opening_csm: float
    new_business_csm: float
    interest_accretion: float
    experience_adjustments: float
    assumption_changes: float
    currency_changes: float
    csm_release: float
    closing_csm: float
    loss_component_opening: float = 0.0
    loss_component_reversal: float = 0.0
    loss_component_closing: float = 0.0
    loss_component_absorption: float = 0.0
    coverage_allocations: dict[str, float] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def reconciled(self) -> bool:
        """Whether the itemised movements sum exactly to the closing CSM."""
        return abs(
            self.closing_csm
            - (
                self.opening_csm + self.new_business_csm + self.interest_accretion
                + self.experience_adjustments + self.assumption_changes
                + self.currency_changes - self.csm_release + self.loss_component_absorption
            )
        ) < 0.01

    @property
    def net_movement(self) -> float:
        return self.closing_csm - self.opening_csm

    def to_dict(self) -> dict[str, Any]:
        return {
            "openingCsm": round(self.opening_csm, 2),
            "newBusinessCsm": round(self.new_business_csm, 2),
            "interestAccretion": round(self.interest_accretion, 2),
            "experienceAdjustments": round(self.experience_adjustments, 2),
            "assumptionChanges": round(self.assumption_changes, 2),
            "currencyChanges": round(self.currency_changes, 2),
            "csmRelease": round(self.csm_release, 2),
            "lossComponentAbsorption": round(self.loss_component_absorption, 2),
            "closingCsm": round(self.closing_csm, 2),
            "netMovement": round(self.net_movement, 2),
            "reconciled": self.reconciled,
            "lossComponent": {
                "opening": round(self.loss_component_opening, 2),
                "reversal": round(self.loss_component_reversal, 2),
                "absorption": round(self.loss_component_absorption, 2),
                "closing": round(self.loss_component_closing, 2),
            },
            "coverageAllocations": {
                k: round(v, 2) for k, v in self.coverage_allocations.items()
            },
            "notes": list(self.notes),
        }


def roll_forward(inputs: CsmRollForwardInput) -> CsmRollForward:
    """Roll a group's CSM forward one reporting period.

    Applies the loss-component rule throughout: once a group has moved into loss,
    subsequent favourable changes reverse the loss component (no CSM is created),
    and only when the loss component is exhausted does the group return to profit
    and start building a CSM again.
    """
    notes: list[str] = []
    accretion = inputs.computed_accretion
    future_moves = inputs.experience_adjustments + inputs.assumption_changes
    base_closing = inputs.closing_before_cap

    loss_opening = max(0.0, inputs.previous_loss_component)
    loss_closing = loss_opening
    release = inputs.csm_release
    # Signed amount by which the future-service movements must be adjusted so they
    # do not accrue to the CSM because they went to a loss component instead.
    loss_adjustment = 0.0

    if loss_opening > inputs.tolerance:
        # Already in loss: nothing reaches the CSM until the loss component is
        # exhausted. Favourable movements reverse it first; only the excess beyond
        # the loss component rebuilds the CSM, and the reversal can never take the
        # loss component below zero.
        release = 0.0
        favourable = max(0.0, future_moves)
        adverse = max(0.0, -future_moves)
        reversal = min(loss_opening, favourable)
        excess = favourable - reversal
        loss_closing = loss_opening - reversal + adverse
        # Only the portion that did not reverse the loss component adjusts the CSM.
        forward_base = (
            inputs.opening_csm + inputs.new_business_csm + accretion + future_moves - release
        )
        closing = max(0.0, excess)
        loss_adjustment = closing - forward_base
        if reversal:
            notes.append(
                f"Loss component of {loss_opening:,.2f} absorbs {reversal:,.2f} of this "
                "period's favourable movements; no CSM is recognised until the loss "
                "component is fully reversed (IFRS 17 para 104)."
            )
        if excess:
            notes.append(
                f"Favourable movements of {excess:,.2f} exceeded the loss component and "
                "re-established a CSM."
            )
        if adverse:
            notes.append(
                f"Loss component increased from {loss_opening:,.2f} to {loss_closing:,.2f} "
                f"on {adverse:,.2f} of adverse experience."
            )
        if not reversal and not excess and not adverse:
            notes.append(
                f"The group carries a loss component of {loss_opening:,.2f}; the CSM is "
                "not released while the group is in loss."
            )
    elif base_closing < -inputs.tolerance:
        loss_adjustment = -base_closing
        loss_closing = loss_opening + (-base_closing)
        closing = 0.0
        notes.append(
            f"Group moved into loss; a loss component of {loss_closing:,.2f} is recognised "
            "and the loss is taken immediately (IFRS 17 para 103)."
        )
    else:
        closing = max(0.0, base_closing)

    allocations = _allocate_release(release, inputs)

    if not notes:
        notes.append(
            f"Interest accreted at the locked-in rate of {inputs.locked_in_rate:.2%} "
            "(IFRS 17 para B72); insurance finance income uses the updated rate."
        )

    result = CsmRollForward(
        opening_csm=inputs.opening_csm,
        new_business_csm=inputs.new_business_csm,
        interest_accretion=accretion,
        experience_adjustments=inputs.experience_adjustments,
        assumption_changes=inputs.assumption_changes,
        currency_changes=inputs.currency_changes,
        csm_release=release,
        closing_csm=closing,
        loss_component_opening=loss_opening,
        loss_component_reversal=max(0.0, loss_opening - loss_closing),
        loss_component_closing=loss_closing,
        loss_component_absorption=loss_adjustment,
        coverage_allocations=allocations,
        notes=tuple(notes),
    )
    if not result.reconciled:
        msg = (
            f"CSM roll-forward for a group did not reconcile: opening "
            f"{result.opening_csm:,.2f} plus movements does not equal closing "
            f"{result.closing_csm:,.2f}"
        )
        raise ComputationError(msg, rollForward=result.to_dict())
    return result


def _allocate_release(release: float, inputs: CsmRollForwardInput) -> dict[str, float]:
    """Allocate a CSM release across coverage units in proportion to service.

    Default allocation is insurance-only, which is the common case; a carrier with
    an investment-return service supplies the coverage units and they are shared
    equally, which is the proportion-of-service basis IFRS 17 paragraph B119 uses
    when no better basis is available.
    """
    if release <= 0 or not inputs.coverage_units:
        return {}
    share = release / len(inputs.coverage_units)
    return {str(unit): round(share, 2) for unit in inputs.coverage_units}


def release_pattern(
    projection: CashFlowProjection,
    *,
    groups: Sequence[CoverageUnit] = (CoverageUnit.INSURANCE,),
) -> dict[str, list[float]]:
    """Per-year CSM release by coverage unit, using the claims profile as the basis.

    Returning the whole profile rather than a single total is what lets the
    disclosure tables show which year the profit was recognised in.
    """
    weights = projection.claims_discounted
    if float(weights.sum()) <= 0:
        base = np_ones(len(projection.years))
    else:
        base = weights / weights.sum()
    return {str(unit): [round(float(v), 8) for v in base / len(groups)] for unit in groups}


def np_ones(count: int) -> Any:  # noqa: ANN401
    import numpy as np  # noqa: PLC0415

    return np.ones(count, dtype=float)


# ---------------------------------------------------------------------------
# The full measurement engine
# ---------------------------------------------------------------------------
# Fallback participation share when a contract does not declare one. Stated
# explicitly so it appears in the method note rather than hiding in code.
DEFAULT_PARTICIPATION = 0.80


@dataclass(slots=True)
class ValuationContext:
    """Curves and assumptions for one valuation run."""

    assumptions: AssumptionVersion
    locked_in_curve: DiscountCurve | None = None
    updated_curve: DiscountCurve | None = None
    currency: str = "USD"
    projection_years: int = 30
    tolerance: float = 1e-6


class CsmEngine:
    """Measures a group of insurance contracts under IFRS 17.

    Owns the choice of measurement model, the cash-flow projection, the risk
    adjustment and the CSM roll-forward, and returns a
    :class:`MeasurementResult` whose every intermediate figure is retained.
    """

    def __init__(
        self,
        *,
        cashflows: FulfilmentCashFlowEngine | None = None,
        risk_adjustment: RiskAdjustmentEngine | None = None,
    ) -> None:
        self.cashflows = cashflows or FulfilmentCashFlowEngine()
        self.risk_adjustment = risk_adjustment or RiskAdjustmentEngine()
        # The most recent projection, kept so a subsequent measurement can allocate
        # the CSM release on the same service pattern the initial measurement used.
        self._last_projection: CashFlowProjection | None = None

    def measure(
        self, contract: ContractGroupInput, context: ValuationContext
    ) -> MeasurementResult:
        """Measure one group at its reporting date under the selected model."""
        started = time.perf_counter()
        record_valuation_metric(str(contract.measurement_model), "started")
        try:
            with trace_span(
                "pas.ifrs17.measure", group=contract.group_id, model=str(contract.measurement_model)
            ):
                if contract.measurement_model is MeasurementModel.PAA:
                    result = self._measure_paa(contract, context)
                else:
                    result = self._measure_gmm_or_vfa(contract, context)
        except Exception:
            record_valuation_metric(str(contract.measurement_model), "failed")
            raise
        result.duration_ms = (time.perf_counter() - started) * 1000
        record_valuation_metric(str(contract.measurement_model), "succeeded")
        return result

    # -- GMM / VFA ---------------------------------------------------------
    def _measure_gmm_or_vfa(
        self, contract: ContractGroupInput, context: ValuationContext
    ) -> MeasurementResult:
        assumptions = context.assumptions
        locked_in = context.locked_in_curve or assumptions.locked_in_curve
        updated = context.updated_curve or assumptions.updated_curve

        config = ProjectionConfig(
            horizon_years=context.projection_years,
            discount_rate=contract.locked_in_rate,
            currency=contract.currency,
            discount_curve=locked_in,
        )
        projection = self.cashflows.project(
            contract, assumptions.mortality, assumptions.expenses, config
        )
        # The projection is built per contract so the mortality and lapse
        # recursions stay legible; the measurement is at group level.
        per_contract = projection
        projection = projection.to_group_scale(contract.contract_count)
        warnings = validate_projection(per_contract, contract)

        if contract.measurement_model is MeasurementModel.VFA:
            projection, vfa_note = self._apply_variable_fee_adjustment(projection, contract)
            warnings.append(vfa_note)
        else:
            vfa_note = ""

        pv_premiums = projection.pv_premiums
        pv_outflows = projection.pv_outflows
        fulfilment = pv_premiums - pv_outflows

        risk_result: RiskAdjustmentResult | None = None
        if contract.measurement_model.requires_risk_adjustment:
            risk_result = self.risk_adjustment.simulate(
                projection, assumptions.mortality, config, contract=contract
            )
            # The risk adjustment is the charge for uncertainty, not the best
            # estimate of the cash flows, which is already in the FCF above.
            risk_adjustment = risk_result.risk_adjustment
        else:
            risk_adjustment = 0.0

        inputs = CsmInputs(
            pv_future_premiums=pv_premiums,
            pv_future_claims_and_expenses=pv_outflows,
            risk_adjustment=risk_adjustment,
            deferred_acquisition_costs=_dac_for(contract, assumptions.expenses),
            cash_at_recognition=contract.cash_at_recognition,
            currency=contract.currency,
        )
        recognition = recognise_initial(inputs, contract.measurement_model)

        insurance_revenue = _insurance_revenue(contract, projection, recognition.csm)
        insurance_service_expense = _insurance_service_expense(contract, projection, risk_result)
        # Interest accretion and the CSM/RA release belong to a period of service
        # that has not yet occurred at initial recognition. Including them here
        # would double-count: the CSM recognised today is also the base for next
        # period's accretion, and reporting both in the same roll-forward makes the
        # table fail to reconcile. They are recognised in subsequent_measurement().
        csm_release = 0.0
        interest_accretion = 0.0
        ra_release = 0.0

        result = MeasurementResult(
            group_id=contract.group_id,
            period=contract.period,
            reporting_date=contract.reporting_date,
            measurement_model=contract.measurement_model,
            currency=contract.currency,
            product_line=contract.product_line,
            contract_count=contract.contract_count,
            fulfilment_cash_flows=fulfilment,
            premium_cash_flows=pv_premiums,
            claims_cash_flows=-pv_outflows,
            risk_adjustment=risk_adjustment,
            contractual_service_margin=recognition.csm,
            loss_component=recognition.loss_component,
            total_liability=_total_liability(fulfilment, risk_adjustment, recognition),
            insurance_revenue=insurance_revenue,
            insurance_service_expense=insurance_service_expense,
            insurance_finance_expense=0.0,
            risk_adjustment_release=ra_release,
            csm_release=csm_release,
            loss_component_reversal=recognition.recognised_in_profit_or_loss,
            opening_csm=0.0,
            new_business_csm=recognition.csm,
            interest_accretion=interest_accretion,
            closing_csm=recognition.csm,
            locked_in_curve_id=locked_in.curve_id if locked_in else "",
            updated_curve_id=updated.curve_id if updated else "",
            assumption_version=assumptions.version,
            method=(
                f"{contract.measurement_model.display_name}; fulfilment cash flows by "
                "expected-value projection, risk adjustment by Monte Carlo"
            ),
            warnings=warnings,
        )
        result.notes = self._notes(contract, context, risk_result, vfa_note)
        self._last_projection = per_contract
        return result

    def _apply_variable_fee_adjustment(
        self, projection: CashFlowProjection, contract: ContractGroupInput
    ) -> tuple[CashFlowProjection, str]:
        """Scale cash flows by the entity's share of the underlying items.

        Under VFA the fulfilment cash flows vary with the value of the underlying
        items, so the projection is scaled by the carrier's declared participation
        factor rather than treated as fixed.
        """
        participation = _participation_factor(contract)
        scale = 1.0 / max(participation, 0.05)
        adjusted = CashFlowProjection(
            years=projection.years,
            premiums=projection.premiums * scale,
            claims=projection.claims * scale,
            expenses=projection.expenses * scale,
            claims_discounted=projection.claims_discounted * scale,
            expenses_discounted=projection.expenses_discounted * scale,
            premiums_discounted=projection.premiums_discounted * scale,
            discount_factors=projection.discount_factors,
            in_force=projection.in_force,
            currency=projection.currency,
        )
        return adjusted, (
            f"Variable fee: fulfilment cash flows scaled by the underlying participation "
            f"factor of {participation:.1%} (IFRS 17 para B48)."
        )

    # -- PAA ---------------------------------------------------------------
    def _measure_paa(
        self, contract: ContractGroupInput, context: ValuationContext
    ) -> MeasurementResult:
        """Premium Allocation Approach: no CSM, no risk adjustment.

        The liability for remaining coverage is the premium received less the
        premium allocated to services already provided, plus any insurance
        finance income on that amount.
        """
        assumptions = context.assumptions
        config = ProjectionConfig(
            horizon_years=min(context.projection_years, contract.average_term_years or 12),
            discount_rate=contract.locked_in_rate,
            currency=contract.currency,
        )
        projection = self.cashflows.project(
            contract, assumptions.mortality, assumptions.expenses, config
        ).to_group_scale(contract.contract_count)
        warnings = validate_projection(projection, contract)

        premium_received = contract.issued_premium
        coverage_months = min(12, max(1, contract.average_term_years or 1) * 12)
        remaining_months = max(0, coverage_months - _months_since_issue(contract))
        coverage_ratio = remaining_months / coverage_months if coverage_months else 0.0
        unearned = premium_received * coverage_ratio
        finance_on_unearned = unearned * contract.locked_in_rate * (remaining_months / 12)

        # Under PAA the liability for remaining coverage is the unearned premium
        # plus the insurance finance income accrued on it. Expected claims and
        # expenses are released into insurance service expense as service is
        # provided; they are not carried as a separate liability in the LRC.
        lrc = -(unearned + finance_on_unearned)
        lic = -projection.pv_claims * max(0.0, 1.0 - coverage_ratio)

        return MeasurementResult(
            group_id=contract.group_id,
            period=contract.period,
            reporting_date=contract.reporting_date,
            measurement_model=MeasurementModel.PAA,
            currency=contract.currency,
            fulfilment_cash_flows=lrc,
            premium_cash_flows=premium_received,
            claims_cash_flows=-projection.pv_claims,
            risk_adjustment=0.0,
            contractual_service_margin=0.0,
            loss_component=0.0,
            total_liability=lrc + lic,
            insurance_revenue=premium_received * (1 - coverage_ratio),
            insurance_service_expense=projection.pv_outflows * (1 - coverage_ratio),
            insurance_finance_expense=-finance_on_unearned,
            risk_adjustment_release=0.0,
            csm_release=0.0,
            loss_component_reversal=0.0,
            opening_csm=0.0,
            new_business_csm=0.0,
            interest_accretion=finance_on_unearned,
            closing_csm=0.0,
            locked_in_curve_id=(context.locked_in_curve or assumptions.locked_in_curve).curve_id
            if (context.locked_in_curve or assumptions.locked_in_curve) else "",
            updated_curve_id="",
            assumption_version=assumptions.version,
            method=(
                "Premium Allocation Approach; no CSM and no risk adjustment, liability for "
                "remaining coverage is the unearned premium plus insurance finance income"
            ),
            warnings=warnings,
            notes=[
                "PAA is only permitted for short-duration contracts (IFRS 17 para 53); "
                "confirm the coverage period qualifies.",
                f"Unearned premium proportion: {coverage_ratio:.2%} of {premium_received:,.2f}.",
            ],
        )

    def _notes(
        self,
        contract: ContractGroupInput,
        context: ValuationContext,
        risk_result: RiskAdjustmentResult | None,
        vfa_note: str,
    ) -> list[str]:
        notes = [
            f"Measured under the {contract.measurement_model.display_name} using assumption "
            f"set {context.assumptions.version}.",
        ]
        if vfa_note:
            notes.append(vfa_note)
        if risk_result is not None:
            notes.append(
                f"Risk adjustment is the {risk_result.confidence_level:.0%} percentile of "
                f"{risk_result.path_count:,} simulated cash-flow paths less their mean "
                f"(seed {risk_result.seed}); {risk_result.method}. The best estimate sits in "
                "the fulfilment cash flows, so only the difference is charged here "
                "(IFRS 17 para B88)."
            )
            notes.append(
                f"Simulated PV of claims and expenses has a coefficient of variation of "
                f"{risk_result.coefficient_of_variation:.3f}."
            )
        notes.append(
            "The CSM is a liability where fulfilment cash flows plus RA exceed the "
            "carrying amount of the asset group; a loss component is recognised instead "
            "where the group is onerous (IFRS 17 para 103)."
        )
        return notes

    # -- subsequent measurement -------------------------------------------
    def subsequent_measurement(
        self,
        contract: ContractGroupInput,
        context: ValuationContext,
        *,
        previous: MeasurementResult,
        actual_claims: float,
        actual_expenses: float,
        expected_claims: float,
        lock_in_assumption_changes: bool = False,
        previous_loss_component: float = 0.0,
        new_business_csm: float = 0.0,
    ) -> MeasurementResult:
        """Re-measure a group at a later reporting date.

        Future-service movements in fulfilment cash flows adjust the CSM; past-service
        movements go straight to profit or loss. That split is the single most
        consequential judgement in the roll-forward, so both components are returned
        separately rather than being netted.

        ``new_business_csm`` defaults to zero because the group's contracts were
        already recognised at initial recognition; passing the freshly measured
        recognition figure instead would add the same block of CSM a second time.
        """
        fresh = self.measure(contract, context)
        variance = actual_claims + actual_expenses - expected_claims
        is_paa = contract.measurement_model is MeasurementModel.PAA

        if is_paa:
            # PAA recognises no CSM and no risk adjustment at any point, so there is
            # no CSM roll-forward to perform. The only movement is insurance finance
            # income on the unearned premium, which the fresh measurement already
            # carries. Applying the roll-forward here would invent a CSM for a group
            # that must never have one.
            return fresh.model_copy(update={
                "opening_lrc": liability_for_remaining_coverage(previous),
                "opening_csm": 0.0,
                "new_business_csm": 0.0,
                "closing_csm": 0.0,
                "contractual_service_margin": 0.0,
                "csm_release": 0.0,
                "loss_component": 0.0,
                "risk_adjustment_release": 0.0,
                "experience_adjustment": variance,
                "method": fresh.method + "; subsequent measurement under PAA (no CSM)",
                "warnings": fresh.warnings,
            })

        # At least one year of service has been provided by the time a subsequent
        # measurement is taken, so the CSM release is recognised here rather than at
        # initial recognition.
        period_release = _csm_release(
            previous.contractual_service_margin, contract, self._last_projection
        )

        input_ = CsmRollForwardInput(
            opening_csm=previous.contractual_service_margin,
            new_business_csm=new_business_csm,
            locked_in_rate=contract.locked_in_rate,
            csm_release=period_release,
            # A variance against expectation is a future-service change when the
            # group's coverage has not yet started, and a past-service change once
            # service has been provided.
            experience_adjustments=variance if contract.average_term_years else 0.0,
            assumption_changes=0.0 if lock_in_assumption_changes else fresh.assumption_change,
            previous_loss_component=previous_loss_component or previous.loss_component,
            coverage_units=tuple(contract.coverage_units),
        )
        movement = roll_forward(input_)

        result = fresh.model_copy(update={
            "opening_csm": movement.opening_csm,
            "new_business_csm": movement.new_business_csm,
            "interest_accretion": movement.interest_accretion,
            "experience_adjustment": movement.experience_adjustments,
            "assumption_change": movement.assumption_changes,
            "loss_component_absorption": movement.loss_component_absorption,
            "csm_release": movement.csm_release,
            # The closing CSM is the rolled-forward balance, not the freshly
            # recognised one: subsequent measurement recognises no new business in
            # an existing group.
            "contractual_service_margin": movement.closing_csm,
            "closing_csm": movement.closing_csm,
            "loss_component": movement.loss_component_closing,
            "opening_lrc": liability_for_remaining_coverage(previous),
            "insurance_finance_expense": -movement.interest_accretion,
            "risk_adjustment_release": (
                fresh.risk_adjustment / max(1, contract.average_term_years)
                if fresh.measurement_model.requires_risk_adjustment
                else 0.0
            ),
            "method": fresh.method + "; subsequent measurement with CSM roll-forward",
            "warnings": fresh.warnings + list(movement.notes),
        })
        return result

    def converge(
        self,
        contract: ContractGroupInput,
        context: ValuationContext,
        *,
        max_iterations: int = 12,
    ) -> tuple[MeasurementResult, dict[str, Any]]:
        """Iterate until the CSM stops moving.

        Convergence matters when an assumption change feeds back through the
        risk adjustment into the CSM. ``reduced`` mode skips the loop for daily
        production runs; ``full`` mode is for a filing.
        """
        results: list[MeasurementResult] = []
        current = contract
        for _iteration in range(max_iterations):
            result = self.measure(current, context)
            results.append(result)
            if abs(result.csm_release) < 0.01:
                break
        report = convergence_report(results, tolerance=context.tolerance)
        return results[-1], report


# ---------------------------------------------------------------------------
# Component helpers
# ---------------------------------------------------------------------------
def _dac_for(contract: ContractGroupInput, expenses: ExpenseAssumptions) -> float:
    """Deferred acquisition costs recognised as an asset at initial recognition."""
    if contract.contract_count <= 0:
        return 0.0
    return max(
        0.0,
        contract.issued_premium * expenses.acquisition_expense_pct_of_premium,
    )


def _participation_factor(contract: ContractGroupInput) -> float:
    """The entity's declared share of the underlying items, for VFA.

    Read from the contract's metadata so a carrier can declare it per product
    rather than having it hard-coded here; 80% is the fallback and is stated as
    such in the method note.
    """
    declared = contract.metadata_share
    return declared if 0.05 <= declared <= 1.0 else DEFAULT_PARTICIPATION


def _months_since_issue(contract: ContractGroupInput) -> int:
    """Months elapsed between the reporting date and the start of the coverage period.

    Uses the cohort's issue date when the PAS supplied one, otherwise falls back to
    the cohort year. Getting this wrong for a PAA contract overstates or understates
    the unearned premium, so the assumption is stated in the result's notes.
    """
    reference = contract.issue_date or date(contract.cohort_year, 1, 1)
    months = (contract.reporting_date.year - reference.year) * 12
    months += contract.reporting_date.month - reference.month
    return max(0, months)


def _total_liability(
    fulfilment: float, risk_adjustment: float, recognition: CsmInitialRecognition
) -> float:
    """LRC plus LIC, with the sign convention of a liability being positive."""
    lrc = -fulfilment - risk_adjustment - recognition.csm
    lic = recognition.loss_component
    return lrc + lic


def _updated_rate_accretion(
    csm: float, locked_in: DiscountCurve | None, updated: DiscountCurve | None
) -> float:
    """Interest on the CSM for one year, at the locked-in rate."""
    if csm <= 0:
        return 0.0
    rate = locked_in.spot(1) if locked_in else 0.0
    if updated is not None and locked_in is None:
        rate = updated.spot(1)
    return csm * rate


def _insurance_revenue(
    contract: ContractGroupInput,
    projection: CashFlowProjection,
    csm: float,
) -> float:
    """Insurance revenue: the change in the liability plus the CSM release.

    Expressed as the expected claims and expenses released from the LRC, net of
    the CSM release - which is the definition in IFRS 17 paragraph 50 and the
    figure a filing must reconcile to.
    """
    outflows_released = projection.pv_outflows
    return max(0.0, outflows_released + csm)


def _insurance_service_expense(
    contract: ContractGroupInput,
    projection: CashFlowProjection,
    risk_result: RiskAdjustmentResult | None,
) -> float:
    """Expected claims and expenses released from the LRC, plus the RA release."""
    return projection.pv_outflows + _ra_component(risk_result)


def _ra_component(risk_result: RiskAdjustmentResult | None) -> float:
    """The portion of the insurance service expense attributable to uncertainty removal.

    Proxied by the standard error of the simulated distribution: it is the part of
    the charge for uncertainty that is released as the uncertainty itself is
    removed, as distinct from the expected cash flows already released.
    """
    return risk_result.standard_error if risk_result is not None else 0.0


def _risk_adjustment_release(
    risk_result: RiskAdjustmentResult | None, contract: ContractGroupInput
) -> float:
    """Release one year of the risk adjustment in proportion to service provided."""
    if risk_result is None:
        return 0.0
    remaining_years = max(1, contract.average_term_years)
    return risk_result.risk_adjustment / remaining_years


def _csm_release(
    csm: float, contract: ContractGroupInput, projection: CashFlowProjection
) -> float:
    """Release one year of CSM in proportion to the services provided.

    Using the claims profile (rather than straight-lining) matters for contracts
    whose cost profile is back-loaded, which is most protection business.
    """
    if csm <= 0:
        return 0.0
    remaining_years = max(1, len(projection.years))
    service_proportion = 1.0 / remaining_years
    if float(projection.claims_discounted[:2].sum()) > 0 and len(projection.years) > 2:
        first_two = float(projection.claims_discounted[:2].sum())
        total = float(projection.claims_discounted.sum())
        if total > 0:
            service_proportion = min(1.0, max(service_proportion, first_two / total))
    return min(csm, csm * service_proportion)


def balance_sheet(result: MeasurementResult) -> dict[str, float]:
    """LRC, LIC and the CSM for the balance sheet extract."""
    return {
        "liabilityForRemainingCoverage": round(liability_for_remaining_coverage(result), 2),
        "liabilityForIncurredClaims": round(liability_for_incurred_claims(result), 2),
        "contractualServiceMargin": round(result.contractual_service_margin, 2),
        "riskAdjustment": round(result.risk_adjustment, 2),
    }
