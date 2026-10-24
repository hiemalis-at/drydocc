---
name: podman-quadlets
description: Render rootless podman + systemd user quadlets from a use-case spec, following drydocc conventions. Use when creating, modifying, or auditing containers, networks, or volumes on a node.
compatibility: Runs inside a drydocc fork; needs a shell.
metadata:
  skillset: drydocc
  requires: conventions secrets
---

# `drydocc` Podman Quadlets

`drydocc` runs containers under rootless podman as the `doccap` user, with
lifetime managed by **systemd user quadlets**. This skill documents the
conventions all use-case quadlets must follow.

## File locations

Quadlets exist only on the node — never in git:

`~/.config/containers/systemd/<slug>-<name>.{container,network}`

The source of truth is the `spec:` block in
`nodes/<node>/use-cases/<slug>.md`. The render function below is a pure
function `spec → {filename: ini-text}`. `/audit-use-case` re-renders it and
diffs the result against the on-disk quadlets to detect drift.

The on-node filenames are prefixed with the use-case slug to make
ownership obvious in `ls`.

## Naming

- Container unit: `<slug>-<role>.container` → `<slug>-<role>.service` at
  runtime (systemd derives the service name).
- Network: `<slug>-<purpose>.network` → unit `<slug>-<purpose>-network.service`.
- Volume: `<slug>-<volume>.volume` → unit `<slug>-<volume>-volume.service`.

Names listed in the use-case doc frontmatter (`containers:`, `networks:`,
`volumes:`) MUST match these patterns.

## Lingering

After `/bootstrap-node`, `loginctl enable-linger doccap` is run so the
doccap user's systemd instance survives logout. Without this, quadlets
stop when the admin's ssh session ends.

## Reload procedure

After writing or modifying any quadlet file:

```bash
systemctl --user daemon-reload
systemctl --user start <slug>-<role>.service
```

Units rendered from quadlets are **generated**, and systemd refuses to
`enable` or `disable` a generated unit (`Unit … is transient or generated`) —
`enable --now` therefore fails before it starts anything. Start-at-boot comes
from the quadlet's own `[Install] WantedBy=default.target`, which the generator
honours on every `daemon-reload`. So:

- bring a unit up with `start` (or `restart`), never `enable`;
- take it down with `stop`, never `disable`; to keep it from returning at the
  next boot, remove its quadlet file and `daemon-reload`.

This applies only to quadlet-generated units. Ordinary unit files a skill
writes under `~/.config/systemd/user/` (backup, maintenance, and telemetry
timers) are enabled and disabled normally.

## EnvironmentFile

Quadlets that need secrets reference them with:

```ini
[Container]
EnvironmentFile=/etc/drydocc/secrets/<slug>.env
```

See `secrets` for how that file gets written.

## Networking

Default to a per-use-case network:

```ini
[Network]
NetworkName=<slug>-net
```

Cross-use-case networking is rare and requires explicit declaration in
both use-case docs' `depends_on`.

A container may join several of the use-case's networks (`networks:`), e.g.
an internal one to its peers plus one with outbound access.

**Host network** (`network: host`) is for a service that cannot sit behind
podman's port forwarding: a relay that binds a large port range on demand, or
a server that must see the real client address (a mail server scoring
senders, a reverse proxy rate-limiting clients). Rootless port forwarding
hides that address. The container then shares the node's network stack:

- it publishes nothing (`ports:` under the container is refused), because its
  sockets already are the node's;
- the ports it binds are declared in `listens:` (`"25/tcp"`, `"3478/udp"`).
  They feed § "Privileged ports" and the audit's listening check;
- whatever must be reachable from outside still goes in the document's
  top-level `ports:`, which drives the firewall exactly as for any other
  use-case;
- it has no network unit, no `networks`, and no `aliases`.

## Privileged ports

Rootless podman publishes a port as the unprivileged `doccap` user, and the
kernel refuses an unprivileged bind below
`net.ipv4.ip_unprivileged_port_start` (1024 by default; the setting covers
IPv6 too). A `PublishPort=443:443/tcp` therefore fails at container start
unless the floor is at or below 443.

**Goal.** Before any container that binds a host port is started, the
node's floor is at or below the lowest host port that container binds. That
is the number before the container port in a `ports:` entry
(`"443:443/tcp"` → 443; `"127.0.0.1:53:53/udp"` → 53), and for a
host-network container each `listens:` entry (`"25/tcp"` → 25; a range
counts by its low end).

