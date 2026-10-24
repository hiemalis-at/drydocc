#!/usr/bin/env bash
# drydocc lab agent adapter: pi (pi.dev) on OpenRouter — its free models by default,
# any OpenRouter model when DRYDOCC_LAB_MODEL names one.
#
# The contract every adapter implements is scripts/lab/agents/README.md. Here:
#   credentials  only $DRYDOCC_LAB_REAL_HOME/.pi/agent/auth.json is linked into the
#                isolated home; OPENROUTER_API_KEY in the environment works as well
#   model        DRYDOCC_LAB_MODEL — one OpenRouter id (pinned) or a comma-separated
#                chain; unset, the chain in pi/models
#   fallback     a model-level failure (model withdrawn, overloaded, erroring) moves
#                the same session to the next model in the chain
#   exit 75      a bad key, an account setting, the shared free-model daily cap, or a
#                chain with no model left — never a verdict on the code under test
#   meta         $DRYDOCC_LAB_TRANSCRIPT.meta.json: the models used, the request count
#   mcp          the fork's drydocc MCP server, when this ref ships it and
#                python3 hosts it: registered user-level in the isolated home,
#                direct exposure; a registration failure is noted, never fatal
#
# pi names skills `/skill:<name>`; pi/skill-commands.ts gives them their canonical
# `/<name>`. pi/loop-guard.ts stops a model repeating one tool call: the step fails. DRYDOCC_OPENROUTER_URL points pi and the preflight at another
# OpenRouter-compatible base URL (a regional endpoint, or a test double).
#
# `--interactive [args…]` starts pi for an admin at the keyboard instead;
# `--check` reports readiness as one JSON line without calling a model.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ext="$here/pi/skill-commands.ts"
guard="$here/pi/loop-guard.ts"
base_url="${DRYDOCC_OPENROUTER_URL:-https://openrouter.ai/api/v1}"

real_home="${DRYDOCC_LAB_REAL_HOME:-$HOME}"
mkdir -p "$HOME/.pi/agent"
cred="$real_home/.pi/agent/auth.json"
if [ -e "$cred" ] && [ "$cred" != "$HOME/.pi/agent/auth.json" ]; then
  ln -sfn "$cred" "$HOME/.pi/agent/auth.json"
fi
if [ -n "${DRYDOCC_OPENROUTER_URL:-}" ]; then
  printf '{"providers": {"openrouter": {"baseUrl": "%s"}}}\n' "$base_url" > "$HOME/.pi/agent/models.json"
fi
# Per-minute 429s are routine on free models: retry them for up to a few minutes
# (DRYDOCC_PI_RETRIES lowers that, e.g. for the adapter's own tests).
printf '{"retry": {"maxRetries": %d, "baseDelayMs": 2000, "maxAgentDelayMs": 60000}}\n' \
  "${DRYDOCC_PI_RETRIES:-12}" > "$HOME/.pi/agent/settings.json"
# The fork's drydocc MCP server, when this ref ships it and python3 can host
# it: registered user-level in the isolated home, stdio, direct exposure so
# the model calls the tools without codeme; by absolute path, so the server
# spawns whatever pi's working directory is. A ref without the server, or a
# python3 without the mcp and PyYAML packages, runs on — the tools are simply
# absent.
mcp_tools=0
[ -e "scripts/drydocc/mcp_server.py" ] && python3 -c 'import mcp, yaml' >/dev/null 2>&1 && mcp_tools=1
register_mcp() {
  [ "$mcp_tools" = 1 ] || return 0
  pi mcp add drydocc --exposure direct \
    --description "drydocc fork tools: validate use-cases, render and write quadlets, lingering sessions" \
    -- python3 "$PWD/scripts/drydocc/mcp_server.py" >/dev/null 2>&1 || {
    [ -n "${DRYDOCC_LAB_TRANSCRIPT:-}" ] && \
      printf 'pi.sh: the drydocc MCP server is not registered — no mcp__drydocc__* tools\n' \
        >> "$DRYDOCC_LAB_TRANSCRIPT.stderr"
  }
}
# pi's attribution headers stay on: OpenRouter serves some free models only to
# the agent harnesses it recognises by them (403 "only available on agentic harnesses").
export PI_SKIP_VERSION_CHECK=1

chain=()
while IFS= read -r m; do
  m="${m#"${m%%[![:space:]]*}"}"; m="${m%"${m##*[![:space:]]}"}"
  [ -n "$m" ] && chain+=("$m")
done < <(if [ -n "${DRYDOCC_LAB_MODEL:-}" ]; then tr ',' '\n' <<<"$DRYDOCC_LAB_MODEL"
         else grep -vE '^[[:space:]]*(#|$)' "$here/pi/models"; fi)

# free: every model of the chain is a `:free` route; anything else is billed per use
cost=free
for m in "${chain[@]}"; do case "$m" in *:free) ;; *) cost=paid ;; esac; done

