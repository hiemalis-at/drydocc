#!/usr/bin/env python3
"""The drydocc MCP server: the operations in ``docc.py`` as MCP tools.

One tool per entry of ``docc.OPS``, generated from the operation's own
signature and docstring, so the tools and ``drydo.py`` never drift apart. A
failing operation is reported as a tool error carrying its message; a result
is compact JSON (the use-case table is plain text). No tool prints, logs or
returns a secret value.

Register it in the agent's MCP config, from a fork checkout:

```json
{
  "mcpServers": {
    "drydocc": {
      "command": "python3",
      "args": ["scripts/drydocc/mcp_server.py"],
      "cwd": "<fork root>"
    }
  }
}
```

It needs python3 with the ``mcp`` and PyYAML packages. Run
``python3 scripts/drydocc/mcp_server.py --self-test`` for a smoke check that
needs neither an MCP client nor a node.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
import tempfile
import typing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drydocc import audit_probe, doc as doc_mod, docc  # noqa: E402

SERVER_INSTRUCTIONS = (
    "drydocc fork tools. Pass the node node-context bound; every tool stays "
    "inside nodes/<node>/. No tool returns a secret value."
)


def _load_mcp():
    """The server class and its tool-error class. mcp 1.x calls the server
    FastMCP; 2.x renamed it MCPServer with the same decorator API."""
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.server.fastmcp.exceptions import ToolError

        return FastMCP, ToolError
    except ImportError:
        pass
    try:
        from mcp.server.mcpserver import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError

        return MCPServer, ToolError
    except ImportError:
        return None, None


def to_text(result) -> str:
    """A result as the tools and the CLI print it."""
    if isinstance(result, str):
        return result
    return json.dumps(result, separators=(",", ":"), default=str)


def _as_tool(fn, tool_error):
    """Wrap an operation as a tool: same parameters, a text result, and the
    operation's errors raised as ``tool_error`` so the client sees an error
    with its message (any other exception reaches it without one)."""
    hints = typing.get_type_hints(fn)
    sig = inspect.signature(fn)
    params = [p.replace(annotation=hints[p.name]) for p in sig.parameters.values()]

    def tool(**kwargs):
        try:
            return to_text(fn(**kwargs))
        except docc.ERRORS as exc:
            raise tool_error(f"{type(exc).__name__}: {exc}") from exc

    tool.__name__ = fn.__name__
    tool.__doc__ = fn.__doc__
    tool.__signature__ = sig.replace(parameters=params, return_annotation=str)
    tool.__annotations__ = {**{p.name: p.annotation for p in params}, "return": str}
    return tool


def build_server():
    server_class, tool_error = _load_mcp()
    if server_class is None:
        raise RuntimeError("the `mcp` package is required: pip install mcp")
    mcp = server_class("drydocc", instructions=SERVER_INSTRUCTIONS)
    for fn in docc.OPS.values():
        # Every result is text; a structured-output schema per tool would only
        # add fixed tokens to every request.
        try:
            register = mcp.tool(structured_output=False)
        except TypeError:  # an mcp 1.x release older than structured output
            register = mcp.tool()
        register(_as_tool(fn, tool_error))
    return mcp


def self_test() -> int:
    """Smoke-check the operations against a throwaway fork."""
    failures = []

    def check(name, fn):
        try:
            fn()
            print(f"ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failures.append(name)
            print(f"FAIL {name}: {exc}")

    with tempfile.TemporaryDirectory() as d:
        uc = Path(d) / "nodes" / "n1" / "use-cases"
        uc.mkdir(parents=True)
        (uc.parent / "node.yaml").write_text("name: n1\ntailscale_hostname: n1\n", encoding="utf-8")
        (uc / "web.md").write_text(
            "---\nname: web\nstatus: active\ncontainers: [app]\nnetworks: []\nvolumes: [data]\n"
            "spec:\n  volumes: {data: {}}\n  containers:\n"
            "    app: {image: 'x:1', network: host, mounts: [{volume: data, target: /data}]}\n---\n",
            encoding="utf-8",
        )
        old = os.environ.get("DRYDOCC_ROOT")
        os.environ["DRYDOCC_ROOT"] = d
        try:
            def _render():
                out = docc.render_quadlets("n1", "web", relabel=True)
                assert "Volume=/var/lib/drydocc/volumes/web/data:/data:Z" in out["files"]["web-app.container"]
                assert out["replay"].endswith("render-quadlets --node n1 --slug web --relabel")

            def _scope():
                for bad in (("../n1", "web"), ("n1", "../web"), ("n2", "web")):
                    try:
                        docc.render_quadlets(*bad)
                    except docc.OpError:
                        continue
                    raise AssertionError(f"{bad} was not refused")

            def _list():
                assert "| web | active |" in docc.list_use_cases("n1")

            def _parse():
                inv = audit_probe.parse_probe(
                    "container=web-app|running|img|id|healthy\n"
                    "quadlet=/home/doccap/.config/containers/systemd/web-app.container\n"
                    "---8<---\n[Unit]\n--->8---\n"
                )
                assert inv["quadlets"]["/home/doccap/.config/containers/systemd/web-app.container"] == "[Unit]\n"

            check("render_quadlets", _render)
            check("node and slug scoping", _scope)
            check("list_use_cases", _list)
            check("parse_probe", _parse)
        finally:
            if old is None:
                os.environ.pop("DRYDOCC_ROOT", None)
            else:
                os.environ["DRYDOCC_ROOT"] = old
    if failures:
        print(f"{len(failures)} self-test(s) failed")
        return 1
    print("all self-tests passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self-test", action="store_true", help="run the smoke check and exit")
    args = parser.parse_args()
    if doc_mod.yaml is None:
        print("drydocc: PyYAML is required (pip install pyyaml)", file=sys.stderr)
        return 2
    if args.self_test:
        return self_test()
    mcp = build_server()
    mcp.run(transport="stdio")  # type: ignore[union-attr]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
