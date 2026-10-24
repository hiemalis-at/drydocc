"""The drydocc operations: one registry, two front ends.

Every operation here is reached through the MCP server (``mcp_server.py``)
and through the CLI (``drydo.py``); both are generated from ``OPS``, so a
tool and its command line never drift apart. The CLI is also the fallback
when no MCP server is configured, and the ``replay`` line an operation
returns is what a journal's ``## Commands`` records.

Scoping is enforced here, not left to the caller: an operation takes a node
name and a use-case slug, never a free path, and resolves both inside
``nodes/<node>/`` of the fork. The skills pass the node `node-context` bound.
Errors raise ``ERRORS`` subclasses; the front ends turn them into a tool error
or a non-zero exit with the message.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from . import audit_probe, doc as doc_mod, lingering, node_io, render, state

# One rule for node names and use-case slugs (bootstrap-node and
# create-use-case validate against the same expression).
NAME = re.compile(r"^[a-z][a-z0-9-]{0,30}[a-z0-9]$")
STEP = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
SURFACES = ("on_node", "on_client")


class OpError(Exception):
    """An argument is out of scope or malformed."""


ERRORS = (
    OpError,
    doc_mod.DocError,
    render.SpecError,
    state.StateError,
    lingering.LingerError,
    OSError,
    ValueError,
)


def root() -> Path:
    """The fork root: ``DRYDOCC_ROOT`` when set (tests), else this checkout."""
    return Path(os.environ.get("DRYDOCC_ROOT") or Path(__file__).resolve().parents[2])


def node_dir(node: str) -> Path:
    if not NAME.match(node or ""):
        raise OpError(f"node {node!r} is not a node name (kebab-case)")
    path = root() / "nodes" / node
    if not (path / "node.yaml").is_file():
        raise OpError(f"no node {node!r}: nodes/{node}/node.yaml is missing")
    return path


def _slug(slug: str) -> str:
    if not NAME.match(slug or ""):
        raise OpError(f"slug {slug!r} is not a use-case slug (kebab-case)")
    return slug


def doc_path(node: str, slug: str) -> Path:
    path = node_dir(node) / "use-cases" / f"{_slug(slug)}.md"
    if not path.is_file():
        raise OpError(f"no use-case {slug!r} on {node}")
    return path


def node_yaml(node: str) -> dict:
    path = node_dir(node) / "node.yaml"
    if doc_mod.yaml is None:
        raise OpError("PyYAML is required to read node.yaml")
    try:
        data = doc_mod.yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except doc_mod.yaml.YAMLError as exc:
        raise OpError(f"nodes/{node}/node.yaml is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise OpError(f"nodes/{node}/node.yaml is not a mapping")
    return data


def _connection(node: str, surface: str) -> lingering.Connection | None:
    """None on the node itself; the node.yaml connection block from the client."""
    if surface not in SURFACES:
        raise OpError(f"surface {surface!r} must be one of {', '.join(SURFACES)}")
    if surface == "on_node":
        return None
    return lingering.Connection.from_node_yaml(node_yaml(node))


def _step(step: str) -> str:
    if not STEP.match(step or ""):
        raise OpError(f"step label {step!r} must be short kebab-case")
    return step


def replay(op: str, **args) -> str:
    """The ``drydo.py`` command line that repeats an operation; defaults are
    left out so the line stays short."""
    words = ["python3", "scripts/drydocc/drydo.py", op.replace("_", "-")]
    for key, value in args.items():
        if value is False or value == "" or value is None:
            continue
        words.append(f"--{key.replace('_', '-')}")
        if value is not True:
            words.append(str(value))
    return " ".join(shlex.quote(w) for w in words)


# --- operations -------------------------------------------------------------


def render_quadlets(node: str, slug: str, relabel: bool = False) -> dict:
    """Render a use-case's spec to quadlet files.

    Args: relabel — the Z/z verdict decided per podman-quadlets § "Render from spec".
    """
    files = render.render_document(doc_path(node, slug), relabel)
    return {
        "files": files,
        "digest": render.digest(files),
        "replay": replay("render_quadlets", node=node, slug=slug, relabel=relabel),
    }


def diff_quadlets(node: str, slug: str, surface: str, relabel: bool = False) -> dict:
    """Render a use-case and classify it against the node's current quadlets
    (same/changed/added/dropped).

    Args: surface — on_node or on_client; relabel — as render_quadlets.
    """
    rendered = render.render_document(doc_path(node, slug), relabel)
    on_disk = audit_probe.read_quadlets(slug, _connection(node, surface))
    return {
        "files": render.diff_rendered(rendered, on_disk),
        "digest": render.digest(rendered),
        "replay": replay("diff_quadlets", node=node, slug=slug, surface=surface, relabel=relabel),
    }


def write_quadlets(node: str, slug: str, surface: str, digest: str, relabel: bool = False) -> dict:
    """Write the confirmed render's changed and added quadlets to the node.

    Args: digest — from the render_quadlets/diff_quadlets result the admin
    confirmed; refused if the render changed since. Dropped files are left
    for the caller to remove after stopping their units.
    """
    rendered = render.render_document(doc_path(node, slug), relabel)
    now = render.digest(rendered)
    if digest != now:
        raise OpError(
            f"the render changed since it was confirmed (expected {digest}, now {now}): "
            "render and confirm again"
        )
    unit = re.compile(rf"^{re.escape(slug)}-[a-z0-9][a-z0-9-]*\.(container|network)$")
    for name in rendered:
        if not unit.match(name):
            raise OpError(f"refusing to write {name!r}: not a <slug>-<name>.container/.network file")
    conn = _connection(node, surface)
    classes = render.diff_rendered(rendered, audit_probe.read_quadlets(slug, conn))
    by_class = lambda c: sorted(n for n, e in classes.items() if e["class"] == c)
    written = by_class("changed") + by_class("added")
    node_io.write_files({n: rendered[n] for n in written}, conn)
    return {
        "written": written,
        "same": by_class("same"),
        "dropped": by_class("dropped"),
        "digest": now,
        "replay": replay(
            "write_quadlets", node=node, slug=slug, surface=surface, digest=digest, relabel=relabel
        ),
    }


def validate_use_case(node: str, slug: str) -> dict:
    """Every static-invariant finding of a use-case document; empty = valid."""
    return {"findings": render.validate_document(doc_path(node, slug))}


def list_use_cases(node: str) -> str:
    """The node's use-case table, read from the repo alone."""
    return state.render_table(state.list_use_cases(node_dir(node)))


