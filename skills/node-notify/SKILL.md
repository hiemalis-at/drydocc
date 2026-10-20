---
name: node-notify
description: Wire a node to an ntfy push-notification channel — failed timer runs, telemetry anomalies, maintenance drift, and missed-run alarms that fire when a timer or the whole node goes silent. Sets up topics, secrets, and the on-node helper every drydocc timer publishes through; sends a verified test message; reports which alarms are armed. Optional — nothing else requires it. Use to get push alerts from a node.
compatibility: Runs inside a drydocc fork; needs a shell and file edits.
metadata:
  skillset: drydocc
  requires: conventions secrets node-context node-exec
---

# /node-notify

Answers "if something on this node breaks at 3 a.m., or the node stops
existing, who finds out — and when?"

Without it, the answer is "whoever next runs a command against the node".
Every drydocc timer already records its outcome in a run log, and the next
interactive run journals it; that is a sound record and a slow signal. Worse,
a node that has died writes no run log at all, so its silence is never
noticed. This command adds one push channel and one dead man's switch: every
successful timed run **re-arms a scheduled message on the ntfy server**, and
if the next run does not happen in time, the server delivers it.

The channel is optional. With no `nodes/notify.yaml`, every other command runs
exactly as it would without this one.

## Contract

- **Args:**
  - On the node: `[--setup | --test | --status]` — no flag means `--status`.
  - On the commissioning client: `<node> [--setup | --test | --status]` — the
    target node is the **positional first argument** (required in client mode;
    `node-context` refuses without it).
  - `--setup` authors or refreshes the manifest, installs the secrets, renders
    the helper, sends a verified test message, and arms alarms for the timers
    already on the node. Re-running it re-renders; it never regenerates topics
    that already resolve.
  - `--test` sends a `test` event to every audience and verifies it arrived.
  - `--status` is read-only: armed alarms, recent `notify.log` lines, the last
    test. It writes nothing and journals nothing.
- **Surface:** either surface, resolved by `node-context`. Every on-node
  command routes through `node-exec` as a `NODE_RUN`. Publishing and polling
  from the agent's side use the same HTTP calls as the node.
- **Reads:** `nodes/notify.yaml` + `nodes/<self>/notify.yaml` (merged,
  `conventions/references/notify-manifest.md`); `nodes/<self>/node.yaml`;
  `nodes/maintenance.yaml` + node layer and `nodes/<self>/use-cases/*.backup.yaml`
  (to know which timers should be armed); the live user timers named
  `drydocc-*`.
- **Writes (in-repo):** `nodes/notify.yaml` (first-run walkthrough only);
  `nodes/<self>/notify.yaml`; the ntfy entries in
  `nodes/<self>/node.yaml:secrets.required_env` — those entries and nothing else
  in that file; the journal entry.
- **On-node side effects:** `/etc/drydocc/secrets/_notify.env` (0600,
  `doccap`); the rendered helper `~/.config/drydocc/notify.sh`;
  `/var/lib/drydocc/notify.log`; pending alarm messages on the ntfy server.
- **Journal scope:** `node`.

## Refusals

1. **No `node.yaml` for `<self>`** — `<self> has no node profile. Run
   /bootstrap-node <self> first.`
2. **No HTTP client on the node** — `Refusing to set up notification: the node
   has no HTTP client to publish with.` Probe per
   `references/notify-recipes.md` § "Capability"; name the missing capability,
   never the system.
3. **`channel` other than `ntfy`**, or an event `id` outside the vocabulary in
   either manifest layer — name the value and the file. A second channel or a
   new event is out of scope — raise it with the admin as a framework change.
4. **Admin and agent topics resolve to the same value** — `The admin and agent
   topics are identical. The agent topic would page the admin with every
   routine message; generate a second topic.`
5. **`--test` or `--status` before setup** — `<self> has no notification set up.
   Run /node-notify <self> --setup.`

## Procedure

1. **Bind `<self>` + surface** via `node-context`. The snippets in
   `references/notify-recipes.md` are the `on_node` form.

2. **Resolve the manifest.** Merge the two layers per
   `conventions/references/notify-manifest.md` § "Merge rule". If the fork-wide
   file does not exist and this is `--setup`, author it at the default routing
   shown in that schema, then continue. Apply refusals 3 and 5.

