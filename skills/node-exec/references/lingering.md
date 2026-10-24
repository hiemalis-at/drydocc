# Lingering: long-running node steps over SSH

When the agent is `on_client`, a long step (large `podman pull`, payload
download, DB init, backup) issued as a single `ssh … '<cmd>'` dies if the SSH
connection drops. To survive disconnects, launch the step **detached on the
node** in a named tmux session, then poll for completion and tail the log.

On the `on_node` surface lingering is unnecessary — run the command inline.

## tmux recipe (primary)

`<host>`/`<port>`/`<key>`/`<user>` are the connection block from
`node-context`; `<slug>` is the use-case; `<step>` is a short label
(`pull`, `dbinit`, `backup`, …). `SESSION = dd-<slug>-<step>`,
`LOG = /tmp/dd-<slug>-<step>.log`.

### 1. Launch (idempotent — re-attaches if already running)

```bash
ssh -i ~/.ssh/<key> -p <port> -o StrictHostKeyChecking=accept-new <user>@<host> '
  S=dd-<slug>-<step>; L=/tmp/dd-<slug>-<step>.log
  if tmux has-session -t "$S" 2>/dev/null; then
    echo "ATTACH: $S already running"
  else
    : >"$L"
    tmux new -d -s "$S" "{ <cmd>
} >>\"$L\" 2>&1; echo __DONE_\$?__ >> \"$L\""
    echo "LAUNCHED: $S"
  fi
'
```

The log is created before the job starts, so a poll never meets a missing file
while the job runs, and a stale sentinel from an earlier run is gone. `<cmd>` is
grouped in `{ … }` so every part of a compound command (`a; b`) is logged, not
only the last; the newline before `}` keeps a command that ends in a comment
intact. The job writes its own exit code as a `__DONE_<rc>__` sentinel on the
log's last line. That sentinel — not "session gone" — is the source of truth for success,
so a session that vanishes for any other reason is never mistaken for a clean
exit.

### 2. Poll until done

```bash
ssh -i ~/.ssh/<key> -p <port> -o StrictHostKeyChecking=accept-new <user>@<host> '
  S=dd-<slug>-<step>; L=/tmp/dd-<slug>-<step>.log
  if tmux has-session -t "$S" 2>/dev/null; then
    echo "RUNNING"; tail -n 3 "$L" 2>/dev/null
  else
    tail -n 1 "$L" 2>/dev/null   # expect __DONE_<rc>__
  fi
'
```

Poll on a sensible cadence for the work (an image pull every ~10–15s; a long
backup less often). On each poll surface the last few log lines to the user so a
multi-minute step shows progress. Stop when the session is gone:

- last line `__DONE_0__` → **success**.
- `__DONE_<n>__` with `n != 0` → **failure**; capture the tail of `$LOG` into
  the journal and mark the use-case action `outcome: partial`.
- session gone but **no** sentinel → the job was killed (OOM, reboot,
  `tmux kill-server`); treat as failure, capture the tail, do not assume
  success.

### 3. Clean up the log (optional)

```bash
ssh … <user>@<host> 'rm -f /tmp/dd-<slug>-<step>.log'
```

Leave it in place if the action ended `partial` — it is diagnostic.

## nohup fallback (when tmux is absent)

`bootstrap-node` / `onboard-node` install `tmux` as part of the baseline stack,
so a `drydocc` node normally has it. On an older or onboarded box that lacks it,
fall back to `nohup` + a PID file:

```bash
ssh … <user>@<host> '
  L=/tmp/dd-<slug>-<step>.log; P=/tmp/dd-<slug>-<step>.pid
  if [ -f "$P" ] && kill -0 "$(cat "$P")" 2>/dev/null; then
    echo "ATTACH: pid $(cat "$P") still running"
  else
    nohup sh -c "<cmd>; echo __DONE_\$?__ >> \"$L\"" >"$L" 2>&1 &
    echo $! > "$P"; echo "LAUNCHED: pid $!"
  fi
'
# poll: kill -0 "$(cat "$P")" → RUNNING; else read the __DONE_<rc>__ sentinel from $L
```

The `__DONE_<rc>__` sentinel + log-tail semantics are identical; only the
"is it still running?" probe differs (`kill -0 <pid>` instead of
`tmux has-session`).

## Notes

- Keep `<step>` labels stable across re-runs so resume re-attaches. Do not embed
  timestamps in the session name.
- The detached command runs as `doccap`; if it needs root (rare for these
  steps) prefix `<cmd>` with `sudo` and ensure the doccap sudoers entry is
  passwordless (it is, post-bootstrap).
- Secret-bearing long steps (rare) still never echo values into `$LOG`; redact
  as the `secrets` skill requires.