```bash
LOWEST=<lowest host port across spec.containers.*.ports and spec.containers.*.listens>
FLOOR=$(sysctl -n net.ipv4.ip_unprivileged_port_start)
if [ "$LOWEST" -lt "$FLOOR" ]; then
  printf 'net.ipv4.ip_unprivileged_port_start=%s\n' "$LOWEST" \
    | sudo install -m 644 /dev/stdin /etc/sysctl.d/99-drydocc-unprivileged-ports.conf
  sudo sysctl -p /etc/sysctl.d/99-drydocc-unprivileged-ports.conf
fi
```

Rules:

- **Only ever lower it, and only as far as a declared port needs.** Never
  raise it: another use-case on the node may depend on the current floor.
  `/remove-use-case` leaves it alone for the same reason.
- **Persist it.** The drop-in under `/etc/sysctl.d/` survives a reboot; the
  `99-` prefix sorts it after distribution defaults so it wins.
- **Say what it costs.** Any unprivileged process on the node may then bind
  ports at or above the new floor. On a single-responsibility node that runs
  only `doccap`'s services this is the standard rootless trade-off, but the
  admin is told before it happens: list the change in the confirmation the
  calling command already shows.
- **Verification.** `sysctl -n net.ipv4.ip_unprivileged_port_start` is at or
  below `$LOWEST` afterwards. Journal the old and new values.

## Logs

Use journald (the systemd default). Inspect with:

```bash
journalctl --user -u <slug>-<role>.service -f
```

## Render from spec

The render function is a pure mapping `spec → {filename: ini-text}`. It is
called by `/apply-use-case` (first apply, greenfield or adoption),
`/modify-use-case` (re-render after a change), and `/audit-use-case` (re-render
in memory, to compare against what is on disk). `/create-use-case` authors the
document and delegates the apply; it renders nothing.

**Where the renderer lives.** The renderer is committed once, at
`scripts/drydocc/render.py`, and reached through `render_quadlets`,
`diff_quadlets` and `audit_probe_run` (§ "The drydocc tools" below). The
mapping below is the contract that renderer implements. Its one
distro-specific choice, whether mounts carry the relabel option, is the
agent's (the `Z` option below); every other cross-distro adjustment is the
agent's to work out per run, too.

### `<slug>-<container>.container` (one per `spec.containers.*`)

```ini
[Unit]
Description=<slug>/<container> (drydocc)
After=<slug>-<network>-network.service <slug>-<dep>.service ...
Requires=<slug>-<network>-network.service <slug>-<dep>.service ...

[Container]
Image=<spec.image>
ContainerName=<slug>-<container>
Network=<slug>-<net>.network               # one per spec.network / spec.networks entry; Network=host for network: host
HostName=<spec.hostname>                   # only if set
User=<spec.user>                           # only if set
AddCapability=<cap> <cap> ...              # only if cap_add is non-empty, space-joined in declared order
EnvironmentFile=<spec.env_from>            # only if env_from is set
Environment=KEY=VAL                        # one line per spec.env entry
Volume=<source>:<target>[:<opts>]           # one per mount; <source> below
PublishPort=[<addr>:]<host>:<container>/<proto>   # one per port, verbatim
HealthCmd=<healthcheck.cmd>                # the five Health* lines only if healthcheck is set,
HealthInterval=<healthcheck.interval>      #   each only if its field is set
HealthTimeout=<healthcheck.timeout>
HealthRetries=<healthcheck.retries>
HealthStartPeriod=<healthcheck.start_period>
PodmanArgs=--entrypoint=<spec.entrypoint>  # only if set
PodmanArgs=--network-alias=<alias>         # one per aliases entry
Exec=<spec.exec>                           # only if non-empty

[Service]
Restart=<spec.restart or on-failure>
TimeoutStartSec=<spec.start_timeout>       # only if set

[Install]
WantedBy=default.target
```

A container with `oneshot: true` is started only by a job (§ "Jobs"), never
at boot. Its `[Service]` section is `Type=oneshot` followed by the optional
`TimeoutStartSec=` line, with no `Restart=` line, and it has **no `[Install]`
section**. Everything in `[Unit]` and `[Container]` renders as for any other
container.

Keys are emitted in exactly the order above, and a key whose field is unset
emits no line. That keeps the render byte-identical for documents that use
none of the optional keys, and keeps the audit's diff stable.

