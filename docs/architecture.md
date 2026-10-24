# `drydocc` Architecture

This document is for **agents working in a fork and for contributors to the
framework**. It describes how the system is put together and which principles
constrain a change to it. What `drydocc` does for its user is
[`README.md`](../README.md); how an admin operates it, step by step, is
[`FORKING.md`](../FORKING.md).

## 1. Fork unit

One fork = one infrastructure = one or more Linux nodes. The fork is cloned
onto every node, and each node writes only to its own `nodes/<self>/`
subdirectory. There is no server, no daemon, and no shared state outside git:
the repository is both the description of the infrastructure and the medium
through which nodes coordinate.

`<self>` is bound at runtime by the `node-context` skill, which also resolves
the **execution surface** (§4) and refuses any write outside the bound node's
scope.

## 2. Design principles

These are the ideas the implementation keeps returning to. Each is enforced
somewhere concrete, and the enforcement point is named.

### Convergence, not one-shot correctness

Every node-touching command is written to be **re-run**. It measures the
distance between the declaration and the live node, applies the smallest step
that reduces it, records where it stood, and stops. `/bootstrap-node` re-run
on a half-built node skips what is already done; `/audit-use-case` reports the
remaining distance without changing anything; `/apply-use-case` cuts over
reversibly and can be run again after a failure.

This is deliberately a gradient-descent shape rather than a transaction. An
LLM cannot be relied upon to be right in one pass, but it can be relied upon
to move downhill and write down where it stopped — so the framework asks only
that of it. Two consequences follow, and they are why the invariants exist:
every step must be **idempotent**, and every step must be **journaled** even
when it fails. A resumed run re-probes the node for truth rather than trusting
a checkpoint — the journal is how it knows what was already attempted, and why
the last pass stopped.

### The declaration is the source of truth; runtime artifacts are derived

A use-case document's `spec:` block is authoritative. Quadlets are *rendered*
from it at apply time into `~/.config/containers/systemd/` and are **not**
committed. Nothing that can be regenerated from a declaration is
stored, because a second copy is a second thing that can drift.

The corollary is the one property the whole framework is built to protect:
**a node must be reconstructible from its declarations alone** (§7). State
that violates this is not a nuisance, it is the primary defect the audit hunts
for.

### Distro-agnosticism by derivation

Generic prose — this document, `AGENTS.md`, SKILL.md bodies, non-recipe
references — never names a package manager, firewall tool, init system, or
MAC framework, not even as a schema field name (`hardening.firewall`, never
`hardening.<tool>`). The alternative — branching on distro identity — bakes
today's tooling into prose that outlives it, and cannot answer for a box
nobody has written down yet.

Concrete dialects are instead **derived on the node**: discovered by a live
probe, verified before anything depends on the result, written to
`node.yaml:system_facts` as an open vocabulary, and journaled at discovery.
Tool syntax lives only in a workflow skill's `references/*-recipes.md`, where
each section is an outcome contract rather than a per-distro command list, and
refusal turns on a missing capability — never on what the box calls itself.
Enforced as invariant 5 in `AGENTS.md`; the full contract, with a
machine-readable blocklist, is
`skills/conventions/references/distro-agnosticism.md`.

### Nothing happens silently

Every state-changing action appends an entry under `nodes/<self>/journal/`, in
the same commit as the change it describes, including when the outcome is
`failed`. The journal is append-only and never hand-edited. Git history plus
the journal is the audit trail; it is also, per the convergence principle, the
checkpoint a resumed run reads. Invariant 2.

### Secrets are never git data

Values live in the admin's KeePass database and on the node at
`/etc/drydocc/secrets/<slug>.env` (chmod 600). Git holds only the manifest —
key names and their purposes — as `<slug>.secrets.yaml`. Secrets are resolved
**just-in-time** by the `secrets` skill, environment first, then KeePassXC,
then an explicit pause for admin-supplied fallback; never eagerly at the top
of a run. Invariant 3.

### One fact, one level

