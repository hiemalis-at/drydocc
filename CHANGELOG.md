# Changelog

All notable changes to `drydocc` are recorded here. Each release is one snapshot
of the framework, published to `hiemalis-at/drydocc`. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the major
version is 0, a minor release may change conventions or the command surface.

## [Unreleased]

## [0.4.1] - 2026-10-20

### Secrets

- **KeePassXC lookup URLs now name their entry in the host**, most specific
  label first: `drydocc://nextcloud.vps-fra-01` for a use-case,
  `drydocc://cold-s3.backup.vps-fra-01` for a backup target,
  `drydocc://doccap-at-vps-fra-01.ssh.vps-fra-01` for a node's SSH key. Globals
  stay `drydocc://tailscale` and `drydocc://ntfy`. KeePassXC matches a URL by
  its host and ignores the path, so 0.4.0's `drydocc://<node>/<use-case>`
  returned every entry of a node for every lookup, and the agent could read
  another use-case's secret.
- **Each entry's title is its URL without `drydocc://`,** and every lookup
  selects by that title. That closes KeePassXC's fallback from a missing
  entry to its parent. An entry created with `kpxc-agent set-login` gets that
  title automatically.
- **Multi-key values now sit in custom fields named `KPH: <NAME>`.**
  KeePassXC hands custom fields to `kpxc-agent` only under that prefix, and
  only with **Settings → Browser Integration → Advanced → "Return advanced
  string fields"** enabled, which is now required. "Return only best-matching
  credentials" is recommended.
- **Requires the `keepassxc-secrets` skill with `kpxc-agent` 1.1.0 or later.**
  It adds the title selection and custom-field reads. With an older one,
  secrets come from the environment or a `.env` file.
- A use-case may not be named `backup` or `ssh`.
- **To migrate a fork from 0.4.0:**
  - Rewrite every `keepass_entry`: `drydocc://<node>/<a>` becomes
    `drydocc://<a>.<node>`, and `drydocc://<node>/<a>/<b>` becomes
    `drydocc://<b>.<a>.<node>`.
  - In KeePassXC, give each entry the same URL and a title without
    `drydocc://`.
  - Prefix every custom field with `KPH: `.
  - Turn on the two settings.
  
  A value still in the path form is refused with its rewrite named.

## [0.4.0] - 2026-10-19

### Secrets

- KeePassXC lookup URLs follow one standard,
  `drydocc://<node-name>[/<use-case>][/<user-or-detail>]`, and `keepass_entry`
  now holds that URL itself instead of a `drydocc/…` path the agent turned into
  `https://drydo.cc.local/…`. Credentials every node shares sit in the node-name
  position (`drydocc://tailscale`, `drydocc://ntfy`), so a node may not be named
  after one. An entry of its own for one credential uses the detail segment:
  an S3 backup target is `drydocc://<node>/backup/<target>`, a node's admin SSH
  key `drydocc://<node>/ssh/doccap-at-<node>`. A use-case's variables still
  share one entry, one custom field each. **To migrate a fork:** rewrite every
  `keepass_entry` value from `drydocc/<rest>` to `drydocc://<rest>` (backup
  targets from `drydocc/<node>/backup-<target>` to
  `drydocc://<node>/backup/<target>`), and set each KeePassXC entry's URL field
  to the same string. A value still in the old spelling is refused with the
  rewrite named.

### Use-cases

- A container's `spec:` can now describe the services a real box runs, not
  only the simple ones. All new keys are optional, and a document that uses
  none of them renders exactly as before:
  - `network: host` for a service that must share the node's network: a relay
    binding a port range on demand, or a mail server or reverse proxy that
    needs the real client address. Such a container publishes nothing and
    declares what it binds in `listens:`.
  - `networks: [a, b]` to join several of the use-case's networks, and
    `aliases:` for extra DNS names on them.
  - `cap_add:`, `hostname:`, `user:`, `entrypoint:`, `healthcheck:` and
    `start_timeout:`.
  - `subnet:` on a network, so an address range that other configuration
    relies on survives a rebuild.
  - A published port may name a bind address, `127.0.0.1:8080:80/tcp`, for a
    backend that must only be reachable from the node itself.
- The top-level `ports:` list is now explicitly the use-case's firewall list:
  what must be reachable from outside, and nothing else. It may hold a range,
  `49152-65535/udp`, opened as one tagged rule. A loopback-only publish is
  never listed there.
