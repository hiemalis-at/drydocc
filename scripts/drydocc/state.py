"""Repo-state readers and writers for the fork's ``nodes/<node>/`` tree.

These functions are the scriptable form of the read steps several skills
perform by hand: the use-case listing table, the resume-pointer
classification, and the journal's derived "rebuilt, not yet restored" query.
They only read, and never touch a secret value.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from . import doc as doc_mod

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

POINTER_PATH = Path(".drydocc/last-command.yaml")


class StateError(Exception):
    """A state file is missing, malformed, or outside its schema."""


def _yaml_load(text: str, path: Path):
    if yaml is None:
        raise StateError(f"{path}: PyYAML is required to read this file")
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise StateError(f"{path}: not valid YAML: {exc}") from exc


def _iso(value) -> str:
    """Normalize a YAML-parsed timestamp to the ``YYYY-MM-DDTHH:MM:SSZ`` string.

    PyYAML parses an ISO 8601 scalar into a datetime; string comparison needs
    the canonical form back.
    """
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def _date(value) -> str:
    """The date portion of an ISO datetime, or ``—`` for null/absent."""
    if not value:
        return "—"
    return _iso(value)[:10]


def _truncate(items, limit: int = 3) -> str:
    items = [str(i) for i in items]
    if not items:
        return "—"
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f", +{len(items) - limit} more"


def list_use_cases(node_dir: str | Path) -> dict:
    """Build the `/list-use-cases` table from the repo alone.

    Returns ``{"node": ..., "rows": [...], "retired": <count or None>,
    "empty": <bool>}``. Each row carries the columns the skill renders:
    name, status, containers, ports, last_applied, last_audited, last_backup.
    A malformed doc becomes a row with ``status: INVALID``; a missing
    audit/backup manifest is normal, not an error.
    """
    base = Path(node_dir) / "use-cases"
    node = Path(node_dir).name
    rows = []
    if not base.is_dir():
        return {"node": node, "rows": [], "retired": None, "empty": True}
    for path in sorted(base.glob("*.md")):
        slug = path.stem
        try:
            fm = doc_mod.load(path)
        except doc_mod.DocError:
            rows.append(
                {
                    "name": slug,
                    "status": "INVALID",
                    "containers": "—",
                    "ports": "—",
                    "last_applied": "—",
                    "last_audited": "—",
                    "last_backup": "—",
                }
            )
            continue
        audit = base / f"{slug}.audit.yaml"
        backup = base / f"{slug}.backup.yaml"
        last_audited = "—"
        if audit.is_file():
            data = _yaml_load(audit.read_text(encoding="utf-8"), audit) or {}
            last_audited = _date(data.get("last_audited"))
        if backup.is_file():
            data = _yaml_load(backup.read_text(encoding="utf-8"), backup) or {}
            last_backup = _date(data.get("last_backup")) if data.get("last_backup") else "never"
        else:
            last_backup = "—"
        rows.append(
            {
                "name": slug,
                "status": str(fm.get("status") or "INVALID"),
                "containers": _truncate(fm.get("containers") or []),
                "ports": _truncate(fm.get("ports") or []),
                "last_applied": _date(fm.get("last_applied")),
                "last_audited": last_audited,
                "last_backup": last_backup,
            }
        )
    retired = base / "_retired"
    retired_count = None
    if retired.is_dir():
        retired_count = sum(1 for _ in retired.glob("*.md"))
    return {"node": node, "rows": rows, "retired": retired_count, "empty": not rows}


def render_table(listing: dict) -> str:
    """Render a listing from `list_use_cases` as the skill's markdown table."""
    lines = [f"Use-cases on {listing['node']}:", ""]
    header = "| name | status | containers | ports | last_applied | last_audited | last_backup |"
    lines.append(header)
    lines.append("|---|---|---|---|---|---|---|")
    for row in listing["rows"]:
        lines.append(
            "| {name} | {status} | {containers} | {ports} | {last_applied} | {last_audited} | {last_backup} |".format(**row)
        )
    if listing["empty"]:
        return f"No use-cases on {listing['node']} yet. Create one with /create-use-case."
    if listing["retired"]:
        lines.append("")
        lines.append(f"Retired: {listing['retired']} use-case(s) under _retired/. Use ls to inspect.")
    return "\n".join(lines)


def read_pointer(repo_root: str | Path) -> dict | None:
    """Read `.drydocc/last-command.yaml`; None when absent."""
    path = Path(repo_root) / POINTER_PATH
    if not path.is_file():
        return None
    data = _yaml_load(path.read_text(encoding="utf-8"), path)
    if not isinstance(data, dict):
        raise StateError(f"{path}: not a YAML mapping")
    return data


def pointer_finished(repo_root: str | Path, pointer: dict) -> dict | None:
    """Judge whether the pointed run finished, per `continue-last-command`.

    A run is finished once ``nodes/<node>/journal/`` holds an entry whose
    ``trigger`` starts with the recorded ``command`` and whose ``timestamp``
    is at or after ``started``. Returns that entry's frontmatter, or None.
    """
    node = pointer.get("node")
    command = str(pointer.get("command") or "")
    started = str(pointer.get("started") or "")
    if not node or not command:
        return None
    jdir = Path(repo_root) / "nodes" / str(node) / "journal"
    if not jdir.is_dir():
        return None
    best = None
    for entry in sorted(jdir.glob("*.md")):
        try:
            fm = doc_mod.read_frontmatter(entry.read_text(encoding="utf-8"))
        except doc_mod.DocError:
            continue
        trigger = str(fm.get("trigger") or "")
        timestamp = _iso(fm.get("timestamp"))
        # The journal filename carries the same UTC minute as the timestamp;
        # compare on that prefix so a clock skew of seconds never misjudges.
        if trigger.startswith(command) and timestamp[:16] >= started[:16]:
            if best is None or timestamp > _iso(best.get("timestamp")):
                best = fm
    return best


def owes_restore(journal_dir: str | Path, slug: str) -> bool:
    """The journal's derived query "Rebuilt, not yet restored".

    True when the most recent ``/apply-use-case`` entry for
    ``scope: use-case/<slug>`` has ``mode: rebuild`` and no later entry for
    the same scope is a successful, non-``--test`` ``/restore-use-case``.
    """
    jdir = Path(journal_dir)
    if not jdir.is_dir():
        return False
    scope = f"use-case/{slug}"
    entries = []
    for path in sorted(jdir.glob("*.md")):
        try:
            fm = doc_mod.read_frontmatter(path.read_text(encoding="utf-8"))
        except doc_mod.DocError:
            continue
        if fm.get("scope") != scope:
            continue
        entries.append((_iso(fm.get("timestamp")), str(fm.get("trigger") or ""), fm))
    if not entries:
        return False
    rebuilt_at = None
    for timestamp, trigger, fm in entries:
        if trigger.startswith("/apply-use-case") and fm.get("mode") == "rebuild":
            rebuilt_at = timestamp
    if rebuilt_at is None:
        return False
    for timestamp, trigger, fm in entries:
        if timestamp <= rebuilt_at:
            continue
        if trigger.startswith("/restore-use-case") and "--test" not in trigger and fm.get("outcome") == "success":
            return False
    return True
