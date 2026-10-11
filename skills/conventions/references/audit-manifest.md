# Use-Case Audit Manifest Convention

## Path

Audit manifests live at `nodes/<node>/use-cases/<slug>.audit.yaml`, as a sibling
to the use-case document at `nodes/<node>/use-cases/<slug>.md`.

The manifest is **entirely optional**. Every declared-vs-live check and every
universal unmanaged-mutation probe in `/audit-use-case` runs without it — see
`audit-use-case/references/audit-recipes.md` § "Comparison table" for the check set. The manifest carries only what the agent *cannot*
derive from `<slug>.md`: what "well" means for this particular service, which
paths are configuration surfaces rather than churn, and the fingerprint baseline
that makes "changed since the last audit" answerable at all.

`/create-use-case` authors a starter manifest; `/modify-use-case` refreshes it;
`/remove-use-case` moves it into `_retired/<slug>/` along with the rest of the
`<slug>.*` siblings, with no special handling.

## Schema

```yaml
use_case: nextcloud
schema: 1

# ─── authored by /create-use-case + /modify-use-case; safe to hand-edit ───

expectations:
  - id: no-failed-jobs
    description: Nextcloud reports a healthy install
    probe: podman exec nextcloud-app php occ status --output=json
    expect: exit-zero
    severity: warn

config_surfaces:
  - volume: nextcloud-data
    path: config/config.php
    kind: app-config
    note: the admin UI writes trusted_domains here

ignore:
  - volume: nextcloud-data
    path: data/appdata_*/

telemetry:
  # ─── authored; every field optional, defaults shown ───
  enabled: true
  window: 7d
  sample_every: 15min
  thresholds:
    mem_factor: 1.5
    cpu_factor: 2.0
    log_factor: 3
    log_floor: 5
  growth:
    min_pct_per_day: 5
    min_r2: 0.8
  exclude_windows:
    - on: "Sun *-*-* 02:00"
      for: 2h
      note: weekly occ files:scan rescans every user's data

  # ─── written by /node-maintenance; do not hand-edit ───
  last_evaluated: 2026-09-12T04:31:00Z
  baseline:
    established: 2026-09-05T04:30:00Z
    commit: a501e0b
    window_start: 2026-08-29T04:30:00Z
    window_end: 2026-09-05T04:30:00Z
    samples: 612
    image_digests:
      nextcloud-app: "sha256:77c1…"
    containers:
      nextcloud-app:
        mem_bytes: {p50: 412000000, p95: 530000000, max: 610000000}
        cpu_pct:   {p50: 1.8, p95: 14.0, max: 88.0}
        warn_per_day: {p50: 3, p95: 9}
        err_per_day:  {p50: 0, p95: 1}

# ─── written by /audit-use-case; do not hand-edit ───

last_audited: 2026-07-26T18:00:00Z
last_audited_commit: a501e0b
fingerprints:
  nextcloud-data/config/config.php: "sha256:1f4c9a…"
  writable_layer/nextcloud-app: "sha256:9ab2f0…"
```

### Field Descriptions

- **`use_case`** — Must equal the filename stem and match `<slug>.md:name`.
  Links the manifest to its use-case document.
- **`schema`** — Integer schema version. Currently always `1`. Lets a future
  change to this file's shape be detected rather than mis-parsed.

#### Authored half

- **`expectations`** — Optional array of use-case-specific probes. This is the
  vocabulary for "is *this* service actually well", which no universal check can
  express. Omit or leave empty when the universal checks suffice.
  - **`id`** — kebab-case identifier, unique within the manifest. Appears in the
    drift report and in the journal, so keep it stable across edits.
  - **`description`** — One-line human statement of what a pass means.
  - **`probe`** — The command to run on the node. Routed through `node-exec`
    like every other probe, so it must be a short, **read-only, idempotent**
    one-shot. A probe that mutates state is a bug: `/audit-use-case` never
    changes the node.
  - **`expect`** — How to judge the probe. One of:
    - `exit-zero` — the command exits `0`.
    - `stdout-contains:<string>` — stdout contains the literal substring.
    - `stdout-json:<dotted.path>=<value>` — stdout parses as JSON and the value
      at the dotted path equals `<value>`.
  - **`severity`** — `info` | `warn` | `critical`. Governs how the finding is
    ranked in the report. A failed `critical` expectation makes the audit's
    `outcome: partial`, like any other drift.
- **`config_surfaces`** — Optional array declaring paths that the application
  writes through its **own** surfaces — an admin UI, a settings menu, a
  service-specific CLI. These are persisted (they live in a bind mount) but
  undeclared (`spec:` cannot express them), so without this list a rebuild
  silently loses them. Declaring a surface makes it fingerprinted and diffed.
  - **`volume`** — Name of a declared volume; must appear in `<slug>.md`'s
    `volumes:` list.
  - **`path`** — File or directory, **relative to the volume root**. Never
    absolute — the host path is derived as
    `/var/lib/drydocc/volumes/<slug>/<volume>/<path>`.
  - **`kind`** — Free-form short label (`app-config`, `plugin-state`,
    `theme`, …). Reported verbatim; not interpreted.
  - **`note`** — One line on what writes this path and why it matters. This is
    what a future admin reads when deciding whether a change is expected.
- **`ignore`** — Optional array of `{volume, path}` entries, same shape as
  `config_surfaces`, for paths that legitimately churn (caches, session stores,
  generated thumbnails). `path` may contain shell globs. Matching findings are
  suppressed entirely. Populated by the `[i] ignore` choice in the capture-back
  menu.