def resume_pointer_status() -> dict:
    """Classify .drydocc/last-command.yaml: none, paused, finished or interrupted."""
    pointer = state.read_pointer(root())
    if pointer is None:
        return {"status": "none"}
    if pointer.get("state") == "waiting_on_secret":
        return {"status": "paused", "pointer": pointer}
    entry = state.pointer_finished(root(), pointer)
    if entry is not None:
        return {"status": "finished", "pointer": pointer, "journal": entry}
    return {"status": "interrupted", "pointer": pointer}


def owes_restore(node: str, slug: str) -> dict:
    """The journal's "rebuilt, not yet restored" query for one use-case."""
    return {"owes_restore": state.owes_restore(node_dir(node) / "journal", _slug(slug))}


def audit_probe_run(
    node: str, slug: str, surface: str, relabel: bool = False, firewall_readback: str = ""
) -> dict:
    """Run the Tier 1 audit probe and compare the node's quadlets with the render.

    Args: surface — on_node or on_client; relabel — as render_quadlets;
    firewall_readback — the read-back from audit-recipes.md § "Observed
    dialects", empty when none is verified.
    """
    path = doc_path(node, slug)
    inputs = audit_probe.probe_inputs_from_doc(path, node_yaml(node), firewall_readback)
    result = audit_probe.run(inputs, _connection(node, surface))
    inventory = result["inventory"]
    on_disk = {Path(p).name: text for p, text in inventory.pop("quadlets").items()}
    try:
        inventory["quadlets"] = render.diff_rendered(render.render_document(path, relabel), on_disk)
    except render.SpecError as exc:
        # The static invariants report this; the rest of the probe still counts.
        inventory["quadlets"] = {"error": str(exc)}
    return {
        "rc": result["rc"],
        "stderr": "\n".join(result["stderr"].splitlines()[-20:]),
        "inventory": inventory,
        "replay": replay(
            "audit_probe_run", node=node, slug=slug, surface=surface,
            relabel=relabel, firewall_readback=firewall_readback,
        ),
    }


def _backend(node: str, backend: str) -> str | None:
    if backend and backend not in ("tmux", "nohup"):
        raise OpError(f"backend {backend!r} must be tmux or nohup")
    return backend or lingering.backend_from_facts(node_yaml(node))


def lingering_launch(node: str, slug: str, step: str, command: str, backend: str = "") -> dict:
    """Launch a long step detached on the node (tmux, or nohup); re-attaches
    when it is already running.

    Args: backend — tmux or nohup; empty uses system_facts.capabilities.tmux.
    """
    conn = lingering.Connection.from_node_yaml(node_yaml(node))
    return lingering.launch(conn, _slug(slug), _step(step), command, _backend(node, backend))


def lingering_poll(node: str, slug: str, step: str, backend: str = "") -> dict:
    """Poll a lingering step once: running (log tail), done (exit code) or
    killed (gone without its sentinel — never success)."""
    conn = lingering.Connection.from_node_yaml(node_yaml(node))
    return lingering.poll(conn, _slug(slug), _step(step), _backend(node, backend))


def lingering_cleanup(node: str, slug: str, step: str) -> dict:
    """Remove a lingering step's log; keep it after a partial outcome."""
    conn = lingering.Connection.from_node_yaml(node_yaml(node))
    lingering.cleanup(conn, _slug(slug), _step(step))
    return {"cleaned": True}


OPS = {
    fn.__name__: fn
    for fn in (
        render_quadlets,
        diff_quadlets,
        write_quadlets,
        validate_use_case,
        list_use_cases,
        resume_pointer_status,
        owes_restore,
        audit_probe_run,
        lingering_launch,
        lingering_poll,
        lingering_cleanup,
    )
}