- `/apply-use-case`, `/modify-use-case` and `/audit-use-case` refuse or report
  contradictory combinations (a host-network container with published ports,
  `network` and `networks` both set), naming the container and the key.
- `/onboard-node` now carries all of the above over from a running container:
  host networking and what it listens on, every network a container is on
  (no longer merged into one), aliases, added capabilities, hostname, user,
  entrypoint, healthcheck, bind addresses and network subnets.
- Configuration files can live in git next to the use-case. A volume that
  declares `source: files` holds exactly the files under
  `nodes/<node>/use-cases/<slug>.files/<volume>/`: synced on every apply,
  re-synced by `/modify-use-case` (which restarts whatever mounts it),
  compared file by file by `/audit-use-case`, and rebuilt from git rather
  than restored from a backup. No templating, and never a secret in such a
  file. A mount may set `subpath` to mount a single file of a volume.
- One use-case can read another's files. A volume declared `shared: true`
  can be mounted read-only by another use-case with `from: <owner>/<volume>`,
  provided the owner is listed in its `depends_on` and is active.
  `/remove-use-case` will not retire an owner that a consumer still mounts.
- A use-case can declare its own scheduled jobs under `spec.jobs`. A job can
  run a container that exists only for it (`oneshot: true`, never started at
  boot), or `exec` a command in a running container. It can watch a volume,
  or a shared volume it reads, and run follow-up actions (restart, exec) only
  when that content changed. A certificate renewal reloads the proxy only
  when a certificate actually changed, and a consumer of those certificates
  restarts itself when they change.
  - Jobs get the same treatment as the backup automation: a lock, a run log,
    a failure notification and a missed-run alarm.
  - `/apply-use-case` runs every job once while you watch.
  - `/audit-use-case` reports failing and missed runs.
  - `/remove-use-case` takes the jobs down with the use-case.
- `/apply-use-case` can adopt containers that another user runs as rootless
  quadlets, the way a box set up by hand before drydocc usually looks. The
  use-case's `## Migration` section names them:
  - `owner:` is the user whose podman runs them (`root` for rootful).
  - `containers:` maps the document's container names to the names they
    carry today (`app=systemd-wiki`).
  - `units:` lists every host unit to switch off with the cut-over, a user's
    own as `<user>/<unit>`. A single `unit:` still works.

  The cut-over stops the old unit and moves its quadlet file aside as
  `.preadopt`, so the old stack can still be started again. Data is copied
  through both users' namespaces, so files keep the owner the containers
  expect. **Changed for host services:** files copied from a host service's
  `config:`/`data:` paths now take the volume's declared owner, instead of
  keeping host ids that no rootless container can use.
- A volume's `owner` may be written `container:<uid>:<gid>`, for a volume
  whose root a container's own user must own (`www-data`, a database user).
  The audit checks it as the containers see it.
- `/onboard-node` now finds the containers and user timers of every
  lingering user, not only the user it connects as. Quadlet containers are
  grouped by the network they share rather than by their common `systemd-`
  name prefix, and their documents get the `owner:` and `containers:` lines
  they need.
- `/modify-use-case` and `/remove-use-case` now mark their confirmations with
  decision ids, so a lab scenario can answer them. So does `/audit-use-case`'s
  capture-back menu: a scenario that expects drift can now run a full audit.
- A `%` in a spec value, such as `date +%s` in a container's command, now
  reaches the container as written. Before, systemd expanded it as a unit-file
  specifier.
- A volume that two containers of one use-case mount, a job's container
  included, now gets the shared relabel on a node whose MAC framework
  relabels mounts. With the private one, the container that started second
  locked the other out. An existing use-case with such a volume shows as
  quadlet drift in `/audit-use-case`; `/modify-use-case` renders it again.
- `/audit-use-case` no longer reports drift on a container nobody touched.
  Every container showed a changed `/etc`, which is where podman puts its own
  `hosts`, `hostname` and `resolv.conf`, and every changed path also listed its
  parent directories. It also accepts a quadlet's unit as `generated`, the
  only state such a unit reports, and counts a legacy quadlet unit that is
  still in place as live, since it starts at boot. A job's `oneshot`
  container, removed after every run, is no longer reported as unknown.