Rules:
- Every value taken from the document is written with each `%` doubled
  (`%%`). systemd expands `%` specifiers in unit files, and the quadlet
  generator passes these values into the service it generates, so an
  undoubled `date +%s` in `exec` would run as `date +<the user's shell>`. The
  container still receives a single `%`.
- `depends_on` entries are rewritten `<dep>` → `<slug>-<dep>.service` for
  both `After=` and `Requires=`.
- Every network unit the container joins is added to both `After=` and
  `Requires=`, in declared order, so the container will not start before its
  networks exist. A container with neither `network` nor `networks` joins
  `<slug>-net`. `network: host` adds no network unit.
- `entrypoint` and `aliases` go through `PodmanArgs=`, not through a dedicated
  quadlet key, because the quadlet generator in older podman releases still
  shipped by long-term-support distributions has no such key. Every
  generator accepts `PodmanArgs=`.
- **Validation** (`/apply-use-case` and `/modify-use-case` refuse a document
  that breaks one, naming the container and the key; `/audit-use-case`
  reports it as a static invariant):
  - `network` and `networks` are never both set;
  - every `networks` entry, and an explicitly set `network` other than
    `host`, names a key of `spec.networks`;
  - `network: host` excludes `ports`, `networks` and `aliases`. A non-host
    container excludes `listens`;
  - `cap_add` entries are bare capability names (`MKNOD`, not `CAP_MKNOD`),
    upper case;
  - `start_timeout` is a positive integer number of seconds;
  - a `ports` entry is `[<addr>:]<host>:<container>/<proto>` with single
    ports. A `listens` entry is `<port>/<proto>` or `<lo>-<hi>/<proto>`;
  - a mount names exactly one of `volume` (a key of this document's
    `spec.volumes`) or `from` (`<owner-slug>/<vol>`, § "Shared volumes");
  - a mount of a files volume (§ "Files volumes"), and every `from` mount,
    sets `ro: true`;
  - `subpath` is relative: no leading `/`, and no `..` component;
  - a `oneshot` container sets no `restart`, and no other container lists it
    in `depends_on`. Nothing would ever start it for them.
- `<source>` of a mount is:
  - `/var/lib/drydocc/volumes/<slug>/<vol>` for `volume: <vol>`;
  - `/var/lib/drydocc/volumes/<owner-slug>/<vol>` for `from: <owner-slug>/<vol>`;
  - either of the above followed by `/<subpath>` when the mount sets
    `subpath`, which mounts one file or subdirectory of the volume (e.g. a
    single configuration file at the path the image reads it from).
- `Volume=` has exactly three colon-separated fields — source, target, and
  one optional **options** field. `<opts>` is the comma-joined list of the
  options below, in this order; when the list is empty, omit the field *and*
  its colon. Never append an option to the target with a comma:
  `<src>:/data,ro` mounts read-write at a directory literally named
  `/data,ro`.
  - `Z` — the relabel option, or `z` for a volume more than one container
    mounts: one that two containers of this document mount (a `oneshot`
    container counts), or a shared volume, on the owner's mounts and every
    consumer's alike (§ "Shared volumes"). A private relabel by one container
    locks every other out. **The agent decides, the renderer does not.** The
    agent reads `system_facts.mac_framework` (enum documented in
    `references/node-profile.md`), checks the recorded framework against that
    framework's own probe tables — never guesses — and passes the verdict to
    every rendering tool as `relabel: true|false`: `true` only
    when the recorded framework is the kind that relabels bind-mount contexts;
    `false` when the value is `none`, `unknown`, or a framework that doesn't
    use this idiom. (An unconditional emit is often harmless — the option is
    commonly a no-op where nothing enforces relabeling — but "often harmless"
    is not the same as verified, and the fact exists precisely so this doesn't
    have to be guessed.)
  - `ro` — included when the mount declares `ro: true`.

  So a read-only mount renders `<src>:<target>:Z,ro` where relabeling
  applies and `<src>:<target>:ro` where it does not; a read-write mount
  renders `<src>:<target>:Z` or plain `<src>:<target>`.
- If `spec.env_from` is set, an `EnvironmentFile=` line is emitted. The
  apply procedure ensures the env file exists before starting the unit
  (via the `secrets` handshake).

### `<slug>-<network>.network` (one per `spec.networks.*`)

```ini
[Unit]
Description=<slug>/<network> (drydocc)

[Network]
NetworkName=<slug>-<network>
Internal=true                              # only if spec.internal == true
Subnet=<spec.subnet>                       # only if set, a CIDR

[Install]
WantedBy=default.target
```

