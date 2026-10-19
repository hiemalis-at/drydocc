#!/usr/bin/env bash
# drydocc lab agent adapter: Mistral Vibe.
#
# The contract every adapter implements is scripts/lab/agents/README.md. Here:
#   credentials  only $DRYDOCC_LAB_REAL_HOME/.vibe/.env is linked into the isolated
#                home — never config.toml, agents, skills, plugins or history, so
#                the agent sees the fork's skills (.vibe/skills) and nothing else
#   model        DRYDOCC_LAB_MODEL, when set, becomes VIBE_ACTIVE_MODEL; otherwise
#                vibe's own default for a fresh home
#   exit 75      an API-level end — invalid or missing key, rate limit, provider
#                error — is infrastructure, never a verdict on the code under test
#
# `--interactive [args…]` starts vibe for an admin at the keyboard instead;
# `--check` reports readiness as one JSON line without calling a model.
set -euo pipefail

: "${DRYDOCC_LAB_REAL_HOME:?}"
mkdir -p "$HOME/.vibe"
cred="$DRYDOCC_LAB_REAL_HOME/.vibe/.env"
if [ -e "$cred" ]; then
  ln -sfn "$cred" "$HOME/.vibe/.env"
fi
# MISTRAL_API_KEY in the environment works as well; vibe reads it first.
if [ -n "${DRYDOCC_LAB_MODEL:-}" ]; then
  export VIBE_ACTIVE_MODEL="$DRYDOCC_LAB_MODEL"
fi

if [ "${1:-}" = "--check" ]; then
  ready=true detail="Mistral Vibe; billed per use"
  if ! command -v vibe >/dev/null; then
    ready=false detail="vibe is not installed"
  elif [ ! -e "$cred" ] && [ -z "${MISTRAL_API_KEY:-}" ]; then
    ready=false detail="no key — run vibe once, or set MISTRAL_API_KEY"
  fi
  python3 -c 'import json, sys; print(json.dumps({"ready": sys.argv[1] == "true", "cost": "paid",
    "model": sys.argv[2], "detail": sys.argv[3]}, ensure_ascii=False))' "$ready" "${DRYDOCC_LAB_MODEL:-}" "$detail"
  exit 0
fi

if [ "${1:-}" = "--interactive" ]; then
  shift
  exec vibe --trust "$@"
fi

prompt_file=${1:?usage: vibe.sh <prompt-file> | --interactive [args…]}
: "${DRYDOCC_LAB_TRANSCRIPT:?}" "${DRYDOCC_LAB_TIMEOUT:?}"

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
errlog="$DRYDOCC_LAB_TRANSCRIPT.stderr"
: > "$errlog"

# last_field <field>: sessionId, or the text of the last assistant message
last_field() {
  python3 - "$DRYDOCC_LAB_TRANSCRIPT" "$1" <<'PY'
import json, sys
sid, text = "", ""
for line in open(sys.argv[1], errors="replace"):
    try:
        e = json.loads(line)
    except ValueError:
        continue
    sid = e.get("sessionId") or sid
    if e.get("type") == "message" and e.get("role") == "assistant":
        text = "".join(c.get("text", "") for c in e.get("content") or [] if isinstance(c, dict))
print(sid if sys.argv[2] == "session" else text)
PY
}

run_round() {  # run_round <prompt> [--resume <id>]
  local prompt=$1; shift
  local left=$(( deadline - $(date +%s) ))
  [ "$left" -gt 60 ] || return 124
  local rc=0
  # vibe has no system-prompt flag: the unattended rule leads the prompt.
  # stdin must be closed — with it open, programmatic mode waits on it.
  timeout --kill-after=30 "$left" \
    vibe -p "$prompt" "$@" --auto-approve --trust --output streaming \
    < /dev/null >> "$DRYDOCC_LAB_TRANSCRIPT" 2>> "$errlog" || rc=$?
  return "$rc"
}

is_infra() {
  grep -qE "API error from |Rate limits exceeded|Invalid API key|Set the environment variable" "$errlog"
}

rc=0
run_round "$UNATTENDED

$(cat "$prompt_file")" || rc=$?
is_infra && exit 75

for _ in 1 2 3; do
  [ "$rc" -eq 0 ] || break
  [ "$(last_field text | tr -d '[:space:]')" = "DONE" ] && break
  session=$(last_field session)
  [ -n "$session" ] || break
  run_round "$CONTINUE" --resume "$session" || rc=$?
  is_infra && exit 75
done
exit "$rc"
