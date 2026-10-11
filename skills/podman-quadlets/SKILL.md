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

## Privileged ports

Rootless podman publishes a port as the unprivileged `doccap` user, and the
kernel refuses an unprivileged bind below
`net.ipv4.ip_unprivileged_port_start` (1024 by default; the setting covers
IPv6 too). A `PublishPort=443:443/tcp` therefore fails at container start
unless the floor is at or below 443.

**Goal.** Before any container that publishes a host port is started, the
node's floor is at or below the lowest host port that container publishes.
The host port is the number before the container port in a `ports:` entry
(`"443:443/tcp"` → 443; `"127.0.0.1:53:53/udp"` → 53).

```bash
LOWEST=<lowest host port across spec.containers.*.ports>
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

**Where the renderer lives.** There is no committed
renderer under `tools/`. The agent emits an inline
`python3 -c '...'` snippet (stdlib + PyYAML) at apply time that reads
the doc's frontmatter and produces the `{filename: ini-text}` dict.
The snippet is journaled verbatim as part of the action's
`## Commands` block, so admins can replay or audit without Claude. The
mapping below is the contract that snippet must implement; cross-distro
adjustments (e.g., paths, package names) are the agent's responsibility
to handle at generation time.

### `<slug>-<container>.container` (one per `spec.containers.*`)

```ini
[Unit]
Description=<slug>/<container> (drydocc)
After=<slug>-<network>-network.service <slug>-<dep>.service ...
Requires=<slug>-<network>-network.service <slug>-<dep>.service ...

[Container]
Image=<spec.image>
ContainerName=<slug>-<container>
Network=<slug>-<spec.network>.network
EnvironmentFile=<spec.env_from>            # only if env_from is set
Environment=KEY=VAL                        # one line per spec.env entry
Volume=/var/lib/drydocc/volumes/<slug>/<vol>:<target>[:<opts>]   # one per mount
PublishPort=<host>:<container>/<proto>     # one per port
Exec=<spec.exec>                           # only if non-empty

[Service]
Restart=<spec.restart or on-failure>

[Install]
WantedBy=default.target
```

Rules:
- `depends_on` entries are rewritten `<dep>` → `<slug>-<dep>.service` for
  both `After=` and `Requires=`.
- The network unit is always added to both `After=` and `Requires=` so
  the container will not start before its network exists.
- `Volume=` has exactly three colon-separated fields — source, target, and
  one optional **options** field. `<opts>` is the comma-joined list of the
  options below, in this order; when the list is empty, omit the field *and*
  its colon. Never append an option to the target with a comma:
  `<src>:/data,ro` mounts read-write at a directory literally named
  `/data,ro`.
  - `Z` — the relabel option, gated on `system_facts.mac_framework` (enum
    documented in `references/node-profile.md`): include it only when the
    recorded framework is the kind that relabels bind-mount contexts; omit it
    when the value is `none`, `unknown`, or a framework that doesn't use this
    idiom. (An unconditional emit is often harmless — the option is commonly
    a no-op where nothing enforces relabeling — but "often harmless" is not
    the same as verified, and the fact exists precisely so this doesn't have
    to be guessed.)
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
step.

## What this skill does NOT do

- Migrate from docker-compose (out of scope).
- Manage non-container services (system packages, firewall rules).
- Detect drift. The render function above is the building block;
  `/audit-use-case` owns the comparison.