`AGENTS.md` holds invariants and routing; SKILL.md bodies hold procedure;
`references/` hold exact commands and schemas.
Each fact lives at exactly one level, and lower levels link upward rather than
restate. Two copies of a fact is how one of them goes stale unnoticed.
Invariant 6; the document topology it produces is mapped in §9.

## 3. Building blocks

**On the node:** git, rootless podman with systemd quadlets, ssh, Tailscale
for the mesh, a host firewall (whichever tool
`node.yaml:system_facts.firewall.tool` reports — discovered, never inferred),
and fail2ban.

**On the client:** VS Code with an AI coding agent extension, plus the Docker
extension pointed at podman when the admin runs containers locally too.

## 4. Execution surfaces

The admin types a slash command; the agent reads the framework's skills, the
fork's conventions, and the node's current state, then acts. *Where* the agent
runs relative to the node is the **surface**, resolved by `node-context` and
consumed by `node-exec`.

- **`on_node`** (preferred) — the agent runs on the node itself, via VS Code
  Remote-SSH or a node-local agent CLI. `/etc/drydocc/node-name` is present
  and binds `<self>`. Repo, podman, systemd-user, and the firewall are local.
- **`on_client`** — the node cannot host the VS Code Remote Server
  (incompatible libc or arch, no outbound to the update host; probed at
  bootstrap/onboard and recorded in `node.yaml:remote_ide`). The admin runs
  the same commands from the laptop, passing the node as the **first
  positional argument**, and `node-exec` SSH-wraps every node-touching
  command. Long steps linger in tmux so a dropped connection does not kill
  them.

Same procedure, same journal, different transport.

```
   Admin in VS Code SSH-Remote (or node-local agent CLI)   [on_node, preferred]
        │ /create-use-case nextcloud "..."
        ▼
   Agent on the node ──► reads skills/* + nodes/<self>/
        │
        ├── edits nodes/<self>/use-cases/nextcloud.md (with spec: block)
        ├── renders quadlets from spec → ~/.config/containers/systemd/ (NOT committed)
        ├── invokes podman / systemctl --user / firewall tool       (via node-exec)
        └── appends nodes/<self>/journal/<ts>-<slug>.md   ← always
        │
        ▼
   git commit + push

   Fallback — node can't host VS Code Remote Server:              [on_client]
   Admin on the laptop ──► /create-use-case <node> nextcloud "..."
        └── node-exec SSH-wraps each command (tmux lingers long steps)
```

### The lab

The same skills also run against **local VMs**, with no change to them. A lab
run is a fork of its own: a standalone clone in the lab store, outside the
admin's checkout, with an empty fleet, a local lab remote as its only remote,
an isolated home, and PATH shims that keep its ssh, tailnet and vault access
inside the run and the hypervisor out of its reach. Its nodes are linked clones of
sealed VM layers — an installed empty OS, then the same plus
`/bootstrap-node` — resealed with their own identity before first boot, for
the host's architecture or an emulated one. A deterministic driver
(`scripts/lab/lab.py`), never the agent under test, owns the VMs, the
verdict and the cleanup. The `on_client` surface is how every lab node is
reached.

- **Test runs** let a headless agent run a scenario's steps, judge them only
  by assertions, and collect everything on a pass. A gitignored overlay
  answers the absent admin's questions by stable decision id; an unanswered
  one blocks the run instead of being guessed.
- **Prototypes** are kept and attended: the admin works in one, keeps states
  as named restore points, and extracts a node as a **runbook bundle** — its
  declarations with the machine-specific fields stripped, plus a runbook
  that a fresh lab box replays before it is stamped verified. Promotion
  lands the bundle as a new `nodes/<target>/` of the real fork; it is the
  only thing that ever leaves the lab.

## 5. Node anatomy

Each node runs an unprivileged `doccap` user in sudoers; root ssh login and
password authentication are disabled. Tailscale SSH is the primary access
path, with bare ssh on `ssh_fallback_port` (default 42022, keyed to `doccap`
only) as the fallback that survives a broken tailnet. The node's identity is
written once to `/etc/drydocc/node-name` by `/bootstrap-node`, and is what
makes node scoping enforceable rather than advisory. A host firewall and
fail2ban, plus the locked-down sshd, are the baseline hardening surface;
`hardening.md` in the `conventions` skill defines it.

