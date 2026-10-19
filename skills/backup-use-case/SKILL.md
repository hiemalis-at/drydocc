---
name: backup-use-case
description: Propose, tune, and activate a use-case's volume backup automation — an on-node timer that snapshots declared volumes to an rsync or S3-compatible target. First call proposes a strategy for the admin to tune; later calls (re)activate it. `--setup` discards the existing automation and starts over. Snapshots already on the target are never deleted by this command. Use when a use-case's data needs protecting or its backup changes.
compatibility: Runs inside a drydocc fork; needs a shell and file edits.
metadata:
  skillset: drydocc
  requires: conventions secrets node-context node-exec restore-use-case
---

# /backup-use-case

Answers "how should `<slug>`'s data survive a lost node" as a **proposal the
admin tunes**, not a one-size policy — a static web volume, a database, and a
multi-container stack all need different treatment, and this command only
knows generic signals (mount targets, `ro` flags, container topology), never
the application behind them.

## Contract

- **Args:**
  - On the node: `[<slug>] [--setup]` — omitted `<slug>` activates (or lists
    the missing manifest for) every `status: active` use-case that has one.
  - On the commissioning client: `<node> [<slug>] [--setup]` — the target
    node is the **positional first argument** (required in client mode;
    `node-context` refuses without it).
- **Surface:** runs from either surface, resolved by
  `node-context`; every on-node command below routes through `node-exec`
  (long steps — the trial backup, any restore test — linger).
- **Reads:** `nodes/backup-targets.yaml` + `nodes/<self>/backup-targets.yaml`
  (merged, `conventions/references/backup-targets.md`);
  `nodes/<self>/use-cases/<slug>.{md,backup.yaml}`; the node's journal for
  the rebuild guard (see Refusals).
- **Writes (in-repo):** `nodes/backup-targets.yaml` and/or
  `nodes/<self>/backup-targets.yaml` (first-run walkthrough only);
  `nodes/<self>/use-cases/<slug>.backup.yaml`; the journal entry.
- **On-node side effects:** a generated SSH keypair (rsync target, first use
  only); a target credential env file at
  `/etc/drydocc/secrets/backup-<target>.env`; a rendered script at
  `~/.config/drydocc/backup-<slug>.sh`; a systemd user
  service+timer at `~/.config/systemd/user/drydocc-backup-<slug>.{service,timer}`,
  enabled once setup's trial + restore test pass.
- **Journal scope:** `use-case/<slug>`.

## Refusals

1. **No use-case doc for `<slug>`** — `No use-case <slug> on <self>. Author
   one with /create-use-case.`
2. **`status: retired`** — `<slug> is retired. Author a fresh use-case
   instead.`
3. **Never applied** (`last_applied` null) — `<slug> has never been applied.
   Run /apply-use-case <slug> first — there is nothing running to back up
   yet.`
4. **No declared volumes** — `<slug> declares no volumes. Nothing for
   /backup-use-case to do.`
5. **Rebuild without a restore since** (checked for every run, setup or
   activate — see step 2b): confirmable, not a hard refusal. Proceeding
   without confirming risks sealing an empty post-rebuild volume as a
   legitimate snapshot and pushing good snapshots out through retention.

## Procedure

1. **Bind `<self>` + surface** via `node-context`. All on-node commands below
   route through `node-exec` — locally when `on_node`, SSH-wrapped when
   `on_client` (long steps `NODE_RUN_LINGER` as `dd-<slug>-backup`).

2. **Resolve targets.** Read and merge `nodes/backup-targets.yaml` +
   `nodes/<self>/backup-targets.yaml` per
   `conventions/references/backup-targets.md`. If **neither** file exists,
   run its "First-run walkthrough" now, before anything else — this applies
   regardless of `<slug>`, since no backup can be declared without at least
   one target. Commit the resulting file(s) and their journal entry, then
   continue.

