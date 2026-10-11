# Changelog

All notable changes to `drydocc` are recorded here. Each release is one snapshot
of the framework, published to `hiemalis-at/drydocc`. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the major
version is 0, a minor release may change conventions or the command surface.

## [Unreleased]

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

[Unreleased]: https://github.com/hiemalis-at/drydocc/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/hiemalis-at/drydocc/releases/tag/v0.1.0
