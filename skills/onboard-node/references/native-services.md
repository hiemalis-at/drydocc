# Native service triage — recipes

Reference material for `/onboard-node` Phase 4. A host-native service is a
systemd unit that maps to no container. Each one is sorted into exactly one of
three outcomes: **node-infra** (no document), **mapped** (an ordinary
containerized use-case document), or **capture** (a document that records the
service without being able to rebuild it yet).

**Dialect drift:** unit names and owning packages differ across distros and
across releases of one distro (`postgresql.service` vs
`postgresql@15-main.service`). Match on the stem patterns below and confirm
against the live unit; never assume a name from the distro alone. When a probe
below returns nothing, report the miss — do not silently fall through.

## Tier 0 — node infrastructure (no document)

These are modelled as `hardening.*` in `node.yaml` and rebuilt by
`/bootstrap-node`. Journal them as node infrastructure and move on.

A box running something outside every row below is not unsupported — it is
simply not Tier 0; confirm against the live unit before deciding, per the
"Dialect drift" note above, and extend the table in place once you've
verified a new one.

### Observed dialects — evidence, not the supported list

Unit names seen in the wild, to match against. **Not exhaustive, and not a
statement of which systems are supported.**

| Match | Why |
| --- | --- |
| `ssh.service`, `sshd.service` | `hardening.ssh_*` |
| `tailscaled.service` | `hardening.tailscale` |
| `fail2ban.service` | `hardening.fail2ban` |
| the unit named by `system_facts.firewall.tool` | `hardening.firewall` |
| `chrony*.service`, `systemd-timesyncd.service`, `ntpd.service` | clock |
| `cron.service`, `crond.service` | scheduler |
| `unattended-upgrades.service`, `dnf-automatic*.timer` | patching |
| `NetworkManager.service`, `systemd-networkd.service` | networking |
| `qemu-guest-agent.service`, `cloud-init*.service` | hypervisor/instance glue |
| `systemd-*`, `dbus*`, `polkit*`, `rsyslog*`, `auditd*` | base OS |

## Tier 1 — mapped daemons

A match emits a normal containerized use-case document. The table gives the
image **repository only** — the agent prompts the admin for a pinned tag or a
`RepoDigest`, because an unpinned image is a validation error.

| Unit stem pattern | Image repository | Config path(s) | Data path(s) |
| --- | --- | --- | --- |
| `nginx` | `docker.io/library/nginx` | `/etc/nginx` | `/var/www`, `/srv/www` |
| `apache2`, `httpd` | `docker.io/library/httpd` | `/etc/apache2`, `/etc/httpd` | `/var/www` |
| `caddy` | `docker.io/library/caddy` | `/etc/caddy` | `/var/lib/caddy` |
| `postgresql*` | `docker.io/library/postgres` | `/etc/postgresql` | `/var/lib/postgresql` |
| `mariadb`, `mysql` | `docker.io/library/mariadb` | `/etc/mysql` | `/var/lib/mysql` |
| `redis*`, `valkey*` | `docker.io/library/redis` | `/etc/redis` | `/var/lib/redis` |
| `mosquitto` | `docker.io/library/eclipse-mosquitto` | `/etc/mosquitto` | `/var/lib/mosquitto` |
| `grafana*` | `docker.io/grafana/grafana` | `/etc/grafana` | `/var/lib/grafana` |
| `prometheus` | `docker.io/prom/prometheus` | `/etc/prometheus` | `/var/lib/prometheus` |

Like Tier 0, this is an observed-patterns table, not an exhaustive or
supported-distro list. Extend it when a new daemon is encountered; a miss is
a Tier 2 capture, never a guess.

`<slug>` throughout this file is the document's slug, derived from the unit name
per `onboard-recipes.md` § "Service discovery rules".

Emit, per match: `status: planned`; `spec.containers.<slug>` with the pinned
image; the config path as a declared volume mounted read-only where the daemon
allows it, the data path as a declared volume; `ports:` from the live listening
sockets; and a `## Migration` section whose `unit:` line names the host unit and
whose `config:`/`data:` lines carry the same host paths the volumes were derived
from — what each of those fields feeds during the cut-over is the `conventions`
skill's `references/use-case-doc.md` § Migration.

**Mount each volume at the host path it came from**, so the mount `target` and
the `## Migration` path are the same string — that identity is what lets
`/apply-use-case` join a path to a volume. Where the upstream image nests the
path (`/var/lib/postgresql` → `/var/lib/postgresql/data`) the strings differ, and
whether the join resolves is decided by that command's parent-of-target rule
(`apply-use-case/SKILL.md` step 5). Where the image puts the data somewhere
unrelated, use the image's path as the target and say so in the `## Migration`
prose: the join will not resolve, the apply plan will show that volume with no
bound source, and reconciling the pair is the admin's work before applying.

## Tier 2 — capture

Everything unmatched. Emit a document with empty
`containers`/`networks`/`volumes`, matching empty `spec:` sub-keys, `ports:`
from the live listening sockets, and a `## Migration` section carrying the full
probe output. The document must say outright that it is not rebuildable as it
stands and what the next step is; the body boilerplate that says it is in
`onboard-recipes.md` § "Spec reverse-engineering rules".

## Probes

Per unit, run over the established session:

```bash
systemctl show <unit> -p FragmentPath,ExecStart,WorkingDirectory,StateDirectory,ConfigurationDirectory,UnitFileState
cat "$(systemctl show <unit> -p FragmentPath --value)"
```

Owning package — dispatch on `system_facts.package_manager`. Goal: identify
the package that installed the unit's fragment file, so the journal can
record provenance. Verify: the dispatch returns either a package name or the
manager's own explicit "not owned by any package" answer — one of the two is
required; a silent miss is a bug, not a valid outcome, and must be reported
rather than swallowed. The per-`package_manager` query syntax that
implements this dispatch is a recipe concern and lives in a workflow
skill's `references/*-recipes.md`, never inline in this file (Rule 2).

Listening sockets come from the Phase 3 inventory (`ss -lntupH`); do not
re-probe. Config and data paths for a Tier 1 match come from the table above,
confirmed to exist with `test -d`. There is deliberately no generic filesystem
crawler.
