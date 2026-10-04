"""Developer smoke script: boot each plugin app and print its routes and schema.

Usage::

    .venv/Scripts/python -m scripts.inspect_app pas_plugins.plugin1_gateway.main:app
"""

from __future__ import annotations

import sys

from starlette.routing import Mount, Route


def walk(routes: object, prefix: str = "") -> list[str]:
    lines: list[str] = []
    for route in routes:  # type: ignore[union-attr]
        path = prefix + getattr(route, "path", "")
        if isinstance(route, Mount):
            lines.append(f"{'MOUNT':7} {path}")
            lines.extend(walk(getattr(route, "routes", []) or [], path))
        elif isinstance(route, Route):
            methods = sorted((route.methods or set()) - {"HEAD"})
            lines.append(f"{','.join(methods):7} {path}")
    return lines


def main(target: str) -> int:
    module_name, _, attr = target.partition(":")
    module = __import__(module_name, fromlist=[attr or "app"])
    app = getattr(module, attr or "app")
    for line in walk(app.routes):
        print(line)
    schema = app.openapi()
    info = schema["info"]
    print()
    print(f"openapi   {schema['openapi']}")
    print(f"paths     {len(schema['paths'])}")
    print(f"schemas   {len(schema['components']['schemas'])}")
    print(f"plugin    {info.get('x-pas-plugin')} {info.get('x-pas-plugin-version')}")
    if "x-pas-atomic-operations" in info:
        print(f"operations {info['x-pas-atomic-operations']}")
    if "x-mcp" in info:
        mcp = info["x-mcp"]
        print(f"mcp       {mcp['server']} tools={mcp['toolCount']} path={mcp['path']}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
