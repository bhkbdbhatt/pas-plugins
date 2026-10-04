"""The product configuration service: draft, validate, simulate, publish.

This is the boundary a carrier integrates against. The lifecycle is deliberate and
strict, because the failure mode of a low-code surface is a half-configured
product reaching a policyholder:

    draft  ->  validate  ->  simulate  ->  publish  ->  retire

* Only a draft is mutable. Publishing produces an immutable version with a content
  hash; a published version cannot be edited, only superseded.
* Publication is blocked while any guardrail blocker stands, unless the carrier
  has explicitly disabled compliance - and that choice is recorded on the version
  itself, so the audit trail shows who accepted the risk.
* Quoting always names a product version. A quote that could silently pick up a new
  rate tomorrow is not a quote.

Simulation exists so the economics are checked before publication rather than
discovered in the first month of new business.
"""

from __future__ import annotations

import hashlib
import json
import random
from datetime import UTC, datetime, timedelta

from pas_plugins.plugin4_productconfig.compliance import ComplianceEngine, GuardrailEngine
from pas_plugins.plugin4_productconfig.diffing import diff_definitions
from pas_plugins.plugin4_productconfig.models import (
    GuardrailReport,
    ProductDefinition,
    ProductStatus,
    PublishedVersion,
    Quote,
    QuoteRequest,
    SimulationProfile,
    SimulationResult,
    VersionDiff,
)
from pas_plugins.plugin4_productconfig.rating import RatingEngine, RatingError
from pas_plugins.plugin4_productconfig.settings import Plugin4Settings

# Assumed benefit cost per thousand of face, used only to compute a simulation
# loss ratio. A carrier replaces this with its own mortality table; the plugin
# reports it explicitly so nobody mistakes it for an actuarial result.
ASSUMED_BENEFIT_COST_PER_THOUSAND = 1.20


class ProductConfigError(ValueError):
    """Raised when an operation is not permitted in the current lifecycle state."""


