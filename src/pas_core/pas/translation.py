"""Configuration-driven translation rules engine.

The specification requires "translation rules engine (YAML/JSON config per PAS
vendor)".  This module is that engine: it maps canonical payloads to and from a
vendor dialect using declarative rules loaded from YAML, so a new carrier or a
new vendor version is onboarded by configuration rather than by a release.

Rule kinds
----------
``field``      rename / nest / flatten a field, with an optional type cast
``enum``       translate a code set (policy status, coverage type, gender...)
``constant``   inject a vendor-mandated constant
``compute``    derive a value from other fields with a safe expression
``drop``       remove a field the vendor rejects
``require``    fail fast when a vendor-mandatory field is absent
``wrap``       wrap the payload in a vendor envelope
``unwrap``     extract the payload from a vendor envelope by JSON path
``map_array``  apply a sub-rule set to every element of an array
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic import ValidationError as PydanticValidationError

from pas_core.errors import TranslationError
from pas_core.observability import trace_span

RULES_DIR = Path(__file__).parent / "rules"

_COMPUTE_PATTERN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(==|!=|>=|<=|>|<|\+|-|\*|/)\s*(.+?)\s*$")


class RuleKind(StrEnum):
    """Discriminator for the kinds of rule this engine understands.

    Keeping the vocabulary as an enum means a typo in a YAML profile fails loudly
    at load time instead of silently no-op'ing at runtime.
    """

    FIELD = "field"
    ENUM = "enum"
    CONSTANT = "constant"
    COMPUTE = "compute"
    DROP = "drop"
    REQUIRE = "require"
    WRAP = "wrap"
    UNWRAP = "unwrap"
    MAP_ARRAY = "map_array"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


class TranslationRule(BaseModel):
    """A single declarative rule."""

    model_config = ConfigDict(extra="forbid")

    kind: RuleKind = Field(description="Rule kind; see pas_core.pas.translation.RuleKind.")
    source: str | None = Field(default=None, description="Source field path (dot notation).")
    target: str | None = Field(default=None, description="Target field path (dot notation).")
    value: Any = None
    mapping: dict[str, str] = Field(default_factory=dict)
    cast: str | None = Field(default=None, description="str, int, float, bool, date, currency, upper, lower.")
    default: Any = None
    condition: str | None = Field(default=None, description="`field op value` guard; rule skipped when false.")
    path: str | None = Field(default=None, description="JSON path for wrap/unwrap, e.g. data.payload.")
    rules: list[TranslationRule] = Field(default_factory=list)
    description: str = Field(default="", description="Why this rule exists - surfaced in audit traces.")
    when_missing: Literal["skip", "error", "null", "default"] = Field(
        default="skip",
        description="Behaviour when the source value is absent.",
    )

    @field_validator("cast")
    @classmethod
    def _known_cast(cls, value: str | None) -> str | None:
        if value is None:
            return None
        allowed = {"str", "int", "float", "bool", "date", "currency", "upper", "lower", "iso_date"}
        if value not in allowed:
            msg = f"unsupported cast '{value}'; expected one of {sorted(allowed)}"
            raise ValueError(msg)
        return value


class OperationTranslation(BaseModel):
    """Rules for one atomic operation, per direction."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str
    description: str = ""
    vendor: str = "generic"
    to_vendor: list[TranslationRule] = Field(default_factory=list)
    from_vendor: list[TranslationRule] = Field(default_factory=list)

    @property
    def direction_count(self) -> int:
        return len(self.to_vendor) + len(self.from_vendor)