### Volumes

**`.volume` units are NOT emitted.**
Persistent storage is always bind-mounted from
`/var/lib/drydocc/volumes/<slug>/<vol>/`. The apply procedure creates
this host directory directly with the declared mode/owner via
`sudo install -d -o <owner> -g <group> -m <mode>`. The `volumes:`
frontmatter list and `spec.volumes.*` map exist solely to drive that
step. A `from:` mount is not a volume of the consuming use-case and is never
listed in its `volumes:`.

`owner` is a host `user:group` (default `doccap:doccap`), or
`container:<uid>:<gid>`: the ids **as a container sees them**, for a volume
whose root a container's non-root user must own (an application served as
`www-data`, a database running as its own user). Rootless podman maps those
ids into doccap's subordinate range, so they have no stable host name. Create
such a volume as doccap, then hand it over inside doccap's user namespace:

```bash
sudo install -d -o doccap -g doccap -m <mode> /var/lib/drydocc/volumes/<slug>/<vol>
podman unshare chown <uid>:<gid> /var/lib/drydocc/volumes/<slug>/<vol>
```

Verify, and audit, the same way: `podman unshare stat -c '%u:%g %a' <dir>`
must read `<uid>:<gid> <mode>`. A host-side `stat` shows mapped numbers, which
are not drift.

#### Files volumes

A volume with `source: files` holds configuration the admin writes, not data
the service writes. Its content is exactly the tree at
`nodes/<node>/use-cases/<slug>.files/<vol>/` in the fork, copied verbatim. It
is a mirror: a file deleted from git is deleted from the node. The defaults
are `mode: "0755"` (directories) and `file_mode: "0644"` (files), so a
container's non-root user can read its configuration. Both can be overridden
on the volume.

- **No secrets and no templating.** A files tree is published to every clone
  of the fork, exactly like the document. A secret value goes through
  `env_from`, a per-node value through `env`. If a service can only read a
  secret from a file, it is out of scope — raise it with the admin as a
  framework change.
- **Always mounted read-only.** A write inside the container would be undone
  by the next sync, and the audit would report it as drift.

**Goal.** The volume directory on the node holds exactly the tree in git,
owned by the volume's owner, with the declared directory and file modes.

```bash
SRC=nodes/<self>/use-cases/<slug>.files/<vol>   # in the fork's working copy
DST=/var/lib/drydocc/volumes/<slug>/<vol>
sudo install -d -o <owner> -g <group> -m <mode> "$DST"
sudo rsync -rlpt --delete --chown=<owner>:<group> \
  --chmod=D<mode>,F<file_mode> "$SRC/" "$DST/"
```

`-p` is not optional: without it `rsync` ignores `--chmod`, and the files
land with the source's mode masked by the umask, possibly unreadable to the
container's user.

On the `on_client` surface the tree is on the laptop, not the node. Send it
with the same `rsync` over the node connection, with `--rsync-path="sudo
rsync"`, through `node-exec`'s SSH options.

**Verification.** The sha256 of every file under `$DST` (relative paths,
sorted) equals the same list computed over `$SRC`, and no other file exists
under `$DST`. Journal the file count and the combined digest, never the
content.

Who runs it:
- `/apply-use-case` syncs at bring-up, for every apply mode. A rebuild
  repopulates the volume from git, so it is never counted as data lost.
- `/modify-use-case` re-syncs when the tree changed, then restarts every
  container that mounts the volume. A single-file `subpath` mount keeps the
  file it was started with until a restart, so restarting is not optional.

#### Shared volumes

A volume with `shared: true` may be mounted read-only by other use-cases on
the same node. Typical use: certificates one use-case obtains and others
serve. The owner declares and writes it, and a consumer mounts it as
`{ from: <owner-slug>/<vol>, target: <path>, ro: true }`. Rules:

- the consumer lists the owner in its top-level `depends_on`, and the owner
  must be `active` before the consumer is applied;
- consumers only ever read; write access across use-cases is out of scope;
- on a node whose `system_facts.mac_framework` relabels bind mounts, a
  shared volume uses the shared relabel option (`z`) on every mount of it,
  the owner's included. A private relabel (`Z`) by either side would lock
  the other out;
- the owner backs it up, and a consumer never does. `/remove-use-case`
  refuses to retire the owner while a non-retired consumer still mounts it;
