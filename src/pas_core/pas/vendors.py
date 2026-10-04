"""Vendor adapter implementations for each supported PAS.

Each adapter documents the vendor's actual quirks: envelope shape, date formats,
status vocabularies and pagination.  The differences are real and are the reason
this layer exists - normalising them here means the rest of the suite (and the AI
agents talking to it) only ever sees the canonical vocabulary from
:mod:`pas_core.acord.models`.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from typing import Any

from pas_core.pas.base import (
    AtomicOperation,
    PasAdapter,
    PasTransport,
    VendorCapabilities,
)
from pas_core.tenancy import TenantContext

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")
_NUMERIC_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_SLASH_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_COMPACT_DATE = re.compile(r"^(\d{4})(\d{2})$")


def to_camel(name: str) -> str:
    """``face_amount`` -> ``faceAmount``.

    Lower camel case, which is what JSON APIs use. The leading character is left
    lower-case on purpose: a vendor's field is ``faceAmount``, not ``FaceAmount``.
    """
    head, *rest = name.replace("-", "_").split("_")
    return head[:1].lower() + head[1:] + "".join(p[:1].upper() + p[1:] for p in rest)


def to_snake(name: str) -> str:
    """``FaceAmount`` -> ``face_amount``."""
    return _CAMEL.sub("_", name).lower()


def parse_vendor_date(value: Any) -> date | None:  # noqa: ANN401
    """Accept the date formats PAS vendors actually emit.

    Handles ISO-8601, ``YYYYMMDD``, ``MM/DD/YYYY`` and ``YYYYMM``.  Returning
    ``None`` rather than raising keeps a single odd record from failing a whole
    batch extraction.
    """
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if iso := _NUMERIC_DATE.match(text):
        return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    if slash := _SLASH_DATE.match(text):
        return date(int(slash.group(3)), int(slash.group(1)), int(slash.group(2)))
    if compact := _COMPACT_DATE.match(text):
        return date(int(compact.group(1)), int(compact.group(2)), 1)
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def strip_namespace(xml: str) -> str:
    """Remove XML namespaces so vendor payloads parse with plain tag names."""
    return re.sub(r"\sxmlns(:\w+)?=\"[^\"]*\"", "", xml)


def xml_to_dict(element: ET.Element) -> dict[str, Any]:
    """Collapse an XML element into a nested dict, repeating keys into lists."""
    result: dict[str, Any] = {}
    for child in element:
        value = xml_to_dict(child) if len(child) else (child.text or "").strip()
        if child.tag in result:
            existing = result[child.tag]
            if isinstance(existing, list):
                existing.append(value)
            else:
                result[child.tag] = [existing, value]
        else:
            result[child.tag] = value
    return result


class EnvelopePasAdapter(PasAdapter):
    """Base class for adapters that wrap calls in a vendor-specific envelope.

    Subclasses declare how to wrap a request and unwrap a response; the shared
    machinery adds the correlation id, the tenant tag and audit metadata, which
    every vendor's operations team needs for support tickets.
    """

    vendor_operation_prefix = ""

    def build_envelope(self, operation: AtomicOperation, payload: dict[str, Any]) -> dict[str, Any]:
        """Wrap the canonical payload in the vendor envelope."""
        return payload

    def unwrap_envelope(self, operation: AtomicOperation, response: Any) -> Any:  # noqa: ANN401
        """Extract the canonical result from the vendor response."""
        return response

    def translate_request(self, operation: AtomicOperation, payload: dict[str, Any]) -> dict[str, Any]:
        return self.build_envelope(operation, payload)

    def translate_response(self, operation: AtomicOperation, response: Any) -> dict[str, Any]:  # noqa: ANN401
        unwrapped = self.unwrap_envelope(operation, response)
        return super().translate_response(operation, unwrapped)

    def vendor_operation_name(self, operation: AtomicOperation) -> str:
        return operation.vendor_operation or f"{self.vendor_operation_prefix}{operation.operation_id}"


# ---------------------------------------------------------------------------
# Majesco LifePlus
# ---------------------------------------------------------------------------
class MajescoLifePlusAdapter(EnvelopePasAdapter):
    """Majesco LifePlus - REST/JSON with an XML document envelope for batch work.

    Notes on the real product
    -------------------------
    * Batch and document services exchange XML even though the transaction
      services are JSON; ``translate_request`` keeps both shapes addressable.
    * Dates are ``MM/DD/YYYY`` in the legacy document services.
    * Status values are ``Inforce``/``Lapsed``/``Terminated`` (capitalised, no
      hyphenation) and must be normalised to the canonical lowercase form.
    """

    vendor = "majesco-lifeplus"
    capabilities = VendorCapabilities(
        vendor="majesco-lifeplus",
        display_name="Majesco LifePlus (Life)",
        transport=PasTransport.REST_JSON,
        api_style="lowercase-camel with XML document envelopes",
        batch_capable=True,
        streaming_capable=False,
        idempotency_support=False,
        notes=(
            "LifePlus distinguishes transaction services (JSON) from batch/document "
            "services (XML). Idempotency is emulated by this gateway, not the PAS."
        ),
    )

    STATUS_MAP = {
        "inforce": "active",
        "in-force": "active",
        "in force": "active",
        "active": "active",
        "lapsed": "lapsed",
        "grace": "lapsed",
        "graceperiod": "lapsed",
        "terminated": "terminated",
        "cancelled": "terminated",
        "canceled": "terminated",
        "expired": "expired",
        "paid-up": "paid_up",
        "paidup": "paid_up",
        "surrendered": "surrendered",
        "quoted": "quoted",
        "pending": "submitted",
    }

    def normalise_status(self, value: Any) -> str:  # noqa: ANN401
        return self.STATUS_MAP.get(str(value).strip().lower(), str(value).lower())

    def build_envelope(self, operation: AtomicOperation, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "documentType": "TRANSACTION",
            "vendorOperation": self.vendor_operation_name(operation),
            "payload": payload,
        }

    def unwrap_envelope(self, operation: AtomicOperation, response: Any) -> Any:  # noqa: ANN401
        if isinstance(response, dict) and "payload" in response:
            return response["payload"]
        return response

    def translate_response(self, operation: AtomicOperation, response: Any) -> dict[str, Any]:
        result = super().translate_response(operation, response)
        status = result.get("status")
        if status is not None:
            result["status"] = self.normalise_status(status)
        for date_field in ("issueDate", "effectiveDate", "expirationDate", "asOfDate"):
            if date_field in result:
                parsed = parse_vendor_date(result[date_field])
                result[date_field] = parsed.isoformat() if parsed else None
        return result


# ---------------------------------------------------------------------------
# Oracle OIPA
# ---------------------------------------------------------------------------
class OracleOipaAdapter(EnvelopePasAdapter):
    """Oracle OIPA (Insurance Policy Administration) - SOAP/JSON hybrid.

    Notes on the real product
    -------------------------
    * Service operations are SOAP-first; OIPA's REST layer wraps them and expects
      a ``RequestContext`` with effective-dated as-of timestamps.
    * Policy status values are single-letter codes (``A`` active, ``L`` lapsed,
      ``T`` terminated) exposed as ``POLICYSTATUS``.
    * Every call is effective-dated: ``AsOfDate`` is mandatory or the service
      raises ``INVALID_ASOF``.
    """

    vendor = "oracle-oipa"
    capabilities = VendorCapabilities(
        vendor="oracle-oipa",
        display_name="Oracle OIPA",
        transport=PasTransport.SOAP,
        api_style="SOAP 1.1 with effective-dated RequestContext",
        batch_capable=True,
        streaming_capable=True,
        idempotency_support=True,
        soap_namespace="http://www.oracle.com/insurance/policy/v1",
        notes="Effective-dated service operations; POLSTATUS uses single-letter codes.",
    )

    STATUS_MAP = {
        "A": "active",
        "L": "lapsed",
        "T": "terminated",
        "C": "terminated",
        "E": "expired",
        "P": "paid_up",
        "S": "surrendered",
        "Q": "quoted",
        "W": "underwriting",
        "R": "referred",
        "N": "submitted",
        "X": "declined",
    }

    def build_envelope(self, operation: AtomicOperation, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "RequestContext": {
                "AsOfDate": payload.get("asOfDate") or date.today().isoformat(),
                "TransactionType": operation.acord_tx_type.value if operation.acord_tx_type else "N",
                "SourceSystem": "pas-plugins-gateway",
            },
            "Operation": self.vendor_operation_name(operation),
            "Payload": payload,
        }

    def unwrap_envelope(self, operation: AtomicOperation, response: Any) -> Any:  # noqa: ANN401
        if isinstance(response, dict):
            if "Return" in response:
                return response["Return"]
            if "Payload" in response:
                return response["Payload"]
        return response

    def normalise_status(self, value: Any) -> str:  # noqa: ANN401
        return self.STATUS_MAP.get(str(value).strip().upper(), str(value).lower())

    def translate_response(self, operation: AtomicOperation, response: Any) -> dict[str, Any]:
        result = super().translate_response(operation, response)
        for key in ("status", "policyStatus", "POLICYSTATUS"):
            if result.get(key) is not None:
                result[key] = self.normalise_status(result[key])
        return result


# ---------------------------------------------------------------------------
# EIS (Employee / Individual life systems)
# ---------------------------------------------------------------------------
class EisAdapter(EnvelopePasAdapter):
    """EIS / EIS Vision - REST/JSON with snake_case keys and a flat result envelope.

    Notes on the real product
    -------------------------
    * Responses are wrapped as ``{"status": "...", "result": {...}}`` with a
      lowercase ``status`` that means *call* status (``success``/``error``), which
      collides conceptually with *policy* status - both are separated here.
    * Amounts are strings with a currency suffix, e.g. ``"1,250.00 USD"``.
    * No native idempotency: the gateway supplies it.
    """

    vendor = "eis"
    capabilities = VendorCapabilities(
        vendor="eis",
        display_name="EIS / EIS Vision",
        transport=PasTransport.REST_JSON,
        api_style="snake_case with a flat {status, result} envelope",
        batch_capable=True,
        streaming_capable=False,
        idempotency_support=False,
        notes="Amounts arrive as formatted strings; call status and policy status are distinct.",
    )

    def build_envelope(self, operation: AtomicOperation, payload: dict[str, Any]) -> dict[str, Any]:
        return {to_snake(k): v for k, v in payload.items()}

    def unwrap_envelope(self, operation: AtomicOperation, response: Any) -> Any:  # noqa: ANN401
        if isinstance(response, dict) and "result" in response:
            return response["result"]
        return response

    @staticmethod
    def parse_amount(value: Any) -> float | None:  # noqa: ANN401
        """``"1,250.00 USD"`` -> ``1250.0``."""
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        digits = re.sub(r"[^0-9.\-]", "", str(value))
        try:
            return float(digits) if digits not in ("", "-", ".") else None
        except ValueError:
            return None

    def translate_response(self, operation: AtomicOperation, response: Any) -> dict[str, Any]:
        result = super().translate_response(operation, response)
        normalised = {to_snake(k): v for k, v in result.items()}
        for key in ("annualised_premium", "face_amount", "amount", "cash_value"):
            if key in normalised:
                amount = self.parse_amount(normalised[key])
                if amount is not None:
                    normalised[to_camel(key)] = amount
        return normalised


# ---------------------------------------------------------------------------
# McCamish NGIN
# ---------------------------------------------------------------------------
class McCamishNginAdapter(EnvelopePasAdapter):
    """McCamish NGIN - REST/JSON behind an ``ngin`` envelope with a policy aggregate.

    Notes on the real product
    -------------------------
    * Every request is ``{"ngin": {"operation": ..., "version": "3", "data": {...}}}``.
    * The policy aggregate nests coverages under ``coverages`` with zero-based
      ordinals and uses ISO-8601 timestamps.
    * NGIN returns ``errors`` (plural) arrays even on success, so a naive client
      misreads them as failures.
    """

    vendor = "mccamish-ngin"
    capabilities = VendorCapabilities(
        vendor="mccamish-ngin",
        display_name="McCamish NGIN",
        transport=PasTransport.REST_JSON,
        api_style='{"ngin": {operation, version, data}} envelope',
        batch_capable=True,
        streaming_capable=False,
        idempotency_support=True,
        notes="Success responses still carry an empty `errors` array; check it explicitly.",
    )

    def build_envelope(self, operation: AtomicOperation, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "ngin": {
                "operation": self.vendor_operation_name(operation),
                "version": "3",
                "data": payload,
            }
        }

    def unwrap_envelope(self, operation: AtomicOperation, response: Any) -> Any:  # noqa: ANN401
        if isinstance(response, dict) and "ngin" in response:
            body = response["ngin"]
            errors = body.get("errors")
            if errors:
                from pas_core.errors import UpstreamPasError  # noqa: PLC0415

                first = errors[0]
                raise UpstreamPasError(
                    f"NGIN reported {len(errors)} error(s): {first.get('message', first)}",
                    vendor=self.vendor,
                    operation=operation.operation_id,
                )
            return body.get("data", body)
        return response


# ---------------------------------------------------------------------------
# Accenture ALIP
# ---------------------------------------------------------------------------
class AccentureAlipAdapter(EnvelopePasAdapter):
    """Accenture ALIP - SOAP-heavy with document-oriented ACSII exchanges.

    Notes on the real product
    -------------------------
    * Contract changes are exchanged as ACSII documents; the transaction API is
      SOAP with a heavy ``ext:`` extension namespace.
    * Effective-dated ``contractChangeDate`` and ``changeReason`` are mandatory.
    * CSB (contract service boundary) endpoints are versioned by date.
    """

    vendor = "accenture-alip"
    capabilities = VendorCapabilities(
        vendor="accenture-alip",
        display_name="Accenture ALIP (Life)",
        transport=PasTransport.SOAP,
        api_style="SOAP with ACSII document payloads and ext: extensions",
        batch_capable=True,
        streaming_capable=True,
        idempotency_support=False,
        soap_namespace="urn:alip:contract:v1",
        notes="CSB endpoints are effective-dated; changeReason is mandatory on contract changes.",
    )

    CHANGE_REASONS = {
        "lapse": "01",
        "reinstate": "02",
        "surrender": "03",
        "coverage_change": "04",
        "beneficiary_change": "05",
        "ownership_change": "06",
        "loan": "07",
        "billing_change": "08",
    }

    def build_envelope(self, operation: AtomicOperation, payload: dict[str, Any]) -> dict[str, Any]:
        change_type = str(payload.get("changeType", "")).lower()
        return {
            "ext:RequestContext": {
                "ext:SourceSystem": "pas-plugins-gateway",
                "ext:CorrelationId": payload.get("correlationId", ""),
                "ext:ChangeReasonCode": self.CHANGE_REASONS.get(change_type, "00"),
            },
            "ext:Operation": self.vendor_operation_name(operation),
            "ext:Data": payload,
        }

    def unwrap_envelope(self, operation: AtomicOperation, response: Any) -> Any:  # noqa: ANN401
        if isinstance(response, dict):
            for key in ("ext:Return", "Return", "ext:Data"):
                if key in response:
                    return response[key]
        return response


# ---------------------------------------------------------------------------
# Sapiens Life & Annuity
# ---------------------------------------------------------------------------
class SapiensAdapter(EnvelopePasAdapter):
    """Sapiens Life & Annuity (formerly Cognizant TriZetto) - REST with policy holds.

    Notes on the real product
    -------------------------
    * Strong read/write REST split; writes require a ``X-Policy-Version`` for
      optimistic concurrency and return ``409`` on mismatch.
    * Monetary values are decimal strings with an explicit ``currency`` field.
    * Annuity and life share one contract aggregate with ``lineOfBusiness``
      distinguishing them.
    """

    vendor = "sapiens"
    capabilities = VendorCapabilities(
        vendor="sapiens",
        display_name="Sapiens Life & Annuity",
        transport=PasTransport.REST_JSON,
        api_style="REST with X-Policy-Version optimistic concurrency",
        batch_capable=True,
        streaming_capable=True,
        idempotency_support=True,
        notes="Writes require the current policy version header; mismatch returns 409.",
    )

    STATUS_MAP = {
        "inforce": "active",
        "issued": "issued",
        "lapse": "lapsed",
        "lapsed": "lapsed",
        "free_look": "issued",
        "terminated": "terminated",
        "expired": "expired",
        "paidup": "paid_up",
        "surrender": "surrendered",
        "withdrawn": "surrendered",
    }

    def normalise_status(self, value: Any) -> str:  # noqa: ANN401
        return self.STATUS_MAP.get(str(value).strip().lower(), str(value).lower())

    def build_envelope(self, operation: AtomicOperation, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "policyVersion": payload.get("policyVersion"),
            "effectiveDate": payload.get("effectiveDate"),
            "attributes": payload,
        }

    def unwrap_envelope(self, operation: AtomicOperation, response: Any) -> Any:  # noqa: ANN401
        if isinstance(response, dict) and "attributes" in response:
            return response["attributes"]
        return response

    def translate_response(self, operation: AtomicOperation, response: Any) -> dict[str, Any]:
        result = super().translate_response(operation, response)
        if result.get("status") is not None:
            result["status"] = self.normalise_status(result["status"])
        return result


# ---------------------------------------------------------------------------
# Generic configurable adapter
# ---------------------------------------------------------------------------
class GenericRestAdapter(EnvelopePasAdapter):
    """Adapter for any other REST/SOAP PAS, driven entirely by configuration.

    This is the escape hatch that makes the suite sellable against a long tail of
    carriers: the operator supplies an endpoint template, a header set and a JSON
    path expression, and the gateway works immediately - no code change.
    """

    vendor = "generic"
    capabilities = VendorCapabilities(
        vendor="generic",
        display_name="Generic REST/SOAP PAS",
        transport=PasTransport.REST_JSON,
        api_style="operator-configured",
        batch_capable=False,
        streaming_capable=False,
        idempotency_support=False,
        notes="Endpoints, headers and response paths supplied as configuration.",
    )

    def __init__(self, endpoint_template: str = "/pas/v1/{operation}", *, headers: dict[str, str] | None = None) -> None:
        self.endpoint_template = endpoint_template
        self.headers = dict(headers or {})

    def endpoint_for(self, operation: AtomicOperation, path_params: dict[str, Any]) -> str:
        params = {**{p.name: p for p in operation.parameters if p.location == "path"}, **path_params}
        try:
            return self.endpoint_template.format(
                operation=operation.operation_id.replace(".", "-"),
                **{k: v for k, v in params.items()},
            )
        except KeyError as exc:
            raise ValueError(f"endpoint template references unknown path parameter {exc}") from exc


VENDOR_ADAPTERS: dict[str, type[EnvelopePasAdapter]] = {
    "majesco-lifeplus": MajescoLifePlusAdapter,
    "oracle-oipa": OracleOipaAdapter,
    "eis": EisAdapter,
    "mccamish-ngin": McCamishNginAdapter,
    "accenture-alip": AccentureAlipAdapter,
    "sapiens": SapiensAdapter,
    "generic": GenericRestAdapter,
}


def build_vendor_adapter(vendor: str, **kwargs: Any) -> PasAdapter:  # noqa: ANN401
    """Instantiate the adapter for ``vendor`` (falls back to ``generic``)."""
    adapter_cls = VENDOR_ADAPTERS.get(vendor, GenericRestAdapter)
    return adapter_cls(**kwargs)


async def probe_vendor(vendor: str, ctx: TenantContext) -> dict[str, Any]:
    """Health probe used by the gateway monitoring screen."""
    adapter = build_vendor_adapter(vendor)
    return await adapter.health()
