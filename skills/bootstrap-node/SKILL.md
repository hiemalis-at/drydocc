---
name: bootstrap-node
description: Take a node to the drydocc baseline — run from the admin's laptop. Optionally provisions a qemu/kvm VM, then establishes SSH key access over an initial root password, hardens the box, joins the tailnet, and clones the fork. Resumable and idempotent. Use for any fresh Linux machine with systemd and SSH — server, PC, SBC, vServer, NAS — or a new VM.
compatibility: Runs inside a drydocc fork; needs a shell, file edits and web access.
metadata:
  skillset: drydocc
  requires: provision-vm conventions secrets node-context
---

# /bootstrap-node

## Contract

- **Args:** `<slug>` (required; kebab-case node name) + an optional free-text
  description (e.g. `/bootstrap-node vps-fra-01 "edge web node for the blog"`).
  The description seeds `roles` / a notes line.
- **Runs on:** the admin's client (local laptop), NOT the node. The agent
  provisions and/or SSHes to the node from here.
- **Resumable & idempotent:** every step probes live state and skips work
  already done. A re-run after any failure picks up where it left
  off. The only persisted resume marker is `provisioning.status` (VM creation).
- **Reads:**
  - `nodes/<slug>/node.yaml` (if it already exists; otherwise scaffolds it)
  - Live distro + SSH-server detection from the node (via SSH)
  - Session secret `DOCCAP_TS_KEY` resolved env-var-first per `secrets` § "Session secrets"
- **Writes (in the repo):**
  - Scaffolds `nodes/<slug>/` (`node.yaml`, `journal/`, `use-cases/`) — Phase 0.
  - Progressively fills `node.yaml`: `distro/distro_version/arch`,
    `system_facts.*`, `identity.*`, `provisioning.*`, `ssh.*`, `hardening.*`,
    `last_bootstrapped`.
  - Appends one journal entry under `nodes/<slug>/journal/` with `scope: hardening`.
- **Writes (on the admin laptop):**
  - `~/.ssh/doccap-at-<slug>` (0600) when a key is generated (`secrets`
    § "SSH key storage").
- **Writes (on the node, via SSH):**
  - `/home/doccap/.ssh/authorized_keys` (doccap:doccap, 0600)
  - `/etc/sudoers.d/doccap` (root, 0440)
  - The SSH server's config: a drop-in file under `ssh.config_include_dir`
    when the server has one, else its main config edited in place (root) —
    see `references/hardening-recipes.md` § "SSH lockdown ordering"
  - The fail2ban jail config (root) — see `references/hardening-recipes.md`
    § "fail2ban"
  - The firewall tool's persistent store (per `system_facts.firewall.tool`)
  - `/root/.ssh/authorized_keys` moved aside to a dated name — see
    `references/hardening-recipes.md` § "SSH lockdown ordering" Step 7
  - The name resolver's drop-in, or a responder-only daemon's unit state —
    see `references/hardening-recipes.md` § "Link-local name responders"
  - `/etc/sysctl.d/90-drydocc-hardening.conf` (root, 0644) — see
    `references/hardening-recipes.md` § "Kernel hardening"
  - The file-integrity tool's exclusions and database (root) — see
    `references/hardening-recipes.md` § "File integrity"
  - `/etc/drydocc/node-name` (root, 0644)
  - `/home/doccap/.ssh/drydocc-fork{,.pub}` — the node's own fork deploy key,
    generated on the node (`references/hardening-recipes.md` § "Fork write access")
  - `/home/doccap/<repo>` (doccap:doccap; cloned with that key, with a
    node-named git identity configured on the working copy)
- **Journal scope:** `hardening` — one entry covering scaffold + provision +
  onboard + harden, all commands verbatim under `## Commands`.

## Prerequisites the admin must satisfy

- For **`source: running`**: a box (flashed SBC / rented vServer) is reachable
  from the laptop, and the admin has its **temporary root password**.
- For **`source: vm`**: the laptop is a qemu/kvm host (`virt-install`,
  `qemu-img`, `genisoimage`/`cloud-localds` available — see `provision-vm`).
- A way to supply `DOCCAP_TS_KEY` per `secrets` § "Session secrets": either
  the `@max-win-at/keepassxc-secrets` skill (a KeePass entry with URL
  `drydocc://tailscale`), or a `.env` holding it.
  Resolved env-var-first and just-in-time in Phase 3, not at the start of the
  run.
