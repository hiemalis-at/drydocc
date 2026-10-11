---
name: continue-last-command
description: Resume the last state-changing command that did not finish — paused for a secret, or cut off when the agent itself failed. Reconciles what the dead run left behind, then re-invokes it with its original args or journals it as abandoned. Use after a run stopped — a missing secret, or the agent failing (unreachable, out of credits, crashed).
compatibility: Runs inside a drydocc fork; needs a shell and file edits.
metadata:
  skillset: drydocc
  requires: conventions secrets node-context node-exec
---

# /continue-last-command

A run can stop in two ways. It can **pause** on purpose, when a secret is
missing (`secrets` § ".env fallback — two paths"). Or it can be **cut off**
when the agent dies mid-run: the model's server is unreachable, the credits
run out, the agent crashes, or its window is closed. A dead agent writes
nothing. So every state-changing command records itself in a resume pointer
*before* it starts, and the journal shows whether it finished. This command
reads both and continues the one unfinished run.

## Contract

- **Args:** none.
- **Reads:** `.drydocc/last-command.yaml` (§ "Resume pointer");
  `nodes/<node>/journal/` (committed or not); `git status` of
  `nodes/<node>/`; for an interrupted run, the node's lingering steps
  (`node-exec`).
- **Writes:**
  - The recorded command writes as it normally would.
  - Its own writes happen only when the admin abandons an interrupted run. It
    journals that run (`outcome: failed`) and commits the entry.
  - It deletes the pointer once the run is finished or abandoned.
- **Journal scope:** none for a resumed run, which journals per its own
  contract. An abandoned run is journaled under the **Journal scope** its own
  command's contract names.

## Resume pointer

One gitignored file per working copy, `.drydocc/last-command.yaml`. It records
the last state-changing command started there:

```yaml
command: /bootstrap-node
args: "vps-fra-01"             # one string, exactly as the command was invoked
node: vps-fra-01
surface: on_client             # on_node | on_client
started: 2026-06-13T11:52:00Z  # UTC, when node-context bound the run
state: running                 # running | waiting_on_secret
phase: 3                       # optional — the phase reached, where the command has phases
waiting_on: [DOCCAP_TS_KEY]    # only with state: waiting_on_secret
```

Who writes what:

- **`node-context`** writes the pointer, with `state: running`, when it binds
  `<self>` for a command that journals (every state-changing command). It does
  not write one for read-only commands such as `/list-use-cases`.
- A command with phases updates `phase` as it enters each one.
- A command that pauses for a secret sets `state: waiting_on_secret` and
  `waiting_on` before it pauses (`secrets` § ".env fallback — two paths").
- **Nothing clears it on success, and no command deletes it**; only this
  command does, once it has judged the run. A run is **finished** once
  `nodes/<node>/journal/` holds an entry whose `trigger` starts with the
  recorded `command` and whose `timestamp` is at or after `started`. Every
  run journals, even a failed one, so the journal is the completion signal
  and no command has to remember to tidy up.

## Procedure

1. **Read the pointer.** If it is missing, there is nothing to continue: say
   so and stop. Bind the recorded `node` and `surface` through `node-context`,
   passing the node as the explicit override. That binding must not replace
   the pointer, because it is this run's.

2. **Classify the run.** Check in this order:
   - **`state: waiting_on_secret`** — paused. Go to step 3.
   - **`state: running`, and the journal shows it finished** — report the
     entry (filename, `outcome`), delete the pointer, and stop. If that entry
     is not committed yet, the agent died between writing and committing it.
     Treat the run as interrupted (step 4) so its leftovers are committed or
     discarded deliberately.
   - **`state: running`, no journal entry since `started`** — **interrupted**.
     Go to step 4.

3. **Paused: re-check the blockers.** For each name in `waiting_on`, confirm
   `$<NAME>` is set in the environment. If any is still unset, name it and
   re-offer the `secrets` `.env` paths rather than re-invoking. Don't loop
   into the same failure. Otherwise go to step 6.

