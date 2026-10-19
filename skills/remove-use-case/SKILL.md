---
name: remove-use-case
description: Retire a use-case — stop on-node services, remove their quadlets, remove firewall rules, move the in-repo doc + siblings under _retired/, journal. Preserves data and the env file. Use to decommission a use-case.
compatibility: Runs inside a drydocc fork; needs a shell and file edits.
metadata:
  skillset: drydocc
  requires: conventions node-context node-exec backup-use-case
---

# /remove-use-case

## Contract

- **Args:**
  - On the node: `<slug>`.
  - On the commissioning client: `<node>` `<slug>` — the target node is the
    **positional first argument** (required in client mode; `node-context`
    refuses without it).
- **Surface:** runs from either surface, resolved by `node-context`:
  - **`on_node`** (preferred) — VS Code Remote-SSH terminal or node-local
    `claude` CLI; commands run locally as written below.
  - **`on_client`** — the admin laptop drives the node over SSH; every
    node-touching command routes through `node-exec`. Use only when the node
    can't host a VS Code Remote Server.
  - Commands are agent-generated ad-hoc bash and journaled verbatim.
- **Reads:** `nodes/<self>/use-cases/<slug>.*`; on-node service state.
- **Writes:** moves in-repo files under
  `nodes/<self>/use-cases/_retired/`; updates frontmatter
  `status: retired`. Stops units, removes on-node quadlets, drops
  firewall rules.
- **Preserves on the node:** `/var/lib/drydocc/volumes/<slug>/` (data)
  and `/etc/drydocc/secrets/<slug>.env` (secrets) — admin removes
  these manually if they want a clean slate.
- **Journal scope:** `use-case/<slug>`.

## Procedure

1. **Bind `<self>` + surface** via `node-context`. It returns the surface
   (`on_node` / `on_client`) and, in client mode, the node connection block.
   **All on-node side effects below (stop units, remove quadlets,
   `daemon-reload`, drop firewall rules) run through `node-exec`** — locally when
   `on_node`, SSH-wrapped when `on_client`. The bash snippets are the `on_node`
   form. When `on_client` and `nodes/<self>/node.yaml:remote_ide.vscode_server`
   is `compatible`, first suggest connecting via VS Code Remote-SSH to
   `doccap@<tailscale_hostname>` and running `/remove-use-case` on the node
   instead; proceed over SSH only on confirmation
   `[decision: remove-use-case.continue-over-ssh]`.

1b. **Refuse while a shared volume is in use.** If any `spec.volumes.<vol>`
   sets `shared: true`, look for another use-case on this node, not retired,
   whose spec mounts `from: <slug>/<vol>`. If one exists, stop:
   `<slug> owns <vol>, which <consumer> still mounts. Remove or modify <consumer> first.`

2. **Confirm with the admin** `[decision: remove-use-case.confirm]`. Spell
   out:
   - Which units will be stopped and removed (`<slug>-*.service`), and every
     job's timer, service and script (`drydocc-job-<slug>-*`), each torn down
     per `podman-quadlets` § "Jobs", alarm disarm included.
   - That on-node quadlets and firewall rules will be removed.
   - If `<slug>.backup.yaml` exists: the backup timer will be disabled and
     removed, but every snapshot already on the target is **preserved** —
     retiring a use-case is not a reason to lose its history.
   - That bind-mounted data under `/var/lib/drydocc/volumes/<slug>/`
     and the env file at `/etc/drydocc/secrets/<slug>.env` are
     **preserved** — the retired doc carries a paste-able cleanup
     snippet for the admin to run when they want a clean slate.

3. **Stop units** in reverse dependency order, jobs first, so no job starts a
   container that is being removed:

   ```bash
   systemctl --user disable --now drydocc-job-<slug>-<name>.timer   # each job (full teardown: podman-quadlets § "Jobs")
   systemctl --user stop <slug>-<container>.service   # each container
   systemctl --user stop <slug>-<network>-network.service   # each network
   ```

   Quadlet units are generated and cannot be `disable`d; removing the quadlet
   files in step 4 is what keeps them from starting at the next boot
   (`podman-quadlets` § "Reload procedure").

4. **Remove on-node quadlets and reload:**

   ```bash
   rm -f ~/.config/containers/systemd/<slug>-*.container ~/.config/containers/systemd/<slug>-*.network
   systemctl --user daemon-reload
   ```

4b. **Remove the backup automation**, if `<slug>.backup.yaml` exists, per
   `backup-use-case/references/backup-recipes.md` § "Rendering the
   automation" (the disable/remove form), including disarming its missed-run
   alarm if the node has notification set up. Sealed snapshots already on the
   target are never touched — only the on-node timer, service, and script.

4c. **Drop the use-case from telemetry sampling.** If the node runs the
   telemetry sampler, re-render it without this use-case per
   `node-maintenance/references/maintenance-recipes.md` § "Telemetry
   sampling", or purge it there if nothing else is left to sample. The
   samples file `/var/lib/drydocc/telemetry/<slug>.samples` stays — like
   volumes, it is data, and the retired `<slug>.audit.yaml` baseline is what
   gives it meaning.

5. **Remove firewall rules** for each top-level `ports:` entry (single port
   or range) via the node's firewall
   tool (`node.yaml:system_facts.firewall.tool`), per
   `bootstrap-node/references/hardening-recipes.md` § "Use-case port rules"
   → **Remove**. Prefer the enumerate-then-remove form given there — a
   reconstructed rule string can fail to match what was actually written. If
   `system_facts.firewall.tag_mechanism` is `none`, rules cannot be
   identified by tag and cleanup will be incomplete.

6. **Mutate the doc frontmatter.** Set `status: retired`,
   `last_applied: null`. Add a `## Retirement` section to the body
   that:
   - Briefly states why.
   - Contains the verbatim cleanup snippet for the admin:
     ```bash
     # Run on the node to fully purge data and secrets:
     sudo rm -rf /var/lib/drydocc/volumes/<slug>/
     sudo rm -f /etc/drydocc/secrets/<slug>.env
     ```

7. **Move all `<slug>.*` siblings** (`.md`, `.secrets.yaml`, `.audit.yaml`,
   `.backup.yaml`) under `nodes/<self>/use-cases/_retired/<slug>/`. Create
   `_retired/` if it doesn't exist. (No `.quadlets/` directory exists —
   quadlets live only on the node.) The audit and backup manifests move unmodified — the audit
   manifest's `config_surfaces`/`fingerprints` and the backup manifest's
   `last_snapshot`/`last_restore_test*` are the record of what the service
   actually looked like and where its data last lived, which is worth keeping
   past decommissioning.

8. **Journal** the retirement, listing every command run.

9. **Commit + push** doc move + journal + `node.yaml` if the firewall state flipped. The
   commit originates from the agent's working copy — the node's when `on_node`,
   the laptop's when `on_client`. After an `on_client` retirement, remind the
   admin to `git pull` on the node's working copy (shared remote).

## What this command does NOT do

- It does not delete bind-mounted data under
  `/var/lib/drydocc/volumes/<slug>/`.
- It does not delete the env file at `/etc/drydocc/secrets/<slug>.env`.
- It does not delete any snapshot already on a backup target — only the
  on-node backup automation, if any.
- It does not remove the KeePass entry — that is a manual admin step.