- Admin rights on the fork's git host, to add the node's deploy key with
  write access when Phase 5 asks.
- The admin has the `tailscale` CLI authenticated to the same tailnet the node
  will join (required for the cut-over check in Phase 4).

## Procedure

Every shell command runs from the admin's client (directly, or over SSH to the
node, or via the hypervisor). Each command is captured (cmd, exit code, stdout
summary) for the journal in Phase 6. **Within Phases 1–3, stop on any non-zero
exit** — the journal is still written (`outcome: partial`) so a re-run resumes.
As each phase starts, set its number as `phase` in the resume pointer
(`continue-last-command` § "Resume pointer"), so an interrupted run shows where
it stopped.

### Phase 0 — Bind, choose source, scaffold

1. Invoke `node-context`. On the admin's laptop the node-side identity
   file does not exist; `<self>` is the `<slug>` argument.
2. Validate `<slug>` against `^[a-z][a-z0-9-]{0,30}[a-z0-9]$`. On failure, print
   why and exit.
2b. **Client preflight** (`references/hardening-recipes.md` § "Client environment preflight"):
   detect the admin laptop's OS/distro and required tooling. Surface what's
   missing. `ssh`/`ssh-keygen` missing is fatal, and so is `tailscale` unless
   this is a lab run with the tailnet off (`conventions` skill's
   `references/lab-overlay.md` § "Tailnet off"); for `source: vm`,
   missing virt tooling is fatal; **`sshpass` missing is not fatal** — switch the
   root-password connection to the `SSH_ASKPASS` fallback for the rest of the
   run. A present `tailscale` CLI does **not** imply a reachable daemon (under
   WSL2 `tailscaled` runs on the Windows host); not fatal — the Phase 4
   cut-over falls back to key-based SSH over the tailscale IP.
   Recommend the `guestfish` tool for VM disk recovery when it can be found;
   the package that provides it varies by system, so resolve its name the
   same way `references/hardening-recipes.md` § "Package installation"
   resolves any package name — same not-fatal hedge as the `sshpass`
   handling above.
   Also run the **secrets preflight** (`secrets` § "Preflight"). Non-fatal —
   any unset secret falls back to a `.env`.
3. Determine run mode:
   - If `nodes/<slug>/node.yaml` exists **and** `last_bootstrapped` is set →
     ask the admin: *"Node `<slug>` is already bootstrapped
     (last_bootstrapped: <date>). Re-bootstrap now (audit & repair)?"*
     `[decision: bootstrap.rebootstrap]` If no,
     exit. If yes, continue in **audit-&-repair** mode (probes still skip
     already-correct state; mismatches are reported, not silently overwritten).
     A `hardening.*` field the profile lacks — one added to the baseline after
     this node was bootstrapped — reads as `unknown`. This run probes it and
     establishes it like any other drift, which is how older nodes catch up.
   - If `nodes/<slug>/` exists but `last_bootstrapped` is `null` → **resume**
     silently.
   - Otherwise → **fresh** run.
4. Ask the provisioning **source**: `running` (an already-reachable box) or
   `vm` (provision a qemu VM first). `[decision: bootstrap.source]`
5. Scaffold `nodes/<slug>/` if absent: write a `node.yaml` stub
   (`name: <slug>`, `doccap_user: doccap`, `ssh_fallback_port: 42022`,
   `tailscale_hostname: <slug>`, `roles:` parsed from the description,
   `provisioning.source: <source>`, `hardening.*: unknown`,
   `created: <today>`, `last_bootstrapped: null`); also write a
   `secrets.required_env` block declaring `DOCCAP_TS_KEY` (required, `keepass_entry:
   drydocc://tailscale`) per `conventions`. Create empty `journal/` and `use-cases/` (`.gitkeep`).
   The journal dir must exist now — every subsequent state change journals
   (invariant 2).
6. **Do not resolve session secrets here.** The tailscale key is resolved
   just-in-time in Phase 3 (step 26). See `secrets` § "Session secrets".

### Phase 1 — Provision (VM only; skipped when `source: running`)

Consult `provision-vm` for every command in this phase.

