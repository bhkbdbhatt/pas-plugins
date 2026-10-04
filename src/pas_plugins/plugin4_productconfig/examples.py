"""A realistic worked example: a 20-year level term product.

This is what a product author would actually configure, and it doubles as the
plugin's smoke test. It is deliberately not a trivial case - it has three age
bands, a tobacco cross-reference, two covers, three compliance rules and a policy
fee - because those are the constructs the guardrails exist to check.
"""

from __future__ import annotations

from pas_plugins.plugin4_productconfig.models import (
    AgeBand,
    Benefit,
    BenefitCategory,
    Charge,
    ChargeFrequency,
    ComplianceRule,
    Cover,
    EligibilityRule,
    ProductCategory,
    ProductDefinition,
    RateBand,
    RatingFactor,
    RatingTable,
    RuleSeverity,
)


def demo_definition() -> ProductDefinition:
    """A configurable 20-year level term product."""
    base_table = RatingTable(
        table_id="tbl-term-base",
        name="Level term base rates",
        currency="USD",
        bands=[
            RateBand(
                age_band=AgeBand(min_age=18, max_age=30),
                rate_per_thousand=0.85,
                policy_fee=90.0,
            ),
            RateBand(
                age_band=AgeBand(min_age=30, max_age=45),
                rate_per_thousand=1.15,
                policy_fee=90.0,
            ),
            RateBand(
                age_band=AgeBand(min_age=45, max_age=60),
                rate_per_thousand=2.40,
                policy_fee=90.0,
            ),
            RateBand(
                age_band=AgeBand(min_age=60, max_age=75),
                rate_per_thousand=6.80,
                policy_fee=120.0,
            ),
        ],
        factors=[
            RatingFactor(
                factor_id="tobacco",
                name="Tobacco use",
                value=1.75,
                direction=RatingFactor.Direction.MULTIPLY,
            ),
            RatingFactor(
                factor_id="occupation",
                name="Hazardous occupation",
                value=1.25,
                direction=RatingFactor.Direction.MULTIPLY,
            ),
            RatingFactor(
                factor_id="preferred",
                name="Preferred class discount",
                value=0.90,
                direction=RatingFactor.Direction.MULTIPLY,
            ),
            RatingFactor(
                factor_id="residentFee",
                name="Non-resident fee",
                value=-150.0,
                direction=RatingFactor.Direction.ADD_BP,
            ),
        ],
        cross_reference={"tobaccoClass": "tobacco", "preferredClass": "preferred"},
    )

    death_benefit = Benefit(
        benefit_id="ben-death",
        name="Level death benefit",
        category=BenefitCategory.DEATH,
        trigger="Death of the insured during the term.",
        amount_mode="faceAmount",
        guarantee_period_years=20,
        description="Level face amount paid on death of the insured.",
    )
    rider = Benefit(
        benefit_id="ben-rider-waiver",
        name="Premium waiver rider",
        category=BenefitCategory.RIDERS,
        trigger="Total disability of the insured.",
        amount_mode="percentOfFace",
        amount_value=100.0,
        description="Waives premium while totally disabled.",
    )

    policy_fee = Charge(
        charge_id="chg-policy-fee",
        name="Policy fee",
        frequency=ChargeFrequency.ANNUAL,
        amount=90.0,
        deducted_from="premium",
    )

    # A rule states the condition that makes an applicant *fail* it. "Under 18" is
    # `lt 18`, not `gte 18` - the rule fires on applicants who do not qualify.
    eligibility = [
        EligibilityRule(
            rule_id="elig-age",
            name="Below minimum issue age",
            field="age",
            operator=EligibilityRule.Operator.LT,
            value=18,
            effect=EligibilityRule.Effect.INELIGIBLE,
            reason_code="BELOW_ISSUE_AGE",
            message="Applicants must be at least 18 at issue.",
        ),
        EligibilityRule(
            rule_id="elig-state",
            name="Unlicensed state",
            field="state",
            operator=EligibilityRule.Operator.IN,
            value=["AK", "HI", "ID", "MT", "WY"],
            effect=EligibilityRule.Effect.INELIGIBLE,
            reason_code="STATE_NOT_LICENSED",
            message="The product is not licensed in this state.",
        ),
        EligibilityRule(
            rule_id="elig-large-face",
            name="Refer large face amounts",
            field="faceAmount",
            operator=EligibilityRule.Operator.GT,
            value=5_000_000,
            effect=EligibilityRule.Effect.REFER,
            reason_code="LARGE_FACE_REVIEW",
            message="Face amounts above $5m require financial underwriter review.",
        ),
        EligibilityRule(
            rule_id="elig-child",
            name="Child rider age",
            field="age",
            operator=EligibilityRule.Operator.LT,
            value=15,
            effect=EligibilityRule.Effect.INELIGIBLE,
            reason_code="CHILD_RIDER_ISSUE",
            message="Applicants under 15 are not eligible for this cover.",
        ),
    ]

    term_cover = Cover(
        cover_id="cv-term-20",
        name="20-year level term",
        category=ProductCategory.TERM,
        min_face_amount=25_000.0,
        max_face_amount=10_000_000.0,
        min_issue_age=18,
        max_issue_age=70,
        term_options_years=[20],
        rating_table_id="tbl-term-base",
        benefits=[death_benefit, rider],
        charges=[policy_fee],
        eligibility=eligibility,
    )

    return ProductDefinition(
        product_id="PROD-TERM20",
        name="Level Term 20",
        version=1,
        category=ProductCategory.TERM,
        description=(
            "Level premium 20-year term life with a premium waiver rider. "
            "Configured without code through the low-code product service."
        ),
        currency="USD",
        covers=[term_cover],
        rating_tables=[base_table],
        compliance_rules=[
            ComplianceRule(
                rule_id="comp-face-cap",
                name="State face amount cap",
                citation="State adoption authority filing",
                kind="maxFaceAmount",
                states=["CA", "NY"],
                threshold=5_000_000.0,
                severity=RuleSeverity.BLOCKER,
                message="Face amount exceeds the state-adopted limit.",
            ),
            ComplianceRule(
                rule_id="comp-issue-age",
                name="Maximum issue age",
                citation="Product filing",
                kind="maxIssueAge",
                threshold=70.0,
                severity=RuleSeverity.BLOCKER,
                message="Issue age exceeds the filed maximum.",
            ),
            ComplianceRule(
                rule_id="comp-rate-to-value",
                name="Rate to value ceiling",
                citation="NAIC rate-to-value guidance",
                kind="rateToValue",
                threshold=1.00,
                severity=RuleSeverity.WARNING,
                message="Premium per unit of face is above the guideline level.",
            ),
        ],
        target_margin_bps=700,
        states=["CA", "NY", "TX", "FL", "OH", "PA"],
        author="product.team",
        tags=["term", "level", "configured"],
    )


def draft_with_low_rates() -> ProductDefinition:
    """A deliberately unviable variant, for exercising the economics guardrail."""
    definition = demo_definition()
    table = definition.rating_tables[0]
    for band in table.bands:
        band.rate_per_thousand = 0.05
        band.policy_fee = 240.0
    # A mortality cost charge large enough that no face amount in the cover's range
    # can carry it: the economics guardrail must refuse this on every band.
    definition.covers[0].charges.append(
        Charge(
            charge_id="chg-mortality-charge",
            name="Mortality cost charge",
            frequency=ChargeFrequency.MONTHLY,
            amount=95.0,
            deducted_from="premium",
        )
    )
    return definition


__all__ = ["demo_definition", "draft_with_low_rates"]


__all__ = ["demo_definition", "draft_with_low_rates"]