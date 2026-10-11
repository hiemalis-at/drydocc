---
name: secrets
description: Resolve and materialize drydocc secrets env-var-first — the environment, then KeePassXC, then a user-supplied .env. Use whenever a secret, tailscale auth key, root or VM password, or per-node SSH key must be obtained or written.
compatibility: Runs inside a drydocc fork; needs a shell.
metadata:
  skillset: drydocc
  requires: conventions node-context continue-last-command
---

# `drydocc` Secrets

This skill defines the **single protocol** the agent follows to obtain any
secret — session secrets it consumes in memory (tailscale/GitHub tokens, initial
root and VM passwords) and use-case secrets it writes to the node's
`/etc/drydocc/secrets/<slug>.env`. **Secret values never enter git.** Only
manifests (key names + purposes) and var-name registries do.

Every secret has a canonical **environment-variable NAME** (`DOCCAP_TS_KEY`,
`DOCCAP_ROOT_PW`, `POSTGRES_PASSWORD`, …). There are exactly two supported ways to
supply one, unified behind a single env-var-first lookup:

1. **Best practice** — the `@max-win-at/keepassxc-secrets` agentskill fetches it
   from KeePassXC. The agent relies on that skill's tested handshake and **never
   hand-writes `keepassxc-cli`.**
2. **Fallback** — the admin pastes the value into a local `.env` file, which
   they load into the environment.

## Preflight: is the keepass skill available?

Run once, early, in any command that may need a secret. Non-fatal, and it
**never installs anything** — adding a skill to the admin's agent is the
admin's decision.

1. Is the `@max-win-at/keepassxc-secrets` skill installed / available? → record
   `keepass_skill: available` and stop.
