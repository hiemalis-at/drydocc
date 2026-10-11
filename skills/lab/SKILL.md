---
name: lab
description: Run drydocc in throwaway local VMs — test skills against disposable nodes with scenario runs judged by assertions, or prototype a node interactively, keep good states as named restore points, and extract it as a replay-verified runbook bundle that builds the same node on real hardware (including an aarch64 board, rehearsed under emulation on an x86 laptop). Use when the admin wants to try something before touching a real box, test a skill change, or turn a prototype into a new node of their fork.
compatibility: Runs inside a drydocc fork; needs a shell.
metadata:
  skillset: drydocc
  requires: conventions
---

# /lab

Answers "can I try this first?" — for a skill change, for a use-case, for a
whole node. The lab is a deterministic driver, `scripts/lab/lab.py`, over a
store of sealed VM layers on the admin's laptop; this skill is when and how to
drive it. Every exact invocation is in
[`references/lab-commands.md`](references/lab-commands.md).

Two kinds of run share one substrate:

- **Test runs** (`run`) start nodes from layers, let a headless agent run the
  steps of a scenario, judge the result only by its assertions, and collect
  everything on a pass. Schema: the `conventions` skill's
  `references/lab-scenario.md`.
- **Prototypes** (`up`) are kept: an admin works in them interactively, keeps
  good states as **restore points**, and **extracts** a node as a runbook
  bundle for a real box. Contract: the `conventions` skill's
  `references/runbook-bundle.md`.

Each run is a fork of the admin's repository — its own worktree, an empty
fleet, `AGENTS.md` exactly as a release ships it, a lab remote as `origin` — so nothing in a run reaches the real fork,
the real remotes, `~/.ssh`, the tailnet or the vault. A runbook bundle,
through `promote`, is the only thing that ever leaves.

## Contract

- **Surface:** the admin's laptop, in the admin's own checkout of the fork —
  never on a node. Inside a prototype's own session only § "Prototype
  session" applies.
- **Args:** a verb and its arguments, as in `references/lab-commands.md`.
  `/lab` alone shows what the lab keeps (`ls`).
- **Host:** the per-user libvirt connection, a libguestfs `virt-customize`, a
  local `git daemon`, and PyYAML or `yq`. At least one **base layer** — an
  empty OS of the wanted distribution, version and architecture — must be in
  the store; § "Base layers".
- **Writes:** only the lab store (`${XDG_DATA_HOME:-~/.local/share}/drydocc-lab`,
  or `$DRYDOCC_LAB_HOME`), the run worktrees under `.worktrees/`, and, on
  `promote`, one new branch `promote/<target>` in the admin's repository.
- **Journal scope:** none of its own outside a run. Inside a run, every skill
  journals as usual; the driver journals the clones, resets and promotions it
  performs as `actor: lab/lab.py`.

## Refusals

1. **Never `promote` an unverified bundle** unless the admin, told that it
   was never replayed (or that its last replay failed), says to anyway.
2. **Never discard what the admin kept** — a restore point, a bundle, a
   prototype before its TTL — without asking. `gc` takes only what is
   collectable by rule: passed runs, expired prototypes, and, with
   `--failed`, failed runs.
3. **Never answer for the admin in a prototype.** Headless steps there need
   explicit `answer`s, exactly as in a scenario.

## Procedure

### Base layers

A run's nodes are clones of sealed layers. The tree grows from **base
layers** — one installed, empty OS per distribution, version and
architecture — and the driver grows **commissioned** layers (base +
`/bootstrap-node`) on demand, keyed on the bootstrap skills, so editing
those rebuilds them. To add a base layer: provision a VM with `/provision-vm`
(an emulated architecture works; the install is paid once), shut it down,
and `layer-import` it as `--kind base`. Pin it.

### Test a skill change

1. Pick or write a scenario: the shipped examples under `lab/scenarios/`
   (`whoami`, `web-db`) show the shape.
2. `run` it — `--repeat N` for a pass rate, since an agent is
   nondeterministic. A run against a ref with a changed bootstrap first
   rebuilds the commissioned layer (minutes on a native guest, much longer
   under emulation); say so before starting.
3. Read the verdict from the report the run names (`report.md`), never from
   the transcript's prose: `pass` is collected, `fail` and `blocked` are kept
   for inspection (`shell` into it), `error` says nothing about the code
   under test — re-run it.

### A real tailnet, ephemerally

