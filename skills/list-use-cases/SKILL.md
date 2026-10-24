---
name: list-use-cases
description: List all use-cases on the current node with status, containers, and last-applied/last-audited info. Use to see what a node runs.
compatibility: Runs inside a drydocc fork; needs a shell.
metadata:
  skillset: drydocc
  requires: conventions node-context
---

# /list-use-cases

## Contract

- **Args:**
  - On the node: none.
  - On the commissioning client: `<node>` — the target node as the
    **positional first argument** (required in client mode).
- **Surface:** works from either surface. This command is a **pure
  repo-frontmatter read**, so unlike the other use-case commands it needs **no
  SSH to the node even when `on_client`** — it just reads
  `nodes/<node>/use-cases/*.md` from the working copy. (It therefore does not
  depend on `node-exec`.)
- **Reads:** `nodes/<self>/use-cases/*.md` frontmatter, plus
  `*.audit.yaml:last_audited` and `*.backup.yaml:last_backup` when present.
- **Writes:** nothing. Pure read.
- **Journal scope:** none (read-only commands don't journal).

## Procedure

1. **Bind `<self>` + surface.** Invoke the `node-context` skill. On the node,
   no argument is needed; on the client, `<self>` comes from the positional
   node argument (`node-context` refuses with a usage hint if it's missing).
   No SSH is performed either way — the listing comes entirely from the repo.
   When `surface == on_client` and
   `nodes/<self>/node.yaml:remote_ide.vscode_server` is `compatible`, you may
   append a one-line hint that the node supports VS Code Remote-SSH (the other
   use-case commands run more comfortably there); this is informational only —
   the listing always proceeds.

2. **Locate use-case files.** Glob `nodes/<self>/use-cases/*.md`
   (NOT `*.secrets.yaml`, NOT `*.audit.yaml`, NOT `_retired/*.md`).

3. **Parse frontmatter.** For each file, read the YAML between the first
   pair of `---` lines. Validate against the use-case-doc schema (see
   `conventions`). If a file is malformed, include it in the
   output with `status: INVALID` and continue — do not abort the listing.

   Then read `<slug>.audit.yaml:last_audited` and `<slug>.backup.yaml:last_backup`
   if those siblings exist. A missing manifest is normal, not an error — both
   are optional.

   `list_use_cases` (`podman-quadlets` § "The drydocc tools") does steps 2–4
   and renders the table below in one call; this procedure remains the
   contract.

4. **Render the table.** Columns:

   | name | status | containers | ports | last_applied | last_audited | last_backup |
   |------|--------|------------|-------|--------------|--------------|-------------|

   - `name` — from frontmatter.
   - `status` — `planned` / `active` / `retired` / `INVALID`.
   - `containers` — comma-joined `containers:` list (truncate to first 3,
     append `, +N more` if longer).
   - `ports` — comma-joined `ports:` list (same truncation rule).
   - `last_applied` — date portion of `last_applied`, or `—` if `null`.
   - `last_audited` — date portion of the manifest's `last_audited`, or `—` if
     the manifest is absent or the field is null. Makes a stale audit visible at
     a glance; run `/audit-use-case` to refresh it.
   - `last_backup` — date portion of the backup manifest's `last_backup`, or
     `never` if the manifest exists but that field is null, or `—` if the
     manifest is absent (no backup strategy declared yet — see
     `/backup-use-case`). Distinguishing `never` from `—` makes "declared but
     unproven" visible at a glance, separately from "not declared at all".

5. **Empty case.** If the directory contains no `*.md` files, print
   exactly:

   ```
   No use-cases on <self> yet. Create one with /create-use-case.
   ```

6. **Retired case.** If `_retired/` exists, append a one-liner after the
   main table:

   ```
   Retired: <N> use-case(s) under _retired/. Use ls to inspect.
   ```

## Example output

For a node with two active use-cases and one retired:

```
Use-cases on vps-fra-01:

| name      | status | containers                        | ports                | last_applied | last_audited | last_backup |
|-----------|--------|-----------------------------------|----------------------|--------------|--------------|-------------|
| nextcloud | active | nextcloud-app, nextcloud-db, ...  | 443/tcp, 3478/udp    | 2026-05-18   | 2026-07-26   | 2026-09-12  |
| matrix    | planned| —                                 | —                    | —            | —            | —           |

Retired: 1 use-case(s) under _retired/. Use ls to inspect.
```

## Reference

- Schema for use-case docs: `conventions` skill (`references/use-case-doc.md`).
- Schema for audit manifests: `conventions` skill
  (`references/audit-manifest.md`) — the source of `last_audited`.
- Schema for backup manifests: `conventions` skill
  (`references/backup-manifest.md`) — the source of `last_backup`.
- Sibling files (`.secrets.yaml`, `.audit.yaml`, `.backup.yaml`) are NOT
  listed as rows — this command lists use-cases, not files. (No
  `.quadlets/` directory exists in git; on-node quadlets are not
  listed either.)

## What this command does NOT do

- Contact the node. It reports what the fork *declares*; whether the node
  matches is `/audit-use-case`'s question.
- Write or journal anything.
- List another node's use-cases — the listing is scoped to the bound
  `<self>`.
- Detail retired use-cases or sibling files; it counts the former and skips
  the latter.
