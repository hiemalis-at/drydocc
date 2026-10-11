---
name: rebuild-node
description: Recover a whole node after a disaster — triage what survived, bring the base back through /bootstrap-node when the box is lost or replaced, re-apply every active use-case in dependency order, and restore each use-case's data from its sealed backup wherever the disaster or the rebuild itself left that data lost. Delegates every change to the command that owns it; adds the triage, the recovery order, the data-loss decision, re-arming of unattended work, and one consolidated recovery report with a volume loss inventory. Use when a node is lost, replaced or badly broken.
compatibility: Runs inside a drydocc fork; needs a shell and file edits.
metadata:
  skillset: drydocc
  requires: conventions secrets node-context node-exec bootstrap-node provision-vm apply-use-case restore-use-case backup-use-case audit-use-case node-maintenance node-notify
---

# /rebuild-node

Answers "this node is gone, or half gone — what does it take to get every
service *and its data* back, in an order that is safe?"

Every step of a recovery already has an owner: `/bootstrap-node` for the base,
`/apply-use-case` (rebuild mode) for a use-case's footprint, `/restore-use-case`
for its data, `/backup-use-case`, `/node-maintenance` and `/node-notify` for the
unattended work that lived only on the box. This command owns none of that
mechanism. It owns the **process** — the same on every node — and leaves every
technology decision to the command and the declaration that already own it:
which firewall tool, which MAC relabel, which backup target, which consistency
mode, which hook. What it adds is what no single command can see: the whole
node's triage, the order, whether data was lost, and one report.

## Contract

- **Args:**
  - On the commissioning client (preferred): `<node> [--report-only]` — the
    node is the **positional first argument**.
  - On the node: `[--report-only]`. Only possible when the base is already
    ready; a lost node has no on-node surface.
  - `--report-only` runs Phases 0, 1 and 3 — triage and the plan — then stops.
    It writes nothing, changes nothing on the node, and journals nothing.
- **Surface:** either, resolved by `node-context`. Every on-node probe routes
  through `node-exec`. Base repair (Phase 2) runs only from the client; the
  delegated commands follow their own surface contracts.
- **Reads:** `nodes/<self>/node.yaml`; `nodes/<self>/use-cases/*.md` (never
  `_retired/`) and their `*.secrets.yaml` / `*.backup.yaml` / `*.audit.yaml`
  siblings; merged backup targets, maintenance posture and notification
  routing; `nodes/<self>/journal/*.md`; plaintext snapshot seals on backup
  targets, through the node's own credentials only.
- **Writes (in-repo):** its own journal entry — nothing else. Every other file
  is written by a delegated command under that command's contract.
- **On-node side effects:** none of its own; read-only probes only. Everything
  that changes the node is a delegated command.
- **On the admin laptop:** with explicit confirmation only, removal of a stale
  host-key entry for the node from `known_hosts`
  (`references/rebuild-recipes.md` § "Replacement host keys").
- **Journal scope:** `recovery` — one entry per run, sub-headed per phase and
  per use-case. Delegated commands journal their own entries under their own
  scopes; this command never journals on their behalf.

## Refusals

1. **No node profile** — `<self> has no node.yaml — there is no declaration to
   rebuild from. A new box is /bootstrap-node <self>; an existing, unmanaged
   one is /onboard-node <self>.`
2. **No active use-case** — `<self> declares no active use-case. Nothing to
   recover beyond the base — that is /bootstrap-node <self>.`
3. **Dependency cycle** among active use-cases' `depends_on` — name the cycle.
   `A recovery order cannot be derived; break the cycle in the declarations
   first.`
4. **Wrong box** — the box answering on `<self>`'s connection block identifies
   as a different node (`/etc/drydocc/node-name` present and not `<self>`):
   `The box answering as <tailscale_hostname> identifies as <other>. Refusing
   to recover <self> onto it.`
5. **Base not ready, on the node surface** — `<self>'s base is not ready
   (<failing check>). Base repair runs from the commissioning client:
   /rebuild-node <self>.`
6. **Missing capability** after base repair — the systemd service manager or
   rootless podman is still absent on the box. Name the failing check, never
   the system: `Refusing: quadlets require systemd;
   system_facts.service_manager is '<value>'.`

## Procedure

As each phase starts, set its number as `phase` in the resume pointer
(`continue-last-command` § "Resume pointer"), so an interrupted run shows where
it stopped.

### Phase 0 — Bind and read the declaration (repo only)

1. **Bind `<self>` + surface** via `node-context`. On-node probes route through
   `node-exec`; the snippets in `references/rebuild-recipes.md` are the
   `on_node` form.

2. **Build the recovery inventory** per `references/rebuild-recipes.md`
   § "The recovery inventory". Apply refusals 1 and 2. Every active document
   with a non-empty `spec.containers` is *recoverable*; an active capture-only
   document is *not declarative* and is reported, never applied; `planned`
   documents are listed and left alone — applying one is a new decision, not
   a recovery.

