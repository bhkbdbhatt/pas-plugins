"""Versioned, auditable business rule engine.

Used by plugin 3 (appetite and eligibility rules), plugin 4 (product eligibility
rules) and plugin 1 (workflow preconditions).  Requirements that drove the design:

* **Underwriter readable.** Rules are data - a decision table an underwriter or a
  product manager can review, diff and sign off, not code.
* **Versioned with rollback.** A rule set is immutable once published; changes
  create a new version, and any version can be reinstated.
* **Auditable.** Every evaluation returns a trace naming each rule that fired,
  with its inputs, so a decision can be reconstructed months later.
* **Deterministic.** Evaluation order is stable and independent of dict ordering,
  which is what makes contract tests and regulatory review possible.

The expression language is deliberately small and side-effect free - no ``eval``.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pas_core.errors import BusinessRuleViolation, ValidationError
from pas_core.observability import trace_span

LOGGER = logging.getLogger("pas_core.rules")

# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------
COMPARATORS: dict[str, Callable[[Any, Any], bool]] = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "in": lambda a, b: a in (b if isinstance(b, (list, tuple, set)) else [b]),
    "not_in": lambda a, b: a not in (b if isinstance(b, (list, tuple, set)) else [b]),
    "contains": lambda a, b: b in (a or []),
    "not_contains": lambda a, b: b not in (a or []),
    "starts_with": lambda a, b: str(a or "").startswith(str(b)),
    "ends_with": lambda a, b: str(a or "").endswith(str(b)),
    "matches": lambda a, b: bool(re.search(str(b), str(a or ""))),
    "is_null": lambda a, b: (a is None) == bool(b),
    "exists": lambda a, b: (a is not None) == bool(b),
}

_TOKEN_PATTERN = re.compile(
    r"""\s*(?P<op>==|!=|>=|<=|>|<|\bmatches\b|\bnot_contains\b|\bcontains\b|\bnot_in\b|\bin\b|\bstarts_with\b|\bends_with\b|\bis_null\b|\bexists\b)""",
)


@dataclass(frozen=True, slots=True)
class Condition:
    """A single ``field <op> value`` predicate.

    ``field`` supports dotted paths (``applicant.address.state``) and ``[i]``
    indexing so rules can reach into nested submission payloads.
    """

    field_path: str
    operator: str
    expected: Any

    def evaluate(self, facts: Mapping[str, Any], *, strict: bool = False) -> bool:
        """Evaluate the predicate against a facts mapping.

        A missing fact is treated as *unsatisfied* rather than as an error: a
        submission that omitted ``beneficiaries.totalSharePercent`` has not
        demonstrated that the shares are acceptable. This keeps one absent field
        from failing an entire rule-set evaluation, while the alternative
        operators (``exists`` / ``is_null``) let a rule author ask the question
        explicitly.  Pass ``strict=True`` to raise instead, which is useful in
        rule-authoring tools where an unresolved path is a typo.
        """
        actual = resolve_path(facts, self.field_path, default=None)
        comparator = COMPARATORS.get(self.operator)
        if comparator is None:
            msg = f"unknown operator '{self.operator}'"
            raise ValidationError(msg, operator=self.operator, supported=sorted(COMPARATORS))
        try:
            return comparator(actual, self.expected)
        except TypeError:
            if strict:
                msg = (
                    f"condition {self.describe()!r} could not be evaluated: "
                    f"{self.field_path} is {type(actual).__name__}"
                )
                raise ValidationError(msg, field=self.field_path, actual=actual) from None
            LOGGER.debug(
                "condition %s evaluated false: %s is %s",
                self.describe(),
                self.field_path,
                type(actual).__name__,
            )
            return False

    def describe(self) -> str:
        return f"{self.field_path} {self.operator} {self.expected!r}"

    @classmethod
    def parse(cls, expression: str) -> Condition:
        """Parse ``"applicant.age >= 18"`` into a :class:`Condition`."""
        text = expression.strip()
        for operator in ("==", "!=", ">=", "<=", ">", "<"):
            index = text.find(operator)
            if index > 0:
                raw_field = text[:index].strip()
                raw_value = text[index + len(operator) :].strip()
                return cls(raw_field, operator, _coerce_literal(raw_value))
        for word_operator in ("not_contains", "not_in", "contains", "matches", "starts_with", "ends_with", "is_null", "exists", "in"):
            pattern = re.compile(rf"\b{word_operator}\b")
            match = pattern.search(text)
            if match and match.start() > 0:
                raw_field = text[: match.start()].strip()
                raw_value = text[match.end() :].strip()
                return cls(raw_field, word_operator, _coerce_literal(raw_value))
        msg = f"cannot parse condition {expression!r}"
        raise ValidationError(msg, expression=expression)

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field_path, "operator": self.operator, "value": self.expected}


def _coerce_literal(raw: str) -> Any:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_coerce_literal(part) for part in inner.split(",")]
    lowered = text.lower()
    if lowered in {"true", "yes"}:
        return True
    if lowered in {"false", "no"}:
        return False
    if lowered in {"null", "none"}:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def resolve_path(source: Mapping[str, Any], path: str, default: Any = None) -> Any:  # noqa: ANN401
    """Resolve a dotted/indexed path against nested mappings and sequences."""
    current: Any = source
    for segment in _split(path):
        if current is None:
            return default
        if isinstance(segment, int):
            if not isinstance(current, Sequence) or isinstance(current, str):
                return default
            if segment >= len(current):
                return default
            current = current[segment]
        else:
            if isinstance(current, Mapping):
                if segment in current:
                    current = current[segment]
                    continue
                return default
            if isinstance(current, Sequence) and not isinstance(current, str):
                # A bare key applied to a list projects across the elements,
                # which is what rules like "any party has role X" need.
                projected = [
                    item[segment] for item in current
                    if isinstance(item, Mapping) and segment in item
                ]
                return projected or default
            return default
    return current


def _split(path: str) -> list[str | int]:
    segments: list[str | int] = []
    for part in path.split("."):
        if not part:
            continue
        if "[" in part and part.endswith("]"):
            name, _, index_part = part.partition("[")
            if name:
                segments.append(name)
            try:
                segments.append(int(index_part[:-1]))
            except ValueError:
                continue
        else:
            segments.append(part)
    return segments


@dataclass(frozen=True, slots=True)
class ConditionGroup:
    """Boolean combination of conditions.

    ``mode`` is ``all`` (AND) or ``any`` (OR).  Nested groups are supported so a
    rule can express ``(age >= 18 and state in {...}) or referralFlag == true``.
    """

    mode: str
    conditions: tuple[Condition | ConditionGroup, ...] = ()

    def evaluate(self, facts: Mapping[str, Any]) -> bool:
        if not self.conditions:
            return True
        results = (c.evaluate(facts) for c in self.conditions)
        return any(results) if self.mode == "any" else all(results)

    def flat(self) -> list[Condition]:
        out: list[Condition] = []
        for item in self.conditions:
            if isinstance(item, Condition):
                out.append(item)
            else:
                out.extend(item.flat())
        return out

    def describe(self) -> str:
        joiner = " OR " if self.mode == "any" else " AND "
        rendered = []
        for item in self.conditions:
            text = item.describe() if isinstance(item, Condition) else f"({item.describe()})"
            rendered.append(text)
        return joiner.join(rendered) if rendered else "TRUE"

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "conditions": [c.to_dict() for c in self.conditions],
        }

    @classmethod
    def from_spec(cls, spec: Any) -> ConditionGroup:  # noqa: ANN401
        """Build a group from a condition, a dict, a list, or ``{"all": [...]}``.

        Accepted forms, all equivalent to an AND of their parts::

            "applicant.age >= 18"
            {"field": "applicant.age", "operator": ">=", "value": 18}
            {"all": [ ... ]}   /   {"any": [ ... ]}   /   {"mode": "all", "conditions": [ ... ]}
            [ ... ]
        """
        if isinstance(spec, str):
            return cls("all", (Condition.parse(spec),))
        if isinstance(spec, Mapping):
            if "field" in spec and "operator" in spec:
                # A bare condition written as a mapping, not a group.
                value = spec.get("value")
                return cls("all", (Condition(str(spec["field"]), str(spec["operator"]), value),))
            mode = str(spec.get("mode", "all"))
            items = spec.get("conditions") or spec.get(mode) or []
            return cls(mode, tuple(cls.from_spec(item) for item in items))
        if isinstance(spec, Sequence):
            return cls("all", tuple(cls.from_spec(item) for item in spec))
        msg = f"cannot build a condition group from {type(spec).__name__}"
        raise ValidationError(msg)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Action:
    """What a rule does when it fires.

    Supported kinds:
    ``set``            assign a literal or derived value
    ``reason_code``    attach an underwriting/decision reason code
    ``override``       replace a fact (e.g. force a manual-review flag)
    ``score``          add to a points total
    ``flag``           set a boolean flag
    ``message``        attach an operator-facing message
    ``require``        demand an item (document, evidence, signature)
    """

    kind: str
    target: str
    value: Any = None
    message: str = ""

    def apply(self, facts: dict[str, Any], trace: list[dict[str, Any]]) -> None:
        if self.kind == "reason_code":
            codes: list[str] = facts.setdefault("reasonCodes", [])
            if self.value not in codes:
                codes.append(self.value)
        elif self.kind == "message":
            messages: list[str] = facts.setdefault("messages", [])
            if self.message or self.value:
                messages.append(self.message or str(self.value))
        elif self.kind == "require":
            requirements: list[dict[str, Any]] = facts.setdefault("requirements", [])
            if self.target not in {r.get("code") for r in requirements}:
                requirements.append({
                    "code": self.target,
                    "description": self.message or self.target,
                    "mandatory": True,
                })
        elif self.kind == "flag":
            facts[self.target] = bool(self.value)
        elif self.kind == "score":
            current = float(facts.get(self.target, 0) or 0)
            facts[self.target] = current + float(self.value or 0)
        else:  # set / override
            facts[self.target] = self.value
        trace.append({"action": self.kind, "target": self.target, "value": self.value})

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target,
            "value": self.value,
            "message": self.message,
        }

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any]) -> Action:
        kind = str(spec.get("kind") or spec.get("action") or "set")
        if kind not in {"set", "reason_code", "override", "score", "flag", "message", "require"}:
            msg = f"unsupported action kind '{kind}'"
            raise ValidationError(msg, kind=kind)
        return cls(
            kind=kind,
            target=str(spec.get("target") or spec.get("field") or ""),
            value=spec.get("value"),
            message=str(spec.get("message") or spec.get("description") or ""),
        )


# ---------------------------------------------------------------------------
# Rules and rule sets
# ---------------------------------------------------------------------------
class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


class Outcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class Rule:
    """One versioned, named business rule."""

    rule_id: str
    name: str
    when: ConditionGroup
    then: tuple[Action, ...]
    severity: Severity = Severity.BLOCKING
    outcome: Outcome = Outcome.FAIL
    description: str = ""
    tags: tuple[str, ...] = ()
    version: int = 1
    enabled: bool = True

    def evaluate(self, facts: Mapping[str, Any]) -> RuleOutcome:
        """Evaluate the rule, returning the outcome plus any actions to apply."""
        if not self.enabled:
            return RuleOutcome(
                self.rule_id, Outcome.PASS, False, "disabled", actions=(), description=self.description
            )
        with trace_span("pas.rule.evaluate", rule=self.rule_id):
            fired = self.when.evaluate(facts)
            matched_conditions = (
                [c.describe() for c in self.when.flat() if c.evaluate(facts)] if fired else []
            )
        return RuleOutcome(
            rule_id=self.rule_id,
            outcome=self.outcome if fired else Outcome.PASS,
            fired=fired,
            reason=self.when.describe() if fired else "",
            matched_conditions=matched_conditions,
            actions=self.then if fired else (),
            severity=self.severity,
            name=self.name,
            description=self.description,
        )

    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "ruleId": self.rule_id,
                "name": self.name,
                "when": self.when.to_dict(),
                "then": [a.to_dict() for a in self.then],
                "severity": str(self.severity),
                "outcome": str(self.outcome),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ruleId": self.rule_id,
            "name": self.name,
            "when": self.when.to_dict(),
            "then": [a.to_dict() for a in self.then],
            "severity": str(self.severity),
            "outcome": str(self.outcome),
            "description": self.description,
            "tags": list(self.tags),
            "version": self.version,
            "enabled": self.enabled,
            "fingerprint": self.fingerprint(),
        }

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any]) -> Rule:
        rule_id = str(spec.get("ruleId") or spec.get("id") or "").strip()
        if not rule_id:
            msg = "every rule requires a ruleId"
            raise ValidationError(msg)
        if "when" not in spec:
            msg = f"rule '{rule_id}' has no 'when' condition"
            raise ValidationError(msg, ruleId=rule_id)
        actions = spec.get("then") or spec.get("actions") or []
        if isinstance(actions, Mapping):
            actions = [actions]
        return cls(
            rule_id=rule_id,
            name=str(spec.get("name") or rule_id),
            when=ConditionGroup.from_spec(spec["when"]),
            then=tuple(Action.from_spec(a) for a in actions),
            severity=Severity(str(spec.get("severity", "blocking"))),
            outcome=Outcome(str(spec.get("outcome", "fail"))),
            description=str(spec.get("description", "")),
            tags=tuple(str(t) for t in (spec.get("tags") or [])),
            version=int(spec.get("version", 1)),
            enabled=bool(spec.get("enabled", True)),
        )


@dataclass(frozen=True, slots=True)
class RuleOutcome:
    """The result of evaluating one rule."""

    rule_id: str
    outcome: Outcome
    fired: bool
    reason: str = ""
    matched_conditions: tuple[str, ...] = ()
    actions: tuple[Action, ...] = ()
    severity: Severity = Severity.BLOCKING
    name: str = ""
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ruleId": self.rule_id,
            "name": self.name,
            "outcome": str(self.outcome),
            "fired": self.fired,
            "severity": str(self.severity),
            "reason": self.reason,
            "matchedConditions": list(self.matched_conditions),
            "actions": [a.to_dict() for a in self.actions],
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    """Aggregate outcome of evaluating a whole rule set."""

    decision: Outcome
    rules: tuple[RuleOutcome, ...]
    facts: dict[str, Any]
    rule_set_id: str = ""
    version: int = 1
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    duration_ms: float = 0.0

    @property
    def fired_rules(self) -> list[RuleOutcome]:
        return [r for r in self.rules if r.fired]

    @property
    def blocking_failures(self) -> list[RuleOutcome]:
        return [
            r for r in self.rules
            if r.fired and r.outcome is Outcome.FAIL and r.severity is Severity.BLOCKING
        ]

    @property
    def review_rules(self) -> list[RuleOutcome]:
        return [r for r in self.rules if r.fired and r.outcome is Outcome.REVIEW]

    @property
    def reason_codes(self) -> list[str]:
        return list(self.facts.get("reasonCodes", []))

    @property
    def requirements(self) -> list[dict[str, Any]]:
        return list(self.facts.get("requirements", []))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ruleSetId": self.rule_set_id,
            "version": self.version,
            "decision": str(self.decision),
            "evaluatedAt": self.evaluated_at.isoformat(),
            "durationMs": round(self.duration_ms, 3),
            "ruleOutcomes": [r.to_dict() for r in self.rules],
            "firedRuleCount": len(self.fired_rules),
            "reasonCodes": self.reason_codes,
            "requirements": self.requirements,
            "derivedFacts": {
                k: v for k, v in self.facts.items()
                if k not in {"reasonCodes", "messages", "requirements"}
            },
            "messages": self.facts.get("messages", []),
        }


@dataclass(frozen=True, slots=True)
class RuleSet:
    """An immutable, versioned collection of rules plus its decision policy."""

    rule_set_id: str
    version: int
    name: str
    rules: tuple[Rule, ...]
    domain: str = "general"
    description: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    created_by: str = "system"
    status: str = "published"
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        ids = [r.rule_id for r in self.rules]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            msg = f"duplicate rule ids in rule set '{self.rule_set_id}': {sorted(duplicates)}"
            raise ValidationError(msg, duplicates=sorted(duplicates))

    def get(self, rule_id: str) -> Rule:
        for rule in self.rules:
            if rule.rule_id == rule_id:
                return rule
        raise ValidationError(f"rule '{rule_id}' not found in '{self.rule_set_id}'", ruleId=rule_id)

    def fingerprint(self) -> str:
        payload = "|".join(sorted(rule.fingerprint() for rule in self.rules))
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def evaluate(self, facts: Mapping[str, Any]) -> EvaluationResult:
        """Evaluate every rule and derive the overall decision.

        Decision policy (deliberately explicit and configurable):
        1. any blocking ``fail``            -> ``FAIL``
        2. else any ``review``              -> ``REVIEW``
        3. else any ``info``/``warning``    -> ``PASS`` with advisories
        4. else                              -> ``PASS``
        """
        started = datetime.now(UTC)
        working = dict(facts)
        outcomes: list[RuleOutcome] = []
        for rule in self.rules:  # deterministic order: declaration order
            result = rule.evaluate(working)
            outcomes.append(result)
            for action in result.actions:
                action.apply(working, [])

        blocking = [
            o for o in outcomes
            if o.fired and o.outcome is Outcome.FAIL and o.severity is Severity.BLOCKING
        ]
        reviews = [o for o in outcomes if o.fired and o.outcome is Outcome.REVIEW]
        if blocking:
            decision = Outcome.FAIL
        elif reviews:
            decision = Outcome.REVIEW
        else:
            decision = Outcome.PASS

        duration = (datetime.now(UTC) - started).total_seconds() * 1000
        return EvaluationResult(
            decision=decision,
            rules=tuple(outcomes),
            facts=working,
            rule_set_id=self.rule_set_id,
            version=self.version,
            duration_ms=duration,
        )

    def simulate(self, cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Run the rule set against labelled cases; the plugin 4 sandbox uses this."""
        results = []
        for case in cases:
            facts = {k: v for k, v in case.items() if k != "_label"}
            label = str(case.get("_label") or facts.get("policyId") or len(results))
            outcome = self.evaluate(facts)
            results.append({
                "case": label,
                "decision": str(outcome.decision),
                "firedRules": [o.rule_id for o in outcome.fired_rules],
                "reasonCodes": outcome.reason_codes,
                "requirements": [r["code"] for r in outcome.requirements],
            })
        passed = sum(1 for r in results if r["decision"] == "pass")
        return {
            "ruleSetId": self.rule_set_id,
            "version": self.version,
            "caseCount": len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "results": results,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "ruleSetId": self.rule_set_id,
            "name": self.name,
            "version": self.version,
            "domain": self.domain,
            "description": self.description,
            "status": self.status,
            "createdAt": self.created_at.isoformat(),
            "createdBy": self.created_by,
            "tags": list(self.tags),
            "fingerprint": self.fingerprint(),
            "rules": [r.to_dict() for r in self.rules],
        }

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any]) -> RuleSet:
        rules = [Rule.from_spec(r) for r in (spec.get("rules") or [])]
        return cls(
            rule_set_id=str(spec.get("ruleSetId") or spec.get("id") or "unnamed"),
            version=int(spec.get("version", 1)),
            name=str(spec.get("name") or spec.get("ruleSetId") or "unnamed"),
            rules=tuple(rules),
            domain=str(spec.get("domain", "general")),
            description=str(spec.get("description", "")),
            created_by=str(spec.get("createdBy", "system")),
            status=str(spec.get("status", "published")),
            tags=tuple(str(t) for t in (spec.get("tags") or [])),
        )


