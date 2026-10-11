# Audit Recipes

Probe matrix, classification rules, and menu copy for `/audit-use-case`.
The manifest schema and the command's write authority are in the `conventions`
skill's `references/audit-manifest.md`.

**This file does not duplicate fix commands.** Every remediation links to the
skill that owns it — `/modify-use-case` for live reconciliation,
`/bootstrap-node` for node hardening. Every render comparison links to
`podman-quadlets` `## Render from spec`.

All probes are **read-only** and run as `NODE_RUN` one-shots through
`node-exec`. Nothing here needs `NODE_RUN_LINGER`; if a probe is slow enough to
want lingering, it does not belong in an audit.

---

## Tier 1 — declared vs live

### The bundled probe

One `bash -s` heredoc, emitting stable `<key>=<value>` lines, in the shape
`onboard-node` established. `$SLUG` is the use-case slug; `$CONTAINERS`,
`$NETWORKS`, `$VOLUMES` are space-separated names read from the doc's
frontmatter; `$MIGRATION_UNIT` is the optional `unit:` line from the doc's
`## Migration` section body. The three `FIREWALL_*` values come from
`node.yaml:system_facts.firewall`, not from the doc. Multi-value keys
(`container=`, `unit=`, `port=`, `firewall=`, `quadlet=`) accumulate.

```bash
SLUG=<slug>
CONTAINERS="<space-separated container names>"
NETWORKS="<space-separated network names>"
VOLUMES="<space-separated volume names>"
MIGRATION_UNIT=<optional: value of unit: line from ## Migration section body, or empty if no Migration section>
FIREWALL_TOOL=<node.yaml:system_facts.firewall.tool — verbatim, whatever the probe recorded; the set is open, not two values, not a fixed list>
FIREWALL_TAG_MECHANISM=<node.yaml:system_facts.firewall.tag_mechanism — verbatim; `none` means no rule on this node can carry the slug>
FIREWALL_READBACK=<rule-listing command for this tool, from § "Observed dialects" below; EMPTY when no verified idiom exists for it — never a guess>

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

# Volumes — bind directories, NOT `podman volume ls` (no .volume units are emitted)
for v in $VOLUMES; do
  stat -c "volume=$v|%U:%G|%a" "/var/lib/drydocc/volumes/$SLUG/$v" 2>/dev/null \
    || echo "volume=$v|absent|"
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

# Host-level unit from ## Migration section (unit: field)
if [ -n "$MIGRATION_UNIT" ]; then
  enabled=$(systemctl is-enabled "$MIGRATION_UNIT" 2>/dev/null || true)
  if [ -z "$enabled" ]; then
    enabled="unknown"
  fi
  active=$(systemctl is-active "$MIGRATION_UNIT" 2>/dev/null || true)
  if [ -z "$active" ]; then
    active="unknown"
  fi
  echo "migration_unit=$MIGRATION_UNIT|$enabled|$active"
fi

# Quadlets — full text, for the render comparison
for f in ~/.config/containers/systemd/"$SLUG"-*.container \
         ~/.config/containers/systemd/"$SLUG"-*.network; do
  if [ -f "$f" ]; then
    echo "quadlet=$f"
    echo "---8<---"
    cat "$f"
    echo "--->8---"
  fi
done
```

`sudo -n` is used deliberately: a node where `doccap` cannot read the firewall
state or the env file yields `UNKNOWN` for that category rather than hanging on
a password prompt.

### Observed dialects — evidence, not the supported list

`FIREWALL_READBACK` sources its value from this table (per
`conventions/references/distro-agnosticism.md` Rule 3): match
`system_facts.firewall.tool` against the `tool` column below and use that
row's `read back` command. A `tool` value with no row here means nobody has
derived and verified a read-back for it yet — `FIREWALL_READBACK` is left
empty and the probe reports `firewall=no-probe`, never a guessed command. This
is the only place in this file where a tool may be named; an agent that
derives and verifies a new dialect extends this table rather than branching on
the tool elsewhere.

