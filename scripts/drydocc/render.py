"""Render a use-case ``spec:`` block into quadlet ini text.

This is the committed home of the pure mapping ``spec -> {filename: ini-text}``
that `podman-quadlets` § "Render from spec" documents as the contract. The
skills previously emitted an inline ``python3 -c '...'`` snippet per run; the
mapping now lives here once, is unit-tested, and is reached through the
``render_quadlets`` / ``diff_quadlets`` operations (``docc.py``), exposed by
the MCP server and by ``drydo.py``.

The render is deterministic: keys emit in the documented order, unset fields
emit no line, and every document value has its ``%`` doubled (systemd expands
``%`` specifiers). No value is ever logged beyond the rendered text itself.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import doc as doc_mod

VOLUME_ROOT = "/var/lib/drydocc/volumes"

# Whether a bind mount carries the relabel option (Z/z) is the AGENT's
# distro-specific decision, passed in as `relabel` — never derived here from
# `system_facts.mac_framework`. The fact is an open vocabulary: a script
# cannot decide, for a value nobody has listed yet, whether that framework
# relabels bind-mount contexts (podman-quadlets § "Render from spec" gates the
# option on exactly that). The agent reads the recorded fact, checks it
# against the framework's own probe tables, and passes the verdict.


class SpecError(Exception):
    """The spec violates a validation rule; names the container and the key."""


def _esc(value) -> str:
    return str(value).replace("%", "%%")


def _mount_source(slug: str, mount: dict) -> str:
    if "volume" in mount and "from" in mount:
        raise SpecError(f"mount names both volume and from: {mount}")
    if "from" in mount:
        base = f"{VOLUME_ROOT}/{mount['from']}"
    elif "volume" in mount:
        base = f"{VOLUME_ROOT}/{slug}/{mount['volume']}"
    else:
        raise SpecError(f"mount names neither volume nor from: {mount}")
    sub = mount.get("subpath")
    if sub is not None:
        if sub.startswith("/") or ".." in Path(sub).parts:
            raise SpecError(f"subpath {sub!r} must be relative with no .. component")
        base = f"{base}/{sub}"
    return base


def _mount_opts(slug: str, mount: dict, spec: dict, relabel: bool) -> str:
    opts: list[str] = []
    if relabel:
        # z (shared) when more than one container of this document mounts the
        # volume, or when it is a shared volume; Z (private) otherwise.
        shared = False
        volume = mount.get("volume")
        vol_def = (spec.get("volumes") or {}).get(volume, {}) if volume else {}
        if vol_def.get("shared"):
            shared = True
        elif volume:
            count = sum(
                1
                for c in (spec.get("containers") or {}).values()
                for m in c.get("mounts") or []
                if m.get("volume") == volume
            )
            shared = count > 1
        opts.append("z" if shared else "Z")
    if mount.get("ro"):
        opts.append("ro")
    return ",".join(opts)


def _container_findings(slug: str, name: str, c: dict, spec: dict) -> list[str]:
    """Every validation rule this container breaks, each naming the key."""
    out: list[str] = []
    networks = spec.get("networks") or {}
    volumes = spec.get("volumes") or {}
    containers = spec.get("containers") or {}
    if "network" in c and "networks" in c:
        out.append(f"{slug}/{name}: network and networks are both set")
    net = c.get("network")
    nets = c.get("networks") or ([net] if net and net != "host" else [])
    for n in nets:
        if n != "host" and n not in networks:
            out.append(f"{slug}/{name}: network {n!r} is not a key of spec.networks")
    if net == "host":
        for forbidden in ("ports", "networks", "aliases", "listens"):
            if forbidden == "listens":
                continue
            if c.get(forbidden):
                out.append(f"{slug}/{name}: network: host excludes {forbidden}")
    else:
        if c.get("listens"):
            out.append(f"{slug}/{name}: a non-host container excludes listens")
    for cap in c.get("cap_add") or []:
        if not re.fullmatch(r"[A-Z0-9_]+", str(cap)) or str(cap).startswith("CAP_"):
            out.append(f"{slug}/{name}: cap_add entry {cap!r} must be a bare upper-case capability name")
    st = c.get("start_timeout")
    if st is not None and (not isinstance(st, int) or st <= 0):
        out.append(f"{slug}/{name}: start_timeout must be a positive integer number of seconds")
    for p in c.get("ports") or []:
        if not re.fullmatch(r"([0-9.]+:)?[0-9]+:[0-9]+/(tcp|udp|sctp)", str(p)):
            out.append(f"{slug}/{name}: ports entry {p!r} must be [<addr>:]<host>:<container>/<proto> with single ports")
    for l in c.get("listens") or []:
        if not re.fullmatch(r"[0-9]+(-[0-9]+)?/(tcp|udp|sctp)", str(l)):
            out.append(f"{slug}/{name}: listens entry {l!r} must be <port>/<proto> or <lo>-<hi>/<proto>")
    for m in c.get("mounts") or []:
        has_vol, has_from = "volume" in m, "from" in m
        if has_vol == has_from:
            out.append(f"{slug}/{name}: a mount names exactly one of volume or from")
        if has_vol and m["volume"] not in volumes:
            out.append(f"{slug}/{name}: mount volume {m['volume']!r} is not a key of spec.volumes")
        if has_vol and (volumes.get(m["volume"]) or {}).get("source") == "files" and not m.get("ro"):
            out.append(f"{slug}/{name}: a mount of a files volume sets ro: true")
        if has_from and not m.get("ro"):
            out.append(f"{slug}/{name}: a from: mount sets ro: true")
    if c.get("oneshot"):
        if c.get("restart"):
            out.append(f"{slug}/{name}: a oneshot container sets no restart")
        for other_name, other in containers.items():
            if name != other_name and name in (other.get("depends_on") or []):
                out.append(f"{slug}/{other_name}: depends_on lists the oneshot container {name!r}")
    image = str(c.get("image", ""))
    if not image or ":" not in image or image.endswith(":latest"):
        out.append(f"{slug}/{name}: image must carry a pinned tag (:latest is a validation error)")
    return out


def _validate_container(slug: str, name: str, c: dict, spec: dict) -> None:
    findings = _container_findings(slug, name, c, spec)
    if findings:
        raise SpecError("; ".join(findings))


def spec_findings(slug: str, spec: dict) -> list[str]:
    """Every validation finding across the spec's containers."""
    out: list[str] = []
    for name, c in (spec.get("containers") or {}).items():
        out.extend(_container_findings(slug, name, c or {}, spec))
    return out


