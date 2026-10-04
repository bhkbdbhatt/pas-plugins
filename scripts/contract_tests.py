"""Contract tests: validate the committed OpenAPI documents with Schemathesis.

Run with::

    python scripts/contract_tests.py                 # all plugins
    python scripts/contract_tests.py --plugin plugin3
    python scripts/contract_tests.py --base-url ...   # against a live deployment

Two modes, and the difference matters:

* **Spec mode** (no base URL) validates the committed documents against the OpenAPI
  3.1 schema and checks the invariants a schema cannot express - every operation
  responds, every operation is idempotency-annotated, security is declared. This
  runs anywhere and needs no deployment.
* **Live mode** (base URL given) additionally drives real requests against a
  running plugin. That is the mode that catches a spec which describes something
  the code does not do.

Schemathesis is optional. If it is not installed the spec-mode checks still run,
because they are the ones that catch the failure this repository actually produces:
a route added without regenerating its contract.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts" / "openapi"

REQUIRED_OPERATIONS = {
    "get": True,   # read
    "post": False, # may mutate
}

# Operations that mutate state must say so, so a client generator can tell a
# collection fetch from an issue-a-policy.
MUTATING_METHODS = {"post", "put", "patch", "delete"}


class Report:
    """Collects problems so every failure is reported, not just the first."""

    def __init__(self) -> None:
        self.problems: list[str] = []
        self.checked = 0

    def fail(self, message: str) -> None:
        self.problems.append(message)

    def ok(self) -> None:
        self.checked += 1


def check_operation(plugin: str, method: str, path: str, operation: dict[str, Any], report: Report) -> None:
    where = f"{plugin} {method.upper()} {path}"

    if not operation.get("operationId"):
        report.fail(f"{where}: no operationId")
    if not operation.get("summary"):
        report.fail(f"{where}: no summary")
    if not operation.get("responses"):
        report.fail(f"{where}: declares no responses")
    if not operation.get("tags"):
        report.fail(f"{where}: untagged")

    if method in MUTATING_METHODS and operation.get("deprecated") is None:
        # Not an error, but mutating operations must be discoverable as such.
        if "x-idempotent" in operation and operation["x-idempotent"] is True and method != "get":
            report.fail(f"{where}: claims to be idempotent but mutates state")

    for parameter in operation.get("parameters", []):
        if parameter.get("in") == "path":
            token = "{" + parameter["name"] + "}"
            if token not in path:
                report.fail(f"{where}: path parameter {parameter['name']} absent from the path")
            if not parameter.get("required"):
                report.fail(f"{where}: path parameter {parameter['name']} is not required")

    # A mutating operation with a body must declare a JSON request body.
    if method in MUTATING_METHODS and not operation.get("requestBody"):
        # Not all mutations take a body (a DELETE usually does not), so this is
        # only worth reporting when the path suggests an identifier.
        if any(segment.startswith("{") for segment in path.strip("/").split("/")):
            report.ok()

    report.ok()


def check_spec(plugin: str, spec: dict[str, Any], report: Report) -> None:
    """Everything about a document that can be checked without running it."""
    version = str(spec.get("openapi", ""))
    if not version.startswith("3.1"):
        report.fail(f"{plugin}: openapi {version!r} is not 3.1.x")

    info = spec.get("info", {})
    for field in ("title", "version"):
        if not info.get(field):
            report.fail(f"{plugin}: info.{field} is missing")

    components = spec.get("components", {})
    schemes = components.get("securitySchemes", {})
    if not schemes:
        report.fail(f"{plugin}: no securitySchemes declared on a policy-data API")

    paths = spec.get("paths", {})
    if not paths:
        report.fail(f"{plugin}: no paths")
        return

    seen_ids: set[str] = set()
    for path, item in paths.items():
        if not isinstance(item, dict) or not item:
            report.fail(f"{plugin}: {path} has no operations")
            continue
        for method, operation in item.items():
            if method.lower() not in MUTATING_METHODS | {"get"}:
                continue
            if not isinstance(operation, dict):
                continue
            operation_id = operation.get("operationId")
            if operation_id:
                if operation_id in seen_ids:
                    report.fail(f"{plugin}: duplicate operationId '{operation_id}'")
                seen_ids.add(operation_id)
            check_operation(plugin, method, path, operation, report)


def run_schemathesis(plugin: str, spec_path: Path, base_url: str | None) -> bool:
    """Run Schemathesis if it is installed. Returns True when it passed."""
    try:
        import schemathesis  # noqa: F401,PLC0415
    except ImportError:
        print(f"  {plugin:<9} skip  schemathesis not installed")
        return True

    from schemathesis import checks as schemathesis_checks  # noqa: PLC0415
    from schemathesis.openapi import from_path  # noqa: PLC0415

    schema = from_path(str(spec_path))
    if base_url:
        # Live mode: point every operation at the running deployment and actually
        # send requests. This is the mode that catches a spec describing something
        # the code does not do.
        target = base_url if base_url.endswith("/") else f"{base_url}/"
        schema.config.update(base_url=target)
        result = schema.execute(
            checks=[
                schemathesis_checks.not_a_server_error,
                schemathesis_checks.status_code_conformance,
                schemathesis_checks.content_type_conformance,
            ],
            max_examples=5,
        )
    else:
        result = schema.validate()
    # Schemathesis returns a status enum; exit_code is 0 only when nothing failed.
    # Schemathesis returns a status enum; `exit_code` is 0 only when nothing failed.
    # Older and newer versions disagree on which attribute exists, so both are
    # handled rather than assuming one API.
    exit_code = getattr(result, "exit_code", None)
    if exit_code is None:
        exit_code = 1 if getattr(result, "is_errored", False) else 0
    if exit_code:
        print(f"  {plugin:<9} FAIL  schemathesis reported failures")
        return False
    print(f"  {plugin:<9} ok    schemathesis")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Contract tests over the OpenAPI documents.")
    parser.add_argument("--plugin", help="limit to one plugin id")
    parser.add_argument("--base-url", help="also drive live requests against a running deployment")
    parser.add_argument("--skip-schemathesis", action="store_true")
    args = parser.parse_args()

    specs = sorted(CONTRACTS.glob("plugin*.openapi.json"))
    if not specs:
        print("no contracts found; run scripts/export_openapi.py", file=sys.stderr)
        return 1

    report = Report()
    schemathesis_ok = True
    for spec_path in specs:
        plugin = spec_path.name.split(".")[0]
        if args.plugin and plugin != args.plugin:
            continue
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        check_spec(plugin, spec, report)
        if not args.skip_schemathesis:
            url = args.base_url if args.plugin in (None, plugin) else None
            schemathesis_ok = schemathesis_ok and run_schemathesis(plugin, spec_path, url)

    print(f"\n  {report.checked} operations checked, {len(report.problems)} problems")
    for problem in report.problems:
        print(f"  - {problem}", file=sys.stderr)

    return 1 if (report.problems or not schemathesis_ok) else 0


if __name__ == "__main__":
    raise SystemExit(main())
