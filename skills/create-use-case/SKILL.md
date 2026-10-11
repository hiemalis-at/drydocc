---
name: create-use-case
description: Author a new use-case on the current node from a description — write the doc, secrets manifest, and audit manifest — then commit it as `planned` and delegate the first apply to `/apply-use-case`. Use when the admin wants a new service on a node.
compatibility: Runs inside a drydocc fork; needs a shell and file edits.
metadata:
  skillset: drydocc
  requires: conventions secrets podman-quadlets node-context node-exec apply-use-case
---

# /create-use-case

## Contract

- **Args:**
  - On the node: `<slug>` (kebab-case, required) and `"<description>"`
    (free text, required).
  - On the commissioning client: `<node>` `<slug>` `"<description>"` — the
    target node is the **positional first argument** (required in client mode;
    `node-context` refuses without it). Example:
    `/create-use-case vps-fra-01 turn-server "coturn TURN/STUN server"`.
- **Surface:** runs from either surface, resolved by `node-context`.
  Surface resolution matters here for two reasons: in client mode the target
  node is the required positional first argument (see `Args:` above), and
  the resolved surface is what gets carried through to step 8's
  `/apply-use-case` invocation.
- **Reads:** `nodes/<self>/`.
- **Writes (in-repo):**
  - `nodes/<self>/use-cases/<slug>.md` — the use-case doc with
    frontmatter (including the `spec:` block quadlets are rendered from) and the four
    required body sections. Written with `status: planned` and
    `last_applied: null`.
  - Optionally `nodes/<self>/use-cases/<slug>.secrets.yaml` if the
    description mentions secret-bearing services.
  - Optionally `nodes/<self>/use-cases/<slug>.audit.yaml` — the audit manifest,
    seeded with the config surfaces the description implies.
- **On-node side effects:** none directly — step 8 delegates every node write
  to `/apply-use-case`.
- **Journal scope:** `use-case/<slug>`.

## Procedure

1. **Bind `<self>` + surface** via `node-context`. It returns the surface
   (`on_node` / `on_client`) and, in client mode, the node connection block.
   This skill has no on-node side effects of its own — steps 2–7 are all
   in-repo. Carry the resolved surface (and node, in client mode) forward to
   step 8, which passes it through as `/apply-use-case`'s own `<slug>` /
   `<node> <slug>` argument form; that command does its own `node-context`
   binding and `node-exec` routing for the node writes it owns.

1b. **Surface nudge (client only).** When `surface == on_client`, read
   `nodes/<self>/node.yaml:remote_ide`:
   - `vscode_server: compatible` → tell the admin: *"`<self>` can host the VS
     Code Remote Server. For full filesystem + container tooling, connect via
     Remote-SSH to `doccap@<tailscale_hostname>` and run `/create-use-case`
     there instead. Continue over SSH anyway? (y/N)"* — proceed only on yes.
     `[decision: create-use-case.continue-over-ssh]`
   - `vscode_server: incompatible` → note SSH is the only surface for this node
     (`<reason>`) and proceed.
   - `vscode_server: unknown` (or block absent) → proceed; suggest re-running
     `/bootstrap-node <self>` to refresh the probe.

2. **Validate the slug.** Must match `^[a-z][a-z0-9-]{0,30}[a-z0-9]$` and
   must not already exist. Refuse if the file is present (suggest
   `/modify-use-case` instead).

3. **Parse the description.** Pull out:
   - Likely container names (e.g., "nextcloud + postgres" → `nextcloud-app`,
     `nextcloud-db`).
   - Pinned image tags for each container — ask the admin for the tag
     if the description doesn't specify; `:latest` is a validation error.
   - Likely ports (e.g., "TURN server" → `3478/udp`, `5349/tcp`).
   - Volumes needed and their mount targets in each container.
   - Whether secrets are needed (admin passwords, db passwords).
   - Inter-container ordering (e.g., db before app → `depends_on`).
   - One-sentence summary.
   - **Config surfaces** — paths the application writes through its *own*
     surfaces (admin UI, settings menu, service CLI) rather than through
     `spec:`. These are what a rebuild would silently lose, and they seed the
     audit manifest in step 5b.

   If anything is unclear, ask the admin (one question at a time).

