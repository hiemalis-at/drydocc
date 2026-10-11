---
name: provision-vm
description: Provision a fresh qemu VM — rootless where the host allows it, for the host's own CPU architecture or an emulated one (e.g. aarch64 on an x86 laptop). Pick a server image (a known distro's latest release, or a custom ISO URL); downloads it, renders its unattended-install config, installs unattended, and leaves the box SSH-reachable. Use when a node should be a local VM. Run /bootstrap-node next to harden it.
compatibility: Runs inside a drydocc fork; needs a shell, file edits and web access.
metadata:
  skillset: drydocc
  requires: conventions secrets node-context
---

# /provision-vm

## Contract

- **Args:** `<slug>` (required; kebab-case node name) + optional free-text description.
- **Runs on:** the admin's client (local laptop), NOT the node.
- **Resumable & idempotent:** probes for an existing VM and `provisioning.status:
  provisioned` before doing any work. A re-run after failure resumes cleanly.
- **Reads:** `nodes/<slug>/node.yaml` (if it already exists).
- **Writes (in the repo):**
  - Scaffolds `nodes/<slug>/` (`node.yaml`, `journal/`, `use-cases/`) if absent.
  - Updates `node.yaml:provisioning.*` on success.
  - Appends one journal entry (`scope: node`).
- **Writes (local):** downloads the server image into `iso/` (gitignored).
  Renders its unattended-install config into a gitignored temp dir (0600,
  deleted after use).
- **Scope:** see `## What this command does NOT do`.

## Prerequisites

- The laptop is a qemu host (KVM acceleration for its own architecture;
  emulation for any other) — see `references/vm-recipes.md` § "Host
  preflight" for the exact tooling and access checks.
- `iso/` is gitignored; it will be created automatically.
- Sufficient disk space for the server image (~900 MB–4 GB) and the VM image.

## Procedure

Consult the `references/vm-recipes.md` for every recipe and snippet in this command.

### Phase 0 — Bind and scaffold

1. Validate `<slug>` matches `^[a-z][a-z0-9-]{0,30}[a-z0-9]$`. Refuse with
   instructions if not.

2. **If `nodes/<slug>/` exists and `provisioning.status: provisioned`:**
   Ask the admin — re-provision (destroy + rebuild) or exit?
   `[decision: provision-vm.reprovision]` If re-provision,
   run teardown from `references/vm-recipes.md` § "Teardown", reset `provisioning.status:
   none`, then continue to Phase 1. If exit, print the recorded
   `provisioning.reach` SSH command and suggest `/bootstrap-node <slug>` to
   continue.

3. **If `nodes/<slug>/` is absent:** scaffold it now —
   ```
   nodes/<slug>/
     node.yaml        ← stub (see below)
     journal/
     use-cases/
   ```
   Stub `node.yaml`:
   ```yaml
   name: <slug>
   created: <today YYYY-MM-DD>
   provisioning:
     source: vm
     status: none
   hardening:
     firewall: unknown
     fail2ban: unknown
     ssh_root_login: unknown
     ssh_password_auth: unknown
     tailscale: unknown
   last_bootstrapped: null
   ```

4. No session secrets are needed for this command (no tailscale or GH token
   yet). The only secret here is the generated VM root password (`DOCCAP_ROOT_PW`) —
   see `secrets` § "VM initial password".

### Phase 1 — Provision VM

Consult `references/vm-recipes.md` for every command in this phase.

5. **Resume probe:** consult `references/vm-recipes.md` § "Resume probe". If
   `provisioning.status: provisioned` and the domain exists, skip to Phase 2.

5b. **Host preflight** (consult `references/vm-recipes.md` § "Host preflight"):
   derive the libvirt connection — the rootless per-user one wherever it
   works, so nothing below needs `sudo`; the system one only as a fallback —
   then verify the qemu/libvirt toolchain is present and whether `/dev/kvm`
   exists. Only for the system connection: the libvirt daemon and default NAT
   network are active and the libvirt qemu user can read the ISO/image paths
   (the recipe carries the access-repair steps). Halt on missing tooling.