class VendorTranslationProfile(BaseModel):
    """A full translation profile for one PAS vendor."""

    model_config = ConfigDict(extra="forbid")

    vendor: str
    display_name: str = ""
    version: str = "1.0.0"
    description: str = ""
    source_ref: str | None = None
    defaults: dict[str, Any] = Field(default_factory=dict)
    operations: list[OperationTranslation] = Field(default_factory=list)

    def for_operation(self, operation_id: str) -> OperationTranslation | None:
        for translation in self.operations:
            if translation.operation_id == operation_id:
                return translation
        return None

    def operation_ids(self) -> list[str]:
        return [t.operation_id for t in self.operations]

    def stats(self) -> dict[str, int]:
        return {
            "operations": len(self.operations),
            "toVendorRules": sum(len(t.to_vendor) for t in self.operations),
            "fromVendorRules": sum(len(t.from_vendor) for t in self.operations),
        }


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
def get_path(source: Mapping[str, Any], path: str, default: Any = None) -> Any:  # noqa: ANN401
    """Read a dotted path, supporting ``a.b[0].c`` array indexing."""
    current: Any = source
    for segment in _split_path(path):
        if current is None:
            return default
        if isinstance(segment, int):
            if not isinstance(current, Sequence) or isinstance(current, str):
                return default
            if segment >= len(current):
                return default
            current = current[segment]
        else:
            if not isinstance(current, Mapping) or segment not in current:
                return default
            current = current[segment]
    return current


def set_path(target: dict[str, Any], path: str, value: Any) -> None:  # noqa: ANN401
    """Write a dotted path, creating intermediate dictionaries."""
    segments = _split_path(path)
    if not segments:
        return
    current: Any = target
    for segment in segments[:-1]:
        if isinstance(segment, int):
            if not isinstance(current, list):
                return
            while len(current) <= segment:
                current.append({})
            current = current[segment]
        else:
            nxt = current.get(segment)
            if not isinstance(nxt, dict):
                nxt = {}
                current[segment] = nxt
            current = nxt
    last = segments[-1]
    if isinstance(last, int):
        if isinstance(current, list):
            while len(current) <= last:
                current.append(None)
            current[last] = value
    elif isinstance(current, dict):
        current[last] = value


def delete_path(target: dict[str, Any], path: str) -> None:
    """Remove a dotted path from a nested mapping."""
    segments = _split_path(path)
    if not segments:
        return
    parent: Any = target
    for segment in segments[:-1]:
        if isinstance(segment, int):
            if not isinstance(parent, list) or segment >= len(parent):
                return
            parent = parent[segment]
        else:
            if not isinstance(parent, dict) or segment not in parent:
                return
            parent = parent[segment]
    last = segments[-1]
    if isinstance(last, int):
        if isinstance(parent, list) and last < len(parent):
            parent.pop(last)
    elif isinstance(parent, dict):
        parent.pop(last, None)


def _split_path(path: str) -> list[str | int]:
    segments: list[str | int] = []
    for part in path.split("."):
        if not part:
            continue
        if "[" in part and part.endswith("]"):
            name, _, index_part = part.partition("[")
            if name:
                segments.append(name)
            segments.append(int(index_part[:-1]))
        else:
            segments.append(part)
    return segments


# ---------------------------------------------------------------------------
# Casts
# ---------------------------------------------------------------------------
_CURRENCY_RE = re.compile(r"[^\d.\-]")


def apply_cast(value: Any, cast: str | None) -> Any:  # noqa: ANN401
    """Apply a declared type coercion; failures raise a translation error."""
    if cast is None or value is None:
        return value
    try:
        if cast == "str":
            return str(value)
        if cast == "int":
            return int(float(str(value).replace(",", "")))
        if cast == "float":
            return float(_CURRENCY_RE.sub("", str(value)) or 0.0)
        if cast == "bool":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"true", "1", "yes", "y", "on"}
        if cast == "currency":
            return float(_CURRENCY_RE.sub("", str(value)) or 0.0)
        if cast == "date":
            return _to_iso(str(value))
        if cast == "iso_date":
            return _to_iso(str(value))
        if cast == "upper":
            return str(value).upper()
        if cast == "lower":
            return str(value).lower()
    except (TypeError, ValueError) as exc:
        msg = f"cannot cast {value!r} to {cast}"
        raise TranslationError(msg, path=cast) from exc
    return value


def _to_iso(value: str) -> str:
    text = value.strip()
    for pattern, order in (
        (r"^(\d{4})-(\d{2})-(\d{2})", "ymd"),
        (r"^(\d{4})(\d{2})(\d{2})$", "ymd"),
        (r"^(\d{1,2})/(\d{1,2})/(\d{4})$", "mdy"),
    ):
        match = re.match(pattern, text)
        if match:
            groups = match.groups()
            if order == "ymd":
                return f"{groups[0]}-{groups[1]}-{groups[2]}"
            return f"{groups[2]}-{int(groups[0]):02d}-{int(groups[1]):02d}"
    raise TranslationError(f"unrecognised date value '{value}'", path="date")


