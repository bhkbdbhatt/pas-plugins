"""The rating engine: base bands, factors and eligibility evaluation.

Pricing a configured product is arithmetic over data, and the engine is fixed so
that a product author cannot smuggle behaviour into a definition. The three
properties that matter:

* **Itemised.** Every quote decomposes into base premium, each factor
  adjustment, and each charge. A premium nobody can decompose cannot be reviewed.
* **Band-explicit.** A quote always names the band it priced from. If the bands
  have a gap, the quote says so rather than silently falling back.
* **Order-independent for multiplication, ordered for basis points.** Multiplicative
  factors commute; additive basis-point offsets are applied in declaration order
  because the cross-reference table may intend a specific sequence.
"""

from __future__ import annotations

from typing import Any

from pas_plugins.plugin4_productconfig.models import (
    Cover,
    EligibilityRule,
    Quote,
    QuoteLine,
    QuoteRequest,
    RatingFactor,
    RatingTable,
    Violation,
)


class RatingError(ValueError):
    """Raised when a quote cannot be produced at all."""


class RatingEngine:
    """Prices a configured product and evaluates its eligibility rules."""

    def __init__(self, default_currency: str = "USD") -> None:
        self._default_currency = default_currency

    def quote(self, cover: Cover, table: RatingTable, request: QuoteRequest) -> Quote:
        """Price one quote against one cover.

        A request outside the cover's permitted ranges produces an *ineligible
        quote*, not an exception. An underwriter asking about a $10 application or
        a 16-year-old needs a decision they can record and explain; a raised error
        gives them a stack trace. Only a genuinely unpriceable request - no rating
        band covers the age - raises, because there is no answer to return.
        """
        out_of_range = self._out_of_range_reasons(cover, request)
        band = table.band_for(request.age)
        if band is None:
            if out_of_range:
                return self._ineligible_quote(request, out_of_range, band_label=None)
            msg = (
                f"rating table '{table.name}' has no band for age {request.age}; "
                f"it covers {self._describe_range(table)}"
            )
            raise RatingError(msg)
        if out_of_range:
            return self._ineligible_quote(request, out_of_range, band_label=band.age_band.span)

        base_annual = band.premium_for(request.face_amount, annual=True)
        lines: list[QuoteLine] = [
            QuoteLine(
                label=f"Base premium ({band.age_band.span}, {band.rate_per_thousand}/1000)",
                amount=base_annual,
                kind="basePremium",
                detail=(
                    f"{request.face_amount:,.0f} / 1000 x {band.rate_per_thousand}"
                    + (f" + {band.policy_fee} policy fee" if band.policy_fee else "")
                ),
            )
        ]

        premium_after_factors, adjustments = self._apply_factors(table, request, base_annual)
        lines.extend(adjustments)
        annual_premium = premium_after_factors

        annual_charges = 0.0
        for charge in cover.charges:
            lines.append(
                QuoteLine(
                    label=charge.name,
                    amount=charge.annualised(),
                    kind="charge",
                    detail=f"{charge.amount} {charge.frequency}, from {charge.deducted_from}",
                )
            )
            annual_charges += charge.annualised()

        reason_codes = self._evaluate_eligibility(cover, request)
        eligibility = self._eligibility_outcome(cover, request, reason_codes)

        return Quote(
            tenant_id=request.tenant_id,
            product_id=request.product_id,
            product_version=0,
            cover_id=cover.cover_id,
            annual_premium=annual_premium,
            monthly_premium=annual_premium / 12.0,
            face_amount=request.face_amount,
            age=request.age,
            currency=cover.currency,
            base_rate_per_thousand=band.rate_per_thousand,
            rating_band=band.age_band.span,
            lines=lines,
            annual_charges=annual_charges,
            margin_bps=self._margin_bps(annual_premium, annual_charges),
            eligibility=eligibility,
            reason_codes=reason_codes,
        )

    # -- internals ---------------------------------------------------------

    def _out_of_range_reasons(self, cover: Cover, request: QuoteRequest) -> list[str]:
        """Reasons the request falls outside what this cover will write.

        These become reason codes on an ineligible quote rather than exceptions,
        so the caller gets a recordable decision.
        """
        reasons: list[str] = []
        if request.face_amount < cover.min_face_amount:
            reasons.append("FACE_BELOW_MINIMUM")
        if request.face_amount > cover.max_face_amount:
            reasons.append("FACE_ABOVE_MAXIMUM")
        if request.age < cover.min_issue_age:
            reasons.append("BELOW_ISSUE_AGE")
        if request.age > cover.max_issue_age:
            reasons.append("ABOVE_ISSUE_AGE")
        if cover.is_fixed_term and request.term_years not in cover.term_options_years:
            reasons.append("TERM_NOT_OFFERED")
        return reasons

    def _ineligible_quote(
        self, request: QuoteRequest, reasons: list[str], band_label: str | None
    ) -> Quote:
        """A decision record for a request this cover will not write."""
        return Quote(
            tenant_id=request.tenant_id,
            product_id=request.product_id,
            cover_id=request.cover_id,
            annual_premium=0.0,
            monthly_premium=0.0,
            face_amount=request.face_amount,
            age=request.age,
            currency=self._default_currency,
            base_rate_per_thousand=0.0,
            rating_band=band_label or "notRated",
            lines=[
                QuoteLine(
                    label=f"Outside the permitted range for this cover: {', '.join(reasons)}",
                    amount=0.0,
                    kind="notPriced",
                    detail="No premium calculated; this request is not eligible for the cover.",
                )
            ],
            eligibility="ineligible",
            reason_codes=reasons,
        )

    @staticmethod
    def _apply_factors(
        table: RatingTable, request: QuoteRequest, base_annual: float
    ) -> tuple[float, list[QuoteLine]]:
        """Return the total multiplier and an itemised line per factor."""
        by_id = {factor.factor_id: factor for factor in table.factors}
        running = base_annual
        basis_points = 0.0
        lines: list[QuoteLine] = []

        for factor_id, value in request.risk_factors.items():
            factor = by_id.get(factor_id)
            if factor is None:
                lines.append(
                    QuoteLine(
                        label=f"Unknown factor '{factor_id}'",
                        amount=0.0,
                        kind="factorAdjustment",
                        detail="Not defined in this rating table; no adjustment applied.",
                    )
                )
                continue
            applied = factor.value * value
            if factor.direction == RatingFactor.Direction.MULTIPLY:
                # Each line reports what *this* factor cost, by differencing the
                # running premium. That keeps the itemisation honest when several
                # multipliers combine.
                before = running
                running *= applied
                lines.append(
                    QuoteLine(
                        label=f"{factor.name} x{applied:.4f}",
                        amount=running - before,
                        kind="factorAdjustment",
                        detail=f"factor '{factor.factor_id}'",
                    )
                )
            else:
                basis_points += applied
                lines.append(
                    QuoteLine(
                        label=f"{factor.name} {applied:+.0f}bp",
                        amount=0.0,
                        kind="factorAdjustment",
                        detail="Applied to the premium after multipliers.",
                    )
                )

        if basis_points:
            before = running
            running *= 1.0 + basis_points / 10_000.0
            lines.append(
                QuoteLine(
                    label=f"Total basis-point adjustment {basis_points:+.0f}bp",
                    amount=running - before,
                    kind="factorAdjustment",
                    detail="Sum of additive factors.",
                )
            )

        return running, lines

    @staticmethod
    def _evaluate_eligibility(cover: Cover, request: QuoteRequest) -> list[str]:
        """Return the reason codes of every rule that fired."""
        fired: list[str] = []
        for rule in cover.eligibility:
            if _rule_matches(rule, request):
                fired.append(rule.reason_code)
        return fired

    @staticmethod
    def _eligibility_outcome(cover: Cover, request: QuoteRequest, reason_codes: list[str]) -> str:
        """Reduce the fired rules to a single outcome, worst rule winning."""
        effects = {
            rule.reason_code: rule.effect
            for rule in cover.eligibility
            if _rule_matches(rule, request)
        }
        if any(effect == EligibilityRule.Effect.INELIGIBLE for effect in effects.values()):
            return "ineligible"
        if any(effect == EligibilityRule.Effect.REFER for effect in effects.values()):
            return "refer"
        return "eligible"

    @staticmethod
    def _margin_bps(annual_premium: float, annual_charges: float) -> int:
        """Gross margin in basis points: premium less charges, over premium.

        Margin here is a pricing-configuration margin, not an actuarial one - it
        excludes benefits, which is why loss ratio is reported separately.
        """
        if annual_premium <= 0:
            return -10_000
        return int(round((annual_premium - annual_charges) / annual_premium * 10_000.0))

    @staticmethod
    def _describe_range(table: RatingTable) -> str:
        spans = [band.age_band.span for band in table.bands]
        return f"{spans[0]} to {spans[-1]}"


