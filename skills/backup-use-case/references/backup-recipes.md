# Backup Recipes

Snapshot layout, seal format, and procedure for `/backup-use-case` and
`/restore-use-case`. Targets are
`conventions/references/backup-targets.md`; the per-use-case policy is
`conventions/references/backup-manifest.md`.

**This file does not duplicate other skills' commands.** Firewall, quadlet,
and secrets handling are unchanged — this file only adds what backup/restore
needs on top of them.

## Contents

- [Snapshot layout and seal](#snapshot-layout-and-seal)
- [Run log](#run-log)
- [Backup tooling](#backup-tooling) — the one genuine dialect derivation in
  this file (package-manager install of `rclone` + `gpg`)
- [Target reachability](#target-reachability)
- [Quiescing a use-case](#quiescing-a-use-case)
- [Capturing a volume with namespace-relative ownership](#capturing-a-volume-with-namespace-relative-ownership)
- [Writing a file-share snapshot](#writing-a-file-share-snapshot)
- [Writing an S3-compatible snapshot](#writing-an-s3-compatible-snapshot)
- [Sealing a snapshot](#sealing-a-snapshot)
- [Retention](#retention)
- [Rendering the automation](#rendering-the-automation)
- [Choosing a snapshot to restore](#choosing-a-snapshot-to-restore)
- [Restoring a snapshot](#restoring-a-snapshot)
- [Verifying a restore against its seal](#verifying-a-restore-against-its-seal)

---

## Snapshot layout and seal

Both target types share one layout, `<root>/<node>/<slug>/<snapshot-id>/`,
where `<root>` is the target's `root` field and `<snapshot-id>` is
`YYYY-MM-DDTHHMMSSZ` (UTC, filesystem/object-key safe).

- **rsync target** — the snapshot id is a real directory holding one
  subdirectory per backed-up volume, hard-linked against its predecessor (see
  § "Writing a file-share snapshot").
- **s3 target** — the snapshot id is an object-key prefix holding one
  encrypted archive object per backed-up volume, plus the seal (see §
  "Writing an S3-compatible snapshot"). There is no directory, only the
  shared key prefix.

**The seal, `manifest.json`, is written last, on both target types.** A
snapshot counts as complete, and therefore restorable and countable toward
`retention.keep`, **only if its seal exists**. A run that dies mid-copy
leaves an unsealed snapshot, which the next run's retention pass prunes
unconditionally (§ "Retention") — this is what makes "the last snapshot
exists" and "the last snapshot is good" the same question.

```json
{
  "node": "vps-fra-01",
  "slug": "nextcloud",
  "snapshot_id": "2026-09-13T033000Z",
  "commit": "8f3c9d2",
  "consistency": "stop",
  "images": {
    "nextcloud-app": "docker.io/nextcloud:29.0.4-apache",
    "nextcloud-db": "docker.io/postgres:16.3-alpine"
  },
  "volumes": {
    "nextcloud-data": { "files": 18213, "bytes": 4021551104, "digest": "sha256:…" },
    "nextcloud-db-data": { "files": 412, "bytes": 88221184, "digest": "sha256:…" }
  }
}
```

- **`digest`** — sha256 over the sorted `path|size|uid|gid|mode` listing of
  **only the files** in the volume **as captured** (namespace-relative
  ownership, § "Capturing a volume") — directories are excluded: a
  directory's reported byte size is filesystem-layout metadata, not content,
  and is not reproducible across a copy (confirmed: the same tree's top-level
  directory reported 24 bytes locally and 60 bytes after an rsync
  round-trip). This is the same field set `<slug>.audit.yaml:fingerprints`
  uses for a directory config surface, but **not** the same `find` scope —
  audit's own formula (`audit-recipes.md`: `find "$P" -type f -printf
  '%P|%s|%T@\n'`) is files-only too, for the same reason. The exact command:

  ```bash
  podman unshare bash -c "cd '<vol-dir>' && find . -type f | sort | \
    while read -r p; do stat -c '%n|%s|%u|%g|%a' \"\$p\"; done" \
    | sha256sum | awk '{print $1}'
  ```
- **`images`** — the pinned tag each container was running at capture time.
  A restore whose current declaration pins a different tag for any container
  is not refused, but is called out to the admin (a data-format change across
  a major version bump is the classic reason this matters for a database).

## Run log

Every automation run — on-demand or unattended — appends one line to
`/var/lib/drydocc/backups/<slug>.runlog` on the node (created on first run):

```
<ts-iso-utc>|<snapshot-id-or->|<rc>|<bytes-or-->
```

`<snapshot-id-or->` is `-` for a run that failed before a snapshot id was
even assigned. This is what lets an unattended timer run journal itself
**after the fact**: `/backup-use-case` activation reads every line newer
than the manifest's `last_backup` and turns each into the journal's `##
Commands`/`## What was done` narrative, without an agent having been present
when the timer fired.

---

## Backup tooling

The one section in this file that is a genuine OS-dialect derivation — every
other section below is procedure, identical on every Linux node, because it
only uses tools the framework's target shape already requires (podman,
rsync, systemd) or a single fixed tool this skill installs itself.

**Goal.** `rclone` (the S3-compatible client, chosen once for the whole
framework rather than re-derived per node — see "What this file does NOT
do") and `gpg` (symmetric encryption for the S3 path) are installed and
importable, idempotently.

**Fact.** `system_facts.package_manager` — already discovered by
`/bootstrap-node` / `/onboard-node`; this section never re-derives it, only
reuses it exactly as `bootstrap-node/references/hardening-recipes.md` §
"Package installation" does for the baseline stack.

**Deriving an unseen dialect.** Resolve `rclone` and `gpg`'s package names
for the confirmed manager the same way § "Package installation" resolves
`tailscale`'s: most names are stable (`gpg`'s package is commonly `gnupg` or
`gnupg2`), but confirm against the manager's own index before assuming.
There is no fallback when nothing on the box manages packages — that
refusal already happened at bootstrap, so this step is never reached on a
box without one.

**Verification.** `command -v rclone gpg` both resolve after the install
command exits zero.

**Record.** `system_facts.capabilities.rclone` and
`system_facts.capabilities.gpg` (`present`), journaled. Installed once, on
the first `/backup-use-case` setup that needs a tool it doesn't have yet —
never at baseline bootstrap, since a node that never backs anything up
shouldn't carry tools it will never use (`conventions/references/distro-agnosticism.md`
Rule 6).

### Observed dialects — evidence, not the supported list

| package_manager | install command | verified on | date |
|---|---|---|---|
<!-- no rows yet — this recipe is unexercised against a live node. Add a row
     the first time an install is derived and verified, per Rule 3. -->

---

## Target reachability

Run once at the start of every setup/activate cycle, and on demand from the
first-run walkthrough (`conventions/references/backup-targets.md`).

- **rsync target:**

  ```bash
  ssh -F /dev/null -o UserKnownHostsFile=/dev/null \
      -i ~/.ssh/<ssh_key_name> -p <port> -o StrictHostKeyChecking=accept-new \
      <user>@<host> \
      "test -d '<root>' && touch '<root>/.drydocc-write-test' && rm -f '<root>/.drydocc-write-test'"
  ```

  `-F /dev/null` is required, not optional: every `ssh` call this skill makes
  runs (directly or through `podman unshare`) inside a context where
  host-root-owned files under `/etc/ssh/ssh_config.d/` can appear to have an
  unmapped/suspicious owner, and `ssh`'s own `Include` processing then refuses
  them ("Bad owner or permissions"), killing the whole connection — the
  system config isn't needed for unattended automation anyway.
  `-o UserKnownHostsFile=/dev/null` silences a related, harmless-but-noisy
  `Could not stat /root/.ssh` warning from the same root cause. Every other
  `ssh`/`-e "ssh ..."` invocation below carries both flags for the same
  reason.

- **s3 target:** (`rclone` configured ad hoc via `--s3-*` flags — no
  persistent rclone config file, so the credentials the `secrets` skill
  resolved stay in the process environment only, never written to disk)

  ```bash
  echo drydocc-write-test | rclone rcat \
    --s3-provider=Other --s3-env-auth=true --s3-endpoint="<endpoint>" \
    ${force_path_style:+--s3-force-path-style} \
    ":s3:<bucket>/<root>/.drydocc-write-test"
  rclone deletefile --s3-env-auth=true --s3-endpoint="<endpoint>" \
    ":s3:<bucket>/<root>/.drydocc-write-test"
  ```

  `--s3-env-auth=true` reads `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` from
  the environment — set them from the resolved `S3_ACCESS_KEY_ID` /
  `S3_SECRET_ACCESS_KEY` for this one command's environment only, never
  exported into the shell's persistent profile.

A failure here is a refusal, not a warning: setup stops before writing
`<slug>.backup.yaml`, and activate stops before touching anything else.

---

## Quiescing a use-case

Applies when `<slug>.backup.yaml:consistency == stop`; skipped entirely
under `live`.

1. Stop the use-case's units in reverse `depends_on` order (the same order
   `/remove-use-case` step 3 already uses):

   ```bash
   systemctl --user stop <slug>-<container>.service   # each container, reverse dependency order
   ```

2. Run every `hooks.pre_backup[]` entry that names a container reachable
   this way — a hook that dumps from a **running** database is a `live`-mode
   hook and belongs before the stop, not here; `stop`-mode hooks that need
   the container up (rare — most `stop` use-cases need no hook at all) are
   the one case where step 1 and step 2 swap order, decided by which
   containers each hook names.
3. Run the volume capture (§ "Capturing a volume…").
4. **Always** restart every unit stopped in step 1, success or failure —
   wrap steps 2–3 in a shell `trap` so a capture failure still restarts the
   use-case:

   ```bash
   trap 'systemctl --user start <slug>-<container>.service ...' EXIT
   ```

Under `consistency: live`, run `hooks.pre_backup[]` (if any) with every
container still up, then go straight to capture — no stop, no trap needed.

---

## Capturing a volume with namespace-relative ownership

The mechanism both target-writing steps below build on. Rootless podman
maps a container's internal uid/gid range to a high, node-specific subuid
range on the host — `/var/lib/drydocc/volumes/<slug>/<vol>/` is therefore
owned by numbers meaningless outside this one node's subuid allocation.
`podman unshare` re-execs a command inside that mapping's user namespace, so
the same command sees uid 0 where the host sees the subuid base, uid 1 where
the host sees base+1, and so on — ownership becomes **portable**: captured
this way, a restore on a *different* node (a different subuid base) maps
back correctly as long as the restore is *also* done through `podman
unshare`.

Every read of a volume for backup, and every write of one for restore, goes
through `podman unshare`. This is a property of podman itself (the target
shape, not a per-distro dependency), so it needs no derivation — just
consistent use.

```bash
podman unshare tar --xattrs --acls -C /var/lib/drydocc/volumes/<slug>/<vol> \
  --exclude='<exclude-glob>' -cf - .
```

(one `--exclude` per entry in that volume's `<slug>.backup.yaml:volumes[].exclude`)

This stream feeds directly into whichever transport the target needs —
§ "Writing a file-share snapshot" unpacks it through the same wrapper on the
receiving side; § "Writing an S3-compatible snapshot" pipes it straight to
encryption without ever touching disk unencrypted.

---

## Writing a file-share snapshot

For an rsync target, transport is a direct `podman unshare`-wrapped `rsync`
per volume — no tar staging needed, since both ends of an rsync-over-ssh
push already stream:

```bash
podman unshare rsync -aHAX --numeric-ids --fake-super \
  -e "ssh -F /dev/null -o UserKnownHostsFile=/dev/null \
      -i ~/.ssh/<ssh_key_name> -p <port> -o StrictHostKeyChecking=accept-new" \
  --link-dest="../<previous-snapshot-id>/<vol>" \
  --exclude='<exclude-glob>' \
  /var/lib/drydocc/volumes/<slug>/<vol>/ \
  <user>@<host>:<root>/<node>/<slug>/<snapshot-id>/<vol>/
```

`--fake-super` is required, not optional, whenever the target account is
non-root — which the convention itself recommends (`doccap-backup`, deliberately
unprivileged). `--numeric-ids` alone only *requests* the numeric owner; a
`chown` to an arbitrary uid/gid needs privilege the receiving account
doesn't have, so without `--fake-super` the OS silently collapses every
transferred file's ownership to the connecting user's own uid instead —
quietly defeating the entire point of `podman unshare`'s namespace-relative
"portable ownership" capture (confirmed by a direct round-trip test).
`--fake-super` preserves full owner/group/permission metadata via a user
xattr instead of a real `chown`, and must be paired with the same flag on
read (§ "Restoring a snapshot") to round-trip correctly.

`--link-dest` is resolved on the **receiving** side (the NAS), so a relative
path pointing at the previous sealed snapshot's copy of the same volume
turns every unchanged file into a hard link rather than a fresh copy —
cheap, incremental snapshots with no special retention-time reconstruction:
each snapshot directory is already a complete, independent tree. Omit
`--link-dest` entirely for the first-ever snapshot (no predecessor exists).

Requires `system_facts.capabilities.rsync: present` (already a baseline
capability per `node-profile.md`) and the target's SSH reachability (§
"Target reachability"). This step is the long one — always `NODE_RUN_LINGER`
(`dd-<slug>-backup`) when `on_client`.

---

## Writing an S3-compatible snapshot

For an s3 target, each volume becomes one encrypted archive object —
captured, encrypted, and uploaded as a single pipeline with no unencrypted
copy ever touching disk:

```bash
podman unshare tar --xattrs --acls -C /var/lib/drydocc/volumes/<slug>/<vol> \
    --exclude='<exclude-glob>' -cf - . \
  | gpg --batch --yes --passphrase-fd 3 3<<<"$BACKUP_ENCRYPTION_PASSPHRASE" \
        --symmetric --cipher-algo AES256 \
  | rclone rcat --s3-provider=Other --s3-env-auth=true --s3-endpoint="<endpoint>" \
        ${force_path_style:+--s3-force-path-style} \
        ":s3:<bucket>/<root>/<node>/<slug>/<snapshot-id>/<vol>.tar.gpg"
```

`--passphrase-fd 3` reads the passphrase from file descriptor 3 (the
here-string), never from argv or an on-disk temp file — the same "never on
the command line, never echoed" discipline `secrets` requires elsewhere.
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` are set in this command's
environment only, resolved from the target's env file. There is no
incremental transport for the S3 tier (object stores have no hard-link
equivalent) — every snapshot is a full copy of every declared volume;
`retention.keep` is what bounds the storage cost.

Requires `system_facts.capabilities.{rclone,gpg}: present` (§ "Backup
tooling"). Always `NODE_RUN_LINGER` (`dd-<slug>-backup`) when `on_client`.

---

## Sealing a snapshot

After every volume for this snapshot has transported successfully (rsync:
`rsync`'s own non-zero exit fails the run per volume; s3: the whole pipeline
must exit zero, i.e. `tar`, `gpg`, **and** `rclone` — check with `PIPESTATUS`
or run under `set -o pipefail`), assemble `manifest.json` (§ "Snapshot
layout and seal") in memory and write it **last**:

- rsync target: `printf '%s' "<json>" | ssh … 'cat > "<root>/<node>/<slug>/<snapshot-id>/manifest.json"'`
- s3 target: `printf '%s' "<json>" | rclone rcat … ":s3:<bucket>/<root>/<node>/<slug>/<snapshot-id>/manifest.json"`
  (the seal itself is not encrypted — it carries no secret content, only
  sizes and digests, and being plaintext is what lets retention and restore
  list snapshots without decrypting anything first)

Any failure before this point leaves an unsealed snapshot; report the
`outcome: partial` and let § "Retention" prune it on the next run.

---

## Retention

After a successful seal, list every sealed snapshot for `<slug>` on the
target (rsync: every `<root>/<node>/<slug>/*/` containing a `manifest.json`;
s3: every distinct `<snapshot-id>` prefix under
`<root>/<node>/<slug>/` containing a `manifest.json` object), sorted by
snapshot id (which sorts chronologically by construction):

1. **Delete every unsealed snapshot** (a directory/prefix with volume data
   but no `manifest.json`) unconditionally — it is neither restorable nor
   countable.
2. **Keep the newest `retention.keep` sealed snapshots**; delete the rest.

If a delete is refused (object-lock retention on an S3 bucket, or an
append-only NAS mount), that is not this step's failure — report which
snapshots could not be pruned and why, and continue; a target configured for
ransomware-resistant immutability is working as intended, and its own
lifecycle rules (bucket policy, NAS-side retention job) are what actually
bounds storage there, not this step.

---

## Rendering the automation

**Bring-up (once per node, before the first render):**
`/var/lib/drydocc/backups/` (the lock file and run log's home, § "Run log")
does not exist on a fresh node — nothing else creates it. Before rendering
any use-case's first automation on this node:

```bash
sudo install -d -o <owner> -g <group> -m 0750 /var/lib/drydocc/backups
```

The same idiom `/apply-use-case` step 7 uses for volume directories. Shared
across every use-case's backup automation on the node — a no-op if it already
exists.

Rendered on-node only, exactly like quadlets — never committed:

- **Script** — `~/.config/drydocc/backup-<slug>.sh`: a shell script
  assembling the exact commands from §§ "Quiescing", "Capturing", "Writing a
  … snapshot", "Sealing", and "Retention" for this use-case's manifest,
  ending with an append to the run log (§ "Run log"). Includes a lock:

  ```bash
  exec 9>"/var/lib/drydocc/backups/<slug>.lock"
  flock -n 9 || { echo "backup or restore already running for <slug>"; exit 1; }
  ```

  The same lock file is taken by `/restore-use-case`, so a backup and a
  restore for the same use-case can never run concurrently.

  Notification is optional and never fails the backup. Source
  `~/.config/drydocc/notify.sh` if it exists and stub its functions if not,
  exactly as `node-maintenance/references/maintenance-recipes.md` § "Rendering
  the automation" shows. After the run-log append: on success,
  `dd_hb_arm backup-<slug> <interval-seconds>` (computed at render time from the
  manifest's `schedule`, per `node-notify/references/notify-recipes.md` §
  "Arming a missed-run alarm"); on a non-zero exit,
  `dd_notify run-failed "backup of <slug> on <node> failed" "rc=<rc>"`. A
  schedule too long to arm is watched by the maintenance sweep instead
  (`node-notify/references/notify-recipes.md` § "Jobs too long to arm").

- **Service unit** — `~/.config/systemd/user/drydocc-backup-<slug>.service`:
  `Type=oneshot`, `ExecStart=` the rendered script.
- **Timer unit** — `~/.config/systemd/user/drydocc-backup-<slug>.timer`:
  `OnCalendar=` from `<slug>.backup.yaml:schedule` (§ backup-manifest.md),
  `Persistent=true` (a missed run — node was off — fires once the node is
  back, rather than silently skipping to the next scheduled time).

```bash
systemctl --user daemon-reload
systemctl --user enable --now drydocc-backup-<slug>.timer
```

(`enable --now` is correct here — this is a plain, hand-authored systemd
timer unit, not a quadlet-generated one. Contrast `podman-quadlets`' §
"Reload procedure": a quadlet-generated `.container`/`.network` unit carries
its own `[Install] WantedBy=` and can only be `start`ed, not `enable`d,
directly.)

Disabling/removing (setup's `--setup` purge, or `/remove-use-case`):

```bash
systemctl --user disable --now drydocc-backup-<slug>.timer drydocc-backup-<slug>.service
rm -f ~/.config/systemd/user/drydocc-backup-<slug>.{service,timer} ~/.config/drydocc/backup-<slug>.sh
systemctl --user daemon-reload
```

Then disarm its missed-run alarm, `dd_hb_disarm backup-<slug>`
(`node-notify/references/notify-recipes.md` § "Disarming a missed-run alarm").

Sealed snapshots already on the target are never touched by a purge — only
the on-node automation is removed.

---

## Choosing a snapshot to restore

The newest sealed snapshot is the right default, and the wrong one exactly
when the loss happened **before** it was taken: a volume wiped at 01:00 is
faithfully sealed empty at 03:30. The seals are plaintext and carry per-volume
totals, so this is decidable without reading any volume data.

1. List sealed snapshots for `<node>/<slug>` as § "Retention" defines them,
   newest first, and read each `manifest.json`.
2. Compare the newest seal with its predecessor, volume by volume. The newest
   has **collapsed** when, for any volume present in both, its `files` or
   `bytes` is `0` where the predecessor's was not, or below half of the
   predecessor's.
3. No collapse → propose the newest. Collapse → walk back to the newest seal
   that did not collapse against its own predecessor, and propose **that**
   one, showing both seals' totals per volume. The admin decides; a volume
   that legitimately shrank (a purge, a cache cleared) looks the same from
   here.
4. `--snapshot <id>` skips this entirely — the admin already chose.

Never propose an unsealed snapshot, and never pick an older snapshot silently:
every snapshot newer than the proposal holds writes the restore would discard.

---

## Restoring a snapshot

Mirrors capture. For each volume in the chosen snapshot:

- **rsync target:**

  ```bash
  podman unshare rsync -aHAX --numeric-ids --fake-super \
    -e "ssh -F /dev/null -o UserKnownHostsFile=/dev/null \
        -i ~/.ssh/<ssh_key_name> -p <port> -o StrictHostKeyChecking=accept-new" \
    <user>@<host>:<root>/<node>/<slug>/<snapshot-id>/<vol>/ \
    <extract-target>/
  ```

  Same `-F /dev/null`/`UserKnownHostsFile=/dev/null`/`--fake-super`
  requirements as § "Writing a file-share snapshot" above, and for the same
  reasons — restore is the read side of the identical transport.

- **s3 target:**

  ```bash
  rclone cat --s3-provider=Other --s3-env-auth=true --s3-endpoint="<endpoint>" \
      ${force_path_style:+--s3-force-path-style} \
      ":s3:<bucket>/<root>/<node>/<slug>/<snapshot-id>/<vol>.tar.gpg" \
    | gpg --batch --yes --passphrase-fd 3 3<<<"$BACKUP_ENCRYPTION_PASSPHRASE" --decrypt \
    | podman unshare tar --xattrs --acls -C <extract-target> -xf -
  ```

`<extract-target>` is either scratch space (`--test`) or, for a live
restore, the freshly re-created volume directory (`/apply-use-case`'s own
`sudo install -d -o <owner> -g <group> -m <mode>` idiom, run **before**
extraction so ownership/mode are right from the first byte written).

---

## Verifying a restore against its seal

Re-run the exact digest command from § "Snapshot layout and seal" over
`<extract-target>` — files only, same sorted `path|size|uid|gid|mode`
listing, same sha256:

```bash
podman unshare bash -c "cd '<extract-target>' && find . -type f | sort | \
  while read -r p; do stat -c '%n|%s|%u|%g|%a' \"\$p\"; done" \
  | sha256sum | awk '{print $1}'
```

and compare against `manifest.json:volumes.<vol>.digest`. A mismatch is a
failed restore — report it plainly and do not proceed to bring the use-case
back up on top of a restore that doesn't match its own seal.

---

## What this file does NOT do

- **Pick per-node S3 clients or encryption tools as a dialect.** Unlike
  package manager or firewall tool, `rclone`/`gpg` are the framework's own
  fixed choice (like `podman`, `tmux`) — installed if absent, never derived
  per node. If a future need genuinely requires supporting an
  admin-mandated alternative client, that is out of scope — raise it with
  the admin as a framework change rather than branching here.
- **Decide *what* to back up.** That is `<slug>.backup.yaml`, authored by
  the admin with `/backup-use-case` setup's help.
- **Coordinate backups across nodes.** Each node's use-cases back up
  independently; nothing here makes a multi-node stack's snapshots
  point-in-time consistent with each other.
- **Prune the on-node run log.** It grows forever; rotating it belongs to
  `/node-maintenance`'s `logs-bounded` posture item, which truncates it to its
  most recent entries and never deletes the file — its existence is what tells
  this skill where to resume its journal catch-up.
