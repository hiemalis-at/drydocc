# Journal Entry Convention

## Path

Journal entries live in `nodes/<node>/journal/` as `YYYY-MM-DD-HHMM-<slug>.md`, where:
- **`YYYY-MM-DD`** — ISO date of the action.
- **`HHMM`** — UTC hour and minute (24-hour format, zero-padded).
- **`<slug>`** — Kebab-case short phrase summarizing the action (e.g., `initial-bootstrap`, `create-use-case-nextcloud`, `rotate-secrets-postgres`).

Example: `nodes/vps-fra-01/journal/2026-05-18-1422-initial-bootstrap.md`.

## Frontmatter Schema

Every journal entry opens with YAML frontmatter describing the action's metadata:

```yaml
---
timestamp: 2026-05-18T14:22:00Z
actor: claude/opus-4-7
trigger: /create-use-case nextcloud "Nextcloud instance for team docs"
scope: use-case/nextcloud
outcome: success
commit_before: a501e0b
commit_after: 8f3c9d2
# mode: rebuild   # present only on apply entries — see Field Descriptions
---
```

### Field Descriptions

- **`timestamp`** — ISO 8601 datetime in UTC (YYYY-MM-DDTHH:MM:SSZ). Marks when the action completed.
- **`actor`** — String identifying who performed the action. Format:
  - `claude/<model>` for agent actions (e.g., `claude/opus-4-7`, `claude/haiku-4-5`).
  - `human` for human-only edits (e.g., manual doc corrections journaled by a skill post-hoc).
- **`trigger`** — The slash-command invocation that produced this entry, or the string `manual` if it was a manual edit. Examples:
  - `/create-use-case nextcloud "Nextcloud instance for team docs"`
  - `/bootstrap-node`
  - `manual` (human edit to a doc, journaled for posterity).
- **`scope`** — Enum defining what was changed:
  - `node` — Changes to the node profile itself (e.g., `/bootstrap-node` Phase 0 scaffolding).
  - `use-case/<slug>` — Changes to a specific use-case (e.g., `/create-use-case nextcloud`, `/modify-use-case nextcloud`).
  - `hardening` — Changes to node security state (e.g., firewall rule updates, fail2ban config tweaks, the `/bootstrap-node` audit-&-repair pass). Not used by `/audit-use-case`, which journals under `use-case/<slug>`.
  - `onboarding` — Reconciliation of a pre-existing box into the framework via `/onboard-node`. A single entry covers the whole run, with per-item sub-headings inside `## Commands` (`### Probe: …`, `### Apply: …`, `### Cluster: …`).
  - `maintenance` — General operational tasks (e.g., package updates, log
    rotations) run by `/node-maintenance`. **One entry per run**, covering
    every posture item assessed, with a sub-heading per item inside
    `## What was done` (`### container-images-current: …`) — the same shape
    `onboarding` uses for a multi-part run. A command the sweep
    delegates to journals its own entry under its own scope; maintenance never
    journals on another command's behalf. Volume backup/restore is scoped
    `use-case/<slug>` instead (`/backup-use-case`, `/restore-use-case`) — it
    is use-case-scoped data, not a node-wide task.
  - `recovery` — A node-wide disaster recovery run by `/rebuild-node`. **One
    entry per run**, with a sub-heading per phase and per use-case inside
    `## What was done` (`### base: replaced`, `### nextcloud: recovered`,
    `### automation: …`). Every command it delegates to — `/bootstrap-node`,
    `/apply-use-case`, `/restore-use-case`, and the re-arming commands —
    journals its own entry under its own scope; the recovery entry names
    those entries by filename and never journals on their behalf.
- **`outcome`** — Enum describing the result:
  - `success` — Action completed as intended; all changes applied.
  - `partial` — Action completed but with warnings or skipped steps (e.g., "created use-case but quadlet materialization deferred").
  - `failed` — Action did not complete; no changes (or rolled back changes) applied. Always journal failures for debugging.
  - `dry-run` — Action was executed in dry-run mode; no live state changed.
- **`commit_before`** — Short commit SHA (7–12 chars) of the HEAD before the action. Allows diffing the state before and after.
- **`commit_after`** — Short commit SHA of the commit that contains the action's changes. Must exist (i.e., the action must commit its changes atomically).
- **`mode`** — Present **only** on `scope: use-case/<slug>` entries written by
  `/apply-use-case`. Enum: `greenfield | adoption | rebuild` — which run mode
  the apply classified into. Omitted by every other
  command.

## Body Sections

After the frontmatter, the journal entry continues with required markdown sections:

### What was done

A bullet-list summary of concrete actions taken. Examples:
- Created `nodes/vps-fra-01/use-cases/nextcloud.md` with frontmatter.
- Wrote `nextcloud.quadlets/{nextcloud-app,nextcloud-db,nextcloud-net}.container`.
- Ran `podman network create nextcloud-net` (actual execution deferred; quadlets-only this pass).
- Fetched POSTGRES_PASSWORD and NEXTCLOUD_ADMIN_PASSWORD from KeePass via `secrets`.
- Wrote `nodes/vps-fra-01/use-cases/nextcloud.secrets.yaml`.
- Updated `node.yaml:hardening.firewall` to reflect new port rules.