| tool | tag_mechanism | read back | verified on |
|---|---|---|---|
| ufw | comment | `ufw status numbered` | test-02, 2026-06 |
| firewalld | log-prefix | `firewall-cmd --list-rich-rules` | test-02, 2026-06 |

### The quadlet comparison

Re-render `spec:` in memory with the same inline `python3 -c '...'` snippet
`/apply-use-case` and `/modify-use-case` use — `podman-quadlets`
`## Render from spec` is the contract it implements. There is no
committed renderer; the snippet is journaled verbatim so an admin can replay the
comparison without Claude.

Compare the rendered `{filename: ini-text}` dict against the `quadlet=` blocks:

- rendered file missing on disk → `DRIFT` / `doc-ahead`
- on-disk text differs from rendered → `DRIFT` / **direction depends**: a
  difference the doc could express is `node-ahead` (someone hand-edited a value
  that belongs in `spec:`); anything else is `doc-ahead`. When in doubt, report
  `node-ahead` and let the capture-back menu's `[v] view` show the admin the
  unified diff.
- on-disk `<slug>-*` file the renderer does not produce → `DRIFT` /
  `node-ahead`, recovery `not rebuildable`

### Comparison table

| Category | Compare | `OK` ⇔ | `DRIFT` ⇔ | `UNKNOWN` ⇔ |
|---|---|---|---|---|
| Containers | `container=` vs `containers:` | every declared container present and `running` | any absent, or state ≠ `running`; any `seen_container=` not declared | `podman` missing / probe non-zero |
| Image pin | `digest=` vs `spec.containers.*.image` | digest resolves and tag matches the pin | tag on the running container ≠ the pinned tag | digest `unknown` (image pruned or offline) |
| Health | `container=` health field | `healthy`, or `none` (image defines no healthcheck) | `unhealthy` while `status: active` | `starting` |
| Units | `unit=` | `enabled` + `active` | `disabled`, `inactive`, `failed`, or `NRestarts` above the crashloop threshold (default 5) | unit not found |
| Quadlets | rendered vs `quadlet=` | byte-identical after trailing-whitespace normalisation | see above | render failed, or quadlet dir unreadable |
| Networks | `network=` vs `networks:` | every declared network present | any absent, or an undeclared `<slug>-*` network exists | `podman network ls` non-zero |
| Volumes | `volume=` vs `spec.volumes.*` | dir exists, owner and mode match (defaults `doccap:doccap`, `0750`) | absent, or owner/mode differ | `stat` denied |
| Ports | `port=` + `firewall=` vs `ports:` | every declared port listening, and — where a rule can be attributed at all — a `firewall=` line matching it | declared but not listening; a tagged rule with no matching `ports:` entry; `firewall=none-tagged` while ports are declared | `firewall=untaggable` (rules on this node cannot carry the slug: `tool: none` or `tag_mechanism: none`); `firewall=no-probe` (tool known, no verified read-back in § "Observed dialects"); `firewall=unreadable` (`sudo -n` denied, or the read-back command failed); or `ss` non-zero |
| Secrets | `envfile=` + `envkey=` vs `<slug>.secrets.yaml` | file present, `0600 doccap:doccap`, key names match `keys[].name` | `has_secrets: true` but file absent; wrong mode/owner; key names present that the manifest does not declare | env file unreadable |
| Status coherence | derived | `active` with every declared container running, **and** — only if the doc declares a migration unit — that unit **not live** in the sense defined below (both fields known, neither reporting liveness); `planned` with nothing live | `active` with nothing running; `active` while a declared migration unit is still **live** (cut-over incomplete — container and host service both serving); `planned` with a running `container=` or a live migration unit (the `/onboard-node` hand-off) | containers `UNKNOWN`, or a declared migration unit **indeterminate** (either field `unknown` — systemctl unreachable or absent) |
| Static invariants | doc only | `containers`/`networks`/`volumes` ⇔ `spec.*` keys; every image pinned | list/`spec` mismatch, or a `:latest` tag | — |

Static invariants need no node access and are checked even when every probe
fails.

