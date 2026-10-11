#!/usr/bin/env bash
# drydocc lab agent adapter: Claude Code.
#
# The contract every adapter implements is scripts/lab/agents/README.md. Here:
#   credentials  only $DRYDOCC_LAB_REAL_HOME/.claude/.credentials.json is linked
#   model        DRYDOCC_LAB_MODEL, when set, becomes --model
#   exit 75      an API-level end (expired auth, rate limit, outage)
#
# `--interactive [args…]` starts the agent for an admin at the keyboard instead.
set -euo pipefail

: "${DRYDOCC_LAB_REAL_HOME:?}"
mkdir -p "$HOME/.claude"
cred="$DRYDOCC_LAB_REAL_HOME/.claude/.credentials.json"
if [ -e "$cred" ]; then
  ln -sfn "$cred" "$HOME/.claude/.credentials.json"
fi
# API-key authentication, when used instead, arrives through the environment.

if [ "${1:-}" = "--interactive" ]; then
  shift
  # a fresh HOME would otherwise greet the admin with first-run setup every time
  [ -e "$HOME/.claude.json" ] || printf '{"hasCompletedOnboarding": true}\n' > "$HOME/.claude.json"
  exec claude "$@"
fi

prompt_file=${1:?usage: claude.sh <prompt-file> | --interactive [args…]}
: "${DRYDOCC_LAB_TRANSCRIPT:?}" "${DRYDOCC_LAB_TIMEOUT:?}"

model_args=()
if [ -n "${DRYDOCC_LAB_MODEL:-}" ]; then
  model_args=(--model "$DRYDOCC_LAB_MODEL")
fi

# Long foreground commands are legitimate here (a package install on an
# emulated guest can take an hour): let the shell tool run them in the
# foreground instead of pushing them into the background.
export BASH_DEFAULT_TIMEOUT_MS=1800000 BASH_MAX_TIMEOUT_MS=7200000

UNATTENDED="This is an unattended, non-interactive session: nobody will read \
your reply in time to answer it, and nobody will resume this session once it \
ends. Never end your turn while work you started is still running — run long \
commands in the foreground, or poll background work until it has finished — \
and then complete the task."
CONTINUE="Continue, still unattended. If any work you started is still pending \
or the task is not finished, finish it now — wait for background work to \
complete. If everything is finished, reply with exactly: DONE"

deadline=$(( $(date +%s) + DRYDOCC_LAB_TIMEOUT ))
: > "$DRYDOCC_LAB_TRANSCRIPT"

# result_field <field>: a field of the transcript's last result event
result_field() {
  python3 - "$DRYDOCC_LAB_TRANSCRIPT" "$1" <<'PY'
import json, sys
last = {}
for line in open(sys.argv[1], errors="replace"):
    try:
        e = json.loads(line)
    except ValueError:
        continue
    if e.get("type") == "result":
        last = e
v = last.get(sys.argv[2], "")
print(json.dumps(v) if isinstance(v, bool) else v)
PY
}

run_round() {  # run_round <prompt> [--resume <id>]
  local prompt=$1; shift
  local left=$(( deadline - $(date +%s) ))
  [ "$left" -gt 60 ] || return 124
  local rc=0
  timeout --kill-after=30 "$left" \
    claude -p "$prompt" "$@" \
    --permission-mode bypassPermissions \
    --append-system-prompt "$UNATTENDED" \
    --output-format stream-json --verbose \
    "${model_args[@]}" \
    >> "$DRYDOCC_LAB_TRANSCRIPT" 2>&1 || rc=$?
  return "$rc"
}

is_infra() {  # an API-level end (expired auth, outage) is infrastructure
  [ "$(result_field is_error)" = "true" ] || return 1
  [ "$(result_field terminal_reason)" = "api_error" ] && return 0
  result_field result | grep -qi authenticate
}

rc=0
run_round "$(cat "$prompt_file")" || rc=$?
is_infra && exit 75

# Up to three continuation rounds: an agent that ended with work pending
# gets the session back until it confirms there is nothing left to do.
for _ in 1 2 3; do
  [ "$rc" -eq 0 ] || break
  [ "$(result_field result | tr -d '[:space:]')" = "DONE" ] && break
  session=$(result_field session_id)
  [ -n "$session" ] || break
  run_round "$CONTINUE" --resume "$session" || rc=$?
  is_infra && exit 75
done
exit "$rc"