2a. **Resolve the use-case set.**
   - `<slug>` given → apply refusals 1–4, then go to step 3.
   - `<slug>` omitted → glob `nodes/<self>/use-cases/*.md` for
     `status: active`. Partition into "has `<slug>.backup.yaml`" (run step
     3's **activate** branch on each) and "has none" (report as a list —
     *"No backup manifest yet: `<slug1>`, `<slug2>`. Run `/backup-use-case
     <slug>` on each to propose a strategy."* — never auto-run setup in bulk;
     it needs an interactive conversation per use-case). A failure on one
     slug never aborts the others.

2b. **Rebuild guard** (per resolved slug). Evaluate
   `conventions/references/journal.md` § "Rebuilt, not yet restored" for
   `<slug>` — a `--test` restore, or a live restore that failed, does not
   count. If the use-case owes a restore:

   > `<slug>`'s last apply was a rebuild on `<ts>` — its volumes came up
   > empty and no restore has run since. Sealing a backup now risks
   > snapshotting empty data and pushing your last good snapshot out through
   > retention. Run `/restore-use-case <slug>` first, or confirm you intend
   > to start fresh (e.g. this is genuinely a new deployment)?
   `[decision: backup.rebuild-guard]`

   On decline, stop with no write. This gate applies to setup and activate
   alike.

3. **Setup** — entered when `<slug>.backup.yaml` doesn't exist, or `--setup`
   was passed (which first purges any existing automation per
   `backup-recipes.md` § "Rendering the automation", then proceeds as if the
   manifest didn't exist — existing sealed snapshots on the target are never
   touched):

   1. **Analyse the declaration** using only generic signals: for each
      declared volume (never a files volume, whose content is in git, and
      never a `from:` mount, which its owner backs up), which container(s)
      mount it and at what target, `ro`
      vs read-write, whether the mount target matches a common data-directory
      shape, `depends_on` topology, and any `config_surfaces` from
      `<slug>.audit.yaml` if present. Classify against
      `conventions/references/backup-manifest.md` § "Common shapes" and
      propose `consistency`, `volumes[]` (with excludes drawn from
      `<slug>.audit.yaml:ignore[]` when present), an empty `hooks:` block,
      a default `schedule` (a random off-peak time, `HH:MM`), and
      `retention.keep: 14`. Propose the merged targets' `default: true`
      target, or ask if none is marked default.
   2. **Show the proposal and let the admin tune it**
      `[decision: backup.strategy]` — one question at a
      time for anything the analysis couldn't infer (a database-shaped volume
      always needs the admin to confirm or supply a dump/load hook if they
      want `live` + hook over the safer `stop` default). Write
      `<slug>.backup.yaml` once agreed.
   3. **Materialize credentials.** For the chosen target: an rsync target
      generates `~/.ssh/doccap-backup-at-<target>` if absent and shows the
      admin the public key to authorize on the NAS
      `[decision: backup.authorize-target-key]` (skip if the key already
      exists — a prior use-case on this node already set this up); an s3
      target resolves `S3_ACCESS_KEY_ID`/`S3_SECRET_ACCESS_KEY`/
      `BACKUP_ENCRYPTION_PASSPHRASE` via `secrets` (env-var-first) and writes
      `/etc/drydocc/secrets/backup-<target>.env` (skip if it already exists
      with all required keys). Confirm `backup-recipes.md` § "Backup
      tooling" (`rclone`/`gpg` present, installing if not) when the target
      is `s3`.
   4. **Render the automation** per `backup-recipes.md` § "Rendering the
      automation" — script + service + timer written, `daemon-reload`d, but
      **not enabled yet**.
   5. **Trial run.** Start the service unit once directly
      (`systemctl --user start drydocc-backup-<slug>.service`, `NODE_RUN_LINGER`
      `dd-<slug>-backup`); poll to completion; verify a sealed snapshot
      exists (`backup-recipes.md` § "Sealing a snapshot").
   6. **Delegate the restore proof**: invoke `/restore-use-case <slug>
      --test`. A failure here means setup does **not** enable the timer —
      report why and stop; an unverified backup is not a backup.
   7. **Enable the timer** only after both 5 and 6 pass:
      `systemctl --user enable --now drydocc-backup-<slug>.timer`.

4. **Activate** — entered when `<slug>.backup.yaml` already exists (and
   `--setup` was not passed):

   1. **Re-render and diff.** Re-render the script/service/timer from the
      current manifest; if the manifest's content differs from what
      `automation_rendered_commit` was rendered from, replace the on-node
      files (same write + `daemon-reload` as setup step 4).
   2. **Ensure credentials are present** — the target's ssh key (rsync) or
      env file (s3); re-materialize via `secrets` if missing (e.g. after a
      rebuild wiped `/etc/drydocc/secrets/`), same as setup step 3.
   3. **Enable the timer** (idempotent if already enabled):
      `systemctl --user enable --now drydocc-backup-<slug>.timer`.
   4. **Catch up the journal.** Read
      `/var/lib/drydocc/backups/<slug>.runlog` (`backup-recipes.md` § "Run log") for lines newer
      than `last_backup`; turn each into `## What was done` narrative in this
      run's journal entry (unattended runs are journaled after the fact, not
      individually). If the node has notification set up, also poll the agent
      topic since `last_backup` per `node-notify/references/notify-recipes.md`
      § "Polling the agent topic" and mention any `run-failed` or `run-missed`
      for this use-case — the run log cannot record a run that never started.
   5. **Offer an on-demand run now** `[decision: backup.on-demand-run]` —
      warn about downtime first if `consistency: stop`.
   6. **Nudge on a stale restore test.** If `last_restore_test` is null, or
      older than `last_backup`, or older than a reasonable staleness window
      the admin sets no schedule for today (default: suggest re-testing if
      it's been longer than `retention.keep` × the backup interval, i.e. the
      test predates every snapshot currently retained), suggest
      `/restore-use-case <slug> --test`.

5. **Update the manifest's skill-written half**
   (`automation_rendered_commit`, `last_backup*`, `last_snapshot`, and — if
   step 3.6/4.6 ran — `last_restore_test*`).

6. **Journal** per `conventions/references/journal.md`, scope
   `use-case/<slug>`. `## What was done` names the phase (setup/activate),
   every target/credential action taken, and (activate) every caught-up run.
   `## Commands` includes the rendered script text verbatim (mirrors how
   `podman-quadlets` render snippets are journaled) so an admin can inspect
   or replay it without the agent. **Never** journal a secret value, the
   encryption passphrase, or an SSH private key.

7. **Commit + push** the manifest, any target-file change, and the journal
   entry. After an `on_client` run, remind the admin to `git pull` on the
   node's working copy.

## What this command does NOT do

- **Decide backup policy unilaterally.** It proposes; the admin tunes and
  approves before anything is written or scheduled.
- **Delete snapshots beyond retention's own rule.** `--setup` purges only the
  on-node automation, never data already on the target.
- **Perform the actual restore.** That is `/restore-use-case`, always
  delegated to, never duplicated here.
- **Run a use-case's backup on a schedule of its own invention.** The
  schedule is whatever `<slug>.backup.yaml:schedule` says; this command only
  renders and enables the timer.
- **Coordinate backups across nodes**, or make a multi-node stack's snapshots
  mutually consistent (`backup-recipes.md` § "What this file does NOT do").

## See also

- `references/backup-recipes.md` — snapshot layout, seal format, and every
  concrete command this skill's steps compose.
- `restore-use-case` — the paired skill; its `--test` mode is this skill's
  own verification path, never re-implemented here.
