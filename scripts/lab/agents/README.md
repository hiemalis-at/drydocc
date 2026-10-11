# Lab agent adapters

An adapter is how the lab drives one vendor's agent CLI. The lab driver
(`scripts/lab/lab.py`) never calls an agent directly: it calls
`scripts/lab/agents/<vendor>.sh`, chosen by `--adapter <vendor>` (default
`claude`). Adding a vendor means adding one file here; nothing else in the
lab, and nothing in a skill, changes.

| adapter | agent CLI | credentials linked into the run | verified |
|---|---|---|---|
| `claude.sh` | Claude Code (`claude -p`) | `~/.claude/.credentials.json` | every lab scenario |
| `vibe.sh` | Mistral Vibe (`vibe -p`) | `~/.vibe/.env` | `whoami` |

An adapter is listed as verified only after a lab scenario has passed with it.

## Contract

`<vendor>.sh <prompt-file>` — one headless agent step.

| input | meaning |
|---|---|
| `$1` | a file holding the prompt for this step |
| cwd | the run's fork worktree; its skills are the ones under test |
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

The agent runs **unattended**: it must not wait for a human, and it must not
end its session while work it started is still pending — nobody resumes it.
Both adapters here state that rule to the agent and, when it ends a session
without saying it is done, resume the session (up to three times) until it
replies `DONE`.

`<vendor>.sh --interactive [args…]` — the same agent for an admin at the
keyboard (`lab.py shell <run> --agent <vendor>`): same cwd, `HOME`, `PATH`
and credential rule, no prompt file, transcript or timeout.

## Writing one

Probe the CLI in a scratch directory with an isolated `HOME` first: which
file holds its credentials, whether its headless mode waits on standard input,
how it prints a session id and the final reply, how it resumes a session, and
what an authentication or rate-limit failure looks like — that last one is
what must become exit 75. Then run a scenario with `--adapter <vendor>` and
add the row above.
