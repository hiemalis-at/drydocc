"""The audit probe, as a committed script instead of an inline heredoc.

`audit-use-case`'s Tier 1 probe (``references/audit-recipes.md`` § "The
bundled probe") is a long shell script the agent used to paste per run. It
lives here once, is unit-tested for its parsing half, and is shipped to the
node as a single argument with stdin closed — never as a ``bash -s`` heredoc a
prompting command could swallow.

The probe is read-only. It emits stable ``<key>=<value>`` lines; the parsing
half (`parse_probe`) turns them into an inventory dict. Multi-value keys
accumulate.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .lingering import Connection, LingerError, shquote

# Keys whose lines accumulate (multi-value). Everything else keeps the last
# line seen.
MULTI_KEYS = {
    "container", "seen_container", "digest", "unit", "network", "volume", "volns",
    "filehash", "shared", "port", "firewall", "envkey", "quadlet",
    "jobfile", "jobtimer", "joblast", "migration_unit", "legacy_container",
    "surface",
}

BLOCK_BEGIN = "---8<---"
BLOCK_END = "--->8---"


@dataclass
class ProbeInputs:
    slug: str
    containers: list[str] = field(default_factory=list)
    networks: list[str] = field(default_factory=list)
    volumes: list[str] = field(default_factory=list)
    files_volumes: list[str] = field(default_factory=list)
    shared_mounts: list[str] = field(default_factory=list)
    jobs: list[str] = field(default_factory=list)
    migration_units: list[str] = field(default_factory=list)
    legacy_owner: str = ""
    legacy_names: list[str] = field(default_factory=list)
    firewall_tool: str = "unknown"
    firewall_tag_mechanism: str = "unknown"
    firewall_readback: str = ""

    def header(self) -> dict[str, str]:
        return {
            "SLUG": self.slug,
            "CONTAINERS": " ".join(self.containers),
            "NETWORKS": " ".join(self.networks),
            "VOLUMES": " ".join(self.volumes),
            "FILES_VOLUMES": " ".join(self.files_volumes),
            "SHARED_MOUNTS": " ".join(self.shared_mounts),
            "JOBS": " ".join(self.jobs),
            "MIGRATION_UNITS": " ".join(self.migration_units),
            "LEGACY_OWNER": self.legacy_owner,
            "LEGACY_NAMES": " ".join(self.legacy_names),
            "FIREWALL_TOOL": self.firewall_tool,
            "FIREWALL_TAG_MECHANISM": self.firewall_tag_mechanism,
            "FIREWALL_READBACK": self.firewall_readback,
        }


_PROBE_HEAD = r"""#!/bin/sh
# drydocc audit probe — read-only, emits <key>=<value> lines.
# Header assignments are filled by the caller (audit_probe.py); do not edit
# the assignments into the script by hand.
SLUG='__SLUG__'
CONTAINERS="__CONTAINERS__"
NETWORKS="__NETWORKS__"
VOLUMES="__VOLUMES__"
FILES_VOLUMES="__FILES_VOLUMES__"
SHARED_MOUNTS="__SHARED_MOUNTS__"
JOBS="__JOBS__"
MIGRATION_UNITS="__MIGRATION_UNITS__"
LEGACY_OWNER='__LEGACY_OWNER__'
LEGACY_NAMES="__LEGACY_NAMES__"
FIREWALL_TOOL='__FIREWALL_TOOL__'
FIREWALL_TAG_MECHANISM='__FIREWALL_TAG_MECHANISM__'
FIREWALL_READBACK='__FIREWALL_READBACK__'


# Containers — presence, state, image, digest, health
for c in $CONTAINERS; do
  podman inspect "$SLUG-$c" --format \
    'container={{.Name}}|{{.State.Status}}|{{.ImageName}}|{{.Image}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    2>/dev/null || echo "container=$SLUG-$c|absent|||"
done

# Any container matching the slug prefix that we did NOT ask about
podman ps --all --filter "name=^$SLUG-" --format '{{.Names}}' 2>/dev/null \
  | sed 's/^/seen_container=/'

# Image digests — catches a manual pull that moved a pinned tag
for c in $CONTAINERS; do
  img=$(podman inspect "$SLUG-$c" --format '{{.ImageName}}' 2>/dev/null) || continue
  podman image inspect "$img" --format "digest=$c|{{index .RepoDigests 0}}" 2>/dev/null \
    || echo "digest=$c|unknown"
done

# Systemd user units — enabled, active, restart count
for c in $CONTAINERS; do
  systemctl --user show "$SLUG-$c.service" \
    -p UnitFileState -p ActiveState -p NRestarts --value 2>/dev/null \
    | paste -sd'|' - | sed "s/^/unit=$SLUG-$c.service|/"
