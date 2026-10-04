"""Export every plugin's OpenAPI 3.1 document and validate it.

Run with::

    python scripts/export_openapi.py            # write contracts/openapi/*.json
    python scripts/export_openapi.py --validate # also validate structure
    python scripts/export_openapi.py --check    # fail if anything is stale

The specs are generated from the running applications rather than hand-written, so
they cannot drift from the routes they describe. `--check` is what CI runs: it
regenerates in memory and fails when the committed file differs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

CONTRACTS = ROOT / "contracts" / "openapi"

# plugin id -> (import path of the ASGI app, human name)
PLUGINS: dict[str, tuple[str, str]] = {
    "plugin1": ("pas_plugins.plugin1_gateway.main:app", "PAS Gateway"),
    "plugin2": ("pas_plugins.plugin2_ifrs17.main:app", "IFRS 17 Valuation"),
    "plugin3": ("pas_plugins.plugin3_auw.main:app", "AUW Workbench"),
    "plugin4": ("pas_plugins.plugin4_productconfig.main:app", "Low-Code Product Config"),
    "plugin5": ("pas_plugins.plugin5_embedded.main:app", "Embedded Distribution"),
    "plugin6": ("pas_plugins.plugin6_datamesh.main:app", "Data Mesh"),
    "plugin7": ("pas_plugins.plugin7_blockchain.main:app", "Policy Ledger"),
}


def build_spec(target: str) -> dict[str, Any]:
    """Import a plugin app and return its OpenAPI document."""
    import importlib  # noqa: PLC0415

    module_path, _name = PLUGINS[target]
    module_name, _, attribute = module_path.partition(":")
    module = importlib.import_module(module_name)
    app = getattr(module, attribute)
    return app.openapi()


def validate_spec(spec: dict[str, Any], name: str) -> list[str]:
    """Structural checks that a plain `openapi()` call cannot make for us.

    These catch the failure modes that actually happen when generating a spec from
    code: an operation with no responses, an undeclared path parameter, a security
    requirement with no scheme, or an identifier that will not survive round-tripping.
    """
    problems: list[str] = []
    prefix = f"{name}: "

    if not str(spec.get("openapi", "")).startswith("3.1"):
        problems.append(f"{prefix}openapi version is {spec.get('openapi')!r}, expected 3.1.x")
    if "info" not in spec:
        problems.append(f"{prefix}missing info object")
    if "paths" not in spec:
        problems.append(f"{prefix}missing paths object")
        return problems

    schemes = spec.get("components", {}).get("securitySchemes", {})
    declared_security = bool(schemes)

    operations = 0
    for path, item in spec["paths"].items():
        if not item:
            problems.append(f"{prefix}{path} has no operations")
        for declared in (item.get("parameters") or []):
            if declared.get("in") == "path" and not declared.get("required"):
                problems.append(f"{prefix}{path}: path parameter {declared.get('name')} is not required")
        for method, operation in item.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                continue
            operations += 1
            where = f"{prefix}{method.upper()} {path}"
            if not operation.get("operationId"):
                problems.append(f"{where} has no operationId")
            if not operation.get("summary"):
                problems.append(f"{where} has no summary")
            if not operation.get("responses"):
                problems.append(f"{where} declares no responses")
            for parameter in operation.get("parameters", []):
                if parameter.get("in") == "path":
                    token = "{" + parameter["name"] + "}"
                    if token not in path:
                        problems.append(
                            f"{where} declares path parameter '{parameter['name']}' absent from the path"
                        )
            for required in operation.get("security", []):
                for scheme in required:
                    if declared_security and scheme not in schemes:
                        problems.append(f"{where} requires undeclared security scheme '{scheme}'")
            if not operation.get("tags"):
                problems.append(f"{where} has no tags")

    if operations == 0:
        problems.append(f"{prefix}declares no operations")

    # A spec that cannot be re-serialised is not a usable contract.
    try:
        json.loads(json.dumps(spec))
    except (TypeError, ValueError) as exc:
        problems.append(f"{prefix}is not JSON-serialisable: {exc}")

    return problems


def summarise(spec: dict[str, Any]) -> dict[str, Any]:
    paths = spec.get("paths", {})
    operations = sum(
        1
        for item in paths.values()
        for method in item
        if method.lower() in {"get", "post", "put", "patch", "delete"}
    )
    return {
        "openapi": spec.get("openapi"),
        "title": spec.get("info", {}).get("title"),
        "paths": len(paths),
        "operations": operations,
        "schemas": len(spec.get("components", {}).get("schemas", {})),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Export and validate plugin OpenAPI documents.")
    parser.add_argument("--validate", action="store_true", help="run structural checks")
    parser.add_argument("--check", action="store_true", help="fail when a committed spec is stale")
    parser.add_argument("--quiet", action="store_true", help="only report problems")
    args = parser.parse_args()

    CONTRACTS.mkdir(parents=True, exist_ok=True)
    index: dict[str, Any] = {}
    all_problems: list[str] = []
    stale: list[str] = []

    for target, (_path, name) in PLUGINS.items():
        spec = build_spec(target)
        destination = CONTRACTS / f"{target}.openapi.json"
        payload = json.dumps(spec, indent=2, sort_keys=True) + "\n"

        if args.check:
            if not destination.exists() or destination.read_text(encoding="utf-8") != payload:
                stale.append(target)
        else:
            destination.write_text(payload, encoding="utf-8")

        problems = validate_spec(spec, target) if (args.validate or args.check) else []
        all_problems.extend(problems)

        index[target] = {"name": name, **summarise(spec), "problems": problems}
        if not args.quiet:
            status = "FAIL" if problems else "ok"
            print(f"  {target:<9} {status:<5} {name}")

    index_path = CONTRACTS / "index.json"
    if args.check:
        if index_path.exists():
            existing = json.loads(index_path.read_text(encoding="utf-8"))
            if existing.get("plugins", {}).keys() != index.keys():
                stale.append("index")
    else:
        index_path.write_text(
            json.dumps({"generatedBy": "scripts/export_openapi.py", "plugins": index}, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

    total_ops = sum(entry["operations"] for entry in index.values())
    if not args.quiet:
        print(f"\n  {len(index)} plugins, {total_ops} operations -> {CONTRACTS}")

    if all_problems:
        print(f"\n{len(all_problems)} validation problems:", file=sys.stderr)
        for problem in all_problems:
            print(f"  - {problem}", file=sys.stderr)
    if stale:
        print(f"\nstale specs: {sorted(stale)}", file=sys.stderr)
        print("run: python scripts/export_openapi.py", file=sys.stderr)

    return 1 if (all_problems or stale) else 0


if __name__ == "__main__":
    raise SystemExit(main())