## 6. Use-case lifecycle

A use-case moves through three states, and each transition has exactly one
owner:

| State | Meaning | Entered by |
|---|---|---|
| `planned` | declared, not running | `/create-use-case`, or `/onboard-node` capturing something already live |
| `active` | deployed and operational | `/apply-use-case` — the **sole** owner of this transition |
| `retired` | decommissioned, retained for audit | `/remove-use-case` |

`/apply-use-case` handles both the greenfield case and adoption: on an
onboarded node it cuts over reversibly from whatever it replaces — a legacy
container or a host-native systemd unit — rather than assuming it is building
on empty ground. `/modify-use-case` reconfigures a use-case that is
already `active`, re-rendering quadlets and restarting only what changed.

`/onboard-node` triages what a box is already running **before** it reconciles
hardening drift. That ordering is load-bearing: it is what lets firewall
reconciliation preserve and correctly tag the ports the adopted services
depend on, instead of closing them.

## 7. Drift and recovery

### The drift model

`/audit-use-case` is **read-only against the node**. It compares declaration
to reality and reports each finding with a *direction*:

- **`doc-ahead`** — the node is behind the declaration. Reconcile with
  `/modify-use-case`.
- **`node-ahead`** — the declaration is behind the node; someone changed the
  box directly. The audit offers to capture that reality back into the
  document, so the use-case stays rebuildable.

Beyond these there is a third class that no `spec:` block can express:
configuration written through an application's *own* admin UI or CLI. It lands
in a bind mount, or — the dangerous case — in the container's writable layer,
where a rebuild destroys it. The optional `<slug>.audit.yaml` manifest
declares which paths are configuration surfaces and fingerprints
them, which is what makes "changed since the last audit" an answerable
question. The detection scope — everything a use-case declares, and nothing
it does not — is `/audit-use-case`'s contract.

### The recovery model

`drydocc` recovers a node by **reconstruction, not restoration**. There is no
image to roll back to; the fork is the recovery artifact. What that buys, and
what it does not, follows directly from what is and is not in git:

**In git, and therefore recoverable:** the node profile (`node.yaml`,
including discovered `system_facts` and the hardening baseline), every
use-case declaration with its `spec:` block, the secrets *manifests*, the
audit manifests, the backup manifests and targets, and the full journal.

**Not in git, and therefore not recovered by a re-run of `/apply-use-case`
alone:**

- **Secret values** — held in the admin's KeePass. Recovery re-materializes
  `/etc/drydocc/secrets/<slug>.env` from there; if KeePass is lost, the node
  is not recoverable, which makes the KeePass database itself the one thing
  an admin must back up conventionally.
- **Volume data** — application state in podman volumes and bind mounts.
  `/apply-use-case` re-creates the volumes empty; getting the data back is
  `/restore-use-case`'s job, against whatever `/backup-use-case`
  has been sealing to a pluggable rsync or S3-compatible target — see §7a.
- **Quadlets** — rendered from `spec:` on apply, so they need no backup.

Rebuilding is therefore `/bootstrap-node <slug>` on fresh hardware (the
existing `nodes/<slug>/` is reused, and the run enters audit-&-repair mode),
`/apply-use-case` per declaration (rebuild mode), then `/restore-use-case
<slug>` per declaration that carries a `<slug>.backup.yaml`.

`/rebuild-node <node>` runs that sequence as one process and owns none of its
mechanism. It triages the whole node read-only first. That tells it whether
the base is lost, replaced or ready, which use-cases' footprints are absent,
present or partial, and which volumes lost their data. It delegates base
repair to `/bootstrap-node`, which re-derives the replacement box's facts
rather than inheriting the lost box's.