### Status coherence — the two conditions its cells turn on

**A declared migration unit falls into exactly one of three classes.** Read the
`migration_unit=<unit>|<enabled>|<active>` line and apply these rules **in this
order** — the precedence is what keeps the classes disjoint:

1. **live** — the enabled field reports `enabled`, **or** the active field
   reports `active`. Positive evidence outranks a missing reading.
2. **indeterminate** — not live by (1), and **either** field reports `unknown`.
   The probe emits `unknown` when `systemctl` returned nothing, so a broken or
   absent unit manager lands here and must never be read as a finished
   cut-over.
3. **not live** — neither field reports liveness **and both** report a known
   state. Only this class supports `OK`.

The two fields are orthogonal, so the partial states are real: `enabled` +
`inactive` is a unit that will start itself at the next boot and fight the
container for its port, and it is **not** a completed cut-over. Every pair the
probe can emit:

| enabled field | active field | Class | Contribution to the cell |
|---|---|---|---|
| `enabled` | `active` | live | `DRIFT` |
| `enabled` | `inactive` | live | `DRIFT` |
| `disabled` | `active` | live | `DRIFT` |
| `disabled` | `inactive` | not live | `OK` |
| `not-found` | `inactive` | not live | `OK` |
| `unknown` | `unknown` | indeterminate | `UNKNOWN` |

Any pair outside this table is still covered by the three rules in order — a
half-read line such as `unknown` + `active` is **live** by (1), and `unknown` +
`inactive` is **indeterminate** by (2). `UNKNOWN` therefore always beats `OK`,
and positive liveness always beats `UNKNOWN`.

A document with no `## Migration` section emits no `migration_unit=` line and
declares no unit — the conjunct drops out of every cell rather than failing, so
an ordinary use-case is judged on its containers alone.

**The remedy depends on whether the document can be applied at all.** For a
`planned` document with a non-empty `spec.containers`, the fix is
`/apply-use-case <slug>` — the hand-off `/onboard-node` left. For a `planned`
document with **empty** `spec.containers` — a Tier 2 capture — that command
refuses by construction, so the finding is `node-ahead` with recovery `not
rebuildable`, and the remedy is the human work the document's own `## Runbook`
names: author a `spec:` block from its `## Migration` section, then apply. That
state is permanent by design until someone does; report it honestly rather than
emitting an invocation that will be refused.

---

## Tier 2 — owned by `/node-maintenance`

Resource footprint (CPU, memory, writable-layer growth), memory-growth anomaly
detection, and container log error/warning scanning are **not** part of an audit.
They belong to `/node-maintenance`: a one-shot sampler records them, a
per-container baseline is learned and kept in the telemetry block of
`<slug>.audit.yaml`, and its `resources-within-baseline` and
`logs-within-baseline` posture items judge sustained deviation
(`node-maintenance/references/maintenance-recipes.md` § "Telemetry sampling",
§ "Baselines and anomalies"). An audit reads that block only to leave it
alone — it never writes or judges it. An audit answers *"does reality match
the declaration, and can I rebuild this?"* — not *"how is it performing?"*.

---

## Tier 3 — unmanaged mutation

The recovery half. Where Tier 1 asks whether the node matches the doc, Tier 3
asks **what exists on the node that the doc could never reproduce**.

### Writable-layer mutation (universal; needs no manifest)

```bash
for c in $CONTAINERS; do
  podman diff "$SLUG-$c" 2>/dev/null | sed "s|^|wdiff=$c\||"
done
```

`podman diff` lists paths `A`dded, `C`hanged, or `D`eleted in the container's
writable layer relative to its image. Filter out every path that falls under a
declared mount target (`spec.containers.<c>.mounts[].target`) — those are
persisted in a bind mount and are Tier 1's business.

**What remains is configuration that a rebuild destroys.** This is the single
highest-value finding in the whole skill, it is universal, and it needs no
manifest. Report it as `DRIFT` / `node-ahead`, recovery `lost on rebuild`.

