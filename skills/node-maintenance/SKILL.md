---
name: node-maintenance
description: Assess and execute recurring maintenance on a node — container image currency, OS package updates, log growth, fact freshness, certificate expiry, intrusion-prevention review, tailnet health, backup coverage, credential rotation windows, per-container resource and log telemetry against a learned baseline, and a security audit (hardening drift, accounts and sudo, privileged files, scheduled jobs, exposed listeners, auth events, file integrity, build toolchains) against the node's own baseline. Derives the work from the node's own inventory of installed use-cases, backup strategies, and security stance rather than running a fixed task list, and holds every node in the fork to one posture. Optionally renders an assess-only timer for unattended sweeps and a one-shot telemetry sampler. Use for routine upkeep or an unattended sweep.
compatibility: Runs inside a drydocc fork; needs a shell and file edits.
metadata:
  skillset: drydocc
  requires: conventions secrets node-context node-exec podman-quadlets node-notify
---

# /node-maintenance

Answers "what upkeep does this node owe, and is it still being held to the same
bar as the rest of the infrastructure?"

It cannot answer that from a fixed task list, because the work depends entirely
on what is on the box: which use-cases are installed and active, which of them
declare a backup strategy and whether that strategy has ever been proven, and
what the node's security stance is. So the command ships a **posture** — a
closed catalog of maintenance *outcomes* every node in the fork is held to — and
derives, per run, which outcomes apply here and what concrete work each implies
on this box. Two nodes with the same posture legitimately do different work on
the same day; that is the design, not a compromise.

## Contract

- **Args:**
  - On the node: `[--setup] [--only <id,…>] [--report-only]`.
  - On the commissioning client: `<node> [--setup] [--only <id,…>] [--report-only]`
    — the target node is the **positional first argument** (required in client
    mode; `node-context` refuses without it).
  - `--report-only` assesses and reports, then stops; it writes no node state
    and no manifest, and never renders or enables the timer. This is the safe
    first invocation on any node.
  - `--only <id,…>` restricts the run to those posture ids. Ids not in the
    catalog are refused by name.
  - `--setup` discards the on-node automation and re-renders it. It never edits
    the authored half of the manifest.
- **Surface:** runs from either surface, resolved by `node-context` — directly
  on the node, or SSH-wrapped from the commissioning client. Every on-node
  command routes through `node-exec` as a `NODE_RUN`; the long ones (image
  pulls, package transactions) use `NODE_RUN_LINGER` as
  `dd-maintenance-<step>`, so a dropped connection does not orphan a
  half-finished transaction.
- **Reads:** `nodes/maintenance.yaml` + `nodes/<self>/maintenance.yaml`
  (merged, `conventions/references/maintenance-manifest.md`);
  `nodes/<self>/use-cases/*.md` and their `*.backup.yaml` / `*.secrets.yaml`
  siblings; `nodes/backup-targets.yaml` + the node layer;
  `nodes/<self>/node.yaml`; the `telemetry:` block of each
  `nodes/<self>/use-cases/*.audit.yaml`; `nodes/notify.yaml` + the node layer,
  if present; the node's journal for the date of the last sweep;
  and every sibling node's `maintenance.yaml` — **only** that file, to report
  whether this node is the exception.
- **Writes (in-repo):** `nodes/maintenance.yaml` (first-run walkthrough only);
  `nodes/<self>/maintenance.yaml`; `nodes/<self>/node.yaml:system_facts` — that
  block and no other; `telemetry.last_evaluated` and `telemetry.baseline` in
  `nodes/<self>/use-cases/<slug>.audit.yaml` — those two fields and nothing else
  in that file; the journal entry.
- **On-node writes from the security audit:** only the file-integrity
  database, and only on an accepted re-base. Every other security item reads.
- **On-node side effects:** a rendered script at
  `~/.config/drydocc/maintenance.sh` and a systemd user service+timer at
  `~/.config/systemd/user/drydocc-maintenance.{service,timer}`; a run log at
  `/var/lib/drydocc/maintenance.runlog`; a telemetry sampler — script
  `~/.config/drydocc/telemetry.sh`, units
  `~/.config/systemd/user/drydocc-telemetry.{service,timer}`, and samples under
  `/var/lib/drydocc/telemetry/`; plus whatever the accepted posture
  items do — pulled images, restarted units, vacuumed journals, applied package
  updates.