Runs keep the tailnet off by default: nodes are hardened, their join is
recorded as owed, and they find each other over a lab network. To exercise
the join itself — and everything that runs on a joined node — a scenario
sets `tailnet: ephemeral`. Its nodes then join the admin's tailnet with a
key the admin made for the lab: ephemeral, pre-approved, reusable, tagged,
and fenced off by an access rule so lab nodes reach nothing else. Setting it
up is a one-time admin step (`references/lab-commands.md` § "Ephemeral
tailnet"); ask the admin for it, never for the key itself in the
conversation. The lab logs its nodes out when it destroys them.

### Prototype a node

1. **Up.** `up <proto> --node <name> …` with the distribution, version and
   architecture. Nodes start from a commissioned layer by default —
   bootstrapped, hardened, no use-cases — or from `base` to prototype the
   bootstrap itself, or from a restore point by name. Tell the admin the
   prototype's TTL (default seven days) and that `ttl` re-arms it.
2. **Work in it.** `shell <proto> --agent <adapter>` starts the admin's agent
   inside the prototype's fork, with its keys and shims — there the admin
   runs `/create-use-case`, `/apply-use-case`, `/audit-use-case` and the rest
   against the lab nodes exactly as against real ones. Alternatively, drive
   steps headless with `agent`, after `answer`ing their decisions.
3. **Keep, discard, continue** — § "Prototype session".
4. **Extract** a finished node: `extract <proto> <node> --as <target>`, with
   `--arch` when the real box differs from the prototype (e.g. prototyped
   natively, deployed to an aarch64 board). The target is the real node's
   name. Show the admin the generated `RUNBOOK.md`: its deltas, its data
   notes, and any check it left out because it did not hold on the
   prototype.
5. **Verify** the bundle: `verify-runbook <bundle>` replays the runbook on a
   fresh base clone named `<target>`, with a headless agent, and stamps
   `runbook.yaml` with the outcome. Steps that need something outside the box
   — a backup target, a notification channel — are not replayed, and the
   stamp says so. A failed replay is kept; show its report.
6. **Promote** a verified bundle: `promote <bundle>` commits `nodes/<target>/`
   on a new branch `promote/<target>` of the admin's repository. Tell the
   admin to review, merge and push it — then build the real box by following
   `nodes/<target>/RUNBOOK.md`.
7. **Clean up** when the admin is done: `destroy <proto>`. Restore points and
   bundles outlive it; `ls` shows them, `layer-rm` removes an unwanted
   restore point.

### Prototype session

In an attended prototype (`lab.attended: true` in the overlay), after each
`/apply-use-case` or `/audit-use-case` that succeeded, ask:

> Keep this state as a restore point, discard everything since the last
> restore point, or continue?

- **keep** — ask for a name; there is no default name, because a name is how
  the admin will recognise this state later (suggest one from the node and
  its use-cases). Run the run's `lab checkpoint <node> --name <name>`. The
  node shuts down, is sealed, and boots again — about a minute, much longer
  under emulation. From then on, `reset` returns here.
- **discard** — destructive: confirm first. Run `lab reset <node>`. Box and
  fork return together; journal entries stay, and the reset journals itself.
- **continue** — do nothing. Also what silence means: nothing is kept unless
  the admin names it.

### Housekeeping

`ls` is the answer to "what is the lab keeping, and until when?" —
prototypes with their age, size and TTL, kept runs, restore points, bundles
and their verification. `tree` shows the layer lineage. `gc --budget <size>`
evicts unpinned leaf layers, least recently used first.

## Example output

```
$ scripts/lab/lab.py ls
PROTOTYPES
  home-proto  age 2h14m  3.1G  expires in 6d21h  nodes: web (web-two-apps)
RUNS
  (none)
RESTORE POINTS
  web-two-apps  of home-proto/web  age 31m  612.0M  pinned  in use by home-proto
BUNDLES
  pi-home-01-20261010-112233  → pi-home-01  from home-proto/web  replay-verified @6b89a6d1c0de on <distro>/<version>/aarch64
```

## What this command does NOT do

- It does not build base layers by itself: a missing one is provisioned with
  `/provision-vm` and imported.
- It does not export data. Volume contents stay in the lab; the runbook lists
  them, and the real box starts empty or restores from the admin's own
  backups.
- It does not export secrets. Lab values are generated per run; the runbook
  lists the KeePass entries to create.
- It does not join a tailnet unless a scenario asks for an ephemeral one,
  and it never reaches a lab node by its tailnet name. Lab nodes reach each other over a
  lab network, and the runbook's deltas say what that changes.
- It does not push. `promote` leaves a local branch for the admin.
- It does not replay steps that need infrastructure outside the box.

## See also

- `conventions` — `references/lab-overlay.md` (what a skill does inside a
  run), `references/lab-scenario.md` (scenarios), `references/runbook-bundle.md`
  (bundles), `references/node-profile.md` § "Box-bound fields".
- `provision-vm` — rootless and emulated VMs, the source of base layers.
- `rebuild-node` — whose recovery order a runbook's applies follow.