Digest the filtered listing and store it as `writable_layer/<container>` in the
manifest's `fingerprints`, so a subsequent audit can distinguish "still the same
known mutation" from "something new happened".

Note the failure mode: a stopped container cannot be diffed. Report `UNKNOWN`
rather than "clean" — a stopped container is never evidence of no mutation.

### Declared config surfaces (needs the manifest)

For each `config_surfaces[]` entry, with `P=/var/lib/drydocc/volumes/$SLUG/<volume>/<path>`:

```bash
# file
sha256sum "$P" 2>/dev/null | awk '{print "surface=<key>|sha256:"$1}'
# directory — digest the sorted listing, not the contents
find "$P" -type f -printf '%P|%s|%T@\n' 2>/dev/null | sort | sha256sum \
  | awk '{print "surface=<key>|sha256:"$1}'
```

Compare against `fingerprints`:

- key absent from `fingerprints` → first sighting; record it, report `OK` with a
  note. Do not alarm on a baseline that has never been taken.
- digest matches → `OK`
- digest differs → `DRIFT` / `node-ahead`, recovery `persisted, undeclared`
- **stale baseline** — if `<slug>.md:last_applied_commit` is newer than the
  manifest's `last_audited_commit`, the use-case was re-applied since the
  baseline was taken. Re-baseline silently and report `OK`; a mismatch there
  says nothing about manual configuration.

Entries matching `ignore[]` are dropped before comparison.

### Remaining universal probes

| Probe | Finding | Recovery |
|---|---|---|
| On-disk `<slug>-*` quadlet the renderer does not produce | hand-added unit | `not rebuildable` |
| `firewall=` rule tagged `<slug>` with no matching `ports:` entry | manually opened port | `not rebuildable` |
| `envkey=` name absent from `<slug>.secrets.yaml:keys[]` | undeclared secret | `not rebuildable` |
| `seen_container=` matching `<slug>-*` but not declared | container outside the declaration | `not rebuildable` |

---

## Classification

Every finding carries a verdict **and** a direction:

| Direction | Meaning | Remedy |
|---|---|---|
| `doc-ahead` | declared, not live — the node is behind | recommend `/modify-use-case` (or `/apply-use-case` if never applied) |
| `node-ahead` | live, not declared — the doc is behind | offer capture-back; the doc catches up |
| — | `OK` or `UNKNOWN` | none |

Per-item failures never halt the run: a failed probe marks its
category `UNKNOWN`, the reason is recorded, and the audit continues.

The firewall probe is the sharpest illustration of "the reason is recorded":
`firewall=untaggable`, `firewall=no-probe`, and `firewall=unreadable` are all
`UNKNOWN` — direction `—`, no capture-back offered — but they are not the same
finding. `untaggable` says rules on this node structurally cannot carry the
slug at all; `no-probe` says the tool is known but nobody has derived and
verified a read-back for it yet (§ "Observed dialects"); `unreadable` says a
verified read-back exists but this run could not use it (permission denied, or
the command itself failed). Report the reason string verbatim rather than
collapsing them to one message — an admin acts on each differently.
`firewall=none-tagged` is not `UNKNOWN` at all: the read-back succeeded and
definitively found no rule for this slug, which is `DRIFT` / `doc-ahead` for
any declared port.

Run outcome: `success` iff every finding is `OK`; `partial` if any `DRIFT` or
`UNKNOWN`; `failed` only if the node was unreachable or the doc unparseable —
i.e. the audit could not be performed at all.

---

## Capture-back menu copy

Rendered once per `node-ahead` finding, in the shape of `onboard-node`'s
reconciliation menu so both skills read identically to an admin. Suppressed
entirely under `--report-only`.

```
[<finding-id>]  node-ahead
  observed:   <live state, one line>
  declared:   <doc state, or "absent">
  recovery:   <lost on rebuild | persisted, undeclared | not rebuildable>

  [p] promote  — write into spec:/ports:/secrets manifest; doc becomes truth
  [d] declare  — record as a config_surface in <slug>.audit.yaml (tracked, not managed)
  [i] ignore   — add to ignore:; never reported again
  [n] note     — append to ## Runbook; no schema change
  [v] view     — print the full diff, re-show the menu
```

