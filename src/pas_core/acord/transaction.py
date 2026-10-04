"""Transaction type codes and the NGDS message envelope.

The Life & Annuity NGDS families used by this suite:

======  ==========================================================
Family  Meaning
======  ==========================================================
TX-1xx  Individual life insurance application and policy servicing
TX-2xx  Individual annuity contract application and servicing
TX-3xx  Worksite / group life
TX-5xx  Annuity contract application, status and transaction
TX-6xx  Annuity payout and settlement
TX-8xx  Regulatory and statutory reporting (NAIC SSAP 102)
TX-9xx  Financial and experience reporting (IFRS 17)
======  ==========================================================

Codes are loaded from ``registry.yaml`` so the catalogue can be updated by
configuration rather than a release.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from pas_core.errors import ValidationError

REGISTRY_PATH = Path(__file__).parent / "registry.yaml"

STANDARD_VERSION = "6.0.0"
ACORD_ALM_VERSION = "NGDS-LA-6.0"


class BusinessArea(StrEnum):
    """ACORD business area codes."""

    LIFE = "01"
    ANNUITY = "02"
    GROUP_LIFE = "03"
    ANNUITY_ANNUITY = "04"
    WORKERS_COMP = "05"
    FINANCIAL_SERVICES = "09"
    REGULATORY_REPORTING = "14"
    CLAIMS = "17"


class EnvelopeDirection(StrEnum):
    REQUEST = "request"
    RESPONSE = "response"


class TransactionTypeCode(StrEnum):
    """Registry-backed transaction codes (Life & Annuity subset)."""

    # --- TX-1xx: life application & underwriting ----------------------------
    TX101_LIFE_APPLICATION_SUBMISSION = "TX-101"
    TX102_LIFE_APPLICATION_ACCEPTANCE = "TX-102"
    TX103_LIFE_APPLICATION_DECISION = "TX-103"
    TX104_LIFE_APPLICATION_STATUS = "TX-104"
    TX105_LIFE_POLICY_ISSUE = "TX-105"
    TX106_LIFE_POLICY_TRANSACTION = "TX-106"
    TX107_LIFE_POLICY_STATUS_INQUIRY = "TX-107"
    TX108_LIFE_POLICY_STATUS_RESPONSE = "TX-108"
    TX109_LIFE_POLICY_TRANSACTION_RESPONSE = "TX-109"
    TX114_LIFE_POLICY_SERVICE_REQUEST = "TX-114"
    TX115_LIFE_POLICY_SERVICE_RESPONSE = "TX-115"
    TX116_LIFE_POLICY_LOAN_TRANSACTION = "TX-116"
    TX117_LIFE_POLICY_LOAN_RESPONSE = "TX-117"
    TX118_LIFE_POLICY_CHANGE_REQUEST = "TX-118"
    TX119_LIFE_POLICY_CHANGE_RESPONSE = "TX-119"
    TX120_LIFE_POLICY_LAPSE_REINSTATEMENT = "TX-120"
    TX121_LIFE_POLICY_LAPSE_RESPONSE = "TX-121"

    # --- TX-2xx: individual annuity application ----------------------------
    TX201_ANNUITY_APPLICATION_SUBMISSION = "TX-201"
    TX202_ANNUITY_APPLICATION_ACCEPTANCE = "TX-202"
    TX203_ANNUITY_APPLICATION_DECISION = "TX-203"
    TX205_ANNUITY_CONTRACT_ISSUE = "TX-205"
    TX206_ANNUITY_CONTRACT_TRANSACTION = "TX-206"
    TX207_ANNUITY_CONTRACT_STATUS_INQUIRY = "TX-207"
    TX208_ANNUITY_CONTRACT_STATUS_RESPONSE = "TX-208"
    TX214_ANNUITY_CONTRACT_SERVICE_REQUEST = "TX-214"
    TX215_ANNUITY_CONTRACT_SERVICE_RESPONSE = "TX-215"

    # --- TX-5xx: annuity contract transactions ------------------------------
    TX503_ANNUITY_CONTRACT_TRANSACTION = "TX-503"
    TX504_ANNUITY_CONTRACT_TRANSACTION_RESPONSE = "TX-504"
    TX505_ANNUITY_CONTRACT_STATUS_INQUIRY = "TX-505"
    TX506_ANNUITY_CONTRACT_STATUS_RESPONSE = "TX-506"
    TX507_ANNUITY_TRANSACTION_REQUEST = "TX-507"
    TX508_ANNUITY_TRANSACTION_RESPONSE = "TX-508"

    # --- TX-8xx: regulatory / statutory --------------------------------------
    TX801_SSAP_POLICY_DATA = "TX-801"
    TX802_SSAP_VALUATION_DATA = "TX-802"

    # --- TX-9xx: financial / IFRS 17 -----------------------------------------
    TX901_IFRS17_GROUPING_REQUEST = "TX-901"
    TX902_IFRS17_MEASUREMENT_RESPONSE = "TX-902"
    TX903_IFRS17_CSM_ROLLFORWARD = "TX-903"
    TX904_IFRS17_DISCLOSURE_DATA = "TX-904"

    # --- TX-0xx: shared / generic --------------------------------------------
    TX002_PARTY_TRANSACTION = "TX-002"
    TX003_PRODUCT_TRANSACTION = "TX-003"


@dataclass(frozen=True, slots=True)
class TransactionCodeDefinition:
    """One registry entry: the code plus the metadata a client needs."""

    code: TransactionTypeCode
    name: str
    business_area: BusinessArea
    family: str
    direction: EnvelopeDirection
    description: str
    legacy_synonyms: tuple[str, ...] = ()

    @property
    def source_ref(self) -> str:
        return "https://www.acord.org/standards-architecture/acord-data-standards/next-generation-digital-standards"


@dataclass(frozen=True, slots=True)
class TransactionContext:
    """Routing context attached to an outbound NGDS message."""

    sender_id: str
    receiver_id: str
    correlation_id: str
    business_area: BusinessArea = BusinessArea.LIFE
    security_level: int = 1
    test_mode: bool = False

    def header_fields(self) -> dict[str, Any]:
        return {
            "SenderID": self.sender_id,
            "ReceiverID": self.receiver_id,
            "MessageID": self.correlation_id,
            "BusinessAreaCode": str(self.business_area.value),
            "SecurityLevel": self.security_level,
        }


class TransactionEnvelope(BaseModel):
    """The NGDS ``ACORD`` request/response envelope.

    Layout follows the standard's ``requestMessage`` / ``responseMessage``
    structure: a header, an optional status block and one or more content
    objects, plus vendor extensions carried under ``Extension``.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow", ser_json_timedelta="float")

    sender_id: str = Field(alias="SenderID", min_length=1, max_length=60)
    receiver_id: str = Field(alias="ReceiverID", min_length=1, max_length=60)
    message_id: str = Field(alias="MessageID", min_length=1, max_length=60)
    transaction_set: str = Field(default="TX", alias="TransactionSet")
    transaction_type_code: str = Field(alias="TransactionTypeCode", min_length=5, max_length=8)
    message_date_time: str = Field(
        alias="MessageDateTime",
        description="ISO-8601 timestamp. NGDS uses UTC with an explicit offset.",
    )
    business_area_code: str = Field(default=BusinessArea.LIFE.value, alias="BusinessAreaCode")
    security_level: int = Field(default=1, alias="SecurityLevel", ge=0, le=9)
    standard_version: str = Field(default=STANDARD_VERSION, alias="StandardVersion")
    acord_version: str = Field(default=ACORD_ALM_VERSION, alias="ACORDVersion")
    test_mode: bool = Field(default=False, alias="TestMode")
    status: dict[str, Any] | None = Field(default=None, alias="Status")
    content: dict[str, Any] = Field(default_factory=dict, alias="Content")
    extension: dict[str, Any] = Field(default_factory=dict, alias="Extension")

    @field_validator("transaction_type_code")
    @classmethod
    def _known_code(cls, value: str) -> str:
        code = resolve_code(value)
        if code is None:
            msg = (
                f"Unknown ACORD transaction type code '{value}'. "
                "Register it in pas_core/acord/registry.yaml before use."
            )
            raise ValueError(msg)
        return code.code.value

    @property
    def is_response(self) -> bool:
        definition = resolve_code(self.transaction_type_code)
        return bool(definition and definition.direction is EnvelopeDirection.RESPONSE)

    def to_json(self) -> str:
        return self.model_dump_json(by_alias=True, exclude_none=True)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)


