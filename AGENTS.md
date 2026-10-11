# `drydocc` fork agent guide

A `drydocc` infrastructure fork. One fork = one infrastructure = one or more
Linux nodes. Each node clones this fork; the agent on that node scopes all work
to `nodes/<self>/`. See `docs/architecture.md` for the full picture.

## First contact

When `nodes/` holds no node yet, the admin is setting up. Unless this is a lab
run (see *Lab runs* below), check `git remote -v` before anything else:

- **`origin` is the public framework** (`hiemalis-at/drydocc`): this is a
  plain clone, not a fork. Do not scaffold a node or commit here. Walk the
  admin through `FORKING.md` §1 to create their private fork first — a fork
  holds hostnames and journals, and must not be public.
- **`origin` is the admin's own repository** and `upstream` is the framework:
  the fork is ready. Continue with `FORKING.md` from §2 (`/bootstrap-node` or
  `/onboard-node`), or offer `/lab` to try the framework on a local VM first.

## Invariants (NEVER violate)

1. **Node scoping.** Before any state-changing action, invoke the
   `node-context` skill to resolve `<self>`. Never write to
   `nodes/<X>/` unless `<X>` equals the bound `<self>` (exceptions:
   `/bootstrap-node` / `/onboard-node` scaffolding their `<slug>`, the
   lab's `promote` landing a runbook bundle as a new `nodes/<target>/`, and
   edits to `docs/`, `skills/`, and `.agents/`).

2. **Append-only journal.** Every state-changing action appends an entry
   under `nodes/<self>/journal/`. Same commit as the change. Even failures
   journal (`outcome: failed`). See the `conventions` skill's
   `references/journal.md`.

3. **Secrets are not git data.** Values live in the admin's KeePass + on the
   node at `/etc/drydocc/secrets/<slug>.env` (chmod 600). Only manifests
   (key names + purposes) live in git as `<slug>.secrets.yaml`. See the
   `conventions` skill's `references/secrets.md`.

4. **Conventions are authoritative.** When writing or modifying use-case
   docs, node profiles, journal entries, or secrets manifests, consult the
   matching reference in the `conventions` skill first.

5. **Distro-agnosticism.** Generic prose — the base repo, `docs/`, SKILL.md
   bodies, and non-recipe references — never names a package manager, firewall
   tool, init system, or MAC framework, neither as a command nor as a schema
   field name (`hardening.firewall`, never `hardening.<tool>`). `system_facts`
   values are an **open vocabulary**: any dialect a live probe returns is
   legal to record, and supporting a new one must never require editing the
   generic layer.

   Concrete syntax lives **only** in a workflow skill's
   `references/*-recipes.md`, where every section is an **outcome contract**
   — goal, the `system_facts` key + probe that selects the dialect, how to
   derive one nobody has written down, **mandatory verification**, and a
   record step — never a per-distro command list. Its dialect table is
   evidence of what has been verified on a real node, not the supported list.
   Refusal turns on a missing **capability**, never on what a box calls
   itself; the one hard gate is `system_facts.service_manager: systemd`,
   because quadlets are a systemd feature. Full contract:
   `skills/conventions/references/distro-agnosticism.md`.

6. **One fact, one level.** `AGENTS.md` holds invariants + routing; SKILL.md
   bodies hold procedure; `references/` hold exact commands and schemas. Each
   fact lives at exactly one level — lower levels link upward, never restate.
   Two copies of one fact is how one of them goes stale unnoticed.

## Skill routing

Every skill is under `skills/` and invoked as `/<name>`; the loaded
skill list carries each one's purpose. What that list does not tell you:

- **Order.** `node-context` binds `<self>` + surface first; `node-exec` then
  routes every node-touching command; workflow skills call both. `conventions`
  gates any write under `nodes/<node>/`.
- **Surface.** The use-case skills run either **on the node**
  (preferred — VS Code Remote-SSH or node-local CLI; first arg is the slug) or
  **on the client** when the node can't host the VS Code Remote Server (pass the
  node as the **first positional argument**: `/create-use-case <node> <slug> …`).
  `node-context` resolves which applies.
- **Recipes.** Concrete dialect syntax lives under the owning workflow skill's
  `references/*-recipes.md` as outcome contracts (invariant 5) — never a bare
  command list, and never in a SKILL.md body.
- **Lab runs.** If `.drydocc/lab.yaml` exists, this fork is a lab run: read
  the `conventions` skill's `references/lab-overlay.md` first. Unless it says
  `attended: true`, nobody is watching: answer an admin question only from
  the overlay's `answers`, by the question's `[decision: …]` id; a question
  without an answer is written to `.drydocc/lab-blocked.yaml` and the step
  stops — never guess.
- **The lab itself** — testing skills on throwaway VMs, prototyping a node
  and turning it into a runbook for real hardware — is the `lab` skill, run
  from the admin's own checkout; inside a prototype, only its § "Prototype
  session" applies.

## Scope

Each skill's closing *What this … does NOT do* section is the authoritative
list of what it leaves out, and `README.md`'s *Status* section lists what the
framework does not implement yet. If a user asks for something no skill
covers, say so plainly and offer to plan it as a framework change.

Changing `drydocc` itself — skills, conventions, scripts? Read
`CONTRIBUTING.md` first.
