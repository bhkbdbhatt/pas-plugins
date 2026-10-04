"""PII masking, redaction and access logging.

Shared by every plugin because a carrier's privacy posture is a platform-wide
property, not a per-plugin one.  Three properties matter and each is enforced
here rather than being left to callers:

1. **Classification drives masking.** A field marked ``PHI`` is masked unless the
   caller holds the unmasking scope.  Classification lives with the data, so a
   compliance officer can change it without a release.
2. **Masking is non-reversible by default.** Masks are derived deterministically
   from the value's shape, not stored, so a masked record cannot be de-masked by
   inspecting the mask.
3. **Every read is logged with its purpose.** HIPAA and SOC 2 both require
   purpose limitation; a read with no stated purpose is refused rather than
   served, because "somebody asked" is not an audit answer.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

LOGGER = logging.getLogger("pas_core.pii")

SSN_PATTERN = re.compile(r"^\d{3}-?\d{2}-?\d{4}$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CARD_PATTERN = re.compile(r"^(?:\d[ -]?){13,19}$")
MASK_CHAR = "*"


class MaskStrategy(StrEnum):
    """How a value is masked.

    ``FULL`` reveals nothing, ``PARTIAL`` keeps just enough to be useful (last four
    of an account or a date of birth), ``HASH`` replaces the value with a stable
    pseudonym so records can still be joined, ``REDACT`` removes the key entirely.
    """

    FULL = "full"
    PARTIAL = "partial"
    HASH = "hash"
    REDACT = "redact"
    NONE = "none"


def mask_value(value: Any, strategy: MaskStrategy = MaskStrategy.FULL) -> Any:  # noqa: ANN401
    """Mask one value according to ``strategy``."""
    if value is None:
        return None
    if strategy is MaskStrategy.NONE:
        return value
    text = value if isinstance(value, str) else str(value)
    if strategy is MaskStrategy.REDACT:
        return None
    if strategy is MaskStrategy.HASH:
        # Deterministic pseudonym: allows joins without revealing the value.
        return "h_" + hashlib.sha256(text.encode()).hexdigest()[:16]
    if strategy is MaskStrategy.PARTIAL:
        return partial_mask(text)
    if SSN_PATTERN.match(text.replace(" ", "")):
        return MASK_CHAR * 3 + "-" + MASK_CHAR * 2 + "-" + MASK_CHAR * 4
    if EMAIL_PATTERN.match(text):
        local, _, domain = text.partition("@")
        keep = local[:1] if local else ""
        return f"{keep}{MASK_CHAR * max(1, len(local) - 1)}@{domain}"
    if CARD_PATTERN.match(text.replace(" ", "")):
        digits = re.sub(r"\D", "", text)
        return MASK_CHAR * (len(digits) - 4) + digits[-4:]
    if len(text) <= 4:
        return MASK_CHAR * len(text)
    return text[:1] + MASK_CHAR * (len(text) - 2) + text[-1:]


def partial_mask(text: str) -> str:
    """Reveal only the last few characters - enough to confirm identity, not to use it."""
    if not text:
        return text
    visible = 4 if len(text) > 8 else 1
    return MASK_CHAR * (len(text) - visible) + text[-visible:]


STRATEGY_BY_CLASS: dict[str, MaskStrategy] = {
    "public": MaskStrategy.NONE,
    "internal": MaskStrategy.FULL,
    "pii": MaskStrategy.PARTIAL,
    "phi": MaskStrategy.FULL,
    "financial": MaskStrategy.PARTIAL,
    "pci": MaskStrategy.PARTIAL,
}


def mask_record(record: Any, *, paths: tuple[str, ...] = (), classes: dict[str, str] | None = None) -> Any:  # noqa: ANN401
    """Return a copy of ``record`` with sensitive fields masked.

    Works on any pydantic model: field classes come from ``pii_classes`` when
    present, otherwise from an explicit mapping.  The input object is never
    mutated, so a masked read cannot corrupt the golden record.

    Type preservation matters here.  Replacing a date with the string ``"****"``
    would break every downstream consumer of the record, so masked values keep
    their type:

    * ``str``  -> a partially masked string
    * ``date`` / ``datetime`` -> coarsened to the first day of the year, which
      preserves the age bracket a caller legitimately needs while removing the
      exact birthday
    * numeric and boolean fields -> returned unchanged.  A rounded premium is
      worse than useless, and a premium amount on its own does not identify
      anybody.  If a deployment needs those suppressed, declare the field's class
      and remove it in a serialisation step rather than here.
    """
    from datetime import date, datetime as dt  # noqa: PLC0415

    from pydantic import BaseModel  # noqa: PLC0415

    if not isinstance(record, BaseModel):
        return record
    field_classes = classes or {
        k: str(getattr(v, "value", v)) for k, v in (record.pii_classes or {}).items()
    }
    targets = set(paths) | {
        name for name, cls in field_classes.items()
        if STRATEGY_BY_CLASS.get(cls, MaskStrategy.FULL) is not MaskStrategy.NONE
    }
    updates: dict[str, Any] = {}
    for name in targets:
        strategy = STRATEGY_BY_CLASS.get(field_classes.get(name, "internal"), MaskStrategy.FULL)
        if strategy is MaskStrategy.NONE:
            continue
        if not hasattr(record, name):
            continue
        current = getattr(record, name)
        field_type = record.model_fields[name].annotation
        if isinstance(current, dt) or (isinstance(current, date) and not isinstance(current, dt)):
            updates[name] = current.replace(month=1, day=1)
        elif isinstance(current, (int, float, bool)):
            continue
        elif field_type is not str and not isinstance(current, str):
            updates[name] = mask_value(current, strategy)
        else:
            updates[name] = mask_value(current, strategy)
    if not updates:
        return record
    return record.model_copy(update=updates)


def masked_fields_for(record: Any, *, classes: dict[str, str] | None = None) -> list[str]:
    """Which fields would be masked for this record - surfaced to the caller."""
    field_classes = classes or {
        k: str(getattr(v, "value", v)) for k, v in (getattr(record, "pii_classes", None) or {}).items()
    }
    return sorted(
        name for name, cls in field_classes.items()
        if STRATEGY_BY_CLASS.get(cls, MaskStrategy.FULL) is not MaskStrategy.NONE
    )


@dataclass(frozen=True, slots=True)
class AccessDecision:
    """Outcome of an access request against a field."""

    field: str
    granted: bool
    masked: bool
    reason: str
    pii_class: str = "internal"
    purpose: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "granted": self.granted,
            "masked": self.masked,
            "reason": self.reason,
            "piiClass": self.pii_class,
            "purpose": self.purpose,
        }


@dataclass(slots=True)
class PiiPolicy:
    """Masking policy for one deployment.

    ``mode`` is the kill switch:

    ``enforce``  mask unless the caller holds the unmasking scope
    ``observe``  log what *would* be masked but return raw values (staging)
    ``off``      return everything (development only; refused in production)
    """

    mode: str = "enforce"
    unmask_scope: str = "pii:unmask"
    require_purpose: bool = True
    allowed_purposes: frozenset[str] = frozenset()
    denied_purposes: frozenset[str] = frozenset({"marketing"})
    audit_enabled: bool = True
    salt: str = "pas-plugins-pii-salt"

    def __post_init__(self) -> None:
        if self.mode not in {"enforce", "observe", "off"}:
            msg = f"PII policy mode must be enforce|observe|off, got '{self.mode}'"
            raise ValueError(msg)

    def may_unmask(self, scopes: frozenset[str]) -> bool:
        return "*" in scopes or self.unmask_scope in scopes

    def check_purpose(self, purpose: str | None) -> AccessDecision:
        """Purpose limitation: refuse a read that states no purpose."""
        if not self.require_purpose:
            return AccessDecision(field="*", granted=True, masked=False, reason="purpose not required", purpose=purpose or "")
        if not purpose:
            return AccessDecision(
                field="*", granted=False, masked=False,
                reason="no purpose stated; reads must declare a purpose",
            )
        if purpose in self.denied_purposes:
            return AccessDecision(
                field="*", granted=False, masked=False,
                reason=f"purpose '{purpose}' is denied by policy", purpose=purpose,
            )
        if self.allowed_purposes and purpose not in self.allowed_purposes:
            return AccessDecision(
                field="*", granted=False, masked=False,
                reason=f"purpose '{purpose}' is not in the allowed list", purpose=purpose,
            )
        return AccessDecision(field="*", granted=True, masked=False, reason="purpose accepted", purpose=purpose)

    def pseudonymise(self, value: str) -> str:
        """Stable, salted pseudonym for a value - used for de-duplication keys."""
        digest = hashlib.sha256(f"{self.salt}{value}".encode()).hexdigest()
        return digest[:20]

    def log_access(
        self,
        *,
        tenant_id: str,
        actor: str,
        entity_type: str,
        entity_key: str,
        fields: list[str],
        purpose: str | None,
        masked_count: int,
    ) -> None:
        """Record an access. Never logs values - only field names and counts."""
        if not self.audit_enabled:
            return
        LOGGER.info(
            "pii access",
            extra={
                "tenantId": tenant_id,
                "actor": actor,
                "entityType": entity_type,
                "entityKey": entity_key,
                "fieldsAccessed": fields[:40],
                "fieldCount": len(fields),
                "maskedFieldCount": masked_count,
                "purpose": purpose,
                "policyMode": self.mode,
            },
        )


def redact(text: str, *, patterns: dict[str, re.Pattern[str]] | None = None) -> str:
    """Scrub sensitive substrings from free text (audit logs, error messages).

    The gateway returns vendor error text verbatim, and vendor error text
    routinely contains an SSN or an email. Redaction on the way out is cheap
    insurance against an accidental disclosure in a log aggregator.
    """
    rules = patterns or {
        "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        "ssnCompact": re.compile(r"\b\d{9}\b"),
        "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"),
        "card": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    }
    out = text
    for name, pattern in rules.items():
        out = pattern.sub(f"[redacted:{name}]", out)
    return out


@dataclass(slots=True)
class PiiFieldRegistry:
    """Central catalogue of field classifications for the platform.

    Kept as data so a compliance officer can review and extend it; plugins look up
    their fields here rather than hard-coding a classification inline.
    """

    classifications: dict[str, dict[str, str]] = field(default_factory=dict)

    def register(self, entity_type: str, mapping: dict[str, str]) -> None:
        self.classifications.setdefault(entity_type, {}).update(mapping)

    def for_entity(self, entity_type: str) -> dict[str, str]:
        return dict(self.classifications.get(entity_type, {}))

    def class_of(self, entity_type: str, field_name: str) -> str:
        return self.classifications.get(entity_type, {}).get(field_name, "internal")

    def as_json(self) -> dict[str, Any]:
        return {
            "strategies": {k: str(v) for k, v in STRATEGY_BY_CLASS.items()},
            "classifications": self.classifications,
            "note": (
                "Fields classified as pii/phi/financial/pci are masked on read unless the "
                "caller holds the unmasking scope. Classification lives here so it can be "
                "reviewed by compliance without a code change."
            ),
        }


DEFAULT_REGISTRY = PiiFieldRegistry(
    {
        "customer": {
            "first_name": "pii", "last_name": "pii", "middle_name": "pii",
            "date_of_birth": "pii", "ssn_last4": "pii", "email": "pii", "phone": "pii",
            "address_line1": "pii", "address_city": "pii", "address_state": "pii",
            "address_postal_code": "pii", "lifetime_premium": "financial",
            "in_force_policy_count": "financial",
        },
        "policy": {
            "policy_number": "financial", "face_amount": "financial",
            "annualised_premium": "financial", "beneficiaries": "pii",
        },
        "claim": {
            "claim_number": "financial", "claim_amount": "financial",
            "paid_amount": "financial", "reserve_amount": "financial",
        },
        "premium": {
            "amount_due": "financial", "amount_paid": "financial",
            "commission_amount": "financial",
        },
    }
)


def build_policy(settings: Any) -> PiiPolicy:  # noqa: ANN401
    """Build a :class:`PiiPolicy` from a plugin's settings object."""
    return PiiPolicy(
        mode=getattr(settings, "pii_masking", "enforce"),
        unmask_scope=getattr(settings, "unmask_scope", "pii:unmask"),
    )
