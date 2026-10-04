"""Plugin 5 - Embedded Distribution.

Carriers have spent two decades building point-of-sale platforms that their
partners cannot use, and 67% of distribution still runs through a phone call and a
PDF. This plugin is the opposite bet: distribution as an API a partner embeds.

The design constraint that shapes everything: **this plugin holds no customer
money and issues no policy.** A partner integrating it is building their own
brand and their own customer relationship, so what they need is a licensed,
auditable set of primitives - a catalog they can render, a quote engine, an
onboarding flow with KYC, a payment intent, and a commission statement - and
nothing that obliges them to hand over their customer to the carrier.

Six capabilities, each addressable independently:

1. **Catalog** - products with partner-visible pricing, filtered by the partner's
   licence and the consumer's jurisdiction.
2. **Quotation** - a quote that names the partner, the commission basis and the
   premium, so both sides of the deal agree on what is being sold.
3. **Onboarding** - applicant capture with KYC/AML screening, consent capture and
   suitability checks before anything is offered.
4. **Payments** - payment intents and settlement against a quote, with webhook
   events for the partner to reconcile.
5. **Commissions** - the statement a partner earns, split by product and month,
   with clawback when a policy lapses inside the free-look period.
6. **Partner management** - licences, quotas and per-product entitlements.

The money-handling rule is the important one: a payment intent is a *request* to
move money, and a settlement is a *record that money moved*. Nothing here is a
ledger of record. A carrier's finance system remains the system of record; this
plugin's job is to make sure both sides see the same numbers at the same time.
"""

from __future__ import annotations

from pas_core.config import PluginSettings


class Plugin5Settings(PluginSettings):
    """Configuration for the embedded distribution platform."""

    plugin_id: str = "plugin5"
    plugin_version: str = "1.0.0"
    service_name: str = "pas-embedded-distribution"

    quote_ttl_minutes: int = 30
    """A quote is only good for as long as its premium is defensible."""
    default_currency: str = "USD"
    require_suitability_check: bool = True
    """Refuse to bind a policy without a suitability assessment on file."""
    require_kyc_before_bind: bool = True
    """Binding before KYC is how front-running happens. Off by default, hard to justify."""
    free_look_days: int = 60
    """Standard free-look period; commissions claw back inside it."""
    commission_haircut_bps: int = 0
    """Platform fee withheld from partner commission, in basis points."""
    webhook_secret_required: bool = True
    default_annual_quota_policies: int = 1_000
    sandbox_partner_ids: list[str] = ["PARTNER-SANDBOX"]
    """Partners that may exercise the full flow without a live payment rail."""
    documentation_url: str = "https://docs.pas-plugins.io/plugins/embedded"


__all__ = ["Plugin5Settings"]