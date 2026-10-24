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

One bundled script, emitting stable `<key>=<value>` lines, in the shape
`onboard-node` established, including how it is sent: as an argument, with
stdin closed, never as a `bash -s` heredoc that a prompting command could
swallow. The script text lives in exactly one place:
`scripts/drydocc/audit_probe.py` (`PROBE_SCRIPT`) — do not copy it here; two
copies drift, and they already have once. `audit_probe_run` fills its
header from the doc and `node.yaml` (the firewall read-back is its
`firewall_readback` argument), runs it, and parses the output. `$SLUG` is the
use-case slug; `$CONTAINERS`, `$NETWORKS`, `$VOLUMES` are space-separated names read from the doc's
frontmatter; `$MIGRATION_UNITS` are the doc's `## Migration` `units:` entries
(or its single `unit:`), `$LEGACY_OWNER` its `owner:` and `$LEGACY_NAMES` the
legacy names its `containers:` pairs map to (the spec keys themselves when the
line is absent). The three `FIREWALL_*` values come from
`node.yaml:system_facts.firewall`, not from the doc. Multi-value keys
(`container=`, `unit=`, `port=`, `firewall=`, `quadlet=`, `filehash=`,
`shared=`, `jobfile=`, `jobtimer=`, `joblast=`, `migration_unit=`,
`legacy_container=`, `volns=`) accumulate.

The header assignments below are the contract `audit_probe_run` fills, and
the one an agent without python3 fills by hand:

```bash
SLUG=<slug>
CONTAINERS="<space-separated container names>"
NETWORKS="<space-separated network names>"
VOLUMES="<space-separated volume names>"
FILES_VOLUMES="<space-separated names of the volumes that declare source: files; empty if none>"
SHARED_MOUNTS="<space-separated <owner-slug>/<vol> values of every from: mount; empty if none>"
JOBS="<space-separated spec.jobs names; empty if none>"
MIGRATION_UNITS="<space-separated units: entries (a unit:, as one entry) from ## Migration; empty if none>"
LEGACY_OWNER=<owner: from ## Migration; empty when absent or doccap>
LEGACY_NAMES="<space-separated legacy container names, only when LEGACY_OWNER is set; the spec keys themselves when the containers: line is absent>"
FIREWALL_TOOL=<node.yaml:system_facts.firewall.tool — verbatim, whatever the probe recorded; the set is open, not two values, not a fixed list>
FIREWALL_TAG_MECHANISM=<node.yaml:system_facts.firewall.tag_mechanism — verbatim; `none` means no rule on this node can carry the slug>
FIREWALL_READBACK=<rule-listing command for this tool, from § "Observed dialects" below; EMPTY when no verified idiom exists for it — never a guess>
```

The probe reads the firewall rules and the env file's key names with
`sudo -n` on purpose: a node where `doccap` cannot read them yields `UNKNOWN`
for that category rather than hanging on a password prompt.

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

`audit_probe_run` re-renders `spec:` in memory (`podman-quadlets`
§ "Render from spec" is the contract) and classifies every file against the
`quadlet=` blocks: `same`, `changed` (with the unified diff), `added` (rendered,
missing on disk) or `dropped` (on disk, not rendered). Its `replay` line lets an
admin repeat the comparison without the agent. A difference only in trailing
newlines is not drift: `node-exec`'s piping contract can add one, and an editor
can drop one. Each class maps to a finding:

- `same` → `OK`
- `added` → `DRIFT` / `doc-ahead`
- `changed` → `DRIFT` / **direction depends**: a
  difference the doc could express is `node-ahead` (someone hand-edited a value
  that belongs in `spec:`); anything else is `doc-ahead`. When in doubt, report
  `node-ahead` and let the capture-back menu's `[v] view` show the admin the
  unified diff.
- `dropped` → `DRIFT` / `node-ahead`, recovery `not rebuildable`

### Comparison table

