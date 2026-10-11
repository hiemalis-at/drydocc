# Runbook bundle — a node prototyped in the lab, ready for a real box

A **runbook bundle** is how a node built in a lab prototype leaves the lab: the
node's declarations, made portable to a different box, plus the runbook that
builds that box. `scripts/lab/lab.py extract` writes it, `verify-runbook`
replays it on a fresh lab box and stamps the outcome, and `promote` lands it in
the admin's fork as an ordinary node directory. The `lab` skill is the
procedure; this file is the contract.

## Layout

A bundle is exactly the directory it becomes in a fork:

```
nodes/<target>/
  node.yaml             # the prototype's profile, box-bound fields stripped
  use-cases/            # every use-case document, as never applied
    <slug>.md
    <slug>.secrets.yaml
    <slug>.audit.yaml
    <slug>.backup.yaml
  maintenance.yaml      # node-level policy layers, when the prototype had them
  notify.yaml
  journal/.gitkeep      # empty: the prototype's journal stays in the lab
  runbook.yaml          # the machine-readable runbook
  RUNBOOK.md            # generated from runbook.yaml — never edited by hand
```

After promotion, `runbook.yaml` and `RUNBOOK.md` stay in the node's directory
as a **record** of how the node was first built. Like a journal entry, they are
never updated afterwards: what the node is now is its profile, its use-case
documents and its journal.

## What extraction carries, and what it leaves

- **`node.yaml`** — the box-bound fields are treated exactly as
  `node-profile.md` § "Box-bound fields" says; the result is a profile
  `/bootstrap-node` *resumes* (its directory exists, `last_bootstrapped` is
  `null`) rather than scaffolds.
- **Use-case documents** — `status: planned`, `last_applied` and
  `last_applied_commit` `null` (`use-case-doc.md`): on the new box nothing has
  been applied. Everything else, including the body, is carried.
- **Secrets manifests** — a node-scoped `keepass_entry`
  (`drydocc/<prototype>/<slug>`) becomes `drydocc/<target>/<slug>`; an
  infra-global one is unchanged. **No value** is ever exported: lab secrets are
  generated per run and die with it.
- **Audit and backup manifests** — the authored half only. The skill-written
  halves (`audit-manifest.md`, including `telemetry`'s learned baseline;
  `backup-manifest.md`) are reset to their never-run values: they describe the
  lab box.
- **Left in the lab** — the journal, the prototype's data (volume contents),
  node-level backup targets (they point at lab machines), and every fork-wide
  file under `nodes/`. The runbook names each of these, so nothing is dropped
  silently.

## `runbook.yaml`

```yaml
schema: 1
target: pi-home-01
platform: {distro: <os-release ID>, version: "<VERSION_ID>", arch: aarch64}
source:                       # where the bundle came from
  prototype: home-proto       # lab run
  node: web                   # node name in the prototype
  layer: 9f2c…                # the layer the node last stood on
  fork_commit: 1a2b3c4d5e6f   # the prototype fork's commit at extraction
  upstream_ref: main@6b89a6d  # the framework the prototype ran
  extracted: 2026-10-10T09:00:00+00:00
prerequisites:
  hardware: one aarch64 machine with a fresh … install, reachable over SSH as root …
  secrets:                    # KeePass entries to create before the steps
    - {entry: drydocc/tailscale, keys: [DOCCAP_TS_KEY], for: node, optional: false}
    - {entry: drydocc/pi-home-01/dbadmin, keys: [POSTGRES_PASSWORD], for: dbadmin, optional: false}
  backup_targets: [nas-primary]   # names the backup manifests use; the fork must declare them
steps:                        # in order
  - {id: bootstrap, kind: skill, run: '/bootstrap-node pi-home-01 "web"', note: …}
  - {id: apply-dbadmin, kind: skill, run: /apply-use-case pi-home-01 dbadmin}
  - {id: backup-dbadmin, kind: skill, run: /backup-use-case pi-home-01 dbadmin,
     needs: ["backup-target:nas-primary"]}
checks:                       # what must hold on the built box
  - {name: dbadmin is active, assert: use_case, slug: dbadmin, status: active}
  - {name: container db runs, assert: command, run: "…", stdout_contains: "true"}
deltas: [ … ]                 # lab vs. hardware, one sentence each
data:                         # volumes whose contents stayed in the lab
  - {use_case: dbadmin, volume: pgdata}
verification:                 # null until verify-runbook replays the bundle
  status: replay-verified     # replay-verified | fail | blocked
  framework_commit: 6b89a6d1c0de
  platform: <distro>/<version>/aarch64
  run: verify-3fa2c1
  at: 2026-10-10T10:30:00+00:00
  skipped: ["backup-dbadmin (needs backup-target:nas-primary)"]
```

- **`steps`** — `/bootstrap-node`, then `/apply-use-case` for every use-case
  the prototype applied, in the `rebuild-node` skill's recovery order
  (`references/rebuild-recipes.md` § "Recovery order") — then backups,
  notification and maintenance, where the prototype had them. Use-cases that
  order cannot place (blocked, capture-only, or only planned) get a delta
  instead of a step. Every command is in its client form: the node is the
  first argument.
- **`needs`** — something outside the box a step depends on: a backup target,
  a notification channel. A lab replay skips such steps and records them under
  `verification.skipped`; on real hardware they are prerequisites.
- **`checks`** — derived from the use-case declarations, and **each one is
  run on the prototype at extraction**: a check that does not hold there is
  left out and named in `deltas`. The check vocabulary is the lab scenario's
  (`lab-scenario.md` § "Assertions"); commands run on the node as `doccap`.
- **`verification`** — written only by `verify-runbook`, from the replay's
  verdict: `replay-verified` on a pass, the verdict otherwise. A replay that
  could not run at all (an infrastructure error) leaves it unchanged.
  `framework_commit` is the framework the replay ran: a bundle is verified
  *for that framework*, on that platform, and nothing more.

## Promotion

`promote` copies the bundle to `nodes/<target>/` of the admin's fork, on a new
branch `promote/<target>`, and journals the promotion there. It refuses when
`nodes/<target>/` already exists at the branch point or the branch exists,
and — unless explicitly overridden — when the bundle is not `replay-verified`.
It never touches the admin's checked-out working tree. This is one of the
node-scoping exceptions `AGENTS.md` lists: it writes a node directory that no
box is bound to yet.