3. **Derive the recovery order** per `references/rebuild-recipes.md`
   § "Recovery order". Apply refusal 3. A use-case whose dependency is not
   active on this node is `blocked` from the start.

4. **Collect the known losses** the declaration already predicts, per
   `references/rebuild-recipes.md` § "Known losses" — volumes no backup
   covers, backups never proven restorable, and the most recent audit's
   `lost on rebuild` / `not rebuildable` findings. These are stated before
   anything is attempted, not discovered afterwards.

### Phase 1 — Triage the node (read-only)

5. **Reach the node** through `node-exec`. A changed host key under the
   node's name is handled per `references/rebuild-recipes.md`
   § "Replacement host keys" — never by accepting it silently. If the node
   cannot be reached, the base is `lost`; skip to step 7.

6. **Run the bundled triage probe** — one round trip, per
   `references/rebuild-recipes.md` § "The bundled triage probe". Apply
   refusal 4 on its `node_name=` line.

7. **Classify** the base, every recoverable use-case's footprint, and every
   declared volume's data per `references/rebuild-recipes.md`
   § "Triage classification". For each backed-up use-case, also evaluate
   `conventions/references/journal.md` § "Rebuilt, not yet restored": a
   use-case that owes a restore has lost data **whatever its live volumes
   contain**, because an application may have initialised fresh files on the
   empty volumes a rebuild created. `unknown` is never read as lost and never
   as intact.

### Phase 2 — Bring the base back (only when the base is not `ready`)

8. **On the node surface, apply refusal 5.** Otherwise:
   - **`lost`** — ask how the replacement comes to exist
     `[decision: rebuild.replacement]`: a running box with a
     temporary root password, a VM to provision, or *the original box will
     come back* (a network or power outage is not a disaster — stop here with
     nothing changed and nothing journaled).
   - **`unrecorded`** — the box answers, but it is not the box `node.yaml`
     describes.

   In both cases delegate **`/bootstrap-node <self>`** and answer its
   audit-&-repair prompt with yes; for a VM, bootstrap delegates to
   `/provision-vm` itself. **The replacement box's tooling is re-derived by
   that run, never inherited from the lost box's profile** — it may carry a
   different firewall tool, MAC framework, package manager or architecture,
   and every later apply renders from what bootstrap records for the box that
   is actually there.

9. **Re-triage** (steps 5–7). If the base is still not `ready`, journal
   `outcome: partial` with the failing check and stop; a re-run resumes.
   Apply refusal 6 when the failing check is a missing capability.

10. **Check the replacement platform** when `node.yaml:arch` changed during
    this run, per `references/rebuild-recipes.md` § "Replacement platform".
    A use-case with an image that does not offer the new platform is
    `blocked`, naming the image; changing a pin is a declaration change the
    admin makes, not something this command decides.

### Phase 3 — Plan and confirm

11. **Derive each use-case's plan**, in recovery order, per
    `references/rebuild-recipes.md` § "The recovery plan" — a footprint action
    (`rebuild` / `none` / `drift` / `blocked` / `not declarative`) and a data
    action (`restore` / `none` / `offer` / `unrecoverable`).

12. **Print the plan** in that section's format: order, per-use-case evidence
    (the same evidence `/apply-use-case` step 3c and `/restore-use-case`
    step 5 would show), downtime, KeePass entries that will be resolved (names
    only), backup-target credentials that will need re-authorizing, the known
    losses from step 4, and what will be left for the admin to prune. Stop here
    under `--report-only`.

13. **Confirm** `[decision: rebuild.confirm-plan]`. The admin accepts all, a subset, or aborts. Abort ends the run
    with nothing changed and nothing journaled. For every accepted use-case,
    the confirmation is **carried** into its delegates; each delegate honours
    it only if its own probe reproduces the evidence shown here.

### Phase 4 — Recover, one use-case at a time, in order

14. For each accepted use-case, in recovery order — **finish one before
    starting the next**, so a dependent never comes up against its
    dependency's empty data:
    1. **Dependencies.** If any `depends_on` slug did not end `recovered` or
       `intact` in this run, mark this one `blocked` and move on.
    2. **Footprint.** On `rebuild`, delegate `/apply-use-case <self> <slug>`
       (on the node: `/apply-use-case <slug>`). Any outcome other than
       `success` marks the use-case `failed`.
    3. **Data.** On `restore` — or `offer` accepted in step 13 — delegate a
       **live** `/restore-use-case <self> <slug>`, with `--snapshot <id>` when
       the admin chose one. The restore resolves the snapshot per
       `backup-recipes.md` § "Choosing a snapshot to restore" and
       re-materializes the target's credentials on a rebuilt box itself. A
       verify failure halts it with the use-case's units down and the
       pre-restore data untouched: mark `restore-failed`.
    4. **No backup.** On `unrecoverable`, record every declared volume in the
       loss inventory — it is running, empty, and there is nothing to restore
       it from.
    5. **Outcome.** `recovered` (footprint rebuilt and every lost volume
       restored, or no volume to restore), `intact`, `failed`,
       `restore-failed`, `blocked`, `drift`, or `not declarative`.

    A failure on one use-case never aborts an independent one. When a
    delegate pauses on a secret, mark the resume pointer per `continue-last-command`
    § "Resume pointer" (`command: /rebuild-node`, `phase: 4`, `waiting_on`),
    journal `outcome: partial`, and stop; the re-run re-triages and skips
    what is already recovered on evidence, not on the pointer.