class ProductConfigService:
    """Owns product definitions, their versions, quotes and simulations."""

    def __init__(
        self,
        settings: Plugin4Settings | None = None,
        *,
        guardrails: GuardrailEngine | None = None,
        compliance: ComplianceEngine | None = None,
    ) -> None:
        self._settings = settings or Plugin4Settings()
        self._compliance = compliance or ComplianceEngine()
        self._guardrails = guardrails or GuardrailEngine(
            self._compliance, allow_negative_margin=self._settings.allow_negative_margin
        )
        self._rating = RatingEngine()
        self._drafts: dict[str, ProductDefinition] = {}
        self._published: dict[str, list[PublishedVersion]] = {}
        self._quotes: dict[str, Quote] = {}

    # -- lifecycle ---------------------------------------------------------

    def save_draft(self, definition: ProductDefinition) -> ProductDefinition:
        """Create or update a draft. Published versions are never touched."""
        if definition.status is not ProductStatus.DRAFT:
            msg = (
                f"cannot save a definition with status '{definition.status}'; "
                "only drafts are mutable"
            )
            raise ProductConfigError(msg)

        latest_published = self._published.get(definition.product_id) or []
        if latest_published and definition.version < latest_published[-1].version:
            msg = (
                f"version {definition.version} is behind published version "
                f"{latest_published[-1].version}; reload before editing"
            )
            raise ProductConfigError(msg)
        if definition.product_id not in self._published and len(self._drafts) >= self._settings.max_products_per_tenant:
            msg = f"tenant has reached its limit of {self._settings.max_products_per_tenant} products"
            raise ProductConfigError(msg)

        definition.updated_at = datetime.now(UTC)
        self._drafts[definition.product_id] = definition
        return definition

    def get_draft(self, product_id: str) -> ProductDefinition | None:
        return self._drafts.get(product_id)

    def get_published(self, product_id: str, version: int | None = None) -> PublishedVersion | None:
        """Fetch a published version, or the latest when no version is given."""
        versions = self._published.get(product_id)
        if not versions:
            return None
        if version is None:
            return versions[-1]
        return next((v for v in versions if v.version == version), None)

    def list_published(self, product_id: str) -> list[PublishedVersion]:
        return list(self._published.get(product_id, []))

    def validate(self, definition: ProductDefinition) -> GuardrailReport:
        """Run every guardrail against a definition without changing anything."""
        return self._guardrails.evaluate(definition)

    def publish(self, product_id: str, actor: str) -> PublishedVersion:
        """Publish the current draft as an immutable, hashed version."""
        draft = self._drafts.get(product_id)
        if draft is None:
            msg = f"no draft for product '{product_id}'"
            raise ProductConfigError(msg)

        report = self.validate(draft)
        if self._settings.require_compliance_pass and not report.can_publish:
            blockers = "; ".join(f"{v.rule_id}: {v.title}" for v in report.blockers)
            msg = f"cannot publish '{product_id}' while blockers stand - {blockers}"
            raise ProductConfigError(msg)

        existing = self._published.get(product_id, [])
        new_version = len(existing) + 1
        proposed = draft.model_copy(update={"version": new_version}, deep=True)
        content_hash = self._hash(proposed)
        if existing and content_hash == existing[-1].content_hash:
            msg = (
                f"product '{product_id}' already has version {existing[-1].version} with "
                "identical content; publishing again would create a version that changes nothing"
            )
            raise ProductConfigError(msg)
        draft.version = new_version
        draft.status = ProductStatus.PUBLISHED
        published = PublishedVersion(
            product_id=product_id,
            version=draft.version,
            status=ProductStatus.PUBLISHED,
            content_hash=content_hash,
            published_at=datetime.now(UTC),
            published_by=actor,
            guardrail_summary={
                "canPublish": report.can_publish,
                "blockerCount": len(report.blockers),
                "warningCount": len(report.warnings),
                "violations": [v.to_dict() for v in report.violations],
                "complianceEnforced": self._settings.require_compliance_pass,
                "negativeMarginAllowed": self._settings.allow_negative_margin,
            },
            definition=draft.model_copy(deep=True),
        )
        existing.append(published)
        self._published[product_id] = existing

        # The draft becomes immutable. Any further edit must be an explicit
        # revision, which bumps the version rather than mutating published data.
        # The stored draft becomes immutable: any further edit must be an explicit
        # revision, which bumps the version rather than mutating published data.
        draft.status = ProductStatus.RETIRED
        return published

    def retire(self, product_id: str, version: int) -> PublishedVersion:
        """Withdraw a published version without altering its content."""
        versions = self._published.get(product_id, [])
        target = next((v for v in versions if v.version == version), None)
        if target is None:
            msg = f"product '{product_id}' has no published version {version}"
            raise ProductConfigError(msg)
        target.status = ProductStatus.RETIRED
        return target

    def diff(self, product_id: str, from_version: int, to_version: int) -> VersionDiff:
        """Diff two published versions of one product."""
        before = self.get_published(product_id, from_version)
        after = self.get_published(product_id, to_version)
        if before is None or before.definition is None:
            msg = f"product '{product_id}' has no published version {from_version}"
            raise ProductConfigError(msg)
        if after is None or after.definition is None:
            msg = f"product '{product_id}' has no published version {to_version}"
            raise ProductConfigError(msg)
        return diff_definitions(before.definition, after.definition)

    # -- pricing -----------------------------------------------------------

    def quote(self, request: QuoteRequest, *, use_published: bool = True) -> Quote:
        """Price a quote against a published version, or the live draft."""
        if use_published:
            published = self.get_published(request.product_id)
            if published is None:
                msg = (
                    f"product '{request.product_id}' has no published version; "
                    "publish it or quote against the draft explicitly"
                )
                raise ProductConfigError(msg)
            definition = published.definition
        else:
            definition = self._drafts.get(request.product_id)
            if definition is None:
                msg = f"no draft for product '{request.product_id}'"
                raise ProductConfigError(msg)

        assert definition is not None  # narrowed above
        cover = definition.cover(request.cover_id)
        if cover is None:
            msg = f"product '{request.product_id}' has no cover '{request.cover_id}'"
            raise ProductConfigError(msg)
        table = definition.table(cover.rating_table_id)
        if table is None:
            msg = f"cover '{request.cover_id}' references a missing rating table"
            raise ProductConfigError(msg)

        quote = self._rating.quote(cover, table, request)
        quote.product_version = definition.version

        compliance = self._compliance.check_quote(definition, request)
        blocking = [v for v in compliance if v.severity.blocks_publish]
        if blocking:
            quote.eligibility = "ineligible"
            quote.reason_codes = [*quote.reason_codes, *[v.rule_id for v in blocking]]
        elif compliance:
            quote.reason_codes = [*quote.reason_codes, *[v.rule_id for v in compliance]]

        quote.expires_at = datetime.now(UTC) + timedelta(days=30)
        self._quotes[quote.product_id] = quote
        return quote

    def simulate(
        self,
        definition: ProductDefinition,
        profile: SimulationProfile | None = None,
        *,
        policies: int = 500,
        seed: int = 424242,
    ) -> SimulationResult:
        """Simulate a definition against a synthetic book of business.

        The purpose is to catch an unviable product before publication, not to
        produce an actuarial estimate. Everything it reports about loss is driven by
        a stated assumption, and that assumption is named in the notes.
        """
        profile = profile or SimulationProfile()
        engine = self._rating

        priced: list[Quote] = []
        unpriceable = 0
        rng = random.Random(seed)
        states = profile.states or ([profile.state] if profile.state else definition.states)
        if not states:
            states = ["CA"]

        for _ in range(policies):
            age = rng.randint(profile.min_age, profile.max_age)
            face = round(rng.uniform(profile.min_face_amount, profile.max_face_amount), -3)
            cover = rng.choice(definition.covers)
            request = QuoteRequest(
                tenant_id="simulation",
                product_id=definition.product_id,
                cover_id=cover.cover_id,
                age=age,
                face_amount=face,
                term_years=rng.choice(cover.term_options_years) if cover.term_options_years else None,
                state=rng.choice(states),
                tobacco=rng.random() < profile.tobacco_rate,
            )
            table = definition.table(cover.rating_table_id)
            if table is None:
                unpriceable += 1
                continue
            try:
                quote = engine.quote(cover, table, request)
            except RatingError:
                unpriceable += 1
                continue
            # An ineligible request produced a decision, not a price. For a
            # simulation it counts as unpriceable: no premium was earned.
            if quote.eligibility == "ineligible":
                unpriceable += 1
                continue
            priced.append(quote)

        if not priced:
            return SimulationResult(
                product_id=definition.product_id,
                version=definition.version,
                profile=profile.label,
                policies=policies,
                average_face_amount=0.0,
                average_annual_premium=0.0,
                average_margin_bps=0,
                loss_ratio=1.0,
                unpriceable=policies,
                gross_written_premium=0.0,
                profit=-1.0,
                notes=["No applicant in this profile could be priced against the definition."],
            )

        count = len(priced)
        average_premium = sum(q.annual_premium for q in priced) / count
        average_face = sum(q.face_amount for q in priced) / count
        average_margin = sum(q.margin_bps for q in priced) / count
        gross_premium = sum(q.annual_premium for q in priced)

        assumed_benefits = sum(
            q.face_amount / 1000.0 * ASSUMED_BENEFIT_COST_PER_THOUSAND for q in priced
        )
        charges = sum(q.annual_charges for q in priced)
        total_cost = assumed_benefits + charges
        loss_ratio = total_cost / gross_premium if gross_premium else 1.0

        notes = [
            f"Benefit cost assumed at {ASSUMED_BENEFIT_COST_PER_THOUSAND}/1000 of face. "
            "Replace with the carrier's mortality basis for a real estimate.",
        ]
        if unpriceable:
            notes.append(f"{unpriceable} of {policies} simulated applicants fell outside the configured ranges.")
        if loss_ratio >= 1.0:
            notes.append("Assumed benefits plus charges exceed premium: this product loses money as configured.")
        if average_margin < 500:
            notes.append(f"Average margin of {average_margin}bp is thin; carriers run term margins low deliberately.")

        return SimulationResult(
            product_id=definition.product_id,
            version=definition.version,
            profile=profile.label,
            policies=count,
            average_face_amount=average_face,
            average_annual_premium=average_premium,
            average_margin_bps=int(round(average_margin)),
            loss_ratio=loss_ratio,
            unpriceable=unpriceable,
            gross_written_premium=gross_premium,
            profit=gross_premium - total_cost,
            notes=notes,
        )

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _hash(definition: ProductDefinition) -> str:
        """Content hash of a definition, excluding its own timestamps.

        Two definitions that differ only in `updated_at` are the same product
        version, and hashing the timestamp would make the hash meaningless.
        """
        payload = definition.to_dict()
        # Identity and bookkeeping fields are excluded. The version number in
        # particular must be: a hash that changes when only the label changes
        # would make "has the content actually changed?" unanswerable.
        for volatile in ("updatedAt", "createdAt", "version", "status"):
            payload.pop(volatile, None)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:32]


_default_service: ProductConfigService | None = None


def default_product_config_service() -> ProductConfigService:
    """Process-wide product configuration service."""
    global _default_service
    if _default_service is None:
        _default_service = ProductConfigService(Plugin4Settings())
    return _default_service


__all__ = [
    "ASSUMED_BENEFIT_COST_PER_THOUSAND",
    "ProductConfigError",
    "ProductConfigService",
    "default_product_config_service",
]