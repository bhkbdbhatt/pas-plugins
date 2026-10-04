"""Guardrails and compliance: the checks that catch configuration mistakes.

A product configured through a UI fails differently from one written in code, and
most of those failures are predictable. These are the predictable ones, grouped by
what they protect:

* **Structural** - does the definition hang together at all?
* **Rating** - do the bands partition the age range with no gaps or overlaps?
* **Compliance** - does it respect the constraints a regulator would check?
* **Economics** - does it price above its own cost?

Findings carry a severity and, where there is one, a remediation. A guardrail that
says only "invalid" makes a product author guess; one that says "age bands overlap
at 40-41" gets fixed in minutes.

The important design point: a blocker stops publication, a warning does not, and
the distinction is recorded on the published version. Guardrails that block
everything get switched off, and then they protect nothing.
"""

from __future__ import annotations

from pas_plugins.plugin4_productconfig.models import (
    ComplianceRule,
    GuardrailReport,
    ProductDefinition,
    QuoteRequest,
    RatingTable,
    RuleSeverity,
    Violation,
    ViolationCategory,
)
from pas_plugins.plugin4_productconfig.rating import RatingEngine

# A premium that leaves less than this fraction for margin is flagged. Carriers
# run thin margins on term deliberately, so this is a warning, not a blocker.
MIN_MARGIN_BPS_WARNING = 500

# Fields an eligibility rule may reference. Kept here so the guardrail and the
# resolver in rating.py cannot drift apart.
ALLOWED_RULE_FIELDS = frozenset(
    {"age", "faceAmount", "termYears", "state", "tobacco", "healthClass", "riskFactors"}
)


