# Secrets Manifest Convention

## Path

Secrets manifests live at `nodes/<node>/use-cases/<slug>.secrets.yaml`, as a sibling to the use-case document at `nodes/<node>/use-cases/<slug>.md`. A secrets manifest is created only when the corresponding use-case document has `has_secrets: true` in its frontmatter. If `has_secrets: false`, no `.secrets.yaml` file should exist.

## Schema

The secrets manifest is a YAML file declaring which environment variables the use-case requires, where they come from (KeePass), where they land on the node, and their rotation schedules:

```yaml
use_case: nextcloud
keepass_entry: "drydocc://vps-fra-01/nextcloud"
target_path: /etc/drydocc/secrets/nextcloud.env
keys:
  - name: POSTGRES_PASSWORD
    purpose: nextcloud-db root password
    rotates: yearly
  - name: NEXTCLOUD_ADMIN_PASSWORD
    purpose: first-boot admin
    rotates: on-first-login
```

### Field Descriptions

- **`use_case`** — Must equal the filename stem (e.g., `nextcloud`). Links the manifest to its corresponding use-case document. Should match `<slug>.md:name`.
- **`keepass_entry`** — The **lookup URL** of the KeePassXC entry holding the secrets: the exact value of that entry's URL field (see "KeePassXC entry setup" below). This is **metadata consumed by the `@max-win-at/keepassxc-secrets` skill** when a value isn't already in the environment — not a raw-CLI argument. The agent never hand-writes `keepassxc-cli`. Grammar and scoping levels: § "URL convention".
- **`target_path`** — Absolute path on the node where the env file lands. Default: `/etc/drydocc/secrets/<slug>.env` (e.g., `/etc/drydocc/secrets/nextcloud.env`). Owner and permissions: `doccap:doccap`, chmod 600. Quadlet files reference this via `EnvironmentFile=<target_path>`.
- **`keys`** — Array of secret definitions. Each key is an environment variable that will be written to the node-side env file:
  - **`name`** — Environment variable name (UPPER_SNAKE_CASE). Examples: `POSTGRES_PASSWORD`, `NEXTCLOUD_ADMIN_PASSWORD`, `API_TOKEN`.
  - **`purpose`** — One-line human description of what the value is for (e.g., "nextcloud-db root password", "first-boot admin account", "external API key for log aggregation"). Helps the admin understand context when rotating.
  - **`rotates`** — Enum declaring the rotation schedule for this secret:
    - `never` — Never rotated; set once at first deployment.
    - `on-first-login` — Generated or set by the service itself on first run; not rotated by the agent.
    - `yearly` — Annual rotation recommended. `/node-maintenance`'s
      `credentials-within-rotation-window` item reports keys past their window
      on every sweep; performing the rotation is this skill's, and is still
      future work (see "Rotation" below).
    - `quarterly` — Quarterly rotation.
    - `monthly` — Monthly rotation.
    - Future: `daily`, `weekly`, or custom intervals.

## KeePassXC entry setup

The `@max-win-at/keepassxc-secrets` skill uses `kpxc-agent`, which queries
KeePassXC via the browser-integration protocol. It matches entries by their
**URL field**, not by title or group path. The admin must set the URL field on
each entry so `kpxc-agent get-logins <url>` finds it.

`kpxc-agent` only ever queries whichever single KeePassXC database is
currently open/unlocked — it cannot search across multiple vaults. A
`get-logins` call for an entry that exists but lives in a *different*,
currently-closed database returns the same empty result as an entry that
doesn't exist at all. See `secrets` skill's resolution protocol step 2 for
how the agent handles that ambiguity with the user.

### URL convention

Every lookup URL has this shape (`[]` mandatory, `{}` optional):

```
[drydocc://][node-name]{/use-case}{/user-or-detail}
```

`keepass_entry` holds the URL itself, and the entry's URL field is set to
exactly the same string — there is no derivation step. The `drydocc://` scheme
is never a real website, so a lookup can never match the admin's ordinary web
credentials.

| Scope | Shape | Example |
|-------|-------|---------|
| Use-case (default) | `drydocc://<node>/<slug>` | `drydocc://vps-fra-01/nextcloud` |
| Global | `drydocc://<service>` | `drydocc://tailscale`, `drydocc://ntfy` |
| Detail | `drydocc://<node>/<scope>/<user-or-detail>` | `drydocc://vps-fra-01/backup/cold-s3`, `drydocc://vps-fra-01/ssh/doccap-at-vps-fra-01` |

