---
name: conventions
description: Authoritative drydocc file schemas — use-case docs, node profiles, journal entries, secrets manifests, audit manifests, backup manifests, backup targets, maintenance posture, notification routing, and runbook bundles. Use before reading or writing any file under nodes/<node>/, or before authoring a new convention, to validate every field against the schema.
compatibility: Runs inside a drydocc fork; reads files only.
metadata:
  skillset: drydocc
---

# `drydocc` Conventions

Every `drydocc` file under `nodes/<node>/` follows one of ten schemas. This
skill is the index; the full prose specification for each lives in
`references/`, and a complete worked example of every file type but the
runbook bundle (whose reference carries its own) lives in
`assets/example-vps/`.

`assets/example-sbc/` is a **second** node profile and journal, on deliberately
different tooling. It exists because one example teaches the wrong lesson: if
the only worked instance is the one where every discovered fact happens to hold
its most common value, the schema reads as a description of that system rather
than as a ledger. Read the two side by side — the second node's tooling matched
no pre-existing dialect row anywhere in this repo, and commissioning it required
no edit to the base repo.

## The ten schemas

| File | Path on a node | Schema | Example |
|---|---|---|---|
| Use-case doc | `nodes/<node>/use-cases/<slug>.md` | [`references/use-case-doc.md`](references/use-case-doc.md) | `assets/example-vps/use-cases/nextcloud.md` |
| Node profile | `nodes/<node>/node.yaml` | [`references/node-profile.md`](references/node-profile.md) | `assets/example-vps/node.yaml` |
| Journal entry | `nodes/<node>/journal/YYYY-MM-DD-HHMM-<slug>.md` | [`references/journal.md`](references/journal.md) | `assets/example-vps/journal/2026-05-18-1422-initial-bootstrap.md` |
| Secrets manifest | `nodes/<node>/use-cases/<slug>.secrets.yaml` | [`references/secrets.md`](references/secrets.md) | `assets/example-vps/use-cases/nextcloud.secrets.yaml` |
| Audit manifest | `nodes/<node>/use-cases/<slug>.audit.yaml` | [`references/audit-manifest.md`](references/audit-manifest.md) | `assets/example-vps/use-cases/nextcloud.audit.yaml` |
| Backup manifest | `nodes/<node>/use-cases/<slug>.backup.yaml` | [`references/backup-manifest.md`](references/backup-manifest.md) | `assets/example-vps/use-cases/nextcloud.backup.yaml` |
| Backup targets | `nodes/backup-targets.yaml` + `nodes/<node>/backup-targets.yaml` | [`references/backup-targets.md`](references/backup-targets.md) | `assets/backup-targets.yaml` + `assets/example-vps/backup-targets.yaml` |
| Maintenance posture | `nodes/maintenance.yaml` + `nodes/<node>/maintenance.yaml` | [`references/maintenance-manifest.md`](references/maintenance-manifest.md) | `assets/maintenance.yaml` + `assets/example-vps/maintenance.yaml` |
| Notification routing | `nodes/notify.yaml` + `nodes/<node>/notify.yaml` | [`references/notify-manifest.md`](references/notify-manifest.md) | `assets/notify.yaml` + `assets/example-vps/notify.yaml` |
| Runbook bundle | `nodes/<node>/runbook.yaml` + `RUNBOOK.md` (a node promoted from a lab prototype) | [`references/runbook-bundle.md`](references/runbook-bundle.md) | — |

A further reference, [`references/hardening.md`](references/hardening.md),
documents the security baseline recorded in `node.yaml:hardening`.

The lab overlay, [`references/lab-overlay.md`](references/lab-overlay.md), is
read before anything else when `.drydocc/lab.yaml` exists at the fork root: it
marks a lab run, answers the admin's decisions, and states what changes when
the tailnet is off or `origin` is a lab remote. Lab scenarios — the runs
`scripts/lab/lab.py run` executes and judges — follow
[`references/lab-scenario.md`](references/lab-scenario.md). Which node-profile
fields describe the machine rather than the node is
[`references/node-profile.md`](references/node-profile.md) § "Box-bound
fields"; moving a node to another box keeps everything else.

Another,
[`references/distro-agnosticism.md`](references/distro-agnosticism.md), is the
derivation contract: how recipe sections state outcomes and probe facts
rather than naming tools, and the machine-readable list of blocked terms. Consult it before writing anything that touches a node's tooling.

## Invariants worth memorizing

- **Use-case doc**: frontmatter `containers`/`networks`/`volumes` lists MUST
  equal the keys under `spec.containers`/`spec.networks`/`spec.volumes`. Every
  `spec.containers.<name>.image` MUST be a pinned tag — `:latest` is a
  validation error. Body headings, in order: `## Intent`, `## Architecture`,
  `## Runbook`, `## Quadlets`. Quadlets are rendered from `spec:` at apply time
  — no quadlet files in git. See the `podman-quadlets`
  skill for the render mapping.
- **Node profile**: `system_facts`, SSH server, and key algorithm are recorded
  from the **live probe** during bootstrap, not from admin claims and never
  inferred from `distro`. The listed enum values make facts greppable but do not
  close the set — record whatever the probe returns. The `ssh:` block
  holds metadata only — never private keys.
- **Journal**: append-only, same commit as the change it records; failures still
  journal (`outcome: failed`).
- **Secrets manifest**: values NEVER in git. `keepass_entry` uses the
  `drydocc/…` prefix and is metadata for the
  `@max-win-at/keepassxc-secrets` skill. The node-side env file is owned
  `doccap:doccap`, chmod 600. See the `secrets` skill for resolution.
- **Audit manifest**: entirely optional — `/audit-use-case`'s universal checks
  run without it. Everything below the `written by /audit-use-case` marker is
  agent-owned and rewritten each run. Content NEVER in git: paths and one-way
  digests only, and env files yield key *names* only. `last_audited*` lives here,
  never as a bump of the doc's `last_applied_commit`.
- **Backup manifest**: entirely optional, like the audit manifest — a use-case
  with no volumes never gets one. Declares `target`/`schedule`/`retention`/
  `consistency`/`volumes`/`hooks`; `/backup-use-case` writes the
  `last_backup*`/`last_restore_test*` half. A backup is not "done" until
  `/restore-use-case --test` has proven it restorable.
- **Backup targets**: `nodes/backup-targets.yaml` (fork-wide) merged with
  `nodes/<node>/backup-targets.yaml` (this node's overrides/additions) by
  `name`. No secret values live here — only which KeePass entry and env-file
  path hold an S3 target's credentials, or an SSH key's metadata for an rsync
  target.
- **Notification routing**: entirely optional — with no `nodes/notify.yaml`
  nothing is sent. Merged fork/node like backup targets, events matched by
  `id` from a closed vocabulary. Topic names and tokens are credentials and
  never appear here — only the env-var names that hold them. Messages leave
  the infrastructure, so they carry ids, verdicts, and counts, never secret
  values or log lines.

## How to use this skill

1. Before writing or modifying any file matching the paths above, open the
   matching `references/` doc and check every field against the schema.
2. When validating a file someone else wrote, flag missing or extra fields.
3. When a schema feels insufficient for a new requirement, stop and raise it
   with the admin as a framework change — never extend the schema in passing.

## What this skill does NOT do

- Run anything, or touch a node. It is reference material.
- Hold procedure. The command that writes a file owns *how* it is written;
  these schemas say only *what* it must contain.
- Hold or resolve secret values — that is `secrets`.
- Extend a schema in passing. A new file, field, or enum value is a
  framework change (step 3 above).