- **Journal scope:** `maintenance` — one entry per run, with a subsection per
  posture item.

## Refusals

1. **No `node.yaml` for `<self>`** — `<self> has no node profile. Run
   /bootstrap-node <self> first — there is no baseline to maintain yet.`
2. **Unknown posture id**, in `--only` or in either manifest layer —
   `No posture item '<id>'. The catalog is closed; see maintenance-recipes.md
   § "The posture catalog".` Naming it is deliberate: an id nobody recognises
   is almost always a typo, and ignoring it silently would drop an outcome the
   admin believes is covered.
3. **`mode: auto` on an id absent from `verified_auto`** — `<id> is set to auto
   but has never completed an interactive run on <self>. Run it interactively
   once; it is promotable afterwards.`
4. **Timer half without a systemd service manager** —
   `Refusing to render maintenance automation: the timer requires systemd;
   system_facts.service_manager is '<value>'.` The interactive assessment still
   runs in full. The refusal names the failing capability, never the system.
5. **Node unreachable** — not a refusal. The run continues against the
   repo-only half of the inventory, marks every node-dependent item `UNKNOWN`,
   and says so in the report. A sweep that cannot reach the box can still find
   a use-case whose backup has never been proven.

## Procedure

1. **Bind `<self>` + surface** via `node-context`. All on-node commands below
   route through `node-exec` — issued locally when `on_node`, SSH-wrapped when
   `on_client`. The snippets in `references/maintenance-recipes.md` are the
   `on_node` form.

2. **Resolve the posture.** Merge `nodes/maintenance.yaml` and
   `nodes/<self>/maintenance.yaml` per
   `conventions/references/maintenance-manifest.md` § "Merge rule". If the
   fork-wide file does not exist, run its first-run walkthrough now — author it
   at the default posture (every item `report`), commit it with its journal
   entry, and continue. Apply refusals 2 and 3 against the merged result.

   Record which items came from the node layer; these are the run's reported
   deviations, and they are what makes a local exception visible rather than
   silent.

3. **Build the inventory** per `references/maintenance-recipes.md` § "The
   inventory join". This is pure repo reading and happens before anything
   touches the node, so an unreachable node still produces a real report. Only
   `status: active` use-cases contribute work; `_retired/` is never traversed.

4. **Run the assessment probe** — one read-only round trip per
   `references/maintenance-recipes.md` § "The bundled assessment probe".
   Substitute only idioms that have been verified for this node's facts; leave
   every unresolved substitution **empty** so the probe reports `no-probe`
   rather than guessing. Parse the `<key>=<value>` lines.

5. **Assess each posture item in the merged catalog.** Each item's section in
   the recipes file says what makes it applicable, what its finding is, and
   whether this command acts or delegates. **A failure on one item never aborts
   the others** — mark it `UNKNOWN`, continue, and let the run's outcome be
   `partial`.

   The three items carrying genuine dialect derivations —
   § "Outstanding OS package updates are applied",
   § "The intrusion-prevention ban ledger is readable", and
   § "TLS material is inventoried with its expiry" — are outcome contracts.
   Follow their verification steps as written; do not proceed on a derivation
   that has not been verified, and append what you verify to their dialect
   tables.

   The two telemetry items — `resources-within-baseline` and
   `logs-within-baseline` — read each active use-case's samples file and judge
   it per `references/maintenance-recipes.md` § "Baselines and anomalies":
   establish a baseline where none exists and enough samples do, report
   "re-baselining" where the baseline is stale, and otherwise test every
   anomaly rule. **Never re-base because of an anomaly.** A use-case with too
   few samples yet is `n/a` with the date its baseline becomes possible, not a
   pass.

   The eight security-audit items follow `references/maintenance-recipes.md`
   § "Security audit". Each compares the live state with this node's own
   `security_baseline`, never with an expected count. A node with no baseline
   yet records one and lists everything it recorded. The integrity check runs
   outside the bundled probe, as `NODE_RUN_LINGER` `dd-maintenance-integrity`,
   and only after the database's hash matches the recorded one.

6. **Render the findings report** per
   `references/maintenance-recipes.md` § "The findings report": one row per
   item with verdict, risk and cost; then the posture deviations; then the
   delegated findings as ready-to-paste invocations. Stop here if
   `--report-only`.