Use-cases are then recovered one at a time in `depends_on` order, each
rebuilt and then restored before its dependents start. A dependent service
therefore never comes up against its dependency's empty data. Afterwards the
run re-arms the unattended work that lived only on the box, rehearses with an
audit, and reports a volume loss inventory. Data counts as lost when the
footprint was gone, when the journal shows a rebuild with no successful live
restore since, or when a backed-up volume is empty. The admin confirms the
plan once. Each delegate honours that confirmation only while its own probe
still shows what the plan showed.

Recovery is a distinct run mode of `/apply-use-case`, not a relaxed refusal.
It is entered when a declaration says `active` and **none** of its
runtime footprint exists on the box — no quadlets, no containers, no declared
volumes, and no legacy object either. Absence must be total: partial absence
is drift, and re-materializing over a half-broken live service is the outcome
the `last_applied` refusal exists to prevent. Machine identity
(`node.yaml:identity`) corroborates but never triggers, because a machine id
also changes for a cloned VM or a re-flashed board. The admin confirms against
the evidence, the journal records `mode: rebuild`, and the run states outright
that volumes came up empty — a rebuild otherwise looks exactly like a
successful first apply until someone opens the application.

**Status:** implemented. `node.yaml:identity` is probed at OS discovery by
`/bootstrap-node` and `/onboard-node`; `/apply-use-case` classifies rebuild in
step 3b and confirms in step 3c before proceeding.

The link back to §2 matters: the recovery path is only real if the
declarations are complete, and a declaration silently drifting out of
completeness is exactly what `/audit-use-case` exists to catch. Every audit
is a rehearsal of the disaster-recovery path — a `not rebuildable` or `lost on
rebuild` finding is a recovery plan reporting that it has stopped working.

### 7a. Backup and restore

Rebuild mode reconstructs a declaration's *effect*; it does not recover its
*data*. `/backup-use-case` and `/restore-use-case` close that second half,
scoped to one use-case at a time, the same unit everything else in the
framework reasons about.

- **Declaration.** An optional `<slug>.backup.yaml` sibling (schema:
  `conventions/references/backup-manifest.md`) names a target, a schedule, a
  retention count, a consistency mode (`stop` or `live`), which declared
  volumes to include, and optional `pre_backup`/`post_restore` hooks. It is
  authored through a conversation, not guessed: `/backup-use-case` analyses
  the use-case's generic shape (mount targets, container topology) and
  proposes a starting point; the admin tunes it.
- **Targets are pluggable and fork-wide.** `nodes/backup-targets.yaml`
  (shared) merged with `nodes/<node>/backup-targets.yaml` (per-node
  overrides/additions) declares each target as `rsync` (a file-share reached
  over rsync-over-ssh) or `s3` (an S3-compatible object store) — exactly
  these two tiers. Credentials follow the same
  KeePass-first contract as any other secret.
- **Verification is not optional.** A snapshot is sealed with a manifest
  recording per-volume digests; `/restore-use-case --test` restores it into
  scratch space and checks the digests before any timer is ever enabled. A
  backup nobody has restored is a hypothesis, not a backup.
- **The automation is on-node only**, like quadlets — a rendered script plus
  a systemd user service/timer, never committed. Once proven, it runs
  unattended; the next agent-invoked `/backup-use-case` call catches the
  journal up from an on-node run log rather than needing to be present for
  every firing.

### 7b. Telemetry and notification

Drift answers "does the node match its declaration?"; telemetry answers "is
the service behaving the way it usually does?". Neither needs a monitoring
daemon.

- **Sampled, not watched.** A one-shot timer counts each container's CPU,
  memory, writable-layer size, restarts, and warning/error log lines every few
  minutes into a node-local samples file. Log lines are counted and discarded.
- **Each service is its own baseline.** `/node-maintenance` learns a per-container
  baseline from a week of samples, excluding known load (the backup window,
  declared maintenance jobs), and keeps it in `<slug>.audit.yaml` beside the
  service's other definitions of *well*. A change to the declaration or the
  image makes the baseline stale; it is rebuilt, never compared against. An
  anomaly never re-bases it.
- **Findings are posture items**, judged at sweep time and reported like any
  other: `resources-within-baseline`, `logs-within-baseline`.
