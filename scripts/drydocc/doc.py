"""Use-case document parsing and editing.

A use-case document is ``nodes/<node>/use-cases/<slug>.md``: YAML frontmatter
between the first pair of ``---`` lines, then markdown body sections. The
frontmatter schema is the `conventions` skill's
``references/use-case-doc.md``; the ``spec:`` block it carries is rendered by
``render.py``.

PyYAML parses the frontmatter. Without it, a small block-YAML fallback reads
only flat top-level fields; anything nested (every ``spec:``) raises
``DocError``, which is why the agent tools require PyYAML.
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only on a bare interpreter
    yaml = None


class DocError(Exception):
    """The document is missing, malformed, or fails a convention."""


_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)

# The optional ## Migration section is parsed: its head is a fixed set of
# labelled lines, each optional (conventions -> use-case-doc.md § Migration).
_MIGRATION_LINE = re.compile(r"^(owner|containers|units|unit):\s*(.*)$")


def read_frontmatter(text: str) -> dict:
    """Parse the YAML frontmatter of a use-case document."""
    m = _FRONTMATTER.match(text)
    if not m:
        raise DocError("no frontmatter block (expected --- ... --- at the top)")
    if yaml is not None:
        try:
            data = yaml.safe_load(m.group(1))
        except yaml.YAMLError as exc:
            raise DocError(f"frontmatter is not valid YAML: {exc}") from exc
    else:
        data = _fallback_load(m.group(1))
    if not isinstance(data, dict):
        raise DocError("frontmatter is not a YAML mapping")
    return data


def split(text: str) -> tuple[dict, str]:
    """Return (frontmatter, body) of a use-case document."""
    m = _FRONTMATTER.match(text)
    if not m:
        raise DocError("no frontmatter block (expected --- ... --- at the top)")
    return read_frontmatter(text), m.group(2)


def load(path: str | Path) -> dict:
    """Read and parse a use-case document, returning its frontmatter."""
    p = Path(path)
    if not p.is_file():
        raise DocError(f"no such use-case document: {p}")
    return read_frontmatter(p.read_text(encoding="utf-8"))


def parse_migration(body: str) -> dict:
    """Parse the labelled head of a ``## Migration`` section.

    Returns ``{"owner": ..., "containers": {spec_key: legacy_name},
    "units": [...]}``; every key is absent when the section or the line is
    absent. ``unit:`` (singular) is read as a one-item ``units:`` list.
    """
    out: dict = {}
    m = re.search(r"^## Migration\s*$(.*?)(?=^## |\Z)", body, re.DOTALL | re.MULTILINE)
    if not m:
        return out
    containers: dict[str, str] = {}
    units: list[str] = []
    owner = None
    for line in m.group(1).splitlines():
        lm = _MIGRATION_LINE.match(line.strip())
        if not lm:
            if line.strip():
                break  # first prose line ends the labelled head
            continue
        key, value = lm.group(1), lm.group(2).strip()
        if key == "owner":
            owner = value
        elif key == "containers":
            for pair in value.split(","):
                pair = pair.strip()
                if not pair:
                    continue
                if "=" not in pair:
                    raise DocError(f"Migration containers: entry {pair!r} is not <spec-key>=<legacy-name>")
                k, v = pair.split("=", 1)
                containers[k.strip()] = v.strip()
        elif key in ("units", "unit"):
            units.extend(u.strip() for u in value.split(",") if u.strip())
    if owner is not None:
        out["owner"] = owner
    if containers:
        out["containers"] = containers
    if units:
        out["units"] = units
    return out


def migration_inputs(body: str) -> dict:
    """Return the ``MIGRATION_UNITS``/``LEGACY_OWNER``/``LEGACY_NAMES`` values
    the audit probe's header takes, derived from a document body."""
    mig = parse_migration(body)
    return {
        "migration_units": mig.get("units", []),
        "legacy_owner": mig.get("owner", ""),
        "legacy_names": list(mig.get("containers", {}).values()),
    }


def set_frontmatter_value(text: str, key: str, value) -> str:
    """Set one top-level frontmatter scalar, preserving the rest of the file.

    Used for ``last_applied`` / ``last_applied_commit`` / ``status`` updates,
    where rewriting the whole document would lose body edits. ``value`` is
    rendered as a YAML scalar; ``None`` renders as ``null``.
    """
    m = _FRONTMATTER.match(text)
    if not m:
        raise DocError("no frontmatter block (expected --- ... --- at the top)")
    fm, body = m.group(1), m.group(2)
    scalar = _dump_scalar(value)
    lines = fm.splitlines()
    pattern = re.compile(rf"^{re.escape(key)}:(\s|$)")
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = f"{key}: {scalar}"
            break
    else:
        lines.append(f"{key}: {scalar}")
    return "---\n" + "\n".join(lines) + "\n---\n" + body


def _dump_scalar(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if yaml is not None:
        return yaml.safe_dump(text, default_flow_style=True).strip()
    if re.match(r"^[A-Za-z0-9_./:-]+$", text) and not text.lower() in ("null", "true", "false", "yes", "no", "on", "off"):
        return text
    import json

    return json.dumps(text)  # a JSON string is a valid YAML double-quoted scalar


def _fallback_load(text: str) -> dict:
    """Minimal block-YAML reader for flat mappings with flow lists.

    Only used when PyYAML is absent; enough for the top-level scalar/list
    fields the read-only tools touch. Raises DocError on anything nested.
    """
    import json

    data: dict = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if not m:
            raise DocError("frontmatter needs PyYAML (nested or non-scalar value)")
        key, raw = m.group(1), m.group(2).strip()
        if raw.startswith("[") and raw.endswith("]"):
            inner = raw[1:-1].strip()
            data[key] = [json.loads(f'"{x.strip().strip(chr(34))}"') if False else _unquote(x.strip()) for x in inner.split(",")] if inner else []
        elif raw == "":
            raise DocError("frontmatter needs PyYAML (nested mapping)")
        else:
            data[key] = _unquote(raw)
    return data


def _unquote(raw: str):
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    if raw in ("null", "~", ""):
        return None
    if raw in ("true", "false"):
        return raw == "true"
    try:
        return int(raw)
    except ValueError:
        return raw