6. **Collect VM specs** from the admin `[decision: provision-vm.specs]`:
   - RAM (`memory_mb`), vCPUs, disk size (`disk_gb`).
   - Guest CPU architecture (default: the host's own). Another architecture
     runs emulated — far slower, see `references/vm-recipes.md` § "Guest
     architecture and acceleration" for what it changes and how long to
     expect.
   - VM name (default: `<slug>`).

7. **Image prompt** `[decision: provision-vm.image]` — ask the admin which server image to install: a known
   distro's latest server release, or a custom ISO URL — built for the guest
   architecture from step 6. Whichever is chosen,
   the **installer framework** the image uses is derived from the image
   itself, never assumed from what the admin typed — see
   `references/vm-recipes.md` § "OS choice and ISO cache contract" for the
   detection probe, asking the admin only when it's inconclusive.

8. **Resolve + download the image** (consult `references/vm-recipes.md`
   § "OS choice and ISO cache contract"):
   - For a known distro's release, `WebSearch` for the latest point-release
     filename + URL and HEAD-check it (`curl -fsIL`). If the search fails or
     the URL doesn't resolve, use the recipe's pinned known-good fallback and
     warn the admin. For a custom URL, the filename is its `basename`.
   - `mkdir -p iso`
   - If `iso/<filename>` exists → use it.
   - If not → `curl -L --progress-bar -o iso/<filename> <download-url>` (show
     progress to the admin; this may take several minutes).
   - Verify it against the publisher's checksum file when one exists, and
     check the image's own architecture metadata against step 6 — both in
     the recipe. Never install from a mismatch.

9. **Generate initial root password** (consult `secrets` § "VM initial
   password"): `openssl rand -base64 18`, with a `python3` fallback when
   `openssl` is absent (see `references/vm-recipes.md` § "Initial root
   password"). In-memory only; never journaled.

10. **Render the unattended install config** into a gitignored temp path,
    dispatched on the installer framework step 7 derived —
    `references/vm-recipes.md` holds one render template per framework;
    consult it rather than assuming a name-to-framework mapping. Every
    template embeds the guest-agent package and any MAC-framework port
    pre-label the later SSH lockdown (Phase 2 of `/bootstrap-node`) needs;
    how the rendered config reaches the installer (an injected file vs. a
    built seed image) is framework-specific and documented alongside each
    template.

11. **`virt-install`** headless unattended (consult `references/vm-recipes.md` § "virt-install"
    — use the per-framework source/config-delivery table verbatim):
    - Create disk image with `qemu-img create`.
    - **Always boot the installer with `--location <iso>`, never `--cdrom`** —
      `--extra-args` is silently dropped with `--cdrom` (the test-02 silent-hang
      trap). See the callout in `references/vm-recipes.md` § "virt-install".
    - Headless but observable: `--graphics none`, a serial console with a
      log file, the guest architecture's own console device as a kernel arg,
      and the architecture's video device where its row requires one —
      without it a healthy installer looks hung. Exact flags per architecture
      in `references/vm-recipes.md` § "Guest architecture and acceleration".
    - The guest NIC and its host-side reach follow the connection from step
      5b — see `references/vm-recipes.md` § "Host-side reach".
    - The exact per-installer-framework `--extra-args` string lives only in
      `references/vm-recipes.md` § "virt-install" — use it verbatim rather
      than retyping it here. Keeping one copy avoids the drift risk of
      multiple installer-flag strings going out of sync as each framework
      evolves its own unattended-install syntax across image releases.

12. **Wait for install + reachability** (consult `references/vm-recipes.md` § "Wait for install + reachability"):
    - Poll the reach address until SSH answers — polls are scaled for an
      emulated guest, which is slow rather than stuck.
    - Surface progress to the admin; the serial log shows what the installer
      is doing.

### Phase 2 — Persist and finalize

13. **Write `provisioning.*` to `node.yaml`** (consult `references/vm-recipes.md` § "Persist +
    converge"):
    ```yaml
    provisioning:
      source: vm
      hypervisor: qemu-kvm
      libvirt_uri: <connection from step 5b>
      arch: <guest architecture from step 6>
      vcpus: <VCPUS>
      memory_mb: <MEMORY_MB>
      disk_gb: <DISK_GB>
      base_image: <filename>
      vm_name: <VM_NAME>
      reach: {host: <address>, ports: {22: <host port>, <fallback port>: <host port>}}
      status: provisioned
    ```

14. **Clean up temp dir** (`rm -rf "$TMP"`). The initial root password is held
    in memory for the summary only and unset immediately after.

15. **Append a journal entry** (`scope: node`, `outcome: success`; commands
    verbatim — **no root password value, no ISO path contents**).
    ```
    nodes/<slug>/journal/<YYYY-MM-DD-HHMM>-provision-vm.md
    ```

16. `git add nodes/<slug>/ && git commit -m "feat(node): provision VM <slug>" && git push`

17. **Print summary:**
    ```
    VM <slug> is ready.
    Reach: <reach.host>, port <reach.ports[22]>
    SSH:   ssh -p <reach.ports[22]> root@<reach.host>   (password: <shown once, then forgotten>)

    Next: /bootstrap-node <slug>
          Phase 1 will be skipped (provisioning.status: provisioned).
          Phase 2 onwards: SSH key setup, hardening, tailscale.
    ```
    Unset `DOCCAP_ROOT_PW` from shell memory after printing.

## What this command does NOT do

- Set up SSH keys, create `doccap`, harden, or join the tailnet — all of
  that is `/bootstrap-node`.
- Provision anywhere but the admin's own qemu host. There is no cloud
  provider integration; a rented vServer enters at `/bootstrap-node` with
  `source: running`.
- Commit the server image or the rendered unattended-install config. Both
  are gitignored, and the config — which embeds the initial root password —
  is deleted after use.
- Destroy an existing VM unasked. Teardown runs only when the admin chooses
  re-provision in Phase 0.