if [ "${1:-}" = "--check" ]; then
  if [ "$cost" = free ]; then
    detail="pi on OpenRouter free models; prompts are logged by the free providers"
  else
    detail="pi on OpenRouter; billed per use"
  fi
  ready=true
  if ! command -v pi >/dev/null; then
    ready=false detail="pi is not installed — see scripts/lab/agents/README.md"
  elif ! pi auth check --provider openrouter >/dev/null 2>&1; then
    ready=false detail="no OpenRouter key — /login openrouter in pi, or set OPENROUTER_API_KEY"
  elif [ "${#chain[@]}" -eq 0 ]; then
    ready=false detail="no model: DRYDOCC_LAB_MODEL and pi/models are both empty"
  fi
  if [ "$mcp_tools" = 1 ]; then
    detail="$detail; the fork's drydocc MCP tools are available (direct exposure)"
  elif [ -e "scripts/drydocc/mcp_server.py" ]; then
    detail="$detail; python3 lacks the mcp or PyYAML package — the drydocc MCP tools will be missing"
  fi
  python3 -c 'import json, sys; print(json.dumps({"ready": sys.argv[1] == "true", "cost": sys.argv[4],
    "model": sys.argv[2], "detail": sys.argv[3]}, ensure_ascii=False))' "$ready" "${chain[0]:-}" "$detail" "$cost"
  exit 0
fi

if [ "${1:-}" = "--interactive" ]; then
  shift
  register_mcp
  exec pi --approve -e "$ext" -e "$guard" --provider openrouter --model "${chain[0]:?no model in the chain}" "$@"
fi

prompt_file=${1:?usage: pi.sh <prompt-file> | --interactive [args…] | --check}
: "${DRYDOCC_LAB_TRANSCRIPT:?}" "${DRYDOCC_LAB_TIMEOUT:?}"

UNATTENDED="This is an unattended, non-interactive session: nobody will read \
your reply in time to answer it, and nobody will resume this session once it \
ends. Never end your turn while work you started is still running — run long \
commands in the foreground, or poll background work until it has finished — \
and then complete the task."
SKILLREF="A skill named as /<name> — in a prompt, in AGENTS.md or inside another \
skill — is the skill at skills/<name>/SKILL.md: when told to invoke or delegate \
to it, read that file and follow it."
CONTINUE="Continue, still unattended. If any work you started is still pending \
or the task is not finished, finish it now — wait for background work to \
complete. If everything is finished, reply with exactly: DONE"

deadline=$(( $(date +%s) + DRYDOCC_LAB_TIMEOUT ))
: > "$DRYDOCC_LAB_TRANSCRIPT"
errlog="$DRYDOCC_LAB_TRANSCRIPT.stderr"
: > "$errlog"
register_mcp
meta="$DRYDOCC_LAB_TRANSCRIPT.meta.json"
used=()

say() { printf 'pi.sh: %s\n' "$*" >> "$errlog"; }

write_meta() {
  python3 - "$DRYDOCC_LAB_TRANSCRIPT" "$meta" "${used[@]}" <<'PY'
import json, sys
requests = 0
for line in open(sys.argv[1], errors="replace"):
    try:
        e = json.loads(line)
    except ValueError:
        continue
    if e.get("type") == "message_end" and (e.get("message") or {}).get("role") == "assistant":
        requests += 1
json.dump({"models": sys.argv[3:], "requests": requests}, open(sys.argv[2], "w"))
PY
}
trap write_meta EXIT

# Preflight: the key works, the account is past the 50-requests/day free tier, and
# the chain is cut to the models OpenRouter still serves with tool calling.
key=$(pi auth print-api-key --provider openrouter 2>/dev/null) || {
  say "no OpenRouter key — /login openrouter in pi, or set OPENROUTER_API_KEY"; exit 75; }
acct=$(curl -fsS -m 30 -H @- "$base_url/key" <<<"Authorization: Bearer $key" 2>>"$errlog") || {
  say "OpenRouter rejected the key or is unreachable ($base_url/key)"; exit 75; }
if [ "$cost" = free ] && python3 -c 'import json, sys; sys.exit(0 if json.loads(sys.argv[1])["data"].get("is_free_tier") else 1)' "$acct"; then
  say "this OpenRouter account never bought credits: 50 free requests/day cannot finish a lab step (buy 10 once for 1000/day)"
  exit 75
fi
# The roster is hundreds of models: a file, never an argument.
listed="$DRYDOCC_LAB_TRANSCRIPT.models.json"
curl -fsS -m 30 -o "$listed" "$base_url/models" 2>>"$errlog" || {
  say "cannot list OpenRouter models ($base_url/models)"; exit 75; }
