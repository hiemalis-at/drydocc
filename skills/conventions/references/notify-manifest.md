# Notification Manifest Convention

## Path

Notification routing is declared in two layers, merged at read time — the same
shape `backup-targets.yaml` and `maintenance.yaml` use:

- **`nodes/notify.yaml`** — the fork-wide channel and routing. Lives under
  `nodes/` so the generic layer stays framework-only.
- **`nodes/<node>/notify.yaml`** — this node's overrides. Optional; a node
  with no file inherits the fork-wide routing exactly. It also carries the
  skill-written half.

Both are authored by `/node-notify --setup` and tuned by the admin afterwards.
**Neither holds a secret value.** Topic names and the access token are
credentials — on the public server, anyone who knows a topic name can read it
and publish to it — so the manifest names only the environment variables that
hold them.

Notification is **optional**. A fork with no `nodes/notify.yaml` sends
nothing, and every rendered timer script runs exactly as it would without it.

## Merge rule

1. Read the fork file. If it does not exist, notification is off for every
   node and nothing below applies.
2. Read the node file if present.
3. Scalars (`server`, `token_env`, `heartbeat.*`, `audiences.<name>.topic_env`)
   in the node file override the fork file's, field by field.
4. For each `events[]` entry in the node file, match by `id` against the fork
   list: same `id` overrides field by field. An `id` outside the event
   vocabulary below is a refusal naming the id and its file — silently
   ignoring it would drop a signal the admin believes is routed.
5. A vocabulary `id` absent from both files takes its default from the table
   below.

## Schema

```yaml
# nodes/notify.yaml — the fork-wide layer
schema: 1

# ─── authored by /node-notify --setup and admin tuning; safe to hand-edit ───

channel: ntfy                       # closed: ntfy
server: https://ntfy.sh             # or a self-hosted instance
token_env: DOCCAP_NTFY_TOKEN        # optional; omit for anonymous publishing
audiences:
  admin:
    topic_env: DOCCAP_NTFY_ADMIN_TOPIC
  agent:
    topic_env: DOCCAP_NTFY_AGENT_TOPIC
heartbeat:
  grace: 2h                         # added to a job's interval before its alarm fires
  rearm_every: 6h                   # at most one re-arm per job per this period
events:
  - id: run-missed
    to: [admin]
    priority: 5
  - id: run-failed
    to: [admin, agent]
    priority: 4
  - id: telemetry-anomaly
    to: [admin, agent]
    priority: 4
  - id: maintenance-drift
    to: [agent]
    priority: 3
  - id: admin-attention
    to: [admin]
    priority: 4
  - id: test
    to: [admin, agent]
    priority: 3
```

```yaml
# nodes/<node>/notify.yaml — this node's layer
node: vps-fra-01
schema: 1

# ─── authored by /node-notify --setup and admin tuning; safe to hand-edit ───

events:
  - id: maintenance-drift
    to: [admin, agent]              # this node's drift also pages the admin
    note: "production box — drift is worth a push"

# ─── written by /node-notify; do not hand-edit ───

helper_rendered_commit: null
last_test: null
last_test_outcome: null
```

## Field descriptions

### Authored half

- **`node`** — Node layer only. Must equal the containing directory name and
  `node.yaml:name`.
- **`schema`** — Integer. Currently always `1`.
- **`channel`** — Closed enum; the only value is `ntfy`. A second channel is
  out of scope — raise it with the admin as a framework change.
- **`server`** — Base URL of the ntfy server, no trailing slash. Default
  `https://ntfy.sh`.
- **`token_env`** — Optional. Name of the environment variable holding an
  access token, sent as a bearer token. Needed only on a server that enforces
  authentication.
- **`audiences.admin.topic_env`**, **`audiences.agent.topic_env`** — Names of
  the environment variables holding each audience's topic. `admin` is the
  topic the admin's phone subscribes to; `agent` is the topic an agent polls.
  The two must hold different values.
- **`heartbeat.grace`** — Duration added to a job's interval before its
  missed-run alarm fires. Default `2h`. Covers `RandomizedDelaySec` and a slow
  run.