def _rule_matches(rule: EligibilityRule, request: QuoteRequest) -> bool:
    """Evaluate one declarative rule against a quote request.

    Only the fixed operator set is honoured. An unknown field or a value of the
    wrong shape makes the rule *not* match rather than raising: a misconfigured
    rule must not silently make a case ineligible, and must not take pricing down.
    The structural guardrails flag the misconfiguration separately.
    """
    actual = _resolve_field(rule.field, request)
    if actual is None:
        return False

    operator = rule.operator
    expected = rule.value
    try:
        if operator == EligibilityRule.Operator.EQ:
            return actual == expected
        if operator == EligibilityRule.Operator.NE:
            return actual != expected
        if operator == EligibilityRule.Operator.IN:
            return actual in expected
        if operator == EligibilityRule.Operator.NOT_IN:
            return actual not in expected
        if operator == EligibilityRule.Operator.BETWEEN:
            return expected[0] <= actual <= expected[1]
        if actual is None or not isinstance(actual, (int, float)):
            return False
        if operator == EligibilityRule.Operator.GT:
            return actual > expected
        if operator == EligibilityRule.Operator.GTE:
            return actual >= expected
        if operator == EligibilityRule.Operator.LT:
            return actual < expected
        if operator == EligibilityRule.Operator.LTE:
            return actual <= expected
    except TypeError:
        return False
    return False