mapfile -t models < <(python3 -c '
import json, sys
live = {m["id"] for m in json.load(open(sys.argv[1]))["data"] if "tools" in (m.get("supported_parameters") or [])}
for m in sys.argv[2:]:
    if m in live:
        print(m)
    else:
        print(f"pi.sh: {m} is not served with tool calling — skipped", file=sys.stderr)
' "$listed" "${chain[@]}" 2>>"$errlog")
if [ "${#models[@]}" -eq 0 ]; then
  say "no model of the chain is served: ${chain[*]}"; exit 75
fi

# round_outcome <from-byte>: classify the last assistant message since that offset.
#   ok <text>     the model answered
#   model <msg>   this model failed: try the next one
#   infra <msg>   the account cannot work at all (key, payment, privacy, daily cap)
round_outcome() {
  python3 - "$DRYDOCC_LAB_TRANSCRIPT" "$1" <<'PY'
import json, os, re, sys
f = open(sys.argv[1], "rb"); f.seek(int(sys.argv[2]))
last = None
for line in f.read().decode(errors="replace").splitlines():
    try:
        e = json.loads(line)
    except ValueError:
        continue
    m = e.get("message") or {}
    if e.get("type") == "message_end" and m.get("role") == "assistant":
        last = m
if last is None:
    print("model no reply from the model"); sys.exit()
if last.get("stopReason") != "error":
    c = last.get("content")
    text = c if isinstance(c, str) else "".join(p.get("text", "") for p in c or [] if isinstance(p, dict))
    print("ok", text.replace("\n", " ")); sys.exit()
err = str(last.get("errorMessage", ""))
status = re.match(r"\s*(\d{3})\b", err)
status = int(status.group(1)) if status else 0
# a 403 that names the model is that model's gate, not the account's
model_gate = status == 403 and (os.environ.get("MODEL", "") in err or "only available" in err)
account = not model_gate and (status in (401, 402, 403) or "per-day" in err or "data policy" in err
                              or "credits" in err.lower() and status != 429)
print("infra" if account else "model", err.replace("\n", " ")[:300])
PY
}

sid="lab-$(date +%Y%m%d%H%M%S)-$$"
i=0
outcome=""

run_round() {  # run_round <prompt>: one pi call on models[i], falling back on model failures
  local prompt=$1
  while :; do
    local left=$(( deadline - $(date +%s) ))
    [ "$left" -gt 60 ] || return 124
    local model=${models[$i]} from rc=0 errfrom
    local n=${#used[@]}
    { [ "$n" -gt 0 ] && [ "${used[n-1]}" = "$model" ]; } || used+=("$model")
    from=$(stat -c %s "$DRYDOCC_LAB_TRANSCRIPT")
    errfrom=$(stat -c %s "$errlog")
    timeout --kill-after=30 "$left" \
      pi --mode json --approve -e "$ext" -e "$guard" --session-id "$sid" \
      --provider openrouter --model "$model" \
      --append-system-prompt "$UNATTENDED $SKILLREF" -- "$prompt" \
      < /dev/null >> "$DRYDOCC_LAB_TRANSCRIPT" 2>> "$errlog" || rc=$?
    [ "$rc" -eq 0 ] || return "$rc"
    # the loop guard aborted a model stuck on one call: a judged failure, no more rounds
    if tail -c +"$(( errfrom + 1 ))" "$errlog" | grep -q "^loop-guard: aborted"; then
      say "$model looped on one tool call; the step fails"
      return 1
    fi
    outcome=$(MODEL="$model" round_outcome "$from")
    case "$outcome" in
      ok\ *|ok) return 0 ;;
      infra\ *) say "$model: ${outcome#infra }"; return 75 ;;
    esac
    say "$model failed: ${outcome#model }"
    if [ $(( i + 1 )) -ge "${#models[@]}" ]; then
      say "no model left in the chain"; return 75
    fi
    i=$(( i + 1 ))
    say "continuing the session on ${models[$i]}"
    prompt=$CONTINUE
  done
}

rc=0
run_round "$(cat "$prompt_file")" || rc=$?
[ "$rc" -eq 75 ] && exit 75

# Up to three continuation rounds: an agent that ended with work pending
# gets the session back until it confirms there is nothing left to do.
for _ in 1 2 3; do
  [ "$rc" -eq 0 ] || break
  [ "$(tr -d '[:space:]' <<<"${outcome#ok}")" = "DONE" ] && break
  run_round "$CONTINUE" || rc=$?
  [ "$rc" -eq 75 ] && exit 75
done
exit "$rc"
