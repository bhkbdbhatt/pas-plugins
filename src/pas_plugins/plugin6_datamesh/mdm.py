"""Master data management: entity resolution and survivorship.

The problem this solves is mundane and expensive: the same person appears in the
PAS as a policyholder, in billing as a payor, in the CRM as a lead, and in an
external enrichment feed as a "customer" with a different address. Without
resolution an agent asking "how many policies does this person have" gets a wrong
answer, and an AI agent gets a confidently wrong one.

Two mechanisms, kept separate because they solve different problems:

* **Blocking** narrows the candidate set cheaply (SSN last-four + DOB, or
  normalised name + postal code) so matching is not O(n^2).
* **Deterministic + probabilistic matching** decides within a block. Deterministic
  rules run first and are explainable; fuzzy scoring handles the rest and is
  thresholded so a match is always reproducible.

Survivorship then chooses which source's value wins per field, from a configurable
and prioritised source ranking.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

from pas_core.errors import PasError, ValidationError
from pas_core.observability import GLOBAL_METRICS
from pas_core.pii import PiiPolicy
from pas_core.tenancy import TenantContext
from pas_plugins.plugin6_datamesh.models import (
    BaseEntity,
    Customer,
    EntityType,
    RecordQuality,
    SourceSystem,
)

NON_ALNUM = re.compile(r"[^A-Z0-9]")
# Apostrophes are elided so "O'Brien" blocks with "OBrien". Hyphens and periods
# become spaces, because "Smith-Jones" is two names while "OBrien" is one.
APOSTROPHE = re.compile(r"['\u2019]")
NAME_SEPARATORS = re.compile(r"[.\-\u2010]")
SUFFIXES = {"JR", "SR", "II", "III", "IV", "MD", "PHD", "DDS", "ESQ"}


def normalise_name(name: str) -> str:
    """Uppercase, strip accents and punctuation, drop generational suffixes."""
    decomposed = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    cleaned = APOSTROPHE.sub("", NAME_SEPARATORS.sub(" ", decomposed.upper()))
    tokens = [t for t in NON_ALNUM.sub(" ", cleaned).split() if t]
    tokens = [t for t in tokens if t not in SUFFIXES]
    return " ".join(tokens)


def name_key(first: str, last: str, middle: str | None = None) -> str:
    """Blocking key from a person's name."""
    parts = [normalise_name(first), normalise_name(last)]
    if middle:
        parts.insert(1, normalise_name(middle)[:1])
    return "|".join(p for p in parts if p)


def soundex(value: str) -> str:
    """Classic Soundex - a cheap phonetic match for name variants."""
    text = normalise_name(value).replace(" ", "")
    if not text:
        return "0000"
    codes = {
        **{c: "1" for c in "BFPV"}, **{c: "2" for c in "CGJKQSXZ"},
        **{c: "3" for c in "DT"}, "L": "4",
        **{c: "5" for c in "MN"}, "R": "6",
    }
    first = text[0]
    digits: list[str] = []
    previous = codes.get(first, "")
    for char in text[1:]:
        digit = codes.get(char, "")
        if digit and digit != previous:
            digits.append(digit)
        if char not in "HW":
            previous = digit
    return (first + "".join(digits) + "000")[:4]


