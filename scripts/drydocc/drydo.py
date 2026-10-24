#!/usr/bin/env python3
"""drydo: the drydocc operations from a shell.

The same operations the MCP server exposes (``docc.OPS``), one subcommand
each, with flags generated from the operation's signature:

    python3 scripts/drydocc/drydo.py render-quadlets --node vps-fra-01 --slug web --relabel

It is the fallback when no MCP server is configured, and the ``replay`` line
an operation returns is a ``drydo.py`` command, so a journal entry can be
re-run by the admin without an agent. Needs python3 with PyYAML; not ``mcp``.
Prints the result (compact JSON, or the use-case table) and exits 1 with the
message when the operation fails.
"""

from __future__ import annotations

import argparse
import inspect
import sys
import typing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drydocc import doc as doc_mod, docc  # noqa: E402
from drydocc.mcp_server import to_text  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="drydo.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="op", required=True, metavar="<operation>")
    for name, fn in docc.OPS.items():
        doc = inspect.getdoc(fn) or ""
        cmd = sub.add_parser(name.replace("_", "-"), help=doc.splitlines()[0], description=doc)
        hints = typing.get_type_hints(fn)
        for p in inspect.signature(fn).parameters.values():
            flag = "--" + p.name.replace("_", "-")
            if hints[p.name] is bool:
                cmd.add_argument(flag, dest=p.name, action="store_true")
            elif p.default is inspect.Parameter.empty:
                cmd.add_argument(flag, dest=p.name, type=hints[p.name], required=True)
            else:
                cmd.add_argument(flag, dest=p.name, type=hints[p.name], default=p.default)
    return parser


def main(argv: list[str] | None = None) -> int:
    if doc_mod.yaml is None:
        print("drydo: PyYAML is required (pip install pyyaml)", file=sys.stderr)
        return 2
    args = vars(build_parser().parse_args(argv))
    fn = docc.OPS[args.pop("op").replace("-", "_")]
    try:
        print(to_text(fn(**args)))
    except docc.ERRORS as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