### Commands

A fenced bash code block listing every shell command run during the action. Examples:

```bash
$ git add nodes/vps-fra-01/use-cases/nextcloud.md nodes/vps-fra-01/use-cases/nextcloud.secrets.yaml
$ git commit -m "feat(use-case): add nextcloud instance for vps-fra-01"
$ git push origin main
```

If no commands were run (pure repo edits), explicitly note "No shell commands executed."

### Notes / drift detected

A narrative section capturing:
- **Surprises or deviations**: e.g., "Container image registry was unreachable; retrying in 60 seconds worked."
- **Discovered facts** (node-scope entries, typically `/bootstrap-node` or
  `/onboard-node`): record every fact just written to `node.yaml:system_facts`
  — package manager, service manager, container engine, `firewall.{tool,
  rule_model,tag_mechanism}`, `rootless_ready`, and any `capabilities.*` probed
  this run — e.g., "Detected package_manager: `<value>`; service_manager:
  `<value>`; container_engine: `<value>`; firewall.tool: `<value>`,
  rule_model: `<value>`, tag_mechanism: `<value>`; rootless_ready: `<value>`;
  capabilities.tmux: `<value>`." Node commissioning is the first point a
  concrete dialect becomes relevant — record what was found
  here (or in `## What was done`), not just in `node.yaml:system_facts`.
- **Drift detected**: e.g., "Live firewall state was missing the 3478/udp rule; `/create-use-case` corrected it."
- **Human interventions**: e.g., "Admin manually edited `nextcloud.md` after creation to add a warning about TLS cert renewal."
- **Deferred work**: e.g., "Quadlet templates are placeholders; materialization deferred until `/apply-use-case`."
- **Rebuild evidence** (entries with `mode: rebuild`): the absence probe
  results that triggered classification, the identity comparison shown to the
  admin, and every volume named in the closing empty-volume warning.
- Leave empty if everything went smoothly with no surprises.

## Derived queries

Answers that more than one command reads out of the journal. Each is defined
once, here, and cited by name — a second copy of a query is how two commands
end up disagreeing about the same node.

### Rebuilt, not yet restored

A use-case **owes a restore** when both hold, reading only
`scope: use-case/<slug>` entries:

1. The most recent entry whose `trigger` starts with `/apply-use-case` has
   `mode: rebuild`; and
2. no **later** entry for the same scope has a `trigger` that starts with
   `/restore-use-case`, does **not** contain `--test`, and has
   `outcome: success`.

A `--test` restore proves a snapshot and touches no live volume, so it never
settles the debt; neither does a live restore that failed its seal check. A
use-case that owes a restore has lost data **whatever its live volumes
contain** — the application may already have initialised fresh files on the
empty volumes the rebuild created, so file counts are not evidence.

Read by `/backup-use-case` (its rebuild guard), `/rebuild-node` (data triage),
and `/node-maintenance` (`data-recoverable`). The `owes_restore` operation
(`podman-quadlets` § "The drydocc tools") answers it for one use-case; this
definition remains the contract.

## Rules

1. **Append-only**: Never rewrite or delete past entries. The journal is an immutable audit log.
2. **Same commit as the change**: Each action's changes (edits to `.md`, `.yaml`, quadlets, etc.) are committed atomically in the same commit referenced by `commit_after`. This ensures the journal entry and its changes are traceable.
3. **Always journal, even on failure**: If an action fails partway through (e.g., a skill encounters an error), still write a journal entry with `outcome: failed` and document what partial state was left behind. This aids debugging and recovery. A run whose agent died before it could journal (crash, unreachable server, exhausted credits) is journaled by `/continue-last-command` when the admin abandons it: the dead command's own scope, its original `trigger`, `outcome: failed`.
4. **Timestamp matches filename**: The `timestamp` field and the filename's `YYYY-MM-DD-HHMM` prefix should agree (or be within a few seconds if the agent and system clocks are slightly skewed).

## Committing

Every node's agent, and the admin's laptop, push to the same fork, so a push
is routinely behind the remote. Stage the change's own paths by name (never
`git add -A` or `git add .`), commit, then:

```bash
git pull --rebase
git push
```

Node scoping makes the rebase safe: another node's commits only touch its own
`nodes/<other>/` paths, so they never conflict with this one's. If the rebase
does stop on a conflict, something wrote outside its scope — stop and show the
admin; never resolve it by taking either side wholesale, and never force-push.
A push that still fails after the rebase (no network, no write access) leaves
the commit local: say so, and push it on the next run before anything else.

A node pushes with its own deploy key, configured on its working copy at
clone time (`bootstrap-node` `references/hardening-recipes.md` § "Fork write
access").

## Example

See `../assets/example-vps/journal/2026-05-18-1422-initial-bootstrap.md` for a complete example.