| Category | Compare | `OK` ⇔ | `DRIFT` ⇔ | `UNKNOWN` ⇔ |
|---|---|---|---|---|
| Containers | `container=` vs `containers:` | every declared container present and `running` — a `oneshot` container instead absent or `exited` (it runs only inside its job) | any absent, or state ≠ `running` (a `oneshot` container: `running` outside a job run is fine, any other state but `exited`/absent is not); any `seen_container=` not declared | `podman` missing / probe non-zero |
| Image pin | `digest=` vs `spec.containers.*.image` | digest resolves and tag matches the pin | tag on the running container ≠ the pinned tag | digest `unknown` (image pruned or offline) |
| Health | `container=` health field | `healthy`, or `none` (neither the image nor `spec` defines a healthcheck) | `unhealthy` while `status: active`; `none` while the container's `spec` declares a `healthcheck` | `starting` |
| Units | `unit=` | `enabled` or `generated` (a quadlet's unit), + `active` (a `oneshot` container's unit: `inactive`, and never `failed` — its job reports its runs) | `disabled`, `inactive`, `failed`, or `NRestarts` above the crashloop threshold (default 5) | unit not found |
| Quadlets | rendered vs `quadlet=` | byte-identical after trailing-whitespace normalisation | see above | render failed, or quadlet dir unreadable |
| Networks | `network=` vs `networks:` | every declared network present | any absent, or an undeclared `<slug>-*` network exists | `podman network ls` non-zero |
| Volumes | `volume=` vs `spec.volumes.*`; `volns=` instead for an `owner: container:<uid>:<gid>` volume | dir exists, owner and mode match (defaults `doccap:doccap`, `0750`; a container-form owner as `<uid>:<gid>` on the `volns=` line) | absent, or owner/mode differ | `stat` denied; `volns=…|unknown` for a container-form owner |
| Ports | `port=` + `firewall=` vs `ports:`, each container's published host ports, and each host-network container's `listens:` | every single port in `ports:`, every published host port (on its bind address, if one is given) and every `listens:` single port listening — a range is never listening-checked; and — where a rule can be attributed at all — a `firewall=` line matching each `ports:` entry, a range by one rule covering it in the tool's range form | a single port declared in any of the three places but not listening; a published port listening on an address other than the declared bind address (a loopback publish answering on every address); a tagged rule with no matching `ports:` entry; `firewall=none-tagged` while ports are declared | `firewall=untaggable` (rules on this node cannot carry the slug: `tool: none` or `tag_mechanism: none`); `firewall=no-probe` (tool known, no verified read-back in § "Observed dialects"); `firewall=unreadable` (`sudo -n` denied, or the read-back command failed); or `ss` non-zero |
| Files | `filehash=` vs the sha256 list of `<slug>.files/<vol>/` in the fork at `HEAD`, and at `last_applied_commit` | node list equals `HEAD`'s | **doc-ahead**: node equals `last_applied_commit`'s but not `HEAD`'s (git changed, never synced — `/modify-use-case`); **node-ahead**: node differs from `last_applied_commit`'s (edited on the node — recovery `lost on rebuild`); `absent` or `empty` while git holds files | `unreadable` |
| Jobs | `jobfile=` vs the rendered script and units (compared as quadlets are, § "The quadlet comparison"); `jobtimer=`, `joblast=` vs `spec.jobs` and each `schedule` | every job's files identical up to trailing newlines, timer `enabled` + `active`, and its last line `rc=0` no older than its interval + 1 h | a file missing, extra or different; a timer not enabled or not active; last line `rc≠0` (failing job); last run older than its interval + 1 h (missed runs) | `joblast=…|never` on a job applied less than one interval ago |
| Shared mounts | `shared=` vs every `from:` mount | every owner directory `present` | any `absent` (the owner was removed or never applied) | — |
| Secrets | `envfile=` + `envkey=` vs `<slug>.secrets.yaml` | file present, `0600 doccap:doccap`, key names match `keys[].name` | `has_secrets: true` but file absent; wrong mode/owner; key names present that the manifest does not declare | env file unreadable |
| Status coherence | derived | `active` with every declared container running (every non-`oneshot` one), **and** — only if the doc declares a migration unit — that unit **not live** in the sense defined below (both fields known, neither reporting liveness); `planned` with nothing live | `active` with nothing running; `active` while a declared migration unit is still **live** (cut-over incomplete — container and host service both serving); `planned` with a running `container=` or a live migration unit (the `/onboard-node` hand-off) | containers `UNKNOWN`, or a declared migration unit **indeterminate** (either field `unknown` — systemctl unreachable or absent) |
| Static invariants | doc only | `containers`/`networks`/`volumes` ⇔ `spec.*` keys; every image pinned; every container passes the validation rules in `podman-quadlets` § "Render from spec" | list/`spec` mismatch, a `:latest` tag, or a failed validation rule (named with its container and key) | — |

Static invariants need no node access and are checked even when every probe
fails.

### Status coherence — the two conditions its cells turn on

**Each declared migration unit falls into exactly one of three classes.**
There is one `migration_unit=<unit>|<enabled>|<active>` line per `units:`
entry. Classify each line by these rules, **in this order** (the precedence
is what keeps the classes disjoint). The document's verdict is then the
strongest class among its units: live beats indeterminate, which beats not
live.

1. **live** — the enabled field reports `enabled` or `generated` (a quadlet's
   unit, which its `[Install]` section starts at boot), **or** the active
   field reports `active`. Positive evidence outranks a missing reading.
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
| `generated` | `inactive` | live | `DRIFT` |
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

**Legacy containers of another owner count as live units.** A
`legacy_container=<name>|running` line (any state but `absent`, `exited`,
`stopped` or `created`) is **live**. `unknown` is **indeterminate**. Absent or
stopped is **not live**. An `active` document with a live legacy container is
a cut-over that was rolled back, or never finished, while the new containers
also run: `DRIFT`, remedy the rollback recipe or a re-run of the cut-over,
never both.

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
writable layer relative to its image. Filter out every path at or under a
declared mount target (`spec.containers.<c>.mounts[].target`) — those are
persisted in a bind mount and are Tier 1's business. Then drop the lines that
say nothing a rebuild would lose:

- a `C` line whose path is a parent of another line's path, or of a declared
  mount target. A changed directory only says that something beneath it
  changed, and that change has its own line or is a mount;
- a `C /etc` with no line beneath it. Every running container reports it,
  because podman places its `hosts`, `hostname` and `resolv.conf` mount
  points there.

**What remains is configuration that a rebuild destroys.** This is the single
highest-value finding in the whole skill, it is universal, and it needs no
manifest. Report it as `DRIFT` / `node-ahead`, recovery `lost on rebuild`.

Digest the filtered listing and store it as `writable_layer/<container>` in the
manifest's `fingerprints`, so a subsequent audit can distinguish "still the same
known mutation" from "something new happened".

Note the failure mode: a stopped container cannot be diffed. Report `UNKNOWN`
rather than "clean" — a stopped container is never evidence of no mutation.
A `oneshot` container is the exception and is skipped: its job removes it
after every run, so nothing in its writable layer outlives the run.

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
  - a files-volume file edited on the node (`node-ahead`) → copy the node's
    file into `<slug>.files/<vol>/` at the same relative path; the next
    `/modify-use-case` run then has nothing to sync
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
