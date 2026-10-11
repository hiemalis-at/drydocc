# Notification Recipes

The concrete layer behind `/node-notify`, and the one place every other skill's
rendered automation learns how to publish. Schemas live in
`conventions/references/notify-manifest.md`; this file assumes them.

Every snippet is the `on_node` form. From the commissioning client, route it
through `node-exec`.

## Contents

- [Capability](#capability)
- [Message content](#message-content)
- [Keeping topics off the command line](#keeping-topics-off-the-command-line)
- [Publishing an event](#publishing-an-event)
- [Verifying delivery](#verifying-delivery)
- [Arming a missed-run alarm](#arming-a-missed-run-alarm)
- [Verifying an armed alarm](#verifying-an-armed-alarm)
- [Disarming a missed-run alarm](#disarming-a-missed-run-alarm)
- [Jobs too long to arm](#jobs-too-long-to-arm)
- [Polling the agent topic](#polling-the-agent-topic)
- [The rendered helper](#the-rendered-helper)
- [Notify log](#notify-log)
- [Status report](#status-report)
- [Observed server behaviour — evidence, not the supported list](#observed-server-behaviour--evidence-not-the-supported-list)
- [What this file does NOT do](#what-this-file-does-not-do)

---

## Capability

**Goal.** The node can make an HTTPS request with custom headers and a body,
reading its URL and headers from standard input rather than from its argument
list.

**Probe.**

```bash
command -v curl >/dev/null && curl --version | head -1
```

**Derivation.** Every snippet below is written against `curl`, whose `--config
-` reads the URL and headers from stdin. A node without it has no verified
client. Installing one is a package change through the node's package manager
(`system_facts.package_manager`) and is the admin's decision, not this skill's;
refuse and name the missing capability. A different client is acceptable only
if it can also take the URL and headers from stdin — verify that before using
it, and record it below.

**Verification.** § "Verifying delivery" is the verification: a client that
cannot get a message to the server and back is not a capability.

---

## Message content

A message leaves the infrastructure, and on a public server the topic name is
the only access control. Every title and body is composed from:

- the node name, the job name, the event id;
- a verdict (`OK`, `DRIFT`, `failed`), a rule or posture id, a severity;
- counts, durations, and timestamps.

Never: a secret value, a topic name, a log line or excerpt, an IP address, a
ban source, a hostname other than the node's own, certificate contents, or a
path inside a volume. When in doubt, the id is enough — the details are on the
node and in the next journal entry.

---

## Keeping topics off the command line

A process's argument list is readable by other local users. The topic is in
the URL and the token is in a header, so neither may appear in an argument.
Every request below hands curl its URL and headers as a config file read from
a process substitution — `--config <(printf 'url = "…"\n…')` — so the argument
list holds only a file-descriptor path. The body, where there is one, arrives
on stdin (`--data-binary @-`). The helper and every script that sources it are
therefore **bash**.

Titles are sanitised before they are placed in a config line: strip `"` and
`\`, and collapse newlines. Bodies never go through the config.

**Verification.** While a request is in flight, `ps -eo args` must not show the
topic.

---

## Publishing an event

1. Look up the event's routing in the merged manifest: `to` (audiences) and
   `priority`. `to: []` means do nothing.
2. For each audience, resolve its topic from `/etc/drydocc/secrets/_notify.env`
   (on the node) or the resolved variable (agent side).
3. Publish:

   ```bash
   printf '%s' "<body>" | curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
     --data-binary @- --config <(printf 'url = "%s/%s"\nheader = "Title: %s"\nheader = "Priority: %s"\nheader = "Tags: drydocc,%s,%s"\n' \
       "$DD_SERVER" "$TOPIC" "<title>" "<priority>" "<node>" "<event>"; \
       [ -n "${DD_TOKEN:-}" ] && printf 'header = "Authorization: Bearer %s"\n' "$DD_TOKEN")
   ```

4. Append a line to the notify log (§ "Notify log") with the HTTP status.
   A non-2xx status is logged and **nothing else happens**: a notification
   that cannot be sent never fails the job that tried to send it.

Title convention: `<event>: <job or item> on <node>`. Body: one or two lines,
per § "Message content".

---

## Verifying delivery

Used by `--setup` and `--test`.

1. Record `T0=$(date +%s)`. Publish a `test` event whose body carries a
   one-time marker: `test <node> <8 random hex chars>`.
2. Wait at least **5 seconds** (see the evidence table: the public server
   batches its cache writes).
3. For each audience, poll the topic and look for the marker:

   ```bash
   curl -s --max-time 10 --config <(printf 'url = "%s/%s/json?poll=1&since=%s"\n' "$DD_SERVER" "$TOPIC" "$((T0-5))") \
     | jq -e --arg m "<marker>" 'select(.event=="message" and .message==$m)' >/dev/null
   ```

4. Found → the audience is verified. Not found after one retry 10 seconds
   later → `failed`, and say whether the publish itself returned 2xx (a server
   problem) or not (a client or network problem).

A server-side verification proves the message was accepted. Only the admin can
confirm a phone showed it; ask.

---

## Arming a missed-run alarm

**Goal.** A message sits on the server, scheduled for just after the job's
next run should have finished. Each successful run pushes it further out. If
the runs stop, it is delivered.

**Inputs**, fixed at render time into the job's own script:

- `<job>` — `maintenance`, `telemetry`, or `backup-<slug>`.
- `<interval-seconds>` — the **largest** gap between consecutive firings of the
  job's `OnCalendar=` expression. Compute it when rendering, from:

  ```bash
  systemd-analyze calendar --iterations=8 '<OnCalendar expression>'
  ```

  Take the `(in UTC)` lines, convert each to epoch seconds, and use the largest
  difference between neighbours. The largest, not the first: `Mon,Fri 03:00`
  has gaps of four and three days, and the alarm must tolerate the longer one.
  **Verify** by checking the computed value against the schedule read aloud —
  a daily job must come out at 86400.

**Procedure** (implemented as `dd_hb_arm <job> <interval-seconds>` in the
helper):

1. **Throttle.** If `/var/lib/drydocc/notify/armed-<job>` holds an epoch less
   than `rearm_every` ago, stop — the pending alarm already covers this run.
2. **Delay** = interval + `grace` + `rearm_every`. The last term compensates
   for the throttle: a run skipped by step 1 is still inside the pending
   alarm's window.
3. If the delay exceeds **259200 seconds (3 days)**, the server cannot hold
   it. Log `too-long` and stop; § "Jobs too long to arm" covers this job.
4. For each audience `run-missed` routes to, publish to the topic **with a
   sequence id**, `<node>-<job>`, as the last URL path segment, and an `In:`
   header:

   ```
   url = "<server>/<topic>/<node>-<job>"
   header = "In: <delay>s"
   header = "Title: run-missed: <job> on <node>"
   header = "Priority: <priority>"
   header = "Tags: drydocc,<node>,run-missed"
   ```

   Body: `no successful <job> run on <node> since <ISO-8601 UTC now>; expected every <interval>`.

   Publishing with a sequence id that already has a pending message
   **replaces** it.
5. On 2xx, write the current epoch to `/var/lib/drydocc/notify/armed-<job>`.
   Log the outcome either way.

**Detection latency.** A job that dies right after arming is reported after
at most interval + grace + `rearm_every`: about 8 h 15 min for a 15-minute
sampler with defaults, 32 h for a daily job. Lower `rearm_every` to tighten
it, at the cost of messages (§ "Observed server behaviour" lists the public
server's daily allowance).

---

## Verifying an armed alarm

Run by `/node-notify --setup` after arming, and by `--status`. Never by the
unattended scripts, which cannot act on the result.

1. Wait at least **5 seconds** after the arm.
2. List pending messages for the sequence id:

   ```bash
   curl -s --max-time 10 --config <(printf 'url = "%s/%s/json?poll=1&since=all&scheduled=1"\n' "$DD_SERVER" "$TOPIC") \
     | jq -c --argjson now "$(date +%s)" --arg q "<node>-<job>" \
         'select(.event=="message" and .sequence_id==$q and .time>$now) | {id,time}'
   ```

3. Judge the count:
   - **exactly one**, with `time` within 120 s of arm-time + delay → verified;
   - **none** → remove the throttle file, re-arm once, re-verify;
   - **more than one** → a replace landed before the server had stored the
     message it was replacing. Disarm (one cancel clears every pending message
     with that sequence id), wait 5 s, arm, wait 5 s, re-verify. Duplicates
     would fire the older alarm early — a false page.
4. Still wrong after one repair → report the alarm `unverified` and do not
   claim the job is watched.

The `sched=1` poll is the verification source because it lists exactly what
the server will deliver; the evidence table records that this was checked
against a live subscriber.

---

## Disarming a missed-run alarm

When a job's automation is purged, its alarm must go too — otherwise the
admin is paged about a timer that was removed on purpose.

Implemented as `dd_hb_disarm <job>`. For each audience `run-missed` routes to:

```
url = "<server>/<topic>/<node>-<job>/delete"
```

(a `GET`; `DELETE` on `<server>/<topic>/<node>-<job>` is equivalent). Then
remove `/var/lib/drydocc/notify/armed-<job>` and log the outcome.

**Verification** (interactive runs only): § "Verifying an armed alarm" must now
list **none**. Leave at least 5 seconds between the arm and a disarm of the
same job; the evidence table shows a cancel sent within about a second of the
message it cancels can miss it.

---

## Jobs too long to arm

A job whose interval + grace + `rearm_every` exceeds three days — a weekly
backup, say — cannot hold a pending message on the server. The daily
maintenance script watches it instead, with
`dd_check_overdue <job> <run-log path> <interval-seconds>`:

1. Age = now − the run log's modification time (`stat -c %Y <run log>`). The
   run log is appended at the end of every run, so its modification time is
   the last completion. A run log that does not exist yet means the job has
   never completed; treat its age as unknown and do nothing.
2. If age > interval + grace, publish `run-missed` for that job — at most once
   per interval, remembered in `/var/lib/drydocc/notify/overdue-<job>`.
3. Clear that file once the age is back within bounds.

This check is only as good as the maintenance timer that runs it, which is why
the maintenance job's own alarm must be armed. `/node-notify --status` flags a
too-long job whose watcher is not.

**Verification** (first render). Touch a copy of a run log with an old
modification time and call the function against it with a short interval; the
event must be published once, and not again on the next call.

---

## Polling the agent topic

For interactive runs catching up on what happened while nobody was watching.

```bash
curl -s --max-time 10 --config <(printf 'url = "%s/%s/json?poll=1&since=%s"\n' "$DD_SERVER" "$AGENT_TOPIC" "<epoch of the last run>") \
  | jq -c 'select(.event=="message") | {time, title, priority, tags}'
```

- Read `title`, `priority`, `tags`, and `time`. The body adds nothing the
  title and the node's own logs do not.
- **State the horizon.** Report the oldest `time` returned, and say plainly
  that the public server keeps messages for roughly 12 hours, so an empty
  result for a three-day gap means "nothing in the last half day", not
  "nothing".
- A `run-missed` delivered by the server is the one thing that appears here
  and in no node-local log — a job that never started cannot log. Everything
  else is also in `notify.log` and the run logs, which remain the record.
- Poll results are signals for the report. They are summarised in the journal
  by event id and count, never by topic.

---

## The rendered helper

`~/.config/drydocc/notify.sh`, rendered on the node from the merged manifest,
mode `0700`, never committed. Every drydocc timer script sources it if present
and stubs its functions if not. It **never** exits and **never** returns
non-zero: a sourcing script's error handling must not be triggered by a
notification.

Rendered header (values substituted at render time):

```bash
# Rendered by /node-notify from the merged notify manifest. Do not edit; re-render.
DD_NODE="<node>"
DD_SERVER="<server>"
DD_GRACE_S=<grace in seconds>
DD_REARM_S=<rearm_every in seconds>
DD_MAX_DELAY_S=259200
DD_LOG=/var/lib/drydocc/notify.log
DD_STATE=/var/lib/drydocc/notify
set -a; . /etc/drydocc/secrets/_notify.env 2>/dev/null; set +a
DD_TOKEN="${<token_env>:-}"
dd__topic() { case "$1" in admin) printf '%s' "${<admin topic_env>:-}";; agent) printf '%s' "${<agent topic_env>:-}";; esac; }
dd__route() {  # <event> -> "<space-separated audiences>|<priority>"
  case "$1" in
    run-missed)        printf '%s' "<to>|<priority>" ;;
    run-failed)        printf '%s' "<to>|<priority>" ;;
    telemetry-anomaly) printf '%s' "<to>|<priority>" ;;
    maintenance-drift) printf '%s' "<to>|<priority>" ;;
    admin-attention)   printf '%s' "<to>|<priority>" ;;
    test)              printf '%s' "<to>|<priority>" ;;
    *)                 printf '%s' "|" ;;
  esac
}
```

Functions, each composed from the sections above:

| function | does | section |
|---|---|---|
| `dd_notify <event> <title> <body>` | publish to every routed audience | § "Publishing an event" |
| `dd_hb_arm <job> <interval-seconds>` | throttled arm with sequence id | § "Arming a missed-run alarm" |
| `dd_hb_disarm <job>` | cancel pending alarm, clear throttle | § "Disarming a missed-run alarm" |
| `dd_check_overdue <job> <run log> <interval-seconds>` | publish `run-missed` for an overdue long job | § "Jobs too long to arm" |

Each function ends with `return 0`. Each creates `$DD_STATE` on first use.

**Verification (every render).** `bash -n` the file; then source it in a
subshell and call `dd_notify test …` — § "Verifying delivery" must pass.

The stub every consuming script carries:

```bash
if [ -r "$HOME/.config/drydocc/notify.sh" ]; then
  . "$HOME/.config/drydocc/notify.sh"
else
  dd_notify() { :; }; dd_hb_arm() { :; }; dd_hb_disarm() { :; }; dd_check_overdue() { :; }
fi
```

---

## Notify log

`/var/lib/drydocc/notify.log`, one line per request:

```
<ts-iso-utc>|<event>|<audience>|<job or ->|<http status or 000>|<note>
```

`<note>` is `sent`, `armed`, `throttled`, `too-long`, `disarmed`, or `failed`.
The log never contains a topic, a token, a title, or a body — it records that a
signal was attempted and whether the server took it.

Interactive runs fold lines newer than their last run into their journal
narrative, as they do their own run logs. `/node-maintenance`'s `logs-bounded`
keeps 30 days.

---

## Status report

`--status` is read-only.

1. List the timers: `systemctl --user list-timers 'drydocc-*' --all --no-legend`.
2. For each, the expected alarm state: armed (with its fire-by time, from
   § "Verifying an armed alarm"), `too long to arm — watched by maintenance`,
   or `unverified`.
3. The last 20 `notify.log` lines, with their counts of `failed`.
4. `last_test` and `last_test_outcome` from the node manifest.

Flag, in this order: a too-long job while the maintenance timer is not
enabled; an enabled timer with no pending alarm; a `failed` rate above zero in
the last day.

---

## Observed server behaviour — evidence, not the supported list

What has been checked against a live subscriber. A self-hosted server is
acceptable; record what you verify on it.

| behaviour | ntfy.sh, 2026-09-13 |
|---|---|
| `In: <n>s` holds a message server-side | yes; 10 s minimum, 3 days maximum |
| republish with the same sequence id replaces a pending message | yes, when ≥ ~2 s after the message it replaces |
| replace or cancel within ~1 s of the target's publish | **unreliable** — the target was sometimes still delivered, consistent with cache writes being batched; hence the 5 s waits |
| one cancel with duplicates pending | clears all of them |
| `GET …/<seq>/delete` and `DELETE …/<seq>` | both cancel when spaced; both return 200 even when they miss |
| `json?poll=1&since=all&scheduled=1` | lists exactly the pending messages later delivered |
| delivered-message cache | about 12 hours |
| free allowance | about 250 messages a day, 60-message burst |

Because a cancel returns 200 even when it misses, **status codes are not
verification** — only the pending-message poll is.

---

## What this file does NOT do

- **Retry until delivered.** One attempt per event, logged. A notification
  path that retries can hold a job hostage.
- **Deduplicate on the server.** Deciding when an event is new is the
  publisher's: telemetry and drift publish on state change, alarms throttle.
- **Hold history.** The server is a signal. Run logs, `notify.log`, and the
  journal are the record.
