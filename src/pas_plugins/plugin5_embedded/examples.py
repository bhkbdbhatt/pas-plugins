"""A worked distribution catalogue and partner.

Enough real data to drive the whole flow: a partner with tiered entitlements, and
three products with different characteristics - a simple term that can be bound
instantly, a whole life that cannot, and an annuity with investment risk. The
differences matter because they exercise different gates.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pas_plugins.plugin5_embedded.models import (
    DistributionProduct,
    DistributionProductCategory,
    DistributionProductStatus,
    KycResult,
    KycStatus,
    Partner,
    PartnerStatus,
    PartnerTier,
    ProductEntitlement,
    SalesChannel,
)

SIMPLE_TERM_RATES: dict[int, float] = {
    18: 0.60,
    30: 0.85,
    40: 1.45,
    50: 3.20,
    60: 7.10,
    70: 14.50,
}

WHOLE_LIFE_RATES: dict[int, float] = {
    18: 9.40,
    30: 9.80,
    40: 11.20,
    50: 14.60,
    60: 22.80,
    70: 41.00,
}

ANNUITY_RATES: dict[int, float] = {
    50: 240.00,
    60: 310.00,
    70: 480.00,
}


def demo_partner() -> Partner:
    """A gold-tier agency licensed in six states."""
    return Partner(
        partner_id="PARTNER-GOLD-001",
        legal_name="Harbour Life Agency LLC",
        display_name="Harbour Life",
        status=PartnerStatus.ACTIVE,
        tier=PartnerTier.GOLD,
        channel=SalesChannel.BROKER,
        country="US",
        annual_quota_policies=2_000,
        entitlements=[
            ProductEntitlement(
                product_id="DIST-TERM-SIMPLE",
                states=["CA", "NY", "TX", "FL", "OH", "PA"],
                min_tier=PartnerTier.STARTER,
            ),
            ProductEntitlement(
                product_id="DIST-WHOLE-001",
                states=["CA", "NY", "TX", "FL"],
                min_tier=PartnerTier.SILVER,
                commission_override_bps=1_300,
                max_face_amount=2_000_000.0,
            ),
            ProductEntitlement(
                product_id="DIST-ANNUITY-001",
                states=["CA", "NY"],
                min_tier=PartnerTier.GOLD,
                commission_override_bps=900,
            ),
        ],
        payout_account_masked="****4417",
        contact_email="partners@harbourlife.example",
        onboarded_at=datetime.now(UTC),
    )


def starter_partner() -> Partner:
    """A starter-tier partner, licensed only for the simple term product."""
    return Partner(
        partner_id="PARTNER-STARTER-002",
        legal_name="Solo Brokers Inc",
        display_name="Solo Brokers",
        status=PartnerStatus.ACTIVE,
        tier=PartnerTier.STARTER,
        channel=SalesChannel.DIGITAL_AGENCY,
        country="US",
        annual_quota_policies=50,
        entitlements=[
            ProductEntitlement(
                product_id="DIST-TERM-SIMPLE",
                states=["CA", "NY", "TX"],
                min_tier=PartnerTier.STARTER,
            )
        ],
        payout_account_masked="****9930",
        contact_email="hello@solobrokers.example",
        onboarded_at=datetime.now(UTC),
    )


def catalog() -> list[DistributionProduct]:
    """Three products with deliberately different gating."""
    return [
        DistributionProduct(
            product_id="DIST-TERM-SIMPLE",
            name="Simplified Term 20",
            carrier="Harbour Mutual Life",
            category=DistributionProductCategory.SIMPLE_TERM,
            status=DistributionProductStatus.LISTED,
            min_age=18,
            max_age=75,
            min_face_amount=25_000.0,
            max_face_amount=1_000_000.0,
            term_options_years=[20],
            rate_per_thousand_by_age=SIMPLE_TERM_RATES,
            base_commission_bps=1_100,
            states=["CA", "NY", "TX", "FL", "OH", "PA"],
            currency="USD",
            underwriting_class="simplified",
            instant_decision=True,
            requires_paramed_exam=False,
            marketing_summary=(
                "No medical exam up to $1m. Instant decision for applicants under 60."
            ),
            tags=["term", "instantDecision", "simplified"],
        ),
        DistributionProduct(
            product_id="DIST-WHOLE-001",
            name="Heritage Whole Life",
            carrier="Harbour Mutual Life",
            category=DistributionProductCategory.WHOLE,
            status=DistributionProductStatus.LISTED,
            min_age=18,
            max_age=80,
            min_face_amount=50_000.0,
            max_face_amount=5_000_000.0,
            term_options_years=[],
            rate_per_thousand_by_age=WHOLE_LIFE_RATES,
            base_commission_bps=1_300,
            states=["CA", "NY", "TX", "FL"],
            currency="USD",
            underwriting_class="paramed",
            instant_decision=False,
            requires_paramed_exam=True,
            marketing_summary="Lifetime coverage with guaranteed cash value. Medical exam required.",
            tags=["whole", "participating", "guaranteedValue"],
        ),
        DistributionProduct(
            product_id="DIST-ANNUITY-001",
            name="Harbour Income Annuity",
            carrier="Harbour Mutual Life",
            category=DistributionProductCategory.ANNUITY,
            status=DistributionProductStatus.LISTED,
            min_age=50,
            max_age=80,
            min_face_amount=10_000.0,
            max_face_amount=500_000.0,
            term_options_years=[],
            rate_per_thousand_by_age=ANNUITY_RATES,
            base_commission_bps=900,
            states=["CA", "NY"],
            currency="USD",
            underwriting_class="standard",
            instant_decision=True,
            requires_paramed_exam=False,
            marketing_summary="Tax-deferred income for the later years. Not market guaranteed.",
            tags=["annuity", "taxDeferred"],
        ),
    ]


def clear_kyc() -> KycResult:
    """A screening result showing the cleared path, for documentation and tests."""
    return KycResult(
        status=KycStatus.CLEAR,
        identity_verified=True,
        consent_recorded=True,
        screened_at=datetime.now(UTC),
    )


__all__ = [
    "ANNUITY_RATES",
    "SIMPLE_TERM_RATES",
    "WHOLE_LIFE_RATES",
    "catalog",
    "clear_kyc",
    "demo_partner",
    "starter_partner",
]