def jaro_winkler(left: str, right: str) -> float:
    """Similarity in ``[0, 1]``. Implemented directly to avoid a fuzzy dependency.

    Jaro-Winkler is used rather than a token-set ratio because insurance names are
    short and order-sensitive: "Smith John" and "John Smith" are the same person,
    and a metric that rewards shared character n-grams handles that well.
    """
    if left == right:
        return 1.0
    if not left or not right:
        return 0.0
    window = max(len(left), len(right)) // 2 - 1
    window = max(window, 0)
    left_matches = [False] * len(left)
    right_matches = [False] * len(right)
    matches = 0
    for i, char in enumerate(left):
        start = max(0, i - window)
        end = min(i + window + 1, len(right))
        for j in range(start, end):
            if right_matches[j] or right[j] != char:
                continue
            left_matches[i] = right_matches[j] = True
            matches += 1
            break
    if matches == 0:
        return 0.0
    # Transpositions compare matched characters pairwise; the two match arrays can
    # differ in length so the zip is intentionally not strict.
    matched_left = [left[i] for i in range(len(left)) if left_matches[i]]
    matched_right = [right[j] for j in range(len(right)) if right_matches[j]]
    transpositions = sum(
        1 for a, b in zip(matched_left, matched_right, strict=False) if a != b
    ) / 2
    jaro = (matches / len(left) + matches / len(right) + (matches - transpositions) / matches) / 3
    prefix = 0
    for a, b in zip(left[:4], right[:4], strict=False):
        if a != b:
            break
        prefix += 1
    return jaro + prefix * 0.1 * (1 - jaro)


class MatchConfidence(StrEnum):
    EXACT = "exact"
    HIGH = "high"
    PROBABLE = "probable"
    REVIEW = "review"
    NO_MATCH = "noMatch"


@dataclass(frozen=True, slots=True)
class MatchRule:
    """A deterministic matching rule, evaluated before any fuzzy scoring.

    Every deterministic rule is fully explainable - "same SSN last-four and same
    date of birth" - which is what makes an entity-resolution decision auditable.
    """

    rule_id: str
    description: str
    field_pairs: tuple[tuple[str, str], ...]
    require_all: bool = True
    confidence: MatchConfidence = MatchConfidence.EXACT
    weight: float = 1.0

    def evaluate(self, left: BaseEntity, right: BaseEntity) -> bool:
        results = [_values_match(left, right, a, b) for a, b in self.field_pairs]
        return all(results) if self.require_all else any(results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ruleId": self.rule_id,
            "description": self.description,
            "fieldPairs": [list(p) for p in self.field_pairs],
            "requireAll": self.require_all,
            "confidence": str(self.confidence),
            "weight": self.weight,
        }


def _values_match(left: BaseEntity, right: BaseEntity, left_field: str, right_field: str) -> bool:
    a = _get_field(left, left_field)
    b = _get_field(right, right_field)
    if a in (None, "") or b in (None, ""):
        return False
    return str(a).strip().upper() == str(b).strip().upper()


def _get_field(entity: BaseEntity, name: str) -> Any:  # noqa: ANN401
    if name in entity.model_fields:
        return getattr(entity, name, None)
    return entity.attributes.get(name)


DEFAULT_RULES: tuple[MatchRule, ...] = (
    MatchRule(
        rule_id="MDM-001",
        description="Same SSN last-four and same date of birth",
        field_pairs=(("ssn_last4", "ssn_last4"), ("date_of_birth", "date_of_birth")),
        confidence=MatchConfidence.EXACT,
    ),
    MatchRule(
        rule_id="MDM-002",
        description="Same policy number",
        field_pairs=(("policy_number", "policy_number"),),
        confidence=MatchConfidence.EXACT,
    ),
    MatchRule(
        rule_id="MDM-003",
        description="Same email address",
        field_pairs=(("email", "email"),),
        confidence=MatchConfidence.HIGH,
    ),
    MatchRule(
        rule_id="MDM-004",
        description="Same claim number",
        field_pairs=(("claim_number", "claim_number"),),
        confidence=MatchConfidence.EXACT,
    ),
    MatchRule(
        rule_id="MDM-005",
        description="Same normalised name and same date of birth",
        field_pairs=(("__name__", "__name__"), ("date_of_birth", "date_of_birth")),
        confidence=MatchConfidence.HIGH,
    ),
    MatchRule(
        rule_id="MDM-006",
        description="Same normalised name and same postal code",
        field_pairs=(("__name__", "__name__"), ("address_postal_code", "address_postal_code")),
        confidence=MatchConfidence.PROBABLE,
    ),
)


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    """A scored candidate pair."""

    left_key: str
    right_key: str
    score: float
    confidence: MatchConfidence
    matched_rules: tuple[str, ...]
    signals: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "leftKey": self.left_key,
            "rightKey": self.right_key,
            "score": round(self.score, 4),
            "confidence": str(self.confidence),
            "matchedRules": list(self.matched_rules),
            "signals": {k: round(v, 4) for k, v in self.signals.items()},
        }