- **`heartbeat.rearm_every`** — Duration. A job re-arms its alarm at most
  once per this period, and lengthens the alarm's delay by the same amount to
  compensate. Default `6h`. This is what keeps a short-interval job inside the
  public server's daily message allowance.
- **`events[]`** — Routing for the closed event vocabulary.
  - **`id`** — One of the vocabulary ids below.
  - **`to`** — List of audiences: `admin`, `agent`, both, or `[]` to silence
    the event.
  - **`priority`** — `1`–`5`, ntfy's scale (`3` is default, `5` is urgent).
  - **`note`** — Free text, human-read only. Encouraged on any node-layer
    override and on any `to: []`.

### Event vocabulary

| id | Raised when | Published by | Default `to` | Default priority |
|---|---|---|---|---|
| `run-missed` | a timed job did not complete within its interval + grace | the server (a pending alarm delivered), or the maintenance script for jobs too long to arm | `[admin]` | 5 |
| `run-failed` | a timed job exited non-zero | that job's script | `[admin, agent]` | 4 |
| `telemetry-anomaly` | a telemetry finding is newly raised, escalated, or cleared | the maintenance script or an interactive sweep | `[admin, agent]` | 4 |
| `maintenance-drift` | an unattended sweep's set of DRIFT items changed | the maintenance script | `[agent]` | 3 |
| `admin-attention` | an interactive run is blocked waiting on the admin | the agent | `[admin]` | 4 |
| `test` | `/node-notify --setup` or `--test` | the agent | `[admin, agent]` | 3 |

The vocabulary is closed for the same reason the maintenance catalog is: an
event nothing raises is a promise nothing keeps.

### Skill-written half

Node layer only. `/node-notify` owns these.

- **`helper_rendered_commit`** — Short SHA of the merged manifest state the
  on-node helper was rendered from. When the merged manifest no longer matches,
  the helper is stale and the next `/node-notify` run re-renders it.
- **`last_test`** — ISO-8601 UTC timestamp of the most recent verified test
  message. `null` until one succeeds.
- **`last_test_outcome`** — `success` or `failed`.

## Secrets

The variables named by `token_env` and `audiences.*.topic_env` resolve through
the `secrets` skill like every other secret, from one global KeePass
entry, `drydocc://ntfy` (title `ntfy`), with one `KPH: <NAME>` custom field per variable. They are listed in
each node's `node.yaml:secrets.required_env` with `optional: true`.

Unlike session secrets, they must be present **on the node**, because
unattended timers publish. They are a node runtime secret, installed as
`/etc/drydocc/secrets/_notify.env` — see `references/secrets.md` § "Node
runtime secrets".

Topic names are generated, never chosen: a guessable topic on a public server
is an open one.

## Contract

- **Message content is not secret-safe.** A message leaves the infrastructure.
  It carries node, job, event id, verdict, counts, and posture ids only —
  never a secret value, a log line, an IP address, a ban source, or
  certificate contents.
- **Notification never fails a job.** A publish that cannot be delivered is
  recorded in the node-local `notify.log` and the calling job continues.
- **The server is a signal, not a record.** The public server keeps delivered
  messages for about 12 hours. Run logs and `notify.log` on the node remain
  authoritative; the journal is written from those.

## Lifecycle

| Event | Effect |
|---|---|
| First `/node-notify --setup` in the fork | Authors `nodes/notify.yaml` with the default routing above |
| `/node-notify --setup` on a node | Authors the node layer, installs `_notify.env`, renders the helper, sends and verifies a `test` message, sets `helper_rendered_commit` and `last_test*` |
| `/node-notify --test` | Sends and verifies a `test` message; updates `last_test*` |
| Manifest changed since `helper_rendered_commit` | Next `/node-notify` run re-renders the helper |
| Any timer's automation purged | That job's alarm is disarmed |

## References

- Publishing, arming, polling, and the helper:
  `skills/node-notify/references/notify-recipes.md`.
- The merge this file copies: `references/backup-targets.md`.
