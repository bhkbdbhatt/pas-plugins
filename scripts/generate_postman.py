"""Generate a Postman collection (v2.1) for every plugin from its OpenAPI document.

Run with::

    python scripts/generate_postman.py

Generating from the spec rather than hand-writing requests means a collection cannot
fall behind the API, and the example bodies come from the schemas rather than from
documentation that was correct once.

Every request carries the tenant header the platform requires, and write operations
are filed under a "Mutating" folder with a warning in the description: a collection
that quietly issues policies is a bad collection.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

COLLECTIONS = ROOT / "contracts" / "postman"

BASE_URL = "{{baseUrl}}"
TENANT = "{{tenantId}}"

PLUGIN_ORDER = [
    ("plugin1", "1 - PAS Gateway", 8001),
    ("plugin2", "2 - IFRS 17 Valuation", 8002),
    ("plugin3", "3 - AUW Workbench", 8003),
    ("plugin4", "4 - Low-Code Product Config", 8004),
    ("plugin5", "5 - Embedded Distribution", 8005),
    ("plugin6", "6 - Data Mesh", 8006),
    ("plugin7", "7 - Policy Ledger", 8007),
]

WRITE_METHODS = {"post", "put", "patch", "delete"}

# Path parameters are filled with obviously-fake values. A request that 404s on
# execution still demonstrates the shape, and a value that accidentally resolves
# against a real system would be worse.
PATH_EXAMPLES = {
    "policyId": "pol-0000000000",
    "quoteId": "QT00000000000000",
    "decisionId": "DEC000000000000",
    "product_id": "PROD-DEMO",
    "submission_id": "SUB0000",
    "leadId": "LEAD-000000",
    "partnerId": "PARTNER-GOLD-001",
    "claim_id": "CL-0001",
    "claimId": "CL-0001",
    "entry_id": "ANN-0001",
    "request_id": "BC-0001",
    "requestId": "BC-0001",
    "event_id": "EV-0001",
    "eventId": "EV-0001",
    "version": "1",
    "from_version": "1",
    "to_version": "2",
    "fromVersion": "1",
    "toVersion": "2",
    "cover_id": "cv-term-20",
    "coverId": "cv-term-20",
    "did": "did:key:zExample",
    "intent_id": "PI000000000000",
    "execution_id": "EXEC-0001",
    "workflow_id": "wf-onboarding",
    "rule_id": "APP-001",
    "feature_name": "premium",
    "dataset_name": "policies",
    "entity_id": "ent-0001",
    "table_id": "tbl-term-base",
}


def example_for(schema: dict[str, Any] | None, depth: int = 0) -> Any:
    """Build a small example body from a JSON Schema fragment.

    Deliberately shallow: a request body that mirrors every nested schema is
    unreadable, and the point of a collection is to show the shape of the call.
    """
    if not schema or depth > 3:
        return {}
    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]

    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        return {
            name: example_for(prop, depth + 1)
            for name, prop in list(schema.get("properties", {}).items())[:8]
        }
    if kind == "array":
        return [example_for(schema.get("items", {}), depth + 1)]
    if kind == "integer":
        return schema.get("minimum", 1)
    if kind == "number":
        return schema.get("minimum", 1.0)
    if kind == "boolean":
        return False
    if kind == "string":
        fmt = schema.get("format")
        if fmt == "date-time":
            return "2026-01-01T00:00:00Z"
        if fmt == "date":
            return "2026-01-01"
        return f"string({schema.get('maxLength', 0) or 'any'})"[:40] or "string"
    return {}


def resolve_ref(spec: dict[str, Any], ref: str) -> dict[str, Any]:
    """Resolve a local `$ref` one hop, which is all FastAPI emits."""
    if not ref.startswith("#/"):
        return {}
    node: Any = spec
    for part in ref.removeprefix("#/").split("/"):
        if not isinstance(node, dict) or part not in node:
            return {}
        node = node[part]
    return node if isinstance(node, dict) else {}


def request_schema(spec: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any]:
    """Find the JSON request body schema for an operation."""
    body = operation.get("requestBody") or {}
    content = body.get("content") or {}
    for media_type in ("application/json", "application/json; charset=utf-8"):
        if media_type in content:
            schema = content[media_type].get("schema") or {}
            if "$ref" in schema:
                return resolve_ref(spec, schema["$ref"])
            return schema
    return {}


def build_request(
    spec: dict[str, Any], method: str, path: str, operation: dict[str, Any], folder: str
) -> dict[str, Any]:
    """Build one Postman request from an OpenAPI operation."""
    segments: list[str] = []
    variables: list[dict[str, str]] = []
    for segment in path.strip("/").split("/"):
        if segment.startswith("{") and segment.endswith("}"):
            name = segment[1:-1]
            segments.append(":" + name)
            variables.append(
                {"key": name, "value": PATH_EXAMPLES.get(name, f"sample-{name}"), "type": "string"}
            )
        else:
            segments.append(segment)

    url = "/".join(segments)
    headers = [
        {"key": "Accept", "value": "application/json"},
        {"key": "X-Tenant-Id", "value": TENANT},
    ]
    body = request_schema(spec, operation)
    raw = json.dumps(example_for(body), indent=2)
    if method in WRITE_METHODS:
        headers.append({"key": "Content-Type", "value": "application/json"})

    is_write = method in WRITE_METHODS
    description = operation.get("summary") or operation.get("description") or ""
    if is_write:
        description = f"[MUTATING - review before sending]\n\n{description}"

    query = [
        {
            "key": parameter["name"],
            "value": str(PATH_EXAMPLES.get(parameter["name"], "1")),
            "description": parameter.get("description", ""),
            "disabled": False,
        }
        for parameter in operation.get("parameters", [])
        if parameter.get("in") == "query"
    ]

    return {
        "name": operation.get("summary") or operation.get("operationId") or f"{method} {path}",
        "request": {
            "method": method.upper(),
            "header": headers,
            "url": {
                "raw": "{{baseUrl}}" + "/" + url,
                "host": ["{{baseUrl}}"],
                "path": url.split("/"),
                "query": query,
                "variable": variables,
            },
            **({"body": {"mode": "raw", "raw": raw, "options": {"raw": {"language": "json"}}}} if is_write else {}),
            "description": description,
        },
        "response": [],
    }


def build_collection(spec: dict[str, Any], folder: str, port: int) -> dict[str, Any]:
    """Build a Postman collection for one plugin's spec."""
    read_items: list[dict[str, Any]] = []
    write_items: list[dict[str, Any]] = []

    for path, item in spec.get("paths", {}).items():
        for method, operation in item.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            request = build_request(spec, method, path, operation, folder)
            (write_items if method in WRITE_METHODS else read_items).append(request)

    return {
        "name": folder,
        "description": (
            f"Generated from {spec.get('info', {}).get('title', folder)} by "
            "scripts/generate_postman.py. Do not edit by hand; regenerate instead.\n\n"
            f"Default port: {port}. Set `baseUrl` to http://localhost:{port}."
        ),
        "variable": [
            {"key": "baseUrl", "value": f"http://localhost:{port}"},
            {"key": "tenantId", "value": "demo-carrier"},
        ],
        "item": [
            {"name": "Read operations", "item": read_items},
            {"name": "Mutating operations", "item": write_items},
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Postman collections from OpenAPI docs.")
    parser.add_argument("--contracts", default=str(ROOT / "contracts" / "openapi"))
    args = parser.parse_args()

    contracts = Path(args.contracts)
    COLLECTIONS.mkdir(parents=True, exist_ok=True)

    collections: list[dict[str, Any]] = []
    total = 0
    for target, folder, port in PLUGIN_ORDER:
        spec_path = contracts / f"{target}.openapi.json"
        if not spec_path.exists():
            print(f"missing {spec_path}; run scripts/export_openapi.py first", file=sys.stderr)
            return 1
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        collection = build_collection(spec, folder, port)
        destination = COLLECTIONS / f"{target}.postman_collection.json"
        destination.write_text(
            json.dumps(collection, indent=2, sort_keys=False) + "\n", encoding="utf-8"
        )
        count = sum(len(folder_item["item"]) for folder_item in collection["item"])
        total += count
        collections.append(collection)
        print(f"  {target:<9} {count:>3} requests -> {destination.name}")

    workspace = {
        "info": {"name": "PAS Plugin Suite", "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
        "item": collections,
        "variable": [
            {"key": "baseUrl", "value": "http://localhost:8001"},
            {"key": "tenantId", "value": "demo-carrier"},
        ],
    }
    (COLLECTIONS / "pas-plugin-suite.postman_collection.json").write_text(
        json.dumps(workspace, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\n  {len(collections)} collections, {total} requests -> {COLLECTIONS}")

    if total < 50:
        print(f"\nexpected at least 50 requests, generated {total}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())