"""The distribution engine: catalog, quoting, KYC, binding and commission.

The sequence a policy takes through this plugin is deliberately the same sequence
it takes in a carrier, because a partner's integration should not have to learn a
different mental model than the business it sits inside:

    lead -> quote -> suitability -> KYC -> bind -> pay -> commission

Each gate exists for a reason, and the gate order is not arbitrary:

* **Suitability before KYC.** If the cover is unsuitable, there is no reason to
  collect identity documents for it.
* **KYC before bind.** Binding before identity is confirmed is how a policy is
  issued to someone who is not who they said they were.
* **Payment after bind.** The premium is a consequence of an issued policy, not a
  precondition for quoting one.

The money rule is absolute: nothing here is a ledger of record. Payment intents
record requests, payments record arrivals, and the carrier's finance system
remains authoritative. `reconcile()` exists so a partner and the carrier can
compare against the same figures rather than each guessing.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, date, datetime, timedelta

from pas_plugins.plugin5_embedded.models import (
    ApplicantProfile,
    Channel,
    CommissionEntry,
    CommissionStatement,
    CommissionStatus,
    DistributionProduct,
    KycResult,
    KycStatus,
    Lead,
    LeadStatus,
    Partner,
    PartnerStatus,
    PartnerTier,
    Payment,
    PaymentIntent,
    PaymentMethod,
    PaymentStatus,
    PepResult,
    Policy,
    PolicyStatus,
    Quote,
    QuoteLineItem,
    QuoteStatus,
    ReconciliationRow,
    SanctionsResult,
    SuitabilityAssessment,
    SuitabilityOutcome,
    WebhookEvent,
)
from pas_plugins.plugin5_embedded.settings import Plugin5Settings

# Coverage requested beyond this multiple of stated need triggers a conduct review.
OVER_INSURED_REVIEW_RATIO = 2.0
# Absolute ceiling on the over-insurance ratio before a quote needs manual review.
OVER_INSURED_BLOCK_RATIO = 4.0
# Face amounts above this need financial underwriting regardless of the product.
INSTANT_DECISION_FACE_LIMIT = 500_000.0


class DistributionError(ValueError):
    """Raised when a distribution operation cannot be performed."""


class DistributionEngine:
    """Catalog, quoting, screening, binding, payment and commission."""

    def __init__(self, settings: Plugin5Settings | None = None) -> None:
        self._settings = settings or Plugin5Settings()
        self._partners: dict[str, Partner] = {}
        self._products: dict[str, DistributionProduct] = {}
        self._leads: dict[str, Lead] = {}
        self._quotes: dict[str, Quote] = {}
        self._policies: dict[str, Policy] = {}
        self._intents: dict[str, PaymentIntent] = {}
        self._payments: dict[str, Payment] = {}
        self._kyc: dict[str, KycResult] = {}
        self._events: list[WebhookEvent] = []

    @property
    def settings(self) -> Plugin5Settings:
        return self._settings

    @property
    def partners(self) -> dict[str, Partner]:
        return dict(self._partners)

    @property
    def products(self) -> dict[str, DistributionProduct]:
        return dict(self._products)

    @property
    def quotes(self) -> dict[str, Quote]:
        return dict(self._quotes)

    def register_product(self, product: DistributionProduct) -> DistributionProduct:
        """Add a product to the distribution catalog.

        Products are loaded rather than hard-coded: a carrier lists what it
        actually sells, and the catalog is the surface a partner integrates with.
        """
        self._products[product.product_id] = product
        return product

    # -- partners and catalog ---------------------------------------------

    def register_partner(self, partner: Partner) -> Partner:
        """Onboard or update a partner. Re-registration is idempotent by identity."""
        existing = self._partners.get(partner.partner_id)
        if existing is not None and existing.status is PartnerStatus.TERMINATED:
            msg = f"partner '{partner.partner_id}' is terminated and cannot be re-onboarded"
            raise DistributionError(msg)
        self._partners[partner.partner_id] = partner
        return partner

    def get_partner(self, partner_id: str) -> Partner | None:
        return self._partners.get(partner_id)

    def list_catalog(
        self,
        partner_id: str,
        *,
        state: str | None = None,
        category: str | None = None,
        include_withdrawn: bool = False,
    ) -> list[DistributionProduct]:
        """Products this partner may sell, filtered by entitlement and jurisdiction.

        Entitlement is checked before status so a partner never learns a product
        exists that they are not licensed to sell.
        """
        partner = self._require_sellable_partner(partner_id)
        today = date.today()
        results: list[DistributionProduct] = []
        for product in self._products.values():
            if not include_withdrawn and not product.status.is_orderable:
                continue
            if category and str(product.category) != category:
                continue
            if state and not product.covers_state(state):
                continue
            if not self._entitled(partner, product, today, state):
                continue
            results.append(product)
        return sorted(results, key=lambda p: p.name)

    def entitlement_for(
        self, partner: Partner, product_id: str, when: date | None = None
    ) -> object | None:
        """The entitlement governing a partner/product pair, if any."""
        when = when or date.today()
        for entitlement in partner.entitlements:
            if entitlement.product_id == product_id and entitlement.is_active_on(when):
                return entitlement
        return None

    def _entitled(
        self, partner: Partner, product: DistributionProduct, when: date, state: str | None
    ) -> bool:
        entitlement = self.entitlement_for(partner, product.product_id, when)
        if entitlement is None:
            return False
        if _TIER_ORDER[PartnerTier(entitlement.min_tier)] > _TIER_ORDER[partner.tier]:
            return False
        if state and not entitlement.covers_state(state):
            return False
        if state and not product.covers_state(state):
            return False
        if entitlement.max_face_amount is not None and product.min_face_amount > entitlement.max_face_amount:
            return False
        return True

    # -- leads -------------------------------------------------------------

    def create_lead(self, lead: Lead) -> Lead:
        """Capture a prospect. The lead records who the consumer belongs to."""
        self._require_sellable_partner(lead.partner_id)
        self._leads[lead.lead_id] = lead
        return lead

    def convert_lead(self, lead_id: str) -> Lead:
        lead = self._leads.get(lead_id)
        if lead is None:
            msg = f"unknown lead '{lead_id}'"
            raise DistributionError(msg)
        if not lead.is_convertible:
            msg = f"lead '{lead_id}' is {lead.status} and cannot be converted"
            raise DistributionError(msg)
        lead.status = LeadStatus.CONVERTED
        return lead

    # -- quoting -----------------------------------------------------------

    def quote(
        self,
        *,
        tenant_id: str,
        partner_id: str,
        product_id: str,
        applicant: ApplicantProfile,
        face_amount: float,
        term_years: int | None = None,
        lead_id: str | None = None,
        channel: Channel = Channel.PARTNER_API,
    ) -> Quote:
        """Produce a partner-specific quote including their commission."""
        partner = self._require_sellable_partner(partner_id)
        product = self._products.get(product_id)
        if product is None:
            msg = f"unknown product '{product_id}'"
            raise DistributionError(msg)
        if not product.status.is_orderable:
            msg = f"product '{product.name}' is {product.status} and cannot be quoted"
            raise DistributionError(msg)

        entitlement = self.entitlement_for(partner, product_id)
        if entitlement is None:
            msg = f"partner '{partner_id}' is not licensed for product '{product.name}'"
            raise DistributionError(msg)
        if _TIER_ORDER[PartnerTier(entitlement.min_tier)] > _TIER_ORDER[partner.tier]:
            msg = (
                f"product '{product.name}' requires tier "
                f"{entitlement.min_tier}; partner is {partner.tier}"
            )
            raise DistributionError(msg)

        state = applicant.state_of_residence
        if not product.covers_state(state):
            msg = f"product '{product.name}' is not offered in {state}"
            raise DistributionError(msg)
        if not entitlement.covers_state(state):
            msg = f"partner '{partner_id}' is not licensed for product '{product.name}' in {state}"
            raise DistributionError(msg)
        if face_amount < product.min_face_amount or face_amount > product.max_face_amount:
            msg = (
                f"face amount {face_amount:,.0f} is outside the range "
                f"{product.min_face_amount:,.0f}-{product.max_face_amount:,.0f} for "
                f"'{product.name}'"
            )
            raise DistributionError(msg)
        if entitlement.max_face_amount is not None and face_amount > entitlement.max_face_amount:
            msg = (
                f"face amount {face_amount:,.0f} exceeds the partner's entitlement limit "
                f"of {entitlement.max_face_amount:,.0f}"
            )
            raise DistributionError(msg)

        band = product.age_band_for(applicant.age)
        if band is None:
            msg = (
                f"applicant age {applicant.age} is outside the issue range "
                f"{product.min_age}-{product.max_age} for '{product.name}'"
            )
            raise DistributionError(msg)

        if product.term_options_years:
            if term_years not in product.term_options_years:
                msg = (
                    f"term {term_years} is not offered by '{product.name}'; "
                    f"available terms are {product.term_options_years}"
                )
                raise DistributionError(msg)

        band_age, rate = band
        base_annual = face_amount / 1000.0 * rate
        tobacco_multiplier = 1.65 if applicant.is_smoker else 1.0
        annual_premium = base_annual * tobacco_multiplier

        commission_bps = entitlement.commission_bps(partner.tier)
        commission = round(annual_premium * commission_bps / 10_000.0, 2)
        platform_fee = round(
            annual_premium * self._settings.commission_haircut_bps / 10_000.0, 2
        )

        suitability = self._assess_suitability(applicant, face_amount, product)
        now = datetime.now(UTC)

        quote = Quote(
            quote_id=f"QT{uuid.uuid4().hex[:14]}",
            tenant_id=tenant_id,
            partner_id=partner_id,
            lead_id=lead_id,
            product_id=product_id,
            applicant_age=applicant.age,
            applicant_key=f"{applicant.first_name}|{applicant.last_name}|{applicant.date_of_birth}",
            face_amount=face_amount,
            term_years=term_years,
            state=state,
            currency=product.currency,
            base_rate_per_thousand=rate,
            annual_premium=annual_premium,
            monthly_premium=round(annual_premium / 12.0, 2),
            annual_commission=max(commission - platform_fee, 0.0),
            commission_bps=commission_bps,
            line_items=[
                QuoteLineItem(
                    label=f"Base premium (age band {band_age}, {rate}/1000)",
                    amount=base_annual,
                    kind="basePremium",
                ),
                QuoteLineItem(
                    label=f"Tobacco multiplier x{tobacco_multiplier:.2f}",
                    amount=round(annual_premium - base_annual, 2),
                    kind="factor",
                ),
                QuoteLineItem(
                    label=f"Partner commission at {commission_bps}bp",
                    amount=round(commission - platform_fee, 2),
                    kind="commission",
                ),
            ],
            suitability=suitability,
            disclosed_risks=self._disclosures(product, term_years, suitability),
            quoted_at=now,
            expires_at=now + timedelta(minutes=self._settings.quote_ttl_minutes),
        )

        self._quotes[quote.quote_id] = quote
        if lead_id and lead_id in self._leads:
            self._leads[lead_id].status = LeadStatus.QUOTED
        self._emit("quote.issued", partner_id, {"quoteId": quote.quote_id, "productId": product_id})
        return quote

    def _assess_suitability(
        self, applicant: ApplicantProfile, face_amount: float, product: DistributionProduct
    ) -> SuitabilityAssessment:
        """Test the cover against the consumer's own stated need.

        A consumer who has told us they need $200k of cover does not need $2m,
        and selling them the latter is a conduct problem even though the premium
        was correctly calculated.
        """
        reasons: list[str] = []
        outcome = SuitabilityOutcome.SUITABLE
        need = applicant.coverage_need
        ratio = (face_amount / need) if need > 0 else 0.0

        if need <= 0:
            reasons.append("noStatedNeed")
            outcome = SuitabilityOutcome.NEEDS_REVIEW
        elif ratio > OVER_INSURED_REVIEW_RATIO:
            reasons.append(f"coverageIs{ratio:.1f}xStatedNeed")
            outcome = (
                SuitabilityOutcome.NOT_SUITABLE
                if ratio > OVER_INSURED_BLOCK_RATIO
                else SuitabilityOutcome.NEEDS_REVIEW
            )
        if applicant.age < 18:
            reasons.append("applicantUnder18")
            outcome = SuitabilityOutcome.NOT_SUITABLE
        if applicant.dependents == 0 and applicant.annual_income < 25_000 and need > 0:
            reasons.append("noDependentsOrLowIncomeForWholeLife")
            if str(product.category) in {"whole", "universalLife", "indexedUniversalLife"}:
                outcome = SuitabilityOutcome.NEEDS_REVIEW
        if face_amount > INSTANT_DECISION_FACE_LIMIT and not product.instant_decision:
            reasons.append("aboveInstantDecisionLimit")
            if outcome is SuitabilityOutcome.SUITABLE:
                outcome = SuitabilityOutcome.NEEDS_REVIEW

        return SuitabilityAssessment(
            outcome=outcome,
            reasons=reasons or ["withinStatedNeed"],
            coverage_need=need,
            coverage_requested=face_amount,
        )

    @staticmethod
    def _disclosures(
        product: DistributionProduct, term_years: int | None, suitability: SuitabilityAssessment
    ) -> list[str]:
        """The risk disclosures a partner must show before the consumer accepts."""
        disclosures = [
            "Premiums are guaranteed for the term only while premiums are paid.",
            "Coverage lapses if premiums are not paid; it does not convert without a new contract.",
        ]
        if product.category in {"whole", "universalLife", "indexedUniversalLife"}:
            disclosures.append(
                "Permanent coverage carries surrender charges in the early policy years."
            )
        if product.category == "annuity":
            disclosures.append(
                "Annuity values are affected by market performance and are not guaranteed."
            )
        if term_years is None:
            disclosures.append("This product is not fixed term; it is flexible premium.")
        if suitability and suitability.outcome is not SuitabilityOutcome.SUITABLE:
            disclosures.append(
                "The requested coverage exceeds the consumer's stated need and needs review."
            )
        return disclosures

    # -- screening ---------------------------------------------------------

    def screen(self, quote_id: str, document_type: str, document_reference: str) -> KycResult:
        """Run identity, sanctions and PEP screening for a quote.

        The screening itself is simulated deterministically from the quote id, so
        the flow is demonstrable and reproducible. The *policy* it implements is
        the real one: consent must be recorded, a confirmed sanctions match is
        never a clear, and a potential match always needs a human.
        """
        quote = self._require_quote(quote_id)
        result = self._simulate_screening(quote)
        result.document_type = document_type
        result.document_reference = document_reference
        self._kyc[quote_id] = result
        self._emit(
            "kyc.screened",
            quote.partner_id,
            {"quoteId": quote_id, "status": str(result.status)},
        )
        return result

    def _simulate_screening(self, quote: Quote) -> KycResult:
        """Deterministic screening outcome for the *person*, not the request.

        Keying on the quote id would make the same consumer clear one day and land
        in review the next, which is both a fairness problem and an untestable one.
        Keying on the applicant's identity means two screenings of the same person
        always agree, and a test can reach either outcome by choosing a different
        applicant.
        """
        applicant = self._applicant_for(quote)
        identity = quote.applicant_key or (
            f"{applicant.first_name}|{applicant.last_name}|{applicant.date_of_birth}"
            if applicant
            else quote.quote_id
        )
        digest = hashlib.sha256(f"{identity}:{quote.partner_id}".encode()).digest()
        roll = int.from_bytes(digest[:8], "big") / float(1 << 64)

        if roll > 0.97:
            return KycResult(
                status=KycStatus.FAILED,
                sanctions_result=SanctionsResult.CONFIRMED_MATCH,
                pep_result=PepResult.CONFIRMED,
                identity_verified=False,
                findings=["confirmedSanctionsMatch", "politicallyExposedPerson"],
            )
        if roll > 0.92:
            return KycResult(
                status=KycStatus.REVIEW,
                sanctions_result=SanctionsResult.POTENTIAL_MATCH,
                pep_result=PepResult.POTENTIAL,
                identity_verified=True,
                findings=["potentialSanctionsMatchRequiresManualReview"],
            )
        if roll > 0.86:
            return KycResult(
                status=KycStatus.REVIEW,
                identity_verified=True,
                adverse_media_count=2,
                findings=["adverseMediaRequiresManualReview"],
            )
        return KycResult(
            status=KycStatus.CLEAR,
            identity_verified=True,
            consent_recorded=True,
            screened_at=datetime.now(UTC),
        )

    def kyc_for(self, quote_id: str) -> KycResult | None:
        return self._kyc.get(quote_id)

    # -- binding -----------------------------------------------------------

    def bind(self, quote_id: str) -> Policy:
        """Bind a policy from a quote, enforcing every gate in order."""
        quote = self._require_quote(quote_id)
        if not quote.is_bindable:
            if quote.is_expired:
                msg = f"quote {quote_id} expired at {quote.expires_at}; re-quote before binding"
            else:
                msg = f"quote {quote_id} is {quote.status} and cannot be bound"
            raise DistributionError(msg)

        partner = self._require_sellable_partner(quote.partner_id)
        if partner.annual_quota_policies and self._quota_used(partner.partner_id) >= partner.annual_quota_policies:
            msg = (
                f"partner '{partner.partner_id}' has reached its annual quota of "
                f"{partner.annual_quota_policies} policies"
            )
            raise DistributionError(msg)

        suitability = quote.suitability
        if self._settings.require_suitability_check:
            if suitability is None:
                msg = f"quote {quote_id} has no suitability assessment on file"
                raise DistributionError(msg)
            if suitability.outcome is SuitabilityOutcome.NOT_SUITABLE:
                msg = (
                    f"quote {quote_id} is not suitable for this consumer: "
                    f"{'; '.join(suitability.reasons)}"
                )
                raise DistributionError(msg)

        kyc = self._kyc.get(quote_id)
        if self._settings.require_kyc_before_bind:
            if kyc is None:
                msg = f"quote {quote_id} has no KYC result; screening must precede binding"
                raise DistributionError(msg)
            if not kyc.status.permits_bind:
                msg = f"KYC for quote {quote_id} is {kyc.status}, which does not permit binding"
                raise DistributionError(msg)

        applicant = self._applicant_for(quote)
        now = datetime.now(UTC)
        effective = date.today()
        policy = Policy(
            policy_id=f"POL{uuid.uuid4().hex[:14]}",
            quote_id=quote_id,
            partner_id=quote.partner_id,
            product_id=quote.product_id,
            holder_name=f"{applicant.first_name} {applicant.last_name}" if applicant else "Unknown",
            face_amount=quote.face_amount,
            annual_premium=quote.annual_premium,
            commission_bps=quote.commission_bps,
            status=PolicyStatus.PENDING_FREE_LOOK,
            effective_date=effective,
            free_look_expires=effective + timedelta(days=self._settings.free_look_days),
            bound_at=now,
            kyc_status=kyc.status if kyc else KycStatus.NOT_STARTED,
            suitability_outcome=suitability.outcome if suitability else SuitabilityOutcome.NEEDS_REVIEW,
            carrier_policy_reference=f"CAR-{uuid.uuid4().hex[:10].upper()}",
        )
        self._policies[policy.policy_id] = policy
        quote.status = QuoteStatus.CONVERTED
        self._emit(
            "policy.bound",
            quote.partner_id,
            {"policyId": policy.policy_id, "quoteId": quote_id},
        )
        return policy

    def _applicant_for(self, quote: Quote) -> ApplicantProfile | None:
        if not quote.lead_id:
            return None
        lead = self._leads.get(quote.lead_id)
        return lead.applicant if lead else None

    def _quota_used(self, partner_id: str) -> int:
        return sum(1 for p in self._policies.values() if p.partner_id == partner_id)

    def activate(self, policy_id: str) -> Policy:
        """Move a policy out of free look into active cover."""
        policy = self._policies.get(policy_id)
        if policy is None:
            msg = f"unknown policy '{policy_id}'"
            raise DistributionError(msg)
        if policy.status is not PolicyStatus.PENDING_FREE_LOOK:
            msg = f"policy '{policy_id}' is {policy.status} and cannot be activated"
            raise DistributionError(msg)
        if policy.in_free_look:
            msg = f"policy '{policy_id}' is still inside its free-look period"
            raise DistributionError(msg)
        policy.status = PolicyStatus.ACTIVE
        self._emit("policy.activated", policy.partner_id, {"policyId": policy_id})
        return policy

    def cancel(self, policy_id: str, reason: str) -> Policy:
        """Cancel a policy, clawing back commission if it is still in free look."""
        policy = self._policies.get(policy_id)
        if policy is None:
            msg = f"unknown policy '{policy_id}'"
            raise DistributionError(msg)
        if not policy.status.is_in_force:
            msg = f"policy '{policy_id}' is {policy.status} and cannot be cancelled"
            raise DistributionError(msg)
        in_free_look = policy.in_free_look
        policy.status = PolicyStatus.CANCELLED
        if in_free_look:
            self._emit(
                "commission.clawed_back",
                policy.partner_id,
                {"policyId": policy_id, "reason": reason},
            )
        self._emit("policy.cancelled", policy.partner_id, {"policyId": policy_id, "reason": reason})
        return policy

    # -- money -------------------------------------------------------------

    def create_payment_intent(
        self, quote_id: str, method: PaymentMethod = PaymentMethod.ACH
    ) -> PaymentIntent:
        """Ask for the first premium against a converted quote."""
        quote = self._require_quote(quote_id)
        if quote.status is not QuoteStatus.CONVERTED:
            msg = f"quote {quote_id} is {quote.status}; payment follows a bound policy"
            raise DistributionError(msg)

        now = datetime.now(UTC)
        intent = PaymentIntent(
            intent_id=f"PI{uuid.uuid4().hex[:14]}",
            quote_id=quote_id,
            partner_id=quote.partner_id,
            amount=quote.annual_premium,
            currency=quote.currency,
            method=method,
            status=PaymentStatus.REQUIRES_ACTION,
            created_at=now,
            expires_at=now + timedelta(days=14),
        )
        self._intents[intent.intent_id] = intent
        self._emit("payment.intent_created", quote.partner_id, {"intentId": intent.intent_id})
        return intent

    def capture_payment(
        self, intent_id: str, *, success: bool = True, amount: float | None = None
    ) -> Payment:
        """Record that money arrived against an intent."""
        intent = self._intents.get(intent_id)
        if intent is None:
            msg = f"unknown payment intent '{intent_id}'"
            raise DistributionError(msg)
        if intent.is_expired:
            msg = f"payment intent {intent_id} expired"
            raise DistributionError(msg)

        intent.attempts += 1
        if not success:
            intent.status = PaymentStatus.FAILED
            self._emit("payment.failed", intent.partner_id, {"intentId": intent_id})
            msg = f"payment intent {intent_id} was attempted and failed"
            raise DistributionError(msg)

        captured = amount if amount is not None else intent.amount
        if captured > intent.amount:
            msg = f"captured amount {captured} exceeds the intent of {intent.amount}"
            raise DistributionError(msg)

        intent.status = PaymentStatus.CAPTURED
        payment = Payment(
            payment_id=f"PAY{uuid.uuid4().hex[:14]}",
            intent_id=intent_id,
            quote_id=intent.quote_id,
            partner_id=intent.partner_id,
            amount=captured,
            currency=intent.currency,
            method=intent.method,
            status=PaymentStatus.CAPTURED,
            settlement_reference=f"STL-{uuid.uuid4().hex[:10].upper()}",
        )
        self._payments[payment.payment_id] = payment
        self._emit(
            "payment.captured",
            payment.partner_id,
            {"paymentId": payment.payment_id, "amount": payment.amount},
        )
        return payment

    def reconcile(self, partner_id: str) -> list[ReconciliationRow]:
        """Compare what was quoted, what was bound and what was collected.

        This is the reconciliation both sides can run. It is not a ledger; the
        carrier's finance system remains the system of record, and this exists so
        a partner is not told two different numbers.
        """
        rows: list[ReconciliationRow] = []
        for quote in self._quotes.values():
            if quote.partner_id != partner_id:
                continue
            collected = sum(
                payment.net_amount
                for payment in self._payments.values()
                if payment.quote_id == quote.quote_id
            )
            bound = quote.status is QuoteStatus.CONVERTED
            rows.append(
                ReconciliationRow(
                    quote_id=quote.quote_id,
                    product_id=quote.product_id,
                    premium=quote.annual_premium,
                    collected=collected,
                    outstanding=round(quote.annual_premium - collected, 2),
                    state="settled" if collected >= quote.annual_premium else ("bound" if bound else "quoted"),
                )
            )
        return rows

    # -- commission --------------------------------------------------------

    def commission_statement(
        self, tenant_id: str, partner_id: str, period_start: date, period_end: date
    ) -> CommissionStatement:
        """Build a partner's commission statement for a period.

        Commission on a policy still inside free look is *accrued* rather than
        payable. Presenting it as payable would invite a partner to book revenue
        that a cancellation will take back.
        """
        partner = self._partners.get(partner_id)
        if partner is None:
            msg = f"unknown partner '{partner_id}'"
            raise DistributionError(msg)

        entries: list[CommissionEntry] = []
        for policy in self._policies.values():
            if policy.partner_id != partner_id:
                continue
            written_on = policy.bound_at.date()
            if not (period_start <= written_on <= period_end):
                continue
            status = CommissionStatus.PAYABLE if policy.commission_payable else CommissionStatus.ACCRUED
            reason = None
            if policy.status is PolicyStatus.CANCELLED:
                status = CommissionStatus.CLAWED_BACK
                reason = "policy cancelled"
            entries.append(
                CommissionEntry(
                    entry_id=f"CE{uuid.uuid4().hex[:12]}",
                    policy_id=policy.policy_id,
                    product_id=policy.product_id,
                    written_on=written_on,
                    annual_premium=policy.annual_premium,
                    commission_bps=policy.commission_bps,
                    amount=policy.annual_commission(),
                    status=status,
                    clawback_reason=reason,
                )
            )

        return CommissionStatement(
            statement_id=f"CS{uuid.uuid4().hex[:14]}",
            tenant_id=tenant_id,
            partner_id=partner_id,
            period_start=period_start,
            period_end=period_end,
            entries=entries,
            settlement_due=date.today() + timedelta(days=partner.tier.settlement_days),
        )

    # -- lookups and events ------------------------------------------------

    def get_quote(self, quote_id: str) -> Quote | None:
        return self._quotes.get(quote_id)

    def get_policy(self, policy_id: str) -> Policy | None:
        return self._policies.get(policy_id)

    def get_payment(self, payment_id: str) -> Payment | None:
        return self._payments.get(payment_id)

    def get_intent(self, intent_id: str) -> PaymentIntent | None:
        return self._intents.get(intent_id)

    def policies_for(self, partner_id: str) -> list[Policy]:
        return [p for p in self._policies.values() if p.partner_id == partner_id]

    def events(self, partner_id: str | None = None) -> list[WebhookEvent]:
        return [e for e in self._events if partner_id is None or e.partner_id == partner_id]

    def _emit(self, event_type: str, partner_id: str, payload: dict[str, object]) -> WebhookEvent:
        event = WebhookEvent(
            event_id=f"EV{uuid.uuid4().hex[:14]}",
            event_type=event_type,
            partner_id=partner_id,
            payload=payload,
        )
        self._events.append(event)
        return event

    def deliver(self, event_id: str) -> WebhookEvent:
        """Mark an event as delivered to the partner."""
        event = next((e for e in self._events if e.event_id == event_id), None)
        if event is None:
            msg = f"unknown event '{event_id}'"
            raise DistributionError(msg)
        event.delivered = True
        event.delivered_at = datetime.now(UTC)
        return event

    # -- internals ---------------------------------------------------------

    def _require_sellable_partner(self, partner_id: str) -> Partner:
        partner = self._partners.get(partner_id)
        if partner is None:
            msg = f"unknown partner '{partner_id}'"
            raise DistributionError(msg)
        if not partner.status.may_sell:
            msg = f"partner '{partner_id}' is {partner.status} and may not sell"
            raise DistributionError(msg)
        return partner

    def _require_quote(self, quote_id: str) -> Quote:
        quote = self._quotes.get(quote_id)
        if quote is None:
            msg = f"unknown quote '{quote_id}'"
            raise DistributionError(msg)
        return quote


# Tier ordering, highest first. Index is the rank.
_TIER_ORDER: dict[PartnerTier, int] = {
    PartnerTier.PLATINUM: 4,
    PartnerTier.GOLD: 3,
    PartnerTier.SILVER: 2,
    PartnerTier.STARTER: 1,
}


__all__ = [
    "DistributionEngine",
    "DistributionError",
    "INSTANT_DECISION_FACE_LIMIT",
    "OVER_INSURED_BLOCK_RATIO",
    "OVER_INSURED_REVIEW_RATIO",
]