"""Smoke test: import every plugin app and confirm it serves a spec.

Run with::

    python scripts/smoke_plugins.py

This is what the container build job runs. It exists because the deployment
artefacts assume one image serves all seven plugins, and that assumption is only
worth anything if something checks it. Importing each app and reading its OpenAPI
document catches a broken import, a bad route declaration or a startup failure at
CI time rather than at deploy time.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

MODULES: dict[str, str] = {
    "plugin1": "pas_plugins.plugin1_gateway.main",
    "plugin2": "pas_plugins.plugin2_ifrs17.main",
    "plugin3": "pas_plugins.plugin3_auw.main",
    "plugin4": "pas_plugins.plugin4_productconfig.main",
    "plugin5": "pas_plugins.plugin5_embedded.main",
    "plugin6": "pas_plugins.plugin6_datamesh.main",
    "plugin7": "pas_plugins.plugin7_blockchain.main",
}


def smoke(plugin: str, module_path: str, verbose: bool) -> tuple[bool, str]:
    """Import one plugin app and report whether it serves an OpenAPI document."""
    try:
        module = importlib.import_module(module_path)
    except Exception as exc:  # noqa: BLE001 - the point is to report any import failure
        return False, f"import failed: {exc}\n{traceback.format_exc(limit=3)}"

    app = getattr(module, "app", None)
    if app is None:
        return False, f"{module_path} exposes no `app` attribute"

    try:
        spec = app.openapi()
    except Exception as exc:  # noqa: BLE001
        return False, f"openapi() failed: {exc}"

    paths = spec.get("paths", {})
    if not paths:
        return False, f"{module_path} serves no paths"

    detail = f"{len(paths):>3} paths"
    if verbose:
        detail += ", " + ", ".join(sorted(paths)[:3])
    return True, detail


def main() -> int:
    parser = argparse.ArgumentParser(description="Import every plugin app and check it serves a spec.")
    parser.add_argument("-v", "--verbose", action="store_true", help="show sample paths")
    args = parser.parse_args()

    failures = 0
    for plugin, module_path in MODULES.items():
        ok, detail = smoke(plugin, module_path, args.verbose)
        if ok:
            print(f"  {plugin:<9} ok    {detail}")
        else:
            failures += 1
            print(f"  {plugin:<9} FAIL  {detail}")

    total = len(MODULES)
    print(f"\n  {total - failures}/{total} plugins importable")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())