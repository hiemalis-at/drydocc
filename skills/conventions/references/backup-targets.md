# Backup Targets Convention

## Path

Backup targets are declared in two layers, merged at read time:

- **`nodes/backup-targets.yaml`** — fork-wide targets every node can use.
  Lives under `nodes/` (not the repo root) so the generic layer stays
  distro/framework-only: a fork's own data lives under `nodes/`.
- **`nodes/<node>/backup-targets.yaml`** — this node's overrides and
  additions. Optional; a node with no file inherits the fork-wide list as-is.

Both are authored by `/backup-use-case`'s first-run walkthrough (never
hand-invented) and are ordinary git data — no secret values live in either
file, only references to where secrets live.

## Merge rule

1. Read the fork file (empty list if the file doesn't exist yet).
2. Read the node file if present.
3. For each node-file entry, match by `name` against the fork list:
   - same `name` → the node entry's fields **override** the fork entry's,
     field by field (a node entry naming only `root` overrides just that
     field; every other field is inherited from the fork entry).
   - new `name` → added as a node-only target.
4. After merging, **at most one** target may carry `default: true`. If the
   merge produces more than one (a node file redundantly re-declaring
   `default: true` on a second target is the usual cause), refuse and name
   both entries — this is a fork/node file inconsistency the admin must
   resolve by hand.

`/backup-use-case` reads this merged view once per run; it never writes to
either file except during the first-run walkthrough (see below) or when the
admin explicitly asks it to add a target.

## Schema

```yaml
# nodes/backup-targets.yaml
schema: 1
targets:
  - name: nas-primary              # kebab-case, referenced by <slug>.backup.yaml:target
    type: rsync                    # rsync | s3
    default: true                  # at most one target, after merge, may be true
    host: nas.example.ts.net
    port: 22
    user: doccap-backup
    root: /backups/drydocc         # remote path snapshots land under
    ssh_key_name: doccap-backup-at-nas-primary   # metadata only; see "rsync credentials"
    ssh_key_fingerprint: "SHA256:…"

  - name: cold-s3
    type: s3
    default: false
    endpoint: https://s3.example-provider.com
    bucket: drydocc-backups
    root: prod                     # bucket-relative prefix snapshots land under
    force_path_style: false        # some S3-compatible providers require true
    keepass_entry: "drydocc/<node>/backup-cold-s3"
    target_path: /etc/drydocc/secrets/backup-cold-s3.env
    keys:
      - name: S3_ACCESS_KEY_ID
        purpose: object storage access key id
        rotates: yearly
      - name: S3_SECRET_ACCESS_KEY
        purpose: object storage secret key
        rotates: yearly
      - name: BACKUP_ENCRYPTION_PASSPHRASE
        purpose: symmetric passphrase encrypting every snapshot before upload
        rotates: never             # rotating it makes every existing snapshot unreadable
```

### Field descriptions

- **`name`** — Unique kebab-case identifier. `<slug>.backup.yaml:target`
  references this name; `nodes/<node>/backup-targets.yaml` matches on it to
  override or extend.
- **`type`** — `rsync` (a file-share reached over rsync-over-ssh) or `s3`
  (an S3-compatible object store). Fixed at exactly these two values. A
  third tier is out of scope — raise it with the admin as a framework
  change; never slip a new enum value in here.
- **`default`** — Optional bool. `/backup-use-case` setup proposes this
  target when a use-case's manifest doesn't already name one; the admin can
  still pick a different target during setup. At most one `true` after merge.
- **rsync fields** — `host`, `port`, `user`, `root` (the remote path under
  which `<node>/<slug>/<snapshot-id>/` trees land). `ssh_key_name` /
  `ssh_key_fingerprint` are metadata only, exactly like `node.yaml:ssh` — the
  private key never enters git. See "rsync credentials" below.
- **s3 fields** — `endpoint`, `bucket`, `root` (the bucket-relative prefix
  under which `<node>/<slug>/<snapshot-id>/` objects land), `force_path_style`
  (some S3-compatible providers need virtual-host-style URLs disabled).
  `keepass_entry`, `target_path`, and `keys[]` follow exactly the
  `references/secrets.md` shape — this is a secrets manifest embedded in the
  target entry, not a new convention. See "s3 credentials" below.

## rsync credentials

Access to an rsync target is a dedicated per-node, per-target SSH keypair —
never a password, never a key shared across targets or nodes:

1. On first use of a `type: rsync` target, `/backup-use-case` generates
   `~/.ssh/doccap-backup-at-<target-name>` on the node (ed25519, no
   passphrase — it authenticates an unattended timer).
2. The walkthrough shows the admin the **public** half and asks them to
   authorize it on the NAS (append to the backup user's
   `authorized_keys`, restricted to that path if the NAS supports
   command/path-restricted keys — a nicety, not a requirement).
3. `ssh_key_name` and `ssh_key_fingerprint` are recorded on the target entry.
   The private key never enters git, the journal, or the backup manifest.

A rebuilt node has no `~/.ssh/doccap-backup-at-<target-name>` — regenerating
it and re-authorizing on the NAS is part of `/restore-use-case`'s first step
on a rebuilt box (the old key is orphaned on the NAS; removing it there is a
manual admin cleanup, same spirit as `/apply-use-case`'s adoption prune list).

## s3 credentials

Follows `references/secrets.md` exactly, with one difference: the manifest is
**target-scoped, not use-case-scoped** — every use-case backing up to the same
S3 target shares one env file, because the credentials authenticate the
*target*, not any one use-case's data.

- `keepass_entry`: `drydocc/<node>/backup-<target-name>` (node-scoped —
  scoping credentials per node means a compromised node can delete at most
  its own node's snapshots, never another node's).
- `target_path`: `/etc/drydocc/secrets/backup-<target-name>.env`, written the
  same way any other secrets env file is (`secrets` skill, `install -m 600`).
- `keys[]`: `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`,
  `BACKUP_ENCRYPTION_PASSPHRASE`. The passphrase is the one thing this schema
  adds beyond ordinary use-case secrets: every snapshot is encrypted on the
  node before it leaves, so the passphrase is as critical as the KeePass
  database itself — **losing it makes every existing S3 snapshot
  unrecoverable**. The walkthrough must say this outright, not bury it.

## First-run walkthrough

When `/backup-use-case` finds neither `nodes/backup-targets.yaml` nor
`nodes/<node>/backup-targets.yaml`, it walks the admin through creating one
before anything else happens:

1. Ask whether the target is shared fork-wide or specific to this node —
   decides which file the new entry lands in. `[decision: backup.target-scope]`
2. Ask the target type, then its fields (host/port/user/root for rsync;
   endpoint/bucket/root/force_path_style for s3). `[decision: backup.target-fields]`
3. For an s3 target: tell the admin the exact KeePass entry and custom
   field names to create (per `references/secrets.md` § "KeePassXC entry
   setup"), including the encryption passphrase, and generate one if the
   admin doesn't already have one (`openssl rand -base64 32` is enough; the
   admin can also supply their own).
4. For an rsync target: generate the SSH keypair and print the public key
   for the admin to authorize on the NAS. `[decision: backup.authorize-target-key]`
5. Verify reachability (`backup-use-case/references/backup-recipes.md` §
   "Target reachability").
6. Write the file, journal, and commit.

## Example

See `../assets/backup-targets.yaml` (fork-wide) and
`../assets/example-vps/backup-targets.yaml` (a node override adding a
second, node-specific target).
