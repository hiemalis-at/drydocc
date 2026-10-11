# Hardening Convention

This document describes the baseline security posture of a `drydocc` node after `/bootstrap-node` completes, and the hardening state tracking mechanism.

## Baseline State (Day-1 after `/bootstrap-node`)

Every `drydocc` node, after `/bootstrap-node` succeeds, reaches the following security baseline:

### User and Access Control

- **Sudoer user `doccap`**: Unprivileged user created during bootstrap, with sudo privileges for specific operations (container management, systemd user service control, firewall rules).
- **Root SSH disabled**: Direct SSH login as `root` is forbidden (`PermitRootLogin no`), set in the node's sshd configuration (`ssh.config_include_dir` when the server has a drop-in mechanism; edited in place when that field is `null`).
- **Password SSH disabled**: Password-based SSH authentication is disabled for all users (`PasswordAuthentication no`). Only SSH public-key authentication is permitted.
- **Primary access via Tailscale**: After bootstrap, the admin connects via `tailscale ssh doccap@<tailscale_hostname>`. This is the primary access path and is secure by default (encrypted via WireGuard, authenticated via Tailscale's PKI).

### Fallback SSH Access

- **Fallback port**: A non-standard SSH port (configured in `node.yaml:ssh_fallback_port`, default `42022`) remains open for key-only access to `doccap` user. This provides a manual recovery path if Tailscale is unavailable or misconfigured.
- **Fallback is key-only**: No password auth on the fallback port; requires SSH public key in `~/.ssh/authorized_keys`.
- **Root cannot use fallback**: Only the `doccap` user can connect on the fallback port (or root via `sudo su -`).

### Firewall

The concrete tool is whichever `system_facts.firewall.tool` reports for the node
(discovered, never inferred). The baseline posture below is
tool-agnostic; `hardening-recipes.md` § Firewall holds the authoritative
per-tool commands.

- **Firewall enabled**: the active firewall tool reports enabled/running.
- **Default deny inbound**: All unsolicited inbound traffic is dropped by default.
- **Explicit inbound rules**: Only the following ports are open:
  - **`ssh_fallback_port`** (default `42022`) — Fallback SSH for `doccap`.
  - **Ports declared by active use-cases** — For each use-case with `status: active`, the ports listed in the use-case document's `ports` field are opened via the node's firewall tool. Example: `nextcloud` declares `["443/tcp", "3478/udp"]`, so a rule allows those.
  - **Tagged rules on adopted nodes** — On an adopted node, tagged rules may also exist for `status: planned` use-cases — `/onboard-node`'s adoption variant pre-allows the ports a live service already depends on, tagged with the slug that will own them once `/apply-use-case` runs. An untagged rule belongs to no use-case and is reported as drift.
  - Tailscale's `tailscale0` interface is always allowed for full Tailscale traffic.
- **Outbound**: Allowed by default (typical for server workloads).

### Intrusion Prevention

- **fail2ban enabled**: Service is running (`systemctl status fail2ban` shows active).
- **Default jails**: At minimum, an SSH jail is active with standard rules
  (ban after repeated failed attempts within a short window); see
  `hardening-recipes.md` § "fail2ban" for the exact idiom and thresholds.
- **Use-case-specific jails**: Future use-cases may declare additional jails (e.g., a web app may declare a jail against its login endpoint). No schema field exists for this yet — not yet designed.

### Rootless Podman

- **Podman installed for `doccap`**: The `doccap` user can run `podman` without root privileges (via cgroup delegation).
- **Systemd user lingering enabled**: `loginctl enable-linger doccap` ensures the user's systemd session persists even after logout, so systemd user services (quadlets) can auto-start on boot.
- **Quadlet storage**: Container/network/volume definitions live in `~/.config/containers/systemd/` (under the `doccap` user's home).

### Node Identity

- **`/etc/drydocc/node-name` written**: A file containing the node's name (matching `node.yaml:name`) is written at bootstrap. This is the authoritative identity: agents read this file to avoid cross-node operations.

### Secrets Directory

- **`/etc/drydocc/secrets/` created**: Directory owned by `doccap:doccap`, chmod 700. Env files (`*.env`) from secrets manifests are installed here with chmod 600.

## Status: Now Enforced

The `/bootstrap-node` command implements this hardening baseline. The procedure is fully defined in `skills/bootstrap-node/SKILL.md`, with execution driven by commands the agent issues from the admin's laptop and journals verbatim.

## State Tracking in `node.yaml`

The hardening state is tracked declaratively in `nodes/<node>/node.yaml` under
the `hardening:` block. Fields, enums, and per-field meaning are authoritative
in `node-profile.md` § "Field Descriptions" (the `hardening.*` entries) —
not restated here.

## Auditability and Drift Detection

Hardening drift is a **node-level** concern, so it is handled by the
`/bootstrap-node` audit-&-repair pass, not by `/audit-use-case`.
Re-running `/bootstrap-node <node>` on an already-bootstrapped node will:

1. Connect to the node as `doccap`.
2. Query live state — the firewall tool's status (probe commands in
   `hardening-recipes.md` § Firewall), fail2ban's active state
   (`hardening-recipes.md` § "fail2ban"), and root-login/password-auth via
   `ssh.effective_config_cmd` when it is set, falling back to a live
   connection attempt when that field is `null` (see `node-profile.md`).
3. Compare live state to declared state in `node.yaml:hardening`.
4. If drift is found (e.g., the firewall shows disabled but `node.yaml` says enabled):
   - Log the discrepancy.
   - Offer to re-apply the baseline idiom that establishes the field.
   - Update `node.yaml:hardening.*` to reflect discovered live state — honestly,
     including when the admin declines the fix.
   - Journal the findings with `outcome: partial` (drift detected and corrected) or `outcome: failed` (drift detected but not auto-correctable).

This ensures that manual configuration changes (e.g., an admin disables the firewall to debug a network issue but forgets to re-enable it) are detected and can be corrected or escalated.

`/onboard-node` performs the same classification when adopting an existing box;
see its `references/onboard-recipes.md` § "Hardening classification rules" for
the `aligned | drift | unknown` table both paths share.

**Not `/audit-use-case`.** That command is use-case-scoped, never
writes `node.yaml`, and never touches the node. Its only
security-adjacent checks are per-use-case: firewall rules tagged with the
use-case slug (per `system_facts.firewall.tool`) and the mode/ownership of
`/etc/drydocc/secrets/<slug>.env`.

## Future Hardening Enhancements

The baseline state above represents a minimal but solid foundation. Future passes, not implemented yet, may add:

- **Mandatory access control enforcement** — policy enforcement via the
  node's MAC framework (`system_facts.mac_framework`) for containers.
- **Automatic security updates** — unattended patching of security fixes.
- **Audit logging** — auditd for comprehensive system call logging.
- **Network segmentation** — Per-use-case network policies, zero-trust micro-segmentation.
- **Secrets rotation automation** — `/node-maintenance` reports keys past their
  declared rotation window on every sweep; performing the rotation
  automatically, end to end, is still future work owned by the `secrets` skill.
- **Compliance scanning** — CIS Benchmarks, SCAP, or custom checks.

## References

- `node.yaml` schema: see `node-profile.md`.
- Bootstrap procedure (and the node-level audit-&-repair pass): see
  `skills/bootstrap-node/SKILL.md`.
- Onboarding classification rules: see
  `skills/onboard-node/references/onboard-recipes.md`.
- Per-use-case drift: see `skills/audit-use-case/SKILL.md` — scoped to a
  use-case, not the node.
- Concrete dialects, e.g. `system_facts.firewall.tool`, are discovered per
  node, never hardcoded here; recipe sections are outcome contracts with
  mandatory verification — see
  `skills/conventions/references/distro-agnosticism.md`.
