"""ACORD Next-Generation Data Standards (NGDS) support.

NGDS gives the suite a single canonical wire vocabulary so that an AI agent, a
partner integration and the legacy PAS can all talk about the same policy
transaction in the same terms.  This package provides:

* :mod:`pas_core.acord.transaction` - the transaction envelope and the
  Life & Annuity transaction-type code registry (TX-1xx / TX-5xx families)
* :mod:`pas_core.acord.models`      - the canonical NGDS entity models
* :mod:`pas_core.acord.schema`      - JSON Schema generation and validation
* :mod:`pas_core.acord.mapping`     - internal domain models -> NGDS payloads

Provenance and governance
-------------------------
The transaction-code registry in ``registry.yaml`` is data, not code, so that a
standards analyst can extend or correct it without a redeploy - exactly the
treatment ACORD itself applies when standards project advisory groups publish
updates.  Always re-validate against the current published standard before
certifying; the accompanying ``sourceRef`` field records the reference used.
"""

from __future__ import annotations

from pas_core.acord.mapping import to_acord_envelope, to_acord_payload
from pas_core.acord.models import (
    Address,
    Coverage,
    LifeEvent,
    Person,
    Policy,
    PolicyStatus,
)
from pas_core.acord.transaction import (
    TRANSACTION_CODES,
    BusinessArea,
    EnvelopeDirection,
    TransactionContext,
    TransactionEnvelope,
    TransactionTypeCode,
    resolve_code,
)

__all__ = [
    "Address",
    "BusinessArea",
    "Coverage",
    "EnvelopeDirection",
    "LifeEvent",
    "TRANSACTION_CODES",
    "Person",
    "Policy",
    "PolicyStatus",
    "TransactionContext",
    "TransactionEnvelope",
    "TransactionTypeCode",
    "resolve_code",
    "to_acord_envelope",
    "to_acord_payload",
]
