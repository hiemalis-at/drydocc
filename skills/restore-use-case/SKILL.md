---
name: restore-use-case
description: Restore a use-case's volumes from a sealed backup snapshot — into scratch space to verify the backup is good (`--test`, no service impact), or live onto the node (moves current volumes aside, never deleting them). The mandatory other half of /backup-use-case's contract — a backup nobody has restored is a hypothesis, not a backup. Use to prove a backup or recover lost data.
compatibility: Runs inside a drydocc fork; needs a shell and file edits.
metadata:
  skillset: drydocc
  requires: conventions secrets node-context node-exec backup-use-case
---

# /restore-use-case

## Contract

- **Args:**
  - On the node: `<slug> [--snapshot <id>] [--source-node <node>] [--test]`.
  - On the commissioning client: `<node> <slug> [--snapshot <id>]
    [--source-node <node>] [--test]` — the target node is the **positional
    first argument** (required in client mode; `node-context` refuses
    without it).
  - `--snapshot <id>` — restore a specific sealed snapshot instead of the
    newest. `--source-node <node>` — restore from a **different** node's
    snapshots on the same target (disaster recovery onto replacement
    hardware with a new node name; defaults to `<self>`).
  - `--test` — restore into scratch space, verify, and discard. No unit is
    stopped, no volume is touched. This is the mode `/backup-use-case`
    setup delegates to and the mode to run on a schedule of its own choosing
    to keep `last_restore_test` fresh.
- **Surface:** runs from either surface, resolved by
  `node-context`; every on-node command routes through `node-exec` (the
  restore transport lingers).
- **Reads:** `nodes/<self>/use-cases/<slug>.{md,backup.yaml,audit.yaml}`;
  the merged backup targets; sealed snapshots on the target.
- **Writes (in-repo):** the journal entry; `<slug>.backup.yaml`'s
  `last_restore_test*` fields (`--test` runs only).
- **On-node side effects (live restore only):** stops the use-case's units;
  moves each current volume aside to `<vol>.pre-restore-<snapshot-id>`
  (a rename — nothing is deleted); extracts the snapshot into a freshly
  created volume; runs `post_restore` hooks; restarts units.
- **Journal scope:** `use-case/<slug>`.

## Refusals

1. **No `<slug>.backup.yaml`** — `<slug> has no backup manifest. Run
   /backup-use-case <slug> first.`
2. **Live restore, but the use-case was never applied or is retired** —
   `<slug> is <status>. A live restore needs an applied use-case to restore
   onto — run /apply-use-case <slug> first, or use --test to verify the
   snapshot in isolation.` (`--test` never hits this refusal — it needs no
   live use-case at all.)
3. **No sealed snapshot exists** on the resolved target for
   `<node>/<slug>` (or for `--snapshot <id>` specifically) — name what was
   searched and where.

## Procedure

1. **Bind `<self>` + surface** via `node-context`. All on-node commands route
   through `node-exec` — the transport step (§ "Restoring a snapshot" in
   `backup-use-case/references/backup-recipes.md`) is always
   `NODE_RUN_LINGER` (`dd-<slug>-restore`) when `on_client`.

2. **Read the manifest and resolve the snapshot.** List sealed snapshots for
   `<source-node or self>/<slug>` on `<slug>.backup.yaml:target` (merged
   targets per `conventions/references/backup-targets.md`); pick the one named
   by `--snapshot`, or otherwise apply `backup-recipes.md` § "Choosing a snapshot to restore"
   — the newest sealed snapshot, unless it captured the loss it
   is meant to undo. Apply refusals 1 and 3.

3. **Compare the seal against the current declaration.** Read the chosen
   snapshot's `manifest.json` (`backup-recipes.md` § "Snapshot layout and
   seal"). Note, but do not refuse on:
   - a declared volume with no entry in the seal (never backed up, or backed
     up under an older manifest that excluded it);
   - a pinned image tag in `spec.containers.*.image` that differs from the
     seal's `images.<container>` — call this out explicitly (a database
     engine's on-disk format changing across a major version is the classic
     reason this matters) and let the admin confirm before proceeding
     `[decision: restore.image-mismatch]`.