- **Notification is optional and one channel.** `/node-notify` wires a node to
  ntfy: every timer publishes failures, the unattended sweep publishes changes
  in its findings, and each successful run re-arms a scheduled *missed-run
  alarm* on the server — so a timer that stops, or a node that dies, is
  reported by its silence. Topic names are credentials; messages carry ids and
  counts, never content. Run logs on the node stay the record.

## 8. Skill topology

Skills are the framework's executable surface, and simultaneously its
shippable product: the same tree is packaged as distributable agentskills, so
skill bodies stay portable and tool-agnostic — no wrapper commands, no
harness-specific assumptions.

Composition, from the bottom up:

- **`node-context`** binds `<self>` and the surface. Invoked *first* by every
  state-changing skill.
- **`node-exec`** routes every node-touching command according to that
  surface — directly when `on_node`, SSH-wrapped when `on_client`, lingering
  in tmux for long steps.
- **`conventions`** gates any write under `nodes/<node>/` and owns the
  schemas.
- **`secrets`** resolves values just-in-time; **`podman-quadlets`** renders
  `spec:` to unit files.
- **Workflow skills** (`bootstrap-node`, `onboard-node`, `provision-vm`, the
  use-case lifecycle, `audit-use-case`, `backup-use-case`/`restore-use-case`,
  `node-maintenance`, `node-notify`) call all of the above and are the only things an admin
  invokes by name.
- **Orchestrators** (`rebuild-node`) sequence workflow skills and own no
  mechanism of their own — every change they cause is a delegated command's,
  journaled under that command's scope.
- **The lab** (`lab`) runs all of the above against local VMs (§4, "The
  lab"). It is invoked from the admin's own checkout, never on a node.
- **The agent tools** (`scripts/drydocc/`) hold the committed
  implementations of the scriptable steps the skills describe — the quadlet
  renderer, the audit probe, the use-case listing, the resume-pointer
  classification, the lingering launch/poll/cleanup — as one set of
  operations exposed both as MCP tools (`mcp_server.py`) and as a command
  line (`drydo.py`). An operation takes a node name, never a path, and its
  `replay` line is what the journal records. The skills' procedures remain
  the contract.

## 9. Document topology

Where a fact belongs, per §2's *one fact, one level*:

| Level | Holds | Path |
|---|---|---|
| Product description | what it does, for a reader deciding | `README.md` |
| Operator manual | the admin's step-by-step path | `FORKING.md` |
| Architecture | this document — model, principles | `docs/architecture.md` |
| Agent guide | invariants + routing, always in context | `AGENTS.md` |
| Procedure | what each command does, in order | `skills/<name>/SKILL.md` |
| Schemas & commands | exact syntax, field-by-field | `skills/*/references/` |
| Contributing | how to report a problem or propose a change | `CONTRIBUTING.md` |

The authoritative schemas are the `conventions` skill's references:
[use-case documents](../skills/conventions/references/use-case-doc.md),
[node profiles](../skills/conventions/references/node-profile.md),
[journal entries](../skills/conventions/references/journal.md),
[secrets](../skills/conventions/references/secrets.md),
[hardening baseline](../skills/conventions/references/hardening.md),
[audit manifests](../skills/conventions/references/audit-manifest.md),
[backup manifests](../skills/conventions/references/backup-manifest.md),
[backup targets](../skills/conventions/references/backup-targets.md),
[maintenance posture](../skills/conventions/references/maintenance-manifest.md),
[notification routing](../skills/conventions/references/notify-manifest.md),
and [runbook bundles](../skills/conventions/references/runbook-bundle.md).
The distro-agnosticism contract every recipe follows is
[`distro-agnosticism.md`](../skills/conventions/references/distro-agnosticism.md).
Extending a convention is a framework change, not an edit in passing — propose
it upstream, as [`CONTRIBUTING.md`](../CONTRIBUTING.md) describes.

## 10. Status and open questions

Implementation status, including what is not built yet, is the *Status*
section of [`README.md`](../README.md) — consult it rather than any prose
summary here, which is how such a summary goes stale.