4. **Write the use-case doc** at `nodes/<self>/use-cases/<slug>.md` with
   frontmatter per `conventions` (including the `spec:` block —
   see `podman-quadlets` `## Render from spec` for the schema)
   and body sections:
   - `## Intent` — what this use-case serves and why.
   - `## Architecture` — containers, networks, volumes, ports.
   - `## Runbook` — placeholder bullets for bring-up, common ops, known
     issues; admin will fill these in as they operate.
   - `## Quadlets` — single line: *"Generated on apply from frontmatter
     `spec:`. See on-node `~/.config/containers/systemd/<slug>-*.{container,network}`."*

5. **If secrets are needed**, write `<slug>.secrets.yaml` with the
   relevant keys (`postgres password`, `admin password`, etc.) per the
   `conventions` skill's `references/secrets.md`. Set `has_secrets: true` in the doc's
   frontmatter and set `env_from: <target_path>` on every container in
   `spec.containers` that consumes the values.

5b. **Write the audit manifest** at `<slug>.audit.yaml` per the `conventions`
   skill's `references/audit-manifest.md`. Seed `config_surfaces` from step 3 —
   each entry names a declared `volume`, a `path` relative to that volume's
   root, a `kind`, and a `note` saying what writes it and why it matters. Leave
   `expectations` empty unless the description implies a specific health check,
   and leave the audit-written half (`last_audited`, `last_audited_commit`,
   `fingerprints`) null/empty for `/audit-use-case` to fill.

   Skip the file entirely if step 3 surfaced no config surfaces and no
   expectation — the manifest is optional, and an empty one is noise.

6. **Journal the declaration** per the `conventions` skill's
   `references/journal.md` — scope `use-case/<slug>`, `outcome: success`,
   recording the doc and siblings authored. Authoring is a state-changing
   action, so it carries its own entry (invariant 2).

7. **Commit** the doc, its siblings, and the journal entry with
   `status: planned` and `last_applied: null`. The use-case now exists as a
   declaration; nothing has been built on the node.

8. **Invoke `/apply-use-case <slug>`** (client form: `/apply-use-case <node>
   <slug>`) to materialize it — volumes, secrets env file, quadlets, services,
   firewall rules — and to flip the doc to `status: active`. That skill owns
   every first apply, greenfield or adoption; do not duplicate its steps here.

   This is a **hand-off**: this command has journaled, so the apply records
   its own run. Rewrite the resume pointer for it — `command: /apply-use-case`,
   its `args`, a fresh `started`, `state: running` (`node-context` § "Resume
   pointer (binds the run)"). Otherwise an apply cut off mid-run would look
   finished.

   If the apply fails, the committed `planned` document is the resume point:
   fix the cause and re-run `/apply-use-case <slug>`.

9. **If the doc declares any volumes**, once applied, suggest
   `/backup-use-case <slug>` to propose a backup strategy — a
   use-case with data and no backup plan is exactly the gap that command
   exists to close.

## What this command does NOT do

- Touch the node. Every node write — volumes, the env file, quadlets, units,
  firewall rules — belongs to the `/apply-use-case` it hands over to in
  step 8.
- Change an existing use-case. It refuses a slug that already exists; that
  is `/modify-use-case`.
- Guess an image tag. An unpinned or `:latest` image is a validation error,
  so it asks.
- Write a secret value. It writes the manifest — key names and purposes
  only; values are resolved at apply time through `secrets`.
- Capture a service that is already running on the box — that is
  `/onboard-node`, which reverse-engineers it into a `planned` document.
- Set up backups. It suggests `/backup-use-case` once the use-case is applied.
