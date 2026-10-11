# Lab scenario — `lab/scenarios/<name>.yaml`

A scenario is one reproducible lab run: which nodes to start from which
layers, which admin decisions are pre-answered, which headless agent steps to
take, and what must be true afterwards. `scripts/lab/lab.py run <file>`
executes it in a fresh lab fork and judges it **only** by its assertions —
never by what the agent says it did. A scenario names concrete images and
distros freely: like `node.yaml`, it is data, not generic prose.

## Schema

```yaml
scenario: whoami-smoke                 # kebab-case, ≤ 24 chars — prefixes the run id
description: one sentence on what this run proves
ref: HEAD                              # the framework ref under test (default: HEAD)
tailnet: off                           # off | ephemeral (needs the admin's key) — see lab-overlay.md
nodes:
  web:                                 # node name, a valid drydocc slug
    from: commissioned                 # base | commissioned | <layer name or key>
    distro: <distro>                   # with from: base | commissioned — which layer
    version: "<version>"
    arch: x86_64                       # default: the host's own
answers:                               # decision id → answer, see lab-overlay.md
  apply-use-case.confirm-render: "yes"
secrets: [POSTGRES_PASSWORD]           # names only — values are generated per run
steps:
  - name: create and apply whoami      # free text, shown in the report
    agent: /create-use-case web whoami "…"
    node: web                          # the node this step targets
    timeout: 2400                      # seconds, before the time scale
    expect: done                       # done (default) | blocked | failed | any
  - name: whoami answers
    assert: http
    node: web
    url: http://127.0.0.1:8080/
    contains: "Hostname:"
```

## Secrets

`secrets` lists environment-variable **names** — the names the use-cases'
secrets manifests declare. For each, the runner generates a fresh random
value per run, keeps it in the run's private secrets directory, and passes it
to every agent step through the environment, which is where the `secrets`
skill looks first. A value never appears in the scenario, the fork, a journal,
or the report.

## Layer resolution

`from: commissioned` asks for a commissioned layer of that distro, version
and architecture **built by the framework ref under test**: its skills digest
must equal the digest of `bootstrap-node` and everything it depends on at
`ref`. When none exists, the runner builds one first: it clones the matching
base layer, runs a headless `/bootstrap-node`, asserts the baseline, and seals
the result. Editing `bootstrap-node` therefore rebuilds the commissioned layer
on the next run; editing an unrelated skill does not. `from: base` takes the
newest matching base layer. A layer name or key pins one layer exactly.

## Assertions

Every assertion step names `assert:` and the fields below. All run from the
laptop; `node`-bound ones run on the node as `doccap` over keyed SSH.

| assert | fields | passes when |
|---|---|---|
| `command` | `node`, `run`, optional `exit` (default 0), `stdout_contains` | the command exits as stated and its output contains the text |
| `http` | `node`, `url`, optional `contains`, `status` (default 200) | the URL, fetched **on the node**, answers with that status and text |
| `journal_entry` | `node`, `trigger_contains`, optional `outcome` (default `success`) | the fork holds a journal entry for the node whose `trigger` contains the text and whose `outcome` matches |
| `node_yaml` | `node`, `path` (dotted), `equals` or `nonempty: true` | the node profile's value at `path` equals `equals`, or is set at all |
| `use_case` | `node`, `slug`, optional `status` (default `active`) | the use-case doc exists with that status |
| `use_cases` | `node`, optional `status` (default `planned`), `count_min` (default 1) | at least that many use-case docs have that status — for runs where the agent names the slugs |
| `checks_pass` | — | no framework check fails on the run's fork that passes on the ref under test — a check already failing at the ref is reported, not counted. A checkout without framework checks (a fork ships none) reports the step as **skipped**, never as passed |

`http` runs on the node because a hardened node's firewall admits use-case
ports from the tailnet, not from the laptop — the lab does not weaken that
posture to make a test pass.

## Steps the lab performs

A `do:` step changes the world under the agent, the way an admin, a power
cut or a disaster would — never the way a skill would:

| do | fields | effect |
|---|---|---|
| `command` | `node`, `run`, optional `exit` | runs a command on the node as the lab (doccap once bootstrapped, root before) |
| `replace-node` | `node`, optional `from`/`distro`/`version`/`arch` (default: a base layer) | destroys the node's box outright and starts a fresh one in its place, at the same addresses; everything in git survives |
| `reboot` | `node` | asks the node to reboot |
| `power-cycle` | `node` | pulls the plug and powers it back on — nothing is flushed |

An agent step may carry `inject: {after: <seconds>, do: reboot | power-cycle,
node: <node>}` to fire a fault while the agent is mid-run. A run with
several nodes resolves each node's name to its labnet address on every peer,
standing in for the tailnet's name resolution. Step text may use `{run}`,
which expands to the run id — a value no earlier run could have left behind.

## Verdicts

- **pass** — every agent step ended as `expect`ed and every assertion held.
  The run is collected; its report and transcripts are kept under
  `results/<run>/` in the lab store.
- **fail** — a step failed, an assertion did not hold, or a step blocked that
  was not expected to. The run is kept, VMs and fork included, for
  inspection until `lab.py gc --failed`.
- **error** — the run could not produce a result: the lab itself failed, or an
  agent could not work at all (its adapter exited 75 — authentication or an
  API outage). Says nothing about the code under test; re-run it.
- **blocked** — an agent step stopped on an unanswered decision. The report
  names the decision; add an answer or sharpen the description.

`--repeat N` runs the scenario N times and reports the pass rate: all passing
is a pass, none passing is a **regression**, anything between is a **flake**
— agents are nondeterministic, and a flake is a finding, not noise.
