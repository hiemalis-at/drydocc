# Changelog

All notable changes to `drydocc` are recorded here. Each release is one snapshot
of the framework, published to `hiemalis-at/drydocc`. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the major
version is 0, a minor release may change conventions or the command surface.

## [Unreleased]

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

[Unreleased]: https://github.com/hiemalis-at/drydocc/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/hiemalis-at/drydocc/releases/tag/v0.3.0
[0.2.0]: https://github.com/hiemalis-at/drydocc/releases/tag/v0.2.0
[0.1.0]: https://github.com/hiemalis-at/drydocc/releases/tag/v0.1.0
