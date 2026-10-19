# Use-Case Backup Manifest Convention

## Path

Backup manifests live at `nodes/<node>/use-cases/<slug>.backup.yaml`, as a
sibling to the use-case document at `nodes/<node>/use-cases/<slug>.md`.

The manifest is **entirely optional**, exactly like `<slug>.audit.yaml`:
a use-case with no volumes has nothing to back up,
and a use-case can run indefinitely without one. It exists once
`/backup-use-case <slug>` has run its setup phase.

`/backup-use-case` authors and refreshes it; `/restore-use-case` reads it and
updates its restore-test timestamps; `/remove-use-case` moves it into
`_retired/<slug>/` along with the rest of the `<slug>.*` siblings.

## Schema

```yaml
use_case: nextcloud
schema: 1

# ─── authored by /backup-use-case (setup) and admin tuning; safe to hand-edit ───

target: nas-primary                # name from the merged backup-targets.yaml
schedule: "03:30"                  # local time, or a raw systemd OnCalendar= expression
retention:
  keep: 14                         # newest N *sealed* snapshots kept on the target
consistency: stop                  # stop | live
volumes:
  - name: nextcloud-data
    exclude: ["appdata_*/", "*/cache/"]
  - name: nextcloud-db-data
    exclude: []
hooks:
  pre_backup:
    - container: nextcloud-db
      command: >-
        pg_dump -U nextcloud -Fc nextcloud
        > /var/lib/postgresql/data/backup/nextcloud.dump
  post_restore: []

# ─── written by /backup-use-case / /restore-use-case; do not hand-edit ───

automation_rendered_commit: null
last_backup: null
last_backup_outcome: null
last_snapshot: null
last_restore_test: null
last_restore_test_snapshot: null
```

### Field descriptions

#### Authored half

- **`use_case`** — Must equal the filename stem and match `<slug>.md:name`.
- **`schema`** — Integer schema version. Currently always `1`.
- **`target`** — A `name` from the merged `backup-targets.yaml` view
  (`conventions/references/backup-targets.md`). Refused if it names a target
  that doesn't exist after the merge.
- **`schedule`** — Either a plain `HH:MM` (24h, node-local time — rendered
  into a daily `OnCalendar=*-*-* HH:MM:00`) or a literal `OnCalendar=`
  expression for anything less common (weekly, twice-daily). Free text passed
  through verbatim in the second form, since `OnCalendar=` syntax is a
  systemd feature, not a distro dialect (the framework's one hard capability
  requirement, already guarantees systemd).
- **`retention.keep`** — How many **sealed** snapshots (see
  `backup-use-case/references/backup-recipes.md` § "Snapshot layout and
  seal") survive on the target; older sealed snapshots are pruned. An
  unsealed (partial/failed) snapshot is never counted towards `keep` and is
  pruned unconditionally on the next run.
- **`consistency`** — `stop` (default; the use-case's units stop before the
  copy and always restart after, success or failure) or `live` (volumes are
  copied while the use-case keeps running — safe only for data the
  application itself doesn't need frozen mid-write, e.g. static content, or
  a database whose `pre_backup` hook already produced a consistent dump).
- **`volumes`** — Which of the use-case's declared volumes to back up, and
  per-volume excludes (shell globs, relative to the volume root — same shape
  as `<slug>.audit.yaml:ignore[].path`). A volume the use-case declares but
  this list omits is never backed up; `/backup-use-case` setup proposes the
  full declared list and lets the admin narrow it. Two kinds of volume never
  appear here. A files volume (`source: files`) has its content in git. A
  volume another use-case shares in through `from:` belongs to its owner,
  and only the owner's manifest lists it.
- **`hooks.pre_backup`** / **`hooks.post_restore`** — Arrays of
  `{container, command}`. `pre_backup` runs (via `podman exec`) before the
  volume copy, in list order; `post_restore` runs after volumes are restored
  and units are back up, in list order. Each hook is generic — the command
  is free text the admin supplies (e.g. a database dump/load), and its output
  is expected to land **inside a declared volume** so the ordinary volume
  copy captures it. This is what lets a database use-case back up cleanly
  without this schema, or `/backup-use-case`, knowing anything about
  postgres, mysql, or any other engine.

#### Skill-written half

- **`automation_rendered_commit`** — Short commit SHA of the doc state the
  on-node timer/service/script were last rendered from. Lets activation
  detect "the manifest changed since the automation was rendered" without
  re-diffing the rendered text.
- **`last_backup`** — ISO 8601 UTC datetime of the most recent **journaled**
  backup run (a run the agent has recorded, whether it ran on-demand or was
  caught up from the on-node run log).
- **`last_backup_outcome`** — `success` | `partial` | `failed`, mirroring
  the journal's `outcome` enum for that run.
- **`last_snapshot`** — The snapshot id (see backup-recipes.md) of the most
  recent sealed snapshot.
- **`last_restore_test`** — ISO 8601 UTC datetime of the most recent
  `/restore-use-case --test` pass. `null` means the backup has never been
  proven restorable — a hypothesis, not a backup.
- **`last_restore_test_snapshot`** — Which snapshot id that test verified.

## Common shapes

Four generic starting points `/backup-use-case` setup proposes from, chosen
by what it can see in the declaration (mount targets, `ro` flags, container
count) — never by recognizing a specific application:

- **Static content** (a volume mounted read-write by exactly one container,
  no companion database container) → `consistency: live`, no hooks. The
  content changes by deploy, not by continuous writes, so a live copy is safe.
- **A database-shaped container** (a volume whose mount target matches a
  common data-directory naming pattern, e.g. ends in `/data` on a container
  with no `ro` mounts) → `consistency: stop` proposed by default;
  `consistency: live` with a `pre_backup` dump hook is offered as the
  lower-downtime alternative and requires the admin to supply the dump
  command themselves — this schema has no idea what database engine it is.
- **A multi-container stack** (`depends_on` chains, several containers
  sharing volumes) → `consistency: stop`, so the whole stack quiesces
  together rather than risking a copy that spans containers mid-write.
- **Config-only** (a volume holding only files the admin edits directly, no
  application writing to it at runtime) → `consistency: live`, `keep` set
  low (configuration churns rarely and every version is small).

## Example

See `../assets/example-vps/use-cases/nextcloud.backup.yaml` for a complete
example (a multi-container stack with a database dump hook).