#### Telemetry block

Optional. Present only once `/node-maintenance`'s telemetry items have
evaluated this use-case, or when the admin authors thresholds ahead of that.
It is split like the rest of the file, by its own marker comment, and has a
**different writer** from the audit-written half: `/node-maintenance` writes
`telemetry.last_evaluated` and `telemetry.baseline`, and `/audit-use-case`
never touches anything under `telemetry:`. The baseline and anomaly rules that
consume these fields are in `node-maintenance/references/maintenance-recipes.md`
§ "Baselines and anomalies".

Authored (all optional; defaults shown in the schema above):

- **`telemetry.enabled`** — `false` excludes this use-case from sampling and
  from both telemetry posture items. Default `true`.
- **`telemetry.window`** — Duration a baseline is computed over. Default `7d`.
  A baseline is established only once the window spans at least three days.
- **`telemetry.sample_every`** — Sampler interval for this node's timer.
  Default `15min`. The node samples at the shortest interval any of its
  use-cases asks for.
- **`telemetry.thresholds.mem_factor`** / **`cpu_factor`** / **`log_factor`**
  — Multipliers applied to the baseline p95 before a deviation counts.
- **`telemetry.thresholds.log_floor`** — Minimum error count in 24 h before
  `log-errors` can fire, however quiet the baseline.
- **`telemetry.growth.min_pct_per_day`** / **`min_r2`** — The memory-growth
  trend a leak finding needs, and how well the trend must fit.
- **`telemetry.exclude_windows[]`** — Known load, declared rather than
  guessed. Samples inside a window never enter a baseline or an anomaly test.
  - **`on`** — A systemd `OnCalendar=` expression for when the window opens.
  - **`for`** — Duration it stays open.
  - **`note`** — What the load is.

  The use-case's own backup window is excluded automatically, from its backup
  manifest; do not declare it again.

Written by `/node-maintenance`:

- **`telemetry.last_evaluated`** — ISO 8601 UTC timestamp of the last sweep
  that evaluated this use-case.
- **`telemetry.baseline`** — `null` until established. Then:
  - **`established`**, **`window_start`**, **`window_end`** — ISO 8601 UTC.
  - **`commit`** — Short SHA of `HEAD` when it was established. If
    `<slug>.md:last_applied_commit` is newer, the baseline is **stale** and is
    rebuilt, never compared against.
  - **`samples`** — Retained sample count after exclusions.
  - **`image_digests`** — Map of container → image digest at establishment.
    A differing digest makes the baseline stale.
  - **`containers.<name>`** — Statistics only: `mem_bytes` and `cpu_pct` as
    `{p50, p95, max}`; `warn_per_day` and `err_per_day` as `{p50, p95}`.

Never in this block: a log line, an excerpt, or anything derived from log
*content*. The sampler counts lines by priority and discards them.

#### Audit-written half

Everything below the marker comment is rewritten by `/audit-use-case` on each
run. Hand-editing is not an error, but the next audit overwrites it.

- **`last_audited`** — ISO 8601 UTC datetime (`YYYY-MM-DDTHH:MM:SSZ`) of the
  most recent completed audit. `/list-use-cases` renders the date portion.
- **`last_audited_commit`** — Short SHA (7–12 chars) of `HEAD` when that audit
  ran. Together with `<slug>.md:last_applied_commit` this reveals a **stale
  baseline**: if the use-case was applied after the last audit, the recorded
  fingerprints describe a superseded configuration and are re-baselined rather
  than reported as drift.
- **`fingerprints`** — Map of surface key → `sha256:<digest>`. Two key shapes:
  - `<volume>/<path>` — digest of a declared `config_surfaces` entry (for a
    directory, the digest is taken over the sorted `path|size|mtime` listing).
  - `writable_layer/<container>` — digest of that container's `podman diff`
    listing, minus paths falling under a declared mount target. A change here
    means files were written into the container's **writable layer**, which a
    rebuild destroys.

## Contract and Security

### Values never in git

Identical to the secrets manifest rule. The audit manifest records *structure,
paths, and digests* — never content. Specifically:

- `expectations[].probe` output is compared and discarded, never stored. Write
  probes whose output is not itself sensitive.
- Env files yield **key names only** (`cut -d= -f1`); values are never read,
  compared, hashed, or journaled.
- `fingerprints` are one-way digests, not content. A digest of a file holding
  secrets is still safe to commit, but prefer not to declare such a file as a
  `config_surface` at all — that is what `<slug>.secrets.yaml` is for.

### Who may write it

`/audit-use-case` may write this manifest — except the `telemetry:` block,
which belongs to `/node-maintenance` and is preserved verbatim on every audit
rewrite — and — with explicit per-finding
confirmation — promote a `node-ahead` finding into `<slug>.md` or
`<slug>.secrets.yaml`, or append a learning to the doc's `## Runbook`. It may
**never** mutate live node state; live reconciliation is delegated to
`/modify-use-case`.

`--report-only` suppresses all repo writes, including this manifest, giving a
genuinely read-only mode.

### Relationship to `last_applied_commit`

`<slug>.md:last_applied_commit` means "the commit that journaled the last action
which **touched the live system**". `/audit-use-case` does not touch the live
system and therefore never bumps it. Audit's own timestamps live here, in
`last_audited` / `last_audited_commit`.

## Example

See `../assets/example-vps/use-cases/nextcloud.audit.yaml` for a complete
example, matching the `nextcloud.md` and `nextcloud.secrets.yaml` fixtures.