4. **Interrupted: reconcile before anything runs again.** Show the admin
   where the run stopped (`command`, `args`, `started`, `phase`). Then:
   1. **Another session?** Ask whether an agent session is still running
      this command somewhere, for example in another window or on the node
      `[decision: continue-last-command.still-running]`. If yes, stop: two
      agents must never drive one run.
   2. **Work still running on the node.** List the node's lingering steps,
      the `dd-<slug>-*` sessions and logs of `node-exec`'s lingering recipe.
      Poll each one: a long step may have finished, or still be running,
      after the agent died. Wait for a running step to finish and report its
      `__DONE_<rc>__`. Never launch a step a second time while its session
      exists.
   3. **Leftovers in the repository.** Run `git status -- nodes/<node>/`. These
      are the dead run's uncommitted edits, possibly including a journal
      entry. Show them and ask
      `[decision: continue-last-command.leftovers]`:
      - **keep** — leave them for the resumed command. Recommend this for a
        phase-resumable command (`/bootstrap-node`, `/onboard-node`,
        `/rebuild-node`), whose probes build on what is already there;
      - **discard** — restore only those paths
        (`git restore --staged --worktree -- <paths>`, and remove new files
        only after naming each one). Recommend this for every other command:
        it re-runs from the top and writes those files again, and a half-written
        leftover would trip its own refusals, such as `/create-use-case`
        refusing a slug whose doc already exists.

      Discarding never undoes what the dead run already changed **on the
      node**. Say so, and recommend `/audit-use-case` afterwards for a
      use-case command.
   4. **Resume or abandon** `[decision: continue-last-command.resume]`:
      - **resume** → step 6;
      - **abandon** → step 5.

5. **Abandon.** Journal the dead run per `conventions` `references/journal.md`:
   - `trigger`: the recorded `command` and `args`;
   - `scope`: the one the command's own contract names;
   - `outcome: failed`;
   - a body that says it was interrupted, when, the phase it reached, what
     step 4 found on the node, and which leftovers were kept or discarded.

   Commit it per § "Committing" of that reference, delete the pointer, and stop.

6. **Re-invoke the recorded command** with the recorded `args`.
   - **Phase-resumable commands** (`/bootstrap-node`, `/onboard-node`,
     `/rebuild-node`) skip completed phases through their own resume probes
     and journal idempotency. `/rebuild-node` re-triages and skips every
     use-case already recovered.
   - **Every other command** runs again from the top and relies on its own
     refusals and idempotency. A refusal can fire because the dead run got
     past its point of no return, for example `/apply-use-case` after
     `last_applied` was set. If that happens, don't force it. Run
     `/audit-use-case <slug>` to compare declared and live state, then
     abandon the dead run (step 5) and record what the audit found.

   The re-invoked command's `node-context` binding writes a fresh pointer. Its
   journal entry then marks it finished, so a later `/continue-last-command`
   reports it as done instead of replaying it.

## Limitations

- Resume granularity is **phase-level**, supplied by the resumable commands'
  own probes. There is no checkpoint inside a step. A step the agent was
  running itself (not lingered) when it died is simply run again, so it must
  be idempotent, as every step's contract already requires.
- There is one pointer per working copy. Starting a different state-changing
  command while one is unfinished replaces it, after `node-context` asks.
- A pointer exists only for runs started after this mechanism existed; an
  older interrupted run has none to find.

## What this command does NOT do

- Read, print, or source the `.env`. The admin loads it before restarting
  the agent; this command only checks that the named variables are now set.
- Resolve a still-missing secret itself. It names the variable and points
  back to the `secrets` `.env` paths.
- Run a second agent alongside one that may still be alive, or relaunch a
  lingering node step whose session still exists.
- Undo on-node changes of a dead run. It discards repository leftovers only
  when the admin says so, and points at `/audit-use-case` for the node side.
- Resume anything but the one recorded command.