def _validate(slug: str, spec: dict) -> None:
    findings = spec_findings(slug, spec)
    if findings:
        raise SpecError("; ".join(findings))


def _container_networks(slug: str, c: dict) -> list[str]:
    """The spec.networks keys this container joins (``[]`` for ``network: host``).

    A container with neither ``network`` nor ``networks`` joins the default
    network, whose key is ``net`` (full name ``<slug>-net``).
    """
    net = c.get("network")
    if net == "host":
        return []
    nets = list(c.get("networks") or [])
    if not nets and net:
        nets = [net]
    if not nets:
        nets = ["net"]
    return nets


def render_container(slug: str, name: str, c: dict, spec: dict, relabel: bool = False) -> str:
    """Render one ``<slug>-<container>.container`` unit."""
    _validate_container(slug, name, c, spec)
    oneshot = bool(c.get("oneshot"))
    nets = _container_networks(slug, c)
    deps = [f"{slug}-{d}.service" for d in c.get("depends_on") or []]
    net_units = [f"{slug}-{n}-network.service" for n in nets]
    after = " ".join(net_units + deps)
    lines = [
        "[Unit]",
        f"Description={_esc(slug)}/{_esc(name)} (drydocc)",
    ]
    if after:
        lines.append(f"After={after}")
        lines.append(f"Requires={after}")
    lines.append("")
    lines.append("[Container]")
    lines.append(f"Image={_esc(c['image'])}")
    lines.append(f"ContainerName={_esc(slug)}-{_esc(name)}")
    if nets:
        for n in nets:
            lines.append(f"Network={_esc(slug)}-{_esc(n)}.network")
    else:
        lines.append("Network=host")
    if c.get("hostname"):
        lines.append(f"HostName={_esc(c['hostname'])}")
    if c.get("user"):
        lines.append(f"User={_esc(c['user'])}")
    caps = c.get("cap_add") or []
    if caps:
        lines.append("AddCapability=" + " ".join(_esc(x) for x in caps))
    if c.get("env_from"):
        lines.append(f"EnvironmentFile={_esc(c['env_from'])}")
    for k, v in (c.get("env") or {}).items():
        lines.append(f"Environment={_esc(k)}={_esc(v)}")
    for m in c.get("mounts") or []:
        src = _mount_source(slug, m)
        opts = _mount_opts(slug, m, spec, relabel)
        line = f"Volume={src}:{m['target']}"
        if opts:
            line += f":{opts}"
        lines.append(line)
    for p in c.get("ports") or []:
        lines.append(f"PublishPort={_esc(p)}")
    hc = c.get("healthcheck") or {}
    if hc:
        if hc.get("cmd"):
            lines.append(f"HealthCmd={_esc(hc['cmd'])}")
        if hc.get("interval"):
            lines.append(f"HealthInterval={_esc(hc['interval'])}")
        if hc.get("timeout"):
            lines.append(f"HealthTimeout={_esc(hc['timeout'])}")
        if hc.get("retries"):
            lines.append(f"HealthRetries={_esc(hc['retries'])}")
        if hc.get("start_period"):
            lines.append(f"HealthStartPeriod={_esc(hc['start_period'])}")
    if c.get("entrypoint"):
        lines.append(f"PodmanArgs=--entrypoint={_esc(c['entrypoint'])}")
    for alias in c.get("aliases") or []:
        lines.append(f"PodmanArgs=--network-alias={_esc(alias)}")
    if c.get("exec"):
        lines.append(f"Exec={_esc(c['exec'])}")
    lines.append("")
    lines.append("[Service]")
    if oneshot:
        lines.append("Type=oneshot")
        if c.get("start_timeout"):
            lines.append(f"TimeoutStartSec={_esc(c['start_timeout'])}")
    else:
        lines.append(f"Restart={_esc(c.get('restart') or 'on-failure')}")
        if c.get("start_timeout"):
            lines.append(f"TimeoutStartSec={_esc(c['start_timeout'])}")
        lines.append("")
        lines.append("[Install]")
        lines.append("WantedBy=default.target")
    return "\n".join(lines) + "\n"