- **Node-name** is the node's slug (`node.yaml:name`). A **global** credential,
  shared by every node of the fork, takes the node-name position with the
  service's name instead. A node slug must therefore never equal a global
  credential's name (`tailscale`, `ntfy`, …); refuse such a slug when a node is
  scaffolded.
- **Use-case** is the use-case slug, or a node-level scope that is not a
  use-case: `backup` (one entry per S3 backup target), `ssh` (the node's
  admin-client key).
- **User-or-detail** names a separate entry only where one credential needs its
  own entry — a backup target, a key, a second account. The env-vars of one
  use-case still live together on **one** entry as custom fields (see "Storing
  values in entries"), never one entry per variable.
- **Validation.** A `keepass_entry` that does not start with `drydocc://` is
  the earlier spelling (`drydocc/<rest>`, looked up as
  `https://drydo.cc.local/<rest>`). Refuse it and name the rewrite: the value
  becomes `drydocc://<rest>`, and the admin sets the entry's URL field in
  KeePassXC to that same string.

No type suffixes (`-pat`, `-key`, `-pwd`). The agent knows from context —
the manifest's `keys[]`, `node.yaml:secrets.required_env`, or the invoking
skill — what credential type to expect and which fields to read.

### Organizing entries in KeePassXC

Only the URL field is matched, so groups and titles are the admin's to
choose. A hierarchy mirroring the lookup URLs keeps the vault readable:

```
drydocc/                          ← top-level group
├── tailscale                      ← global entry (URL: drydocc://tailscale)
├── ntfy                           ← global entry (URL: drydocc://ntfy)
└── vps-fra-01/                    ← node group
    ├── nextcloud                  ← entry (URL: drydocc://vps-fra-01/nextcloud)
    ├── grafana                    ← entry (URL: drydocc://vps-fra-01/grafana)
    └── backup/
        └── cold-s3                ← detail entry (URL: drydocc://vps-fra-01/backup/cold-s3)
```

### Storing values in entries

**Single-credential use-cases** (one password, one PAT, one auth key): use
the standard `Username` and `Password` fields. The agent reads whichever
field the context requires (typically `password`).

**Multi-key use-cases** (e.g., nextcloud needs `POSTGRES_PASSWORD`,
`NEXTCLOUD_ADMIN_PASSWORD`, `ETURNAL_SECRET`): store each value as a
**custom string field** on the entry. The field name must match the env-var
name from `keys[].name`. The agent reads each field by name.

```
Entry: nextcloud
  Group:    drydocc/vps-fra-01/
  URL:      drydocc://vps-fra-01/nextcloud
  Custom fields:
    POSTGRES_PASSWORD        = ****
    NEXTCLOUD_ADMIN_PASSWORD = ****
    ETURNAL_SECRET           = ****
```

## Contract and Security

### Values never in git

Secret values are **never** committed to the repository. The manifest declares *structure* and *metadata* only; values live in:
1. The admin's local KeePass database.
2. The node's `/etc/drydocc/secrets/` directory (transient; not version-controlled).

### Node-side env file

When `/apply-use-case` or `/modify-use-case` runs:
1. For each `keys[].name`, the `secrets` skill resolves the value
   **env-var-first** (see "Resolution protocol" below): the environment, then the
   `@max-win-at/keepassxc-secrets` skill, then a user-supplied `.env`.
2. It constructs an env file in memory:
   ```bash
   POSTGRES_PASSWORD=<resolved value>
   NEXTCLOUD_ADMIN_PASSWORD=<resolved value>
   ```
3. The env file is written to the node via the calling skill's `node-exec`
   contract: written locally when the agent runs on the node (VS Code
   Remote-SSH terminal or node-local `claude` CLI), or SSH-wrapped
   (env body piped through, never echoed) when driven from the commissioning
   laptop:
   ```bash
   sudo install -m 600 -o doccap -g doccap /dev/stdin /etc/drydocc/secrets/nextcloud.env
   ```
   (The `install` command atomically writes with correct owner and mode in one step.)

### Quadlet integration

Quadlet container/service files reference the env file via the `EnvironmentFile=` directive:

```ini
[Container]
EnvironmentFile=/etc/drydocc/secrets/nextcloud.env
Environment=NEXTCLOUD_LOGLEVEL=info
```

This pattern loads all secrets from the env file into the container's environment.

## Resolution protocol (env-var-first)

Every secret has a canonical env-var NAME (= `keys[].name`). The
`secrets` skill is authoritative; in summary, to resolve `<NAME>`:

1. **Environment** — if `$<NAME>` is set and non-empty, use it by reference. The
   agent never reads or prints a `.env`.
2. **keepass skill** — else, if `@max-win-at/keepassxc-secrets` is available
   (when it is not, the `secrets` skill § "Preflight: is the keepass skill
   available?" suggests how to install it — never installs it), it passes
   `keepass_entry` — already the lookup URL — to `kpxc-agent get-logins` and
   fetches the value. The agent never hand-writes
   `keepassxc-cli`. Never pass `--json` (or otherwise print the raw entry)
   when only checking for existence — extract the needed field (e.g.
   `--field password`) into a variable instead, so the password never lands
   in tool output or shell history. If no entry matches, ask the user to
   confirm the correct vault is open in KeePassXC and that the entry's URL
   field matches exactly, before falling through to step 3. Full detail:
   `secrets` skill § "resolution protocol".
3. **`.env` fallback** — else, the agent names the missing var(s), prints the
   hidden-paste command from `secrets` § ".env fallback — paste the value", and
   offers two paths:
   - **Exit, load, continue:** `/exit` → `set -a; . ./.env; set +a` → restart →
     `/continue-last-command`. Before pausing, the command marks its
     `.drydocc/last-command.yaml` resume pointer as waiting on those names. Stronger: the value never enters the agent
     session.
   - **Source in place:** the user sources the `.env` in the agent's own shell.
     The agent **must not read the file or print any value** and **warns that
     secrecy cannot be guaranteed** (env vars leak via `/proc`, child processes,
     shell history).

Then the skill builds the in-memory env file, installs it on the node
(`sudo install -m 600 -o doccap -g doccap /dev/stdin <target_path>`), unlinks
the in-memory copy, and **journals that secrets were deployed without the
values**.

## Node runtime secrets

Most secrets belong to one use-case and live in that use-case's env file. Two
other kinds exist, and they differ in one respect that matters: whether the
value may ever touch the node's disk.

| Kind | Declared in | On the node? | Example |
|---|---|---|---|
| Use-case secret | `<slug>.secrets.yaml` | yes — `/etc/drydocc/secrets/<slug>.env` | `POSTGRES_PASSWORD` |
| Session secret | `node.yaml:secrets.required_env` | **never** — held in the agent's process for one run | `DOCCAP_TS_KEY` |
| Node runtime secret | `node.yaml:secrets.required_env` | yes — `/etc/drydocc/secrets/_<purpose>.env` | `DOCCAP_NTFY_ADMIN_TOPIC` |

A **node runtime secret** is node-level, like a session secret, but something
unattended on the node needs it after the agent has gone — a timer publishing a
notification, for instance. It is resolved through the same protocol as every
other secret and installed the same way:

```bash
sudo install -m 600 -o doccap -g doccap /dev/stdin /etc/drydocc/secrets/_notify.env
```

- The file name starts with `_`. Use-case slugs are kebab-case and can never
  begin with one, so a node runtime secret file cannot collide with a
  use-case's env file.
- Its `required_env` entries carry `optional: true` when the feature they serve
  is optional; an unresolved optional runtime secret disables that feature and
  blocks nothing else.
- It is removed only by the skill that installed it, when that feature is torn
  down.

Current node runtime secrets: `_notify.env`, installed by `/node-notify`
(`DOCCAP_NTFY_ADMIN_TOPIC`, `DOCCAP_NTFY_AGENT_TOPIC`, optional
`DOCCAP_NTFY_TOKEN`; global `keepass_entry: "drydocc://ntfy"`). Its schema
is in `references/notify-manifest.md`.

## Rotation

Rotation is split between two commands. `/node-maintenance` **finds** keys that
are due — its `credentials-within-rotation-window` item compares each declared
`rotates:` window against the last recorded rotation and emits an invocation of
this skill per finding. It never rotates anything itself: rotation updates a
vault, rewrites an env file atomically, and reloads consuming services in an
order that depends on the use-case, and doing that from a sweep (least of all an
unattended one) risks leaving a service holding a credential that no longer
works.

Performing the rotation is this skill's, and is **still future work** — the
finding half ships today, the execution half does not. When it lands it will:

1. Identify keys in active use-cases where the `rotates` window has elapsed since the last rotation (e.g. `yearly` and >365 days ago).
2. Prompt the admin to regenerate the value in KeePass (e.g., use `pwgen` or the admin's generator of choice).
3. Redeploy the env file to the node using the same handover flow.
4. Journal the rotation:
   - `scope: use-case/<slug>`
   - `outcome: success`
   - Note which keys were rotated (e.g., "Rotated POSTGRES_PASSWORD").
   - Never include the old or new value.

## Example

See `../assets/example-vps/use-cases/nextcloud.secrets.yaml` for a complete example.