class RuleVersionStore:
    """Keeps every published version so rollback is a single call.

    Backed by PostgreSQL in production; the in-memory implementation makes the
    underwriter dashboard and the tests fully functional without a database.
    """

    def __init__(self) -> None:
        self._versions: dict[str, list[RuleSet]] = {}

    def publish(self, rule_set: RuleSet) -> RuleSet:
        versions = self._versions.setdefault(rule_set.rule_set_id, [])
        if any(v.version == rule_set.version for v in versions):
            msg = f"version {rule_set.version} of '{rule_set.rule_set_id}' already exists"
            raise BusinessRuleViolation(msg, ruleId=rule_set.rule_set_id)
        versions.append(rule_set)
        versions.sort(key=lambda v: v.version)
        return rule_set

    def next_version(self, rule_set_id: str) -> int:
        versions = self._versions.get(rule_set_id, [])
        return (max(v.version for v in versions) + 1) if versions else 1

    def get(self, rule_set_id: str, version: int | None = None) -> RuleSet:
        versions = self._versions.get(rule_set_id)
        if not versions:
            msg = f"rule set '{rule_set_id}' has no published versions"
            raise ValidationError(msg, ruleSetId=rule_set_id)
        if version is None:
            return versions[-1]
        for candidate in versions:
            if candidate.version == version:
                return candidate
        msg = f"rule set '{rule_set_id}' has no version {version}"
        raise ValidationError(msg, ruleSetId=rule_set_id, available=[v.version for v in versions])

    def versions(self, rule_set_id: str) -> list[RuleSet]:
        return list(self._versions.get(rule_set_id, []))

    def rollback(self, rule_set_id: str, to_version: int, *, by: str = "system") -> RuleSet:
        """Re-publish an earlier version as a new version (never mutate history)."""
        target = self.get(rule_set_id, to_version)
        restored = RuleSet(
            rule_set_id=target.rule_set_id,
            version=self.next_version(rule_set_id),
            name=target.name,
            rules=target.rules,
            domain=target.domain,
            description=f"Rollback to v{to_version}",
            created_by=by,
            status="published",
            tags=target.tags + (f"rollback-to-v{to_version}",),
        )
        return self.publish(restored)

    def diff(self, rule_set_id: str, from_version: int, to_version: int) -> dict[str, Any]:
        """Field-level diff between two versions, for the reviewer UI."""
        left = self.get(rule_set_id, from_version)
        right = self.get(rule_set_id, to_version)
        left_rules = {r.rule_id: r for r in left.rules}
        right_rules = {r.rule_id: r for r in right.rules}
        added = sorted(set(right_rules) - set(left_rules))
        removed = sorted(set(left_rules) - set(right_rules))
        changed = []
        for rule_id in sorted(set(left_rules) & set(right_rules)):
            if left_rules[rule_id].fingerprint() != right_rules[rule_id].fingerprint():
                changed.append({
                    "ruleId": rule_id,
                    "before": left_rules[rule_id].to_dict(),
                    "after": right_rules[rule_id].to_dict(),
                })
        return {
            "ruleSetId": rule_set_id,
            "fromVersion": from_version,
            "toVersion": to_version,
            "added": added,
            "removed": removed,
            "changed": changed,
            "unchangedCount": len(set(left_rules) & set(right_rules)) - len(changed),
        }

    def all_rule_sets(self) -> list[RuleSet]:
        return [versions[-1] for versions in self._versions.values()]