7. **Resume probe:** if `provisioning.status: provisioned` and the domain
   exists → skip to Phase 2. Probe the domain **independently of `node.yaml`**
   (`virsh dominfo`, on `provisioning.libvirt_uri` when recorded; the
   provision-vm probe checks both libvirt connections) — a fresh worktree may
   have no `node.yaml` while an orphaned domain still exists; treat that as
   interrupted (offer `provision-vm` § "Teardown"), not a fresh provision. See
   `provision-vm` § "Resume probe".
8. If not yet provisioned: run `provision-vm` § "Host preflight", then its
   § "Phase 1 — Provision VM" (steps 6–12) in full — specs, OS choice,
   latest-release resolution, ISO download, seed render, `virt-install`,
   reach discovery. Every recipe lives there; do not restate it here.
9. On completion `provisioning.status: provisioned` is written to `node.yaml`;
   the box is SSH-reachable through `provisioning.reach` with a known root
   password — identical to the `source: running` precondition for Phase 2.

### Phase 2 — SSH onboarding (shared)

Establish key-based access and lock SSH down **before** tailscale exists.
Consult `references/hardening-recipes.md` for each idempotent snippet.

13. Determine the initial root connection: for `source: running`, prompt the
    admin for `<initial-ip>`, `<initial-port>` (default `22`), and the
    **temporary root password** (passed via `sshpass`/heredoc — never logged).
    `[decision: bootstrap.initial-connection]` — in a lab run the overlay's
    `nodes.<slug>.initial` and `DOCCAP_ROOT_PW` from the environment answer it.
    For `source: vm`, use `provisioning.reach.host` as `<initial-ip>`, the
    host port `provisioning.reach.ports` maps guest port 22 to as
    `<initial-port>`, and the password set in Phase 1. The fallback SSH port
    is forwarded at the same number, so every later `-p <ssh_fallback_port>`
    works unchanged.
