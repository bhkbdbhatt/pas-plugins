"""Vendor registry and adapter resolution.

Adapters are resolved per request from the tenant context, which means one gateway
deployment can front a book of Majesco, OIPA and NGIN tenants simultaneously
without cross-talk - the isolation guarantee is structural, not conventional.
"""

from __future__ import annotations

import functools
from typing import Any

from pas_core.pas.base import PasAdapter, PasTransport, VendorCapabilities
from pas_core.pas.vendors import VENDOR_ADAPTERS, build_vendor_adapter

SUPPORTED_VENDORS: tuple[str, ...] = (
    "majesco-lifeplus",
    "oracle-oipa",
    "eis",
    "mccamish-ngin",
    "accenture-alip",
    "sapiens",
    "generic",
    "simulated",
)

_EXTRA_ADAPTERS: dict[str, type[PasAdapter]] = {}


def register_adapter(vendor: str, adapter_cls: type[PasAdapter]) -> type[PasAdapter]:
    """Register a carrier-specific adapter without forking the suite."""
    _EXTRA_ADAPTERS[vendor] = adapter_cls
    return adapter_cls


def available_vendors() -> list[str]:
    """Every vendor the gateway can front, with a human label."""
    labels = {
        "majesco-lifeplus": "Majesco LifePlus (Life)",
        "oracle-oipa": "Oracle OIPA",
        "eis": "EIS / EIS Vision",
        "mccamish-ngin": "McCamish NGIN",
        "accenture-alip": "Accenture ALIP",
        "sapiens": "Sapiens Life & Annuity",
        "generic": "Generic REST/SOAP PAS",
        "simulated": "Simulated PAS (demo)",
    }
    return [
        {"vendor": vendor, "displayName": labels.get(vendor, vendor)}
        for vendor in (*SUPPORTED_VENDORS, *_EXTRA_ADAPTERS)
    ]


def vendor_capabilities(vendor: str) -> VendorCapabilities:
    """Static capability matrix for a vendor, used for routing and the UI."""
    adapter_cls = _EXTRA_ADAPTERS.get(vendor) or VENDOR_ADAPTERS.get(vendor)
    if adapter_cls is None:
        return VendorCapabilities(
            vendor=vendor,
            display_name=vendor,
            transport=PasTransport.REST_JSON,
            api_style="unknown",
            batch_capable=False,
            streaming_capable=False,
            idempotency_support=False,
            notes="Vendor is not in the certification matrix; supply configuration to use it.",
        )
    return adapter_cls.capabilities


@functools.lru_cache(maxsize=32)
def get_adapter(vendor: str) -> PasAdapter:
    """Return a cached adapter instance for ``vendor``."""
    if vendor in _EXTRA_ADAPTERS:
        return _EXTRA_ADAPTERS[vendor]()
    if vendor == "simulated":
        from pas_core.pas.base import SimulatedPasAdapter  # noqa: PLC0415

        return SimulatedPasAdapter(vendor="simulated")
    return build_vendor_adapter(vendor)


def reset_adapter_cache() -> None:
    """Drop cached adapters (used by tests and after a config reload)."""
    get_adapter.cache_clear()


def vendor_supports(vendor: str, operation_id: str) -> bool:
    return get_adapter(vendor).supports(operation_id)


def describe_vendor(vendor: str) -> dict[str, Any]:
    """Capability record rendered as JSON for the management UI."""
    capabilities = vendor_capabilities(vendor)
    return {
        "vendor": capabilities.vendor,
        "displayName": capabilities.display_name,
        "transport": str(capabilities.transport),
        "apiStyle": capabilities.api_style,
        "batchCapable": capabilities.batch_capable,
        "streamingCapable": capabilities.streaming_capable,
        "idempotencySupport": capabilities.idempotency_support,
        "soapNamespace": capabilities.soap_namespace,
        "notes": capabilities.notes,
        "configured": vendor in VENDOR_ADAPTERS or vendor in _EXTRA_ADAPTERS,
    }
