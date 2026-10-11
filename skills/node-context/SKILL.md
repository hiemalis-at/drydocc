---
name: node-context
description: Resolve which node you are operating on (bind <self>) and enforce the node-scoping guardrail. Use first, before any state-changing action that touches nodes/<node>/, so every subsequent file write is scoped to the right node.
compatibility: Runs inside a drydocc fork; needs a shell and file edits.
metadata:
  skillset: drydocc
  requires: continue-last-command
---

# `drydocc` Node Context

Binds `<self>` — the name of the node the agent is currently acting on — **and**
the **execution surface** (where the agent is running relative to that node).
Every state-changing skill MUST invoke this skill first and use its result both
to scope file writes and to decide how its node commands reach the node (via
`node-exec`).

## Inside a fork

These skills manage nodes through a `drydocc` fork: a git working tree whose
root holds `AGENTS.md` with the heading `` # `drydocc` fork agent guide `` and a
`nodes/` directory. Check that first. Outside one — a skill installed on its own
from a registry, or run in an unrelated project — **refuse** before anything
else, and print *"This is not a `drydocc` fork — its skills manage your nodes
through one. Create your private fork first:
https://github.com/hiemalis-at/drydocc#quickstart"*.

## Resolution order (binds `<self>`)

1. **Explicit override.** If the user invoked a command with `--node <name>`,
   use that. (Several commands also accept the node as a positional first
   argument when run on the client — see "Execution surface" below; the caller
   normalizes that into this override before invoking this skill.)

2. **Node-side identity file.** If `/etc/drydocc/node-name` exists and is
   readable, read it and use its trimmed contents as `<self>`. This is the
   normal path on a bootstrapped node.

3. **Hostname fallback (interactive).** If neither of the above resolves,
   run `hostnamectl --static` (or `hostname` on systems without
   `hostnamectl`). Look for a directory `nodes/<hostname>/` in the repo.
   If found, ask the admin: "Operate as node `<hostname>`? (y/N)". On
   confirmation, bind `<self>`.

4. **Admin-laptop mode.** If no identity file resolves `<self>`, the agent is on
   the admin's commissioning laptop:
   - `/bootstrap-node <slug>`, `/provision-vm <slug>`, `/onboard-node <slug>` —
     set `<self>` to the slug argument (they run client-side before the node has
     an identity file).
   - `/create-use-case`, `/modify-use-case`, `/remove-use-case`,
     `/list-use-cases`, `/audit-use-case`, `/node-maintenance`,
     `/backup-use-case`, `/restore-use-case`, `/node-notify`,
     `/rebuild-node` — set `<self>`
     from the node argument these accept on the client (see "Execution
     surface"). If no node argument was supplied, **refuse**: print
     *"Running on the commissioning client — supply the target node as the first
     argument (e.g. `/create-use-case <node> <slug> …`), or run the command on
     the node."*
   - Any other command with no node argument → refuse and instruct the admin to
     run on the node or pass the node.

## Execution surface (binds `surface` + connection block)

After binding `<self>`, resolve **where the agent is running** relative to it.
`node-exec` consumes this to decide whether to run commands directly or wrap
them in SSH.

- **`on_node`** — `/etc/drydocc/node-name` is present and resolved `<self>`
  (resolution step 2 or 3). The repo working copy, podman, systemd-user, and
  the firewall tool are local. This is the **preferred** surface (VS Code
  Remote-SSH terminal or node-local `claude` CLI).
- **`on_client`** — `<self>` was bound from a `--node`/positional node argument
  on the admin laptop (resolution step 1 or 4) and no identity file is present.
  The node is reached over SSH. Read the **connection block** from
  `nodes/<self>/node.yaml` once and hand it to `node-exec`:
  `tailscale_hostname`, `ssh_fallback_port`, `doccap_user`, and
  `ssh.key_name` (key at `~/.ssh/<key_name>` on the laptop).

The four use-case commands accept the target node as a **positional first
argument** on the client (`/create-use-case <node> <slug> …`); on the node that
first argument is the slug. The discriminator is the surface: an identity file
present means `on_node` (first arg = slug); its absence on the laptop means
`on_client` (first arg = node).

## Resume pointer (binds the run)

Once `<self>` and `surface` are bound for a command that journals (every
state-changing command; not read-only ones such as `/list-use-cases`), record
the run in `.drydocc/last-command.yaml` before the command does anything else.
The schema, and how the run is later judged finished, are in
`continue-last-command` § "Resume pointer". This is what lets a run the agent
never finished — the agent crashed, its server was unreachable, its credits ran
out — be continued.

1. If a pointer exists, its run is unfinished (the pointer is still in
   `state: waiting_on_secret`, or no journal entry since its `started` names
   its `command`), and its `command` + `args` differ from this one, ask before
   replacing it: *"`<command> <args>` from `<started>` did not finish. Run
   `/continue-last-command` first, or drop it and start this command?"*
   `[decision: node-context.replace-unfinished]`. Re-running the same command
   with the same args is the continuation itself and needs no question.
2. Write the pointer: `command`, `args`, `node`, `surface`, `started` (now,
   UTC), `state: running`.

Leave an existing pointer as it is in two cases. The first is a **delegate**
called while the outer run is still unfinished, such as `/rebuild-node` calling
`/bootstrap-node` or `/restore-use-case`. The outer run is the one to continue,
and its re-run skips what is already done. A **hand-off** is different. When
the outer command has already journaled, as `/create-use-case` has by the time
it hands off to `/apply-use-case`, the called command records its own run as
usual and needs no question. The second case is `/continue-last-command`
binding a node to inspect an unfinished run.

## Guardrails (NEVER violate)

- **No cross-node writes.** Never write under `nodes/<X>/` unless
  `<X> == <self>`. The only exceptions are:
  - `/bootstrap-node <slug>` (Phase 0 scaffolds `nodes/<slug>/`),
    `/provision-vm <slug>` (Phase 0 scaffolds `nodes/<slug>/`), and
    `/onboard-node` (lands a fork onto a new node) — all bind `<self>` to
    their slug argument first.
  - Edits to `docs/`, `skills/`, and `.agents/` (framework changes, not node
    state).

- **Refuse on ambiguous binding.** If multiple sources disagree (e.g.,
  `/etc/drydocc/node-name` says `foo` but the user typed `--node bar` or passed
  positional node `bar`), STOP and ask. You cannot drive node `bar` from node
  `foo` — go to the laptop (`on_client`) for cross-node work.

- **Footgun guard (client mode).** When `on_client`, if the resolved node has no
  `nodes/<self>/` directory in the repo, refuse: the admin likely forgot the
  node argument and the slug was misread as a node. Print the corrected usage.

- **Single binding per invocation.** Bind `<self>` once at the start of a
  command and reuse it. Do not re-resolve mid-flow.

## Example use in another skill

> Step 1: Invoke `node-context`. Receive `<self>` + `surface`
>   (+ connection block when `on_client`).
> Step 2: Read `nodes/<self>/use-cases/*.md`.
> Step 3: For any command that touches the node, route it through `node-exec`
>   (it runs directly when `on_node`, SSH-wrapped when `on_client`).
> Step 4: Refuse if asked to touch `nodes/<other>/`.

## What this skill does NOT do

- It does not perform any privileged operation.
- It does not run commands against the node — that is `node-exec`, which
  consumes the `surface` + connection block this skill resolves.
- It does not modify `/etc/drydocc/node-name` — that file is written
  exactly once by `/bootstrap-node`.
- It does not decide whether a run finished or continue one — that is
  `/continue-last-command`; this skill only records the run as it starts.