class ComplianceEngine:
    """Evaluates compliance rules against a prospective quote or definition."""

    def check_quote(self, definition: ProductDefinition, request: QuoteRequest) -> list[Violation]:
        """Run every applicable compliance rule against a quote request."""
        violations: list[Violation] = []
        for rule in definition.compliance_rules:
            if not rule.applies_in(request.state):
                continue
            violation = self._check(rule, request, definition)
            if violation is not None:
                violations.append(violation)
        return violations

    def _check(
        self, rule: ComplianceRule, request: QuoteRequest, definition: ProductDefinition
    ) -> Violation | None:
        kind = rule.kind
        threshold = rule.threshold

        if kind == "maxFaceAmount":
            if threshold is not None and request.face_amount > threshold:
                return self._violation(rule, f"face amount {request.face_amount:,.0f} exceeds {threshold:,.0f}")
            return None

        if kind == "minFaceAmount":
            if threshold is not None and request.face_amount < threshold:
                return self._violation(rule, f"face amount {request.face_amount:,.0f} is below {threshold:,.0f}")
            return None

        if kind == "minIssueAge":
            if threshold is not None and request.age < threshold:
                return self._violation(rule, f"issue age {request.age} is below the minimum {int(threshold)}")
            return None

        if kind == "maxIssueAge":
            if threshold is not None and request.age > threshold:
                return self._violation(rule, f"issue age {request.age} is above the maximum {int(threshold)}")
            return None

        if kind == "stateEligibility":
            if definition.states and request.state not in definition.states:
                return self._violation(
                    rule, f"state {request.state} is not licensed for product '{definition.name}'"
                )
            return None

        if kind == "rateToValue":
            return self._check_rate_to_value(rule, definition, request)

        if kind == "surrenderCharge":
            return self._check_surrender_charge(rule, definition, request)

        if kind == "minPremium":
            return self._check_min_premium(rule, definition, request)

        return Violation(
            rule_id=rule.rule_id,
            title=rule.name,
            severity=RuleSeverity.WARNING,
            category=ViolationCategory.COMPLIANCE,
            message=f"compliance rule '{rule.name}' has unrecognised kind '{kind}' and was not evaluated",
            location=f"complianceRules.{rule.rule_id}",
            remediation="Use a supported kind, or extend the compliance engine.",
        )

    def _check_rate_to_value(
        self, rule: ComplianceRule, definition: ProductDefinition, request: QuoteRequest
    ) -> Violation | None:
        """Compare the annual premium to face amount against a rate-to-value cap.

        Rate-to-value is a leverage test, not a pricing test: it asks whether the
        premium is proportionate to the death benefit. A product that fails it can
        still be sold in some jurisdictions, which is why the rule carries its own
        severity rather than always blocking.
        """
        cover = definition.cover(request.cover_id)
        table = definition.table(cover.rating_table_id) if cover else None
        if cover is None or table is None:
            return None
        band = table.band_for(request.age)
        if band is None:
            return None
        cap_bp = (rule.threshold or 100.0) / 100.0
        actual_bp = band.rate_per_thousand / 100.0
        if actual_bp > cap_bp:
            return self._violation(
                rule,
                f"rate {band.rate_per_thousand}/1000 implies {actual_bp:.1f}bp of face, "
                f"above the {cap_bp:.1f}bp limit",
            )
        return None

    def _check_surrender_charge(
        self, rule: ComplianceRule, definition: ProductDefinition, request: QuoteRequest
    ) -> Violation | None:
        """Percentage-based surrender charges are prohibited in many states."""
        if request.age <= 10:
            return None
        cover = definition.cover(request.cover_id)
        if cover is None:
            return None
        percentage_charges = [
            charge
            for charge in cover.charges
            if charge.amount > 0 and str(charge.frequency) == "annual" and charge.amount > 1.0
        ]
        if percentage_charges and rule.states:
            return self._violation(
                rule,
                f"cover '{cover.name}' levies an annual charge that may be percentage-based "
                f"after issue age 10",
            )
        return None

    def _check_min_premium(
        self, rule: ComplianceRule, definition: ProductDefinition, request: QuoteRequest
    ) -> Violation | None:
        cover = definition.cover(request.cover_id)
        table = definition.table(cover.rating_table_id) if cover else None
        if cover is None or table is None:
            return None
        band = table.band_for(request.age)
        if band is None:
            return None
        monthly = band.premium_for(request.face_amount, annual=False)
        minimum = rule.threshold
        if minimum is not None and monthly < minimum:
            return self._violation(
                rule, f"monthly premium {monthly:.2f} is below the minimum {minimum:.2f}"
            )
        return None

    @staticmethod
    def _violation(rule: ComplianceRule, detail: str) -> Violation:
        return Violation(
            rule_id=rule.rule_id,
            title=rule.name,
            severity=rule.severity,
            category=ViolationCategory.COMPLIANCE,
            message=f"{rule.message} ({detail})",
            location=f"complianceRules.{rule.rule_id}",
            remediation=f"Adjust the product or the rule. Citation: {rule.citation or 'internal policy'}.",
        )


