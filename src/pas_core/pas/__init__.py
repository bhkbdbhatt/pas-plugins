"""PAS vendor adapters and the atomic operation abstraction.

A *Policy Administration System* exposes monolithic, proprietary interfaces.  The
gateway's job (plugin 1) is to decompose those into **atomic operations** - one
business intent each, self-describing, with a complete request/response schema.
An adapter's only responsibility is to translate one atomic operation to and from
its vendor's dialect.

Supported out of the box
------------------------
=====================  ==========================================================
``majesco-lifeplus``   Majesco LifePlus (Life) - REST + XML envelopes
``oracle-oipa``        Oracle OIPA (Policy/Claim/Integration) - SOAP + REST
``eis``                EIS / EIS Vision (Employee / Individual life) - REST + JSON
``mccamish-ngin``      McCamish NGIN - REST with a proprietary ``ngin`` envelope
``accenture-alip``     Accenture ALIP - SOAP-heavy, ACSII-style documents
``sapiens``            Sapiens Life & Annuity - REST with a different envelope
``generic``            Configurable HTTP adapter for any other REST/SOAP PAS
=====================  ==========================================================
"""

from __future__ import annotations

from pas_core.pas.base import (
    AtomicOperation,
    OperationParameter,
    OperationRegistry,
    PasAdapter,
    PasTransport,
    SimulatedPasAdapter,
)
from pas_core.pas.registry import (
    SUPPORTED_VENDORS,
    VendorCapabilities,
    available_vendors,
    get_adapter,
    register_adapter,
    vendor_capabilities,
)

__all__ = [
    "AtomicOperation",
    "OperationParameter",
    "OperationRegistry",
    "PasAdapter",
    "PasTransport",
    "SUPPORTED_VENDORS",
    "SimulatedPasAdapter",
    "VendorCapabilities",
    "available_vendors",
    "get_adapter",
    "register_adapter",
    "vendor_capabilities",
]