### Branch semantics

- **`[p] promote`** — offered **only** when the doc catching up makes doc and
  node consistent with no further action:
  - an undeclared firewall-allowed port → append to `ports:`
  - an undeclared env key → append to `<slug>.secrets.yaml:keys[]` with
    `purpose: "adopted by audit; verify"` and `rotates: never`
  - an undeclared `<slug>-*` container or network → reverse-map into `spec:`
    using `onboard-node/references/onboard-recipes.md` § "Spec
    reverse-engineering rules", then add to the matching top-level list so the
    list ⇔ `spec` equality invariant holds
  - a hand-edited quadlet value that `spec:` can express → write the value into
    `spec:`

  After a promotion, re-run the affected Tier 1 check to confirm the finding
  clears. If it does not, revert the edit and fall through to `[n] note` — a
  promotion that leaves drift behind is worse than no promotion.

- **`[d] declare`** — appends a `config_surfaces[]` entry and records the current
  digest. The path becomes tracked but stays unmanaged: future changes are
  reported, not prevented. The right choice for app-written config that `spec:`
  genuinely cannot express.

- **`[i] ignore`** — appends to `ignore[]`. For paths that legitimately churn.

- **`[n] note`** — appends a dated bullet under the doc's `## Runbook`, the
  authority `conventions/references/use-case-doc.md` already grants
  (*"Agent appends learnings here as `/audit-use-case` … uncover issues"*).

- **`[v] view`** — prints the full diff or listing, then re-shows the menu.

### When promotion is not the answer

For a `lost on rebuild` finding, promotion is impossible — `spec:` cannot express
"someone edited `/etc/foo` inside the container". The real fix is to make the
path persist, which is a `/modify-use-case` job. Emit a ready-to-paste
invocation alongside the menu:

```
recovery: /modify-use-case <slug> "mount <path> in <container> from a new
          volume so this configuration survives a rebuild"
```

This is the point of the whole tier: turning an invisible, rebuild-fatal
mutation into a declared volume that `/apply-use-case` would reproduce.

### `doc-ahead` findings

No menu. `/audit-use-case` never touches the node. Report
the finding and emit the delegated invocation:

```
recovery: /modify-use-case <slug> "<what to reconcile>"
```

---

## What this skill does NOT do

- **It does not probe a `container_engine` other than podman.** Every runtime
  probe here — `podman inspect`, `podman diff`, `podman image inspect`,
  `podman network ls` — assumes `system_facts.container_engine: podman`.
  `onboard-node`'s inventory probe already records `docker_container=` lines
  for a box it finds running docker, so a docker-shaped adopted use-case can
  reach `status: planned` (or later `active`); this file has no read-back for
  it. Auditing one today reports every container `absent` rather than
  `UNKNOWN` — that gap is unclosed here.
- **It assumes `system_facts.service_manager: systemd` throughout** —
  `systemctl --user show`, `NRestarts`, and the quadlet paths under
  `~/.config/containers/systemd/`. Per Rule 4 of
  `conventions/references/distro-agnosticism.md` this is not a dialect
  assumption to relax: systemd is the one hard capability a node must have to
  be admitted at all, so every use-case this skill can ever audit
  already has it.
- **It does not execute commands itself.** It is reference material
  `/audit-use-case` reads when composing the next probe.
- **It does not hold remediation commands.** Every fix links to the skill
  that owns it — `/modify-use-case` for live reconciliation, `/bootstrap-node`
  for node hardening, `podman-quadlets` `## Render from spec` for the render
  comparison.
- **It does not decide a capture-back itself.** The menu copy and branch
  semantics above are what `/audit-use-case` shows; the admin (or the calling
  skill, under `--report-only`) makes the call.
- **It does not evaluate performance, memory growth, or scan logs.** That is
  Tier 2, owned by `/node-maintenance` (see above).
