"""JSON Schema generation and validation for ACORD NGDS payloads.

The suite publishes OpenAPI 3.1 documents whose schemas are the single source of
truth; this module renders the same pydantic models as draft 2020-12 JSON Schema
for (a) the ``/acord/schemas`` discovery endpoint and (b) Schemathesis contract
tests that generate valid ACORD payloads.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel

from pas_core.acord import models, transaction
from pas_core.errors import ValidationError

MODEL_REGISTRY: dict[str, type[BaseModel]] = {
    "Address": models.Address,
    "Contact": models.Contact,
    "Person": models.Person,
    "RoleAssignment": models.RoleAssignment,
    "Coverage": models.Coverage,
    "LifeEvent": models.LifeEvent,
    "Policy": models.Policy,
    "BeneficiaryAllocation": models.BeneficiaryAllocation,
    "TransactionEnvelope": transaction.TransactionEnvelope,
}


def schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """Draft 2020-12 JSON Schema for a pydantic model."""
    return model.model_json_schema(by_alias=True, ref_template="#/$defs/{model}")


@lru_cache(maxsize=len(MODEL_REGISTRY))
def _cached_schema(name: str) -> str:
    model = MODEL_REGISTRY[name]
    return json.dumps(schema_for(model), sort_keys=True)


def registry_of_schemas() -> dict[str, Any]:
    """Every canonical NGDS schema, keyed by entity name."""
    return {name: schema_for(model) for name, model in MODEL_REGISTRY.items()}


def bundled_schema() -> dict[str, Any]:
    """A single self-contained document with ``$defs`` for all entities."""
    merged: dict[str, Any] = {"$defs": {}}
    for name, model in MODEL_REGISTRY.items():
        schema = schema_for(model)
        defs = schema.pop("$defs", {})
        merged["$defs"].update(defs)
        merged["$defs"][name] = schema
    merged["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    merged["$id"] = "https://docs.pas-plugins.io/schemas/acord-ngds-la.json"
    merged["title"] = "ACORD NGDS - Life and Annuity canonical schemas"
    return merged


def validator_for(entity: str) -> Draft202012Validator:
    """A JSON Schema validator for a registered entity."""
    if entity not in MODEL_REGISTRY:
        raise ValidationError(f"Unknown ACORD entity '{entity}'", entity=entity)
    return Draft202012Validator(json.loads(_cached_schema(entity)))


def validate_payload(entity: str, payload: Any) -> Any:
    """Validate a payload against an entity schema, raising a catalogue error."""
    validator = validator_for(entity)
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        path = "/".join(str(p) for p in first.absolute_path)
        raise ValidationError(
            f"Payload does not satisfy ACORD '{entity}' schema: {first.message}",
            entity=entity,
            path=path,
            violations=[
                {
                    "path": "/".join(str(p) for p in err.absolute_path),
                    "message": err.message,
                    "validator": err.validator,
                }
                for err in errors[:20]
            ],
        )
    return payload


def validate_model(entity: str, payload: Any) -> BaseModel:
    """Validate then coerce into the pydantic model."""
    validate_payload(entity, payload)
    return MODEL_REGISTRY[entity].model_validate(payload)


def transaction_schema() -> dict[str, Any]:
    """JSON Schema for a complete NGDS transaction envelope."""
    return schema_for(transaction.TransactionEnvelope)