7. **Execute accepted items.** Present the menu from that same section and act
   only on what the admin accepts. Verify each item against its own
   verification step before calling it done — an item that acted but did not
   verify is reported as failed, not as success. Two rules hold regardless of
   what was accepted:

   - **Never reboot the node without a separate, explicit confirmation**, taken
     after the package updates have been applied and verified.
   - **Never perform a delegated remedy.** Hardening repair, backup setup,
     restore proof and credential rotation are emitted as invocations of the
     command that owns them, and this command does not do them itself.

   The security-audit items act only on `[s] accept <id>`, one item at a
   time, after every change it accepts has been shown. A critical finding is
   never accepted this way. They never remove an account, a privilege bit, a
   scheduled job or a package.

   The telemetry items act only on `[r] rebase <slug>` — accepting current
   behaviour as the new normal — and only when the admin chooses it for that
   use-case. The remedy for a genuine anomaly is an investigation, which is the
   admin's.

   While a step blocks on the admin (a reboot confirmation, a secret pause),
   publish `admin-attention` per `node-notify/references/notify-recipes.md`
   § "Publishing an event", if the node has notification set up.

   After an item completes and verifies interactively, offer to promote it to
   `mode: auto`. That success is exactly the evidence the promotion gate asks
   for, and it is the only way an item becomes eligible for unattended running
   on this node.

8. **Render or refresh the automation** per
   `references/maintenance-recipes.md` § "Rendering the automation". Re-render
   when the merged manifest no longer matches `automation_rendered_commit`.
   Enable the timer only once at least one item is in `verified_auto` —
   before that an unattended run could only assess, and an assessment nobody
   reads is noise. `--setup` purges and re-renders first.

   Render the telemetry sampler per § "Telemetry sampling" when either
   telemetry item is not `off` and at least one active use-case declares
   containers, and enable it once the admin has accepted it in this run.
   Sampling is read-only, so it has no promotion gate. Purge it when both items
   are `off` or nothing is left to sample.

9. **Catch up the run log.** Read `/var/lib/drydocc/maintenance.runlog` for
   lines newer than `last_maintenance` and fold each into this run's journal
   narrative. Unattended runs are journaled after the fact; nothing commits
   from the node without an agent present. If the node has notification set
   up, also fold in `/var/lib/drydocc/notify.log` and poll the agent topic
   since `last_maintenance` per `node-notify/references/notify-recipes.md`
   § "Polling the agent topic" — saying plainly how far back the server's cache
   reaches.

10. **Write the manifest's skill-written half** — `last_maintenance`,
    `last_maintenance_outcome`, `automation_rendered_commit`, and any id newly
    added to `verified_auto`. For each use-case the telemetry items evaluated,
    write `telemetry.last_evaluated`, and `telemetry.baseline` where one was
    established or an accepted re-base replaced it — preserving every other
    field of `<slug>.audit.yaml` verbatim. Write `security_baseline` where a
    first run recorded it or an accepted re-base replaced it, including the
    new `integrity_db_hash` after an integrity re-base. Write back `node.yaml:system_facts` if
    § "Fact freshness" ran and found changed keys — that block only, and only
    after the probe verified each value.

11. **Journal** per `conventions/references/journal.md`, scope `maintenance`,
    one entry for the run. `## What was done` carries a sub-heading per posture
    item (`### container-images-current: …`) with its verdict, what was acted
    on, and what was delegated — the same shape `scope: onboarding` uses for a
    multi-part run. `## Commands` carries the composed commands verbatim, so an
    admin can inspect or replay them without the agent. **Never** journal a secret value,
    a ban source address, certificate contents, a notification topic, or a log
    line — telemetry findings are statistics and counts. The same holds for
    the security audit: never a password hash, key material, a login's source
    address, a file's contents or a scheduled job's command line. Paths,
    account names, unit names, counts and `sha256:` digests only. `outcome` is `success` when
    every assessed item resolved, `partial` when any item failed or was left
    `UNKNOWN`, `dry-run` under `--report-only`.

12. **Commit + push** the manifest, any `node.yaml` change, and the journal
    entry. After an `on_client` run, remind the admin to `git pull` on the
    node's working copy.

## Example output