class RuleEngine:
    """Evaluates rule sets and remembers which version each tenant runs."""

    def __init__(self, store: RuleVersionStore | None = None) -> None:
        self.store = store or RuleVersionStore()

    def load(self, specs: Iterable[Mapping[str, Any]], *, publish: bool = True) -> list[RuleSet]:
        """Load rule sets from dictionaries or YAML-loaded mappings."""
        loaded: list[RuleSet] = []
        for spec in specs:
            rule_set = RuleSet.from_spec(spec)
            if publish:
                self.store.publish(rule_set)
            loaded.append(rule_set)
        return loaded

    def evaluate(
        self,
        rule_set_id: str,
        facts: Mapping[str, Any],
        *,
        version: int | None = None,
    ) -> EvaluationResult:
        return self.store.get(rule_set_id, version).evaluate(facts)

    def assert_passes(
        self, rule_set_id: str, facts: Mapping[str, Any], *, version: int | None = None
    ) -> EvaluationResult:
        """Evaluate and raise :class:`BusinessRuleViolation` when it fails."""
        result = self.evaluate(rule_set_id, facts, version=version)
        if result.decision is not Outcome.PASS:
            failed = [r.rule_id for r in result.blocking_failures or result.review_rules]
            msg = (
                f"rule set '{rule_set_id}' returned {result.decision} "
                f"({len(failed)} rule(s) fired: {', '.join(failed[:5])})"
            )
            raise BusinessRuleViolation(
                msg,
                rule_id=rule_set_id,
                ruleSetId=rule_set_id,
                firedRules=failed,
                reasonCodes=result.reason_codes,
            )
        return result

    def matches(self, rule_set_id: str, pattern: str) -> list[Rule]:
        """Glob-search a rule set; used by the underwriter's rule search box."""
        rule_set = self.store.get(rule_set_id)
        return [
            r for r in rule_set.rules
            if fnmatch.fnmatch(r.rule_id.lower(), pattern.lower())
            or fnmatch.fnmatch(r.name.lower(), pattern.lower())
            or any(fnmatch.fnmatch(t.lower(), pattern.lower()) for t in r.tags)
        ]