done
for n in $NETWORKS; do
  systemctl --user show "$SLUG-$n-network.service" \
    -p UnitFileState -p ActiveState -p NRestarts --value 2>/dev/null \
    | paste -sd'|' - | sed "s/^/unit=$SLUG-$n-network.service|/"
done

# Networks
podman network ls --filter "name=^$SLUG-" --format 'network={{.Name}}' 2>/dev/null

# Volumes — bind directories, NOT `podman volume ls` (no .volume units are emitted).
# volns= is the same directory as doccap's containers see it, for owner: container:<uid>:<gid>
for v in $VOLUMES; do
  stat -c "volume=$v|%U:%G|%a" "/var/lib/drydocc/volumes/$SLUG/$v" 2>/dev/null \
    || echo "volume=$v|absent|"
  podman unshare stat -c "volns=$v|%u:%g|%a" "/var/lib/drydocc/volumes/$SLUG/$v" 2>/dev/null \
    || echo "volns=$v|unknown|"
done

# Files volumes — one line per file: relative path and sha256, never content
for v in $FILES_VOLUMES; do
  d="/var/lib/drydocc/volumes/$SLUG/$v"
  if [ ! -d "$d" ]; then echo "filehash=$v|absent|"; continue; fi
  out=$(cd "$d" && find . -type f -print0 | sort -z | xargs -0r sha256sum) \
    || { echo "filehash=$v|unreadable|"; continue; }
  [ -n "$out" ] || { echo "filehash=$v|empty|"; continue; }
  printf '%s\n' "$out" | sed "s|^\([0-9a-f]*\)  \./\(.*\)\$|filehash=$v\|\2\|\1|"
done

# Shared volumes this use-case reads — the owner's directory must exist
for m in $SHARED_MOUNTS; do
  if [ -d "/var/lib/drydocc/volumes/$m" ]; then echo "shared=$m|present"
  else echo "shared=$m|absent"; fi
done

# Listening ports
ss -tulpnH 2>/dev/null | awk '{print "port="$1"|"$5}'

# Firewall rules tagged for this use-case. Five outcomes, kept distinguishable:
# rules read back / read back but none tagged / tool known but this read failed /
# no verified read-back for this tool / rules cannot carry a tag at all.
if [ "$FIREWALL_TOOL" = none ] || [ "$FIREWALL_TAG_MECHANISM" = none ]; then
  echo "firewall=untaggable|$FIREWALL_TOOL|$FIREWALL_TAG_MECHANISM"
elif [ -z "$FIREWALL_READBACK" ]; then
  echo "firewall=no-probe|$FIREWALL_TOOL"
elif rules=$(sudo -n sh -c "$FIREWALL_READBACK" 2>/dev/null); then
  tagged=$(printf '%s\n' "$rules" | grep -F "$SLUG") || tagged=""
  if [ -n "$tagged" ]; then
    printf '%s\n' "$tagged" | sed 's/^/firewall=/'
  else
    echo "firewall=none-tagged|$FIREWALL_TOOL"
  fi
else
  echo "firewall=unreadable|$FIREWALL_TOOL"
fi

# Secrets env file — mode, owner, and key NAMES only (never values)
if [ -f "/etc/drydocc/secrets/$SLUG.env" ]; then
  stat -c "envfile=%U:%G|%a" "/etc/drydocc/secrets/$SLUG.env"
  sudo -n cut -d= -f1 "/etc/drydocc/secrets/$SLUG.env" 2>/dev/null \
    | grep -E '^[A-Z0-9_]+$' | sed 's/^/envkey=/'
else
  echo "envfile=absent|"
fi

# Units the use-case replaced, from ## Migration units: — a system unit bare,
# a user's own unit as <user>/<unit>, asked of that user's manager
for u in $MIGRATION_UNITS; do
  case "$u" in
    */*) sc="sudo -n systemctl --user -M ${u%%/*}@"; unit="${u#*/}" ;;
    *)   sc="systemctl"; unit="$u" ;;
  esac
  enabled=$($sc is-enabled "$unit" 2>/dev/null || true); [ -n "$enabled" ] || enabled="unknown"
  active=$($sc is-active "$unit" 2>/dev/null || true);   [ -n "$active" ]  || active="unknown"
  echo "migration_unit=$u|$enabled|$active"
done

