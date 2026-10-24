"""Write rendered files into doccap's quadlet directory on the node.

The one place a drydocc operation writes to the node. Every file lands
atomically — a temporary file in the same directory, mode 0644, then a
rename — so a dropped connection never leaves a half-written unit for the
next ``daemon-reload`` to pick up. Content travels as data (Python on the
node, stdin over SSH); the remote command is always an argument.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from .lingering import Connection, LingerError, shquote

QUADLET_DIR = ".config/containers/systemd"


def write_files(files: dict[str, str], conn: Connection | None = None) -> None:
    """Write ``{filename: text}`` into ``~/.config/containers/systemd/``.

    ``conn`` None is the ``on_node`` surface (this process is doccap on the
    node); otherwise one SSH call per file. The caller validates the names.
    """
    if conn is None:
        target = Path.home() / QUADLET_DIR
        target.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            fd, tmp = tempfile.mkstemp(prefix=".dd-", dir=target)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(text)
                os.chmod(tmp, 0o644)
                os.replace(tmp, target / name)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        return
    for name, text in files.items():
        remote = (
            f'd="$HOME/{QUADLET_DIR}"; mkdir -p "$d" && t=$(mktemp "$d/.dd-XXXXXX") && '
            f'{{ cat >"$t" && chmod 644 "$t" && mv -f "$t" "$d"/{shquote(name)}; }} '
            f'|| {{ rm -f "$t"; exit 1; }}'
        )
        proc = subprocess.run(conn.ssh_base() + [remote], input=text, capture_output=True, text=True)
        if proc.returncode != 0:
            raise LingerError(f"writing {name} failed (rc={proc.returncode}): {proc.stderr.strip()}")