def resolve_code(code: str) -> TransactionCodeDefinition | None:
    """Resolve a code or a documented legacy synonym to its registry entry."""
    return TRANSACTION_CODES.get(code.upper()) or TRANSACTION_CODES.get(code.strip().upper())


def definition_for(code: str) -> TransactionCodeDefinition:
    """Like :func:`resolve_code` but raises instead of returning ``None``."""
    definition = resolve_code(code)
    if definition is None:
        raise ValidationError(f"Unknown ACORD transaction type code '{code}'", code=code)
    return definition


@lru_cache(maxsize=1)
def _load_registry() -> tuple[dict[str, TransactionCodeDefinition], dict[str, str]]:
    raw = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8")) or {}
    definitions: dict[str, TransactionCodeDefinition] = {}
    synonyms: dict[str, str] = {}
    for code, spec in (raw.get("transactions") or {}).items():
        enum_member = TransactionTypeCode(code)
        entry = TransactionCodeDefinition(
            code=enum_member,
            name=spec["name"],
            business_area=BusinessArea(str(spec.get("businessArea", "01"))),
            family=code.split("-")[0],
            direction=EnvelopeDirection(spec.get("direction", "request")),
            description=spec.get("description", ""),
            legacy_synonyms=tuple(spec.get("legacySynonyms", [])),
        )
        definitions[code] = entry
        for synonym in entry.legacy_synonyms:
            synonyms[synonym.upper()] = code
    return definitions, synonyms


class _TransactionCodeMap(dict):
    """Mapping that also resolves legacy synonyms before falling back to lookup."""

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        definitions, synonyms = _load_registry()
        if key in definitions:
            return definitions[key]
        canonical = synonyms.get(key.upper())
        if canonical:
            return definitions[canonical]
        return default

    def __contains__(self, key: object) -> bool:
        return self.get(str(key)) is not None


TRANSACTION_CODES: dict[str, TransactionCodeDefinition] = _TransactionCodeMap()  # type: ignore[assignment]


def registry_as_json() -> dict[str, Any]:
    """Publish the registry as JSON for the management UI and agent discovery."""
    definitions, _ = _load_registry()
    return {
        "standardVersion": STANDARD_VERSION,
        "acordVersion": ACORD_ALM_VERSION,
        "sourceRef": (
            "https://www.acord.org/standards-architecture/acord-data-standards/"
            "next-generation-digital-standards"
        ),
        "transactions": [
            {
                "code": d.code.value,
                "name": d.name,
                "businessArea": d.business_area.value,
                "family": d.family,
                "direction": str(d.direction),
                "description": d.description,
            }
            for d in sorted(definitions.values(), key=lambda x: x.code.value)
        ],
    }