14. **Discover the OS and its tool dialects:** `cat /etc/os-release && uname -m`.
    In fresh/resume mode, write `distro`/`distro_version` into `node.yaml` as
    discovered facts, and `arch` **verbatim as `uname -m` reports it** — never
    normalised (`conventions` skill's `references/node-profile.md`). Then probe
    the full `system_facts` ledger using the detection commands in
    `references/hardening-recipes.md` § "System facts discovery" (which covers
    `service_manager`, `container_engine`, `mac_framework`,
    `firewall.{tool,rule_model,tag_mechanism}`, `rootless_ready`, and
    `capabilities.{python3,rsync,tmux}`) and § "Package installation" (for
    `package_manager`), and write each plus `.detected` (see
    `conventions` skill's `references/node-profile.md`). **`service_manager` is
    a refusal gate**: if it is not systemd, stop here with that reason stated
    (quadlets are a systemd feature) rather than failing later on a user unit.
    These are **discovered facts, never assumed from `distro` alone**.
    Also probe machine identity via `references/hardening-recipes.md`
    § "Machine identity probe" and write `identity.machine_id_hash` /
    `.host_key_fingerprint` (`.first_seen` only if the block is not already
    present). In audit-&-repair mode, compare all of the above,
    **including `identity`**, to the existing values and **warn on drift** (do
    not silently overwrite). A `system_facts` or `identity` mismatch — a box
    whose tooling changed generation, or whose machine id changed, since the
    last run — is exactly what this step exists to catch. Show each old and
    live value side by side; once the admin confirms the box was replaced or
    retooled, **record the live values** (`identity.first_seen` excepted). A
    profile describing a box that is no longer there would make every later
    apply render for the wrong firewall tool, MAC framework or architecture. If
    the admin cannot account for the drift, stop and journal `partial`.
15. **Detect the SSH server + usable key algorithms** (`references/hardening-recipes.md`
    § "SSH server & algorithm detection"). Record `ssh.server`.
16. **Create `doccap`** + passwordless-sudo sudoers entry
    (`references/hardening-recipes.md` § "Create doccap via root password").
17. **Choose the key source** (ask the admin) `[decision: bootstrap.key-source]`:
    - **generate** `doccap-at-<slug>` with an algorithm negotiated against
      `ssh.server` (default `ed25519`; `rsa-4096` fallback). Store per
      `secrets` § "SSH key storage". Record `ssh.key_*`.
    - **import** a key from a KeePassXC ssh-agent the admin provides; record its
      public-key fingerprint + algorithm.
18. **Install the public key** into `~doccap/.ssh/authorized_keys` using the
    root password (`references/hardening-recipes.md` § "Install pubkey via password").
19. **Verify key + sudo:** connect as `doccap` with the key and run
    `sudo -n true`. Both must pass **before** touching sshd. On failure, halt.
20. **Lock SSH down — dual-port transitional** (`references/hardening-recipes.md`
    § "SSH lockdown ordering"):
    a. **Only when `system_facts.mac_framework` names a framework that
       requires a new port to be explicitly permitted before a service can
       bind it**, permit the port first (`references/hardening-recipes.md`
       § "SSH lockdown ordering", Step 1). Most values of `mac_framework` —
       including `none`, `unknown`, and any framework nobody has recorded
       yet — don't enforce this and skip the step; the recipe's Step 1 states
       which recorded value does.
    b. **Open the firewall** for `<ssh_fallback_port>` now — *before* the move
       (`references/hardening-recipes.md` § "Firewall" → "Open the fallback SSH port"),
       using whichever tool `system_facts.firewall.tool` names. Some systems
       ship an active firewall by default that would otherwise block it.
    c. Write the SSH server's config with root login off, password auth off,
       the session policy (empty passwords and X11 forwarding refused,
       public keys on, dead-peer keepalive),
       and **both** `Port 22` and `Port <ssh_fallback_port>` listening, exactly
       as `references/hardening-recipes.md` § "SSH lockdown ordering" Step 3
       specifies; then **reload** (not restart) it so the root session
       survives.
    d. Verify the doccap key on `<ssh_fallback_port>`.
    e. Only after that passes, rewrite the config to `Port <ssh_fallback_port>`
       only and reload again, per the same recipe section's Step 5.
21. **Re-verify** the doccap key on the new port. On failure at step 20d/21:
    the box is still reachable on port 22 (and the root session is live) — keep
    sessions intact, journal `partial`, halt (recovery checklist below).
21b. **Retire root's authorized keys** (lockdown Step 7). Only after step 21
    passed. Move every file the server reads for root aside to a dated name,
    and journal the key fingerprints.
22. Persist `ssh.*`, `hardening.ssh_root_login: disabled`,
    `hardening.ssh_password_auth: disabled`, and `hardening.ssh_session_policy`
    (as the recipe's Record step decides) to `node.yaml`.

### Phase 3 — Hardening (shared)

All commands now run as `doccap` over the key on `ssh_fallback_port`,
escalating with `sudo`. Consult `references/hardening-recipes.md`.

23. Install the framework's package set — podman and its rootless networking
    helper, tailscale, the node's firewall tool, fail2ban, git, **tmux**,
    **rsync** —
    per `references/hardening-recipes.md` § "Package installation", which
    resolves concrete package names for the detected package manager; do not
    re-list them here. `tmux` backs the `node-exec` lingering contract so
    long on-client use-case steps survive SSH drops. Refusal turns
    on capability, never identity: the one hard gate is
    `system_facts.service_manager` being anything other than `systemd`
    (quadlets are a systemd feature); state the reason and the
    failing check, per `references/distro-agnosticism.md` Rule 4, rather than
    naming a distro.
24. Bring the firewall to default-deny-inbound with `ssh_fallback_port` and
    the tailnet interface allowed, per `references/hardening-recipes.md`
    § "Firewall" → "Full hardening" — do not restate its rule sequence here.
25. Configure fail2ban on `ssh_fallback_port`; enable + start.
26. **Tailnet off** (a lab run, `conventions` skill's
    `references/lab-overlay.md` § "Tailnet off"): skip this step entirely —
    no key is resolved and nothing joins; step 31 records the join as
    `deferred`. Otherwise:
    **Resolve `DOCCAP_TS_KEY` now** (`secrets` § "Session secrets"; required).
    On a keepass miss, apply that skill's not-found handling; if it still
    can't be resolved, mark the resume pointer waiting on it and offer the `.env` →
    `/exit` → `set -a; . ./.env; set +a` → `/continue-last-command` path.
    Then `tailscale up --authkey "$DOCCAP_TS_KEY" --hostname <tailscale_hostname>
    --ssh` — **`--ssh` is required**: it enables the Tailscale SSH listener that
    `tailscale ssh doccap@<hostname>`, the primary access path, depends on.
    Poll until online (timeout 60s). Follow with an unconditional
    `tailscale set --ssh` so resumed/already-joined nodes self-heal too.
27. `loginctl enable-linger doccap`.
28. Write `/etc/drydocc/node-name` containing `<slug>`.
28a. **Silence link-local name responders** per
    `references/hardening-recipes.md` § "Link-local name responders": find
    each LLMNR/mDNS listener's owner by socket, switch the responder off in
    that daemon (or disable a responder-only daemon), and confirm both that
    nothing listens beyond loopback and that ordinary lookups still resolve.
28b. **Harden kernel parameters** per `references/hardening-recipes.md`
    § "Kernel hardening". It runs after the tailnet join because the
    forwarding verdict reads the node's own tailnet preferences. Derive the
    verdict, write the drop-in, re-apply it in boot order, and read every key
    back. Journal the verdict and the consumer probe that decided it.

### Phase 4 — Tailscale cut-over verification

29. Run both checks from the client (`references/hardening-recipes.md` § "Tailscale cut-over
    routine"):
    - tailnet reachability: `tailscale ssh doccap@<tailscale_hostname> 'echo ok'`,
      falling back to key-based SSH over the node's tailscale IP when `tailscale
      ssh` can't reach the local daemon (e.g. WSL2) — see the reference.
    - `ssh -p <ssh_fallback_port> -o StrictHostKeyChecking=accept-new -i ~/.ssh/<key> doccap@<initial-ip> 'echo ok'`
    Both must print `ok`. (The SSH-key cut-over was already proven in Phase 2.)
    With the tailnet off, skip the tailnet check; the fallback-port check
    still runs and must pass.
    On either failure: journal `outcome: partial`, leave sessions usable, halt.

29b. **Probe VS Code Remote Server compatibility** — over the keyed
    SSH, run a small read-only sweep checking CPU arch, C library, and
    outbound reachability of the VS Code update host, then classify
    `remote_ide.vscode_server` per the exact thresholds and verdicts the
    `conventions` skill's `references/node-profile.md` documents for that
    field — do not re-derive them here. Hold the verdict + reason for the
    Phase 6 manifest write. Probe failure (SSH error) → `unknown`; do not halt
    the run on this read-only step.

### Phase 5 — Land the repo

30. **Give the node write access to the fork and clone it**, over `tailscale
    ssh doccap@<tailscale_hostname>` (keyed SSH on the fallback port when the
    tailnet is off), per `references/hardening-recipes.md`
    § "Fork write access": generate the node's deploy key on the node, have
    the admin register it **with write access**, clone, configure the working
    copy's key and git identity, and verify read *and* write. Every on-node
    command ends in a commit + push, so a clone that cannot push is a
    failure: journal `partial` and halt on a failed verification rather than
    finishing with a read-only node. When `origin` is a lab remote, the
    recipe's lab-remote clause applies: the URL is used unchanged and no
    deploy key is registered. Resume probe: a working copy whose
    `origin` is already the SSH URL and whose verification passes is
    skipped; any other existing working copy is re-pointed, not re-cloned.
30b. **Initialise file integrity**, last on the node, per
    `references/hardening-recipes.md` § "File integrity": write the
    exclusions, init the database under `node-exec`'s lingering contract,
    promote it, and confirm the first check is clean. Hold the database's
    `sha256:` hash for the journal. Resume probe: a database whose hash
    matches the one the last bootstrap journal recorded, and whose check is
    clean, is skipped. A database that exists but matches no recorded hash is
    drift. Report it, and re-init only with the admin's approval.

### Phase 6 — Finalize

31. Update `nodes/<slug>/node.yaml`: `last_bootstrapped: <ISO8601 UTC>`,
    `hardening.firewall: enabled`, `hardening.fail2ban: enabled`,
    `hardening.ssh_root_login: disabled`, `hardening.ssh_password_auth: disabled`,
    `hardening.ssh_session_policy` (as step 22 recorded it),
    `hardening.kernel_hardening: enabled`, `hardening.file_integrity: enabled`,
    `hardening.name_responders: disabled`,
    `hardening.tailscale: joined` (`deferred` when the tailnet is off), the `remote_ide` block from the step-29b probe
    (`vscode_server`, `reason`, `probed: <today>`), plus any
    `provisioning.*`/`ssh.*`/`system_facts.*`/`identity.*` not yet written (the
    latter two should already be set from step 14; confirm rather than
    re-detect).
32. Append a journal entry at `nodes/<slug>/journal/YYYY-MM-DD-HHMM-bootstrap.md`
    per the `conventions` skill's `references/journal.md`:
    - frontmatter: `scope: hardening`, `outcome: success` (or `partial`/`failed`),
      `trigger: /bootstrap-node <slug>`, `commit_before`/`commit_after`, actor.
    - `## What was done` — bullet list (provisioning, key, hardening), naming
      the `system_facts` and `identity` discovered in step 14 explicitly (the
      **hash**, never the raw machine id); see `references/journal.md` for the
      phrasing. Also name the forwarding verdict (step 28b), and the integrity
      database path and its `sha256:` hash (step 30b). That hash is the
      off-node record every later integrity check compares against.
    - `## Commands` — every command verbatim, with exit codes.
    - `## Notes / drift detected` — anything surprising.
    - **NEVER include the root password, VM password, `$DOCCAP_TS_KEY`, or
      any private key.** The journal records that secrets were used, not values.
33. Commit and push per `conventions` `references/journal.md` § "Committing":
    ```bash
    git add nodes/<slug>/node.yaml nodes/<slug>/journal/<entry>.md
    git commit -m "node(<slug>): bootstrap"
    git pull --rebase && git push
    ```
    Then bring the node's working copy up to this commit, so its first
    on-node command starts from the bootstrap record:
    `tailscale ssh doccap@<tailscale_hostname> 'git -C ~/<repo> pull --ff-only'`
    (keyed SSH on the fallback port when the tailnet is off).
34. Unset `DOCCAP_TS_KEY` and the root/VM password from the agent's
    environment (each only if it was fetched — a run that failed before Phase 3
    never set `DOCCAP_TS_KEY`).
35. Print a summary: tailscale hostname, hardened endpoint
    (`doccap@<initial-ip>:<ssh_fallback_port>`), suggested Remote-SSH target
    (`doccap@<tailscale_hostname>`), key path (`~/.ssh/<key>`),
    `remote_ide.vscode_server` verdict, and the recommended next surface:
    - `compatible` → "Connect via VS Code Remote-SSH to
      `doccap@<tailscale_hostname>` and run `/create-use-case` there (preferred)."
    - `incompatible`/`unknown` → "Drive use-cases from this laptop with the node
      as the first arg, e.g. `/create-use-case <node> <slug> …`."
    Next command either way: `/list-use-cases`.

## Failure handling

Within Phases 1–3, any non-zero exit halts with `outcome: partial`. The journal
entry is still written and committed. No automatic rollback. To recover: read
the journal, fix the cause, re-run `/bootstrap-node <slug>` — probe-derived
steps skip what's done and the failed step re-attempts. No flag is needed; the
audit-&-repair prompt only appears once `last_bootstrapped` is set.

## Recovery checklist (when a cut-over verification fails)

The prior session (initial root in Phase 2, or doccap-on-fallback in Phase 4)
is still alive. Common causes:

- Phase 2: the dual-port move leaves the box on port 22 — connect there as root
  or as doccap (key) and check, in order: (1) if `system_facts.mac_framework`
  is a value step 20a permits the port for, that permit is in place (re-run
  step 20a if not); (2) the node's firewall allows the fallback port;
  (3) the SSH service reloaded cleanly —
  the unit is not named the same everywhere, so probe which one is active
  (`systemctl list-units --type=service | grep -E '^(ssh|sshd)\.service'`)
  before checking `systemctl status`/`journalctl -u` on it — and its listening
  port is correct. Probe/fix commands: `hardening-recipes.md` §§ "SSH lockdown
  ordering", "Firewall".
- Phase 4: `tailscale up` succeeded but the device isn't routable yet — wait 30s
  and re-run (step 26's status poll can race). Or the firewall blocked the port.

Fix from the live session, then re-run `/bootstrap-node <slug>`.

## What this command does NOT do

- Require a VS Code Remote-SSH connection — everything runs from the admin's client.
- Write any secret value to the node disk beyond the `authorized_keys` install
  and the node's own fork deploy key, which is generated on the node and never
  leaves it. Passwords and tokens are consumed in-memory; the admin's private
  key never leaves the laptop/KeePass.
- Adopt an existing, externally-configured box — that is `/onboard-node`.
- Configure use-cases. The next surface is whichever step 35 prints.