@dataclass(slots=True)
class MatchCluster:
    """A resolved group of source records believed to be the same real-world entity."""

    cluster_id: str
    entity_type: EntityType
    member_keys: list[str]
    confidence: MatchConfidence
    match_type: str = "deterministic"
    matched_rules: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "clusterId": self.cluster_id,
            "entityType": str(self.entity_type),
            "memberCount": len(self.member_keys),
            "members": self.member_keys[:25],
            "confidence": str(self.confidence),
            "matchType": self.match_type,
            "matchedRules": list(self.matched_rules),
        }


class EntityResolver:
    """Blocks, scores and clusters candidate records."""

    def __init__(self, *, threshold: float = 0.86, pii_policy: PiiPolicy | None = None) -> None:
        self._threshold = threshold
        self._rules = list(DEFAULT_RULES)
        self._pii = pii_policy or PiiPolicy()
        self._survivorship = default_survivorship()

    def add_rule(self, rule: MatchRule) -> MatchRule:
        self._rules.append(rule)
        return rule

    def blocking_keys(self, entity: BaseEntity) -> set[str]:
        """Cheap keys that group obviously-related records.

        Several keys are emitted per record so two records that agree on *any* of
        them land in the same block - a missing SSN must not prevent a match on
        name and postal code.
        """
        keys: set[str] = set()
        entity_type = entity.entity_type
        if entity_type is EntityType.CUSTOMER:
            ssn = _get_field(entity, "ssn_last4")
            dob = _get_field(entity, "date_of_birth")
            if ssn and dob:
                keys.add(f"ssndob|{ssn}|{dob}")
            first, last = _get_field(entity, "first_name"), _get_field(entity, "last_name")
            postal, city = _get_field(entity, "address_postal_code"), _get_field(entity, "city")
            if first and last:
                base = name_key(str(first), str(last))
                if postal:
                    keys.add(f"namezip|{base}|{postal}")
                elif city:
                    keys.add(f"namecity|{base}|{normalise_name(str(city))}")
                else:
                    keys.add(f"name|{base}")
            email = _get_field(entity, "email")
            if email:
                keys.add(f"email|{str(email).lower()}")
        elif entity_type is EntityType.POLICY:
            number = _get_field(entity, "policy_number")
            if number:
                keys.add(f"policy|{number}")
        elif entity_type is EntityType.CLAIM:
            number = _get_field(entity, "claim_number")
            if number:
                keys.add(f"claim|{number}")
        elif entity_type is EntityType.PREMIUM:
            number, due = _get_field(entity, "policy_number"), _get_field(entity, "due_date")
            if number and due:
                keys.add(f"premium|{number}|{due}")
        return keys or {f"type|{entity.entity_type}|{entity.natural_key}"}

    def score_pair(self, left: BaseEntity, right: BaseEntity) -> MatchCandidate | None:
        """Score a candidate pair: deterministic rules first, then fuzzy signals."""
        signals: dict[str, float] = {}
        fired: list[str] = []
        best: MatchConfidence = MatchConfidence.NO_MATCH
        total_weight = 0.0

        for rule in self._rules:
            matched = rule.evaluate(left, right)
            if matched:
                fired.append(rule.rule_id)
                total_weight += rule.weight
                if _confidence_rank(rule.confidence) > _confidence_rank(best):
                    best = rule.confidence
            signals[f"rule:{rule.rule_id}"] = 1.0 if matched else 0.0

        first_l, last_l = _get_field(left, "first_name"), _get_field(left, "last_name")
        first_r, last_r = _get_field(right, "first_name"), _get_field(right, "last_name")
        if first_l and first_r and last_l and last_r:
            first_sim = jaro_winkler(normalise_name(str(first_l)), normalise_name(str(first_r)))
            last_sim = jaro_winkler(normalise_name(str(last_l)), normalise_name(str(last_r)))
            soundex_sim = 1.0 if soundex(str(last_l)) == soundex(str(last_r)) else 0.0
            dob_l, dob_r = _get_field(left, "date_of_birth"), _get_field(right, "date_of_birth")
            dob_sim = 1.0 if dob_l and dob_r and str(dob_l) == str(dob_r) else 0.0
            signals.update({
                "firstNameSimilarity": first_sim,
                "lastNameSimilarity": last_sim,
                "lastNameSoundex": soundex_sim,
                "dateOfBirthMatch": dob_sim,
            })
            fuzzy = (
                0.35 * first_sim + 0.35 * last_sim + 0.15 * soundex_sim + 0.15 * dob_sim
            )
        else:
            fuzzy = 1.0 if fired else 0.0

        # Deterministic evidence dominates; fuzzy evidence only matters once some
        # deterministic rule has fired, which prevents a coincidentally similar
        # name in another state from merging two people.
        score = min(1.0, (0.65 * min(1.0, total_weight)) + (0.35 * fuzzy if fired else 0.0))
        if not fired:
            return None
        return MatchCandidate(
            left_key=left.natural_key,
            right_key=right.natural_key,
            score=score,
            confidence=best if score >= self._threshold else MatchConfidence.REVIEW,
            matched_rules=tuple(fired),
            signals=signals,
        )

    def resolve(self, records: Sequence[BaseEntity], entity_type: EntityType) -> list[MatchCluster]:
        """Group records into clusters using blocking plus union-find."""
        blocks: dict[str, list[BaseEntity]] = {}
        for record in records:
            if record.entity_type is not entity_type:
                continue
            for key in self.blocking_keys(record):
                blocks.setdefault(key, []).append(record)

        parent: dict[str, str] = {r.natural_key: r.natural_key for r in records}

        def find(node: str) -> str:
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        evidence: dict[str, tuple[MatchConfidence, tuple[str, ...]]] = {}
        compared: set[tuple[str, str]] = set()
        for group in blocks.values():
            unique = {id(r): r for r in group}.values()
            ordered = sorted(unique, key=lambda r: r.natural_key)
            for i, left in enumerate(ordered):
                for right in ordered[i + 1:]:
                    pair = (left.natural_key, right.natural_key)
                    if pair in compared:
                        continue
                    compared.add(pair)
                    candidate = self.score_pair(left, right)
                    if candidate is None or candidate.score < self._threshold:
                        continue
                    union(left.natural_key, right.natural_key)
                    root = find(left.natural_key)
                    previous = evidence.get(root)
                    if previous is None or _confidence_rank(candidate.confidence) > _confidence_rank(previous[0]):
                        evidence[root] = (candidate.confidence, candidate.matched_rules)

        grouped: dict[str, list[str]] = {}
        for record in records:
            if record.entity_type is not entity_type:
                continue
            grouped.setdefault(find(record.natural_key), []).append(record.natural_key)

        clusters: list[MatchCluster] = []
        for root, members in sorted(grouped.items()):
            confidence, rules = evidence.get(root, (MatchConfidence.PROBABLE, ()))
            cluster_id = hashlib.sha256(
                f"{entity_type}:{root}".encode()
            ).hexdigest()[:20]
            clusters.append(MatchCluster(
                cluster_id=cluster_id,
                entity_type=entity_type,
                member_keys=sorted(members),
                confidence=confidence,
                match_type="deterministic" if any(r.startswith("MDM-00") and r[7:9] in "12" for r in rules) else "probabilistic",
                matched_rules=rules,
            ))

        GLOBAL_METRICS.increment(
            "mdm_clusters_total", entityType=str(entity_type), outcome=str(self._quality_label(clusters))
        )
        return clusters

    @staticmethod
    def _quality_label(clusters: Sequence[MatchCluster]) -> str:
        if any(c.confidence is MatchConfidence.REVIEW for c in clusters):
            return "needsReview"
        return "resolved"