def render_network(slug: str, name: str, n: dict) -> str:
    """Render one ``<slug>-<network>.network`` unit."""
    lines = [
        "[Unit]",
        f"Description={_esc(slug)}/{_esc(name)} (drydocc)",
        "",
        "[Network]",
        f"NetworkName={_esc(slug)}-{_esc(name)}",
    ]
    if n.get("internal"):
        lines.append("Internal=true")
    if n.get("subnet"):
        lines.append(f"Subnet={_esc(n['subnet'])}")
    lines.append("")
    lines.append("[Install]")
    lines.append("WantedBy=default.target")
    return "\n".join(lines) + "\n"


def render_spec(spec: dict, slug: str, relabel: bool = False) -> dict[str, str]:
    """Render a whole ``spec:`` block to ``{filename: ini-text}``.

    Filenames are ``<slug>-<container>.container`` and
    ``<slug>-<network>.network``. ``.volume`` units are never emitted.
    """
    _validate(slug, spec)
    out: dict[str, str] = {}
    for name, n in (spec.get("networks") or {}).items():
        out[f"{slug}-{name}.network"] = render_network(slug, name, n or {})
    for name, c in (spec.get("containers") or {}).items():
        out[f"{slug}-{name}.container"] = render_container(slug, name, c or {}, spec, relabel)
    return out


def render_document(path: str | Path, relabel: bool = False) -> dict[str, str]:
    """Load a use-case document and render its ``spec:`` block."""
    fm = doc_mod.load(path)
    spec = fm.get("spec")
    if not isinstance(spec, dict):
        raise doc_mod.DocError(f"{path}: frontmatter has no spec: mapping")
    slug = str(fm.get("name") or Path(path).stem)
    return render_spec(spec, slug, relabel)


def validate_document(path: str | Path) -> list[str]:
    """Check the static invariants that need no node access.

    Returns a list of human-readable findings; empty means the document is
    statically valid. Mirrors the rules in `podman-quadlets` § "Render from
    spec" and the summary/spec key agreement in `conventions`.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
        fm = doc_mod.read_frontmatter(text)
    except (OSError, doc_mod.DocError) as exc:
        return [f"INVALID: {exc}"]
    findings: list[str] = []
    slug = str(fm.get("name") or p.stem)
    spec = fm.get("spec") or {}
    if not isinstance(spec, dict):
        return [f"INVALID: frontmatter spec: is not a mapping"]
    for list_key, spec_key in (("containers", "containers"), ("networks", "networks"), ("volumes", "volumes")):
        summary = fm.get(list_key) or []
        spec_names = set(spec.get(spec_key) or {})
        if set(summary) != spec_names:
            findings.append(
                f"{slug}: top-level {list_key} {sorted(summary)} != spec.{spec_key} keys {sorted(spec_names)}"
            )
    findings.extend(spec_findings(slug, spec))
    return findings


def normalize(text: str) -> str:
    """Text with exactly one final newline.

    A file written with ``printf '%s\\n'`` (node-exec's piping contract) gains
    a second final newline, and an admin's editor may drop it; neither is a
    change to the unit, so comparisons are made on this form.
    """
    return text.rstrip("\n") + "\n"


def digest(files: dict[str, str]) -> str:
    """A short fingerprint of a render: what the admin was shown and confirmed."""
    import hashlib

    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode() + b"\0" + normalize(files[name]).encode() + b"\0")
    return h.hexdigest()[:16]


def diff_rendered(rendered: dict[str, str], on_disk: dict[str, str]) -> dict[str, dict]:
    """Classify rendered files against on-disk quadlet text.

    Returns ``{filename: {"class": "same"|"changed"|"added"|"dropped", ...}}``:
    ``changed`` carries the unified ``diff``; ``added`` (rendered, not on
    disk) and ``dropped`` (on disk, no longer rendered) carry the unit's full
    ``text`` so the caller can show it. ``same`` carries nothing else. Texts
    are compared ``normalize``d: trailing newlines are never a change.
    """
    import difflib

    out: dict[str, dict] = {}
    for name in sorted(set(rendered) | set(on_disk)):
        r, d = rendered.get(name), on_disk.get(name)
        if r is not None and d is not None:
            r, d = normalize(r), normalize(d)
            if r == d:
                out[name] = {"class": "same"}
            else:
                diff = "".join(
                    difflib.unified_diff(
                        d.splitlines(keepends=True), r.splitlines(keepends=True), fromfile=f"on-node/{name}", tofile=f"rendered/{name}"
                    )
                )
                out[name] = {"class": "changed", "diff": diff}
        elif r is not None:
            out[name] = {"class": "added", "text": r}
        else:
            out[name] = {"class": "dropped", "text": d}
    return out
