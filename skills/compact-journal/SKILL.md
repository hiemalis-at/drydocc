---
name: compact-journal
description: Audit a node's journal for ambiguities, contradictions and outdated statements, then compact verbose entry bodies in place — frontmatter verbatim, full originals recoverable from git history — with additive resolution annotations and a findings report in the run's journal entry. Use to keep a long journal cheap to read without losing history resolution.
compatibility: Runs inside a drydocc fork; needs a shell and file edits.
metadata:
  skillset: drydocc
  requires: conventions node-context continue-last-command
---

# /compact-journal

## Contract

- **Args:**
  - On the node: none.
  - On the commissioning client: `<node>` — the target node as the
    **positional first argument** (required in client mode).
- **Surface:** works from either surface. A **pure repo operation** — like
  `/list-use-cases` it needs **no SSH to the node even when `on_client`**, and
  does not depend on `node-exec`. It reads and writes only the working copy.
- **Reads:** `nodes/<self>/journal/*.md`; sibling nodes' journals **only** when
  a finding crosses nodes (a use-case that migrated); the journal convention.
- **Writes:** the bodies of eligible entries in `nodes/<self>/journal/` per the
  journal convention's § Compaction, plus the run's own journal entry. Nothing
  else — no frontmatter rewrite, no other node's files, no live-node state.
- **Journal scope:** `node` — one entry per run, carrying the findings report.

The full compaction contract (frontmatter immutability, keep/drop rules,
resolution annotations, the git-history guarantee, eligibility) lives in
`conventions/references/journal.md` § Compaction. This procedure is the
contract for *running* it; it never restates the schema.

## Procedure

1. **Bind `<self>` + surface.** Invoke the `node-context` skill. On the node no
   argument is needed; on the client `<self>` comes from the positional node
   argument. No SSH is performed either way. Record the baseline answer of the
   journal's derived queries for this node now (step 4 compares against it):
   `owes_restore` for every use-case slug that has a rebuild or restore history.

2. **Analysis sweep — grep-first, never bulk-read bodies.** Token efficiency is
   the point; read frontmatter and grep hits, not whole corpora.
   - **Frontmatter conformance:** for every entry check timestamp↔filename
     agreement, the `scope`/`outcome` enums, `trigger`/`actor` formats, the
     commit fields, and the required section headings. Collect deviations.
     **Do not rewrite frontmatter** — deviations are reported as accepted
     historical deviations, not corrected.
   - **Open-loop sweep:** grep bodies for deferred / pending / "left for" /
     "not done here" / TODO markers. For each hit, search *later* entries of the
     same scope for a resolution; where the story crosses nodes, read the
     implicated sibling entries.
   - Full body reads **only** for entries the sweep flags.

3. **Compaction pass.** Eligible = body exceeds 40 lines and no `compacted:`
   field. Read eligible bodies in chronological batches and write compacted
   versions incrementally (so work survives context pressure). Copy each
   entry's frontmatter **byte-identical** and add `compacted: <today>`; rewrite
   the body to the canonical sections under the keep/drop contract. Add a
   resolution annotation only where a later entry verifiably settles the
   statement, citing that entry by filename; an unsettled item goes to the
   findings report, never into an annotation asserting live state.

4. **Verify mechanically.** For every compacted entry, diff against HEAD: no
   frontmatter line changed except the added `compacted:`; the three canonical
   headings are present. Re-run the derived queries from step 1 — every answer
   must be unchanged. Recover one or two originals via `git log -p -- <path>`
   and confirm no decision-relevant fact was lost.

5. **Journal** one entry, `scope: node`, `trigger:` the invocation as typed,
   `outcome: success`. Its Notes carry the node's findings report: schema
   deviations (accepted historical deviations), contradictions, outdated
   statements with their resolution pointers or an "unresolved — verify on
   node" status, and the annotations added.

6. **Commit + push** per the journal convention's § Committing: stage the run's
   paths by name (never `git add -A`), one commit holding the compacted entries
   and the journal entry together, then `git pull --rebase && git push`. A run
   that dies mid-way is reconciled by `/continue-last-command` like any other
   command.

7. **Report** to the admin: per-node byte reduction, the findings, and any open
   item that needs on-node verification (reported, not acted on here).

## Refusals

- Rewrite any frontmatter field, or normalize historical `actor`/`trigger`/
  `scope`/`commit_after` values — compaction is body-only.
- Delete, merge, reorder, or rename entries.
- Touch live-node state, or verify a drift on the node — open items are
  reported for a later on-node audit.
- Compact another node's journal in the same run (one `<self>` per run).

## What this skill does NOT do

- Normalize historical metadata (actors, triggers, scopes, SHAs stay as
  written); schema gaps are fixed in the convention, not by rewriting history.
- Delete or merge entries, or compact non-journal files (use-case docs,
  manifests).
- Run any node command or confirm live drift — it is repo-only.
- Wire compaction into `/node-maintenance` or any timer (a later, separate
  change).
