# Rebuild Recipes

The inventory, ordering, triage and reporting that `/rebuild-node` composes.

**This file does not duplicate other skills' commands.** Hardening, provisioning,
quadlet rendering, firewall rules, secrets, backup transport and restore are
unchanged and owned elsewhere. This file only adds what a node-wide recovery
needs on top of them. Almost everything here is plain procedure over the
framework's target shape (git, podman, systemd): the one step whose syntax
varies by node, bringing the base back, belongs entirely to `/bootstrap-node`,
which derives it per box.

## Contents

- [The recovery inventory](#the-recovery-inventory)
- [Recovery order](#recovery-order)
- [Known losses](#known-losses)
- [Replacement host keys](#replacement-host-keys)
- [The bundled triage probe](#the-bundled-triage-probe)
- [Triage classification](#triage-classification)
- [Replacement platform](#replacement-platform)
- [The recovery plan](#the-recovery-plan)
- [Unattended work to re-arm](#unattended-work-to-re-arm)
- [The recovery report](#the-recovery-report)

---

## The recovery inventory

Built from the working copy before anything touches the node, so a node that
cannot be reached still yields a real plan.

| Source | Fields read | Decides |
|---|---|---|
| `nodes/<node>/node.yaml` | `tailscale_hostname`, `ssh_fallback_port`, `ssh.key_name`, `identity.*`, `arch`, `system_facts.service_manager`, `provisioning.source`, `last_bootstrapped` | reachability, base readiness, platform comparison |
| `nodes/<node>/use-cases/*.md` (never `_retired/`) | `status`, `depends_on`, `spec.containers` (empty or not), `spec.containers.*.image`, `volumes`, `has_secrets`, `last_applied`, `last_applied_commit` | the recoverable set, the order, the evidence |
| `nodes/<node>/use-cases/*.secrets.yaml` | `keepass_entry` | which KeePass entries the plan names |
| `nodes/<node>/use-cases/*.backup.yaml` | `target`, `volumes[].name`, `consistency`, `last_snapshot`, `last_restore_test` | which volumes have a backup, downtime, whether the backup was ever proven |
| `nodes/backup-targets.yaml` + node layer | merged targets: `type`, `ssh_key_name`, `keepass_entry` | credentials a rebuilt box must re-materialize |
| `nodes/maintenance.yaml` + node layer | `automation_rendered_commit`, `verified_auto` | whether a maintenance timer existed |
| `nodes/notify.yaml` + node layer | presence | whether notification existed |
| `nodes/<node>/journal/*.md` | `scope`, `trigger`, `mode`, `outcome`, `timestamp` | the rebuilt-not-restored query; the latest audit findings; earlier `recovery` runs |

A document is:

- **recoverable** — `status: active` and `spec.containers` non-empty;
- **not declarative** — `status: active`, `spec.containers` empty (a
  capture-only host service). Its `## Migration` head (`unit:`, `packages:`,
  `data:`) goes into the report as the manual worklist;
- **listed only** — `status: planned`.

---

## Recovery order

A topological sort of the recoverable set over each document's frontmatter
`depends_on` (use-case slugs on the same node). Container order *within* a
use-case is `/apply-use-case`'s business and is not considered here.

1. Nodes: every recoverable slug. Edges: `dep → slug` for each `dep` in
   `slug`'s `depends_on`.
2. A `dep` that is not a recoverable slug on this node — missing, `planned`,
   `retired`, or not declarative — makes `slug` **blocked** from the start,
   and transitively everything that depends on `slug`. Remove those from the
   sort and report the reason.
3. Repeatedly take every slug with no remaining incoming edge, **in slug
   order** (so two runs on the same declarations produce the same plan), and
   append it to the order.
4. Slugs left with incoming edges form a cycle: refuse and name them.

---

## Known losses

What the declaration already says will not come back, stated in the plan
before anything runs:

- **Volumes with no backup** — every declared volume of a recoverable
  use-case that has no `<slug>.backup.yaml`, or that its manifest's
  `volumes[]` omits.
- **Unproven backups** — `last_restore_test: null`. The restore is still the
  only copy there is; say that it has never been proven and may fail.
- **Excluded paths** — each backed-up volume's `volumes[].exclude` globs, by
  volume.
- **Audit findings** — in the most recent `/audit-use-case` journal entry for
  each use-case, every finding with recovery `lost on rebuild` (writable-layer
  state) or `not rebuildable` (live state no declaration covers). Quote the
  finding and its date; an audit older than the use-case's `last_applied` is
  reported as stale evidence, not dropped.
- **Unreadable targets** — an S3 target whose encryption passphrase the admin
  no longer has makes every snapshot on it unreadable. Ask before planning a
  restore from one.

---

## Replacement host keys

**Goal.** The client reaches the replacement box under the node's name without
ever trusting a changed host key the admin cannot account for.

A rebuilt or replaced box answering on the same tailnet name or address
presents a new SSH host key, so the client's strict host-key check fails on
the first `node-exec` call. That failure looks identical to a man-in-the-middle.
Treat it as one until the admin says otherwise.

1. Detect it: the connection fails with a host-key-changed or
   host-key-verification error, not a timeout or a refused key.
2. Show the evidence: the recorded `node.yaml:identity.host_key_fingerprint`,
   the fingerprint the box offers now, and the recovery reason.

   ```bash
   ssh-keyscan -p <ssh_fallback_port> <tailscale_hostname> 2>/dev/null | ssh-keygen -lf -
   ```

3. Ask: *"The box answering as `<tailscale_hostname>` presents a new host key.
   Did you replace or reinstall `<node>`?"* If not, **stop**. An unexplained
   host-key change is a security incident, not a rebuild. Journal
   `outcome: failed` and say so.
4. On yes, remove only the stale entries for this node, then reconnect. The
   `node-exec` wrapper's accept-new policy records the new key.

   ```bash
   ssh-keygen -R "<tailscale_hostname>"
   ssh-keygen -R "[<tailscale_hostname>]:<ssh_fallback_port>"
   ```

**Verification.** The next `node-exec` call succeeds, and step 6's probe emits
`node_name=<node>`. `/bootstrap-node` then records the new fingerprint into
`identity`. Journal both fingerprints and the admin's confirmation.

The tailnet has the same problem. The dead device may still hold
`<tailscale_hostname>`, so the replacement joins as a suffixed name and the
connection block points at the wrong device. When bootstrap's tailnet
cut-over fails this way, name the stale device in the report. Removing it
is the admin's action in the tailnet's own console; this command never
does it.

---

## The bundled triage probe

One read-only round trip through `node-exec`, the `on_node` form shown.
Substitute `<node>`, the recoverable slugs, and each slug's declared volumes.
Every probe that cannot answer says `unknown`. It never goes silent, because
silence would read as absence.

```bash
NODE=<node>

# Who is this box? Absent file = not bootstrapped as any node.
if [ -r /etc/drydocc/node-name ]; then echo "node_name=$(tr -d '[:space:]' < /etc/drydocc/node-name)"
else echo "node_name=absent"; fi

# Identity — the same computation as the bootstrap identity probe; hash only.
MID_RAW="$( (cat /etc/machine-id 2>/dev/null || cat /var/lib/dbus/machine-id 2>/dev/null) | tr -d '[:space:]')"
[ -n "$MID_RAW" ] && echo "machine_id_hash=sha256:$(printf '%s' "$MID_RAW" | sha256sum | awk '{print $1}')" \
                  || echo "machine_id_hash=unknown"
unset MID_RAW
HK="$(ls /etc/ssh/ssh_host_ed25519_key.pub 2>/dev/null || ls /etc/ssh/ssh_host_*_key.pub 2>/dev/null | head -1)"
[ -n "$HK" ] && echo "host_key_fingerprint=$(ssh-keygen -lf "$HK" | awk '{print $2}')" || echo "host_key_fingerprint=unknown"
echo "arch=$(uname -m)"

# Capabilities the target shape needs — verdicts, not deductions.
echo "user_manager=$(systemctl --user is-system-running 2>/dev/null || echo unknown)"
echo "rootless=$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null || echo unknown)"

for SLUG in <space-separated recoverable slugs>; do
  n=0
  for f in ~/.config/containers/systemd/"$SLUG"-*.container ~/.config/containers/systemd/"$SLUG"-*.network; do
    [ -f "$f" ] && n=$((n+1))
  done
  echo "quadlets=$SLUG|$n"
  podman ps --all --filter "name=^$SLUG-" --format "container=$SLUG|{{.Names}}|{{.State}}" 2>/dev/null \
    || echo "container=$SLUG|unknown"

  for VOL in <space-separated volumes declared by $SLUG>; do
    D="/var/lib/drydocc/volumes/$SLUG/$VOL"
    if [ ! -e "$D" ]; then echo "volume=$SLUG|$VOL|absent"; continue; fi
    # Contents belong to the container's user namespace; read them from inside it.
    FILES=$(podman unshare find "$D" -mindepth 1 2>/dev/null | wc -l) || FILES=""
    BYTES=$(podman unshare find "$D" -type f -exec stat -c %s {} + 2>/dev/null | awk '{s+=$1} END {print s+0}') || BYTES=""
    if [ -z "$FILES" ] || [ -z "$BYTES" ] || ! podman unshare test -r "$D" 2>/dev/null; then
      echo "volume=$SLUG|$VOL|unknown"
    else
      echo "volume=$SLUG|$VOL|present|$FILES|$BYTES"
    fi
  done

  if [ -e "/etc/drydocc/secrets/$SLUG.env" ]; then echo "env=$SLUG|present"
  elif [ -d /etc/drydocc/secrets ]; then echo "env=$SLUG|absent"
  else echo "env=$SLUG|unknown"; fi
done

# Unattended work that lived only on the box.
systemctl --user list-unit-files 'drydocc-*' --no-legend 2>/dev/null \
  | awk '{print "unit=" $1 "|" $2}' || echo "unit=unknown"
[ -x ~/.config/drydocc/notify.sh ] && echo "notify_helper=present" || echo "notify_helper=absent"
[ -r /etc/drydocc/secrets/_notify.env ] && echo "notify_env=present" || echo "notify_env=absent"
```

When the node has backup targets it can already reach, read the newest seal
for each backed-up use-case too. Use the node's existing credentials and
the listing and seal paths from
`backup-use-case/references/backup-recipes.md` § "Snapshot layout and seal".
Only `manifest.json` is read, never volume data. On a rebuilt box these
credentials do not exist yet. Skip the seal in that case, and judge on live
evidence alone (see below).

---

## Triage classification

### Base

| State | Condition |
|---|---|
| `lost` | `node-exec` cannot reach the box, after § "Replacement host keys" is resolved |
| `wrong box` | `node_name=` is present and is not `<node>` → refusal |
| `unrecorded` | reachable, but any of: `node_name=absent`; `machine_id_hash` or `host_key_fingerprint` differs from `node.yaml:identity` (or the block is missing); `rootless` is not `true`; `user_manager` is `unknown` or offline; `node.yaml:last_bootstrapped` is null |
| `ready` | reachable, `node_name=<node>`, identity matches the profile, `rootless=true`, the user service manager answers |

`unrecorded` is the everyday shape of a replacement box that the admin
already bootstrapped by hand before running this command. It sends the run
through bootstrap's audit-&-repair, so the profile describes the box that
answers. An identity mismatch is corroborating evidence, never the trigger for
recovery on its own. The footprint and data columns are what decide what gets
recovered.

### Footprint, per recoverable use-case

| State | Condition |
|---|---|
| `absent` | `quadlets=<slug>\|0`, no `container=<slug>\|…` lines, every declared volume `absent` |
| `present` | every declared container listed and `running`, `quadlets` non-zero, every declared volume `present` |
| `partial` | anything between: drift, not recovery |
| `unknown` | any line for the slug says `unknown`, or the node is `lost` and the base has not been re-triaged yet |

This is triage, not a verdict. `/apply-use-case` re-probes the four absence
conditions itself and makes the rebuild classification. If its answer differs
from this table, its answer stands, and the carried confirmation is void.

### Data, per declared volume

Evaluate in this order; the first match wins.

| State | Condition |
|---|---|
| `lost` | the footprint is `absent` — rebuild mode will create the volume empty |
| `lost` | the use-case owes a restore per `conventions/references/journal.md` § "Rebuilt, not yet restored", **whatever the live counts say** |
| `unknown` | the volume line says `unknown` |
| `lost` | the volume is in the backup manifest, and is `absent` or has 0 entries, and either no seal could be read or the seal records `files > 0` |
| `suspect` | a seal was read, and the live `FILES` or `BYTES` is below half of the seal's `files` or `bytes` for that volume |
| `empty` | the volume is not covered by any backup, and is `absent` or has 0 entries |
| `intact` | everything else |

`suspect` is deliberately weak. A volume can shrink legitimately after a
cleanup, and live data may be newer than every snapshot. It earns an offer,
never a default restore.

---

## Replacement platform

**Goal.** Every pinned image of a recoverable use-case runs on the replacement
box's architecture before the use-case is attempted.

1. Compare `node.yaml:arch` before this run with the value bootstrap recorded
   during it. Normalise before comparing, never refuse on spelling:
   `x86_64` → `amd64`, `aarch64` → `arm64`, `armv7l` → `arm/v7`. A match means
   stop here.
2. For each `spec.containers.*.image`, ask the registry which platforms the
   pinned tag offers:

   ```bash
   podman manifest inspect <image>
   ```

   Read `.manifests[].platform` from the JSON — `os`, `architecture`, and
   `variant` when present — rather than line-matching it; field order inside a
   platform object is not fixed. If the command fails or the JSON has no
   `manifests` array, the tag is a single-platform image, not a manifest list. Pull it for the new platform and ask the image itself:

   ```bash
   podman pull --arch <oci-arch> <image> && podman image inspect <image> --format '{{.Architecture}}'
   ```

**Verification.** The new platform is in the list, or the pulled image reports
it. Anything else blocks the use-case, and the report names the image and the
platforms it does offer.

**Data portability.** File data crosses architectures unchanged. Some
application stores do not: a database engine's on-disk format can be tied
to architecture or word size. When the platform changed, the plan says so
for every backed-up use-case. The restore's own hooks decide whether a
dump/load path exists. If none does, the admin decides.

---

## The recovery plan

### Footprint action

| Triage footprint | Action |
|---|---|
| `absent` | `rebuild` — `/apply-use-case`, which confirms rebuild mode on its own evidence |
| `present` | `none` |
| `partial` | `drift` — hand off `/audit-use-case <node> <slug>` then `/modify-use-case`; its dependents are `blocked` |
| `unknown` | `blocked` — re-run once the probe can answer |
| dependency or platform block | `blocked`, with the reason |
| not declarative | `not declarative` — the `## Migration` worklist |

### Data action, per use-case

| Volumes' data states | Backup manifest | Action |
|---|---|---|
| any `lost` | yes | `restore` — live `/restore-use-case`, newest usable seal |
| any `suspect`, none `lost` | yes | `offer` — shown with both counts; default is no |
| any `lost` or `empty` | no, or the volume is not in it | `unrecoverable` — into the loss inventory |
| all `intact` | — | `none` |
| any `unknown` | — | `none` this run; reported, with the volume |

A restore replaces **every** volume in the manifest, not only the lost one.
When a use-case has a `lost` volume beside an `intact` one, say so. Every
intact volume is rolled back to the snapshot, and the restore keeps it aside
as `pre-restore`.

### Plan format

```
Recovery plan for <node> — base: <state> (<evidence>)
Order: <slug1> → <slug2> → …

<n>. <slug>  [<footprint action>, <data action>]
     declares active since <last_applied> (<last_applied_commit>); live: <footprint evidence>
     identity: <unchanged | changed since that apply | not recorded>
     volumes: <vol> <state> (<live files/bytes> vs seal <files/bytes>) …
     restore: target <target> (<type>), newest sealed snapshot resolved at restore time;
              consistency <stop|live> → units down during restore
     needs:   KeePass <entry>, …; re-authorize NAS key <key> on <host>
     leaves:  <vol>.pre-restore-<id> for you to prune
     blocked: <reason>                                   (when blocked)

Known losses: <from § "Known losses">
Not declarative: <slug> — reinstall <unit> (<packages>) by hand
Planned, left alone: <slugs>

Proceed? [a] all  [s] choose  [x] abort
```

---

## Unattended work to re-arm

On-node automation is rendered, never committed, so a lost box takes all of it
with it. The repo says what existed; the triage probe says what exists now.

| Work | It existed when | Present when the probe shows | Owning command | Precondition |
|---|---|---|---|---|
| Backup timer for `<slug>` | `<slug>.backup.yaml` exists | `unit=drydocc-backup-<slug>.timer\|enabled` | `/backup-use-case <node> <slug>` | the data outcome is `intact` or restored this run |
| Maintenance sweep | `maintenance.yaml:automation_rendered_commit` is set | `unit=drydocc-maintenance.timer\|enabled` | `/node-maintenance <node> --setup` | the base is `ready` |
| Telemetry sampler | a telemetry posture item is not `off` in the merged posture | `unit=drydocc-telemetry.timer\|enabled` | the same `/node-maintenance` run | — |
| Notification | `nodes/notify.yaml` exists | `notify_helper=present` and `notify_env=present` | `/node-notify <node> --setup` | run **last**, so it arms alarms for the timers above |

A backed-up use-case whose data ended `lost` or `restore-failed` gets **no**
backup re-arm offer. Its first unattended run would seal empty data, and
retention would then push the last good snapshot off the target. The report
names it instead. Telemetry baselines for rebuilt or restored use-cases
become stale by the maintenance rules themselves; nothing here re-bases them.

---

## The recovery report

1. **Header** — the node, the base state and how it got there (for example
   *replaced, bootstrapped `<ts>`*), and the run's outcome.
2. **Outcome table** — one row per recoverable use-case in recovery order:
   footprint action taken, data action taken (the snapshot id and seal
   verification when restored), and outcome.
3. **Volume loss inventory** — stated outright, never folded into the table:
   every declared volume that now holds no recovered data, with its reason:
   - `no backup manifest`;
   - `not in the backup manifest`;
   - `restore declined`;
   - `restore failed seal verification; pre-restore data kept`;
   - `blocked`.

   A clean run says *"No volume lost data."* in so many words.
4. **Automation** — what was re-armed, what was offered and declined, and
   what was deliberately not offered, with the reason.
5. **Hand-offs** — ready-to-paste invocations: drift audits, failed
   restores (with `--snapshot` when a pre-collapse seal was proposed),
   backups still missing, the full audit.
6. **Yours to clean up** — never done by this command:
   - stale tailnet devices;
   - NAS keys authorized for the lost box;
   - `pre-restore` directories;
   - the dead VM or its disk image;
   - a stale host-key entry the admin declined to remove.