- a consumer reads files with its own user. When the owner writes files only
  its own user may read (a private key, say), granting the consumer access is
  the owner's job (a group or mode its writer sets). Document it in both
  use-cases' `## Runbook`.

## Jobs

A use-case's scheduled work: renewing a certificate, running an application's
background worker, reacting to a change in a volume it reads. Declared under
`spec.jobs`, rendered on the node like the backup automation, never committed:

```yaml
spec:
  jobs:
    renew:
      schedule: "*-*-* 03,15:17:00"     # HH:MM, or a literal OnCalendar= expression
      run: certbot                      # a oneshot container of this use-case
      watch: { volume: certs, path: live }
      then:
        - exec: { container: proxy, command: "nginx -s reload" }
      timeout: 900                      # seconds; default 900
    cron:
      schedule: "*:0/5"
      exec: { container: app, user: www-data, command: "php -f /var/www/html/cron.php" }
```

- **`run`** starts a `oneshot` container of this use-case and waits for it to
  exit. **`exec`** runs `command` (shell words) in a running container of this
  use-case, optionally as `user`. A job has at most one of the two.
- **`watch`** names a tree: `volume: <vol>` of this use-case, or `from:
  <owner-slug>/<vol>` of a shared volume this use-case mounts. `path` is
  relative and defaults to the whole volume. Each run computes one digest of
  the tree, following symbolic links, and compares it with the previous run's.
  The first run only records it.
- **`then`** is a list of `restart: <container>` and `exec: {container,
  command, user}` actions on **this use-case's** containers, run in order
  after a successful `run`/`exec`. With `watch`, they run only when the
  digest changed. Without `watch`, they run after every success. A use-case
  that must react to another's change declares its own job watching the
  shared volume. The owner never acts on a consumer's containers.
- **Validation** (refused like any other spec error): `schedule` is set; at
  least one of `run`, `exec`, `then`; `run` names a `oneshot` container;
  `exec` and `then` name non-`oneshot` containers of this use-case; a `watch`
  `from:` is a shared volume this use-case mounts; `path` follows the
  `subpath` rule.

**Rendered files**, per job, with `<job>` = `job-<slug>-<name>`:

- **Script** `~/.config/drydocc/<job>.sh`, mode `0700`, assembled from the
  declaration. Sections absent from the declaration are left out:

  ```bash
  #!/usr/bin/env bash
  # Rendered by drydocc from spec.jobs.<name> of use-case <slug>. Do not edit; re-render.
  set -uo pipefail
  JOB=<job>; ST=/var/lib/drydocc/jobs/<slug>-<name>
  if [ -r "$HOME/.config/drydocc/notify.sh" ]; then
    . "$HOME/.config/drydocc/notify.sh"
  else
    dd_notify() { :; }; dd_hb_arm() { :; }; dd_hb_disarm() { :; }; dd_check_overdue() { :; }
  fi
  exec 9>"$ST.lock"
  flock -n 9 || { echo "$JOB already running"; exit 0; }
  rc=0; changed=no

  # run — or exec
  systemctl --user start <slug>-<run>.service || rc=$?
  podman exec [--user <user>] <slug>-<container> <command> || rc=$?

  # watch — the digest is taken inside doccap's user namespace, so files a
  # container user owns are readable
  if [ "$rc" -eq 0 ]; then
    W=/var/lib/drydocc/volumes/<slug or owner-slug>/<vol>/<path>
    now=$(podman unshare sh -c 'cd "$1" && find -L . -type f -print0 | sort -z | xargs -0r sha256sum | sha256sum' sh "$W" | cut -d' ' -f1) || rc=$?
    prev=$(cat "$ST.watch" 2>/dev/null || true)
    [ "$rc" -eq 0 ] && [ -n "$prev" ] && [ "$now" != "$prev" ] && changed=yes
    [ "$rc" -eq 0 ] && printf '%s\n' "$now" > "$ST.watch"
  fi

  # then — after success; with watch, only on a change
  if [ "$rc" -eq 0 ] && [ "$changed" = yes ]; then     # without watch: if [ "$rc" -eq 0 ]; then
    systemctl --user restart <slug>-<container>.service || rc=$?
    podman exec [--user <user>] <slug>-<container> <command> || rc=$?
  fi

  printf '%s rc=%s changed=%s\n' "$(date -u +%FT%TZ)" "$rc" "$changed" >> "$ST.log"
  if [ "$rc" -eq 0 ]; then dd_hb_arm "$JOB" <interval-seconds>
  else dd_notify run-failed "$JOB on <node> failed" "rc=$rc"; fi
  exit "$rc"
  ```

  `<interval-seconds>` is computed at render time from `schedule`, exactly as
  `node-notify/references/notify-recipes.md` § "Arming a missed-run alarm"
  defines it. A job whose interval is too long to arm is watched by the
  maintenance sweep (§ "Jobs too long to arm" there), with `$ST.log` as its
  run log.
