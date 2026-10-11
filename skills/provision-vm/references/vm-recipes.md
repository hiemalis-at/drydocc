
# `drydocc` VM Provisioning

Reference material for `/provision-vm` and for `/bootstrap-node` **Phase 1**
when the admin chose `source: vm`. The commands orchestrate; this
skill supplies the qemu recipes. Scope is deliberately minimal: a VM that the
host can SSH into, provisioned rootless wherever the host allows it, for the
host's own CPU architecture or an emulated one. No bridged/public networking —
the node is tailscaled later in the shared hardening phase. Output is a running
box reachable over SSH with a known **initial root password**, matching the
`source: running` precondition for Phase 2 of `/bootstrap-node`.

## Contents

- [Host preflight](#host-preflight-run-first-on-the-admin-laptop--qemu-host) — libvirt connection, tooling, KVM, system-connection checks, qemu image access, local MAC-framework check
- [Guest architecture and acceleration](#guest-architecture-and-acceleration) — native vs emulated guest, firmware, console, poll scaling
- [Resume probe](#resume-probe-run-this-first) — detect an already-provisioned or orphaned domain
- [OS choice and ISO cache contract](#os-choice-and-iso-cache-contract) — image prompt, latest-release resolution, ISO download, installer-framework detection
- [Initial root password](#initial-root-password) — generate `DOCCAP_ROOT_PW` (openssl + python3 fallback)
- [Unattended install seed](#unattended-install-seed) — installer-framework outcome contract + observed seed templates
- [Host-side reach](#host-side-reach) — how the host reaches the VM before the tailnet exists
- [virt-install (headless, unattended)](#virt-install-headless-unattended) — the `--location` callout + per-framework flags
- [Wait for install + reachability](#wait-for-install--reachability) — restart a shut-off domain, poll SSH on the reach address
- [Persist + converge](#persist--converge)
- [Teardown (for clean re-provision)](#teardown-for-clean-re-provision)
- [Failure handling / recovery checklist](#failure-handling--recovery-checklist) — symptom → cause → fix table
- [Secret-safety](#secret-safety)
- [What this skill does NOT do](#what-this-skill-does-not-do)

## Host preflight (run first, on the admin laptop / qemu host)

Verify the qemu/libvirt host is usable *before* downloading ISOs or rendering
configs. The first test run also hit the classic libvirt gotcha: the qemu user
could not read the repo's `iso/` dir, so `virt-install` failed silently. This
preflight picks the libvirt connection, then checks tooling, KVM, **and** —
for the system connection — the network and qemu's access to the image paths.

### Libvirt connection

**Goal.** A libvirt connection the invoking user can drive **without `sudo` or
a polkit prompt**, so a provision can run unattended, falling back to one that
needs elevated access only when no rootless one works.

**Fact.** `provisioning.libvirt_uri` — persisted, because the resume probe,
teardown and every later `virsh` call must reach the same connection the domain
was defined on (a domain defined on one connection is invisible on the other).

**Derive.** Prefer the per-user **session** connection: it runs qemu as the
invoking user, so there is no daemon to enable, no group to join, no ACL on
`iso/`, and no `sudo` anywhere. It needs a user-mode network backend for the
guest NIC (§ "Host-side reach"). Fall back to the **system** connection only
when the session connection cannot be opened or no user-mode backend exists:

```bash
if virsh -c qemu:///session uri >/dev/null 2>&1 && command -v passt >/dev/null; then
  LIBVIRT_URI=qemu:///session
  VM_IMAGES="$HOME/.local/share/libvirt/images"     # user-owned; no sudo, no ACL
else
  LIBVIRT_URI=qemu:///system
  VM_IMAGES=/var/lib/libvirt/images                 # libvirt-managed pool
fi
mkdir -p "$VM_IMAGES" 2>/dev/null || sudo mkdir -p "$VM_IMAGES"
VIRSH="virsh -c $LIBVIRT_URI"      # every virsh call below goes through this
```

A connection URI other than these two (a remote hypervisor) is out of scope —
see § "What this skill does NOT do".

**Verification.** `$VIRSH list --all` succeeds. For the session connection it
must succeed **without** `sudo`; if it only works with `sudo`, it is not the
session connection that answered — re-run the derivation.

**Record.** `provisioning.libvirt_uri` in § "Persist + converge". Every `virsh`
call in this file is written as `$VIRSH`, and every `virt-install` call carries
`--connect "$LIBVIRT_URI"`.

#### Observed dialects — evidence, not the supported list

| connection | user-mode NIC backend | elevated access needed | verified on |
|---|---|---|---|
| `qemu:///session` | passt (port forwards bound to a chosen loopback address) | none | WSL2 Fedora 44 host, QEMU 10.2, libvirt session, 2026-10 (x86_64 KVM and aarch64 TCG guests) |
| `qemu:///system` | default NAT network (`virbr0`) | `libvirt` group or `sudo` per call | test-01/test-02, 2026-06 |

### Tooling and acceleration

```bash
for t in virsh virt-install qemu-img genisoimage curl; do
  command -v "$t" >/dev/null || echo "MISSING: $t"
done
# KVM acceleration — only matters for a guest of the host's own architecture
# (§ "Guest architecture and acceleration")
[ -e /dev/kvm ] || echo "WARN: /dev/kvm absent — every guest runs emulated (slow)"
```

### System connection checks (`qemu:///system` only)

Skip this subsection, and the qemu-access and MAC-framework subsections below
it, when `LIBVIRT_URI=qemu:///session` — none of them apply to a qemu process
running as the invoking user. (They were written for the system connection;
the test-02 host required `sudo` for every `virsh` call there.)

```bash
# libvirt group membership — qemu:///system needs it, else virsh needs sudo
if ! id -nG | grep -qw libvirt && ! id -nG | grep -qw libvirtd; then
  echo "WARN: $USER not in libvirt group — virsh against qemu:///system needs sudo."
  echo "      Fix: sudo usermod -aG libvirt $USER && re-login (or prefix virsh with sudo)."
fi

# libvirt daemon
systemctl is-active libvirtd >/dev/null || sudo systemctl enable --now libvirtd

# default NAT network: define it if missing (a stale virbr0 with no libvirt net
# definition makes `net-start default` fail with "Network not found" — test-02).
if ! $VIRSH net-info default >/dev/null 2>&1; then
  ip link show virbr0 >/dev/null 2>&1 && sudo ip link delete virbr0 2>/dev/null || true
  sudo $VIRSH net-define /usr/share/libvirt/networks/default.xml 2>/dev/null \
    || echo "WARN: default.xml not found — define the default NAT network manually"
fi
# active + autostarting
$VIRSH net-info default 2>/dev/null | grep -q 'Active:.*yes' || sudo $VIRSH net-start default
$VIRSH net-info default 2>/dev/null | grep -q 'Autostart:.*yes' || sudo $VIRSH net-autostart default
```

**qemu user access to the image + ISO paths.** libvirt's qemu process (user
`qemu`/`libvirt-qemu`, depending on distro) must be able to read both the base
ISO and the seed ISO and read/write the disk image. The simplest robust choice
is to keep VM images and the seed ISO under a libvirt-owned directory; if you
instead serve the base ISO from the repo's gitignored `iso/`, ensure qemu can
traverse and read it:

```bash
# make the iso/ dir + file readable+traversable by the qemu user (group or ACL)
sudo setfacl -m u:qemu:--x "$PWD" "$PWD/iso" 2>/dev/null \
  || sudo setfacl -m u:libvirt-qemu:--x "$PWD" "$PWD/iso" 2>/dev/null || true
sudo setfacl -R -m u:qemu:r-X "$PWD/iso" 2>/dev/null \
  || sudo setfacl -R -m u:libvirt-qemu:r-X "$PWD/iso" 2>/dev/null || true
```

### Local MAC-framework check

The ACL step above grants a POSIX permission; on a host running a mandatory
access control framework that permission can still be vetoed, which shows up as
`virt-install` failing to read the ISO/disk with "permission denied" even after
the ACL succeeded. This is **host-local, ephemeral state** — it describes the
admin's own laptop, not the node being provisioned, so none of it is a
`system_facts` fact and nothing here is written to any `node.yaml` (contrast
with the node-side probe in `onboard-node/references/onboard-recipes.md`, which
*does* persist `system_facts.mac_framework` because it describes the box the
framework will manage afterward).

**Goal.** qemu can read the base ISO, the seed ISO, and the disk image path,
regardless of what — if anything — is enforcing on this laptop.

**Fact.** Not a `system_facts` key. Probe what's actually enforcing on this
host and act on it directly; there is nothing to read back from a prior run.

```bash
# $HOST_MAC_PROBES: newline-separated "<value>|<status-cmd>|<enforcing-regex>|
# <relabel-idiom>" quadruples, filled from § "Observed dialects" below (or
# freshly derived and appended there) — same probe-then-act shape as the
# node-side mac_framework loop in `onboard-node/references/onboard-recipes.md`,
# applied locally instead of over SSH.
HOST_MAC_FOUND=""
while IFS='|' read -r MAC_VAL MAC_CMD MAC_RE MAC_FIX; do
  [ -n "$MAC_VAL" ] || continue
  MAC_OUT="$(eval "$MAC_CMD" 2>/dev/null)"
  if printf '%s' "$MAC_OUT" | grep -qiE "$MAC_RE"; then
    echo "host_mac_framework=$MAC_VAL — applying its relabel idiom"
    eval "$MAC_FIX" || echo "WARN: relabel failed — falling back to the managed-pool path below"
    HOST_MAC_FOUND=1
    break
  fi
done <<HOSTMACPROBE
$HOST_MAC_PROBES
HOSTMACPROBE
[ -n "$HOST_MAC_FOUND" ] || echo "host_mac_framework=none — no relabel needed"
```

**Deriving an unseen framework.** A local MAC framework worth acting on has two
properties: (a) a status query that distinguishes *enforcing* from
permissive/disabled — never a process grep, the same rule the node-side probe
follows — and (b) either a per-path relabel command or a pre-trusted directory
its own default profile already covers. Check the framework's own status tool
first; if it reports enforcing, look for its relabel/label command before
assuming none exists. If it is enforcing with no relabel path, or there is no
candidate to try in the first place, the managed-pool fallback below is the
whole answer — don't invent a bespoke exception for a framework nobody here has
verified yet.

**Verification.** Retry the exact access the ACL step was meant to grant:

```bash
sudo -u qemu test -r "iso/$FILENAME" 2>/dev/null && echo ok \
  || sudo -u libvirt-qemu test -r "iso/$FILENAME" 2>/dev/null && echo ok \
  || echo "still denied — fall back to the managed-pool path"
```

A framework that reports its relabel succeeded but still denies the read is not
verified — treat it as `none` for planning purposes and use the fallback below,
never trust the report alone.

If access still can't be granted after the applicable idiom — or there was no
candidate to try — fall back to placing the base ISO under
`/var/lib/libvirt/images/`, a libvirt-managed pool location every local MAC
framework's own default profile already trusts. A libvirt-managed pool labels
new images automatically, which is why it's the simplest fix when the ACL/MAC
dance above gets complicated.

**Record.** Nothing persists — this is host-local, ephemeral state, unlike
`system_facts`, which describes the node. Note the applied idiom in the
preflight's own console output for the admin; append a newly-verified idiom to
Observed dialects below when one is derived.

#### Observed dialects — evidence, not the supported list

| value | status command | enforcing regex | relabel idiom | verified on |
|---|---|---|---|---|
| selinux | `getenforce 2>/dev/null` | `^Enforcing$` | `sudo chcon -t virt_image_t -R iso/ ~/.local/share/libvirt/images/` (or use a libvirt-managed pool, which labels automatically) | test-01/test-02 (Fedora host) |
| apparmor | `aa-status --enabled 2>&1; echo $?` | `^0$` | libvirt's own abstraction already covers `/var/lib/libvirt/images/`; a home/repo path is **not** covered by it — skip the relabel attempt and use the managed-pool path instead | not yet independently verified anywhere in this repo — confirm before trusting |

## Guest architecture and acceleration

**Goal.** Install a guest of the CPU architecture the admin asked for — the
host's own by default, or an emulated one (an SBC rehearsed on a laptop) —
with firmware, console and timeouts that actually fit that architecture, so a
slow emulated install is never mistaken for a hang.

**Fact.** `provisioning.arch` — the guest architecture requested at provision
time, in `uname -m` spelling. Persisted so a re-provision (or `/rebuild-node`
delegating here) reproduces the same machine. It is a *request*: the node's
probed top-level `arch` (written by `/bootstrap-node` Phase 2) is the evidence,
and the two must agree — see Verification.

**Derive.**

```bash
HOST_ARCH=$(uname -m)
GUEST_ARCH=${GUEST_ARCH:-$HOST_ARCH}          # admin's choice in SKILL step 6
if [ "$GUEST_ARCH" = "$HOST_ARCH" ] && [ -e /dev/kvm ]; then
  VIRT_TYPE=kvm;  POLL_SCALE=1
else
  VIRT_TYPE=qemu; POLL_SCALE=15               # TCG: full emulation
fi
command -v "qemu-system-$GUEST_ARCH" >/dev/null || echo "MISSING: qemu-system-$GUEST_ARCH"
# From the $GUEST_ARCH row of Observed dialects below (or a derived new row):
ARCH_FLAGS="<machine + firmware flags; plus the CPU flag when VIRT_TYPE=qemu>"
VIDEO="<video flag>"
CONSOLE_TTY="<serial console device>"
```

Per architecture the recipe needs four things — the machine type and boot
firmware, the serial console device the kernel writes to, the video device the
installer's text UI needs, and the CPU model under emulation. Take them from
Observed dialects below. For an architecture with no row, read the
`qemu-system-<arch>` machine list (`-machine help`) for its generic virtual
board, check the host has UEFI firmware for it (`virt-install --boot uefi`
fails loudly when none is installed), and find the serial console name in that
architecture's kernel documentation; verify per below, then add a row.

The installer image must be built for the guest architecture — resolve and
download the image for `$GUEST_ARCH` in § "OS choice and ISO cache contract",
never the host's. Once it is on disk, check the image's own metadata when it
names an architecture (a release-tree descriptor at the image root may carry
an `arch` key — `isoinfo -R -x /.treeinfo -i iso/$FILENAME | grep '^arch'`);
a mismatch with `$GUEST_ARCH` stops the run before any install is attempted.
An image that names no architecture is checked after the install instead
(Verification).

**Verification.** After the install, `uname -m` over the initial SSH session
equals `$GUEST_ARCH`; and the install finished within the scaled wait in §
"Wait for install + reachability". An emulated guest that never reaches SSH
within the scaled wait is a failure to investigate on the serial log (§
"virt-install"), not a reason to raise the scale silently.

**Record.** `provisioning.arch` in § "Persist + converge"; the acceleration
(`kvm` vs emulated) is re-derived on every run and never persisted.

### Observed dialects — evidence, not the supported list

| guest arch | machine + firmware flags | console | video | CPU (emulated) | measured | verified on |
|---|---|---|---|---|---|---|
| `x86_64` | (defaults: q35, BIOS) | `ttyS0` | `--video vga` — **mandatory**: without a video device the installer's text UI has no output backend and a healthy install looks hung (found on test-01, re-confirmed 2026-10) | — | full install ~3 min under KVM | test-01/test-02, 2026-06; WSL2 Fedora 44 host session connection, 2026-10 |
| `aarch64` | `--arch aarch64 --machine virt --boot uefi` (edk2/AAVMF firmware; libvirt picks the qcow2-format firmware + NVRAM template itself) | `ttyAMA0` | `--video none` (no VGA on `virt`; the installer uses the serial console) | `--cpu maximum` | install ~40 min under TCG on a 20-core x86 host; boot to SSH 76 s | WSL2 Fedora 44 host session connection, Fedora Server 44 aarch64 DVD, 2026-10 |

## Resume probe (run this first)

VM creation is the one bootstrap step that can't be safely re-probed
mid-install, so it carries a persisted marker. Before doing anything:

Probe the **domain independently of `node.yaml`** — a fresh worktree may have no
`node.yaml` while the libvirt domain still exists (test-02), and that orphan must
be caught before re-provisioning collides with it.

```bash
# Does the domain exist? (works even when node.yaml is absent)
# LIBVIRT_URI: provisioning.libvirt_uri when node.yaml records one, else the
# connection § "Libvirt connection" derives. A domain is only visible on the
# connection it was defined on, so check the other one too before declaring
# "no domain" — an orphan on the other connection still collides by name/disk.
$VIRSH dominfo "$VM_NAME" >/dev/null 2>&1 && DOMAIN_EXISTS=yes || DOMAIN_EXISTS=no
for OTHER in qemu:///session qemu:///system; do
  [ "$OTHER" = "$LIBVIRT_URI" ] && continue
  virsh -c "$OTHER" dominfo "$VM_NAME" >/dev/null 2>&1 \
    && echo "WARN: $VM_NAME exists on $OTHER — orphan on the other connection; offer § \"Teardown\" there"
done
# node.yaml may not exist yet → STATUS may be empty
STATUS=$(yq '.provisioning.status' nodes/$SELF/node.yaml 2>/dev/null)

if [ "$DOMAIN_EXISTS" = yes ] && [ "$STATUS" = provisioned ]; then
  echo "VM $VM_NAME already provisioned — skip to Phase 2"
fi
```

- domain exists **and** `status: provisioned` → skip to Phase 2.
- domain exists but `status` ≠ `provisioned` (**including a missing `node.yaml`**)
  → an install was interrupted or the domain is orphaned; offer the admin the
  § "Teardown" recipe and start clean.
- no domain → fresh provision.

## OS choice and ISO cache contract

### Image choice prompt

Ask the admin which server image to install:

1. **A known distro's latest server release** — the admin names any distro;
   § "Resolve latest release" below resolves the current point-release filename
   and URL at run time. Naming one that hasn't been resolved here before just
   means the web search does real work instead of hitting a pinned fallback —
   nothing about this prompt closes the list to distros this file has already
   seen.
2. **A custom URL** — the admin provides a direct download link; the filename
   is the URL's `basename`.

Whichever is chosen, **do not** ask the admin which installer framework the
image uses, and do not infer it from the distro name typed above — §
"Installer framework detection" below derives it from the downloaded image
itself once it's on disk. The admin is asked only if that derivation is
genuinely inconclusive.

Record the chosen image name in `node.yaml:provisioning.base_image`.

### Resolve latest release (web search, pinned fallback)

ISO versions go stale fast, so for a known distro's release **do not
hardcode** a version — resolve the current point release at run time:

1. `WebSearch` for the latest server ISO of the named distro **built for
   `$GUEST_ARCH`** (§ "Guest architecture and acceleration"), e.g. *"latest
   \<distro\> server iso \<arch\> download filename"*. Parse the current
   point-release **filename** and **download URL** from the results. An image
   built for another architecture installs nothing useful — the filename
   usually carries the architecture token; check it before downloading.
2. HEAD-check that the URL resolves before committing to it:

   ```bash
   curl -fsIL "$DOWNLOAD_URL" >/dev/null && echo "url ok" || echo "url failed"
   ```

3. **Fallback** — if the search yields nothing usable or the HEAD check fails,
   use the pinned known-good filename/URL for the named distro from § "Observed
   dialects" below, if one exists there, and **warn the admin** that a stale pin
   is in use (search failed). If no pinned fallback exists for this distro
   either, ask the admin for a direct URL instead — equivalent to falling
   through to the Custom URL choice.

For **Custom URL**, skip the search entirely: the filename is the URL
`basename` and the admin-provided URL is used directly.

Set `os-variant` for `virt-install` to the closest match `virt-install --osinfo
list` reports for the resolved distro + version. A brand-new release is often
missing from the host's osinfo-db (§ "Observed dialects" below has a worked
case) — use the closest older variant, or `--osinfo detect=on,require=off` when
nothing is close enough to guess.

#### Observed dialects — evidence, not the supported list

| distro | pinned fallback filename | fallback URL | closest verified `--osinfo` | verified on |
|---|---|---|---|---|
| ubuntu | `ubuntu-24.04.2-live-server-amd64.iso` | `https://releases.ubuntu.com/24.04/ubuntu-24.04.2-live-server-amd64.iso` | `ubuntu24.04` | test-02, 2026-06 |
| fedora | `Fedora-Server-dvd-x86_64-44-1.7.iso` | `https://download.fedoraproject.org/pub/fedora/linux/releases/44/Server/x86_64/iso/Fedora-Server-dvd-x86_64-44-1.7.iso` | `fedora43` on test-02; `fedora-unknown` on a host whose osinfo-db stops at 41 (see "Failure handling") | test-02, 2026-06; session connection, 2026-10 |
| fedora (aarch64) | `Fedora-Server-dvd-aarch64-44-1.7.iso` | `https://download.fedoraproject.org/pub/fedora/linux/releases/44/Server/aarch64/iso/Fedora-Server-dvd-aarch64-44-1.7.iso` (checksum file `Fedora-Server-44-1.7-aarch64-CHECKSUM` alongside) | `fedora-unknown` | session connection, TCG, 2026-10 |

### ISO download

Server images live under `iso/` at the fork root (gitignored — large binaries).

```bash
mkdir -p iso
FILENAME="<derived from image choice>"
DOWNLOAD_URL="<derived from image choice or admin-provided>"

if [ -f "iso/$FILENAME" ]; then
  echo "ISO already cached: iso/$FILENAME — skipping download."
else
  echo "Downloading $FILENAME (~1–4 GB, may take a while)…"
  curl -L --progress-bar -o "iso/$FILENAME" "$DOWNLOAD_URL"
fi
ISO_SHA256=$(sha256sum "iso/$FILENAME" | cut -d' ' -f1)
```

When the publisher ships a checksum file next to the image, fetch it and
compare it with `$ISO_SHA256` before going further; a mismatch means a
truncated or tampered download — delete it and download again, never install
from it. Note `$ISO_SHA256` in this run's journal entry either way: it, not
the filename, is what identifies the image (a re-spun release can reuse a
filename).

### Installer framework detection

**Goal.** Know which installer framework the downloaded image uses *before*
rendering any seed, so § "Unattended install seed" picks the config format and
delivery mechanism the image will actually honor — never guessed from the
distro name typed at the prompt above.

**Fact.** `$INSTALLER_FRAMEWORK` — **not** a `system_facts` key. Per the `conventions` skill's
`references/distro-agnosticism.md` Rule 6 this is a one-shot value describing the *install medium*, never re-read
once the box exists: the node's own `system_facts` are filled by
`/bootstrap-node` Phase 2's live probe against the *running* box, not the ISO
that installed it. Probe the file that just landed in `iso/`:

```bash
# List the image's ISO 9660 tree directly — no root needed, so this is the
# default (an unattended run must never stop at a sudo password prompt). Fall
# back to a read-only loopback mount only when isoinfo is absent, and only
# with non-interactive sudo:
MOUNT="$TMP/iso-probe"; mkdir -p "$MOUNT"; MOUNTED=no
if command -v isoinfo >/dev/null; then
  TREE="$(isoinfo -R -l -i "iso/$FILENAME" 2>/dev/null)"
  BOOTCFG="$(for c in /isolinux/isolinux.cfg /EFI/BOOT/grub.cfg /boot/grub/grub.cfg; do
               isoinfo -R -x "$c" -i "iso/$FILENAME" 2>/dev/null; done)"
  MANIFEST="$(isoinfo -R -x /casper/filesystem.manifest -i "iso/$FILENAME" 2>/dev/null)"
elif sudo -n mount -o loop,ro "iso/$FILENAME" "$MOUNT" 2>/dev/null; then
  MOUNTED=yes
  TREE="$(find "$MOUNT" -maxdepth 2 -mindepth 1)"
  BOOTCFG="$(cat "$MOUNT"/isolinux/isolinux.cfg "$MOUNT"/EFI/BOOT/grub.cfg "$MOUNT"/boot/grub/grub.cfg 2>/dev/null)"
  MANIFEST="$(cat "$MOUNT"/casper/filesystem.manifest 2>/dev/null)"
fi

# $IMG_PROBES: newline "<value>|<tree-marker-regex>|<bootcfg-kernel-arg-regex>|<alt-signal-regex>"
# quadruples, filled from § "Observed dialects" below (or freshly derived and
# appended there). The 4th field is optional — a fallback second signal
# checked against a live-filesystem manifest (e.g. `casper/filesystem.manifest`)
# when the bootloader kernel-arg regex doesn't match (a live-server-shaped
# image's default menu is interactive by convention and carries no pre-baked
# answer-file arg — the admin adds it themselves, so its *absence* is not
# evidence against the framework). A candidate must match the tree marker
# PLUS (the bootloader marker OR the alt marker) — see Verification.
IMG_FOUND=""
while IFS='|' read -r IMG_VAL IMG_TREE_RE IMG_CFG_RE IMG_ALT_RE; do
  [ -n "$IMG_VAL" ] || continue
  TREE_HIT=""; printf '%s\n' "$TREE"    | grep -qE "$IMG_TREE_RE" && TREE_HIT=1
  CFG_HIT="";  printf '%s\n' "$BOOTCFG" | grep -qE "$IMG_CFG_RE"  && CFG_HIT=1
  if [ -z "$CFG_HIT" ] && [ -n "$IMG_ALT_RE" ]; then
    printf '%s\n' "$MANIFEST" | grep -qE "$IMG_ALT_RE" && CFG_HIT=1
  fi
  if [ -n "$TREE_HIT" ] && [ -n "$CFG_HIT" ]; then
    echo "installer_framework=$IMG_VAL"
    INSTALLER_FRAMEWORK="$IMG_VAL"
    IMG_FOUND=1
    break
  fi
done <<IMGPROBE
$IMG_PROBES
IMGPROBE
[ -n "$IMG_FOUND" ] || echo installer_framework=unknown
[ "$MOUNTED" = yes ] && sudo -n umount "$MOUNT"
```

**Deriving an unseen dialect.** An installer framework leaves two independent
kinds of evidence on its own image: a manifest file whose *format* identifies
the installer generation (an ini-style release-tree descriptor is one shape; a
live-filesystem manifest naming its own installer is another), and the
bootloader config (`isolinux.cfg`, `grub.cfg`) whose existing interactive menu
entry already passes the machine-readable-answer-file kernel argument — that
argument's *name* is the framework's own convention (a `…=file:…`-shaped
pointer, a `…ds=nocloud…`-shaped pointer, and an `…auto=true…`-shaped pointer
are the three conventions seen so far; see Observed dialects for the exact
regexes). Read the manifest first; if nothing recognised turns up, grep the
bootloader config for a kernel argument that looks like "point at an answer
file" and read that framework's own documentation for what it expects and for
how to deliver the answer file (an injected file vs. a second attached volume
— § "virt-install" documents the delivery shape alongside each verified row).
Add a row to Observed dialects once verified per below.

If the image offers **no** unattended path at all — no recognised manifest, no
matching kernel argument, and its own documentation doesn't describe one — that
is a genuinely unattended-incapable image. Stop and ask the admin which
Observed-dialects template it most resembles, or for a different image URL;
don't guess a framework the image gives no evidence for.

**Verification.** The tree marker must agree with a second signal — the
bootloader kernel-arg marker, or (when that doesn't match and a row defines
one) its alt-signal fallback. A tree marker alone, with neither second signal
matching, is not enough to render a seed against; treat that as inconclusive
and fall through to the admin per Deriving above. Agreement here only proves
the *identification*;
it does not yet prove the seed will be accepted by the installer — that is
verified separately, by SSH reachability and a partitioned disk, once the
install actually runs (§ "Unattended install seed" → its own Verification).

**Record.** Hold the value in `$INSTALLER_FRAMEWORK` for the rest of this run —
not a `system_facts` field. Note it in this run's provisioning journal entry
alongside `provisioning.base_image` (§ `conventions` skill's
`references/journal.md`). When a probe above derives and verifies a framework
that isn't already in the table below, append the row before moving on.

#### Observed dialects — evidence, not the supported list

| fact value | tree marker | bootloader kernel-arg marker | alt second-signal (when the bootloader marker doesn't match) | verified on |
|---|---|---|---|---|
| autoinstall | a live-filesystem tree (`casper/`) plus a disc-info file naming a live-server image, at the ISO root | `autoinstall ds=nocloud` already present in the shipped grub menu entry | `casper/filesystem.manifest` names `snap:subiquity` — needed on point releases whose default menu is interactive (no pre-baked answer-file arg) | tree+bootloader marker: test-02, 2026-06; tree+manifest fallback (bootloader marker absent): backup-01, 2026-09, Ubuntu 24.04.4 |
| kickstart | a `.treeinfo` file at the ISO root with a `[general]` section | an `inst.`-prefixed installer boot argument (`inst\.(ks|stage2)=`) in the shipped menu — releases without an isolinux menu carry only `inst.stage2=` in `EFI/BOOT/grub.cfg`, never `inst.ks=` | — | test-02, 2026-06 (isolinux, `inst.ks=`); Fedora Server 44 x86_64 + aarch64 DVD, 2026-10 (grub only, `inst.stage2=`) |
| preseed | a disc-info file plus an `install.<arch>/` tree at the ISO root, and **no** `.treeinfo` | `auto=true priority=critical` already present in the shipped isolinux menu entry | — | not yet verified anywhere in this repo — confirm before trusting |

## Initial root password

Generate a strong initial root password. Prefer `openssl`, but fall back to
`python3` (already a hard dependency) when `openssl` is absent — as on the WSL2
host in the test-02 run:

```bash
DOCCAP_ROOT_PW=$(openssl rand -base64 18 2>/dev/null \
  || python3 -c "import secrets,base64; print(base64.b64encode(secrets.token_bytes(18)).decode())")
```

It is an in-memory secret: it goes into the seed config below (written only to a
**gitignored temp dir**, never committed) and is handed to Phase 2 as `DOCCAP_ROOT_PW`.
Never journal it, never commit it. See `secrets` § "VM initial
password".

## Unattended install seed

**Goal.** The image installs unattended and leaves a box where: the initial
access account exists with the generated password, SSH is reachable with that
password (locked down in Phase 2), the disk is **actually partitioned** — not
merely offered the option; an installer that silently skips partitioning when
no disk directive is given is a real failure mode this file has hit, not a
hypothetical one — and, when the installed box's own MAC framework would
otherwise block the fallback SSH port after Phase 2 moves sshd onto it, that
port is pre-labelled so Phase 2's port move isn't the first time anything asks
the MAC framework's permission.

**Fact.** `$INSTALLER_FRAMEWORK`, derived once in § "OS choice and ISO cache
contract" → "Installer framework detection" immediately after download — never
re-derived here.

**Deriving an unseen dialect.** A seed config for a framework with no template
in Observed dialects below still has to satisfy every property in Goal,
regardless of its concrete answer-file syntax:

- an identity/account directive (username + password, in whatever form —
  plaintext or a hash — the framework's own docs accept for unattended access);
- an **explicit disk-target directive** — do not assume a default/empty
  partitioning path exists; one of the templates below exists *because* its
  installer silently never wrote to disk without one;
- an SSH-enable directive, plus whatever the framework's own convention is for
  permitting the initial password login (locked down again in Phase 2);
- the guest-agent package + its enablement, for in-band recovery if SSH never
  comes up;
- a hook — a post-install script, a late-command, whatever the framework calls
  its post-install stage — that pre-labels the fallback SSH port for the
  installed box's own MAC framework, **if and only if** the box will have one.

Render the answer file into the same gitignored temp dir as the templates
below, build whatever delivery artifact the framework expects (an injected file
vs. a seed image — § "virt-install" documents which each framework needs), test
it against a live install, and add the verified result as a new row in
Observed dialects.

**Verification.** SSH answers on port 22 with the generated password within the
wait loop in § "Wait for install + reachability", **and** the disk is
demonstrably partitioned — e.g. `ssh <initial> lsblk` shows more than the bare
device node. A box that comes up reachable but unpartitioned is not a pass; a
malformed disk directive is exactly the failure this Goal calls out.

**Record.** Note which framework's template rendered in this run's journal
entry. Append a new row to Observed dialects when a genuinely new framework was
derived and verified end-to-end — not merely identified; the flags-only entry
below is the cautionary example of the difference.

### Observed dialects — evidence, not the supported list

Two templates below are verified end-to-end (test-02, 2026-06). A third is
carried as flags only — never independently authored or tested — see its own
note.

**`autoinstall` (cloud-init user-data)** — verified on test-02, 2026-06 and
re-verified (template corrected) on backup-01, 2026-09, Ubuntu 24.04.4. Render
to `$TMP/user-data` (0600, gitignored temp dir), plus an empty `$TMP/meta-data`,
and build a seed ISO labelled `cidata`:

```yaml
#cloud-config
autoinstall:
  version: 1
  identity:
    hostname: <slug>
    username: install          # NOT root — curtin's `useradd root --create-home`
                                # fails (exit 9, account already exists); doccap
                                # is created in Phase 2, this account is discarded
    password: "<MKPASSWD_SHA512>"   # openssl passwd -6 "$DOCCAP_ROOT_PW"
  ssh:
    install-server: true
    allow-pw: true            # initial password login; locked down in Phase 2
  packages:
    - qemu-guest-agent        # in-band recovery if SSH fails (no MAC-framework port issue with this framework's default image)
  late-commands:
    - "curtin in-target --target=/target -- usermod --password '<MKPASSWD_SHA512>' root"
    - "echo 'PermitRootLogin yes' > /target/etc/ssh/sshd_config.d/00-initial.conf"
    - "curtin in-target --target=/target -- systemctl enable qemu-guest-agent"
```

Initial SSH access is still as `root` (with the generated password) — only the
*mechanism* for setting it changes: `usermod` **modifies** an existing account
(`root` always exists), so it doesn't hit the `useradd`-on-an-existing-account
conflict `identity.username: root` does. `<MKPASSWD_SHA512>` here is the same
hash as `identity.password` above.

```bash
touch "$TMP/meta-data"
genisoimage -output "$TMP/seed.iso" -volid cidata -joliet -rock \
  "$TMP/user-data" "$TMP/meta-data"

# The seed ISO must sit where the qemu process can read it ($VM_IMAGES, from
# § "Libvirt connection"). Under the session connection qemu runs as the
# invoking user, so a plain copy is enough. Under the system connection $TMP
# (a 0700 `mktemp -d`) is not traversable by the qemu user — copy it into the
# libvirt-managed pool and hand it to that user:
if [ "$LIBVIRT_URI" = qemu:///session ]; then
  install -m 0600 "$TMP/seed.iso" "$VM_IMAGES/$VM_NAME-seed.iso"
else
  sudo cp "$TMP/seed.iso" "$VM_IMAGES/$VM_NAME-seed.iso"
  sudo chown qemu:qemu "$VM_IMAGES/$VM_NAME-seed.iso"
fi
```

**`kickstart`** — verified on test-02, 2026-06. Render to `$TMP/ks.cfg` (0600,
gitignored temp dir):

```ini
text --non-interactive
network --bootproto=dhcp --hostname=<slug>
rootpw --plaintext <DOCCAP_ROOT_PW>      # initial access; locked down in Phase 2
services --enabled=sshd

ignoredisk --only-use=vda     # this installer needs the disk directives below
clearpart --all --initlabel --drives=vda    # explicitly, or it never writes to disk
autopart --type=plain

firstboot --disable
reboot
%packages
openssh-server
qemu-guest-agent              # in-band recovery via virsh qemu-agent-command
policycoreutils-python-utils  # provides the port-label tool for the Phase 2 port label
%end
%post
echo 'PermitRootLogin yes' > /etc/ssh/sshd_config.d/00-initial.conf
systemctl enable qemu-guest-agent
semanage port -a -t ssh_port_t -p tcp ${SSH_FALLBACK_PORT} || true   # pre-label the fallback SSH port so the Phase 2 port move can't be blocked by the installed box's MAC framework (the failure that locked out the first test run)
%end
```

This framework's answer file is delivered with `--initrd-inject` and
referenced from the kernel command line with `inst.ks=file:///ks.cfg` (three
slashes — the canonical absolute `file:` URI; see § "virt-install"). It is
served via `--location` — never `--cdrom` (see the callout in §
"virt-install").

`%packages` resolves **only** against the install medium's own repository —
there is no network repository in this template. A package the medium does
not carry aborts the whole install (`No match for argument: <pkg>`; seen with
`git` on a server DVD, 2026-10). Keep the list to what the template needs;
everything else arrives with `/bootstrap-node`, which has the network.

The same template installs an aarch64 guest under UEFI unchanged: automatic
partitioning creates the EFI system partition itself (session connection,
Fedora Server 44 aarch64 DVD, 2026-10).

**`preseed`** — not yet verified anywhere in this repo. No answer-file content
has ever been authored or tested against a live install; only the `virt-install`
delivery flags exist (§ "virt-install" → Observed dialects), carried forward
because they document a real convention (the injected file must be named
`preseed.cfg`) even though nobody has proven it end-to-end. Confirm before
trusting — author an answer file satisfying every property in Deriving above,
test it, then promote this to a full template like the two above.

## Host-side reach

**Goal.** The host can reach the VM's initial SSH port, and later its fallback
SSH port, before any tailnet exists — on a stable address that never collides
with another VM on the same host.

**Fact.** `provisioning.reach` — persisted, because `/bootstrap-node` Phase 2
connects through it after this command has exited:

```yaml
reach:
  host: <address the host dials>
  ports: {22: <host port>, <ssh_fallback_port>: <host port>}   # guest → host
```

**Derive.** Keyed on the connection (§ "Libvirt connection"):

- **Session connection** — the guest NIC is a user-mode (passt) interface.
  Its guest address is a copy of the host's own and is useless to dial; reach
  it through **port forwards bound to a loopback address of its own**, one
  per VM. An unprivileged user cannot bind host port 22, so guest 22 maps to a
  high host port; the fallback SSH port is forwarded at the **same** number,
  which keeps every later `-p <ssh_fallback_port>` in `/bootstrap-node`
  correct unchanged. Pick the first loopback address in `127.0.10.0/24` that
  no session domain already forwards on and nothing listens on:

  ```bash
  SSH_FALLBACK_PORT=${SSH_FALLBACK_PORT:-42022}
  INITIAL_HOST_PORT=2222
  USED=$($VIRSH list --all --name | while read -r d; do
           [ -n "$d" ] && $VIRSH dumpxml "$d" | grep -o "portForward[^>]*address='[^']*'"
         done | grep -o "address='[^']*'" | cut -d"'" -f2)
  for n in $(seq 2 254); do
    REACH_HOST=127.0.10.$n
    printf '%s\n' "$USED" | grep -qx "$REACH_HOST" && continue
    ss -ltn | grep -q "$REACH_HOST:" && continue
    break
  done
  NET_FLAGS="--network type=user,backend.type=passt,model=virtio,\
portForward0.proto=tcp,portForward0.address=$REACH_HOST,\
portForward0.range0.start=$INITIAL_HOST_PORT,portForward0.range0.to=22,\
portForward0.range1.start=$SSH_FALLBACK_PORT"
  ```

- **System connection** — the guest sits on the default NAT network; the
  address is its DHCP lease (§ "Wait for install + reachability" discovers
  it), ports map to themselves (`{22: 22, <fallback>: <fallback>}`), and
  `NET_FLAGS="--network network=default"`. A lease can change across host
  reboots — re-discover it with `$VIRSH domifaddr` when a recorded address
  stops answering.

**Verification.** The initial SSH port answers on `reach.host` at the mapped
port within the scaled wait (§ "Wait for install + reachability"). For the
session connection, `ss -ltn` on the host shows both forwards bound to
`$REACH_HOST` once the domain is running.

**Record.** `provisioning.reach` in § "Persist + converge"; it is also what
the summary prints as the SSH command.

### Observed dialects — evidence, not the supported list

| connection | reach | verified on |
|---|---|---|
| session (passt) | per-VM loopback address, host 2222 → guest 22, fallback port 1:1; the guest's default gateway maps to host loopback (a host service bound to loopback is reachable from the guest) | WSL2 Fedora 44 host, 2026-10 |
| system (default NAT) | DHCP lease via `domifaddr`, ports 1:1 | test-01/test-02, 2026-06 |

## virt-install (headless, unattended)

> ⚠️ **Always boot the installer with `--location <iso>`, never `--cdrom`.**
> `--extra-args` is honored **only** with `--location`; with `--cdrom` it is
> silently dropped, so the framework's own kernel argument for finding its
> answer file (§ "Unattended install seed" → Observed dialects names each one
> concretely) never reaches the kernel. The installer then falls back to
> **interactive** mode and waits for input — which, under `--noautoconsole`,
> looks exactly like a hang that never writes to disk. This was the test-02
> hang (§ "Unattended install seed" → Observed dialects records which
> framework); the mechanism is framework-independent — every framework's
> answer-file argument breaks the same way under `--cdrom`. The universal
> pattern is **`--location` for the source, `--initrd-inject` for the config
> file, the framework's own kernel arg to find it, and a serial console so a
> headless install is observable.**

```bash
# disk
qemu-img create -f qcow2 "$VM_IMAGES/$VM_NAME.qcow2" "${DISK_GB}G"

# Common flags (every framework, every architecture). The serial console plus
# the console=<tty> kernel argument make the headless install observable, and
# log.file keeps everything the installer printed even when nobody was
# attached — read it first when an install looks stuck. ARCH_FLAGS, VIDEO and
# CONSOLE_TTY come from § "Guest architecture and acceleration"; NET_FLAGS from
# § "Host-side reach". bus=virtio pins the disk name the seed's disk directive
# targets.
virt-install \
  --connect "$LIBVIRT_URI" --virt-type "$VIRT_TYPE" $ARCH_FLAGS \
  --name "$VM_NAME" \
  --memory "$MEMORY_MB" --vcpus "$VCPUS" \
  --disk path="$VM_IMAGES/$VM_NAME.qcow2",format=qcow2,bus=virtio \
  --os-variant <detect via osinfo-query / virt-install --osinfo list> \
  $NET_FLAGS \
  --graphics none $VIDEO \
  --console pty,target_type=serial,log.file="$VM_IMAGES/$VM_NAME-serial.log" \
  --noautoconsole \
  <SOURCE + CONFIG-DELIVERY flags, keyed on $INSTALLER_FRAMEWORK — see Observed dialects below>
```

`--noautoconsole` returns as soon as the domain is created; § "Wait for
install + reachability" does the waiting. Drop it only while debugging a stuck
install interactively.

Per-framework source and config-delivery flags are keyed on
`$INSTALLER_FRAMEWORK` (derived in § "OS choice and ISO cache contract" →
"Installer framework detection"; the matching seed was rendered in § "Unattended
install seed") and appended to the common flags above. The rows below are
evidence of what has been verified, not a closed list — a framework with no row
here needs the same three ingredients any unattended install does: a way to
point the installer at the source (always `--location`, per the callout
above), a way to deliver the answer file (an injected file via
`--initrd-inject`, or a second attached volume when the framework expects to
find it on a device rather than in the initrd), and the framework's own kernel
argument naming where to find it.

### Observed dialects — evidence, not the supported list

**`kickstart`** (`--initrd-inject`, no seed ISO needed):
```bash
  --location iso/<base_image>.iso \
  --initrd-inject "$TMP/ks.cfg" \
  --extra-args "inst.ks=file:///ks.cfg inst.text console=$CONSOLE_TTY,115200n8"
```

**`autoinstall`** (keep the `cidata` seed ISO as a second cdrom; the
live-server-shaped image needs its own live kernel/initrd named on
`--location`):
```bash
  --location iso/<base_image>.iso,kernel=casper/vmlinuz,initrd=casper/initrd \
  --disk path="$VM_IMAGES/$VM_NAME-seed.iso",device=cdrom \
  --extra-args "autoinstall ds=nocloud;s=/cidata/ console=$CONSOLE_TTY,115200n8"
```

(the qemu-accessible copy from § "Unattended install seed" — **not**
`$TMP/seed.iso` directly, which `virt-install` cannot read)

**`preseed`** (flags only — not yet verified against a live install; the
injected file MUST be named `preseed.cfg` for this framework's own convention
to auto-load it from the initrd):
```bash
  --location iso/<base_image>.iso \
  --initrd-inject "$TMP/preseed.cfg" \
  --extra-args "auto=true priority=critical console=$CONSOLE_TTY,115200n8"
```

Use `--osinfo` matching the resolved image — § "Resolve latest release" →
Observed dialects records verified values; for a release newer than the host's
own osinfo-db, use the closest older variant or `--osinfo
detect=on,require=off`. `--graphics none` keeps it headless; the serial
`console=$CONSOLE_TTY,115200n8` arg (plus each framework's own text-mode flag,
where it has one) keeps the installer on a console you can actually attach
to — and `--graphics none` alone is **not** enough on an architecture whose
row in § "Guest architecture and acceleration" requires a video device.

## Wait for install + reachability

The unattended install finishes by rebooting **or powering off** — with
`--noautoconsole` and a reboot/poweroff directive the domain is frequently left
**shut off** (test-02 had to be started by hand). Start it if needed, then poll
the reach address from § "Host-side reach" until SSH answers. Every poll is
scaled by `$POLL_SCALE` (§ "Guest architecture and acceleration"): an emulated
install is slow, not stuck.

```bash
# the domain may be "shut off" after the install — start it before polling
if [ "$($VIRSH domstate "$VM_NAME" 2>/dev/null)" = "shut off" ]; then
  echo "VM $VM_NAME is shut off post-install — starting it."
  $VIRSH start "$VM_NAME"
fi

# system connection only: discover the DHCP lease on the default network
if [ "$LIBVIRT_URI" = qemu:///system ]; then
  for i in $(seq 1 $((120 * POLL_SCALE))); do
    # a still-shut-off domain never gets a lease — restart and keep polling
    [ "$($VIRSH domstate "$VM_NAME" 2>/dev/null)" = "shut off" ] && $VIRSH start "$VM_NAME" 2>/dev/null
    REACH_HOST=$($VIRSH domifaddr "$VM_NAME" 2>/dev/null | awk '/ipv4/{print $4}' | cut -d/ -f1)
    [ -n "$REACH_HOST" ] && break; sleep 5
  done
  INITIAL_HOST_PORT=22
fi

# wait for sshd on the reach address (session: the loopback forward)
for i in $(seq 1 $((180 * POLL_SCALE))); do
  [ "$($VIRSH domstate "$VM_NAME" 2>/dev/null)" = "shut off" ] && $VIRSH start "$VM_NAME" 2>/dev/null
  nc -z -w3 "$REACH_HOST" "$INITIAL_HOST_PORT" && break; sleep 5
done
```

Surface progress to the admin while polling. A session-connection forward
accepts TCP as soon as the domain runs, before sshd is up inside — treat the
port probe as "maybe", and confirm with a real SSH banner (`ssh -o
BatchMode=yes … true` failing on *authentication*, not on connection) before
declaring the VM reachable. While waiting, the serial log
(`$VM_IMAGES/$VM_NAME-serial.log`) shows what the installer is doing.

## Persist + converge

On success, write to `node.yaml`:

```yaml
provisioning:
  source: vm
  hypervisor: qemu-kvm
  libvirt_uri: <LIBVIRT_URI>
  arch: <GUEST_ARCH>
  vcpus: <VCPUS>
  memory_mb: <MEMORY_MB>
  disk_gb: <DISK_GB>
  base_image: <base_image>
  vm_name: <VM_NAME>
  reach:
    host: <REACH_HOST>
    ports: {22: <INITIAL_HOST_PORT>, <SSH_FALLBACK_PORT>: <SSH_FALLBACK_PORT>}
  status: provisioned
```

Then hand `reach.host` (as `INITIAL_IP`), `reach.ports[22]` (as
`INITIAL_PORT`), user `root`, and `DOCCAP_ROOT_PW` to Phase 2 — identical to
the `source: running` path from here on.

## Teardown (for clean re-provision)

Never `undefine --remove-all-storage`: the install image stays attached to the
domain as a cdrom, so that flag **deletes the shared image in `iso/`** along
with the VM's own disk (backup-01, 2026-09). Remove exactly the files this
command created:

```bash
$VIRSH destroy "$VM_NAME" 2>/dev/null
# --nvram: a UEFI domain refuses a plain undefine and keeps its NVRAM file
$VIRSH undefine "$VM_NAME" --nvram 2>/dev/null || $VIRSH undefine "$VM_NAME"
rm -f "$VM_IMAGES/$VM_NAME.qcow2" "$VM_IMAGES/$VM_NAME-seed.iso" \
      "$VM_IMAGES/$VM_NAME-serial.log"
# then reset provisioning.status: none in node.yaml
```

Under the system connection the pool files may be root-owned — prefix the
`rm` with `sudo`. A session domain's loopback forwards disappear with it.

## Failure handling / recovery checklist

Provisioning leaves a persisted marker (`provisioning.status`), so a re-run after
a failure is safe: the resume probe skips a finished install and offers teardown
for an interrupted one. When a step fails, work down this table by symptom. To
observe a stuck install, read `$VM_IMAGES/$VM_NAME-serial.log` first; attach
live with `$VIRSH console "$VM_NAME"` (drop `--noautoconsole` from
`virt-install` while debugging).

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Install looks hung: serial console quiet after early boot, CPU idle, disk image stuck at a few MB | no video device — `--graphics none` without the architecture's video flag leaves the installer's text UI with no output backend, so it waits invisibly | add the video flag from § "Guest architecture and acceleration" → Observed dialects; tear down and re-run |
| Installer aborts: `No match for argument: <pkg>` (serial log) | the answer file's package list names something the install medium's own repository does not carry | drop the package from the template; `/bootstrap-node` installs it later over the network — see § "Unattended install seed" |
| `undefine` → "cannot undefine domain with nvram" | a UEFI domain | `$VIRSH undefine "$VM_NAME" --nvram` — see § "Teardown" |
| A session VM fails to start: "Address already in use" on a forward | another domain or process already holds that loopback address/port | re-run the allocation loop in § "Host-side reach" to pick a free address and redefine the domain |
| Emulated install still running long after a native one would have finished | expected — emulation is roughly an order of magnitude slower | keep waiting within the scaled poll; read the serial log for progress rather than tearing down |
| Install never writes to disk; console sits at an interactive prompt | booted with `--cdrom`, so `--extra-args` (the framework's answer-file kernel argument) was dropped | use `--location <iso>` — see the callout in § "virt-install" |
| Reach poll spins forever; `$VIRSH domstate` shows **shut off** | installer rebooted/powered off and libvirt left the domain stopped | the wait loop `start`s it; if still stuck, `$VIRSH start "$VM_NAME"` by hand |
| `virt-install` fails to read the ISO / disk ("permission denied") | qemu user can't traverse `iso/`; a local MAC framework on the admin's laptop is vetoing the read | re-run the qemu-access ACL step, and the local MAC-framework check, in § "Host preflight" — its Observed dialects has the relabel idiom; or move the ISO under `/var/lib/libvirt/images/` |
| `virt-install` fails to read the **seed** ISO specifically ("permission denied"), base ISO reads fine | seed ISO still lives under its `mktemp -d` (0700, owned by the invoking user) — the system connection's qemu user can't traverse that, distinct from the base-ISO case above | copy it into `$VM_IMAGES` before attaching — see § "Unattended install seed" |
| `virsh net-start default` → "Network not found" | stale `virbr0` with no libvirt net definition | the preflight self-heals (delete bridge → `net-define` → start); see § "Host preflight" |
| every `virsh` call → "failed to connect to the hypervisor" | user not in the `libvirt` group | `sudo usermod -aG libvirt $USER` + re-login, or prefix with `sudo` |
| `virt-install` → "Unknown OS name" / osinfo error | resolved image is newer than the host's own osinfo-db | use the closest older variant or `--osinfo detect=on,require=off` — see § "OS choice and ISO cache contract" |
| `openssl: command not found` during password gen | no `openssl` on the host (e.g. WSL2) | the `python3` fallback covers it — see § "Initial root password" |
| no DHCP lease after several minutes, domain **running** | install genuinely failed mid-way | attach `virsh console`, read the installer log; teardown (above) and re-provision |
| `installer_framework` comes back `unknown` after § "Installer framework detection" | the image's tree marker and bootloader kernel-arg marker disagree, or neither matched | derive and verify a new dialect per that section's guidance and add a row, or ask the admin which Observed-dialects template the image most resembles |

## Secret-safety

- The rendered seed config embeds the initial root password → it lives only in
  a `mktemp -d` gitignored temp dir, `chmod 0600`, deleted after the seed ISO
  (when the framework needs one) is built. Never committed, never journaled.
- The seed ISO (`seed.iso`) carries the same password-bearing content. Its
  `$TMP` original stays in the gitignored temp dir; § "Unattended install
  seed" also places a **transient copy** at `$VM_IMAGES/$VM_NAME-seed.iso`
  (qemu needs to read it from somewhere it can traverse, and `$TMP` isn't
  that place). That copy is outside the repo and not a temp path, so remove it
  once the install completes, alongside the routine `$TMP` cleanup: `rm -f
  "$VM_IMAGES/$VM_NAME-seed.iso"` (`sudo` under the system connection).
- The serial log (`$VM_IMAGES/$VM_NAME-serial.log`) records installer output,
  not the answer file — it carries no password. Keep it until the install is
  verified; § "Teardown" removes it with the VM.
- `iso/` (large server images) is gitignored.

## What this skill does NOT do

- It does not harden the VM — that is Phases 2–3 (`bootstrap-node`).
- It does not support hypervisors other than qemu (KVM-accelerated or
  emulated), nor libvirt connections other than the local session and system
  ones.
- It does not emulate a specific board. An emulated guest runs on the
  architecture's generic virtual machine type; board-specific firmware,
  storage and peripherals are not reproduced.
- It does not configure bridged/public networking — host-local reachability
  only; tailscale comes later.
- It does not execute commands itself — reference material the command reads.
- It does not refuse an image for what distro it claims to be, or for the name
  the admin typed at the image prompt (`distro-agnosticism.md` Rule 4). It refuses only on:
  - an outcome that cannot be **verified** after a genuine derivation attempt —
    `installer_framework` stays `unknown` after both signals in § "Installer
    framework detection" are checked and neither matches, or § "Unattended
    install seed"'s own Verification (SSH reachable **and** the disk actually
    partitioned) fails once a rendered seed's install completes; or
  - a **capability** the goal genuinely requires being absent — the
    qemu/libvirt toolchain itself missing (§ "Host preflight"'s tool loop), or
    the system emulator for the requested guest architecture missing — not
    merely `/dev/kvm` being absent (that's a **warn**: slow, not impossible).

  Example: `Refusing: installer_framework is unknown after checking both
  derivation signals against <the image the admin named> — supply a custom answer
  file, or say which Observed-dialects template it resembles.` Never a bare
  "not supported."