# Legacy containers of another owner — after the cut-over none may run
if [ -n "$LEGACY_OWNER" ]; then
  OUID=$(id -u "$LEGACY_OWNER" 2>/dev/null) || OUID=""
  for n in $LEGACY_NAMES; do
    st=$( [ -n "$OUID" ] && sudo -n -u "$LEGACY_OWNER" XDG_RUNTIME_DIR="/run/user/$OUID" \
          podman inspect "$n" --format '{{.State.Status}}' 2>/dev/null ) || st=""
    if [ -z "$OUID" ]; then echo "legacy_container=$n|unknown"
    elif [ -n "$st" ]; then echo "legacy_container=$n|$st"
    else echo "legacy_container=$n|absent"; fi
  done
fi

"""

# Quadlets — full text, for the render comparison. Shared with read_quadlets.
# A file without a final newline gets one, so the closing marker always
# starts its own line and never swallows the rest of the output.
QUADLET_DUMP = r"""for f in ~/.config/containers/systemd/"$SLUG"-*.container \
         ~/.config/containers/systemd/"$SLUG"-*.network; do
  if [ -f "$f" ]; then
    echo "quadlet=$f"
    echo "---8<---"
    cat "$f"; [ -z "$(tail -c 1 "$f")" ] || echo
    echo "--->8---"
  fi
done
"""

_PROBE_TAIL = r"""
# Jobs — rendered files (same comparison as quadlets), timer state, last run
for f in ~/.config/drydocc/job-"$SLUG"-*.sh \
         ~/.config/systemd/user/drydocc-job-"$SLUG"-*.service \
         ~/.config/systemd/user/drydocc-job-"$SLUG"-*.timer; do
  if [ -f "$f" ]; then
    echo "jobfile=$f"
    echo "---8<---"
    cat "$f"; [ -z "$(tail -c 1 "$f")" ] || echo
    echo "--->8---"
  fi
done
for j in $JOBS; do
  systemctl --user show "drydocc-job-$SLUG-$j.timer" \
    -p UnitFileState -p ActiveState --value 2>/dev/null \
    | paste -sd'|' - | sed "s/^/jobtimer=$j|/"
  last=$(tail -n 1 "/var/lib/drydocc/jobs/$SLUG-$j.log" 2>/dev/null) || last=""
  echo "joblast=$j|${last:-never}"
done
"""

PROBE_SCRIPT = _PROBE_HEAD + "# Quadlets — full text, for the render comparison\n" + QUADLET_DUMP + _PROBE_TAIL


def _sh_single_quote(value: str) -> str:
    """POSIX single-quote a value for a shell assignment.

    ``'a'\\''b'`` — close the quote, an escaped quote, reopen — is the only
    form that carries an arbitrary string (single quotes included) through a
    shell assignment verbatim. Substituting the raw value into
    ``KEY='__KEY__'`` breaks on any value containing a single quote, which
    free-text values like the firewall read-back command can.
    """
    return "'" + str(value).replace("'", "'\\''") + "'"


def render_script(inputs: ProbeInputs) -> str:
    """Fill the probe's header assignments.

    Every value — including the firewall read-back command, which is
    agent-supplied free text — is single-quoted with the POSIX
    ``'a'\\''b'`` idiom, so no value can break out of its assignment, and
    ``render_script`` raises rather than emit a script with an unfilled
    placeholder.
    """
    header = inputs.header()
    script = PROBE_SCRIPT
    for key, value in header.items():
        quoted = _sh_single_quote(value)
        for form in (f"='__{key}__'", f"=\"__{key}__\""):
            if form in script:
                script = script.replace(form, f"={quoted}")
                break
        else:
            raise ValueError(f"probe template has no quoted placeholder for {key}")
    if re.search(r"__[A-Z_]+__", script):
        raise ValueError("unfilled probe header assignment remains")
    return script


def parse_probe(output: str) -> dict:
    """Parse probe output into an inventory dict.

    Multi-value keys accumulate into lists; ``quadlet=``/``jobfile=`` blocks
    (``---8<---`` ... ``--->8---``) become ``{path: text}`` maps under
    ``quadlets``/``jobfiles``, each text ending in a newline as the file does.
    All other keys keep the last line seen.
    """
    inv: dict = {"quadlets": {}, "jobfiles": {}}
    block_key = None
    block_path = None
    block_lines: list[str] = []

    def flush():
        nonlocal block_key, block_path, block_lines
        if block_key is not None and block_path is not None:
            inv[block_key][block_path] = "".join(f"{l}\n" for l in block_lines)
        block_key, block_path, block_lines = None, None, []

    for line in output.splitlines():
        if line == BLOCK_BEGIN:
            block_lines = []
            continue
        if line == BLOCK_END:
            flush()
            continue
        if block_key is not None:
            block_lines.append(line)
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key in ("quadlet", "jobfile"):
            block_key = "quadlets" if key == "quadlet" else "jobfiles"
            block_path = value
            block_lines = []
            continue
        if key in MULTI_KEYS:
            inv.setdefault(key, []).append(value)
        else:
            inv[key] = value
    flush()
    return inv


def run_script(script: str, conn: Connection | None = None) -> subprocess.CompletedProcess:
    """Run a read-only script locally (``conn`` is None: the ``on_node``
    surface) or on the node over SSH (``on_client``).

    The script travels as one argument with stdin closed — never a
    ``bash -s`` heredoc a prompting command could swallow. Over SSH, a
    non-zero exit with no output is a transport failure and raises.
    """
    if conn is None:
        return subprocess.run(["sh", "-c", script], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    proc = subprocess.run(
        conn.ssh_base() + [f"sh -c {shquote(script)}"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        raise LingerError(f"ssh failed (rc={proc.returncode}): {proc.stderr.strip()}")
    return proc


def run(inputs: ProbeInputs, conn: Connection | None = None) -> dict:
    """Run the probe on either surface and parse it."""
    proc = run_script(render_script(inputs), conn)
    return {"rc": proc.returncode, "inventory": parse_probe(proc.stdout), "stderr": proc.stderr}


def read_quadlets(slug: str, conn: Connection | None = None) -> dict[str, str]:
    """The node's current ``<slug>-*.{container,network}`` quadlets, as
    ``{filename: text}`` — the probe's own quadlet section, run alone."""
    script = f"SLUG={shquote(slug)}\n" + QUADLET_DUMP
    quadlets = parse_probe(run_script(script, conn).stdout)["quadlets"]
    return {Path(path).name: text for path, text in quadlets.items()}