def _confidence_rank(confidence: MatchConfidence) -> int:
    return {
        MatchConfidence.NO_MATCH: 0,
        MatchConfidence.REVIEW: 1,
        MatchConfidence.PROBABLE: 2,
        MatchConfidence.HIGH: 3,
        MatchConfidence.EXACT: 4,
    }[confidence]


# ---------------------------------------------------------------------------
# Survivorship
# ---------------------------------------------------------------------------
class SurvivorshipRule(StrEnum):
    """Which source wins for a field."""

    MOST_RECENT = "mostRecent"
    HIGHEST_QUALITY = "highestQuality"
    MOST_COMPLETE = "mostComplete"
    SOURCE_PRIORITY = "sourcePriority"
    MOST_FREQUENT = "mostFrequent"


@dataclass(frozen=True, slots=True)
class SurvivorshipPolicy:
    """Per-field survivorship configuration."""

    field_rules: dict[str, SurvivorshipRule]
    source_priority: tuple[SourceSystem, ...] = (
        SourceSystem.PAS,
        SourceSystem.CLAIMS,
        SourceSystem.BILLING,
        SourceSystem.CRM,
        SourceSystem.EXTERNAL_ENRICHMENT,
        SourceSystem.AGENCY,
        SourceSystem.MANUAL,
    )

    def rule_for(self, field_name: str) -> SurvivorshipRule:
        return self.field_rules.get(field_name, SurvivorshipRule.MOST_RECENT)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fieldRules": {k: str(v) for k, v in self.field_rules.items()},
            "sourcePriority": [str(s) for s in self.source_priority],
        }


