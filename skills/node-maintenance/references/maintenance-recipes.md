# Node Maintenance Recipes

The concrete layer behind `/node-maintenance`: the posture catalog, the
inventory join that decides what applies, the probes that measure it, and the
commands each outcome composes once derived.

**This file does not duplicate other skills' commands.** Where a maintenance
outcome's remedy already belongs to another command — hardening repair to
`/bootstrap-node`, backup setup and restore proof to `/backup-use-case` and
`/restore-use-case`, credential rotation to `secrets` — this file derives the
*finding* and emits the invocation. It never re-implements the fix. Schemas
live in the `conventions` skill; this file assumes them.

## Contents

- [The posture catalog](#the-posture-catalog) — the eleven outcomes, closed set
- [The inventory join](#the-inventory-join) — what is read, and what it decides
- [The bundled assessment probe](#the-bundled-assessment-probe) — one read-only
  round trip
- [Outstanding OS package updates are applied](#outstanding-os-package-updates-are-applied)
  — outcome contract
- [The intrusion-prevention ban ledger is readable](#the-intrusion-prevention-ban-ledger-is-readable)
  — outcome contract
- [TLS material is inventoried with its expiry](#tls-material-is-inventoried-with-its-expiry)
  — outcome contract
- [Image currency and targeted restart](#image-currency-and-targeted-restart)
- [Log hygiene](#log-hygiene)
- [Fact freshness](#fact-freshness)
- [Backup coverage and proof](#backup-coverage-and-proof)
- [Credential rotation windows](#credential-rotation-windows)
- [Tailnet reachability](#tailnet-reachability)
- [Telemetry sampling](#telemetry-sampling)
- [Baselines and anomalies](#baselines-and-anomalies)
- [Risk and cost classification](#risk-and-cost-classification)
- [The findings report](#the-findings-report)
- [Rendering the automation](#rendering-the-automation)
- [Run log](#run-log)
- [What this file does NOT do](#what-this-file-does-not-do)

---

## The posture catalog

Eleven outcomes. The set is **closed** — a fork cannot add an id, because an id
without a derivation and a verification here is a promise nothing can keep.
Adding one is a change to this file and the skill that reads it.

Each item states an end state. What work that implies is derived per run from
the inventory; on a node where the triggering inventory is absent, the item
reports `n/a` and costs nothing.

| id | Outcome — true when… | Applies when | Disposition |
|---|---|---|---|
| `container-images-current` | every declared container runs what its pinned tag resolves to today | ≥1 active use-case declares containers | **acts** |
| `os-packages-current` | no outstanding security-relevant package updates remain | always | **acts** |
| `logs-bounded` | no log store on the node grows without bound | always | **acts** |
| `node-facts-fresh` | `system_facts` describes the box as it is now | always | **acts** |
| `tls-material-valid` | no TLS material sits inside its renewal window | ≥1 active use-case declares a TLS-bearing port or a certificate volume | **acts** where a renewer exists |
| `intrusion-prevention-reviewed` | the ban ledger has been read and anomalies surfaced | `hardening.fail2ban` is `enabled` | **reports** |
| `tailnet-healthy` | the node is reachable on the tailnet under its declared name | `hardening.tailscale` is `joined` | **reports** → `/bootstrap-node` |
| `data-recoverable` | every active use-case's data is backed up **and proven restorable** | ≥1 active use-case declares volumes | **reports** → `/backup-use-case`, `/restore-use-case --test` |
| `credentials-within-rotation-window` | no declared secret is past its stated rotation window | ≥1 secrets manifest declares `rotates:` | **reports** → `secrets` |
| `resources-within-baseline` | every container's CPU and memory behave the way its own baseline says they do | ≥1 active use-case declares containers | **reports**; acts only on an accepted re-base |
| `logs-within-baseline` | no container's error output has risen above its own baseline | ≥1 active use-case declares containers | **reports**; acts only on an accepted re-base |

**Acts** means this skill owns the remedy and will perform it on acceptance.
**Reports** means another command owns it; the finding ends in a ready-to-paste
invocation, never in this skill doing the work itself. The split is not
stylistic — a remedy implemented twice drifts, and the second copy is the one
nobody updates.

---

## The inventory join

Built from the working copy before anything touches the node. Every input
already exists; none of it is collected specially for maintenance.

| Source | Fields read | Decides |
|---|---|---|
| `nodes/maintenance.yaml` + `nodes/<node>/maintenance.yaml` | merged `posture[]`, `schedule`, skill-written half | which items run, in which mode, and the reported deviations |
| `nodes/<node>/use-cases/*.md` (excluding `_retired/`) | `status`, `containers`, `ports`, `volumes`, `spec.containers.*.image`, `has_secrets`, `last_applied` | the applicability of image, TLS, log and backup items |
| `nodes/<node>/use-cases/*.backup.yaml` | `schedule`, `retention.keep`, `automation_rendered_commit`, `last_backup*`, `last_restore_test*` | every `data-recoverable` finding |
| `nodes/backup-targets.yaml` + node layer | merged target list | whether a declared backup has a target that still exists |
| `nodes/<node>/use-cases/*.secrets.yaml` | key names, `rotates`, `purpose` | `credentials-within-rotation-window` |
| `nodes/<node>/use-cases/*.audit.yaml` | the `telemetry:` block only | thresholds, exclusions, and the baseline both telemetry items judge against |
| `nodes/notify.yaml` + node layer | presence, event routing | whether the rendered automation publishes, and what |
| `nodes/<node>/node.yaml` | `system_facts.*`, `hardening.*`, `tailscale_hostname`, `detected` | dialect selection, and the applicability of the security items |
| `nodes/<node>/journal/*.md` | most recent `scope: maintenance` entry | how long since the last sweep |

Two rules govern the join:

1. **Only `status: active` use-cases contribute work.** A `planned` use-case
   has no runtime to maintain; a `retired` one is gone. `planned` docs are
   still counted in the report's header line, because a node with six planned
   and one active use-case is a node in the middle of something.
2. **Absent input is a finding, not an error.** No backup manifest for an
   active use-case with volumes is precisely what `data-recoverable` exists to
   say. No `maintenance.yaml` anywhere means first run. An absent
   `system_facts` block means every fact is unknown and `node-facts-fresh`
   fires with maximum urgency.

### Fork-wide awareness

The posture merge is what makes one node's sweep aware of the infrastructure.
Two things are reported from it on every run:

- **Deviations** — each merged item whose `mode` came from the node layer
  rather than the fork-wide layer, with its `note`. An `off` with no note is
  itself worth flagging.
- **Absent fork-wide layer** — if `nodes/maintenance.yaml` does not exist,
  there is no infrastructure-wide bar at all and every node is improvising.
  Say so once, and offer to author it.

Sibling nodes' own directories are **read-only** to this run and only their
`maintenance.yaml` is consulted, to answer "is this node the exception?". No
sibling node's state is probed, written, or connected to.

---

## The bundled assessment probe

One read-only round trip, issued as a single `NODE_RUN` with a `bash -s`
heredoc, following the shape `/audit-use-case`'s Tier 1 probe uses. It emits
`<key>=<value>` lines on stdout and nothing else, so a partial failure inside
the script costs one key rather than the whole assessment.

Header assignments the agent substitutes before sending:

```bash
SLUGS="<space-separated active slugs>"
IMAGES="<space-separated pinned image references>"
PKG_CHECK="<confirmed update-check idiom — see the package-update contract>"
BAN_READ="<confirmed ban-ledger read idiom — see the ban-ledger contract>"
TLS_PATHS="<space-separated candidate certificate paths>"
```

Every unresolved substitution is left **empty**, never guessed. An empty
`PKG_CHECK` makes the probe emit `packages=no-probe`, which the report renders
as "no verified update-check idiom for this node's package manager yet" — an
honest gap, not a silent pass. This is the same discipline `audit-recipes.md`
applies to its firewall read-back.

Keys the probe emits:

| key | value | feeds |
|---|---|---|
| `uptime_s` | seconds since boot | reboot-pending context |
| `reboot_required` | `yes` / `no` / `unknown` | `os-packages-current` |
| `packages` | count of pending updates, or `no-probe` | `os-packages-current` |
| `image:<ref>` | local digest of that pinned reference | `container-images-current` |
| `unit:<name>` | `active` / `failed` / `inactive` | restart targeting |
| `jlog_user_bytes` | disk used by the user journal | `logs-bounded` |
| `jlog_system_bytes` | disk used by the system journal | `logs-bounded` |
| `runlog:<slug>` | byte size of that use-case's backup run log | `logs-bounded` |
| `wlayer:<container>` | writable-layer size | `logs-bounded` |
| `bans` | ban count, or `no-probe` | `intrusion-prevention-reviewed` |
| `tls:<path>` | expiry as a UTC date, or `unreadable` | `tls-material-valid` |
| `tailnet` | `online` / `offline` / `no-probe` | `tailnet-healthy` |
| `timer:<slug>` | backup timer state | `data-recoverable` |
| `fact:<key>` | re-probed `system_facts` value | `node-facts-fresh` |
| `tsamples:<slug>` | `<line count>\|<newest sample epoch>`, or `absent` | both telemetry items |
| `telemetry_timer` | `active` / `inactive` / `absent` | both telemetry items |
| `notify_helper` | `present` / `absent` | whether the automation can publish |

The probe **never writes**, never installs, and never restarts anything. A node
that is unreachable produces no keys at all; the run continues against the
repo-only half of the inventory and marks every node-dependent item `unknown`,
which is a materially useful report on its own — it still finds an active
use-case that has never had a restore tested.

---

## Outstanding OS package updates are applied

**Goal.** The node carries no outstanding security-relevant package updates
from its configured sources, and the agent knows whether the result requires a
reboot to take effect.

**Fact.** `system_facts.package_manager` — already discovered by
`/bootstrap-node` or `/onboard-node`. This section never re-derives it; it
reuses the confirmed value exactly as
`backup-use-case/references/backup-recipes.md` § "Backup tooling" does. When
the key is absent or `unknown`, do not guess: run the manager-detection probe
that `bootstrap-node/references/hardening-recipes.md` § "Package installation"
already specifies, record the result, and continue from there.

**Deriving an unseen dialect.** Any package manager that can serve this outcome
has four properties, and they are what to look for rather than a name:

1. A **refresh** operation that updates its index of available versions from
   configured sources, without changing installed packages.
2. A **query** operation that lists upgradable packages and exits zero when
   there are none — read-only, safe to run on every sweep.
3. An **apply** operation that upgrades packages already installed, and that
   can be run without interactive prompts.
4. Ideally, a way to distinguish security-relevant updates from the rest. Many
   managers have no such distinction; where it is absent, treat every pending
   update as in scope and say so in the report rather than inventing a filter.

Separately, determine whether the box signals a **pending reboot** — usually a
sentinel file or a manager sub-command. If nothing on the system signals it,
emit `reboot_required=unknown` rather than `no`; claiming a reboot is
unnecessary when nothing was asked is worse than admitting ignorance.

If nothing on the box manages packages, this item reports `n/a` permanently.
There is no fallback and none is wanted — a node whose packages nothing tracks
is not a node this outcome can be true of, and refusing to invent one is the
honest result. The refusal names the failing check, never the system.

**Verification.** After the apply operation exits zero, re-run the **query**
operation from property 2. The outcome holds when it reports nothing pending.
Any package that remains pending after a successful apply is reported by name —
held back versions and dependency conflicts are normal, and silently counting
them as done would make the outcome a lie. Re-check the reboot signal after the
apply, not before.

**Do not proceed on an unverified derivation.** A query idiom that has not been
observed to exit zero on a clean system cannot distinguish "nothing pending"
from "command misused", and the whole item rests on that distinction.

**Record.** Write the confirmed manager to
`node.yaml:system_facts.package_manager` if it was absent or corrected, journal
the run, and append the verified refresh/query/apply triple to Observed
dialects below.

### Observed dialects — evidence, not the supported list

| package_manager | refresh + query | apply | reboot signal | verified on | date |
|---|---|---|---|---|---|
<!-- No rows yet: this contract has not been exercised against a live node.
     Add a row the first time a triple is derived and verified on real
     hardware, per the derivation rules in
     conventions/references/distro-agnosticism.md. A missing row means
     nobody has been there yet — never that a manager is unsupported. -->

---

## The intrusion-prevention ban ledger is readable

**Goal.** The agent can enumerate the node's currently banned sources and the
jails producing them, so a run can surface anomalies — a jail that has banned
nothing in months, a ban count that jumped an order of magnitude, a jail that
is configured but not running.

This item **reports only**. It never unbans, never edits jail configuration,
and never changes the intrusion-prevention service's state; that is node
hardening, and re-running `/bootstrap-node` owns it.

**Fact.** `hardening.fail2ban` says only *whether* intrusion prevention is on.
There is deliberately no `system_facts` key naming the tool, and none should be
added — a fact earns a slot only when a skill branches on it structurally, and
this is probe-on-demand tool presence. Record what is found in the open
`system_facts.capabilities` map instead, as `present` or `absent`.

**Deriving an unseen dialect.** An intrusion-prevention implementation that can
serve this outcome offers:

1. A **status** query naming the active jails (or the equivalent grouping),
   read-only.
2. A **per-jail** query listing currently banned sources and a cumulative
   count.
3. Both runnable without stopping or reloading the service.

Where the implementation exposes only an aggregate count and no per-jail
breakdown, take the aggregate and report the reduced fidelity rather than
skipping the item. Where the service is enabled but exposes no machine-readable
ledger at all, emit `bans=no-probe`; an unreadable ledger is itself the
finding, because nobody is reviewing what they cannot read.

**Verification.** The status query exits zero and names at least one jail on a
node where `hardening.fail2ban` is `enabled`. Zero jails on an enabled service
is a **finding**, not a verification failure — it means protection is running
and guarding nothing, which is exactly the kind of thing this sweep exists to
notice.

**Record.** `system_facts.capabilities.<discovered tool>: present`, journaled,
plus a row below. The ban counts themselves are **not** written to git — they
are volatile operational state, and the journal entry carries the narrative.

### Observed dialects — evidence, not the supported list

| capability | status query | per-jail query | verified on | date |
|---|---|---|---|---|
<!-- No rows yet — unexercised against a live node. The framework's baseline
     installs fail2ban, so that is the expected first row; it is not the
     only legal one. -->

---

## TLS material is inventoried with its expiry

**Goal.** Every certificate a use-case depends on is known to the run, with its
expiry, so material inside its renewal window is surfaced before it expires
rather than after.

**Fact.** No `system_facts` key, for the same reason as the ban ledger: this is
probe-on-demand tool presence, recorded under `system_facts.capabilities`.
Candidate certificate paths come from the **inventory**, not from a filesystem
sweep: an active use-case's declared volumes, and the conventional bind-mount
root for each. A node-wide search for certificate-shaped files is explicitly
not done — it is slow, it finds material no use-case declares, and acting on
undeclared material is the opposite of what the framework does.

**Deriving an unseen dialect.** Two capabilities, and they are independent:

1. **Reading an expiry** from a certificate file. Any implementation that can
   print a certificate's `notAfter` in a parseable form will do. This is the
   part that must exist for the item to function at all; without it, emit
   `tls:<path>=unreadable` and report that the node has TLS material nobody can
   inspect.
2. **Renewing**, which is optional and separate. A renewer is recognised by:
   it owns the material (it issued it), it can be invoked non-interactively,
   and it exits non-zero when renewal fails rather than leaving stale material
   in place. Where a use-case's certificates are issued by something the node
   does not run — an upstream proxy, a platform-managed certificate, material
   an admin copies in by hand — there is **no renewer**, and the correct
   behaviour is to report the expiry and stop. Inventing a renewal for material
   the node does not own is how a working service breaks at 04:30.

**Verification.** After any renewal, re-read the expiry from property 1 and
confirm it moved further out than it was. A renewer that exits zero without
advancing `notAfter` has not renewed anything, and this is the only check that
catches it. Then confirm the consuming container has picked the material up —
most do not re-read certificates without a reload — and reload it if the
use-case declares how.

**Do not proceed on an unverified derivation.** Never schedule a renewal
unattended that has not been watched to succeed interactively on this node; the
promotion gate in `conventions/references/maintenance-manifest.md` enforces
this for every item, and it matters most here.

**Record.** `system_facts.capabilities` for the reader and, if present, the
renewer. Expiry dates are **not** written to git — they change on every
renewal and the node is their source of truth.

### Observed dialects — evidence, not the supported list

| capability | read expiry | renew | verified on | date |
|---|---|---|---|---|
<!-- No rows yet — unexercised against a live node. -->

---

## Image currency and targeted restart

No dialect: podman is the framework's target shape, so it is named directly.

Applies to every container declared by an active use-case. The declaration
pins a tag, and a pinned tag is not immutable — upstreams rebuild tags for
security fixes, which is the entire reason this item exists.

1. **Resolve what is running.** For each declared image reference, read the
   local digest:

   ```bash
   podman image inspect --format '{{.Digest}}' <ref>
   ```

2. **Resolve what the tag points at now**, without changing anything:

   ```bash
   podman manifest inspect <ref>
   ```

   A registry that cannot be reached is `unknown` for that image, not
   `current`. Rate limits are common; report them as what they are.

3. **Compare.** Equal digests mean current. Different digests mean the tag has
   been rebuilt and the node is behind.

4. **Pull, on acceptance only** — this is a long step and lingers as
   `dd-maintenance-images`:

   ```bash
   podman pull <ref>
   ```

5. **Restart only what actually changed.** Map each refreshed image back to the
   containers that declare it, and restart those units alone:

   ```bash
   systemctl --user restart <slug>-<role>.service
   ```

   Respect `depends_on`: restart in declared dependency order, and where a
   use-case declares a dependency chain, restarting the dependency first avoids
   a dependent coming up against a service that is still going down.

**Verification.** After the restart, the unit is `active` and the container's
digest matches what was pulled. A unit that comes back `failed` is reported
immediately with its last log lines, and the run does not continue restarting
further containers of that same use-case — one broken restart is a finding, a
cascade is an outage.

Pulling without restarting is a legitimate, lower-risk choice and is offered
separately in the menu: the new image sits on disk and takes effect at the next
natural restart.

---

## Log hygiene

No dialect: journald and podman are both target shape.

Three stores grow, and they grow for different reasons:

1. **The systemd journals** (user and system). Bounded by configuration, not
   by deletion. Read current usage:

   ```bash
   journalctl --user --disk-usage
   journalctl --disk-usage
   ```

   Where usage exceeds the threshold the run reports against, vacuum to a
   bounded size or age:

   ```bash
   journalctl --user --vacuum-size=<limit>
   ```

   Prefer setting a durable limit in the journal's own configuration over
   repeatedly vacuuming by hand — a sweep that has to vacuum every month is
   reporting a missing configuration, and should say so rather than quietly
   absorbing the work forever.

2. **Backup run logs** — `/var/lib/drydocc/backups/<slug>.runlog`, one line per
   run, forever. `backup-use-case/references/backup-recipes.md` § "Run log"
   defines the format and explicitly leaves rotation here. Rotate by truncating
   to the most recent entries, never by deleting the file: the file's existence
   is what tells `/backup-use-case` where to resume its journal catch-up, and
   lines older than the manifest's `last_backup` have already been journaled
   and are safe to drop.

3. **Container writable layers**, which is a different problem wearing the same
   clothes:

   ```bash
   podman ps --size --format json
   ```

   Growth here is an application writing outside its declared mounts. This item
   **reports** it and does not prune — the data is undeclared, a rebuild would
   destroy it, and deciding what it is belongs to `/audit-use-case`'s
   unmanaged-mutation findings. Emit the finding with a pointer to that command.

4. **Telemetry samples** — `/var/lib/drydocc/telemetry/<slug>.samples`. Keep
   twice the use-case's `telemetry.window` and drop older lines; a baseline is
   never computed from more than one window, and the second window is what an
   accepted re-base reads. Truncate in place, like the backup run logs.

5. **The notification log** — `/var/lib/drydocc/notify.log`. Keep the last
   30 days. Lines older than `last_maintenance` have been folded into a journal
   entry already.

**Verification.** Re-read the usage figure after acting and confirm it dropped
below the threshold. For the run log, the samples files, and the notification
log, confirm the file still exists and its newest line is unchanged.

This is the lowest-risk item in the catalog and the natural first candidate for
promotion to unattended — with the writable-layer half staying report-only
regardless of mode, because it never acts.

---

## Fact freshness

`node.yaml:system_facts.detected` records when the node's facts were last
established. Facts drift: a box gains a firewall, loses a tool, changes its
service configuration, or is upgraded across a major release that renames its
own idioms.

Report the age of `detected`. Where it exceeds the staleness window the run
uses, or where `system_facts` is absent entirely, re-run the same probes
`/bootstrap-node` uses to establish facts, compare each against what is
recorded, and write back **only** the keys that actually changed.

This is the one item that writes `node.yaml`, and it writes exactly one block.
`hardening.*` is never touched here even when a probe reveals it is wrong —
that block describes protections whose repair belongs to the bootstrap
audit-and-repair pass, and recording a corrected value without repairing it
would make the file claim a posture nobody restored. Report the discrepancy and
emit `/bootstrap-node <node>` instead.

**Verification.** Re-read the written file and confirm each changed key parses
and holds the probed value. Set `detected` to today only after the write
succeeds — a `detected` date ahead of the facts it claims to describe is worse
than a stale one.

An **open vocabulary** applies: a probe returning a value nobody has recorded
before is legal and is written verbatim. It is never normalised to something
already in the file, and it never causes a refusal on its own.

---

## Backup coverage and proof

Pure inventory reasoning plus two probed facts. This item **reports**; every
remedy is another command's.

Findings, in descending order of seriousness:

0. **Restore owed.** The use-case owes a restore per
   `conventions/references/journal.md` § "Rebuilt, not yet restored": it was
   rebuilt and its data never came back, whatever its volumes now contain.
   Emit `/restore-use-case <slug>`, and suppress finding 5 for it — a timer
   that is not running is the safe state until the restore happens.
1. **Unprotected.** An active use-case declaring volumes with no
   `<slug>.backup.yaml` at all. Emit `/backup-use-case <slug>`.
2. **Unproven.** A manifest whose `last_restore_test` is `null`. A backup
   nobody has restored is a hypothesis, not a backup — this is the finding the
   whole item exists for, and it is reported at the same weight as having no
   backup, because operationally it is nearly the same thing. Emit
   `/restore-use-case <slug> --test`.
3. **Stale proof.** `last_restore_test` older than every snapshot currently
   retained — roughly `retention.keep` × the backup interval. The proof refers
   to data that no longer exists on the target.
4. **Stale automation.** The manifest's content no longer matches
   `automation_rendered_commit`: the live timer was rendered from an older
   manifest. Emit `/backup-use-case <slug>`.
5. **Not running.** `timer:<slug>` reports the backup timer inactive or absent
   while a manifest exists.
6. **Overdue.** `last_backup` older than `schedule` implies, allowing one
   missed cycle before reporting — a node that was off overnight is not a
   failure.
7. **Orphaned target.** The manifest names a `target` that no longer exists in
   the merged target list.

The probed half is small:

```bash
systemctl --user is-active drydocc-backup-<slug>.timer
systemctl --user list-timers --all drydocc-backup-<slug>.timer
```

**Verification.** None — this item changes nothing. Its correctness is the
correctness of the inventory join, and a finding it emits is verified by the
command it hands off to.

---

## Credential rotation windows

Pure repo reasoning. Read every active use-case's `<slug>.secrets.yaml` and
each merged backup target's key list, and compare each key's `rotates:` window
against the last recorded rotation.

Report keys past their window, keys with no recorded rotation at all, and keys
whose window the node cannot evaluate. Emit the `secrets` invocation for each.

This item **never rotates anything**, and the reason is worth stating: rotation
is a multi-step operation that updates a vault, rewrites an env file atomically,
and reloads the consuming services in an order that depends on the use-case. It
belongs to the command that owns secrets, and performing it from a maintenance
sweep — least of all an unattended one — would risk leaving a service holding a
credential that no longer works.

Never read, print, or journal a secret **value**. Key names, purposes, and
dates only. A key marked `rotates: never` is skipped silently; some keys, such
as the passphrase encrypting existing backup snapshots, make every prior
snapshot unreadable if rotated, and the correct number of times to suggest that
is zero.

---

## Tailnet reachability

Reports only. Confirm the node is online on the tailnet under the name
`node.yaml:tailscale_hostname` records:

```bash
tailscale status --json
```

Findings: the daemon not running; the node online under a **different** name
than the profile records (a rename nobody wrote down, which breaks every
`on_client` invocation); expiring node keys; and the profile claiming
`hardening.tailscale: joined` while the node is not. A node recorded as
`deferred` — hardened, its tailnet join still owed (a lab-built node) — is
not `joined`, so this item does not apply to it; the owed join is
`/bootstrap-node`'s to finish, not a health failure.

Every remedy here is bootstrap's — emit `/bootstrap-node <node>` and stop. A
maintenance sweep that re-authenticates a tailnet is a sweep that can lock an
admin out of the box it is running on.

Where the daemon is absent entirely, this is `n/a` rather than a failure; a
node reached on its fallback port is a supported shape.

---

## Telemetry sampling

No dialect: podman, journald, and systemd timers are target shape. The only
derivation is which `podman stats` template fields yield raw numbers on this
node's podman, and that is verified on first render (below).

The sampler is a **one-shot timer, not a daemon**. Rendered on-node only,
never committed.

- **Script** — `~/.config/drydocc/telemetry.sh`. Takes a lock
  (`/var/lib/drydocc/telemetry.lock`, `flock -n`), reads the previous sample
  time from `/var/lib/drydocc/telemetry/last`, and for each active use-case
  whose `telemetry.enabled` is not `false`, for each declared container,
  appends one line to `/var/lib/drydocc/telemetry/<slug>.samples`:

  ```
  <epoch>|<container>|<cpu_pct>|<mem_bytes>|<mem_limit_bytes>|<wlayer_bytes>|<restart_count>|<oom_killed 0|1>|<warn_n>|<err_n>
  ```

  A field that could not be read is `-`, never `0`: a stopped container has no
  memory figure, and zero would enter the baseline as a real one.

  The readings:

  ```bash
  podman stats --no-stream --no-reset --format '{{.Name}}|{{.ContainerStats.CPU}}|{{.ContainerStats.MemUsage}}|{{.ContainerStats.MemLimit}}' <container…>
  podman ps --all --size --format json --filter name=^<container>$
  podman inspect --format '{{.RestartCount}}|{{.State.OOMKilled}}' <container>
  journalctl --user -u <unit> --since "@<prev_epoch>" -p warning --output=cat --quiet | wc -l
  journalctl --user -u <unit> --since "@<prev_epoch>" -p err     --output=cat --quiet | wc -l
  ```

  The top-level `{{.MemUsage}}` and `{{.CPUPerc}}` template fields are
  formatted for people (`30.43MB / 33.56GB`, `0.73%`); the
  `{{.ContainerStats.…}}` fields are the raw numbers. `wlayer_bytes` is
  `.Size.rwSize` from the `ps` JSON.

  `-p warning` includes everything more severe, so `warn_n` is the first
  count minus the second. `<unit>` is the container's quadlet-generated
  service. The `wc -l` is the whole point: **lines are counted and dropped,
  never stored**, because log content can carry secrets and these numbers end
  up in git.

- **Service unit** — `~/.config/systemd/user/drydocc-telemetry.service`,
  `Type=oneshot`, `ExecStart=` the script.
- **Timer unit** — `~/.config/systemd/user/drydocc-telemetry.timer`,
  `OnCalendar=` derived from the shortest `telemetry.sample_every` among the
  node's use-cases (`15min` → `*:0/15`), `AccuracySec=1min`. **Not**
  `Persistent=true`: a sample missed while the node was down is simply absent,
  and firing a catch-up sample at boot would record boot load as normal.

  At the end of a successful run, `dd_hb_arm telemetry <interval-seconds>` (helper
  sourced as in § "Rendering the automation"). The helper throttles re-arming,
  so a 15-minute sampler does not spend the notification budget.

```bash
systemctl --user daemon-reload
systemctl --user enable --now drydocc-telemetry.timer
```

Purge (both telemetry items `off`, or no active use-case with containers):

```bash
systemctl --user disable --now drydocc-telemetry.timer drydocc-telemetry.service
rm -f ~/.config/systemd/user/drydocc-telemetry.{service,timer} ~/.config/drydocc/telemetry.sh
systemctl --user daemon-reload
```

then `dd_hb_disarm telemetry`. Samples files are left in place; they are data,
and `logs-bounded` bounds them.

**Verification (first render on a node).** Run the service once by hand, then
compare the line it appended against `podman stats --no-stream` for the same
container: `mem_bytes` must be an integer number of bytes within a few percent
of the human-readable figure, and `cpu_pct` a bare number. If the template
yields unit-suffixed strings on this podman, the derivation is wrong — fix the
format fields before enabling the timer, and record what worked below.

### Observed podman template fields — evidence, not the supported list

| podman version | stats fields that yield raw numbers | verified on |
|---|---|---|
| 5.8.4 | `.ContainerStats.CPU` (float percent), `.ContainerStats.MemUsage` and `.ContainerStats.MemLimit` (integer bytes); **not** `.MemUsage`/`.MemUsageBytes`/`.CPUPerc`, which are human-formatted | commissioning client (WSL2), 2026-09-13 — re-verify on each node's first render |

---

## Baselines and anomalies

Both telemetry items judge a use-case against **its own** past, never against
a fleet norm or a fixed number. Everything here is statistics over the samples
file; nothing reads log content.

### Which samples count

1. Take lines for the container newer than `now − telemetry.window`
   (default `7d`) and with numeric fields.
2. Drop samples inside an **exclusion window**:
   - the use-case's backup windows — each scheduled start from
     `<slug>.backup.yaml:schedule` (`systemd-analyze calendar
     --iterations=<n> '<schedule>'` lists them) until the first backup run-log
     line after it, plus one sample interval; two hours when there is no such
     line;
   - each authored `telemetry.exclude_windows[]` entry, expanded the same way
     from `on` for `for`.
3. What remains is the **retained** set.

### Establishing a baseline

A baseline is established when there is none, or the current one is stale,
and the retained set spans **at least three days** and holds **at least 200
samples**. Otherwise the items report `N/A` with the date the window will be
full — "not enough history" is not a pass.

Per container, compute over the retained set:

- `mem_bytes` and `cpu_pct` — p50, p95, max;
- `warn_per_day` and `err_per_day` — sum `warn_n` / `err_n` per UTC day, then
  p50 and p95 over the days.

Percentile of one column (field 4 is `mem_bytes`):

```bash
awk -F'|' -v c="<container>" -v from="<epoch>" '$2==c && $1>=from && $4!="-" {print $4}' <slug>.samples \
  | sort -n | awk '{v[NR]=$1} END {if (NR) {i=int(NR*0.95+0.5); if (i<1) i=1; print v[i]}}'
```

Record `commit` (short `HEAD`), the window bounds, the retained count, and
each container's current image digest (`podman inspect --format
'{{.ImageDigest}}'`). Write them to `telemetry.baseline` — statistics only.

**Verification.** Recompute one container's `mem_bytes` p95 with the snippet
above over the recorded window and confirm it equals the value about to be
written. A baseline written from a different window than the one recorded is
worse than none.

### When a baseline is stale

Any of these makes a baseline stale. A stale baseline is **never compared
against**: the item reports "re-baselining since <reason>" and establishes a
new one once enough post-change history exists.

- `<slug>.md:last_applied_commit` is newer than `baseline.commit` — the
  declaration changed; last month's normal described a different service.
- A container's image digest differs from `baseline.image_digests`.
- `<slug>.backup.yaml:last_restore` (a live restore) is newer than
  `baseline.established`.

An admin's `[r] rebase` replaces a baseline with one computed from the most
recent full window. **Nothing else replaces one** — above all, not an anomaly.
Re-basing automatically on a deviation would teach the baseline that a leak is
normal.

### Anomaly rules

Each rule compares the **last 24 hours** of retained samples against a fresh,
non-stale baseline. A deviation must be sustained to count; a single spike
never is.

| rule | fires when | severity |
|---|---|---|
| `mem-over-baseline` | last-24h p95 of `mem_bytes` > baseline p95 × `mem_factor`, **and** ≥ 25% of last-24h samples exceed that line | warn |
| `mem-growth` | daily-p50 `mem_bytes` trend ≥ `min_pct_per_day` of the window mean, fit R² ≥ `min_r2`, over ≥ 3 days, with no `restart_count` increase in between | warn; **critical** when the trend reaches `mem_limit_bytes` within 7 days |
| `cpu-sustained` | last-24h **p50** of `cpu_pct` > baseline p95 × `cpu_factor` | warn |
| `log-errors` | last-24h `err_n` sum > max(baseline `err_per_day` p95 × `log_factor`, `log_floor`) | warn |
| `restart-or-oom` | `restart_count` rose, or `oom_killed` is `1`, since the last sweep | critical — needs no baseline |
| `wlayer-growth` | `wlayer_bytes` trend is positive with R² ≥ `min_r2` over the window | reported and handed to `/audit-use-case <slug>` |

`resources-within-baseline` owns the memory, CPU, restart, and writable-layer
rules; `logs-within-baseline` owns `log-errors`. Defaults for every factor are
in `conventions/references/audit-manifest.md` § "Telemetry block".

The memory-growth trend, from `<day index>|<daily p50>` pairs:

```bash
awk -F'|' '{n++; x+=$1; y+=$2; xx+=$1*$1; yy+=$2*$2; xy+=$1*$2}
  END { if (n<3) exit 1
        b=(n*xy-x*y)/(n*xx-x*x); a=(y-b*x)/n
        r2=((n*xy-x*y)^2)/((n*xx-x*x)*(n*yy-y*y))
        printf "slope_pct_per_day=%.2f r2=%.3f intercept=%d\n", 100*b/(y/n), r2, a }'
```

Every finding states the numbers it was decided on — baseline figure,
current figure, factor — so the admin can check the judgement rather than
trust it:

```
nextcloud-app  mem-growth  +9.1%/day (R² 0.93, 4 days)  412 MB → 560 MB; limit 2 GB reached in ~9 days
```

When the unattended sweep raises, escalates, or clears a finding, it publishes
`telemetry-anomaly` with the use-case, container, rule id, and severity — never
a figure that could identify data and never a log line.

---

## Risk and cost classification

Every finding carries both, and they are shown before the admin accepts
anything. Risk is about what could break; cost is about how long the node is
degraded.

| Risk | Meaning | Items |
|---|---|---|
| `none` | read-only, or writes only repo state | ban review, backup coverage, credential windows, tailnet, writable-layer growth, telemetry evaluation, baseline re-base |
| `low` | changes node state, no service interruption | image pull without restart, log vacuum, run-log and samples rotation, fact write-back, rendering the telemetry sampler |
| `restart` | one or more units restart; that use-case is briefly unavailable | image refresh with restart, certificate renewal with reload |
| `reboot` | the node goes down and comes back | package updates that set the reboot signal |

| Cost | Meaning |
|---|---|
| `none` | no interruption |
| `seconds` | a unit restart |
| `minutes` | a package transaction, a large image pull |
| `downtime` | a reboot, with everything on the node unavailable until it returns |

Two rules bind these to behaviour:

- **`reboot` is never unattended.** Regardless of `mode`, an item that would
  reboot the node reports and stops. The unattended half may apply updates; it
  may never act on the reboot they request. An admin decides when a box goes
  down.
- **`restart` unattended requires the promotion gate**, like every `auto` item,
  and the report says which use-cases would be interrupted before the admin
  promotes it.

---

## The findings report

Rendered before anything is accepted. Summary line first, then one row per
posture item, then the deviations, then the menu.

```
Maintenance posture for <node> — last sweep <date> (<N> days ago)

| item                              | mode   | verdict | finding                                | risk    | cost     |
|-----------------------------------|--------|---------|----------------------------------------|---------|----------|
| container-images-current          | report | DRIFT   | 2 of 5 images behind their pinned tag  | restart | seconds  |
| os-packages-current               | report | DRIFT   | 14 pending, reboot required            | reboot  | downtime |
| logs-bounded                      | auto   | OK      | user journal 180 MB, within limit      | low     | none     |
| node-facts-fresh                  | report | DRIFT   | facts last probed 118 days ago         | low     | none     |
| tls-material-valid                | off    | SKIPPED | disabled on this node: upstream-only   | —       | —        |
| intrusion-prevention-reviewed     | report | OK      | 3 jails, 41 bans, none anomalous       | none    | none     |
| tailnet-healthy                   | report | OK      | online as <hostname>                   | none    | none     |
| data-recoverable                  | report | DRIFT   | 1 use-case unproven, 1 unprotected     | none    | none     |
| credentials-within-rotation-window| report | UNKNOWN | node unreachable; repo-only assessment | none    | none     |
| resources-within-baseline         | report | DRIFT   | nextcloud-app mem-growth +9%/day       | none    | none     |
| logs-within-baseline              | report | N/A     | matrix: baseline possible 2026-09-17   | none    | none     |

Posture deviations from the fork-wide bar:
  logs-bounded          auto  (promoted here; fork-wide is report)
  tls-material-valid    off   ("upstream-only; edge proxy terminates TLS")

Delegated — these are not this command's to fix:
  /restore-use-case nextcloud --test   backup never proven restorable
  /backup-use-case matrix              active use-case, no backup strategy
  /bootstrap-node <node>               hardening says fail2ban enabled; service is not running
```

Verdicts are `OK`, `DRIFT`, `UNKNOWN` (probe failed or node unreachable),
`SKIPPED` (`mode: off`), and `N/A` (the inventory makes it inapplicable).
`UNKNOWN` is never rendered as `OK`.

### Menu copy

```
Act on which findings?

  [a] all acting items          — everything marked risk none/low
  [i] images                    — pull and restart 2 containers   (restart, seconds)
  [p] packages                  — apply 14 updates                (reboot requested, minutes)
  [l] logs                      — vacuum journal, rotate run logs (low, none)
  [f] facts                     — re-probe and write system_facts (low, none)
  [r] rebase <slug>             — accept current behaviour as normal (none, none)
  [n] none                      — report only, change nothing
  [v] view                      — show the exact commands for an item first

Delegated findings are not in this list; run the commands above yourself.
```

### Branch semantics

- **`a`** — every item whose risk is `none` or `low`. Never includes `restart`
  or `reboot`; those are always chosen explicitly by their own key.
- **`r`** — re-base one use-case's telemetry. Show its baseline statistics
  beside the current window's first, then write a new baseline from the most
  recent full window per § "Baselines and anomalies". Never part of `a`, and
  never offered for more than one use-case per keypress: accepting a new normal
  is a judgement about one service.
- **`i`/`p`/`l`/`f`** — one item. Offered only when that item produced a
  finding and its `mode` is not `off`.
- **`p`** applies updates and then **reports** the reboot request. It never
  reboots. Rebooting is a separate, explicit confirmation after the updates
  have been verified.
- **`n`** — the run still journals and still writes `last_maintenance`. A sweep
  that found things and changed nothing is a real, useful outcome and the
  journal should say it happened.
- **`v`** — print the composed commands for an item, then re-present this menu.
  Nothing is executed.

After a successful interactive execution of an item, offer to promote it to
`mode: auto` — and only then, since that success is exactly the evidence the
promotion gate asks for.

---

## Rendering the automation

Rendered on-node only, never committed — the same treatment quadlets and the
backup automation get.

- **Script** — `~/.config/drydocc/maintenance.sh`: runs the assessment probe,
  appends its findings to the run log, and executes **only** the items the
  merged manifest marks `auto`. It takes a lock so two sweeps cannot overlap:

  ```bash
  exec 9>"/var/lib/drydocc/maintenance.lock"
  flock -n 9 || { echo "maintenance already running"; exit 1; }
  ```

  The script contains no interactive prompt and no reboot path. Items whose
  risk is `reboot` are assessed and logged, never applied, regardless of mode —
  the script cannot be configured into rebooting the node.

  It evaluates the telemetry items against thresholds **frozen into the
  script at render time** — each container's baseline p95 multiplied by its
  factor — using the same computations as § "Baselines and anomalies". It
  keeps the set of open telemetry findings in
  `/var/lib/drydocc/telemetry/findings.state` and publishes only when that set
  changes. Because the thresholds are frozen, a re-based or newly established
  baseline makes the automation stale exactly as a manifest change does.

  Notification is optional and never fails the script. Source the helper if
  it exists and stub it if not:

  ```bash
  if [ -r "$HOME/.config/drydocc/notify.sh" ]; then
    . "$HOME/.config/drydocc/notify.sh"
  else
    dd_notify() { :; }; dd_hb_arm() { :; }; dd_hb_disarm() { :; }; dd_check_overdue() { :; }
  fi
  ```

  At the end of a run: on success, `dd_hb_arm maintenance <interval-seconds>`; on
  a non-zero exit, `dd_notify run-failed "maintenance on <node> failed" "rc=<rc>"`.
  When the set of `DRIFT` items differs from the previous run's, publish
  `maintenance-drift` with the item ids. For every job whose alarm cannot be
  armed because its interval is too long, call `dd_check_overdue` — see
  `node-notify/references/notify-recipes.md` § "Jobs too long to arm". The
  maintenance job's own alarm is what makes that check trustworthy.

- **Service unit** — `~/.config/systemd/user/drydocc-maintenance.service`:
  `Type=oneshot`, `ExecStart=` the rendered script.

- **Timer unit** — `~/.config/systemd/user/drydocc-maintenance.timer`:
  `OnCalendar=` from the merged `schedule` — the same expression the
  missed-run alarm's `<interval-seconds>` is computed from at render time
  (`node-notify/references/notify-recipes.md` § "Arming a missed-run alarm") —
  plus

  ```
  Persistent=true
  RandomizedDelaySec=900
  ```

  `Persistent=true` fires a missed run once the node is back rather than
  skipping a cycle. `RandomizedDelaySec` keeps every node in a fork from
  hitting the same registry and package sources at the same minute — a fleet
  that shares one posture shares one schedule, and that is worth smearing.

```bash
systemctl --user daemon-reload
systemctl --user enable --now drydocc-maintenance.timer
```

Purge (`--setup`, or when the posture turns every item `off`):

```bash
systemctl --user disable --now drydocc-maintenance.timer drydocc-maintenance.service
rm -f ~/.config/systemd/user/drydocc-maintenance.{service,timer} ~/.config/drydocc/maintenance.sh
systemctl --user daemon-reload
```

Then disarm the job's missed-run alarm (`dd_hb_disarm maintenance`, per
`node-notify/references/notify-recipes.md` § "Disarming a missed-run alarm") —
a purged job that stays armed pages the admin about a timer that was removed on
purpose.

"Stale" for this automation means either the merged manifest has changed
since `automation_rendered_commit`, or any active use-case's
`telemetry.baseline` has changed since then.

The timer is rendered but **not enabled** until at least one interactive run has
completed on the node — until then there is nothing in `verified_auto`, so an
unattended run could only assess, and an assessment nobody reads is noise.

---

## Run log

Every automation run appends one line per posture item to
`/var/lib/drydocc/maintenance.runlog` on the node:

```
<ts-iso-utc>|<item-id>|<verdict>|<acted:yes|no>|<rc>|<one-line summary>
```

This is what lets an unattended run be journaled **after the fact**. The next
interactive run reads every line newer than the manifest's `last_maintenance`
and turns them into that run's journal narrative, without an agent having been
present when the timer fired — the mechanism `/backup-use-case` established for
its own run log.

The summary field is single-line and carries no secret values, no ban source
addresses, and no certificate contents. It is written to a node-local file that
later becomes git data via the journal, and the rule that secrets are not git
data holds all the way through.

This file is itself rotated by `logs-bounded`, which is as it should be: the
sweep that bounds every other log bounds its own.

---

## What this file does NOT do

- **Re-implement another command's remedy.** Hardening repair, backup setup,
  restore proof, and credential rotation are all delegated with an emitted
  invocation. A remedy implemented in two places drifts, and the copy nobody
  looks at is the one that rots.
- **Reboot a node unattended**, or configure a path that could. The reboot
  decision is an admin's, every time.
- **Keep log content.** The sampler counts lines by priority and discards them.
  A finding says *how many* errors, never *what* they said.
- **Alert in real time.** Samples are taken on an interval and judged at sweep
  time; a spike that comes and goes between samples is not seen, by design.
- **Probe or write sibling nodes.** A run reads the fork-wide posture to know
  whether this node is the exception; it touches nothing under another node's
  directory.
- **Extend the posture catalog per fork.** The catalog is closed. An outcome
  without a derivation and a verification is a promise nothing can keep.
- **Prune undeclared data.** Writable-layer growth and undeclared volumes are
  reported and handed to `/audit-use-case`; deleting data no declaration
  describes is not a maintenance task.