- After adopting another user's quadlet stack, the prune list also names that
  user's network quadlets the adopted containers used. They stay in place
  until you remove them, so a rollback can still start the old stack.

### Hardening

- `/bootstrap-node` no longer bans its own commissioning client when it
  enables fail2ban. A jail reads the auth log back when it starts; the
  lockdown's own check that root is refused logs up to three failures each
  time, and two such checks were enough to ban the admin's address on the
  spot. The client is now exempt while the jail starts, the exemption is
  dropped once the jail is up, and a fresh connection must succeed before the
  step counts as done. Found when a lab bootstrap locked itself out;
  reproduced and verified on a lab node.

### Lab

- Lab runs no longer see each other. A run's fork is now a standalone clone
  in the lab store instead of a worktree of your checkout, so an agent can no
  longer read another run's branch — a model in the first qualification
  copied an earlier model's work that way. Your repository gains no lab
  branches; forks made before this are still cleaned up.
- In a lab run, a check that must come from off the node (a published port
  answers) is made from a peer node when the run has one, and otherwise
  named in the journal as not verifiable in the lab instead of being skipped
  silently or turning the apply into `partial`.
- Agents in a lab run can no longer reach the hypervisor. `virsh`, the
  disk-image tools and the libvirt connection are out of reach, so an agent
  that loses a node cannot "repair" it from the host — a real box has no host.
  A node that stops answering is recorded as a finding and ends the step.

- The lab can run on free models: a new agent adapter, `pi`, drives pi
  (pi.dev) on OpenRouter's free models — or on any cheap paid model there. Lab runs no longer have to cost money
  per request. Free models get withdrawn and throttled, so it falls back
  through a ranked list (`scripts/lab/agents/pi/models`) within the same
  session. A shared daily cap or a bad key ends the step as an
  infrastructure error, never as a test failure. Setup is in
  `scripts/lab/agents/README.md` — including the privacy notice: free
  providers log what the lab agent sees.
- `lab.py qualify` measures which models can run drydocc: scenarios × models,
  one pinned model per run, and a pass-rate matrix with requests and minutes.
  An overloaded model is skipped, not fatal; it resumes after a daily cap. A
  model stuck repeating one tool call is stopped and fails its step. Its
  runs are independent: it starts only when no other lab run's work is
  readable, and cleans up each run once recorded. First results: Nemotron 3
  Ultra (free), DeepSeek V4.1 Flash and Claude Haiku 5.5 (about 4 cents a
  scenario) pass `whoami` and `web-db`.
- The lab now tells you your agent options. `lab.py agents` lists every
  adapter: ready or not, paid or free, its model and qualification. `run`,
  `up`, `verify-runbook` and `qualify` open with the agent in use, its cost,
  and the alternatives. `/lab` asks which agent to use and recommends the
  cheapest qualified one.
- `DRYDOCC_LAB_ADAPTER` sets the default adapter, and `run --model` /
  `DRYDOCC_LAB_MODEL` the model. Reports name the models a step used and its
  request count.

## [0.3.0] - 2026-10-15

### Hardening

- `ssh_root_login: disabled` now also means that root holds no authorized
  SSH key. `/bootstrap-node` moves root's keys aside to a dated file (never
  deletes them) once the lockdown is verified, and journals their
  fingerprints. Provisioning, cloud images and hosting providers often leave
  such keys behind. `/onboard-node` does the same as part of the
  `ssh_root_login` item, with the admin's approval.
- A new baseline field, `name_responders: disabled`: no LLMNR or mDNS
  responder answers beyond loopback. The responder is switched off in the
  daemon that owns the listener, found by socket, not only blocked by the
  firewall. Ordinary name resolution keeps working.
- Nodes bootstrapped earlier pick up both on their next `/bootstrap-node`
  audit & repair. `/node-maintenance`'s `node-hardening-holds` checks both.
- `/onboard-node` initialises the file-integrity database as the last thing
  it does on the box, after the fork clone. It first creates `doccap`'s
  config directories, so the first check on an adopted box is clean.

### Maintenance

- `/node-maintenance`'s questions carry decision ids
  (`maintenance.act`, `.reboot`, `.promote`, `.enable-sampler`), and
  `/onboard-node`'s forwarding question does too
  (`onboard.forwarding-consumer`), so lab scenarios can answer them.