def _evaluate_condition(payload: Mapping[str, Any], condition: str | None) -> bool:
    """Evaluate a safe ``field op value`` guard. Unsupported guards are ignored."""
    if not condition:
        return True
    match = _COMPUTE_PATTERN.match(condition)
    if not match:
        return True
    left_field, operator, right_literal = match.groups()
    left = get_path(payload, left_field)
    right: Any = right_literal.strip().strip("'\"")
    try:
        if left is not None:
            left = float(left) if operator in {"==", "!=", ">", "<", ">=", "<=", "+", "-", "*", "/"} else left
        if right.replace(".", "", 1).lstrip("-").isdigit() and operator in {"==", "!=", ">", "<", ">=", "<="}:
            right = float(right)
    except ValueError:
        pass
    try:
        if operator == "==":
            return left == right
        if operator == "!=":
            return left != right
        if operator == ">":
            return left > right  # type: ignore[operator]
        if operator == "<":
            return left < right  # type: ignore[operator]
        if operator == ">=":
            return left >= right  # type: ignore[operator]
        if operator == "<=":
            return left <= right  # type: ignore[operator]
    except TypeError:
        return False
    return True


@dataclass(slots=True)
class TranslationResult:
    """Outcome of a translation, with a field-level trace for the audit trail."""

    payload: dict[str, Any]
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_trace(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "skipped": self.skipped,
            "errors": self.errors,
        }


