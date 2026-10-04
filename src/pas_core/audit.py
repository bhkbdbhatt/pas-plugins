"""Tamper-evident, tenant-scoped audit trail.

Regulated carriers (and NAIC SSAP 102 / SOX-style controls) require evidence of
*who did what to which policy, when, from where, and what changed*.  This module
provides that as a first-class primitive rather than an afterthought:

* every mutating operation emits an :class:`AuditEvent`
* events are chained with SHA-256 so deletions or edits are detectable
* each event carries the ACORD transaction type when one applies
* sinks are pluggable: structured log, PostgreSQL, OpenTelemetry, or a webhook
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pas_core.tenancy import TenantContext, current_tenant_id, maybe_current_context

logger = logging.getLogger("pas_core.audit")


class AuditAction(StrEnum):
    """Canonical action verbs. Keep the list closed so dashboards stay stable."""

    CREATE = "create"
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"
    QUOTE = "quote"
    BIND = "bind"
    RATE = "rate"
    CHANGE = "change"
    LAPSE = "lapse"
    TERMINATE = "terminate"
    CLAIM = "claim"
    CALCULATE = "calculate"
    SCORE = "score"
    DECIDE = "decide"
    DEPLOY = "deploy"
    APPROVE = "approve"
    ROLLBACK = "rollback"
    AUTH = "auth"
    ACCESS = "access"
    EXPORT = "export"
    MCP_TOOL_CALL = "mcp.tool_call"
    LEDGER_COMMIT = "ledger.commit"


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """A single immutable audit record."""

    action: AuditAction
    resource_type: str
    resource_id: str
    outcome: AuditOutcome = AuditOutcome.SUCCESS
    tenant_id: str = field(default_factory=current_tenant_id)
    actor: str = "unknown"
    actor_type: str = "unknown"
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    correlation_id: str = ""
    source_ip: str | None = None
    user_agent: str | None = None
    acord_tx_type: str | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    changes: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    previous_hash: str = ""
    event_hash: str = ""
    sequence: int = 0
    schema_version: str = "1.0"

    def compute_hash(self) -> str:
        """SHA-256 over the canonical serialisation, chained to the previous event."""
        payload = {
            key: (value.isoformat() if isinstance(value, datetime) else value)
            for key, value in asdict(self).items()
            if key != "event_hash"
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(f"{self.previous_hash}|{canonical}".encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["occurred_at"] = self.occurred_at.isoformat()
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), default=str)


class AuditSink(Protocol):
    """Destination for audit events."""

    def write(self, event: AuditEvent) -> None: ...


class StructuredLogSink:
    """Writes one JSON object per line to the audit log stream."""

    def __init__(self, log: logging.Logger | None = None) -> None:
        self._log = log or logger

    def write(self, event: AuditEvent) -> None:
        self._log.info("audit %s", event.to_json(), extra={"audit": event.to_dict()})


class InMemoryAuditSink:
    """Keeps events in a bounded deque for tests, the UI feed and demos."""

    def __init__(self, max_events: int = 10_000) -> None:
        self._events: list[AuditEvent] = []
        self._max = max_events

    def write(self, event: AuditEvent) -> None:
        self._events.append(event)
        if len(self._events) > self._max:
            del self._events[: len(self._events) - self._max]

    @property
    def events(self) -> list[AuditEvent]:
        return list(self._events)

    def for_tenant(self, tenant_id: str) -> list[AuditEvent]:
        return [e for e in self._events if e.tenant_id == tenant_id]

    def for_resource(self, resource_type: str, resource_id: str) -> list[AuditEvent]:
        return [
            e for e in self._events
            if e.resource_type == resource_type and e.resource_id == resource_id
        ]

    def verify_chain(self) -> bool:
        """Recompute the hash chain; ``False`` means the log was tampered with."""
        previous = ""
        for event in self._events:
            if event.previous_hash != previous:
                return False
            if event.compute_hash() != event.event_hash:
                return False
            previous = event.event_hash
        return True


class CompositeAuditSink:
    """Fan-out to several sinks; a failing sink never blocks the operation."""

    def __init__(self, *sinks: AuditSink) -> None:
        self._sinks = list(sinks)

    def write(self, event: AuditEvent) -> None:
        for sink in self._sinks:
            try:
                sink.write(event)
            except Exception:  # noqa: BLE001, PERF203
                logger.exception("audit sink %s failed", type(sink).__name__)


AuditHook = Callable[[AuditEvent], None]


class AuditTrail:
    """Records audit events, maintaining the per-tenant hash chain."""

    def __init__(
        self,
        sink: AuditSink | None = None,
        *,
        service_name: str = "pas-plugins",
        enrichers: Iterable[Callable[[AuditEvent], None]] | None = None,
    ) -> None:
        self.sink = sink or StructuredLogSink()
        self.service_name = service_name
        self._sequence = 0
        self._last_hash = ""
        self._enrichers = list(enrichers or ())

    def record(
        self,
        action: AuditAction,
        resource_type: str,
        resource_id: str,
        *,
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        ctx: TenantContext | None = None,
        actor: str | None = None,
        acord_tx_type: str | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Create, hash-chain and emit an audit event."""
        context = ctx or maybe_current_context()
        self._sequence += 1
        event = AuditEvent(
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id),
            outcome=outcome,
            tenant_id=context.tenant_id if context else current_tenant_id(),
            actor=actor or (context.principal.subject if context else "system"),
            actor_type=context.principal.auth_method if context else "system",
            correlation_id=context.correlation_id if context else uuid.uuid4().hex,
            acord_tx_type=acord_tx_type,
            before=before,
            after=after,
            changes=diff_dicts(before, after) if (before and after) else None,
            metadata={"service": self.service_name, **(metadata or {})},
            previous_hash=self._last_hash,
            sequence=self._sequence,
        )
        for enricher in self._enrichers:
            try:
                enricher(event)
            except Exception:  # noqa: BLE001, PERF203
                logger.exception("audit enricher failed")
        object.__setattr__(event, "event_hash", event.compute_hash())
        self._last_hash = event.event_hash
        self.sink.write(event)
        return event

    def record_access_denied(
        self, resource_type: str, resource_id: str, reason: str, **metadata: Any
    ) -> AuditEvent:
        """Cross-tenant and scope failures are always audited, never just logged."""
        return self.record(
            AuditAction.ACCESS,
            resource_type,
            resource_id,
            outcome=AuditOutcome.DENIED,
            metadata={"reason": reason, **metadata},
        )