- **Service** `~/.config/systemd/user/drydocc-<job>.service`: `Type=oneshot`,
  `ExecStart=` the script, `TimeoutStartSec=` the job's `timeout`.
- **Timer** `~/.config/systemd/user/drydocc-<job>.timer`: `OnCalendar=` from
  `schedule` (`HH:MM` → `*-*-* HH:MM:00`), `Persistent=true`.

**Bring-up and teardown.** `/var/lib/drydocc/jobs/` is created once per node,
like the backup state directory (`sudo install -d -o doccap -g doccap -m 0750
/var/lib/drydocc/jobs`). These are plain units, so they are enabled normally:

```bash
systemctl --user daemon-reload
systemctl --user enable --now drydocc-<job>.timer
# teardown
systemctl --user disable --now drydocc-<job>.timer drydocc-<job>.service
rm -f ~/.config/systemd/user/drydocc-<job>.{service,timer} ~/.config/drydocc/<job>.sh
systemctl --user daemon-reload
```

On teardown, disarm the job's missed-run alarm (`dd_hb_disarm <job>`) and
delete `$ST.watch`. The run log stays as history.

**Verification.** `systemctl --user list-timers drydocc-<job>.timer` shows a
next elapse. Then `systemctl --user start drydocc-<job>.service` once by hand
(apply and modify do this, so a broken job fails while the admin is watching,
not at 3 a.m.), and its run log's last line reads `rc=0`.

## The drydocc tools

The scriptable core of the use-case workflows ships at `scripts/drydocc/`
as eleven operations, reached two ways with the same names and arguments:

- **As MCP tools**, from `scripts/drydocc/mcp_server.py` (stdio). It needs
  python3 with the `mcp` and PyYAML packages. Register it from a fork
  checkout:

  ```json
  {
    "mcpServers": {
      "drydocc": {
        "command": "python3",
        "args": ["scripts/drydocc/mcp_server.py"],
        "cwd": "<fork root>"
      }
    }
  }
  ```

- **From a shell**, as `python3 scripts/drydocc/drydo.py <operation> …`
  (`render-quadlets --node <self> --slug <slug> --relabel`). It needs
  python3 with PyYAML only. This is the fallback when the server is not
  configured.

| operation | used by |
|---|---|
| `render_quadlets` | `/apply-use-case` (this skill's render) |
| `diff_quadlets` | `/modify-use-case` (render + compare with the node's quadlets) |
| `write_quadlets` | `/apply-use-case`, `/modify-use-case` (write the changed and added files) |
| `audit_probe_run` | `/audit-use-case` Tier 1 (probe + quadlet comparison) |
| `validate_use_case` | the static invariants (every finding, not the first) |
| `list_use_cases` | `/list-use-cases` |
| `resume_pointer_status` | `/continue-last-command` |
| `owes_restore` | the journal's "Rebuilt, not yet restored" query |
| `lingering_launch`, `lingering_poll`, `lingering_cleanup` | `node-exec`'s `NODE_RUN_LINGER` |

Every operation takes the bound node's name (and a slug), never a path, and
refuses anything outside `nodes/<node>/`. A node-touching one also takes the
bound surface. `render_quadlets` and `diff_quadlets` return a `digest` of
the render; `write_quadlets` requires the digest of the render the admin
confirmed and refuses if the render changed since, so what lands on the node
is what was approved. Every comparison ignores trailing newlines: a file
written with an extra final newline is not a change. A failure comes back
as an error carrying its message. An
operation that renders or probes returns a `replay` line: the `drydo.py`
command that repeats it. That line is what a journal's `## Commands`
records, so an admin can re-run the step at that entry's commit without
the agent. No operation prints, logs, or returns a secret value.

When neither python3 nor PyYAML is available where the agent runs, each
skill's written procedure remains the contract to follow by hand.

## What this skill does NOT do

- Migrate from docker-compose (out of scope).
- Manage non-container services (system packages, firewall rules). A job
  only ever runs containers of its own use-case.
- Detect drift. The render function above is the building block;
  `/audit-use-case` owns the comparison.