class TranslationEngine:
    """Applies :class:`VendorTranslationProfile` rules to payloads."""

    def __init__(self, profiles: dict[str, VendorTranslationProfile] | None = None) -> None:
        self._profiles: dict[str, VendorTranslationProfile] = dict(profiles or {})

    def register(self, profile: VendorTranslationProfile) -> VendorTranslationProfile:
        self._profiles[profile.vendor] = profile
        return profile

    def profile(self, vendor: str) -> VendorTranslationProfile | None:
        return self._profiles.get(vendor)

    def vendors(self) -> list[str]:
        return sorted(self._profiles)

    def load_directory(self, directory: Path | str = RULES_DIR) -> list[VendorTranslationProfile]:
        """Load every ``*.yaml`` profile in a directory.

        A malformed profile is a deployment error, not a warning: it fails loudly
        with the offending file named, because a silently-ignored profile means
        requests get sent in the wrong dialect.
        """
        loaded: list[VendorTranslationProfile] = []
        for path in sorted(Path(directory).glob("*.yaml")):
            profile = load_profile_file(path)
            self.register(profile)
            loaded.append(profile)
        return loaded

    def to_vendor(
        self, vendor: str, operation_id: str, payload: dict[str, Any]
    ) -> TranslationResult:
        """Translate a canonical payload into the vendor dialect."""
        return self._apply(vendor, operation_id, payload, direction="to_vendor")

    def from_vendor(
        self, vendor: str, operation_id: str, payload: dict[str, Any]
    ) -> TranslationResult:
        """Translate a vendor response back into the canonical shape."""
        return self._apply(vendor, operation_id, payload, direction="from_vendor")

    def _apply(
        self, vendor: str, operation_id: str, payload: dict[str, Any], *, direction: str
    ) -> TranslationResult:
        profile = self._profiles.get(vendor)
        if profile is None:
            return TranslationResult(payload=dict(payload))
        translation = profile.for_operation(operation_id)
        if translation is None:
            result = TranslationResult(payload=dict(payload))
            result.applied.append(f"passthrough:{vendor}:{operation_id}")
            return result
        rules = translation.to_vendor if direction == "to_vendor" else translation.from_vendor
        working = dict(payload)
        if direction == "to_vendor":
            # Profile defaults describe what the *vendor* requires on an outbound
            # request (system-of-record tags, envelope versions). They must never
            # be injected into a response we are normalising.
            for key, value in profile.defaults.items():
                working.setdefault(key, value)
        return self._execute(rules, working, translation.operation_id, direction)

    def _execute(
        self,
        rules: Iterable[TranslationRule],
        payload: dict[str, Any],
        operation_id: str,
        direction: str,
    ) -> TranslationResult:
        result = TranslationResult(payload=dict(payload))
        with trace_span("pas.translation", vendor=operation_id, direction=direction):
            for index, rule in enumerate(rules):
                label = f"{index}:{rule.kind}:{rule.target or rule.source or rule.path or ''}"
                try:
                    self._execute_one(rule, result.payload, result, operation_id)
                    result.applied.append(label)
                except TranslationError as exc:
                    result.errors.append(f"{label}: {exc.message}")
                    if rule.when_missing == "error" or rule.kind == "require":
                        raise
        return result

    def _execute_one(
        self,
        rule: TranslationRule,
        payload: dict[str, Any],
        result: TranslationResult,
        operation_id: str,
    ) -> None:
        kind = rule.kind

        if kind == "require":
            value = get_path(payload, rule.source or "")
            if value in (None, "", [], {}):
                msg = (
                    f"vendor '{operation_id}' requires field '{rule.source}' "
                    f"({rule.description or 'no description provided'})"
                )
                raise TranslationError(msg, path=rule.source or "")
            return

        if kind == "constant":
            set_path(payload, rule.target or "", rule.value)
            return

        if kind == "drop":
            delete_path(payload, rule.source or "")
            return

        if kind == "unwrap":
            path = rule.path or rule.source or ""
            extracted = get_path(payload, path)
            if extracted is None:
                if rule.when_missing == "skip":
                    result.skipped.append(f"unwrap:{path}")
                    return
                if rule.when_missing == "null":
                    payload.clear()
                    return
                raise TranslationError(
                    f"expected to unwrap '{path}' from the {operation_id} response",
                    path=path,
                )
            payload.clear()
            payload.update(extracted if isinstance(extracted, dict) else {"value": extracted})
            return

        if kind == "wrap":
            path = rule.path or rule.target or ""
            wrapped: dict[str, Any] = {}
            set_path(wrapped, path, payload)
            payload.clear()
            payload.update(wrapped)
            return

        if not _evaluate_condition(payload, rule.condition):
            result.skipped.append(f"condition:{rule.condition}")
            return

        source_value = get_path(payload, rule.source or "")

        if kind == "field":
            if source_value is None:
                if rule.when_missing == "error":
                    raise TranslationError(
                        f"required source field '{rule.source}' missing", path=rule.source
                    )
                if rule.when_missing == "default" and rule.default is not None:
                    set_path(payload, rule.target or "", rule.default)
                else:
                    result.skipped.append(f"missing:{rule.source}")
                return
            set_path(payload, rule.target or "", apply_cast(source_value, rule.cast))
            if rule.source and rule.target and rule.source != rule.target:
                delete_path(payload, rule.source)
            return

        if kind == "enum":
            if source_value is None:
                result.skipped.append(f"missing:{rule.source}")
                return
            # Vendor vocabularies are inconsistently cased ("InForce", "inforce",
            # "INFORCE"); treat enum translation case-insensitively so a rule
            # author never has to enumerate every spelling.
            lookup = {k.casefold(): v for k, v in rule.mapping.items()}
            mapped = lookup.get(str(source_value).casefold())
            if mapped is None:
                if rule.when_missing == "error":
                    raise TranslationError(
                        f"value '{source_value}' of '{rule.source}' has no vendor mapping; "
                        f"known values: {sorted(rule.mapping)}",
                        path=rule.source or "",
                    )
                result.skipped.append(f"unmapped:{rule.source}={source_value}")
                return
            set_path(payload, rule.target or "", apply_cast(mapped, rule.cast))
            if rule.source and rule.target and rule.source != rule.target:
                delete_path(payload, rule.source)
            return

        if kind == "compute":
            computed = self._compute(rule, payload)
            if computed is None:
                if rule.when_missing == "error":
                    raise TranslationError(
                        f"cannot compute '{rule.target}': operands missing", path=rule.target or ""
                    )
                result.skipped.append(f"uncomputed:{rule.target}")
                return
            set_path(payload, rule.target or "", computed)
            return

        if kind == "map_array":
            source = get_path(payload, rule.source or "")
            if not isinstance(source, list):
                result.skipped.append(f"not-an-array:{rule.source}")
                return
            sub_result = TranslationResult(payload={})
            mapped_items: list[Any] = []
            for item in source:
                child = TranslationResult(payload=dict(item) if isinstance(item, dict) else {"value": item})
                for sub_rule in rule.rules:
                    self._execute_one(sub_rule, child.payload, child, operation_id)
                mapped_items.append(child.payload)
            payload.update(sub_result.payload)
            set_path(payload, rule.target or rule.source or "", mapped_items)
            return

        raise TranslationError(f"unsupported rule kind '{kind}'", path=rule.kind)

    def _compute(self, rule: TranslationRule, payload: Mapping[str, Any]) -> Any:  # noqa: ANN401
        """Evaluate ``target = source op value`` with a deliberately tiny grammar.

        Only arithmetic and concatenation over literal or payload-derived operands
        are supported.  There is no ``eval``: a translation profile is operator
        configuration, and configuration must never be able to execute code.
        """
        left_raw = get_path(payload, rule.source or "")
        operator, right_raw = self._parse_expression(rule.value)
        if operator is None:
            return None
        left = self._operand(left_raw, rule.cast, numeric=True)
        right = self._operand(right_raw, None, numeric=True)
        if left is None or right is None:
            return None
        try:
            if operator == "+":
                if isinstance(left, str) or isinstance(right, str):
                    return f"{left}{right}"
                return float(left) + float(right)
            if operator == "-":
                return float(left) - float(right)
            if operator == "*":
                return float(left) * float(right)
            if operator == "/":
                if float(right) == 0:
                    raise TranslationError("division by zero in compute rule", path=rule.target or "")
                return float(left) / float(right)
        except (TypeError, ValueError) as exc:
            raise TranslationError(f"compute failed: {exc}", path=rule.target or "") from exc
        return None

    @staticmethod
    def _parse_expression(expression: Any) -> tuple[str | None, Any]:  # noqa: ANN401
        if expression is None:
            return None, None
        text = str(expression).strip()
        for operator in ("+", "-", "*", "/"):
            index = text.find(operator)
            if index > 0:
                return operator, text[index + 1 :].strip()
        return None, text

    @staticmethod
    def _operand(raw: Any, cast: str | None, *, numeric: bool) -> Any:  # noqa: ANN401
        if raw is None:
            return None
        text = str(raw).strip().strip("'\"")
        if numeric:
            cleaned = _CURRENCY_RE.sub("", text)
            try:
                return float(cleaned)
            except ValueError:
                return None
        return apply_cast(raw, cast)