```
Maintenance posture for vps-fra-01 — last sweep 2026-08-14 (30 days ago)
3 active use-cases, 1 planned.

| item                              | mode   | verdict | finding                                | risk    | cost     |
|-----------------------------------|--------|---------|----------------------------------------|---------|----------|
| container-images-current          | report | DRIFT   | 2 of 5 images behind their pinned tag  | restart | seconds  |
| os-packages-current               | report | OK      | nothing pending                        | none    | none     |
| logs-bounded                      | auto   | DRIFT   | user journal 2.1 GB, over 1 GB limit   | low     | none     |
| node-facts-fresh                  | report | DRIFT   | facts last probed 118 days ago         | low     | none     |
| tls-material-valid                | report | OK      | 1 certificate, 67 days to expiry       | none    | none     |
| intrusion-prevention-reviewed     | report | OK      | 3 jails, 41 bans, none anomalous       | none    | none     |
| tailnet-healthy                   | report | OK      | online as vps-fra-01                   | none    | none     |
| data-recoverable                  | report | DRIFT   | 1 unproven, 1 unprotected              | none    | none     |
| credentials-within-rotation-window| off    | SKIPPED | disabled here: quarterly access audit  | —       | —        |
| resources-within-baseline         | report | DRIFT   | nextcloud-app memory +9%/day, R² 0.93  | none    | none     |
| logs-within-baseline              | report | OK      | 2 use-cases in baseline; 1 re-baselining | none  | none     |
| node-hardening-holds              | report | OK      | firewall, fail2ban, sshd, kernel hold  | none    | none     |
| access-reviewed                   | report | OK      | root only at UID 0; 1 key holder       | none    | none     |
| privileged-files-within-baseline  | report | OK      | 31 setuid/setgid, none new             | none    | none     |
| persistence-reviewed              | report | DRIFT   | 1 system timer not seen before         | none    | none     |
| exposure-matches-declaration      | report | OK      | 4 listeners, all declared              | none    | none     |
| auth-events-reviewed              | report | OK      | 212 failures, 0 burst-then-success     | none    | none     |
| file-integrity-verified           | report | DRIFT   | 14 changed: 13 package-explained, 1 not | low    | none     |
| build-toolchain-absent            | report | OK      | no compiler or build system            | none    | none     |

Posture deviations from the fork-wide bar:
  logs-bounded                        auto  (promoted here; fork-wide is report)
  credentials-within-rotation-window  off   ("reviewed in the quarterly access audit, not per-sweep")

Delegated — these are not this command's to fix:
  /restore-use-case nextcloud --test   backup never proven restorable
  /backup-use-case matrix              active use-case, no backup strategy
```

## What this command does NOT do

- **Run a fixed task list.** The posture catalog is fixed; the work is derived
  from the node's inventory every run.
- **Fix what another command owns.** Hardening drift goes to `/bootstrap-node`,
  backup coverage and restore proof to `/backup-use-case` and
  `/restore-use-case`, credential rotation to `secrets`. This command finds
  them and hands them over.
- **Reboot a node unattended**, or render automation that could. Applying
  updates and acting on the reboot they request are separate decisions, and the
  second one is always the admin's.
- **Write `node.yaml:hardening`.** It reports a discrepancy there and emits
  `/bootstrap-node <node>`; recording a corrected value without repairing it
  would make the profile claim a posture nobody restored.
- **Touch sibling nodes.** It reads their `maintenance.yaml` to answer "is this
  node the exception?" and nothing else — no probe, no connection, no write.
- **Alert in real time.** Telemetry is sampled on an interval and judged at
  sweep time. The sampler publishes nothing; the unattended sweep publishes
  state changes. A spike that comes and goes between samples is not seen, by
  design.
- **Keep log content.** The sampler counts warning and error lines per unit
  and discards them. Reading what the errors say is the admin's, on the node.
- **Remove what the security audit finds**, or run a rootkit scanner or a
  third-party hardening score. Findings are the admin's to act on; the items
  are outcomes this skill derives and verifies.
- **Extend the catalog per fork.** An outcome with no derivation and no
  verification behind it is a promise nothing can keep.

## See also

- `references/maintenance-recipes.md` — the posture catalog, the outcome
  contracts, the security audit, and every concrete command this skill's steps
  compose.
- `conventions/references/maintenance-manifest.md` — the two-layer posture
  schema, the merge rule, and the promotion gate.
- `list-use-cases` — the same inventory join, rendered as a plain table with no
  node access.
- `node-notify` — the optional notification channel the rendered automation
  publishes through, and the missed-run alarms that watch it.
