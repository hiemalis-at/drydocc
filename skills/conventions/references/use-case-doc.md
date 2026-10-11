# Use-Case Document Convention

## Path

Use-case documents live at `nodes/<node>/use-cases/<slug>.md`, where `<slug>` is a kebab-case identifier (e.g., `nextcloud`, `monitoring-stack`).

## Sibling-File Convention

To avoid a filesystem collision between a file named `<slug>.md` and a directory of the same name, all artifacts belonging to a use-case live as siblings with a dot-separated kind suffix:

- **`<slug>.md`** — this document
- **`<slug>.secrets.yaml`** — secrets manifest (required only if `has_secrets: true` in frontmatter; see `secrets.md`)
- **`<slug>.audit.yaml`** — audit manifest (entirely optional; see `audit-manifest.md`)
- **`<slug>.backup.yaml`** — backup manifest (entirely optional; see `backup-manifest.md`)

Quadlet files are rendered on apply from the doc's `spec:` block and live only on the node at `~/.config/containers/systemd/<slug>-*.{container,network}`. No `<slug>.quadlets/` directory exists in git.

This convention keeps all artifacts of a use-case greppable by slug and avoids the dir-vs-file name collision.

## Frontmatter Schema

Every use-case document opens with a YAML frontmatter block. This block is the machine-readable contract; all fields must be present. Here is the schema:

```yaml
---
name: nextcloud
status: active
owner: doccap
summary: Nextcloud + Postgres + eturnal TURN server
created: 2026-05-18
last_applied: 2026-05-18T14:22:00Z
last_applied_commit: a501e0b
depends_on: []
containers: [nextcloud-app, nextcloud-db, eturnal]
networks: [nextcloud-net]
volumes: [nextcloud-data, nextcloud-db-data]
ports: ["443/tcp", "3478/udp"]
has_secrets: true
spec:
  networks:
    nextcloud-net:
      internal: false                       # optional; default false
  volumes:
    nextcloud-data:
      mode: "0750"                          # optional; default 0750
      owner: "doccap:doccap"              # optional; default doccap:doccap
    nextcloud-db-data: {}
  containers:
    nextcloud-app:
      image: docker.io/nextcloud:29.0.4-apache   # PINNED TAG REQUIRED — :latest is a validation error
      network: nextcloud-net                # default <slug>-net
      env:
        NEXTCLOUD_LOGLEVEL: "info"
      env_from: /etc/drydocc/secrets/nextcloud.env  # required iff has_secrets: true
      mounts:
        - { volume: nextcloud-data, target: /var/www/html, ro: false }
      ports:
        - "443:443/tcp"                     # <host>:<container>/<proto>
      depends_on: [nextcloud-db]            # sibling container names → systemd After=/Requires=
      restart: on-failure                   # default on-failure
      exec: ""                              # optional; overrides CMD
---
```

### Field Descriptions

- **`name`** — Must equal the filename stem (e.g., `nextcloud`). Kebab-case, alphanumeric + hyphens only.
- **`status`** — Enum: `planned | active | retired`. Governs `/list-use-cases` filtering and which use-cases `/audit-use-case` selects when invoked without a slug (only `active` ones). `status` is itself audited for coherence (drift conditions defined in `audit-use-case/references/audit-recipes.md` § "Comparison table", Status coherence row). The lifecycle is one-directional and each transition has exactly one owner: a document is born `planned` (by `/create-use-case`'s authoring half or by `/onboard-node`'s scaffolding) and committed in that state; `/apply-use-case` is the **only** transition to `active`, and it sets `last_applied` in the same commit; `/remove-use-case` is the only transition to `retired`. A `planned` document with a null `last_applied` is therefore always safe to re-apply. `retired` means the use-case has been decommissioned; the document is preserved under `_retired/` as its final declared state; its history is the node journal's `scope: use-case/<slug>` entries. **Rebuild mode is a self-transition, not a new one**: `/apply-use-case` can re-apply an `active` document whose runtime footprint is entirely gone from the node, refreshing `last_applied`/`last_applied_commit` without changing `status`. The "always safe to re-apply" guarantee is not weakened by this — rebuild replaces the bookkeeping test with live evidence of total absence, confirmed by the admin before it proceeds.
- **`owner`** — Typically `doccap` (the agent account). May differ if a human or external system owns the service.
- **`summary`** — One-line human summary of what the use-case provides (e.g., "Nextcloud + Postgres + eturnal TURN server").
- **`created`** — ISO date (YYYY-MM-DD) when this use-case was first declared.
- **`last_applied`** — ISO datetime (YYYY-MM-DDTHH:MM:SSZ, UTC) when `/apply-use-case` or `/modify-use-case` last touched the live system. `null` if the use-case has never been applied.
- **`last_applied_commit`** — Short commit SHA (7–12 chars) matching the commit that journaled the most recent `/apply-use-case` or `/modify-use-case`. Allows diffing the state at that timestamp. `null` if never applied. **Not bumped by `/audit-use-case`** — this field means "last touched the live system", which an audit by construction never does; audit records its own timestamps in `<slug>.audit.yaml`.
- **`depends_on`** — List of other use-case slugs (strings) that must exist and be active on the same node before this one is safe to bring up (e.g., `["base-network", "storage"]`). Empty list if none.
- **`containers`** — List of container names (as they appear in quadlets and `podman ps`). May be empty if the use-case does not declare containers.
- **`networks`** — List of custom podman network names declared by this use-case. May be empty.
- **`volumes`** — List of named volumes declared by this use-case. May be empty.
- **`ports`** — List of strings in the format `"<port>/<proto>"` (always quoted to parse as strings, not numbers). Examples: `["443/tcp", "3478/udp"]`. May be empty. These ports are declared for the node's firewall rule set (per `system_facts.firewall.tool`).
- **`has_secrets`** — Boolean. If `true`, a sibling `<slug>.secrets.yaml` file must exist and contain the secrets manifest (see `secrets.md`). When `true`, every container in `spec.containers` that needs the values must set `env_from` to the manifest's `target_path` (typically `/etc/drydocc/secrets/<slug>.env`).
- **`spec`** — The implementation block consumed by the render function in `podman-quadlets` to produce on-node quadlets. It has three sub-keys: `networks`, `volumes`, `containers`. Invariant: the names under `spec.networks`/`spec.volumes`/`spec.containers` MUST equal the top-level `networks`/`volumes`/`containers` lists (the lists are the public summary `/list-use-cases` parses; `spec:` is the implementation detail). The full per-key schema lives in `skills/podman-quadlets/SKILL.md` under `## Render from spec`.