- An accepted file-integrity re-base (`[s] accept file-integrity-verified`)
  now runs last in the sweep, after the maintenance automation is rendered.
  Before, the new database missed the run's own files and the next check
  flagged them.

### Lab

- Commissioned clones no longer get the lab's key for root. The lab reaches
  them as `doccap`, and root keeps no key, as bootstrap left it.

## [0.2.0] - 2026-10-14

### Hardening

- `/bootstrap-node` adds three items to the baseline, each a new
  `hardening.*` field in `node.yaml`:
  - `kernel_hardening` — a drydocc sysctl drop-in: address-space
    randomisation, restricted kernel pointers and kernel log, SYN cookies, no
    ICMP redirects. Packet forwarding is turned off only when nothing on the
    node forwards. A docker host, or a node advertising tailnet routes, keeps
    it on with loose reverse-path filtering.
  - `ssh_session_policy` — sshd refuses empty passwords and X11 forwarding,
    requires public keys, and drops dead peers through keepalive probes.
    Idle sessions stay connected.
  - `file_integrity` — a file-integrity database, initialised last on the
    hardened box. Its hash goes in the journal, so a later check can tell if
    the database was rewritten.
- sshd's lockdown also writes `KbdInteractiveAuthentication no`.
  `PasswordAuthentication no` alone left password login open through PAM's
  keyboard-interactive prompt. `ssh_password_auth: disabled` now needs both
  to be off. Existing nodes get the line on their next audit-&-repair run.
- `/onboard-node` classifies and reconciles all eight fields. It never turns
  forwarding off under a running consumer, and it initialises the integrity
  database after everything else the run changes.
- Nodes bootstrapped earlier read the new fields as `unknown`. Re-run
  `/bootstrap-node <node>` (audit & repair) to establish them.
- The `doccap` sudo rule is documented as what it is: passwordless, for any
  command, and the one such rule the framework writes.

### Maintenance

- `/node-maintenance` gains eight security-audit posture items:
  `node-hardening-holds`, `access-reviewed`,
  `privileged-files-within-baseline`, `persistence-reviewed`,
  `exposure-matches-declaration`, `auth-events-reviewed`,
  `file-integrity-verified` and `build-toolchain-absent`. Each compares the
  node with its own last accepted state, recorded in the new
  `security_baseline` block of `nodes/<node>/maintenance.yaml`. They report
  and never remove anything. `[s] accept <id>` re-bases one item after
  showing its changes, and critical findings cannot be accepted that way.
- Add the eight ids to `nodes/maintenance.yaml` to set their mode
  fork-wide. Ids absent from both layers default to `report`.

- The bundled probes of `/node-maintenance`, `/onboard-node` and
  `/audit-use-case` are sent as an argument with stdin closed, not as a
  heredoc. A command that prompted on stdin, such as a package manager asking
  to import a repository key, could swallow the rest of the probe.
- The sudo review treats an `ALL` rule for `root`, or for a group with no
  members, as information rather than a finding.

### Lab

- `extract` resets the skill-written half of `maintenance.yaml`
  (`verified_auto`, `security_baseline`, the last-run fields), because it
  describes the lab box. An `auto` item stays refused on the real box until
  that box has watched it succeed.

## [0.1.0] - 2026-10-11

First public release.

### Agents and skills

