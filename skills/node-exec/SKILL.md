---
name: node-exec
description: Run a command against a node from either execution surface — directly on-node, or SSH-wrapped (with tmux lingering for long steps) from the commissioning client. Use after node-context resolves the surface, whenever a skill must touch the node.
compatibility: Runs inside a drydocc fork; needs a shell.
metadata:
  skillset: drydocc
  requires: node-context
---

# `drydocc` Node Exec

The **single contract** for running a command against a node, in either surface
`node-context` resolves. Every use-case skill routes its node-touching commands
through this contract rather than emitting them raw, so one procedure works on
both surfaces.

Invoke `node-context` **first** — it binds `<self>` and resolves the `surface`
(and, in client mode, the connection block). This skill consumes that result;
it never re-resolves identity.

## Surfaces

`node-context` returns one of:

- **`on_node`** — the agent runs on the node itself (VS Code Remote-SSH
  terminal, or a node-local `claude` CLI in a plain SSH session). The repo
  working copy, podman, systemd-user, and the firewall tool are all **local**.
- **`on_client`** — the agent runs on the admin's commissioning laptop. The
  node is reached over SSH, using the connection block from
  `nodes/<node>/node.yaml`:
  - host: `tailscale_hostname`
  - port: `ssh_fallback_port`
  - user: `doccap_user` (default `doccap`)
  - key: `~/.ssh/<ssh.key_name>` on the laptop

## Running a one-shot node command — `NODE_RUN`

For any short, idempotent command (writes via `install`, `daemon-reload`,
`podman ps`, `systemctl --user …`, the node's firewall-tool command,
`journalctl --user …`):

- **`on_node`** — run `<cmd>` directly, exactly as the skills do today.
- **`on_client`** — wrap it:

  ```bash
  ssh -i ~/.ssh/<key_name> -p <ssh_fallback_port> \
      -o StrictHostKeyChecking=accept-new \
      <doccap_user>@<tailscale_hostname> '<cmd>'
  ```

  Prefer `tailscale ssh <doccap_user>@<tailscale_hostname> '<cmd>'` **when the
  local tailscale daemon is reachable**; fall back to the keyed `ssh` on
  `ssh_fallback_port` otherwise (e.g. under WSL2, where `tailscaled` runs on the
  Windows host). This mirrors `bootstrap-node` Phase 4's cut-over routine.

### Piping content to a node command

Quadlet ini text, env-file bodies (`secrets` § "Use-case secrets → node env
file"), and any other stdin payload are piped **through** the wrapper unchanged:

- **`on_node`**:

  ```bash
  printf '%s\n' "<text>" | install -m 644 /dev/stdin <path>
  ```

- **`on_client`** (the local `printf` feeds the remote `install` over SSH):

  ```bash
  printf '%s\n' "<text>" | ssh -i ~/.ssh/<key_name> -p <ssh_fallback_port> \
      -o StrictHostKeyChecking=accept-new \
      <doccap_user>@<tailscale_hostname> 'install -m 644 /dev/stdin <path>'
  ```

  Secret-bearing stdin (env files) follows the same shape but with
  `sudo install -m 600 -o doccap -g doccap /dev/stdin <path>` on the remote
  side. The value is never echoed, never journaled (`secrets` rules still hold).

## Running a long-running node command — `NODE_RUN_LINGER`

Steps that can outlive a single SSH invocation — large `podman pull`, use-case
payload downloads, in-between backups, database initialization — must **survive
an SSH disconnect** in `on_client` mode. They run as a **detached, named job**
on the node; the agent polls for completion and captures the log. On the node
surface, lingering is unnecessary (run inline). See `references/lingering.md`
for the full recipe; the contract:

`lingering_launch`, `lingering_poll` and `lingering_cleanup`
(`podman-quadlets` § "The drydocc tools") run this recipe from the node name
alone: they read the connection block from `nodes/<self>/node.yaml` and pick
tmux or nohup from `system_facts.capabilities.tmux`. The recipe remains the
contract.

- Session/log name is `dd-<slug>-<step>` (derived from the use-case slug + a
  short step label) so a re-run **re-attaches** to an in-flight job instead of
  relaunching it — this is what makes the long step resumable across a dropped
  connection.
- The job records a `__DONE_<rc>__` sentinel on its own last line so the poller
  can read the real exit code, not just "session gone".
- The agent surfaces progress by tailing `/tmp/dd-<slug>-<step>.log`.

## When to linger vs run inline

| Step kind | Idiom |
|-----------|-------|
| Quadlet/env-file writes, `daemon-reload`, `systemctl enable/restart`, `podman ps`, firewall rules, `journalctl` capture | `NODE_RUN` (inline) |
| `podman pull <pinned-image>`, payload download, DB init/migration, backup/rsync | `NODE_RUN_LINGER` (on_client) / inline (on_node) |

When in doubt, a step that typically completes in seconds runs inline; a step
that can take minutes and produces a growing log lingers.

## Guardrails

- **Never re-resolve identity here.** `<self>` and `surface` come from
  `node-context`. If a skill reached this contract without a resolved surface,
  that is a bug — stop and invoke `node-context`.
- **One connection block per run.** Read `tailscale_hostname`,
  `ssh_fallback_port`, and `ssh.key_name` from `nodes/<node>/node.yaml` once;
  reuse for every command in the invocation.
- **Secrets discipline is unchanged.** The wrapper transports stdin; it does not
  relax any `secrets` rule. No secret value is echoed, logged, or journaled —
  only that a command ran.
- **Journal the effective command.** Journal the command as actually issued
  (including the SSH wrapper in client mode), per the `conventions` journal
  schema, so the journal stays a faithful replay record on either surface.
- **Surface failures honestly.** If the SSH wrapper can't reach the node (host
  down, key rejected) or the remote command exits non-zero, surface the stderr
  verbatim and stop — do not retry silently or fall through. The calling
  use-case skill journals the step `outcome: partial` and halts, exactly as it
  would for a failed local command on-node. (Long-running jobs: failure
  semantics are in `references/lingering.md`.)

## What this skill does NOT do

- Decide *which* node — that is `node-context`.
- Perform privileged operations itself; it defines only how the calling skill's
  commands reach the node.
- Apply output-filtering or other command wrappers — the agent applies those
  at execution time per its project instructions; they are never written into
  the contract.