3. **Probe the capability** per § "Capability". Refusal 2 if absent.
   Stop here for `--status`, render the report per § "Status report", and
   exit.

4. **Resolve the secrets** (`--setup`). Resolve each variable named by
   `audiences.*.topic_env` and `token_env` through the `secrets` skill. For any
   topic that does not resolve:
   - generate it per the `secrets` skill § "Node runtime secrets" — never
     accept a memorable name;
   - tell the admin exactly which KeePassXC entry and custom fields to create
     (URL `drydocc://ntfy`, title `ntfy`, one `KPH: <NAME>` field per
     variable). Custom fields are created by hand: `kpxc-agent set-login`
     writes only a username and password;
   - re-resolve before continuing.

   Apply refusal 4. Install `/etc/drydocc/secrets/_notify.env` per
   `conventions/references/secrets.md` § "Node runtime secrets", and add any
   missing ntfy entries to `node.yaml:secrets.required_env` with
   `phase: runtime`, `optional: true`.

5. **Render the helper** per § "The rendered helper" when it is absent or the
   merged manifest no longer matches `helper_rendered_commit`. Verify it parses
   (`bash -n`) before going on.

6. **Send and verify a test** (`--setup`, `--test`) per § "Publishing an event"
   and § "Verifying delivery". Tell the admin how to subscribe their phone to
   the admin topic on the configured server, and ask them to confirm the push
   arrived; a message the server holds but no phone shows is a subscription
   problem, and saying so saves an hour. Record `last_test` and
   `last_test_outcome` either way.

7. **Arm the existing timers** (`--setup`). For every enabled `drydocc-*`
   timer on the node, arm its alarm now per § "Arming a missed-run alarm"
   rather than waiting for its next run, and verify each per § "Verifying an
   armed alarm". A job whose interval is too long to arm is listed as covered
   by the maintenance sweep instead (§ "Jobs too long to arm"), and flagged
   if the maintenance timer itself is not enabled — that job would have no
   watcher at all.

8. **Write the node layer's skill-written half**: `helper_rendered_commit`,
   `last_test`, `last_test_outcome`.

9. **Journal** per `conventions/references/journal.md`, scope `node`. Record
   which audiences were verified, which alarms were armed and verified, and
   which jobs are watched by the sweep. **Never** journal a topic name, a
   token, or a message URL — write "the admin topic", not the topic.
   `outcome: failed` when the test did not verify; `partial` when the test
   verified but an alarm did not.

10. **Commit + push** the manifests, the `node.yaml` change, and the journal
    entry. After an `on_client` run, remind the admin to `git pull` on the
    node's working copy.

## Example output

```
Notification for vps-fra-01 — channel ntfy (https://ntfy.sh)

Audiences:  admin  verified 2026-09-13 21:04 (phone confirmed)
            agent  verified 2026-09-13 21:04

| job                  | schedule        | alarm                     |
|----------------------|-----------------|---------------------------|
| maintenance          | daily 05:15     | armed, fires if silent by 2026-09-15 07:15 |
| telemetry            | every 15 min    | armed, fires if silent by 2026-09-14 03:20 |
| backup-nextcloud     | daily 03:30     | armed, fires if silent by 2026-09-15 05:30 |
| backup-matrix        | weekly Sun 02:00| too long to arm — watched by maintenance   |
```

## What this command does NOT do

- **Carry content.** Messages leave the infrastructure. They carry node, job,
  event id, verdict, and counts — never secret values, log lines, IP
  addresses, or certificate contents.
- **Keep the record.** The public server holds messages for about half a day.
  Run logs and `notify.log` on the node stay authoritative, and the journal is
  written from them.
- **Decide what is worth sending.** Each command that publishes decides when
  its event is raised; routing decides who receives it. This command owns the
  channel, not the judgement.
- **Support a second channel.** One channel, closed.
- **Tear the channel down.** Deleting the manifests, the helper, and
  `_notify.env`, then disarming every alarm, is manual today; every rendered
  script already treats a missing helper as "notification off".

## See also

- `references/notify-recipes.md` — every HTTP call, the helper, alarms,
  verification, polling.
- `conventions/references/notify-manifest.md` — the two-layer routing schema
  and the event vocabulary.
- `node-maintenance` — the sweep that watches jobs too long to arm and
  publishes telemetry and drift events.