- 21 skills in the [Agent Skills](https://agentskills.io) format, with
  frontmatter strict enough for any compliant client and for the skills.sh and
  agentskill.sh registries. Each description says what the skill does and when
  to use it.
- `AGENTS.md` holds the invariants every agent keeps: node scoping, the
  append-only journal, secrets out of git, conventions as the authority,
  distro-agnosticism, and one fact, one level. It is aliased as `CLAUDE.md`
  and `VIBE.md`. Skills are exposed through `.claude/skills`, `.vibe/skills`
  and `.agents/skills`.
- *First contact* routing: in a fresh clone, the agent tells a plain clone of
  this repository from a ready private fork, and walks the admin to the right
  step.
- A skill run outside a fork refuses and points to the quickstart, so a skill
  installed on its own from a registry leads to a fork.
- Tested with Claude Code and Mistral Vibe.

### Nodes

- `/provision-vm` provisions a local qemu VM, rootless through the per-user
  libvirt connection wherever the host allows it. It works for the host's own
  architecture or an emulated one, such as an aarch64 node on an x86 laptop.
  It downloads the image, renders the unattended install, and leaves the box
  reachable over SSH.
- `/bootstrap-node` takes a fresh box to the baseline, optionally provisioning
  it as a local VM first. The baseline is:
  - key-only SSH on its own port, with root and password login off;
  - the unprivileged `doccap` user;
  - host hardening;
  - a tailnet join;
  - a clone of the fork that pushes with the node's own deploy key.

  The run is resumable and idempotent, and verifies every hardening line it
  writes.
- `/onboard-node` adopts a box that is already configured without flattening
  it. Running containers and host services are reverse-engineered into
  planned use-cases first. Every hardening difference is then offered as
  apply, skip or defer.
- `/rebuild-node` is whole-node disaster recovery:
  - triage, and repair of the base on replacement hardware;
  - every use-case rebuilt in dependency order, with lost data restored;
  - unattended work re-armed;
  - a report of every volume that did not come back.
- Distribution tooling is probed on the box and recorded as `system_facts`,
  never assumed. systemd is the one hard requirement.

### Use-cases

- `/create-use-case`, `/apply-use-case`, `/modify-use-case`,
  `/remove-use-case` and `/list-use-cases` cover the use-case lifecycle on
  rootless podman quadlets. Each runs on the node itself, or from the admin's
  laptop over SSH.
- `/apply-use-case` cuts over whatever a use-case replaces reversibly. It
  recognises a disaster-recovery rebuild when an active use-case's footprint is
  gone from the box. It lowers the unprivileged port floor only as far as a
  declared host port needs.
- `/audit-use-case` reports drift between a declaration and the live node in
  both directions. It also flags unmanaged configuration that a rebuild would
  silently destroy.

### Data protection

- `/backup-use-case` proposes, tunes and activates per-use-case volume backups
  to an rsync or S3-compatible target.
- `/restore-use-case` restores a sealed snapshot, into scratch space to prove
  the backup (`--test`) or live. It moves current volumes aside and never
  deletes them.

### Operations

- `/node-maintenance` performs recurring upkeep derived from what the node
  actually runs:
  - image currency, package updates and log growth;
  - fact freshness and certificate expiry;
  - intrusion-prevention review and tailnet health;
  - backup coverage and credential rotation windows;
  - per-container telemetry, judged against a learned baseline.

  Every node in the fork is held to one posture.
- `/node-notify` sends optional ntfy push notifications for failed runs,
  telemetry anomalies, and missed-run alarms when a timer, or a whole node,
  goes silent.
- `/continue-last-command` resumes a run that paused for a missing secret, or
  that the agent itself dropped: an unreachable server, exhausted credits, a
  crash, a closed window. It first reconciles what the dead run left behind,
  then resumes the run or journals it as abandoned.
- Secrets are resolved just in time, from the environment, then KeePassXC, then
  a pasted `.env` that never shows a value on screen. The recommended
  `keepassxc-secrets` skill is suggested, never installed.

### Lab

- `/lab` runs `drydocc` on throwaway local VMs, cloned from sealed layers and
  isolated from the admin's real remotes, tailnet and vault.
  - **Scenario test runs.** A headless agent drives the steps, and only
    deterministic assertions judge them. `--repeat` tells a flake from a
    regression. Two example scenarios ship in `lab/scenarios/`.
  - **Prototypes.** Interactive, with named restore points.
  - **Runbook bundles.** Extracted from a prototype, replay-verified on a fresh
    box, and promoted into the fork as a new node on its own branch.
- Multi-node runs, power-cut and reboot fault injection, and optional runs on a
  real tailnet with ephemeral, tagged keys.
- Agent adapters for Claude Code and Mistral Vibe, behind one documented
  contract in `scripts/lab/agents/README.md`.

[Unreleased]: https://github.com/hiemalis-at/drydocc/compare/v0.4.1...HEAD
[0.4.1]: https://github.com/hiemalis-at/drydocc/releases/tag/v0.4.1
[0.4.0]: https://github.com/hiemalis-at/drydocc/releases/tag/v0.4.0
[0.3.0]: https://github.com/hiemalis-at/drydocc/releases/tag/v0.3.0
[0.2.0]: https://github.com/hiemalis-at/drydocc/releases/tag/v0.2.0
[0.1.0]: https://github.com/hiemalis-at/drydocc/releases/tag/v0.1.0