2. Otherwise record `keepass_skill: unavailable`, tell the admin once how to
   install it, and carry on:

   > KeePassXC secrets are not set up. To fetch secrets from your vault next
   > time, install the `keepassxc-secrets` skill:
   > - skills.sh: `npx skills add max-win-at/keepassxc-cli-integration --skill keepassxc-secrets`
   > - agentskill.sh: `/learn @max-win-at/keepassxc-secrets`
   >
   > For now, any secret not already in the environment is pasted into a
   > `.env` file (I'll show you how when one is needed).

## The resolution protocol (env-var-first)

To resolve a secret with env-var NAME `<NAME>`:

1. **Environment.** If `$<NAME>` is set and non-empty, use it **by reference
   only**. Do not read or `cat` any `.env`. Do not echo the value.
2. **keepass skill.** Else, if `keepass_skill: available`, derive the lookup
   URL from the `keepass_entry` recorded in the manifest (use-case secrets) or
   `node.yaml:secrets.required_env` (session secrets):

   ```
   drydocc/<rest>  →  https://drydo.cc.local/<rest>
   ```

   Then invoke `@max-win-at/keepassxc-secrets` with that URL to fetch `<NAME>`.
   For multi-key entries, read the field matching `<NAME>` (custom string field
   on the KeePassXC entry). See `conventions` → `references/secrets.md` §
   "KeePassXC entry setup" for the full URL convention.

   **No `--json` for existence checks.** Never pass `--json` (or otherwise let
   the raw entry print unfiltered) when checking whether an entry exists or
   fetching a value — the full entry, password included, would land in the
   Bash tool's visible output and shell/session history. Use `--field
   password` (or whichever field is needed) captured into a shell variable
   via `$(...)`; never print it. Reserve `--json` for disambiguating multiple
   matches programmatically, and even then keep it piped into `jq`/a
   variable, never bare stdout.

   **Not-found handling.** If `get-logins` returns an empty/zero-count
   result, that is indistinguishable from "wrong vault currently open" —
   `kpxc-agent` only ever queries the single database KeePassXC currently
   has unlocked, it cannot search across multiple vaults. Before falling
   through to the `.env` fallback (step 3), tell the user:
   - Confirm the KeePass **vault containing this entry is the one currently
     open/unlocked** in KeePassXC (switching the active database is a
     manual step in the app).
   - Confirm the entry's **URL field** is set exactly to the derived lookup
     URL (`https://drydo.cc.local/<rest>`) — title and group path aren't
     matched, only the URL field.

   Retry step 2 once the user confirms; if they confirm the entry genuinely
   doesn't exist, proceed to the `.env` fallback.
3. **.env fallback.** Else, name the exact missing var(s) and what each is for,
   print the paste command below, and offer the two `.env` paths after it.

### .env fallback — paste the value

The admin writes the value into `<fork>/.env` (the fork root's `.env`, which is
gitignored) without it appearing on screen, in shell history, or in this
session. Print this with the real path and variable names filled in:

```bash
# Paste one NAME='value' line per secret — DOCCAP_TS_KEY='…' — then Enter and Ctrl-D.
# Your input is hidden. Single quotes keep characters like $ or ! literal.
(umask 077; trap 'stty echo' EXIT INT TERM; stty -echo; cat >> <fork>/.env); chmod 600 <fork>/.env
```

It appends, so earlier keys in the file survive and a repeated key's last line
wins. The agent never reads the file or asks for the value in chat.

### .env fallback — two paths

**(a) Exit, source, continue** *(keeps the value out of the agent's session;
stronger)*:

> The secret `<NAME>` isn't set. Paste it into `.env` with the command above, then:
> 1. `/exit` this agent.
> 2. `set -a; . ./.env; set +a` in the shell, so every line is exported to
>    the agent you start next.
> 3. Restart the agent and run `/continue-last-command` — I'll pick up where I
>    left off.

Before pausing, the command marks its resume pointer `state: waiting_on_secret`
with the names in `waiting_on` (`continue-last-command` § "Resume pointer").

**(b) Source in place** *(convenient; weaker secrecy)*: the user loads the
`.env` into the agent's own shell without exiting (`set -a; . ./.env; set +a`).
Rules:

- The agent **must not read the `.env` file** and **must not print** any value.
- After the user confirms they have sourced it, **re-run step 1** of the
  protocol (the var is now in the environment).
- **Warn explicitly:** sourcing in-place attempts secrecy but **cannot guarantee
  it** — env vars are visible to child processes, may appear in
  `/proc/<pid>/environ`, and can leak into shell history. Path (a) is stronger.

## Use-case secrets → node env file

For a use-case with `has_secrets: true`:

1. Invoke `node-context`. Receive `<self>`.
2. Read `nodes/<self>/use-cases/<slug>.secrets.yaml`. Validate against
   `conventions`.
3. For each `key` in the manifest, resolve its value via the protocol above
   (env-var NAME = `key.name`; `keepass_entry` from the manifest). The agent
   assembles an in-memory env-file string of the shape:

   ```
   <KEY1>=<value1>
   <KEY2>=<value2>
   ```

4. Write atomically to the node-local target:

   ```bash
   cat <<'EOF' | sudo install -m 600 -o doccap -g doccap /dev/stdin <target_path>
   <env contents>
   EOF
   ```

   The use of `install` ensures atomic write + correct mode/owner.

5. Reload the consuming quadlet(s):

   ```bash
   systemctl --user daemon-reload && systemctl --user restart <unit>
   ```

6. Append a journal entry under `nodes/<self>/journal/<ts>-secrets-<slug>.md`
   with `scope: use-case/<slug>`, listing WHICH keys were written/rotated
   (NEVER the values), and the rotation reason.

The env-file write (step 4) and the reload (step 5) go through the calling
skill's `node-exec` contract: when the agent runs **on the node**
(VS Code Remote-SSH terminal or node-local `claude` CLI) the
`sudo install … /dev/stdin` and `systemctl --user` run locally; when it runs
**on the commissioning client**, the same commands are SSH-wrapped (the env body
is piped through the wrapper, never echoed). This skill defines *what* the env
file contains; `node-exec` defines *how* it reaches the node.

## What never enters git or the journal

- Plaintext values
- Hashes of plaintext values
- Anything derived from the values
- The contents of a user-supplied `.env`

## Rotation

**Not automated.** `/node-maintenance` reports keys past their declared
`rotates` window; nothing performs a rotation end to end yet (the `conventions`
skill's `references/secrets.md` § "Rotation" says why). Rewriting the env file
with a new value is this skill's ordinary write — but for most keys the
application itself must also learn the new value (a database password lives in
the database, not just in the env file), and that order is use-case specific.
So a rotation is the admin's, step by step: change the credential in the
application, store the new value in KeePass, then rewrite the env file per
§ "Use-case secrets → node env file", which restarts its consumers and
journals which keys rotated and why — never the values.

## Failure modes to handle gracefully

- The keepass skill returns an error → surface it to the user; offer the `.env`
  fallback. Do not retry silently.
- `sudo install` fails (permissions / disk full / target dir missing) → surface
  the error verbatim; do not proceed to step 5 (reload).
- `target_path` already exists and differs → ask the user whether to overwrite;
  default no.
- (Session secrets) `tailscale ssh` fails → suggest the user fall back to the
  keyed ssh on `ssh_fallback_port`; do not proceed.

## Session secrets (bootstrap / onboard)

`/bootstrap-node` and `/onboard-node` need values before any per-use-case
manifest exists. Their env-var names live in `node.yaml:secrets.required_env`
(see `conventions`), each carrying a `keepass_entry` for protocol
step 2.

- `DOCCAP_TS_KEY` — required for the tailnet join. Resolved just-in-time in Phase 3
  (immediately before `tailscale up`). `keepass_entry`: `drydocc/tailscale`
  (infra-global; URL: `https://drydo.cc.local/tailscale`).

Fork access is **not** a session secret. A node pushes its own commits, so it
needs write access, and a token placed in a clone URL is stored in plaintext
in the working copy's `.git/config`. Instead each node generates its own
deploy key on the node; the private half never leaves it, and the admin
registers the public half on the git host
(`bootstrap-node/references/hardening-recipes.md` § "Fork write access").

Resolve just-in-time: a run that dies in Phase 2 must never have prompted for a
value it would not use.

Once resolved, a session secret is held in a shell env var in the agent's own
process — **never written to the node disk, never echoed, never journaled.** It
is passed to the remote shell via heredoc + env passthrough:

```bash
ssh <initial> "DOCCAP_TS_KEY='$DOCCAP_TS_KEY' bash -s" <<'REMOTE'
  tailscale up --authkey "$DOCCAP_TS_KEY" --hostname <tailscale_hostname> --ssh
REMOTE
```

Quoting matters: the outer `'$DOCCAP_TS_KEY'` is single-quoted on the agent side so the
value is interpolated by the client shell; the inner `"$DOCCAP_TS_KEY"` is double-quoted
so the remote shell reads the exported env var. After the run, the agent unsets
`DOCCAP_TS_KEY`. The journal records that a secret was read and used
(e.g. "resolved DOCCAP_TS_KEY for tailnet join"), never the value.

## Node runtime secrets

Some node-level secrets are needed by unattended jobs on the node after the
agent has gone, so unlike session secrets they **are** written to the node —
once, by the skill that owns the feature, as `/etc/drydocc/secrets/_<purpose>.env`
(`install -m 600 -o doccap -g doccap`, the same handover as a use-case env
file). They are listed in `node.yaml:secrets.required_env` with
`phase: runtime`, and resolved through the same protocol above.

- `DOCCAP_NTFY_ADMIN_TOPIC`, `DOCCAP_NTFY_AGENT_TOPIC`, optional
  `DOCCAP_NTFY_TOKEN` — notification topics and token, installed by
  `/node-notify` as `_notify.env`. `keepass_entry`: `drydocc/ntfy`
  (infra-global; URL: `https://drydo.cc.local/ntfy`), one custom field per
  variable. A topic name is a credential: on a public server anyone who knows
  it can read and publish. Generate each one — never let the admin pick a
  memorable name:

  ```bash
  printf 'dd-%s\n' "$(head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  ```

Schema and the distinction from session secrets:
`conventions/references/secrets.md` § "Node runtime secrets".

## Initial root password

`/bootstrap-node` Phase 2 connects to the bare box as `root` over a **temporary
password**, resolved through the protocol as `DOCCAP_ROOT_PW`:

- **`source: running`** — the user supplies the box's temporary root password
  (env-var `DOCCAP_ROOT_PW`, or pasted interactively). The agent passes it to `ssh` via
  `sshpass -p "$DOCCAP_ROOT_PW"` — or, when `sshpass` is absent, the `SSH_ASKPASS`
  helper documented in the `bootstrap-node` skill's
  `references/hardening-recipes.md` § "Connection model across phases".
- **`source: vm`** — the password is the one the `provision-vm` skill generated
  and baked into the VM's unattended-install seed; it is reused as
  `DOCCAP_ROOT_PW`.

Rules: never echoed, never written to the node disk, never journaled, never
committed. Unset at the end of Phase 6 alongside
`DOCCAP_TS_KEY`.

## VM initial password

When provisioning a VM, the rendered unattended-install seed file embeds the
initial root password (it must, to seed first-boot SSH). That file therefore
lives only in a `mktemp -d` gitignored temp directory at mode `0600`, is used
to build the seed ISO, and is deleted once the install starts. The temp dir
and `iso/` are gitignored. The plaintext never enters git or the journal.

## SSH key storage

`/bootstrap-node` Phase 2 establishes a **dedicated per-node keypair**
`doccap-at-<slug>`. The private key is an admin-client secret, not
git data (invariant 3):

- **`key_source: generated`** — `ssh-keygen` writes the private key to
  `~/.ssh/doccap-at-<slug>` (0600) on the **admin laptop**. The admin then
  stores it in KeePass under `drydocc/<infra>/ssh-keys/doccap-at-<slug>`
  (attachment or private-key field) so it survives laptop loss — a manual admin
  step, not an agent action.
- **`key_source: keepass-agent`** — the admin already holds the desired key in a
  KeePassXC-backed `ssh-agent`; nothing is written to `~/.ssh`. The agent only
  reads the public key + fingerprint from `ssh-add -L`.

`node.yaml:ssh` records **metadata only** — `key_name`, `key_algorithm`,
`key_source`, `public_key_fingerprint`. The private key never enters git, the
journal, or the node disk (only its public half lands in the node's
`authorized_keys`).

## What this skill does NOT do

- Rotate a credential end to end — see § "Rotation".
- Hand-write `keepassxc-cli`. KeePassXC access is the keepass skill's;
  this skill only calls it.
- Read or print a user-supplied `.env`, or ask for a secret value in chat.
- Install the keepass skill, or any other skill or tool — it suggests, the
  admin installs.
- Hold the node's fork deploy key. Each node generates its own, and its
  private half never leaves the node (`bootstrap-node`
  `references/hardening-recipes.md` § "Fork write access").
- Decide which secrets a use-case needs — that is its `<slug>.secrets.yaml`,
  authored by `/create-use-case`.
- Back up the KeePass database. It is the one thing a rebuild cannot
  recover, and it is the admin's to back up conventionally.
