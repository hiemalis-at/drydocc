# Node Maintenance Manifest Convention

## Path

The maintenance posture is declared in two layers, merged at read time —
the same shape `backup-targets.yaml` uses, and for the same reason:

- **`nodes/maintenance.yaml`** — the fork-wide posture. This is the standard
  every node in the infrastructure is held to. Lives under `nodes/` (not the
  repo root) so the generic layer stays framework-only:
  a fork's own data lives under `nodes/`.
- **`nodes/<node>/maintenance.yaml`** — this node's deviations. Optional; a
  node with no file inherits the fork-wide posture exactly.

Both are authored by `/node-maintenance`'s first-run walkthrough and tuned by
the admin afterwards. Neither holds secret values.

The split is the point. A fork exists so its nodes are held to one bar; a node
exists because it is not identical to its siblings. The fork file carries the
bar, the node file carries the exception, and because an exception is a
*file-level diff* rather than an improvised omission, `/node-maintenance`
reports every deviation on every run. A posture item turned `off` on one box is
a decision somebody can see, not a silence.

## Merge rule

1. Read the fork file. If it doesn't exist, every catalog item defaults to
   `mode: report` — the safe posture, which assesses everything and changes
   nothing unattended.
2. Read the node file if present.
3. For each `posture[]` entry in the node file, match by `id` against the fork
   list:
   - same `id` → the node entry's fields **override** the fork entry's, field
     by field.
   - new `id` → added, provided the `id` is in the catalog (see
     `node-maintenance/references/maintenance-recipes.md` § "The posture
     catalog"). An unknown `id` is a refusal, naming the id and the file it
     came from — it is almost always a typo, and silently ignoring it would
     drop a maintenance outcome the admin believes is covered.
   - a catalog `id` absent from **both** files → `mode: report`.
4. After merging, every `id` with `mode: auto` must also appear in the **node**
   file's `verified_auto` list. If one does not, refuse and name it: an item
   may only run unattended on a node where it has already been watched to
   succeed. See "The promotion gate" below.
5. Record which items came from the node layer. These are the run's reported
   deviations.

`/node-maintenance` reads this merged view once per run.

## Schema

```yaml
# nodes/maintenance.yaml — the fork-wide layer
schema: 1

# ─── authored by /node-maintenance (first run) and admin tuning; safe to hand-edit ───

schedule: "04:30"             # local time, or a raw OnCalendar= expression
posture:
  - id: container-images-current
    mode: report              # report | auto | off
  - id: os-packages-current
    mode: report
  - id: logs-bounded
    mode: report
  - id: node-facts-fresh
    mode: report
  - id: tls-material-valid
    mode: report
  - id: intrusion-prevention-reviewed
    mode: report
  - id: tailnet-healthy
    mode: report
  - id: data-recoverable
    mode: report
  - id: credentials-within-rotation-window
    mode: report
  - id: resources-within-baseline
    mode: report
  - id: logs-within-baseline
    mode: report
```

```yaml
# nodes/<node>/maintenance.yaml — this node's layer
node: vps-fra-01
schema: 1

# ─── authored by /node-maintenance (first run) and admin tuning; safe to hand-edit ───

schedule: "05:15"             # overrides the fork-wide time on this node only
posture:
  - id: logs-bounded
    mode: auto                # promoted after one watched interactive success
  - id: tls-material-valid
    mode: off                 # this node terminates no TLS; nothing to check
    note: "edge proxy terminates TLS; this box is upstream-only"

# ─── written by /node-maintenance; do not hand-edit ───

automation_rendered_commit: null
last_maintenance: null
last_maintenance_outcome: null
verified_auto: [logs-bounded]
```

## Field descriptions

### Authored half

- **`node`** — Must equal the containing directory name and `node.yaml:name`.
  Present only in the node layer; the fork-wide file has no `node` key.
- **`schema`** — Integer schema version. Currently always `1`.
- **`schedule`** — When the unattended assessment runs. Either a plain `HH:MM`
  (24h, node-local, rendered into a daily `OnCalendar=*-*-* HH:MM:00`) or a
  literal `OnCalendar=` expression for anything less common. The second form
  is passed through verbatim — `OnCalendar=` is a systemd feature, and systemd
  is the framework's one hard capability requirement, so this is not a
  dialect concern.
- **`posture[]`** — The per-item modes. Order is irrelevant; `id` is the key.
- **`posture[].id`** — A catalog id. The catalog is closed and is defined in
  `node-maintenance/references/maintenance-recipes.md` § "The posture
  catalog" — not here, and not extensible per fork. A new maintenance outcome
  is a change to the skill, because the skill is what knows how to derive and
  verify it.
- **`posture[].mode`** — One of:
  - `report` (default) — assess it, report findings, and offer to act only
    with an admin present. Never acts unattended.
  - `auto` — the unattended timer may execute this item's derived work on
    this node. Gated on `verified_auto`.
  - `off` — skip entirely. The item is still listed in the report as
    deliberately disabled, with its `note` if one is set.
- **`posture[].note`** — Free text, human-read only. Strongly encouraged
  alongside `off`: a disabled maintenance outcome with no stated reason is
  indistinguishable from an oversight six months later.

### Skill-written half

Present only in the node layer. `/node-maintenance` owns these; hand-editing
them makes the node lie about itself.

- **`automation_rendered_commit`** — Short SHA of the manifest state the
  on-node service and timer were last rendered from. When the merged manifest
  no longer matches this, the live automation is stale and the next run
  re-renders it. Same mechanism as the backup manifest's field of the same
  name.
- **`last_maintenance`** — ISO-8601 UTC timestamp of the most recent
  *journaled* run (interactive, or an unattended run caught up from the run
  log). `null` until the first run completes.
- **`last_maintenance_outcome`** — `success`, `partial`, or `failed`, matching
  the journal entry's `outcome`. `partial` is the normal result when one
  posture item failed and the rest completed.
- **`verified_auto`** — The ids proven on this node: each has completed
  successfully at least once in an **interactive** run here. Append-only in
  practice; `/node-maintenance` adds an id after a watched success and never
  removes one on its own. Removing an id by hand is how an admin revokes
  unattended permission.

## The promotion gate

`mode: auto` is only legal for an id already in `verified_auto`, and only the
node layer can contain `verified_auto`. The fork-wide file therefore **cannot**
turn anything on unattended across the fleet — it can express the intent by
setting `auto`, but each node still refuses until that specific box has watched
the item succeed once.

This is deliberate. A maintenance outcome derives different concrete work on
every node; "it worked on the other box" is not evidence about this one. The
gate converts unattended execution from a policy decision into an observed
fact.

The consequence worth stating plainly: a fresh node runs assess-only until an
admin has sat through an interactive run with it, no matter what the fork-wide
posture says.

## Lifecycle

| Event | Effect |
|---|---|
| First `/node-maintenance` on any node in the fork | First-run walkthrough authors `nodes/maintenance.yaml` at the default posture (everything `report`) |
| First `/node-maintenance` on this node | Authors `nodes/<node>/maintenance.yaml` with an empty `posture[]` and the skill-written half nulled |
| Each interactive run | Updates `last_maintenance*`; appends to `verified_auto` on a watched success; re-renders automation and updates `automation_rendered_commit` if the merged manifest changed |
| Each unattended run | Writes to the on-node run log only. The manifest is updated when the next interactive run catches that log up — the node's working copy is not a place unattended jobs commit from |
| `/node-maintenance --setup` | Discards and re-renders the on-node automation, then proceeds as if `automation_rendered_commit` were null. Never edits the authored half |

## References

- Posture catalog and the work each item derives:
  `skills/node-maintenance/references/maintenance-recipes.md`.
- The two-layer merge this file copies:
  `conventions/references/backup-targets.md`.
- Journal entries for maintenance runs: `conventions/references/journal.md`
  (`scope: maintenance`).
