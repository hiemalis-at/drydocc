---
name: modify-use-case
description: Reconfigure an existing use-case from a mutation description (adding containers, changing ports, swapping dependencies, bumping image tags). Re-renders quadlets, restarts only what changed, and journals. Use for any change to an active use-case.
compatibility: Runs inside a drydocc fork; needs a shell and file edits.
metadata:
  skillset: drydocc
  requires: conventions secrets podman-quadlets node-context node-exec
---

# /modify-use-case

## Contract

- **Args:**
  - On the node: `<slug>` and `"<mutation description>"`.
  - On the commissioning client: `<node>` `<slug>` `"<mutation description>"` —
    the target node is the **positional first argument** (required in client
    mode; `node-context` refuses without it).
- **Surface:** runs from either surface, resolved by `node-context`:
  - **`on_node`** (preferred) — VS Code Remote-SSH terminal or node-local
    `claude` CLI; commands run locally as written below.
  - **`on_client`** — the admin laptop drives the node over SSH; every
    node-touching command routes through `node-exec` (long steps linger). Use
    only when the node can't host a VS Code Remote Server.
  - Commands are agent-generated ad-hoc bash + inline `python3 -c '...'`
    snippets and are journaled verbatim.
- **Reads:** `nodes/<self>/use-cases/<slug>.md` and siblings
  (`<slug>.secrets.yaml`, `<slug>.audit.yaml`); current on-node quadlets at
  `~/.config/containers/systemd/<slug>-*`.
- **Writes:** the same in-repo files, modified; replaces on-node
  quadlets that changed, writes those that are new, and deletes those the
  mutation dropped. Creates the host directory of any newly declared volume;
  never deletes a dropped volume's data. Lowers the unprivileged port floor
  when a new host port needs it. Refreshes the audit manifest and invalidates
  fingerprints the mutation supersedes.
- **Journal scope:** `use-case/<slug>`.

## Procedure

1. **Bind `<self>` + surface** via `node-context`. It returns the surface
   (`on_node` / `on_client`) and, in client mode, the node connection block.
   **All on-node reads and side effects below run through `node-exec`** —
   locally when `on_node`, SSH-wrapped when `on_client`. The bash snippets are
   the `on_node` form.

1b. **Surface nudge (client only).** When `surface == on_client`, read
   `nodes/<self>/node.yaml:remote_ide` and, if `vscode_server: compatible`,
   suggest connecting via VS Code Remote-SSH to `doccap@<tailscale_hostname>`
   and running `/modify-use-case` on the node instead; proceed over SSH only on
   confirmation `[decision: modify-use-case.continue-over-ssh]`. If `incompatible`, note SSH is the only surface (`<reason>`)
   and proceed. If `unknown`/absent, proceed and suggest re-running
   `/bootstrap-node <self>` to refresh the probe.

2. **Read the existing doc.** Refuse if the slug doesn't exist (suggest
   `/create-use-case`). Refuse if `last_applied` is null — the use-case has
   never been built on this node, and this skill only diffs against live
   quadlets: `<slug> has never been applied. Run /apply-use-case <slug> first.`

3. **Parse the mutation description.** Determine:
   - Fields to change in frontmatter (top-level summary list AND the
     corresponding `spec:` keys — they must stay in sync).
   - Body sections to extend (`Runbook` usually).
   - Whether `has_secrets` flips (new secrets manifest needed or old one
     to retire).
   - Whether an image tag is being bumped (triggers `podman pull`).
   - Which jobs (`spec.jobs`) the mutation adds, changes or drops.
   - Whether a files tree (`<slug>.files/<vol>/`) changes. The mutation may be
     nothing but such a change (an edited virtual host), with no frontmatter
     change at all. Compare the tree's sha256 list against the node's volume
     (`podman-quadlets` § "Files volumes", verification) to find out.
   - Which containers, networks, and volumes the mutation **adds** or
     **drops** — each one changes which on-node files exist, not just their
     content (steps 7–7c).
   - Whether the mutation adds, moves, or removes a **config surface** — a path
     the application writes through its own surfaces. Adding a mount to make an
     app-written path survive a rebuild is the canonical case, and it is exactly
     what `/audit-use-case` recommends when it finds writable-layer mutation.

3b. **Refresh the audit manifest** at `<slug>.audit.yaml`, per the `conventions`
   skill's `references/audit-manifest.md`:
   - Add, retarget, or drop `config_surfaces` entries to match step 3. A surface
     whose volume or path changed must be updated, or the next audit reports a
     phantom finding against a path that no longer exists.
   - **Invalidate stale fingerprints**: delete every `fingerprints` key whose
     surface this mutation touched, and every `writable_layer/<container>` key
     for a container whose image, mounts, or exec changed or that the
     mutation dropped. The next audit
     re-baselines those instead of alarming on a change the admin just made
     deliberately.
   - Create the manifest if the mutation introduces the first config surface;
     leave it absent if there still is none.

4. **Validate, then show the doc diff** to the admin before applying. The
   mutated document must pass the same checks as a first apply: the
   list ⇔ `spec` invariant, pinned images, and every container's validation
   rules in `podman-quadlets` § "Render from spec". On a failure, name the
   container and field and exit without writing. If the admin declines the
   diff `[decision: modify-use-case.confirm-diff]`, exit without writing.

5. **Apply the diff** to the doc and any siblings (including the audit
   manifest from step 3b).

6. **Re-render quadlets in-memory** via an inline `python3 -c '...'`
   snippet implementing `podman-quadlets` `## Render from spec`.