def default_survivorship() -> SurvivorshipPolicy:
    """A defensible default policy a carrier's data steward would recognise.

    The PAS is the system of record for anything contractual, so it wins by
    default; enrichment fills only what the PAS does not have.
    """
    return SurvivorshipPolicy(
        field_rules={
            "first_name": SurvivorshipRule.SOURCE_PRIORITY,
            "last_name": SurvivorshipRule.SOURCE_PRIORITY,
            "date_of_birth": SurvivorshipRule.SOURCE_PRIORITY,
            "ssn_last4": SurvivorshipRule.SOURCE_PRIORITY,
            "address_line1": SurvivorshipRule.MOST_RECENT,
            "address_city": SurvivorshipRule.MOST_RECENT,
            "address_state": SurvivorshipRule.MOST_RECENT,
            "address_postal_code": SurvivorshipRule.MOST_RECENT,
            "email": SurvivorshipRule.MOST_RECENT,
            "phone": SurvivorshipRule.MOST_RECENT,
            "preferred_language": SurvivorshipRule.MOST_COMPLETE,
            "tags": SurvivorshipRule.MOST_COMPLETE,
            "lifetime_premium": SurvivorshipRule.HIGHEST_QUALITY,
            "in_force_policy_count": SurvivorshipRule.HIGHEST_QUALITY,
        }
    )


@dataclass(slots=True)
class SurvivorshipResult:
    """The surviving record plus the provenance of every chosen field."""

    entity: BaseEntity
    chosen: dict[str, str] = field(default_factory=dict)
    conflicts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "surrogateKey": self.entity.surrogate_key,
            "chosenFields": self.chosen,
            "conflictFields": {
                field_name: values for field_name, values in self.conflicts.items()
            },
            "conflictCount": len(self.conflicts),
        }