4. **`--test` path** (no live impact; skip straight to step 8 when done):
   1. Check free space in scratch (`df` on the scratch filesystem against
      the seal's total `bytes`).
   2. Restore every sealed volume into scratch space (`backup-recipes.md` §
      "Restoring a snapshot", `<extract-target>` = a `mktemp -d` scratch
      dir).
   3. Verify each against the seal (`backup-recipes.md` § "Verifying a
      restore against its seal").
   4. Delete the scratch copy.
   5. Record `last_restore_test` (now) and `last_restore_test_snapshot`
      (the snapshot id) in `<slug>.backup.yaml`.
   6. Report pass/fail per volume and stop — no unit was touched, nothing
      else to do.

5. **Live restore — confirm the plan** (skipped under `--test`). Apply
   refusal 2. Print and require an explicit yes `[decision: restore.confirm-live]`:
   - every unit that will stop, and for how long this typically takes;
   - every current volume that will be renamed aside to
     `<vol>.pre-restore-<snapshot-id>` (nothing deleted);
   - the snapshot id and its `manifest.json` totals;
   - any image-tag mismatch noted in step 3.

   **Carried confirmation.** When `/rebuild-node` invokes this command for a
   use-case the admin accepted in its recovery plan, that acceptance is the
   explicit yes — **provided** the units, volumes and snapshot resolved here are
   the ones the plan showed. An image-tag mismatch, a collapsed-snapshot
   proposal, or anything else the plan did not show is always asked here.

6. **Stop, move aside, extract:**
   1. Pause the timer and take the shared lock (`backup-recipes.md` §
      "Rendering the automation" — same `flock` the rendered backup script
      uses, so a backup can never start mid-restore):
      `systemctl --user stop drydocc-backup-<slug>.timer`.
   2. Stop units in reverse `depends_on` order.
   3. For each declared volume: `mv
      /var/lib/drydocc/volumes/<slug>/<vol>
      /var/lib/drydocc/volumes/<slug>/<vol>.pre-restore-<snapshot-id>`.
   4. Recreate the volume directory with its declared owner/mode
      (`spec.volumes.<vol>` defaults `doccap:doccap`, `0750` — the same
      `sudo install -d` idiom `/apply-use-case` step 7 uses).
   5. Extract and verify each volume (`backup-recipes.md` §§ "Restoring a
      snapshot", "Verifying a restore against its seal"). On a verify
      failure for any volume, **stop here** — do not start units on top of a
      restore that doesn't match its seal; the pre-restore volume is still
      sitting right next to it, untouched, for the admin to fall back to
      manually.

7. **Bring back up:**
   1. Start units in topological `depends_on` order.
   2. Run every `hooks.post_restore[]` entry (`podman exec`).
   3. Verify: `podman ps --filter name=<slug>- --format json` — every
      declared container `Up`; if `<slug>.audit.yaml` exists, also run its
      `expectations[]` probes (same read-only probes `/audit-use-case` §
      "Tier 1" runs).
   4. Resume the timer: `systemctl --user start drydocc-backup-<slug>.timer`.

8. **Update the doc/manifest.** Live restore: no frontmatter field changes
   (status stays `active`; this is not an apply). `--test`: the
   `last_restore_test*` fields from step 4.5.

9. **Journal.** `## What was done` — the snapshot id, per-volume
   verification result, every hook run, and (live restore) the prune list of
   `<vol>.pre-restore-<snapshot-id>` directories with the one-line command to
   remove each — removing them is the admin's decision, exactly like
   `/apply-use-case`'s adoption prune list. `outcome: success` if every
   declared container verified `Up` (live) or every volume verified against
   its seal (`--test`); `partial` otherwise. **Never** journal a secret
   value or the encryption passphrase.

10. **Commit + push** the journal entry (and `<slug>.backup.yaml` if
    `--test` updated it). After an `on_client` run, remind the admin to
    `git pull` on the node's working copy.

## Rollback

Until the admin removes them, every `<vol>.pre-restore-<snapshot-id>`
directory is the pre-restore state, untouched:

```bash
systemctl --user stop <slug>-<container>.service   # each container
rm -rf /var/lib/drydocc/volumes/<slug>/<vol>
mv /var/lib/drydocc/volumes/<slug>/<vol>.pre-restore-<snapshot-id> \
   /var/lib/drydocc/volumes/<slug>/<vol>
systemctl --user start <slug>-<container>.service
```

A restore that failed verification (step 6.5) never reached step 7, so units
are still down and nothing needs rolling back — just re-run once the cause
is fixed.

## Rebuilt-node case

On a node whose `/backup-use-case`-managed SSH key or S3 env file was lost
(a rebuild wipes `/etc/drydocc/secrets/` and `~/.ssh/`), a restore's
credential resolution runs the same materialization
`/backup-use-case` setup step 3 does — regenerate the rsync key and have the
admin re-authorize it on the NAS (the old key is orphaned there; removing it
is a manual NAS-side cleanup), or re-resolve the S3 env file via `secrets`.
This is why `/apply-use-case` rebuild mode's closing summary names
`/restore-use-case` as the very next command.

## What this command does NOT do

- **Decide which snapshot is "good."** It restores the newest sealed one, or
  the one named, and verifies it against its own seal — it does not
  second-guess older snapshots.
- **Reconcile a partially-successful restore.** A verify failure halts with
  the pre-restore data intact; recovering further is the admin's call.
- **Modify `<slug>.backup.yaml`'s authored half.** Only the skill-written
  restore-test fields change, and only under `--test`.

## See also

- `backup-use-case/references/backup-recipes.md` — every concrete command
  this skill's steps compose (§§ "Restoring a snapshot", "Verifying a
  restore against its seal").
