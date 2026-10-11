---
timestamp: 2026-05-18T14:22:00Z
actor: claude/opus-4-7
trigger: /bootstrap-node
scope: hardening
outcome: success
commit_before: 0000000
commit_after: 0000001
---

## What was done
- **Discovered facts** (probed, never inferred from `distro`):
  package manager `apt`; service manager `systemd`; container engine `podman`;
  MAC framework `none`; firewall tool `ufw` (rule-list model, tags via rule
  comments); rootless podman verified working for `doccap`; `python3`, `rsync`
  and `tmux` all present. Written to `node.yaml:system_facts`, `detected:
  2026-05-18`.
- Created `doccap` user (uid 1000), added to sudoers.
- Configured sshd: `PermitRootLogin no`, `PasswordAuthentication no`,
  added `Port 42022` block keyed to `doccap` only.
- Installed `podman`, `slirp4netns`, `tailscale`, `ufw`, `fail2ban`,
  `git`.
- Configured ufw: default deny inbound, allow 42022/tcp, allow out;
  enabled.
- Configured fail2ban with default sshd jail.
- `tailscale up --hostname example-vps --ssh`; node joined the tailnet with
  Tailscale SSH enabled.
- `loginctl enable-linger doccap`.
- Wrote `/etc/drydocc/node-name=example-vps` (root, chmod 644).

## Commands
```bash
$ command -v apt-get dnf zypper pacman apk    # -> /usr/bin/apt-get
$ ufw --version                                # -> firewall tool confirmed
$ getenforce 2>/dev/null; aa-status --enabled 2>/dev/null; echo "mac=none"
$ grep -c "^doccap:" /etc/subuid /etc/subgid && podman info --format '{{.Host.Security.Rootless}}'
$ adduser --disabled-password doccap
$ usermod -aG sudo doccap
$ apt-get install -y podman slirp4netns tailscale ufw fail2ban git
$ ufw default deny incoming && ufw allow 42022/tcp && ufw enable
$ systemctl enable --now fail2ban
$ tailscale up --hostname example-vps --ssh
$ loginctl enable-linger doccap
$ echo example-vps > /etc/drydocc/node-name
```

## Notes / drift detected
None. Initial bootstrap; system matches the declared hardening baseline.