class GoldenRecordBuilder:
    """Produces the single record per real-world entity that the API serves."""

    def __init__(self, resolver: EntityResolver | None = None) -> None:
        self.resolver = resolver or EntityResolver()
        self._gold: dict[str, dict[str, BaseEntity]] = {}

    def build(
        self, records: Sequence[BaseEntity], ctx: TenantContext, *, quality_floor: float = 0.0
    ) -> list[SurvivorshipResult]:
        """Resolve, survive and (optionally) gate on a quality floor."""
        results: list[SurvivorshipResult] = []
        for entity_type in {r.entity_type for r in records}:
            clusters = self.resolver.resolve(records, entity_type)
            by_key = {r.natural_key: r for r in records}
            for cluster in clusters:
                members = [by_key[k] for k in cluster.member_keys if k in by_key]
                if not members:
                    continue
                result = self._survive(members, cluster)
                if result.entity.quality_score < quality_floor:
                    continue
                store = self._gold.setdefault(ctx.tenant_id, {})
                store[result.entity.surrogate_key] = result.entity
                results.append(result)
        GLOBAL_METRICS.increment("mdm_gold_records_total", tenant=ctx.tenant_id, count=len(results))
        return results

    def _survive(self, members: Sequence[BaseEntity], cluster: MatchCluster) -> SurvivorshipResult:
        policy = self.resolver._survivorship  # noqa: SLF001 - internal by design
        winner = self._pick_primary(members, policy)
        chosen: dict[str, str] = {}
        conflicts: dict[str, list[dict[str, Any]]] = {}

        for field_name in self._mergeable_fields(members[0]):
            rule = policy.rule_for(field_name)
            candidates = [
                {"value": getattr(m, field_name), "source": str(m.source_system), "recordId": m.natural_key}
                for m in members
                if getattr(m, field_name, None) not in (None, "", [], {})
            ]
            if not candidates:
                continue
            distinct = {str(c["value"]) for c in candidates}
            if len(distinct) > 1:
                conflicts[field_name] = candidates
            chosen[field_name] = str(self._resolve(rule, candidates, members, policy))

        lineage = [entry for member in members for entry in member.lineage]
        merged = winner.model_copy(update={
            "source_system": winner.source_system,
            "attributes": {
                **winner.attributes,
                "_merged_members": list(cluster.member_keys),
                # Retained so quality rule DQ-009 can surface records whose sources
                # disagree, and so a data steward can chase the offending source.
                "_conflicts": {
                    field_name: values for field_name, values in conflicts.items()
                },
            },
            "lineage": lineage,
            "quality_tier": RecordQuality.GOLDEN,
            "quality_score": _quality_score(members, conflicts),
            "effective_from": min(m.effective_from for m in members),
            "effective_to": None,
        })
        return SurvivorshipResult(entity=merged, chosen=chosen, conflicts=conflicts)

    @staticmethod
    def _pick_primary(members: Sequence[BaseEntity], policy: SurvivorshipPolicy) -> BaseEntity:
        order = {str(s): i for i, s in enumerate(policy.source_priority)}
        return sorted(
            members,
            key=lambda m: (order.get(str(m.source_system), 99), -m.quality_score, m.natural_key),
        )[0]

    @staticmethod
    def _mergeable_fields(entity: BaseEntity) -> list[str]:
        excluded = {
            "surrogate_key", "tenant_id", "entity_type", "natural_key", "source_system",
            "source_record_id", "attributes", "lineage", "quality_tier", "quality_score",
            "pii_classes", "effective_from", "effective_to", "recorded_at", "consent",
        }
        return [
            name for name in entity.model_fields
            if name not in excluded and not name.startswith("_")
        ]

    @staticmethod
    def _resolve(
        rule: SurvivorshipRule,
        candidates: list[dict[str, Any]],
        members: Sequence[BaseEntity],
        policy: SurvivorshipPolicy,
    ) -> Any:  # noqa: ANN401
        if rule is SurvivorshipRule.SOURCE_PRIORITY:
            order = {str(s): i for i, s in enumerate(policy.source_priority)}
            return min(candidates, key=lambda c: order.get(c["source"], 99))["value"]
        if rule is SurvivorshipRule.HIGHEST_QUALITY:
            scores = {m.natural_key: m.quality_score for m in members}
            return max(candidates, key=lambda c: (scores.get(c["recordId"], 0.0), c["source"]))["value"]
        if rule is SurvivorshipRule.MOST_COMPLETE:
            return max(candidates, key=lambda c: len(str(c["value"])))["value"]
        if rule is SurvivorshipRule.MOST_FREQUENT:
            counts: dict[str, int] = {}
            for candidate in candidates:
                counts[str(candidate["value"])] = counts.get(str(candidate["value"]), 0) + 1
            return max(candidates, key=lambda c: counts[str(c["value"])])["value"]
        return candidates[0]["value"]

    # -- access -------------------------------------------------------------
    def get(self, ctx: TenantContext, surrogate_key: str) -> BaseEntity | None:
        return self._gold.get(ctx.tenant_id, {}).get(surrogate_key)

    def find_by_natural_key(self, ctx: TenantContext, natural_key: str) -> BaseEntity | None:
        for entity in self._gold.get(ctx.tenant_id, {}).values():
            if entity.natural_key == natural_key:
                return entity
        return None

    def search(
        self,
        ctx: TenantContext,
        *,
        entity_type: EntityType | None = None,
        query: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> list[BaseEntity]:
        rows = list(self._gold.get(ctx.tenant_id, {}).values())
        if entity_type is not None:
            rows = [r for r in rows if r.entity_type is entity_type]
        if query:
            needle = query.lower()
            rows = [
                r for r in rows
                if needle in str(getattr(r, "natural_key", "")).lower()
                or any(needle in str(v).lower() for v in r.attributes.values() if isinstance(v, str))
                or _matches_name(r, needle)
            ]
        rows.sort(key=lambda r: r.natural_key)
        return rows[offset: offset + limit]

    def all(self, ctx: TenantContext) -> list[BaseEntity]:
        return list(self._gold.get(ctx.tenant_id, {}).values())

    def stats(self, ctx: TenantContext) -> dict[str, Any]:
        rows = self.all(ctx)
        by_type: dict[str, int] = {}
        for row in rows:
            key = str(row.entity_type)
            by_type[key] = by_type.get(key, 0) + 1
        merged = sum(
            1 for r in rows
            if len(r.attributes.get("_merged_members") or []) > 1
        )
        conflicted = sum(
            1 for r in rows if r.attributes.get("_conflicts")
        )
        return {
            "tenantId": ctx.tenant_id,
            "goldenRecords": len(rows),
            "mergedRecords": merged,
            "recordsWithConflicts": conflicted,
            "survivorshipRatio": round(merged / len(rows), 4) if rows else 0.0,
            "byEntityType": by_type,
            "survivorshipPolicy": self.resolver._survivorship.to_dict(),  # noqa: SLF001
        }


def _matches_name(entity: BaseEntity, needle: str) -> bool:
    if isinstance(entity, Customer):
        haystack = normalise_name(entity.full_name)
    else:
        haystack = ""
    return bool(haystack) and needle in haystack


def _quality_score(members: Sequence[BaseEntity], conflicts: dict[str, Any]) -> float:
    """Score a merged golden record.

    Completeness is measured against the entity's declared :attr:`GOLD_FIELDS`
    rather than against every field the model happens to define, so a carrier
    without a ``middle_name`` column is not penalised for it.

    Cross-source conflicts then apply a penalty, because a record where the PAS
    and the CRM disagree on the date of birth is genuinely less trustworthy even
    when every required field is populated.
    """
    probe = _GoldenFieldProbe.names(members[0])
    gold_fields = [name for name in (members[0].GOLD_FIELDS or tuple(probe)) if name in probe]
    populated = {
        name for name in probe
        if any(getattr(m, name, None) not in (None, "", [], {}) for m in members)
    }
    if not gold_fields:
        completeness = len(populated) / len(probe) if probe else 1.0
    else:
        completeness = len(set(gold_fields) & populated) / len(gold_fields)
    conflict_penalty = min(0.4, 0.05 * len(conflicts))
    return round(max(0.0, min(1.0, completeness - conflict_penalty)), 4)


class _GoldenFieldProbe:
    """Excludes envelope fields from the completeness calculation."""

    ENVELOPE = frozenset({
        "surrogate_key", "tenant_id", "entity_type", "natural_key", "source_system",
        "source_record_id", "attributes", "lineage", "quality_tier", "quality_score",
        "pii_classes", "effective_from", "effective_to", "recorded_at", "consent",
    })

    @classmethod
    def names(cls, entity: BaseEntity) -> list[str]:
        return [n for n in entity.model_fields if n not in cls.ENVELOPE and not n.startswith("_")]


def resolve_entities(
    records: Iterable[BaseEntity],
    ctx: TenantContext,
    *,
    threshold: float = 0.86,
    quality_floor: float = 0.0,
) -> list[SurvivorshipResult]:
    """One-shot helper: resolve and survive in a single call."""
    builder = GoldenRecordBuilder(EntityResolver(threshold=threshold))
    return builder.build(list(records), ctx, quality_floor=quality_floor)


def customer360_key(customer: Customer) -> str:
    """Stable identifier for the customer 360 view."""
    return f"customer/{customer.surrogate_key}"


def validate_cluster(cluster: MatchCluster, resolver: EntityResolver) -> None:
    """Reject a cluster whose confidence is below the automatic threshold."""
    if _confidence_rank(cluster.confidence) < _confidence_rank(MatchConfidence.PROBABLE):
        msg = (
            f"cluster '{cluster.cluster_id}' matched only at confidence "
            f"'{cluster.confidence}'; promote to manual review instead"
        )
        raise ValidationError(msg, clusterId=cluster.cluster_id)


def raise_if_empty(rows: Sequence[BaseEntity], entity: str, key: str) -> None:
    """Consistent not-found error for the data API."""
    if not rows:
        from pas_core.errors import NotFoundError  # noqa: PLC0415

        raise NotFoundError(entity, key)


def default_customer_payloads(count: int = 20) -> list[dict[str, Any]]:
    """Sample CRM/PAS customer payloads with deliberate duplicate identities."""
    out: list[dict[str, Any]] = []
    for index in range(count):
        surname = ["Smith", "Jones", "Patel", "Garcia"][index % 4]
        given = ["Jane", "Alex", "Maria", "Wei"][index % 4]
        dob = date(1960 + index % 30, index % 12 + 1, 1)
        out.append({
            "id": f"CUST{index + 1:06d}",
            "first_name": given,
            "last_name": surname,
            "date_of_birth": dob.isoformat(),
            "ssn_last4": f"{1000 + index % 8000:04d}",
            "email": f"{given.lower()}.{surname.lower()}@example.com",
            "phone": f"+1555000{index:04d}",
            "address_line1": f"{index + 10} Elm Street",
            "address_city": "New York",
            "address_state": "NY",
            "address_postal_code": "10012",
            "source": "crm",
        })
    # A near-duplicate with a different address and a middle initial: the kind of
    # record that defeats naive matching and needs Soundex + survivorship.
    out.append({
        "id": "CUST900001",
        "first_name": "Jane",
        "middle_name": "Q",
        "last_name": "Smith",
        "date_of_birth": date(1960, 1, 1).isoformat(),
        "ssn_last4": "1000",
        "email": "j.smith@work.example.com",
        "address_line1": "900 Second Avenue",
        "address_city": "Brooklyn",
        "address_state": "NY",
        "address_postal_code": "11201",
        "source": "enrichment",
    })
    return out


def pas_error_for_missing(entity_type: EntityType, key: str) -> PasError:
    """Consistent catalogue error for a missing golden record."""
    from pas_core.errors import ErrorCode  # noqa: PLC0415

    return PasError(
        ErrorCode.NOT_FOUND,
        f"No golden {entity_type} record found for '{key}'",
        {"entityType": str(entity_type), "key": key},
    )