### Phase 5 — Re-arm unattended work

15. **Derive what ran unattended** from the repo and compare it with what the
    triage probe found, per `references/rebuild-recipes.md` § "Unattended work
    to re-arm". Offer each missing piece as its owning command, in this order:
    `/backup-use-case`, `/node-maintenance --setup`, `/node-notify --setup`
    (last, so it arms alarms for the timers now present). Never offer a backup
    re-arm for a use-case whose data ended `lost` or `restore-failed` — sealing
    empty data would push the good snapshots out through retention.

### Phase 6 — Rehearse, journal, report

16. **Rehearse.** Run `/audit-use-case <self> --report-only` and fold its
    verdicts into the report. A `not rebuildable` finding now is a finding
    about the declarations, not about the recovery; emit the full audit (which
    refreshes fingerprints) as a next command.

17. **Journal** per `conventions/references/journal.md`, scope `recovery`.
    `## What was done` carries `### base: <state>`, one `### <slug>: <outcome>`
    per use-case, and `### automation: …`; each names the delegated journal
    entries it caused, by filename. `## Notes / drift detected` carries the
    triage evidence and the volume loss inventory. `outcome`: `success` when
    every recoverable use-case ended `recovered` or `intact` and the loss
    inventory holds only volumes the plan declared unrecoverable; `partial`
    otherwise; `failed` when the base could not be brought back. **Never**
    journal a secret value, a root or VM password, or the raw machine id.

18. **Commit + push** the journal entry. After an `on_client` run, remind the
    admin to `git pull` on the node's working copy.

19. **Print the recovery report** per `references/rebuild-recipes.md`
    § "The recovery report" — the per-use-case outcome table, the **volume
    loss inventory** stated outright, the hand-offs, and what only the admin
    may clean up.

## Example output

```
Recovery of vps-fra-01 — base: replaced (bootstrapped 2026-09-13T09:12Z)

| # | use-case  | footprint | data                                        | outcome   |
|---|-----------|-----------|---------------------------------------------|-----------|
| 1 | postgres  | rebuilt   | restored 2026-09-12T033000Z, seal verified  | recovered |
| 2 | nextcloud | rebuilt   | restored 2026-09-12T033000Z, seal verified  | recovered |
| 3 | blog      | rebuilt   | no backup — volumes empty                    | recovered |
| 4 | matrix    | —         | —                                            | blocked: depends on synapse-db (restore-failed) |

Volume loss inventory — these hold no recovered data:
  blog/blog-content        no backup manifest
  synapse-db/db-data       restore failed seal verification; pre-restore data kept

Re-armed: backup timers postgres, nextcloud; maintenance; notification alarms.
Not re-armed: synapse-db backup (data not recovered).

Hand-offs:
  /restore-use-case vps-fra-01 synapse-db --snapshot 2026-09-11T033000Z
  /backup-use-case vps-fra-01 blog
  /audit-use-case vps-fra-01

Yours to clean up: tailnet device "vps-fra-01" (offline, replaced);
  NAS key doccap-backup-at-nas-primary from the lost box; 2 pre-restore dirs.
```

## What this command does NOT do

- **Re-implement anything it delegates.** Base repair is `/bootstrap-node`,
  a use-case's footprint is `/apply-use-case`, its data is
  `/restore-use-case`, and unattended work is `/backup-use-case`,
  `/node-maintenance` and `/node-notify`.
- **Reconcile partial absence.** A use-case whose footprint partly survived
  is drift; it is handed to `/audit-use-case` then `/modify-use-case`, never
  re-materialized.
- **Apply a `planned` use-case, or a capture-only one.** Neither ever ran
  from its declaration, so neither can be recovered from it.
- **Restore over live data on its own judgement.** A `suspect` volume is
  offered, never restored by default — live data may be newer than every
  snapshot.
- **Recover what was never kept.** Secret values come from KeePass; data with
  no backup is gone and reported as such; an S3 target without its encryption
  passphrase is unreadable.
- **Recover a node under a different name.** It rebuilds `<self>` as `<self>`.
  Restoring one use-case from another node's snapshots is
  `/restore-use-case --source-node`.
- **Make restores point-in-time consistent across use-cases.** Each use-case
  restores its own snapshot.
- **Delete anything it leaves behind** — a stale tailnet device, an orphaned
  NAS key, `pre-restore` directories, the dead VM. They are listed for the
  admin.
- **Touch sibling nodes.**

## See also

- `references/rebuild-recipes.md` — the inventory, the order, the triage probe
  and classification, the plan and report formats.
- `apply-use-case` — rebuild mode, the per-use-case recovery this command
  sequences.
- `restore-use-case` — the data half, and its rebuilt-node credential case.