def diff_dicts(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any]:
    """Shallow field-level diff used for policy and configuration change records."""
    b, a = before or {}, after or {}
    changes: dict[str, Any] = {}
    for key in sorted(set(b) | set(a)):
        old, new = b.get(key), a.get(key)
        if old != new:
            changes[key] = {"from": old, "to": new}
    return changes


# ---------------------------------------------------------------------------
# Global default trail.  Plugins obtain it through ``app_factory`` so that a
# single sink can serve HTTP, MCP and workflow events alike.
# ---------------------------------------------------------------------------
_DEFAULT_TRAIL = AuditTrail()


def get_audit_trail() -> AuditTrail:
    """Return the process-wide audit trail."""
    return _DEFAULT_TRAIL


def configure_audit_trail(trail: AuditTrail) -> AuditTrail:
    """Install a custom trail (used by tests and by the PostgreSQL sink wiring)."""
    global _DEFAULT_TRAIL  # noqa: PLW0603
    _DEFAULT_TRAIL = trail
    return trail


class RedactingAuditSink:
    """Strips PII before persistence, keeping only masked digests.

    HIPAA-style carriers configure this so that the audit store never becomes a
    secondary copy of the customer record.
    """

    PII_KEYS = frozenset({
        "ssn", "social_security_number", "date_of_birth", "dob", "address", "email",
        "phone", "first_name", "last_name", "full_name", "tax_id", "passport",
    })

    def __init__(self, inner: AuditSink) -> None:
        self._inner = inner

    def write(self, event: AuditEvent) -> None:
        self._inner.write(_redact_event(event))


def _mask(value: str) -> str:
    if len(value) <= 4:
        return "****"
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


def _redact_event(event: AuditEvent) -> AuditEvent:
    data = event.to_dict()
    for section in ("before", "after", "metadata"):
        if isinstance(data.get(section), dict):
            data[section] = _redact_mapping(data[section])
    return AuditEvent(**data)


def _redact_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        if key.lower() in RedactingAuditSink.PII_KEYS and isinstance(value, str):
            out[key] = _mask(value)
        elif isinstance(value, dict):
            out[key] = _redact_mapping(value)
        else:
            out[key] = value
    return out