# Dotted rule-field paths mapped to the attribute that supplies each value.
#
# This is an explicit table rather than a getattr on the path. A rule names fields
# in camelCase because that is what an author sees in the UI, while the model is
# snake_case; resolving with getattr would silently return None for every field
# except the ones that happen to match, and a rule that never fires is a rule that
# quietly stops enforcing an underwriting constraint.
_RULE_FIELDS: dict[str, str] = {
    "age": "age",
    "faceAmount": "face_amount",
    "termYears": "term_years",
    "state": "state",
    "tobacco": "tobacco",
    "healthClass": "health_class",
    "riskFactors": "risk_factors",
}


def _resolve_field(path: str, request: QuoteRequest) -> Any:
    """Resolve the dotted field paths eligibility rules may reference.

    The set is closed on purpose. An open resolver would let a rule read anything
    on the request object, which is how a configuration surface turns into an
    injection point.
    """
    attribute = _RULE_FIELDS.get(path)
    if attribute is not None:
        return getattr(request, attribute, None)
    if path.startswith("riskFactors."):
        return request.risk_factors.get(path.split(".", 1)[1])
    return None


def guardrail_violation(
    rule_id: str,
    title: str,
    message: str,
    location: str,
    remediation: str,
    severity: str = "blocker",
) -> Violation:
    """Build a Violation from the guardrail module."""
    from pas_plugins.plugin4_productconfig.models import RuleSeverity, ViolationCategory  # noqa: PLC0415

    return Violation(
        rule_id=rule_id,
        title=title,
        severity=RuleSeverity(severity),
        category=ViolationCategory.CONSISTENCY,
        message=message,
        location=location,
        remediation=remediation,
    )


__all__ = ["RatingEngine", "RatingError", "guardrail_violation"]