7. **Read current on-node quadlets and diff:**

   ```bash
   for f in ~/.config/containers/systemd/<slug>-*.container ~/.config/containers/systemd/<slug>-*.network; do
     [ -f "$f" ] && { echo "=== $f ==="; cat "$f"; }
   done
   ```

   Classify every file as **changed**, **added** (rendered, not on disk), or
   **dropped** (on disk, no longer rendered — a container or network the
   mutation removed). Show the unified diff, including each dropped file in
   full, to the admin; abort if they decline
   `[decision: modify-use-case.confirm-render]`. If a new host port (published, or
   a host-network container's `listens:`) is below the node's unprivileged port
   floor, list the floor change here too.

7b. **Prepare what new units need**, before any unit starts:
   - **New volumes.** For each volume the mutation adds to `spec.volumes`,
     create its host directory with the declared mode/owner (defaults `0750`,
     `doccap:doccap`), exactly as `/apply-use-case` step 7 does:
     `sudo install -d -o <owner> -g <group> -m <mode> /var/lib/drydocc/volumes/<slug>/<vol>`.
     Podman refuses to start a container whose bind-mount source does not
     exist, so this is not optional.
   - **New low ports.** If a new host port (published or `listens:`) is below
     the floor, lower it per
     `podman-quadlets` § "Privileged ports".

7c. **Take down what the mutation dropped**, in reverse dependency order —
   dependents first, so no running container loses a network under it:

   ```bash
   systemctl --user stop <slug>-<container>.service       # each dropped container
   systemctl --user stop <slug>-<network>-network.service # each dropped network
   rm -f ~/.config/containers/systemd/<slug>-<name>.<kind> # each dropped file
   ```

   A dropped **volume**'s host directory under
   `/var/lib/drydocc/volumes/<slug>/` is left in place — data is never
   deleted by a modify. Name it in the journal and in the closing message,
   with the command to remove it, as `/remove-use-case` does for a whole
   use-case.

8. **Write changed and added files** (per file: `printf '%s\n' "<text>" |
   install -m 644 /dev/stdin ~/.config/containers/systemd/<slug>-<name>.<kind>`).

9. **`systemctl --user daemon-reload`**.

10. **Targeted restart** — apply the minimum disruption. Quadlet units are
    generated, so bring them up with `start`/`restart`, never `enable`
    (`podman-quadlets` § "Reload procedure"). Rewrite the env file and pull
    new images (the secrets and image-tag bullets below) before starting or
    restarting anything that consumes them:
    - Added network or container → `systemctl --user start <unit>`, networks
      first, then containers in `depends_on` order.
    - Network unit changed → restart the network unit, then all
      containers on it.
    - Container env/mount/port/exec changed → `systemctl --user restart
      <slug>-<container>.service`.
    - Image tag changed, or a container added → `podman pull <new-image>`
      first, then restart (or, for an added container, start) its unit.
      (No `Pull=newer` / `AutoUpdate=`; the pinned tag IS the contract.)
      When `on_client`, run the pull via `node-exec` lingering
      (`dd-<slug>-pull`) so a large layer download survives an SSH
      disconnect.
    - Secrets manifest changed → re-resolve each key via `secrets`
      (env-var-first) to rewrite the env file, then restart every container
      that consumes it.
    - Job added or changed → re-render its script and units, `daemon-reload`,
      enable its timer, and run it once by hand as `/apply-use-case` step 8
      does. Job dropped → its teardown per `podman-quadlets` § "Jobs",
      alarm disarm included.
    - Files tree changed → re-sync the volume per `podman-quadlets`
      § "Files volumes", then restart every container that mounts it —
      including consumers in other use-cases that mount it through `from:`,
      which are named in the confirmation and the journal. A restart is
      needed even for a directory mount: most services read their
      configuration only at start.

11. **Verify** with `podman ps --filter name=<slug>-`; capture
    `journalctl --user -u <unit> -n 50` for any unit not `Up`. Mark
    `outcome: partial` on failure.

12. **Firewall delta.** Via the node's firewall tool
    (`node.yaml:system_facts.firewall.tool`), add rules for new top-level
    `ports:` entries and remove rules for entries that disappeared — single
    ports and ranges alike; concrete commands are in
    `bootstrap-node/references/hardening-recipes.md` § "Use-case port rules"
    (**Add** / **Remove**). Rules stay tagged with `<slug>`. If
    `system_facts.firewall.tag_mechanism` is `none`, old rules cannot be
    identified by tag and removal will fail.

13. **Update doc**: `last_applied`, `last_applied_commit`.

14. **Journal** the change with the doc diff and the on-node diff
    included in `## What was done`. List every shell command run in
    `## Commands`.

15. **Commit + push** doc + siblings (`<slug>.secrets.yaml`,
    `<slug>.audit.yaml`) + node.yaml (if the firewall state changed) + journal. The commit
    originates from the agent's working copy — the node's when `on_node`, the
    laptop's when `on_client`. After an `on_client` apply, remind the admin to
    `git pull` on the node's working copy (shared remote) to avoid divergence.

## What this command does NOT do

- Apply a use-case that has never been applied — it refuses; that is
  `/apply-use-case`.
- Delete data. A dropped volume's host directory stays on the node, and is
  named with the command to remove it.
- Retire a use-case — that is `/remove-use-case`.
- Raise the unprivileged port floor when a low port disappears; another
  use-case may still need it.
- Rotate a credential inside the application. It rewrites the env file and
  restarts the containers that read it; changing the credential the
  application itself holds is the admin's (`secrets` § "Rotation").
- Track image updates. The pinned tag is the contract — no `Pull=newer`, no
  `AutoUpdate=`; image currency is a `/node-maintenance` finding.
- Capture changes someone made directly on the node — `/audit-use-case`
  finds those and offers to write them back into the document.