## Body Sections

After the frontmatter, the document continues with four required markdown sections in this order, plus one optional fifth (`Migration`) placed between `Runbook` and `Quadlets`:

### Intent

A narrative section (2–4 paragraphs) explaining:
- What this use-case serves (e.g., "Web CMS for team documentation").
- Why it exists (business or operational rationale).
- Who uses it and how.
- Any key dependencies or assumptions.

### Architecture

A technical section describing:
- All containers declared in frontmatter, their roles, and dependencies.
- Networks: custom networks, bridge/host modes, overlay topology if multi-node.
- Volumes: mount points, data lifecycle, backup strategy if relevant.
- How containers communicate (inter-container networking, port mappings).
- Any external dependencies (databases, caches, external APIs).

Include a diagram if helpful (e.g., ASCII or link to external).

### Runbook

Operational guidance:
- **Bring-up**: manual steps or agent command (`/apply-use-case`, the sole owner of the `planned → active` transition) to provision and start services.
- **Common operations**: scaling, backup/restore, config hot-reload.
- **Known issues**: workarounds, gotchas, performance tuning.
- **Debugging**: how to inspect logs, check container health, manually test endpoints.

Agent appends learnings here as `/audit-use-case` or human troubleshooting uncover issues. This is `/audit-use-case`'s `[n] note` capture-back choice: a dated bullet recording a finding the admin chose not to promote into `spec:`.

### Migration (optional)

Present only on a document that adopts a host-native service — either a mapped daemon being replaced by a container image, or a capture-only document for a service with no container equivalent yet. Absent on every other use-case.

This section is **parsed**, so its head is a fixed set of labelled lines, followed by free prose:

```
unit:      nginx.service
packages:  nginx, nginx-common
config:    /etc/nginx
data:      /var/www/html
ports:     80/tcp, 443/tcp
enabled:   true
```

Two of these fields are machine-consumed during an adoption cut-over, and each has exactly one job:

- **`unit:`** names the host unit — it is the only field that identifies what `/apply-use-case` disables.
- **`config:`** and **`data:`** are comma-separated host paths, and they are the only source of truth for what that command copies into the declared volumes. Nothing else in the document carries a legacy path: `spec.volumes.<vol>` holds mode and owner, and `spec.containers.*.mounts[]` holds the in-container target. How the paths are joined to volumes is `/apply-use-case`'s procedure, not this schema's.

`packages:`, `ports:`, and `enabled:` carry what the onboarding probe found and are read by humans only, so a capture-only document is a worklist item someone can act on rather than a dead end. A capture-only document additionally has empty `containers`/`networks`/`volumes` lists with matching empty `spec:` sub-keys; `/apply-use-case` refuses it until the section is worked up into a real `spec:` block.

### Quadlets

A one-line pointer: *"Generated on apply from frontmatter `spec:`. See on-node `~/.config/containers/systemd/<slug>-*.{container,network}`."* No in-repo quadlet files exist; the render mapping is documented in `skills/podman-quadlets/SKILL.md`.

## Retirement

When a use-case reaches end-of-life:
1. `/remove-use-case <slug>` sets `status: retired` in the frontmatter.
2. It moves the entire use-case (all `<slug>.*` siblings) to `nodes/<node>/use-cases/_retired/<slug>/`.
3. The document stays under `_retired/<slug>/` as the use-case's final declared state. Its history — the create, every apply and modify, and the retirement itself — is the node journal's `scope: use-case/<slug>` entries; there is no second record. The retired entry no longer appears in `/list-use-cases` output (status filter).

## Example

See `../assets/example-vps/use-cases/nextcloud.md` for a fully-fleshed example conforming to this convention.
