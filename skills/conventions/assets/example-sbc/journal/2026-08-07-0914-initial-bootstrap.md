---
timestamp: 2026-08-07T09:14:00Z
actor: claude/opus-5
trigger: /bootstrap-node
scope: hardening
outcome: success
commit_before: 0000000
commit_after: 0000001
---

## What was done
- **Discovered facts** (probed, never inferred from `distro`):
  package manager `zypper`; service manager `systemd`; container engine
  `podman`; MAC framework `apparmor`; firewall tool `firewalld`, zone rule
  model, tags carried on a log prefix because the tool has no comment
  attribute; rootless podman verified working for `doccap`. `python3` present;
  `rsync` and `tmux` **absent**. Written to `node.yaml:system_facts`,
  `detected: 2026-08-07`.
- **Derived, verified, and recorded three idioms this repo had no recipe row
  for.** The package-install and firewall read-back commands were derived from
  the tools' own help output following the recipe's derivation guidance, each
  verified against its section's verification step before being relied on, then
  appended to the matching `### Observed dialects` tables. No step was taken on
  an unverified derivation.
- SSH server has **no drop-in config directory**, so the lockdown edited
  `sshd_config` in place; recorded as `ssh.config_include_dir: null`. The
  effective-config dump was available, so the lockdown was verified by dumping
  it rather than by connection attempt alone.
- Created `doccap` user, added to sudoers, installed the framework package set,
  set default-deny inbound with the fallback SSH port and the tailnet interface
  allowed, enabled the SSH jail, joined the tailnet, enabled lingering, wrote
  `/etc/drydocc/node-name`.

## Commands
```bash
$ command -v zypper dnf apt-get pacman apk   # -> /usr/bin/zypper
$ zypper --version                            # generation check before use
$ firewall-cmd --state                        # -> running
$ aa-status --enabled && echo "mac=apparmor"
$ grep -c "^doccap:" /etc/subuid /etc/subgid
$ podman info --format '{{.Host.Security.Rootless}}'   # -> true
$ command -v python3 rsync tmux               # rsync, tmux not found
$ test -d /etc/ssh/sshd_config.d || echo "config_include_dir=null"
```

## Notes / drift detected
None — initial bootstrap.

Two capability gaps are recorded rather than fixed, because neither blocks the
baseline:
- **`rsync` absent.** `/apply-use-case`'s adoption cut-over copies volume data
  with it. Install it before adopting anything on this node, or that step will
  fail partway through an irreversible operation.
- **`tmux` absent.** `node-exec` uses its nohup fallback for long steps here.

This node's tooling matched no pre-existing dialect row in any recipe. Nothing
in the base repo needed editing to commission it — the facts absorbed the
difference, and the three derived idioms were contributed back as evidence.
