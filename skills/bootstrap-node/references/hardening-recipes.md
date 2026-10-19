
# `drydocc` Bootstrap

Reusable bits that the `bootstrap-node` skill leans on. The orchestration
sequence lives in the skill's `SKILL.md`; this reference holds the parts shaped by
distro, by idempotency, by SSH onboarding, or by cut-over verification. VM
provisioning lives in the sibling `provision-vm` skill.

## Contents

- [Prerequisites](#prerequisites)
- [Client environment preflight (Phase 0)](#client-environment-preflight-phase-0) — admin-laptop tooling; tailscale CLI vs daemon (WSL2)
- [Connection model across phases](#connection-model-across-phases) — root-password vs doccap-key paths
- [SSH server & algorithm detection](#ssh-server--algorithm-detection)
- [Machine identity probe](#machine-identity-probe)
- [System facts discovery](#system-facts-discovery) — the Phase 2 producers for
  the `system_facts` ledger; `service_manager` is the one hard refusal gate
- [Create doccap via root password](#create-doccap-via-root-password)
- [Generate or import the doccap-at-\<self\> keypair](#generate-or-import-the-doccap-at-self-keypair)
- [Install pubkey via password](#install-pubkey-via-password)
- [Verify key + sudo (before touching sshd)](#verify-key--sudo-before-touching-sshd)
- [SSH lockdown ordering (dual-port transitional)](#ssh-lockdown-ordering-dual-port-transitional)
- [Package installation](#package-installation)
- [Firewall](#firewall) — rule tagging; fallback port; full Phase 3 hardening;
  [adoption variant](#adoption-variant-called-by-onboard-node-phase-7);
  [use-case port rules](#use-case-port-rules) (add/remove, called by the
  use-case skills)
- [fail2ban](#fail2ban)
- [Kernel hardening](#kernel-hardening) — the drydocc sysctl drop-in;
  packet forwarding derived from its consumers
- [Link-local name responders](#link-local-name-responders) — LLMNR and mDNS
  off at their owner, not only at the firewall
- [Tailscale up](#tailscale-up)
- [Lingering & identity file](#lingering--identity-file)
- [Fork write access](#fork-write-access) — per-node deploy key; clone, git identity, push verification
- [File integrity](#file-integrity) — the integrity database, initialised last;
  its hash leaves the node
- [SSH-key cut-over routine (Phase 2)](#ssh-key-cut-over-routine-phase-2)
- [Tailscale cut-over routine (Phase 4)](#tailscale-cut-over-routine-phase-4) — `tailscale ssh` with IP fallback (WSL2)
- [Idempotency / resumability](#idempotency--resumability)
- [Failure handling](#failure-handling)
- [What this skill does NOT do](#what-this-skill-does-not-do)

## Prerequisites

- Admin laptop has `ssh`, `sshpass` (for the initial root-password connection),
  `ssh-keygen`, and (for the cut-over routine) the `tailscale` CLI authenticated
  to the same tailnet the node will join.
- `nodes/<self>/node.yaml` exists (scaffolded in `/bootstrap-node` Phase 0) and
  carries at least `name`, `ssh_fallback_port`, `tailscale_hostname`,
  `doccap_user`. `distro`/`distro_version`/`ssh.server` are discovered in
  Phase 2 and written back.
- Session secrets resolve per `secrets` § "Session secrets" (`DOCCAP_TS_KEY`,
  required) via the env-var-first protocol — the keepass skill or a
  user-supplied `.env`. Fork access needs no secret from the admin: the node
  generates its own deploy key (§ "Fork write access"). The initial **root
  password** is resolved as
  `DOCCAP_ROOT_PW` per `secrets` § "Initial root password".

## Client environment preflight (Phase 0)

Run on the admin's laptop before connecting to anything. The first test run
tripped here: `sshpass` was missing on the admin's laptop, and offline disk
recovery needed `guestfish`. Detect the client OS and required tooling, then
**adapt** rather than fail late.

```bash
# client OS / distro
uname -s
[ -r /etc/os-release ] && . /etc/os-release && echo "$ID $VERSION_ID"
```

Check each tool with `command -v <tool>` and report what is missing:

| Tool | When required | If missing |
|------|---------------|------------|
| `ssh`, `ssh-keygen` | always | fatal — cannot bootstrap |
| `tailscale` | always (Phase 4 cut-over) | **absent** is fatal. Note: CLI present ≠ daemon reachable (under WSL2 `tailscaled` runs on the Windows host) — that is **not** fatal; the Phase 4 cut-over falls back to key-based SSH over the tailscale IP. |
| `sshpass` | initial root-password connection | **adapt:** use the `SSH_ASKPASS` fallback (see § "Connection model across phases"). Not fatal. |
| `@max-win-at/keepassxc-secrets` skill | preferred secret source (Phase 3) | run the `secrets` preflight, which suggests how to install it and never installs it. Not fatal — any unset secret falls back to a `.env`. |
| `virt-install`, `qemu-img`, `genisoimage`/`mkisofs`, `curl` | `source: vm` only | fatal for `source: vm`; irrelevant for `source: running` |
| `guestfish` (`libguestfs-tools`) | offline VM disk recovery | warn — recommend installing so a failed VM can be repaired in-place |

Adaptation rules:

- **`sshpass` absent** → switch the root-password connection helper to the
  `SSH_ASKPASS` + `SSH_ASKPASS_REQUIRE=force` form. Record nothing in git; this
  is a client-side runtime choice.
- **`source: vm` and virt tooling absent** → halt with the install hint for the
  client's distro (defer the qemu/libvirt *host* checks to `provision-vm` §
  "Host preflight").
- Everything else → surface a single summary of what is missing and why, then
  proceed.

## Connection model across phases

| Phase | Connect as | Auth | Port |
|-------|-----------|------|------|
| 2 (onboard, pre-lockdown) | `root` | initial password (`sshpass`) | initial (22) |
| 2 (verify) / 3+ | `doccap` | `~/.ssh/doccap-at-<self>` key | `ssh_fallback_port` |

Helper shapes (the agent substitutes real values; the password is never echoed
or logged):

```bash
# root over initial password (Phase 2 pre-lockdown) — sshpass path
sshpass -p "$DOCCAP_ROOT_PW" ssh -o StrictHostKeyChecking=accept-new \
  -p "$INITIAL_PORT" root@"$INITIAL_IP" "<cmd>"

# doccap over key (Phase 2 verify onward)
ssh -i ~/.ssh/doccap-at-"$SELF" -p "$SSH_FALLBACK_PORT" \
  -o StrictHostKeyChecking=accept-new doccap@"$INITIAL_IP" "<cmd>"
```

**`sshpass` fallback (`SSH_ASKPASS`).** When `sshpass` is absent on the admin
laptop (the client preflight detects this — some client package repos don't
carry it by default), use OpenSSH's askpass mechanism instead. Write a tiny
askpass helper into a gitignored temp dir (0700), point `SSH_ASKPASS` at it, and
force its use with `SSH_ASKPASS_REQUIRE=force` and a detached stdin (`setsid`/
`< /dev/null`):

```bash
ASKPASS="$(mktemp -d)/askpass.sh"
printf '#!/bin/sh\nexec printf %%s "$DOCCAP_ROOT_PW"\n' > "$ASKPASS"
chmod 0700 "$ASKPASS"
SSH_ASKPASS="$ASKPASS" SSH_ASKPASS_REQUIRE=force setsid -w \
  ssh -o StrictHostKeyChecking=accept-new -p "$INITIAL_PORT" \
  root@"$INITIAL_IP" "<cmd>" < /dev/null
rm -rf "$(dirname "$ASKPASS")"   # delete immediately after the connection block
```

`$DOCCAP_ROOT_PW` is exported in the agent's environment only; the helper script prints
it but is never committed and is deleted as soon as the password-auth phase ends.
This pattern was proven in the first test run, on an admin laptop without
`sshpass`.

## SSH server & algorithm detection

Run over the root connection. Decide `ssh.server` and the key algorithm to use.

```bash
# What SSH server is listening?
( ssh -V 2>&1 | grep -qi openssh && echo openssh ) \
  || ( command -v dropbear >/dev/null 2>&1 && echo dropbear ) \
  || ( ps -e 2>/dev/null | grep -q dropbear && echo dropbear ) \
  || echo unknown
```

Algorithm negotiation:

- **openssh** → does it advertise ed25519? `ssh -Q key 2>/dev/null | grep -q
  ssh-ed25519` → use `ed25519`. Otherwise fall back to `rsa-4096`.
- **dropbear** → many builds reject ed25519; default to `rsa-4096` unless the
  build is known-recent.
- **unknown** → default `rsa-4096` (widest compatibility).

Record the result in `node.yaml:ssh.server` / `ssh.key_algorithm`.

## Machine identity probe

Run over the root connection, during OS discovery — this is `node.yaml:identity`,
the box's own identity, not the admin's client key recorded above.
Detect and branch rather than assume one location exists, the same rule this
file follows for package managers:

```bash
# Machine id — prefer the systemd location, fall back to the D-Bus one, else unknown
MID_RAW="$( (cat /etc/machine-id 2>/dev/null || cat /var/lib/dbus/machine-id 2>/dev/null) | tr -d '[:space:]')"
if [ -n "$MID_RAW" ]; then
  MACHINE_ID_HASH="sha256:$(printf '%s' "$MID_RAW" | sha256sum | awk '{print $1}')"
else
  MACHINE_ID_HASH="unknown"
fi
unset MID_RAW   # never let the raw id linger in a variable longer than needed

# Host key — enumerate what sshd actually offers, prefer ed25519 when present
HOST_KEY_PUB="$(ls /etc/ssh/ssh_host_ed25519_key.pub 2>/dev/null \
  || ls /etc/ssh/ssh_host_*_key.pub 2>/dev/null | head -1)"
if [ -n "$HOST_KEY_PUB" ]; then
  HOST_KEY_FINGERPRINT="$(ssh-keygen -lf "$HOST_KEY_PUB" | awk '{print $2}')"
else
  HOST_KEY_FINGERPRINT="unknown"
fi
```

**Never emit or journal the raw machine id** — only `$MACHINE_ID_HASH`, which
already carries the `sha256:` prefix the schema declares. Record the result in
`node.yaml:identity.machine_id_hash` / `identity.host_key_fingerprint`. Write
`identity.first_seen` only when the block is not already present on the node —
it is set once, never rewritten by a later re-run.

## System facts discovery

Run over the root connection in **Phase 2**, immediately after the machine
identity probe. This section exists because the facts below are needed *before*
Phase 3 can act: `/bootstrap-node` step 14 writes the whole `system_facts`
ledger, and a fact whose only detection lived inside the Phase 3 step that
consumes it would be required before it could be produced. `package_manager`
has its own detection under § "Package installation"; the rest are here.

**Goal.** Every key in `node.yaml:system_facts` holds a value that came from
asking *this box*, or the explicit value `unknown`. No key is inferred from
`distro`, and no key is left absent while a later phase assumes a default.

**Fact.** These are the producers, not consumers. Probe each independently —
they do not imply one another. A box can run one init system with another's
tooling installed, or ship a MAC framework that is present but not enforcing.

- **`service_manager`** — the **one hard capability gate** (quadlets
  are systemd). Ask PID 1 what it is rather than testing for a binary, since a
  client tool can be installed without that init running. If the answer is not
  systemd, **refuse here** with the reason and the failing check, per
  `conventions` skill's `references/distro-agnosticism.md` Rule 4 — do not
  continue into Phase 3 and fail later on a user unit.
- **`container_engine`** — `command -v` across candidates, then ask the hit for
  its own version to confirm it runs. Record what is *there*; the framework
  converges on podman, but `/onboard-node` can meet a box already running
  another engine and the fact must say so honestly.
- **`mac_framework`** — distinguish **installed** from **enforcing**: ask the
  framework's own status command for its mode. A framework in a permissive or
  disabled mode is recorded as its name only if it is actually enforcing;
  otherwise `none`. Consumers branch on this to decide whether a relabel or a
  port label is needed at all, so a false positive costs a wasted step and a
  false negative costs a broken service.
- **`firewall.{tool,rule_model,tag_mechanism}`** — see § "Firewall" for the
  full derivation, including how to recognise a tag mechanism on a tool nobody
  has written down. Probe it *here* so Phase 3 has it; that section explains
  how to interpret what you find.
- **`rootless_ready`** — a **verdict from a live check, never a deduction**
  from its ingredients. Both must hold for `doccap_user`: subordinate uid/gid
  ranges are allocated to it, and the user's own manager can actually start a
  delegated unit. Ask the container engine itself whether it considers rootless
  operation available for that user — it already performs this check and will
  name what is missing. The entire use-case surface rests on this being true,
  so record `false` loudly rather than optimistically.
- **`capabilities.{python3,rsync,tmux}`** — presence only, via `command -v`.
  Each is load-bearing somewhere and none may be assumed: `python3` backs
  inline probe parsing, `rsync` backs `/apply-use-case`'s **irreversible**
  adoption cut-over, `tmux` backs `node-exec`'s lingering contract (which has
  a documented fallback when it is absent). An **absent key means never
  probed**, which is not the same as `absent` — write the key.

**Deriving an unseen dialect.** Each probe above asks a *component about
itself* rather than matching a name, so an unrecognised answer is a value to
record, not a failure: write it verbatim and journal it. The candidate lists
are seeds, not closed sets (`references/distro-agnosticism.md` Rule 5). When a
probe cannot answer at all, `unknown` is always legal and always better than a
guess — every consumer in this repo is written to degrade on `unknown`, and
none is written to survive a confidently wrong value.

**Verification.** Re-read the block back from `node.yaml` and confirm every key
in the schema is present with either a probed value or `unknown` — a silently
missing key is the failure mode this section exists to prevent. Confirm
`service_manager` is systemd, or that the run refused. Where a probe returned a
value, the component it names must answer a second, different question to prove
the first answer was not an artefact of the probe itself.

**Record.** All of the above plus `system_facts.detected: <today>`, in
`node.yaml`, journaled in the same commit — see `conventions` skill's
`references/node-profile.md` and `references/journal.md`. In audit-&-repair
mode, compare against the existing block and **warn on drift** rather than
overwriting: a box whose tooling changed generation since the last run is
exactly what this step exists to catch.

## Create doccap via root password

Run over the root connection.

**Goal.** `doccap` exists with a home directory and an interactive login
shell, and can run any command via `sudo` without a password (this is a
framework design choice, not a discovered dialect: there is no
password-sudo variant in the unified flow).

**Fact.** None. User creation is a one-shot action at bootstrap time, never
re-read afterward, so it doesn't earn a `system_facts` slot (`conventions`
skill's `references/distro-agnosticism.md` Rule 6 — rejected candidates). Any
user-management tool that can create a POSIX account with a home directory
and a shell satisfies the goal; there is no dialect *value* to select on.

**Deriving an unseen idiom.** Prefer whichever user-creation tool is present
that creates the home directory and sets the shell in the same invocation.
`command -v` the candidates and use the first that supports both; if a tool
only creates the bare account, follow up with whatever separate step fills in
the home directory and shell rather than accepting a partial result silently.
Granting sudo is not part of this choice — write the `NOPASSWD` sudoers.d
drop-in below regardless of which tool created the account; `#includedir
/etc/sudoers.d` support is standard, not a dialect.

```bash
id doccap &>/dev/null || <confirmed user-creation idiom — see Observed dialects> doccap
# Check the CONTENT, not mere existence: an interrupted write (a power cut,
# 2026-10 lab run) leaves an empty file that an existence check accepts,
# silently leaving doccap without sudo. Write atomically: sudo ignores
# sudoers.d names containing a dot, so the temp file is never live, and mv
# replaces the real one in a single step.
LINE='doccap ALL=(ALL) NOPASSWD: ALL'
grep -qxF "$LINE" /etc/sudoers.d/doccap 2>/dev/null \
  || { printf '%s\n' "$LINE" | install -m 0440 /dev/stdin /etc/sudoers.d/.doccap.tmp \
       && visudo -cf /etc/sudoers.d/.doccap.tmp \
       && mv -f /etc/sudoers.d/.doccap.tmp /etc/sudoers.d/doccap; }
```

**Verification.** `id doccap` reports a uid; `getent passwd doccap` shows a
home directory and the intended shell; `sudo -n true` succeeds as `doccap`
once its key is installed (§ "Verify key + sudo" already exercises this —
re-run standalone only when verifying this step in isolation).

**Record.** Journal the command used. No `system_facts` write (Rule 6, above).

#### Observed dialects — evidence, not the supported list
| idiom | verified on | date |
|---|---|---|
| `useradd --create-home --shell /bin/bash doccap` | test-01/test-02 (Fedora 44 VM) | 2026-06 |
| `adduser --disabled-password doccap` | `conventions/assets/example-vps` illustrative journal | 2026-05 |

## Generate or import the doccap-at-<self> keypair

**Generate** (on the admin laptop — see `secrets` § "SSH key storage"
for where the private key is stored):

```bash
# ed25519
ssh-keygen -t ed25519 -N "" -C "doccap-at-$SELF" -f ~/.ssh/doccap-at-"$SELF"
# rsa-4096 fallback
ssh-keygen -t rsa -b 4096 -N "" -C "doccap-at-$SELF" -f ~/.ssh/doccap-at-"$SELF"

# fingerprint for node.yaml:ssh.public_key_fingerprint
ssh-keygen -lf ~/.ssh/doccap-at-"$SELF".pub | awk '{print $2}'
```

**Import** (admin provides a key via a KeePassXC ssh-agent): list the agent's
keys, let the admin pick one, capture its public line + fingerprint. `key_source`
becomes `keepass-agent`; nothing is written to `~/.ssh`.

The **public** key string (`PUBKEY`) is what gets installed on the node either way.

## Install pubkey via password

Run over the root connection:

```bash
sshpass -p "$DOCCAP_ROOT_PW" ssh -p "$INITIAL_PORT" root@"$INITIAL_IP" \
  "install -d -m 0700 -o doccap -g doccap /home/doccap/.ssh && \
   grep -qxF '$PUBKEY' /home/doccap/.ssh/authorized_keys 2>/dev/null \
   || (echo '$PUBKEY' >> /home/doccap/.ssh/authorized_keys \
       && chown doccap:doccap /home/doccap/.ssh/authorized_keys \
       && chmod 0600 /home/doccap/.ssh/authorized_keys)"
```

## Verify key + sudo (before touching sshd)

```bash
ssh -i ~/.ssh/doccap-at-"$SELF" -p "$INITIAL_PORT" \
  -o StrictHostKeyChecking=accept-new doccap@"$INITIAL_IP" 'sudo -n true && echo ok'
```

Must print `ok`. **Do not proceed to lockdown if this fails** — without a
working key you would lock yourself out.

## SSH lockdown ordering (dual-port transitional)

Only after the key+sudo verification passes. The first test run locked the box
out of **both** ports: moving sshd to `${SSH_FALLBACK_PORT}` in one step, before
the node's MAC framework and firewall allowed the new port, killed sshd on
reload and dropped port 22 too. The routine below makes that impossible by
**labelling/opening the new port first** and **listening on both ports** until
the key is proven on the new one. Throughout, the live root session on port 22
is the safety net, and we **reload** (never restart) so it survives.

Two `node.yaml` facts decide how Steps 3, 5 and 6 write and verify the config,
both discovered during OS probing, never assumed here: `ssh.config_include_dir`
(where a drop-in fragment goes — **`null` means the server has no drop-in
mechanism**, and the main config is edited in place instead) and
`ssh.effective_config_cmd` (the command that dumps the server's effective
configuration — **`null` means the server can't report one**, and verification
falls back to a live connection attempt only).

**Step 1 — permit the port under the node's MAC framework, if it enforces
one.** Some MAC frameworks refuse to let a service bind a port that isn't
explicitly labelled for it, and sshd then fails to reload instead of falling
back to the ports it already had — see Observed dialects below for the
framework and command this file already hit in the wild.

**Fact.** `system_facts.mac_framework`. When unknown, probe with each
candidate's own purpose-built status query — *enforcing*, never merely
installed — the same candidate-loop shape `onboard-node/references/
onboard-recipes.md` § "Inventory probes" uses. Record `none` only after every
candidate tried came back not-enforcing; record `unknown` when there was no
candidate to try at all — don't collapse the two, an admin acts on them
differently.

**Deriving an unseen framework.** Ask whether binding a new port under this
framework requires an explicit grant before sshd can use it. If the framework
ships a port-labelling tool, run it for `${SSH_FALLBACK_PORT}` tagged as the
ssh port type, treating "already defined" as success (idempotent). If the
framework doesn't restrict port binding this way — true for most values,
including `none` and `unknown` — there is nothing to do here; skip to Step 2.
If you can't tell either way, don't guess by skipping: Step 3's reload will
fail loudly and safely (the root session on port 22 survives — see §
"Failure handling"), and that failure is itself the answer.

```bash
# over the root connection — run only when Step 1's Fact says this framework enforces it
<framework's own port-label idiom for ${SSH_FALLBACK_PORT} — see Observed dialects>
```

#### Observed dialects — evidence, not the supported list
| mac_framework | command | verified on | date |
|---|---|---|---|
| selinux | `sudo dnf install -y policycoreutils-python-utils; sudo semanage port -a -t ssh_port_t -p tcp "${SSH_FALLBACK_PORT}" 2>/dev/null \|\| sudo semanage port -m -t ssh_port_t -p tcp "${SSH_FALLBACK_PORT}" 2>/dev/null \|\| true` | test-01/test-02 (Fedora 44 VM) | 2026-06 |

**Step 2 — open the firewall for the new port now**, before the move (see §
"Firewall" → "Open the fallback SSH port"), using whichever tool
`system_facts.firewall.tool` names. Some tools are default-deny for
unsolicited inbound out of the box and would otherwise block the new port
immediately; others ship inactive on a fresh box, where opening early is
simply harmless and idempotent. Either way this step never waits to find out
which — it opens the port unconditionally.

**Step 3 — write the config listening on BOTH ports**, root/password off.

"Password off" means **both** password routes are off. `PasswordAuthentication
no` closes only the plain password method. A server that uses PAM still asks
for the same password through keyboard-interactive unless
`KbdInteractiveAuthentication no` closes that too, and with that route open,
`hardening.ssh_password_auth: disabled` would be false. Older OpenSSH releases
call the keyword `ChallengeResponseAuthentication` and report it under that
name in their effective-config dump. Write whichever name `sshd -t` accepts on
this server, and check the name the dump actually prints.

Every form of the config below also carries the **session policy**
(`hardening.ssh_session_policy`): empty passwords refused, X11 forwarding
off, public-key authentication on, and dead peers dropped by keepalive
probes, every 300 s and after 3 unanswered probes. That ends a session whose
client vanished. An idle client that still answers stays connected, because
Remote-SSH and `node-exec` sessions sit idle on purpose. **Never set the
probe count to `0` to get an idle timeout**: on current OpenSSH that value
turns the check off. The keywords are OpenSSH's. For another `ssh.server`,
derive the same five outcomes from that server's own options, or record
`ssh_session_policy: unknown` when it has no equivalent. Never write
keywords the server does not parse, because `sshd -t` (or its equivalent)
rejects the whole file and the reload fails.

When `ssh.config_include_dir` is not `null`, (over)write a drop-in fragment
there — the include mechanism was already confirmed present when this fact
was probed, so auto-loading it on reload is safe to assume in this branch.

The server takes the **first** value it reads for most settings, and reads
drop-ins in name order — so the fragment must sort **before** every other
one, or an earlier fragment silently wins. Two have been seen winning: the
initial-access fragment provisioning writes (`00-initial.conf`,
`PermitRootLogin yes`, 2026-10 lab run — root login stayed on after a
lockdown that "succeeded"), and a cloud image's own fragment re-enabling
password login. Hence the `00-` name; and the initial-access fragment is
drydocc's own, so it is retired here, together with the fragment's earlier
unprefixed name:

```bash
# over the root connection — ssh.config_include_dir not null
rm -f "${SSH_CONFIG_INCLUDE_DIR}/00-initial.conf" "${SSH_CONFIG_INCLUDE_DIR}/drydocc.conf"
cat > "${SSH_CONFIG_INCLUDE_DIR}/00-drydocc.conf" <<EOF
# Written by /bootstrap-node — managed by drydocc (transitional: both ports)
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PubkeyAuthentication yes
X11Forwarding no
ClientAliveInterval 300
ClientAliveCountMax 3
Port 22
Port ${SSH_FALLBACK_PORT}
EOF
sshd -t && systemctl reload sshd   # validate config, then reload
```

When `ssh.config_include_dir` is `null`, the server has no drop-in mechanism —
edit the main config in place instead, confined to a marker-delimited block so
a re-run replaces only what drydocc owns and never touches admin-authored
lines above or below it:

```bash
# over the root connection — ssh.config_include_dir is null
MARK_BEGIN='# >>> drydocc managed (transitional: both ports) >>>'
MARK_END='# <<< drydocc managed <<<'
awk -v b="$MARK_BEGIN" -v e="$MARK_END" \
  'BEGIN{skip=0} $0==b{skip=1} $0==e{skip=0; next} !skip{print}' \
  "${SSHD_MAIN_CONFIG}" > "${SSHD_MAIN_CONFIG}.new"
{
  cat "${SSHD_MAIN_CONFIG}.new"
  printf '%s\nPermitRootLogin no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitEmptyPasswords no\nPubkeyAuthentication yes\nX11Forwarding no\nClientAliveInterval 300\nClientAliveCountMax 3\nPort 22\nPort %s\n%s\n' \
    "$MARK_BEGIN" "${SSH_FALLBACK_PORT}" "$MARK_END"
} > "${SSHD_MAIN_CONFIG}"
rm -f "${SSHD_MAIN_CONFIG}.new"
sshd -t && systemctl reload sshd
```

`${SSHD_MAIN_CONFIG}` is whatever the server itself reports as its config file
— prefer asking it (its effective-config dump, when `ssh.effective_config_cmd`
is not `null`) over assuming a path. This edit-in-place form has not yet run
against a live node in this repo's test history: every server bootstrapped so
far had a drop-in directory. Treat it as a derivation, not an Observed
dialect, until it has been run and verified once — then append it below.

**Step 4 — verify the doccap key on `${SSH_FALLBACK_PORT}`** (see "SSH-key
cut-over routine"). When `ssh.effective_config_cmd` is not `null`, also dump
the effective config and confirm it echoes what was just written — a config
that fails to load silently keeps serving the *previous* one, which a bare
connection check alone cannot distinguish from success:

```bash
# only when ssh.effective_config_cmd is not null — EVERY line must be there:
# matching any one of them would pass with root login still on
EFFECTIVE="$(sudo sh -c "${SSH_EFFECTIVE_CONFIG_CMD}")"
for want in "permitrootlogin no" "passwordauthentication no" "kbdinteractiveauthentication no" \
            "permitemptypasswords no" "pubkeyauthentication yes" "x11forwarding no" \
            "clientaliveinterval 300" "clientalivecountmax 3" \
            "port 22" "port ${SSH_FALLBACK_PORT}"; do
  printf '%s\n' "$EFFECTIVE" | grep -qix "$want" || { echo "effective config lacks: $want"; exit 1; }
done
```

A missing line with the fragment in place means another fragment or the
main config won — find it in the include directory (or the main config)
and resolve it with the admin; never weaken the check to pass.

When `ssh.effective_config_cmd` is `null`, the live connection attempt above
**is** the entire verification — there is nothing else to ask the server,
which is exactly what a `null` value means. Either way: if verification
fails, the box is still fully reachable on port 22 — fix the cause (MAC
framework label? firewall?) and re-run; do not proceed.

**Step 5 — drop port 22.** Only once the key is proven on the new port,
rewrite the config to the final single-port form using the same branch as
Step 3 (drop-in overwrite, or the same marker-delimited edit-in-place with the
block body holding the lockdown and session-policy lines and only
`Port ${SSH_FALLBACK_PORT}`), and reload:

```bash
# over the root connection — ssh.config_include_dir not null
cat > "${SSH_CONFIG_INCLUDE_DIR}/00-drydocc.conf" <<EOF
# Written by /bootstrap-node — managed by drydocc
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PubkeyAuthentication yes
X11Forwarding no
ClientAliveInterval 300
ClientAliveCountMax 3
Port ${SSH_FALLBACK_PORT}
EOF
sshd -t && systemctl reload sshd
```

**Step 6 — re-verify** the doccap key on `${SSH_FALLBACK_PORT}` once more, the
same way as Step 4 — with the effective-config loop now expecting
`port ${SSH_FALLBACK_PORT}` and **no** `port 22` line. The content of both config forms is fixed, so re-writing
either is idempotent. The initial root session stays usable until the admin
closes it.

**Step 7 — retire root's authorized keys.** Only after Step 6 passes, so
root login is already refused and `doccap`'s key is proven. Provisioning, a
cloud image or a hosting provider often leaves keys for `root`. With root
login off they serve nobody, but they are one config edit away from working
again. Move the file aside rather than deleting it: the admin may need to
know whose keys they were.

```bash
# over the doccap session, with sudo
for f in $(sudo sh -c "${SSH_EFFECTIVE_CONFIG_CMD}" 2>/dev/null \
             | awk '$1=="authorizedkeysfile"{for(i=2;i<=NF;i++) print $i}'); do
  # expand the server's %h / %u tokens for root; skip a relative path's base
  p="$(printf '%s' "$f" | sed -e 's#%h#/root#g' -e 's#%u#root#g' -e 's#%%#%#g')"
  case "$p" in /*) ;; *) p="/root/$p" ;; esac
  sudo test -s "$p" || continue
  sudo ssh-keygen -lf "$p"                      # fingerprints → the journal
  sudo mv "$p" "$p.drydocc-retired-$(date -u +%Y%m%d%H%M%S)"
done
```

When `ssh.effective_config_cmd` is `null`, use the server's documented
default location, `~/.ssh/authorized_keys`, and say in the journal that the
list of locations was assumed, not asked. A retired file keeps its mode and
owner. A re-run finds nothing left to move, which makes it idempotent.

**Verification.** No location the server reads for root holds a non-empty
file. Re-run the loop's `test -s` alone, and expect no hit.

**Record.** `hardening.ssh_root_login: disabled` once Step 6 passes and
Step 7 has left root no key. Also `hardening.ssh_password_auth: disabled`,
and `hardening.ssh_session_policy: enabled` once the Step 6 loop passes. The
journal names each retired file and its key fingerprints, never the keys. When
`ssh.effective_config_cmd` is `null`, the connection proves only that key
login works, not the session policy, so record `ssh_session_policy: unknown`
and say so in the journal.

**Already-bootstrapped node.** An audit-&-repair run on a node locked down
before the session-policy lines existed finds them missing. It rewrites the
final single-port form directly (Step 5) and verifies it (Step 6). Steps 1–4
exist only to move the port safely, and the port is already moved. Step 7
runs as well, because a node locked down before Step 7 existed may still
hold keys for root.

## Package installation

One command that installs the full stack idempotently. Run as `doccap` with
`sudo` over the key (Phase 3).

**Goal.** podman (with its rootless networking helper), tailscale, git,
ca-certificates, fail2ban, tmux (backs `node-exec` lingering), rsync (backs
volume backup and restore, and adoption's data migration — every later skill
treats it as baseline), a file-integrity tool (backs § "File integrity"; the
AIDE family is the seed candidate, resolved per step 2 of the derivation
below), and the node's firewall tool (`system_facts.firewall.tool`) are installed and
importable from a live package repository, idempotently — re-running installs
nothing twice.

**Fact.** `system_facts.package_manager`. When unknown, probe: `command -v`
across candidates, confirm the first hit is *actually managing this install*
— ask it something only the manager in charge can answer (its own package
count, or a lookup of a package already on the box), never presence alone; a
rescue or secondary manager can be co-installed without being the one running
the system. Then, for the confirmed manager, check whether its own CLI has
shipped more than one generation and probe which — see Observed dialects
below for the exact split this file already hit once in the wild, including
the confirm commands that tell the generations apart. **This is the pattern
every fact in this file follows: probe a tool's own generation marker from
its own version/help output, never infer it from `distro_version`** — a
package manager's own releases drift independently of the distro's, and a
hardcoded single-generation assumption here once silently failed on a live
node.

**Deriving an unseen dialect.** Grow the candidate list rather than treating
it as closed. For a manager not yet in Observed dialects:
1. Confirm it per the Fact paragraph above.
2. Resolve each framework package's **name** for that manager — most names
   are stable across ecosystems (`git`, `podman`, `tailscale`), but when the
   obvious name 404s, search the manager's own package index before assuming
   the package doesn't exist under any name.
3. If installing `tailscale` needs a third-party repository the base install
   doesn't carry, add it the way the confirmed manager's own documentation
   calls its supported form (a keyring + source-list file, or a single repo
   descriptor file — whichever that manager uses) before installing.
4. If the confirmed manager exposes more than one CLI generation (step above),
   let the **generation value**, not the binary name, be what gets recorded to
   `system_facts.package_manager`.
There is no fallback when nothing on the box manages packages at all — refuse
and say so in the journal; a box with no package manager cannot reach the
baseline, and that is a capability gap, not a distro to name (`conventions`
skill's `references/distro-agnosticism.md` Rule 4).

**Verification.** Every package in the goal resolves after the install
command exits zero: `command -v podman tailscale git fail2ban-client tmux rsync`,
plus whatever binary/unit `system_facts.firewall.tool` names and the
integrity tool's own binary (`aide` for the seed candidate). A manager
reporting success is not sufficient by itself — a partially-applied install
can still exit zero; re-run the presence check before trusting it.

**Record.** `system_facts.package_manager` (the confirmed generation value,
not necessarily the binary name it was probed through) and `.detected:
<today>`, journaled — see `conventions` skill's `references/node-profile.md`
and `references/journal.md`. Node commissioning is the first and only point a
concrete dialect becomes relevant; this file only encodes *how to detect and
branch*, never a single hardcoded current syntax.

#### Observed dialects — evidence, not the supported list

`tmux` and `rsync` joined the goal after the rows below were last written;
fold them into the same install invocation, and update a row once reverified.
`tmux` has been: appended to the dnf5 row's install command, it installed
and resolved on a Fedora 44 lab node (2026-10). `rsync` was found missing
from the baseline by a lab backup run on that same node — a minimal install
does not ship it. The file-integrity tool joined the goal later still. All three
are now in the dnf5 row, verified in a lab bootstrap (2026-10-14). The other
rows still lack them.

| package_manager | confirm command | install command | verified on | date |
|---|---|---|---|---|
| apt | `test -d /var/lib/dpkg && apt list --installed >/dev/null 2>&1 && echo ok` | `sudo DEBIAN_FRONTEND=noninteractive apt-get update && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y podman slirp4netns tailscale ufw fail2ban git ca-certificates` (add the Tailscale repo first if `apt-cache policy tailscale` shows no candidate — see below) | not independently verified in this repo's test history — carried from this file's earlier single-dialect version | — |
| dnf | `dnf --version 2>/dev/null \| head -1 \| grep -qvi '^Dnf5' && dnf --version >/dev/null 2>&1 && echo ok` | `sudo dnf -y install dnf-plugins-core; sudo dnf config-manager --add-repo https://pkgs.tailscale.com/stable/fedora/tailscale.repo; sudo dnf -y install podman slirp4netns tailscale firewalld fail2ban git ca-certificates; sudo systemctl enable --now tailscaled` | not independently verified — Fedora 44 (test-01/test-02) shipped dnf5, so only the dnf5 row below was actually exercised; this classic form is retained for pre-dnf5 Fedora (<41) | — |
| dnf5 | `dnf --version 2>/dev/null \| head -1 \| grep -qi '^Dnf5' && echo ok` | `sudo dnf config-manager addrepo --from-repofile=https://pkgs.tailscale.com/stable/fedora/tailscale.repo; sudo dnf -y install podman slirp4netns tailscale firewalld fail2ban git ca-certificates tmux rsync aide; sudo systemctl enable --now tailscaled` | test-01/test-02 (Fedora 44 VM) — the fix for an earlier dnf4-only assumption that failed on these nodes; `tmux rsync aide` added and verified in a lab bootstrap, Fedora 44 x86_64 | 2026-06, 2026-10-14 |

**apt's Tailscale repository add** (only when the base install has no
candidate):
```bash
curl -fsSL https://pkgs.tailscale.com/stable/debian/$(. /etc/os-release && echo $VERSION_CODENAME).noarmor.gpg \
  | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
curl -fsSL https://pkgs.tailscale.com/stable/debian/$(. /etc/os-release && echo $VERSION_CODENAME).tailscale-keyring.list \
  | sudo tee /etc/apt/sources.list.d/tailscale.list
sudo apt-get update
```
(swap `debian`→`ubuntu` in both URLs on an Ubuntu box.) Not independently
verified in this repo's test history — same provenance as the apt row above.

## Firewall

**Goal.** Traffic the framework needs — SSH on the fallback port, the
tailnet, and each use-case's declared ports — stays reachable; everything
else is denied by default; and every rule the framework itself added can be
identified and later removed without disturbing anything else.

**Fact.** `system_facts.firewall.{tool, rule_model, tag_mechanism}`
(`conventions` skill's `references/node-profile.md`). When unknown, probe:
`command -v` across candidates, then confirm each is *managing the live
ruleset* — a tool that is installed but inactive is not the answer. On a
genuinely fresh box where nothing is filtering yet, the framework brings one
up itself (§ "Full hardening" below) rather than guessing which the box
"should" have. Record `tool: none` and say so in the journal if nothing is
filtering and nothing gets installed.

**Deriving an unseen tool.** `rule_model` is either a flat rule list (a rule
you add and remove by its own spec) or a zone/ruleset shape (a rule that
first has to belong to a named group — and, structurally, an interface can be
assigned straight to that group without ever becoming a "rule" in its own
right; see § "Full hardening" below, where the tailnet allow takes one shape
or the other depending on `rule_model`). Classify by reading the tool's own
rule-listing output: a flat numbered or ordered list is `rule-list`; grouped
output under named zones/tables is `zone` or `ruleset`, whichever the tool
itself calls its grouping unit. `tag_mechanism` is derived in § "Rule
tagging" below. This file has so far only met a rule-list-shaped tool and a
zone-shaped one (see Observed dialects throughout this section) — a
ruleset-shaped tool is legal per the schema but nothing here has derived one
yet; do not invent its command forms, derive them fresh against the live tool
the same way this section already did twice.

**Verification.** Each subsection below states its own — "reachable" and
"denied by default" are two different live checks, so there is no single
verification for the section as a whole. Never proceed past a rule change
that hasn't been re-confirmed from where it matters: off-node for an allow,
on-node for a deny.

**Record.** `system_facts.firewall.*`, the journal, and each subsection's own
Observed dialects table.

### Rule tagging (read this before any rule below)

Every rule the framework adds carries a **tag**, so `/audit-use-case` and
`/remove-use-case` can identify exactly the rules a given use-case owns and
leave everything else alone.

**Goal.** Every rule the framework writes, baseline or use-case, can be
identified by tool-appropriate means as belonging to the framework — the
shared precondition every rule-writing step below depends on.

**Fact.** `system_facts.firewall.tag_mechanism` — probed together with
`tool`/`rule_model` per § "Firewall" above.

**Deriving an unseen tool's tag mechanism.** Check the tool's own help output
for a comment or description attribute first — that makes `tag_mechanism:
comment`. If it has none, look for its nearest other free-text carrier; the
zone-shaped tool on record here has no comment attribute at all but does
expose one indirectly (see Observed dialects below), which makes
`tag_mechanism: log-prefix` and means **every** rule the framework writes for
that tool must go through whichever rule form carries that field — never the
tool's plainest add-rule form — so that every rule stays taggable and
readable through one command. If the carrying field can flood a log, cap it
with whatever rate-limit option the tool offers on that same field. If no
free-text carrier exists at all, record `tag_mechanism: none` and stop — do
not invent a silent convention `/remove-use-case` cannot read back.

Two exceptions, structural rather than tool-specific, and likely to recur for
any tool this file hasn't met yet:
- **Interface allows often cannot carry a tag.** A rule matching an interface
  rather than a port may not accept free text in the same rule form that
  carries the tag elsewhere; leave it untagged and note it — the tailnet
  allow in § "Full hardening" is the case on record.
- **Pre-existing baseline rules keep their original wording.** Retagging a
  rule on a live node orphans the old one and can drop the connection you are
  using to make the change.

Tag values: baseline rules use `drydocc-<purpose>`; **use-case rules use the
bare `<slug>`**, which is what `audit-recipes.md`'s probe greps for verbatim
— keep both forms exactly as recorded in Observed dialects below.

> Validate a new tool's tag-carrying rule form on a live node the first time
> it runs — confirm a malformed rule is rejected for that tool at add time,
> never silently ignored, before trusting it.

**Verification.** Not standalone — each subsection below verifies its own
rule's tag via this section's read-back column.

**Record.** Documented here for every subsection to reuse; no separate write
of its own.

#### Observed dialects — evidence, not the supported list
| tool | rule_model | tag_mechanism | add form carries the tag via | read back with |
|---|---|---|---|---|
| ufw | rule-list | comment | `comment '<tag>'` appended to the rule | `ufw status numbered` |
| firewalld | zone | log-prefix | `log prefix="<tag> " level="info" limit value="1/m"` inside a **rich rule** (firewalld's rich-rule language has no comment attribute at all — `log prefix` is its only free-text field, so every framework firewalld rule is a rich rule, not `--add-port`) | `firewall-cmd --list-rich-rules` |

Baseline-rule tag text observed in the wild does not actually match the
`drydocc-<purpose>` form above for one tool: ufw's baseline comments were
written `drydocc fallback ssh` / `drydocc tailnet` (space-separated, no
`<purpose>` interpolation), while firewalld's log-prefix used the hyphenated
form (`drydocc-fallback-ssh`). Both are reproduced verbatim in this section's
commands rather than silently reconciled — this is a real pre-existing
inconsistency, not a typo introduced here; fix it deliberately (updating both
the live rule and this doc together) rather than letting a future edit drift
one without the other.

### Open the fallback SSH port (called from lockdown Step 2)

Before the port move (lockdown Step 2) the new port must already be allowed,
or the move locks the box out. This is the *minimal* allow rule, run as
`doccap` with `sudo` (or over the root connection if invoked pre-`doccap` —
both work). It does **not** enable default-deny yet; that is the full
hardening below.

**Goal.** `${SSH_FALLBACK_PORT}/tcp` is allowed through whatever
`system_facts.firewall.tool` names, tagged per § "Rule tagging", before
lockdown Step 3 moves sshd onto it.

**Fact / Deriving.** Reuse `system_facts.firewall.{tool,rule_model,
tag_mechanism}` from § "Firewall" above — this step never re-derives it. Add
the rule using the confirmed tool's tag-carrying form (§ "Rule tagging"). If
the tool ships inactive by default, bringing it fully up is not this step's
job — only that the allow rule exists so the coming port move isn't blocked.
If the tool is active-by-default for unsolicited inbound, this is the rule
that matters most: without it the move fails exactly as if the tool had never
been opened at all.

**Verification.** The rule read-back (§ "Rule tagging") names the port and
the tag. This step cannot verify *reachability* yet — sshd isn't listening on
the new port until lockdown Step 3 — so the load-bearing proof is lockdown
Step 4's live connection attempt; this step only confirms the rule itself
took.

**Record.** Journal the command; no new fact (already recorded in § "Firewall").

#### Observed dialects — evidence, not the supported list
| tool | command | verified on | date |
|---|---|---|---|
| ufw | `sudo ufw allow ${SSH_FALLBACK_PORT}/tcp comment 'drydocc fallback ssh'` — adding a rule does not itself enable the firewall | test-01 / test-02 | 2026-06 |
| firewalld | active by default, so this is the one that matters: `sudo systemctl enable --now firewalld && sudo firewall-cmd --permanent --add-rich-rule="rule port port=\"${SSH_FALLBACK_PORT}\" protocol=\"tcp\" log prefix=\"drydocc-fallback-ssh \" level=\"info\" limit value=\"1/m\" accept"; sudo firewall-cmd --permanent --remove-port=${SSH_FALLBACK_PORT}/tcp 2>/dev/null` (the `--remove-port` migrates a node bootstrapped before rules were tagged — no-op once the legacy entry is gone) `; sudo firewall-cmd --reload` | test-01 / test-02 | 2026-06 |

Both rows are idempotent (re-adding an existing rule is a no-op).

### Full hardening (Phase 3)

The fallback-port allow above is a subset of this; re-running it here is
safe. This runs only on a fresh box (`/bootstrap-node`) — an already-adopted
box uses § "Adoption variant" instead, which never resets anything.

**Goal.** Default-deny for unsolicited inbound, with `${SSH_FALLBACK_PORT}`
and the tailnet interface allowed — the framework's baseline posture.

**Fact / Deriving.** Reuse `system_facts.firewall.{tool,rule_model,
tag_mechanism}`. Two things to check before writing a command, never assume:
- **Does the tool already provide default-deny for unsolicited inbound?**
  Some tools ship that way out of the box (the zone-shaped tool on record
  does — see Observed dialects), in which case there is no equivalent "set
  default deny" command to run at all — the goal is the **posture**, and a
  tool that already has it needs nothing further here. Confirm with the
  tool's own state query rather than assuming either way.
- **Is the tailnet allow a rule, or a zone/interface assignment?** This
  follows directly from `rule_model` (§ "Firewall" above). A `zone`-shaped
  tool assigns the interface straight to a trusted zone/group — that
  assignment *is* the allow, and per § "Rule tagging" it cannot carry a tag.
  A `rule-list`-shaped tool instead needs a genuine allow rule matching the
  interface.

A tool that offers a reset-to-known-state operation may use it here — this
runs only on a fresh box, so nothing is lost by starting clean — but
re-adding the fallback-port rule afterward is not optional: the Phase 2
early-open (§ "Open the fallback SSH port") must survive it.

**Verification.** Off-node: `${SSH_FALLBACK_PORT}` answers and every other
port does not (excluding whatever the tailnet interface itself permits).
On-node: the tool's own state query reports the deny-by-default posture (or
confirms it was already there) and lists both the fallback-port rule and the
tailnet allow.

**Record.** `hardening.firewall: enabled`, journal, Observed dialects below.

#### Observed dialects — evidence, not the supported list
| tool | command | verified on | date |
|---|---|---|---|
| ufw | `sudo ufw --force reset && sudo ufw default deny incoming && sudo ufw default allow outgoing && sudo ufw allow ${SSH_FALLBACK_PORT}/tcp comment 'drydocc fallback ssh' && sudo ufw allow in on tailscale0 comment 'drydocc tailnet' && sudo ufw --force enable` — `--force reset` always lands at the same state (idempotent in effect) but drops unrelated rules, acceptable only because this path is fresh-box-only; it re-adds the fallback-port rule itself | test-01 / test-02 | 2026-06 |
| firewalld | already default-deny for unsolicited inbound — no reset and no default-deny command exists to run: `sudo systemctl enable --now firewalld && sudo firewall-cmd --permanent --add-rich-rule="rule port port=\"${SSH_FALLBACK_PORT}\" protocol=\"tcp\" log prefix=\"drydocc-fallback-ssh \" level=\"info\" limit value=\"1/m\" accept" && sudo firewall-cmd --permanent --remove-port=${SSH_FALLBACK_PORT}/tcp 2>/dev/null; sudo firewall-cmd --permanent --zone=trusted --add-interface=tailscale0 && sudo firewall-cmd --reload` — the interface allow is a zone assignment, not a rich rule, because interface allows cannot carry a tag (§ "Rule tagging") | test-01 / test-02 | 2026-06 |

### Adoption variant (called by /onboard-node Phase 7)

`/bootstrap-node` runs "Full hardening" above, which may reset the rule set
on a tool that offers that operation — correct on a fresh box, destructive on
an adopted one, where existing rules are what keep live services reachable.
Onboarding uses this variant instead.

**Goal.** The same default-deny-with-fallback-port-and-tailnet posture as
"Full hardening", reached **without resetting anything** (adoption
is non-destructive by invariant) — every port a live, accepted use-case or
host service needs stays reachable through the transition, and every
pre-existing rule that isn't the framework's own is left exactly as found.

**Compute `at_risk_ports` first.** It is the set of ports that are reachable
now and would not be after the framework's own rules are in place:

```bash
# currently listening (from the Phase 3 inventory — do not re-probe)
ss -lntupH | awk '{print $5}' | sed 's/.*://' | sort -u
```

Then, whatever `system_facts.firewall.tool` names, list what it already
permits, using its own rule-listing command (see Observed dialects below).

`at_risk_ports` = (published container ports ∪ host listening sockets) −
(fallback SSH port ∪ ports reachable over `tailscale0`) − (ports already
permitted by an existing rule). Each entry carries the slug of the accepted
document that will own it — container cluster or adopted host-native service
alike — or `adopted-unclassified` when no accepted document claims it.

**Fact / Deriving.** Same fact group as "Full hardening" above, with one
difference that is not optional: **never use a reset/clear-all operation this
tool offers**, even if it is the tool's own idiomatic way to reach a known
state — that is exactly what non-destructive adoption forbids here. Bring up default-deny (or
confirm it's already the posture) and add each allow — fallback port,
tailnet interface, and one per `at_risk_ports` entry — using the tool's
tag-carrying rule form. **The at-risk template must stay byte-identical to §
"Use-case port rules" → Add.** They are the same rules, written early. If
they drift, `/apply-use-case`'s later Add stops being a no-op and
`/remove-use-case` cannot match them — the failure this file's removal
caveat already describes.

**Verification.** Same as "Full hardening": off-node reachability for every
`at_risk_ports` entry plus the fallback port; on-node rule read-back naming
each tag. Additionally confirm **nothing** that was reachable before this ran
became unreachable — a false negative here is a live incident, not a
finding.

**Record.** `hardening.firewall: enabled`, the journal (name every
`at_risk_ports` entry and its owning slug or `adopted-unclassified`), and
Observed dialects below.

Pre-existing rules that survive are journaled as drift: they are not tagged,
so no use-case owns them, and nothing in the framework will ever remove them.

#### Observed dialects — evidence, not the supported list
| tool | already-permitted read | add (no reset — note the absence) | verified on | date |
|---|---|---|---|---|
| ufw | `sudo ufw status \| awk '/ALLOW/ {print $1}'` | `sudo ufw default deny incoming && sudo ufw default allow outgoing && sudo ufw allow ${SSH_FALLBACK_PORT}/tcp comment 'drydocc fallback ssh' && sudo ufw allow in on tailscale0 comment 'drydocc tailnet' && sudo ufw allow ${PORT}/${PROTO} comment "${SLUG}"` (one `allow` per at-risk port) `&& sudo ufw --force enable` | derived from the "Full hardening" idiom above by dropping the reset — not yet independently run through `/onboard-node` against a live adopted box | — |
| firewalld | `sudo firewall-cmd --list-ports; sudo firewall-cmd --list-rich-rules` | `sudo systemctl enable --now firewalld && sudo firewall-cmd --permanent --add-rich-rule="rule port port=\"${SSH_FALLBACK_PORT}\" protocol=\"tcp\" log prefix=\"drydocc-fallback-ssh \" level=\"info\" limit value=\"1/m\" accept" && sudo firewall-cmd --permanent --zone=trusted --add-interface=tailscale0 && sudo firewall-cmd --permanent --add-rich-rule="rule port port=\"${PORT}\" protocol=\"${PROTO}\" log prefix=\"${SLUG} \" level=\"info\" limit value=\"1/m\" accept"` (one rich rule per at-risk port) `&& sudo firewall-cmd --reload` | derived from the "Full hardening" idiom above by dropping the reset — not yet independently run through `/onboard-node` against a live adopted box | — |

### Use-case port rules

Called by `/apply-use-case` (step 9), `/modify-use-case` (firewall delta), and
`/remove-use-case` (step 5), once per entry in the doc's `ports:` list. `$SLUG`
is the use-case slug and **is the tag** — nothing else identifies these rules.
`$PORT`/`$PROTO` come from the `"<port>/<proto>"` entry. `$PORT` may be a
range, `<lo>-<hi>` (`"49152-65535/udp"`); one entry is always one rule.

**Goal.** `${PORT}/${PROTO}` is allowed (Add) or no longer allowed (Remove)
through the node's firewall tool, identified by `${SLUG}` and nothing else.

**Fact / Deriving.** Reuse `system_facts.firewall.{tool,rule_model,
tag_mechanism}`. A range is written in the tool's own range form, which is
not always the document's `<lo>-<hi>` spelling: derive it from the tool's
documentation for its port argument, and record it in the `range form`
column below once verified. Never expand a range into one rule per port.
**Add** uses the tool's tag-carrying form from § "Rule
tagging" with `${SLUG}` as the tag — idempotent, re-adding an existing rule
is a no-op. **Remove** should delete by matching the tag whenever the tool
supports that; a tool whose remove operation instead matches the whole rule
**string** verbatim (the case on record — see Observed dialects) cannot
safely reconstruct that string from `${PORT}`/`${PROTO}`/`${SLUG}` alone if
the add-side template has ever changed, so its safe default is
enumerate-then-remove: list every rule containing the tag and remove each
returned line verbatim, never a freshly-built guess.

**Verification.** Add: off-node the port answers, and on-node the read-back
(§ "Rule tagging") shows the tag. For a range, the read-back shows one rule
covering the whole range, and the off-node check is skipped: nothing need
answer on a range (a relay binds its ports on demand). Remove: off-node the port no longer answers
(unless another rule independently permits it), and on-node the read-back no
longer lists the tag.

**Record.** Journal the command; no new fact.

#### Observed dialects — evidence, not the supported list

**Add** (idempotent — re-adding an existing rule is a no-op):

| tool | range form | command | verified on | date |
|---|---|---|---|---|
| ufw | `<lo>:<hi>` — set `PORT=${PORT/-/:}` first | `sudo ufw allow ${PORT}/${PROTO} comment "${SLUG}"` | derived from § "Rule tagging"'s add form — not yet independently exercised through `/apply-use-case` against a live node | — |
| firewalld | `<lo>-<hi>`, unchanged | `sudo firewall-cmd --permanent --add-rich-rule="rule port port=\"${PORT}\" protocol=\"${PROTO}\" log prefix=\"${SLUG} \" level=\"info\" limit value=\"1/m\" accept" && sudo firewall-cmd --reload` | derived from § "Rule tagging"'s add form — not yet independently exercised through `/apply-use-case` against a live node | — |

**Remove** (idempotent — removing an absent rule is a no-op):

| tool | command | matches by | verified on | date |
|---|---|---|---|---|
| ufw | `sudo ufw delete allow ${PORT}/${PROTO}`, a range in `<lo>:<hi>` form as for Add | rule spec (delete-by-spec is native to a rule-list tool) | not yet independently exercised through `/remove-use-case` against a live node | — |
| firewalld | enumerate-then-remove — the rule string must match byte-for-byte, so never reconstruct it: `sudo firewall-cmd --list-rich-rules \| grep -F "${SLUG}" \| while IFS= read -r r; do sudo firewall-cmd --permanent --remove-rich-rule="$r"; done; sudo firewall-cmd --reload` | full rule string (`--remove-rich-rule` has no by-tag form) | not yet independently exercised through `/remove-use-case` against a live node | — |

If the add-side template above ever changes, rules written by the old
template become unremovable by a reconstructed new one — the
enumerate-then-remove form above is what makes `/remove-use-case` safe
regardless.

## fail2ban

**Goal.** fail2ban is enabled and actively watching SSH auth failures on
`${SSH_FALLBACK_PORT}`.

**Fact / Deriving.** Not a `system_facts` candidate — fail2ban itself is the
same tool everywhere the framework installs it (§ "Package installation");
only its own config layout can vary, and fail2ban can report that about
itself rather than have it assumed. Prefer fail2ban's own default SSH jail
name over a hardcoded guess: read the vendor `jail.conf` for the bracketed
section whose `filter` is `sshd`, falling back to `sshd` — the name shipped
in every fail2ban release seen so far — when nothing matches; drop the
override into that same install's `jail.d/` directory, alongside `jail.conf`:

**The commissioning client must stay in.** A jail that starts reads the
auth log back over its `findtime`. That window holds this bootstrap's own
failures from the client. The lockdown's check that root is refused, made
while root still holds the client's key, logs every refused root login as
up to three failures. Two such checks are over fail2ban's default
`maxretry`, and the jail's first act is to ban the client that is
commissioning the box. Start the jail
with the client's address — as the node sees it, the first field of the
session's `SSH_CONNECTION` — exempt, and drop the exemption once the jail
has read its history. Nothing stays exempt afterwards: a standing exemption
would also cover everything else behind that address.

```bash
JAIL="$(awk -F'[][]' '/^\[/{n=$2} /^[ \t]*filter[ \t]*=[ \t]*sshd/{print n; exit}' /etc/fail2ban/jail.conf 2>/dev/null)"
JAIL="${JAIL:-sshd}"
JAILD="$(dirname /etc/fail2ban/jail.conf)/jail.d"
CLIENT_ADDR="${SSH_CONNECTION%% *}"   # empty on a node-local surface: nothing to exempt
jail_conf() {  # jail_conf [extra ignoreip]
  printf '[%s]\nenabled = true\nport    = %s\n' "$JAIL" "${SSH_FALLBACK_PORT}"
  [ -z "${1:-}" ] || printf 'ignoreip = 127.0.0.1/8 ::1 %s\n' "$1"
}
sudo install -d -m 0755 "$JAILD"
if ! test -f "${JAILD}/${JAIL}.local"; then
  jail_conf "$CLIENT_ADDR" | sudo install -m 0644 /dev/stdin "${JAILD}/${JAIL}.local"
  sudo systemctl enable --now fail2ban
  for _ in $(seq 30); do sudo fail2ban-client status "$JAIL" >/dev/null 2>&1 && break; sleep 1; done
  sleep 2   # let the jail finish reading its history while the client is still exempt
  jail_conf | sudo install -m 0644 /dev/stdin "${JAILD}/${JAIL}.local"
  [ -z "$CLIENT_ADDR" ] || sudo fail2ban-client set "$JAIL" delignoreip "$CLIENT_ADDR" >/dev/null
else
  sudo systemctl enable --now fail2ban
fi
```

Run the block as **one** remote command: split across connections, a ban
issued between them would end the session that is meant to lift it.

**Verification.** `fail2ban-client status "$JAIL"` lists the jail as active,
its monitored port(s) include `${SSH_FALLBACK_PORT}`, and its banned list does
not hold `$CLIENT_ADDR`. Then open a **fresh** connection from the client to
`${SSH_FALLBACK_PORT}` — it must succeed. A jail name fail2ban itself doesn't
recognize means the derivation guessed wrong — fall back to `sshd` before
assuming a fresh install is broken. A client that is banned anyway has lost
the node: stop, journal the step with `outcome: failed` and the ban, leave
`hardening.fail2ban: unknown`, and tell the admin it lifts after the jail's `bantime` (or from the console with
`fail2ban-client set <jail> unbanip <address>`). Never retry in a loop: each
attempt extends the ban. A connection reset right after failed logins, with
the jail clear, is the SSH server's own per-source penalty (seconds to
minutes, healing itself): wait, don't hammer.

**Record.** `hardening.fail2ban: enabled`, journal.

Every run so far has resolved to jail name `sshd` and jail.d directory
`/etc/fail2ban/jail.d`, matching the fallback above — verified on test-01 /
test-02, 2026-06.

The client exemption was added after a lab bootstrap (x86_64 lab VM,
2026-10-15): a jail started without it read back the bootstrap's own refused
root logins from the lab host's address and banned it at once. Verified the
same day on a lab node with root holding the client's key and two refused
root logins in the window: the earlier form banned the client on start; this
form did not, the exemption was gone afterwards, and new failures from the
same address were banned again.

## Kernel hardening

**Goal.** The kernel runs with the framework's network and information-leak
hardening, the setting survives a reboot, and packet forwarding is on only
where something on this node actually forwards. The live values are the
state. `hardening.kernel_hardening` records that they were verified.

**Fact.** None new. `sysctl.d` drop-ins are part of the systemd baseline the
framework already gates on (`system_facts.service_manager`), so there is no
dialect to select. The forwarding decision reads facts that already exist:
`system_facts.container_engine`, and `hardening.tailscale` together with the
node's own tailnet preferences.

**Deriving the forwarding verdict.** Probe for a **consumer**: something on
this node that routes packets through the host's own stack. The verdict
decides two keys.

- **A rootful container engine with host-side networks** forwards. Examples
  are an adopted box's docker daemon, or any engine that `container_engine`
  names running as root. Ask the engine itself for its networks, because its
  presence alone proves nothing. Rootless podman under `doccap` is **not** a
  consumer: its networking runs in the user's own namespace, which is why a
  fresh drydocc node has none.
- **A node that advertises tailnet routes or offers itself as an exit node**
  forwards. Ask the node's own tailnet preferences, never the peer table. A
  non-empty advertised-routes list is a consumer, and an exit node advertises
  the default routes.
- **Anything else the admin names** while forwarding is already on. Ask on
  `/onboard-node`, and record the answer in the journal as the consumer.

| verdict | forwarding keys | reverse-path filter |
|---|---|---|
| no consumer | written as `0` | `1` (strict) |
| consumer found | **not written**: left exactly as found, never forced on | `2` (loose). Strict filtering drops the asymmetric replies a forwarding node sees |

Adoption never turns forwarding off under a running consumer, even when the
admin applies this item. Breaking an adopted box's networking is the failure
non-destructive adoption exists to prevent.

```bash
# RP_FILTER and the forwarding block come from the verdict above
{
  printf '%s\n' \
    '# Written by /bootstrap-node — managed by drydocc' \
    'kernel.randomize_va_space = 2' \
    'kernel.kptr_restrict = 2' \
    'kernel.dmesg_restrict = 1' \
    'net.ipv4.tcp_syncookies = 1' \
    'net.ipv4.conf.all.accept_redirects = 0' \
    'net.ipv4.conf.default.accept_redirects = 0' \
    'net.ipv4.conf.all.send_redirects = 0' \
    'net.ipv4.conf.default.send_redirects = 0' \
    'net.ipv6.conf.all.accept_redirects = 0' \
    'net.ipv6.conf.default.accept_redirects = 0' \
    "net.ipv4.conf.all.rp_filter = ${RP_FILTER}" \
    "net.ipv4.conf.default.rp_filter = ${RP_FILTER}"
  [ "$FORWARDING_CONSUMER" = none ] && printf '%s\n' \
    'net.ipv4.ip_forward = 0' \
    'net.ipv6.conf.all.forwarding = 0'
} | sudo install -m 0644 /dev/stdin /etc/sysctl.d/90-drydocc-hardening.conf
sudo sysctl --system >/dev/null
```

A kernel built or booted without IPv6 has no `net.ipv6.*` keys. Leave those
lines out instead of writing keys that fail to apply, and journal that they
were omitted. Every other missing key is a finding to raise with the admin,
not to drop silently.

The drop-in is drydocc's own file and is rewritten whole, so re-running it is
idempotent. It never touches `99-drydocc-unprivileged-ports.conf`, which
`podman-quadlets` § "Privileged ports" owns, and the two share no key.
`sysctl --system` re-applies every drop-in in boot order, so the read-back
below proves the value a reboot would give, not just what this one write
left behind.

**Verification.** Every key the drop-in wrote reads back at its written value
via `sysctl -n <key>`. A key that reads back different means a file applied
later in boot order overrides it. Find it with the service manager's own
config listing (`systemd-analyze cat-config sysctl.d`) and resolve it with the
admin. Never rename drydocc's file to win the ordering silently. On a node with
an active use-case, its published ports must still answer off-node: losing
one means the forwarding verdict was wrong. Revert the forwarding lines,
record the consumer, and re-derive.

**Record.** `hardening.kernel_hardening: enabled`, and journal the forwarding
verdict naming the consumer probe that decided it. On `/onboard-node`, also
journal each key's value as it was before the write.

#### Observed dialects — forwarding consumers; evidence, not the supported list
| consumer | how it was recognised | verified on | date |
|---|---|---|---|
| rootful podman networks | a container started as root with a published port, on podman's default bridge network. Forwarding keys not written, so `ip_forward=1` stayed as found; `rp_filter` written `2`; the container still answered | lab, Fedora 44 x86_64 (`/onboard-node` of a hand-built box) | 2026-10-14 |
| none | the rootful engine (podman) had no containers and only its default network; `tailscale debug prefs` reported `AdvertiseRoutes: null` and no exit node. Forwarding written `0`, `rp_filter` `1`; all 14 keys read back after `sysctl --system` | lab, Fedora 44 x86_64 (fresh bootstrap) | 2026-10-14 |

## Link-local name responders

**Goal.** No LLMNR or mDNS responder answers on any interface beyond
loopback, unless an active use-case declares that port. The firewall dropping
the traffic is a second layer and does not satisfy this goal. The goal is
that nothing is listening.

**Fact.** None new. The owner is found by **socket**, never by a name guess:
the processes holding the LLMNR (5355, udp and tcp) and mDNS (5353/udp)
listeners in `ss -lntupH`. Record each owner as
`system_facts.capabilities.<daemon>: present`.

**Deriving an unseen owner.** For each owner, ask:

1. **Is responding a side job?** A general resolver that also answers
   LLMNR or mDNS keeps resolving. Turn the responder off with its own config
   switch, in a drydocc drop-in when the daemon reads a drop-in directory,
   else a marker-delimited block in its main config (the shape § "SSH
   lockdown ordering" uses). Then restart only that daemon.
2. **Is responding its only job?** An mDNS daemon is the case. Disable and
   stop its unit **and** any socket unit that would restart it. On
   `/onboard-node` this needs the admin's approval: something on an adopted
   box may browse for services through it.
3. **Does an active use-case declare the port?** Leave it alone, and record
   the slug in the journal as the exception.

Resolution of ordinary names, the system's own lookups, must keep working
afterwards. Check it before calling the step done.

**Verification.**
- `ss -lntupH` shows nothing on 5355 or 5353 beyond loopback, apart from
  declared exceptions.
- A name lookup that worked before still works.
- After restarting the owning daemon, the listener stays gone: a config the
  daemon ignored would bring it straight back.

**Record.** `hardening.name_responders: disabled`, each owner under
`system_facts.capabilities`, and journal what was switched off and where.

#### Observed dialects — evidence, not the supported list
| owner | switch | verified on | date |
|---|---|---|---|
| systemd-resolved (LLMNR on `0.0.0.0:5355` and `[::]:5355`, udp+tcp) | a side job: `printf '[Resolve]\nLLMNR=no\nMulticastDNS=no\n' \| sudo install -m 0644 /dev/stdin /etc/systemd/resolved.conf.d/90-drydocc-responders.conf && sudo systemctl restart systemd-resolved`. The stub on `127.0.0.53:53` keeps resolving | lab, Fedora 44 x86_64 (`/onboard-node` of a hand-built box) | 2026-10-14 |

## Tailscale up

**Goal.** The node is joined to the tailnet as `${TAILSCALE_HOSTNAME}`,
online, and reachable via `tailscale ssh` (the `--ssh` flag enables the
listener the primary access path depends on).

**Fact.** None — the `tailscale` CLI and its subcommands are the same
everywhere the framework installs it; there is no dialect to select.

Judge the join from the node's **own** status entry, never from a line of
plain `tailscale status`: that table lists peers too (a peer whose name
contains `${TAILSCALE_HOSTNAME}` matches a grep), and the node's own row never
carries an `active` marker — that appears on peer rows only.

```bash
# this node, running, under its own name, online — and not renamed by the
# tailnet because another device already holds the name
ts_self_up() {
  local s; s=$(tailscale status --json --peers=false 2>/dev/null) || return 1
  grep -q '"BackendState": "Running"' <<<"$s" &&
  grep -q "\"HostName\": \"${TAILSCALE_HOSTNAME}\"" <<<"$s" &&
  grep -q "\"DNSName\": \"${TAILSCALE_HOSTNAME}\." <<<"$s" &&
  grep -q '"Online": true' <<<"$s"
}

ts_self_up || sudo tailscale up --authkey "$DOCCAP_TS_KEY" --hostname "${TAILSCALE_HOSTNAME}" --ssh

# poll for online (timeout 60s)
for i in $(seq 1 30); do
  ts_self_up && break
  sleep 2
done
```

**Verification.** The poll above is not itself sufficient proof — it only
stops early on success. Check its result explicitly before proceeding:

```bash
ts_self_up || { echo "tailscale did not come up as ${TAILSCALE_HOSTNAME}"; \
  tailscale status --json --peers=false | grep -E '"(BackendState|HostName|DNSName|Online)"'; exit 1; }

# idempotent — also self-heals nodes joined before --ssh was added here
sudo tailscale set --ssh
```

**Record.** `hardening.tailscale: joined`, journal. § "Tailscale cut-over
routine" (Phase 4) is the independent, off-node re-confirmation of this same
goal.

## Lingering & identity file

**Goal.** `doccap`'s systemd user instance survives logout (so rootless
podman quadlets and `node-exec` lingering keep running unattended), and
`/etc/drydocc/node-name` names this node for any script that needs it without
an SSH round-trip.

**Fact.** None — lingering is a systemd user-manager feature, and
`system_facts.service_manager: systemd` is already the one hard capability
gate this framework requires everywhere. It is a rejected
`system_facts` candidate in its own right: "lingering mechanism — derivable
from `service_manager`, no slot" (`conventions` skill's `references/
distro-agnosticism.md` Rule 6).

```bash
loginctl show-user doccap --property=Linger 2>/dev/null | grep -q 'Linger=yes' \
  || sudo loginctl enable-linger doccap

test -f /etc/drydocc/node-name && grep -qxF "${NODE_NAME}" /etc/drydocc/node-name \
  || (sudo install -d -m 0755 /etc/drydocc \
      && echo "${NODE_NAME}" | sudo install -m 0644 /dev/stdin /etc/drydocc/node-name)
```

**Verification.** `loginctl show-user doccap --property=Linger --value`
reports `yes`; `cat /etc/drydocc/node-name` matches `${NODE_NAME}` exactly.

**Record.** Journal both writes. No `system_facts` entry (Rule 6, above).

## Fork write access

**Goal.** The fork is cloned at `~doccap/<repo>` on the node, and an agent
working there can commit and push its own `nodes/<self>/` changes — every
on-node command ends in a commit + push. Access is a **per-node SSH deploy
key with write access**, generated on the node: the private key never leaves
it, so it never transits the agent, never sits in KeePass, and never lands in
a URL or `.git/config`. A lost or compromised node is cut off by deleting
its one deploy key on the git host; a replacement box generates a new one.
Deploy keys with write access exist on the common hosts (GitHub, GitLab,
Gitea/Forgejo); where a host offers none, use a dedicated machine account
holding this same key.

**Fact.** None — the remote URL is read from the laptop's working copy
(`git remote get-url origin`). `REMOTE_SSH_URL` is its SSH form — convert an
HTTPS remote `https://<host>/<owner>/<repo>[.git]` to
`git@<host>:<owner>/<repo>.git`; `REPO` is `<repo>` without `.git`;
`NODE_NAME` is the node's slug.

**Lab remote.** When `origin` is a lab remote (`conventions` skill's
`references/lab-overlay.md` § "Lab remote"), `REMOTE_SSH_URL` is the URL
**unchanged** — it is not a git host and has no SSH form — and step 2 is
answered by `bootstrap.fork-key-registered: lab-remote`: the key is still
generated, there is just nowhere to register it. Steps 1 and 3 and the
verification run as written.

All commands run **as `doccap` on the node**, over whichever session the
calling command holds (`tailscale ssh doccap@<tailscale_hostname>` in
`/bootstrap-node`; the established SSH session wrapped in `sudo -u doccap -H`
in `/onboard-node`).

1. **Key.** Idempotent; print the public half and its fingerprint:

   ```bash
   install -d -m 700 ~/.ssh
   test -f ~/.ssh/drydocc-fork \
     || ssh-keygen -t ed25519 -N '' -C "drydocc-${NODE_NAME}" -f ~/.ssh/drydocc-fork
   cat ~/.ssh/drydocc-fork.pub
   ssh-keygen -lf ~/.ssh/drydocc-fork.pub
   ```

2. **Register it — the admin's step.** Show the public key and ask the admin
   to add it to the fork as a deploy key titled `drydocc-<node>` **with write
   access enabled**, then wait for confirmation
   `[decision: bootstrap.fork-key-registered]`. If an older
   `drydocc-<node>` key is listed there (a replaced box), tell the admin to
   delete it.

3. **Clone and configure.** `core.sshCommand` pins the key to this one
   working copy; the identity names the node so `git log` shows which node
   made each commit; `pull.rebase` makes the pre-push pull in
   `conventions` `references/journal.md` § "Committing" a rebase.

   ```bash
   KEY_SSH='ssh -i ~/.ssh/drydocc-fork -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new'
   test -d ~/"${REPO}"/.git \
     || GIT_SSH_COMMAND="$KEY_SSH" git clone "${REMOTE_SSH_URL}" ~/"${REPO}"
   git -C ~/"${REPO}" remote set-url origin "${REMOTE_SSH_URL}"
   git -C ~/"${REPO}" config core.sshCommand "$KEY_SSH"
   git -C ~/"${REPO}" config user.name "drydocc ${NODE_NAME}"
   git -C ~/"${REPO}" config user.email "doccap@${NODE_NAME}.invalid"
   git -C ~/"${REPO}" config pull.rebase true
   ```

   `remote set-url` also repairs a working copy cloned some other way — an
   HTTPS remote, or one with a token embedded in its URL, which this
   replaces. The `.invalid` domain is reserved and never routes; it marks the
   address as an identity, not a mailbox.

**Verification.** Both must pass:

```bash
git -C ~/"${REPO}" ls-remote origin HEAD >/dev/null      # read
git -C ~/"${REPO}" push --dry-run origin HEAD            # write
```

`push --dry-run` opens the same write channel a real push does without
changing the remote, so a key registered **without** write access fails
here rather than at the first real commit on the node. On a failure, re-show
the public key and repeat step 2; never fall back to a token in the URL.

**Record.** Journal the public-key fingerprint (not the key), the remote URL,
and both verification results. The clone HEAD should match the laptop's
`git rev-parse HEAD`; a difference is a warning, not a failure.

## File integrity

Run **last** on the node, after § "Fork write access", so the database
records the hardened box with its deploy key, not the box halfway through.

**Goal.** A file-integrity database describes the node's system files as the
baseline left them, and its hash is recorded off the node. A later check can
then tell an unexplained change from an explained one, and can tell when the
database itself was rewritten.

**Fact.** The integrity tool is probe-on-demand presence, not a
`system_facts` key: nothing branches on which tool it is
(`conventions` skill's `references/distro-agnosticism.md` Rule 6). Record it
in the open map as `system_facts.capabilities.<tool>: present`. The package
comes from § "Package installation", never from upstream source, so
`/node-maintenance` keeps it patched.

**Deriving an unseen tool.** A tool serves this outcome when it offers all of
the following:

1. An **init** that writes a database of the configured paths.
2. A **check** that compares live files against the database, read-only, and
   exits non-zero when something changed.
3. An **update** (re-base) that writes a new database from the live state.
4. **Exclusion rules** in its config.

Ask the tool's own config where it reads and writes its database. Never
assume a path, because the input and output database names differ between
packagings of the same tool, and some packagings wrap init in a helper
script. Promote the freshly written output database to the input name the
config declares.

**Exclusions** go in the tool's include directory when its config has one.
Otherwise they go in a marker-delimited block in the main config, the same
shape § "SSH lockdown ordering" uses for an sshd with no drop-in directory.
Exclude what changes by design:

- `doccap`'s container storage (`~doccap/.local/share/containers`) and cache
  (`~doccap/.cache`)
- the fork working copy (`~doccap/<repo>`)
- log stores and the journal
- the tailnet daemon's state directory, which rotates its own keys

Keep `~doccap/.ssh`, `~doccap/.config` (quadlets and user units),
`/etc/drydocc`, and `/etc` as a whole in scope. Changes there are exactly
what a later check exists to surface. The check explains the legitimate ones
from the journal.

**Create the in-scope framework directories before init**, owned by
`doccap`: `~doccap/.config`, `~doccap/.config/systemd/user` and
`~doccap/.config/containers/systemd`. On a fresh bootstrap the rootless
probe has usually created them already. On an adopted box `doccap` is new,
and the first tool that touches its config after init would otherwise
surface as an added directory. The 2026-10 lab onboarding hit exactly that.
Later contents of these directories are changes the journal explains; the
directories themselves are not.

```bash
sudo -u doccap -H install -d -m 0700 ~doccap/.config \
  ~doccap/.config/systemd/user ~doccap/.config/containers/systemd
```

Init can take minutes on a slow disk or an emulated board. Run it under
`node-exec`'s lingering contract, not as a bare SSH command a dropped
connection would kill.

```bash
# after init: hash the database the tool will read from now on
sudo sha256sum "${INTEGRITY_DB}" | awk '{print "sha256:"$1}'
```

**Verification.** Run the check immediately after init. It must report no
changes. A change reported here means something is still writing to a path
in scope. Exclude it if it changes by design, or find out why it is
changing, then re-init. Never record a database whose first check is
already dirty.

**Record.** `hardening.file_integrity: enabled`, and
`system_facts.capabilities.<tool>: present`. Journal the database path and
its `sha256:` hash. The journal entry is the off-node record that
`/node-maintenance`'s `file-integrity-verified` item compares against,
because the database on the node could be rewritten along with the files it
describes.

#### Observed dialects — evidence, not the supported list
| tool | init | promote | check | update | verified on | date |
|---|---|---|---|---|---|---|
| aide 0.19 | `sudo aide --init -c /etc/aide.conf`, under lingering (7 s on a native guest). `/etc/aide.conf` has no include directory, so exclusions go in the marker-delimited block, followed by `sudo restorecon /etc/aide.conf` under enforcing selinux and `sudo aide --config-check` | `sudo mv -f /var/lib/aide/aide.db.new.gz /var/lib/aide/aide.db.gz` (`database_out` → `database_in` in the config) | `sudo aide --check -c /etc/aide.conf`, exit 0 when clean | `sudo aide --update -c /etc/aide.conf`, then the same promote. Its exit status is the same change bitmask as the check (`5` = added + changed), not a failure. Verified by a `/node-maintenance` `[s] accept` re-base, 2026-10-15 | lab, Fedora 44 x86_64 (fresh bootstrap) | 2026-10-14 |

## SSH-key cut-over routine (Phase 2)

After lockdown, before relying on the new endpoint. Proves the key works on the
new port; the root session is the safety net.

```bash
ssh -i ~/.ssh/doccap-at-"$SELF" -p "${SSH_FALLBACK_PORT}" \
  -o StrictHostKeyChecking=accept-new doccap@"${INITIAL_IP}" 'sudo -n true && echo ok' \
  || { echo "key cut-over failed — DO NOT close the root session"; exit 1; }
```

## Tailscale cut-over routine (Phase 4)

After hardening. Both must pass; either failure halts with `outcome: partial`.

The first check verifies tailnet reachability. Prefer `tailscale ssh`, but it
needs a reachable `tailscaled` on the **admin** side — under WSL2 the CLI is
present while the daemon runs on the Windows host, so `tailscale ssh` fails even
though the tailnet is fine. Detect that and fall back to key-based SSH over the
node's tailscale IP (fetched from the node itself over the still-open
fallback-port session). If this fallback triggers on a non-WSL2 client, treat
it as a signal that the node's Tailscale SSH listener isn't enabled (a Phase 3
regression — see the "Tailscale up" recipe above) rather than an expected
client-side quirk, and flag it instead of silently proceeding:

```bash
if ! tailscale ssh doccap@"${TAILSCALE_HOSTNAME}" 'echo ok' 2>/dev/null; then
  echo "tailscale ssh unavailable (e.g. WSL2: tailscaled on the host) — falling back to IP."
  TS_IP=$(ssh -i ~/.ssh/doccap-at-"$SELF" -p "${SSH_FALLBACK_PORT}" \
            -o StrictHostKeyChecking=accept-new doccap@"${INITIAL_IP}" 'tailscale ip -4' | head -1)
  ssh -i ~/.ssh/doccap-at-"$SELF" -o StrictHostKeyChecking=accept-new \
    doccap@"${TS_IP}" 'echo ok' || { echo "tailnet reachability check failed"; exit 1; }
fi

# second mandatory check: key-based SSH over the fallback port still works
ssh -i ~/.ssh/doccap-at-"$SELF" -p "${SSH_FALLBACK_PORT}" \
  -o StrictHostKeyChecking=accept-new doccap@"${INITIAL_IP}" 'echo ok' \
  || { echo "fallback ssh failed"; exit 1; }
```

## Idempotency / resumability

Every command is check-then-act so a re-run after partial failure is safe and
resumes naturally: `id doccap || <create-user idiom>`, `grep -qxF
|| append`, `tailscale status | grep || tailscale up`, etc. The agent derives "where to
resume" by probing live state; the only persisted marker is
`provisioning.status` for VM creation (`provision-vm`).

## Failure handling

Each command the agent issues is followed by an exit-code check. On non-zero:

1. Capture stderr.
2. Append the journal entry with `outcome: partial`, the failed command, stderr.
3. Halt — do not run subsequent commands, do not roll back.
4. The most-recent working session (root in Phase 2, doccap-on-fallback later)
   is left intact; the admin inspects the journal and re-runs `/bootstrap-node
   <slug>`.

## What this skill does NOT do

- It does not execute commands itself — it is reference material the agent reads
  when composing the next command.
- It does not hold the orchestration sequence (that is the command).
- It does not fetch secrets (that is `secrets`).
- It does not provision VMs (that is `provision-vm`).
