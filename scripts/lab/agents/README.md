# Lab agent adapters

An adapter is how the lab drives one vendor's agent CLI. The lab driver
(`scripts/lab/lab.py`) never calls an agent directly: it calls
`scripts/lab/agents/<vendor>.sh`, chosen by `--adapter <vendor>` (default
`$DRYDOCC_LAB_ADAPTER`, else `claude`). Adding a vendor means adding one file
here; nothing else in the lab, and nothing in a skill, changes.
`lab.py agents` shows which adapters are ready on this machine.

| adapter | agent CLI | cost | credentials linked into the run | verified |
|---|---|---|---|---|
| `claude.sh` | Claude Code (`claude -p`) | paid | `~/.claude/.credentials.json` | every lab scenario |
| `vibe.sh` | Mistral Vibe (`vibe -p`) | paid | `~/.vibe/.env` | `whoami` |
| `pi.sh` | pi (`pi --mode json`) on OpenRouter | free by default; paid for a non-`:free` model | `~/.pi/agent/auth.json` | `whoami`, `web-db` |

An adapter is listed as verified only after a lab scenario has passed with it.

## Contract

`<vendor>.sh <prompt-file>` — one headless agent step.

| input | meaning |
|---|---|
| `$1` | a file holding the prompt for this step |
| cwd | the run's fork (a standalone clone in the lab store); its skills are the ones under test |
| `HOME` | an isolated, initially empty home for this run |
| `PATH` | the run's shims first (ssh, tailscale, vault client, `lab-admin`, …) |
| `DRYDOCC_LAB_TRANSCRIPT` | write the agent's full transcript here |
| `DRYDOCC_LAB_TIMEOUT` | seconds; the agent must be gone by then |
| `DRYDOCC_LAB_REAL_HOME` | the admin's real home: link **only** the CLI's credentials from it, never its settings, memory, skills or plugins, so the agent sees nothing but the fork |
| `DRYDOCC_LAB_MODEL` | optional: the model to use, in the CLI's own naming |

| exit status | meaning |
|---|---|
| 0 | the agent finished; whether the step *worked* is for the lab's assertions to say, never the agent's prose |
| non-zero | the agent failed or timed out |
| 75 (`EX_TEMPFAIL`) | the agent could not work for reasons outside the code under test — authentication, a rate limit, an API outage. The lab reports an infrastructure **error**, never a test failure |

Optional outputs, used when present:

| file | meaning |
|---|---|
| `$DRYDOCC_LAB_TRANSCRIPT.meta.json` | `{"models": [...], "requests": N}` — the models the step used, in order, and its model requests. The report names them; `lab.py qualify` totals them |
| `$DRYDOCC_LAB_TRANSCRIPT.stderr` | on exit 75, its last line says why; the report quotes it |

The agent runs **unattended**: it must not wait for a human, and it must not
end its session while work it started is still pending — nobody resumes it.
Both adapters here state that rule to the agent and, when it ends a session
without saying it is done, resume the session (up to three times) until it
replies `DONE`.

`<vendor>.sh --interactive [args…]` — the same agent for an admin at the
keyboard (`lab.py shell <run> --agent <vendor>`): same cwd, `HOME`, `PATH`
and credential rule, no prompt file, transcript or timeout.

`<vendor>.sh --check` — readiness, without calling a model: one JSON line
`{"ready": bool, "cost": "paid"|"free", "model": "…", "detail": "…"}` and exit
0. `detail` says what to do when not ready. The lab runs it with a throwaway
`HOME` and `DRYDOCC_LAB_REAL_HOME` set, for `lab.py agents` and for the banner
every `run`, `up`, `verify-runbook` and `qualify` opens with.

## pi on OpenRouter's free models