class GuardrailEngine:
    """Structural, rating and economics checks over a whole definition."""

    def __init__(self, compliance: ComplianceEngine | None = None, allow_negative_margin: bool = False) -> None:
        self._compliance = compliance or ComplianceEngine()
        self._allow_negative_margin = allow_negative_margin

    def evaluate(self, definition: ProductDefinition) -> GuardrailReport:
        """Run every guardrail and return the findings."""
        report = GuardrailReport(product_id=definition.product_id, version=definition.version)
        for check in (
            self._check_structure,
            self._check_rating_tables,
            self._check_covers,
            self._check_eligibility_rules,
            self._check_benefits_and_charges,
            self._check_economics,
        ):
            report.violations.extend(check(definition))
        return report

    # -- structure ---------------------------------------------------------

    def _check_structure(self, definition: ProductDefinition) -> list[Violation]:
        findings: list[Violation] = []
        if not definition.states:
            findings.append(
                self._finding(
                    "GR-001",
                    "Product is offered in no states",
                    "A product with an empty state list cannot be quoted anywhere.",
                    "states",
                    "Add the states the product is licensed in.",
                    RuleSeverity.BLOCKER,
                    ViolationCategory.STRUCTURAL,
                )
            )
        if not definition.covers:
            findings.append(
                self._finding(
                    "GR-002",
                    "Product has no covers",
                    "A product must define at least one cover to be sellable.",
                    "covers",
                    "Add a cover.",
                    RuleSeverity.BLOCKER,
                    ViolationCategory.STRUCTURAL,
                )
            )
        if definition.effective_date is None:
            findings.append(
                self._finding(
                    "GR-003",
                    "No effective date set",
                    "Published products need an effective date for rate filings.",
                    "effectiveDate",
                    "Set the effective date before publishing.",
                    RuleSeverity.WARNING,
                    ViolationCategory.STRUCTURAL,
                )
            )
        duplicate_tags = {
            tag for tag in definition.tags if definition.tags.count(tag) > 1
        }
        if duplicate_tags:
            findings.append(
                self._finding(
                    "GR-004",
                    "Duplicate tags",
                    f"Tags {sorted(duplicate_tags)} appear more than once.",
                    "tags",
                    "Remove the duplicates.",
                    RuleSeverity.INFO,
                    ViolationCategory.STRUCTURAL,
                )
            )
        return findings

    # -- rating ------------------------------------------------------------

    def _check_rating_tables(self, definition: ProductDefinition) -> list[Violation]:
        findings: list[Violation] = []
        for table in definition.rating_tables:
            findings.extend(self._check_bands(table))
            findings.extend(self._check_cross_reference(table))
        return findings

    def _check_bands(self, table: RatingTable) -> list[Violation]:
        findings: list[Violation] = []
        bands = table.bands
        ordered = sorted(bands, key=lambda b: b.age_band.min_age)

        for index, band in enumerate(ordered):
            if not band.age_band.is_open:
                continue
            if index != len(ordered) - 1:
                findings.append(
                    self._finding(
                        "GR-101",
                        "Open-ended band is not last",
                        f"Band {band.age_band.span} in table '{table.name}' is open-ended "
                        f"but is followed by {ordered[index + 1].age_band.span}.",
                        f"ratingTables.{table.table_id}.bands.{index}",
                        "Move the open-ended band to the end or close it.",
                        RuleSeverity.BLOCKER,
                        ViolationCategory.RATING,
                    )
                )

        for previous, current in zip(ordered, ordered[1:]):
            if previous.age_band.max_age is None:
                continue
            if current.age_band.min_age < previous.age_band.max_age:
                findings.append(
                    self._finding(
                        "GR-102",
                        "Age bands overlap",
                        f"Bands {previous.age_band.span} and {current.age_band.span} overlap "
                        f"in table '{table.name}'.",
                        f"ratingTables.{table.table_id}.bands",
                        f"Set the later band to start at {previous.age_band.max_age} or later.",
                        RuleSeverity.BLOCKER,
                        ViolationCategory.RATING,
                    )
                )
            elif current.age_band.min_age > previous.age_band.max_age:
                findings.append(
                    self._finding(
                        "GR-103",
                        "Age bands leave a gap",
                        f"No band covers age {previous.age_band.max_age} to "
                        f"{current.age_band.min_age} in table '{table.name}'. "
                        "A quote in that range cannot be priced.",
                        f"ratingTables.{table.table_id}.bands",
                        f"Close the gap by starting the later band at {previous.age_band.max_age} "
                        "or earlier.",
                        RuleSeverity.BLOCKER,
                        ViolationCategory.RATING,
                    )
                )

        zero_rate_bands = [b for b in bands if b.rate_per_thousand == 0.0]
        if zero_rate_bands:
            findings.append(
                self._finding(
                    "GR-104",
                    "Zero premium band",
                    f"Table '{table.name}' has {len(zero_rate_bands)} band(s) priced at zero.",
                    f"ratingTables.{table.table_id}.bands",
                    "Confirm a zero rate is intended; free policies are rarely correct.",
                    RuleSeverity.WARNING,
                    ViolationCategory.RATING,
                )
            )
        return findings

    def _check_cross_reference(self, table: RatingTable) -> list[Violation]:
        findings: list[Violation] = []
        factor_ids = {factor.factor_id for factor in table.factors}
        for class_name, factor_id in table.cross_reference.items():
            if factor_id not in factor_ids:
                findings.append(
                    self._finding(
                        "GR-105",
                        "Cross-reference points at an unknown factor",
                        f"Class '{class_name}' references factor '{factor_id}', "
                        f"which table '{table.name}' does not define.",
                        f"ratingTables.{table.table_id}.crossReference.{class_name}",
                        "Define the factor or correct the reference.",
                        RuleSeverity.BLOCKER,
                        ViolationCategory.CONSISTENCY,
                    )
                )
        return findings

    # -- covers ------------------------------------------------------------

    def _check_covers(self, definition: ProductDefinition) -> list[Violation]:
        findings: list[Violation] = []
        for cover in definition.covers:
            table = definition.table(cover.rating_table_id)
            if table is None:
                continue
            band_low, band_high = table.covers
            if band_low > cover.min_issue_age:
                findings.append(
                    self._finding(
                        "GR-201",
                        "Rating table does not cover the full issue-age range",
                        f"Cover '{cover.name}' issues from age {cover.min_issue_age}, "
                        f"but table '{table.name}' starts at {band_low}.",
                        f"covers.{cover.cover_id}.ratingTableId",
                        "Extend the rating table or raise the cover's minimum issue age.",
                        RuleSeverity.BLOCKER,
                        ViolationCategory.RATING,
                    )
                )
            if band_high is not None and cover.max_issue_age > band_high:
                findings.append(
                    self._finding(
                        "GR-202",
                        "Rating table ends before the issue-age range",
                        f"Cover '{cover.name}' issues to age {cover.max_issue_age}, "
                        f"but table '{table.name}' ends at {band_high}.",
                        f"covers.{cover.cover_id}.ratingTableId",
                        "Extend the rating table or lower the cover's maximum issue age.",
                        RuleSeverity.BLOCKER,
                        ViolationCategory.RATING,
                    )
                )
        return findings

    # -- rules -------------------------------------------------------------

    def _check_eligibility_rules(self, definition: ProductDefinition) -> list[Violation]:
        findings: list[Violation] = []
        for cover in definition.covers:
            seen: set[str] = set()
            for rule in cover.eligibility:
                if rule.reason_code in seen:
                    findings.append(
                        self._finding(
                            "GR-301",
                            "Duplicate eligibility reason code",
                            f"Cover '{cover.name}' uses reason code '{rule.reason_code}' more than once, "
                            "so a decision cannot be attributed to one rule.",
                            f"covers.{cover.cover_id}.eligibility.{rule.rule_id}",
                            "Give each rule a distinct reason code.",
                            RuleSeverity.WARNING,
                            ViolationCategory.CONSISTENCY,
                        )
                    )
                seen.add(rule.reason_code)

                base_field = rule.field.split(".", 1)[0]
                if base_field not in ALLOWED_RULE_FIELDS:
                    findings.append(
                        self._finding(
                            "GR-302",
                            "Eligibility rule references an unknown field",
                            f"Rule '{rule.name}' references '{rule.field}', which the pricing "
                            f"engine does not resolve. The rule can never fire.",
                            f"covers.{cover.cover_id}.eligibility.{rule.rule_id}",
                            f"Use one of: {sorted(ALLOWED_RULE_FIELDS)}.",
                            RuleSeverity.BLOCKER,
                            ViolationCategory.STRUCTURAL,
                        )
                    )
        return findings

    def _check_benefits_and_charges(self, definition: ProductDefinition) -> list[Violation]:
        findings: list[Violation] = []
        for cover in definition.covers:
            if not cover.benefits:
                findings.append(
                    self._finding(
                        "GR-401",
                        "Cover has no benefits",
                        f"Cover '{cover.name}' pays nothing, which cannot be issued.",
                        f"covers.{cover.cover_id}.benefits",
                        "Add at least a death benefit.",
                        RuleSeverity.BLOCKER,
                        ViolationCategory.STRUCTURAL,
                    )
                )
            dead_benefits = [
                b for b in cover.benefits if str(b.category) == "death" and not b.guarantee_period_years
            ]
            if dead_benefits:
                findings.append(
                    self._finding(
                        "GR-402",
                        "Death benefit without a guarantee period",
                        f"Cover '{cover.name}' has a death benefit with no guarantee period stated.",
                        f"covers.{cover.cover_id}.benefits",
                        "State the guarantee period so the illustration can disclose it.",
                        RuleSeverity.INFO,
                        ViolationCategory.CONSISTENCY,
                    )
                )
            if not cover.charges:
                findings.append(
                    self._finding(
                        "GR-403",
                        "Cover has no charges",
                        f"Cover '{cover.name}' has no policy fee or charge, so "
                        "targetMarginBps will overstate profitability.",
                        f"covers.{cover.cover_id}.charges",
                        "Add at least a policy fee unless a free policy is intended.",
                        RuleSeverity.WARNING,
                        ViolationCategory.ECONOMICS,
                    )
                )
        return findings

    # -- economics ---------------------------------------------------------

    def _check_economics(self, definition: ProductDefinition) -> list[Violation]:
        findings: list[Violation] = []
        engine = RatingEngine()
        reference_age = max(30, definition.covers[0].min_issue_age + 1)
        reference_face = None
        for cover in definition.covers:
            reference_face = reference_face or (cover.min_face_amount + cover.max_face_amount) / 2.0

        for cover in definition.covers:
            table = definition.table(cover.rating_table_id)
            if table is None or reference_face is None:
                continue
            try:
                quote = engine.quote(
                    cover,
                    table,
                    QuoteRequest(
                        tenant_id="guardrail",
                        product_id=definition.product_id,
                        cover_id=cover.cover_id,
                        age=reference_age,
                        face_amount=reference_face,
                        term_years=cover.term_options_years[0] if cover.term_options_years else None,
                    ),
                )
            except ValueError:
                # The structural rules already report unpriceable configurations;
                # economics cannot be assessed on a cover that cannot be priced.
                continue

            if quote.margin_bps < 0 and not self._allow_negative_margin:
                findings.append(
                    self._finding(
                        "GR-501",
                        "Negative margin",
                        f"Cover '{cover.name}' prices {quote.margin_bps}bp of margin at age "
                        f"{reference_age}: premium does not cover its own charges.",
                        f"covers.{cover.cover_id}.charges",
                        "Reduce charges or increase the base rate.",
                        RuleSeverity.BLOCKER,
                        ViolationCategory.ECONOMICS,
                    )
                )
            elif quote.margin_bps < MIN_MARGIN_BPS_WARNING:
                findings.append(
                    self._finding(
                        "GR-502",
                        "Thin margin",
                        f"Cover '{cover.name}' prices {quote.margin_bps}bp of margin at age "
                        f"{reference_age}, below the {MIN_MARGIN_BPS_WARNING}bp warning level.",
                        f"covers.{cover.cover_id}.charges",
                        "Confirm this is intended; carriers run thin term margins deliberately.",
                        RuleSeverity.WARNING,
                        ViolationCategory.ECONOMICS,
                    )
                )

            if quote.margin_bps > definition.target_margin_bps + 1_000:
                findings.append(
                    self._finding(
                        "GR-503",
                        "Margin far above target",
                        f"Cover '{cover.name}' prices {quote.margin_bps}bp against a target of "
                        f"{definition.target_margin_bps}bp; the product may be over-priced.",
                        f"covers.{cover.cover_id}.ratingTableId",
                        "Review competitiveness against the market.",
                        RuleSeverity.INFO,
                        ViolationCategory.ECONOMICS,
                    )
                )
        return findings

    @staticmethod
    def _finding(
        rule_id: str,
        title: str,
        message: str,
        location: str,
        remediation: str,
        severity: RuleSeverity,
        category: ViolationCategory,
    ) -> Violation:
        return Violation(
            rule_id=rule_id,
            title=title,
            severity=severity,
            category=category,
            message=message,
            location=location,
            remediation=remediation,
        )


__all__ = [
    "ALLOWED_RULE_FIELDS",
    "ComplianceEngine",
    "GuardrailEngine",
    "MIN_MARGIN_BPS_WARNING",
]