def probe_inputs_from_doc(
    doc_path: str | Path,
    node_yaml: dict | None = None,
    firewall_readback: str = "",
) -> ProbeInputs:
    """Derive the probe's inputs from a use-case document and node.yaml.

    The doc supplies the slug, containers/networks/volumes, files volumes,
    shared mounts, jobs, and the ``## Migration`` values; ``node_yaml``
    supplies the ``system_facts.firewall`` triple.

    The ``## Migration`` derivation applies the recipe's two header rules
    (`audit-recipes.md` § "The bundled probe"):

    - ``LEGACY_OWNER`` is empty when the section is absent or the owner is
      ``doccap`` (the probe only asks about *another* user's legacy
      containers);
    - ``LEGACY_NAMES`` falls back to the spec keys themselves when the
      ``containers:`` line is absent (a spec key not listed adopts a legacy
      container of its own name — `use-case-doc.md` § Migration).

    ``firewall_readback`` is the agent's distro-specific decision, passed in
    from the verified dialect table in `audit-recipes.md` § "Observed
    dialects" — never guessed. Empty means "no verified read-back for this
    tool", which the probe reports as ``firewall=no-probe``.
    """
    from . import doc as doc_mod

    p = Path(doc_path)
    text = p.read_text(encoding="utf-8")
    fm, body = doc_mod.split(text)
    spec = fm.get("spec") or {}
    slug = str(fm.get("name") or p.stem)
    containers = spec.get("containers") or {}
    networks = spec.get("networks") or {}
    volumes = spec.get("volumes") or {}
    files_volumes = [v for v, d in volumes.items() if (d or {}).get("source") == "files"]
    shared_mounts = []
    for c in containers.values():
        for m in (c or {}).get("mounts") or []:
            if "from" in m and m["from"] not in shared_mounts:
                shared_mounts.append(str(m["from"]))
    jobs = list((spec.get("jobs") or {}).keys())
    mig = doc_mod.migration_inputs(body)
    legacy_owner = mig["legacy_owner"]
    if legacy_owner in ("", "doccap"):
        legacy_owner = ""
    legacy_names = mig["legacy_names"]
    if not legacy_names and legacy_owner:
        # containers: absent — every spec key adopts a legacy container of
        # its own name.
        legacy_names = list(containers)
    fw = ((node_yaml or {}).get("system_facts") or {}).get("firewall") or {}
    return ProbeInputs(
        slug=slug,
        containers=list(containers),
        networks=list(networks),
        volumes=list(volumes),
        files_volumes=files_volumes,
        shared_mounts=shared_mounts,
        jobs=jobs,
        migration_units=mig["migration_units"],
        legacy_owner=legacy_owner,
        legacy_names=legacy_names,
        firewall_tool=str(fw.get("tool") or "unknown"),
        firewall_tag_mechanism=str(fw.get("tag_mechanism") or "unknown"),
        firewall_readback=firewall_readback,
    )