`pi.sh` runs pi on OpenRouter's free models: no cost per request, at the price
of a weaker model and a daily cap. Its fallback chain is
[`pi/models`](pi/models), most capable first. Before a step, it drops what
OpenRouter no longer serves with tool calling. A model that fails mid-step
(withdrawn, overloaded, erroring) hands the same session to the next one.
`DRYDOCC_LAB_MODEL` replaces the chain: a comma-separated list, or one id to
pin it. Account-level failures are exit 75: a bad key, the privacy setting,
the daily cap that every free model shares.
[`pi/skill-commands.ts`](pi/skill-commands.ts) lets pi take a skill as
`/<name>`, as every other agent does (pi's own form is `/skill:<name>`).
[`pi/loop-guard.ts`](pi/loop-guard.ts) stops a model that repeats one tool
call — one free model called `echo "DONE"` 272 times in a turn: the fifth
identical call in a row is refused, the tenth ends the step as failed.
`pi.sh` also registers the fork's drydocc MCP server
([`scripts/drydocc/mcp_server.py`](../../drydocc/mcp_server.py), when the ref
ships it and python3 hosts it) in the isolated home with direct exposure, so
the model calls the `mcp__drydocc__*` tools like built-ins. Without the server
or the packages the run degrades to the CLI, and `--check` and the run's
stderr say so.

Setup, once:

1. Install pi, pinned: `npm install -g --ignore-scripts @earendil-works/pi-coding-agent@1.2.0`.
2. An OpenRouter account that has bought at least 10 credits, once. That
   lifts free use from 50 to 1,000 requests a day; one lab step takes 50–150.
   The adapter refuses an account still on the 50/day tier.
3. OpenRouter Settings → Privacy: allow providers that may log and train on
   prompts — free endpoints require it. **Everything the lab agent sees goes
   to them**: the public framework, throwaway VM data, and in `tailnet:
   ephemeral` scenarios your lab tailnet auth key. Never point this adapter at
   a real fork.
4. An API key, ideally with a guardrail allowing only `:free` models and a
   small credit limit.
5. The key for pi: `/login openrouter` inside `pi`, or `OPENROUTER_API_KEY`.
   `~/.pi/agent/auth.json` may hold `"key": "!<command>"` so that the key
   comes from a password manager.
6. `lab.py agents` shows `pi` ready; `lab.py run lab/scenarios/whoami.yaml
   --adapter pi` is the smoke test.
7. Optional: `export DRYDOCC_LAB_ADAPTER=pi`.

Which model can run drydocc is measured, not assumed:
`lab.py qualify --adapter pi --models <a>,<b> <scenario>…` builds the matrix
that orders `pi/models`. A model without the `:free` suffix is any other
OpenRouter model, billed per use. Cheap paid models are often the better
trade than free ones: a few cents a run, with no shared daily cap. The
adapter labels its cost from the chain — `free` only when every model in it
is a `:free` route.

Measured on 2026-10-15/17, `whoami` and `web-db` from a commissioned node,
one run per cell — thin evidence, rerun before relying on it:

| model | cost | passed | per scenario |
|---|---|---|---|
| `nvidia/nemotron-3-ultra-550b-a55b:free` | free | 2/2 | ~110 requests, 15–25 min, often overloaded |
| `deepseek/deepseek-v4.1-flash` | paid | 4/4 | ~$0.04, ~40 requests, ~4 min |
| `anthropic/claude-haiku-5.5` | paid | 2/2 | ~$0.04, ~30 requests, ~4 min |
| `qwen/qwen3.7-flash` | paid | 1/2 | a wrong journal `trigger` |
| `openai/gpt-5-nano` | paid | 0/2 | never applied |

The other free models in `pi/models` either failed `whoami` or are
unmeasured. For a run that must finish, pin a qualified paid model with
`--model`.

## Writing one

Probe the CLI in a scratch directory with an isolated `HOME` first: which
file holds its credentials, whether its headless mode waits on standard input,
how it prints a session id and the final reply, how it resumes a session, and
what an authentication or rate-limit failure looks like — that last one is
what must become exit 75. Add `--check`. Then run a scenario with
`--adapter <vendor>` and add the row above.