def load_profile_file(path: Path) -> VendorTranslationProfile:
    """Parse one YAML profile, converting failures into a catalogue error."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        msg = f"{path.name} is not valid YAML: {exc}"
        raise TranslationError(msg, path=str(path)) from exc
    raw.setdefault("vendor", path.stem)
    try:
        return VendorTranslationProfile.model_validate(raw)
    except PydanticValidationError as exc:
        violations = [
            {
                "path": ".".join(str(p) for p in e["loc"]),
                "message": e["msg"],
            }
            for e in exc.errors()[:20]
        ]
        msg = (
            f"Translation profile '{path.name}' is invalid: "
            f"{'; '.join(f'{v['path']}: {v['message']}' for v in violations[:3])}"
        )
        raise TranslationError(msg, path=str(path), violations=violations) from exc


# ---------------------------------------------------------------------------
# Module-level engine shared by the gateway
# ---------------------------------------------------------------------------
_ENGINE: TranslationEngine | None = None


def get_engine(*, rules_dir: Path | str | None = None) -> TranslationEngine:
    """Return the process-wide engine, loading bundled profiles on first use."""
    global _ENGINE  # noqa: PLW0603
    if _ENGINE is None:
        engine = TranslationEngine()
        engine.load_directory(rules_dir or RULES_DIR)
        _ENGINE = engine
    return _ENGINE


def set_engine(engine: TranslationEngine) -> TranslationEngine:
    global _ENGINE  # noqa: PLW0603
    _ENGINE = engine
    return engine


def reset_engine() -> None:
    global _ENGINE  # noqa: PLW0603
    _ENGINE = None
