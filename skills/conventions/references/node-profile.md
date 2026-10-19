# Node Profile Convention

## Path

The node profile lives at `nodes/<node>/node.yaml`, where `<node>` is the kebab-case name of the infrastructure node (e.g., `vps-fra-01`, `rpi-home-01`).

## Schema

Every node profile is a YAML file containing metadata about the node's hardware, OS, identity, and hardening state. Here is the authoritative schema:

<!-- fact-vocabulary: the one place dialect names appear as data rather than as
     instruction — the documented common values that keep each fact greppable
     without closing its set (distro-agnosticism.md Rule 5). -->

```yaml
name: vps-fra-01
distro: <os-release ID=>     # recorded, never branched on
distro_version: "12"         # os-release VERSION_ID, quoted
arch: x86_64                 # verbatim `uname -m`, never normalised
system_facts:
  # Common values are listed to make facts greppable, NOT to close the set.
  # Any other value a probe returns is legal — record it verbatim and journal it.
  package_manager: apt        # apt | dnf | dnf5 | zypper | pacman | apk | unknown
  service_manager: systemd    # systemd | openrc | runit | unknown
  container_engine: podman    # podman | docker | unknown
  mac_framework: none         # selinux | apparmor | none | unknown
  firewall:
    tool: ufw                 # ufw | firewalld | nftables | iptables | none | unknown
    rule_model: rule-list     # rule-list | zone | ruleset | unknown
    tag_mechanism: comment    # comment | log-prefix | named-set | none | unknown
  rootless_ready: true        # true | false | unknown — verified, not assumed
  capabilities:               # open map, probed on demand; absent key = never probed
    python3: present          # present | absent
    rsync: present
    tmux: absent
  detected: 2026-05-18        # ISO date these facts were last (re)detected
identity:
  machine_id_hash: "sha256:…"      # hash of the host machine id, never the raw value
  host_key_fingerprint: "SHA256:…" # the node's own ssh host key
  first_seen: 2026-05-18            # ISO date identity was first recorded
hostname: vps-fra-01
doccap_user: doccap
ssh_fallback_port: 42022
tailscale_hostname: vps-fra-01
roles: [edge, web]
provisioning:
  source: running              # running | vm — how this node was provisioned
  # vm-only sub-fields (present iff source: vm):
  hypervisor: qemu-kvm
  libvirt_uri: qemu:///session # the libvirt connection the domain lives on
  arch: x86_64                 # guest architecture requested (uname -m spelling)
  vcpus: 2
  memory_mb: 4096
  disk_gb: 40
  base_image: server-lts-2024.04   # the image the VM was installed from
  vm_name: vps-fra-01
  reach:                       # how the host reaches the VM before the tailnet
    host: 127.0.10.2
    ports: {22: 2222, 42022: 42022}   # guest port → host port
  status: provisioned          # none | provisioned — resume marker for VM creation
ssh:
  server: openssh              # openssh | dropbear | unknown — detected on the node
  config_include_dir: /etc/ssh/sshd_config.d   # null when the server has no drop-in dir
  effective_config_cmd: "sshd -T"              # null when the server cannot dump config
  key_name: doccap-at-vps-fra-01
  key_algorithm: ed25519       # ed25519 | rsa-4096
  key_source: generated        # generated | keepass-agent
  public_key_fingerprint: "SHA256:…"
hardening:                     # each: enabled | disabled | unknown
  firewall: enabled            # whether a firewall is on — not which tool
  fail2ban: enabled
  ssh_root_login: disabled     # sshd refuses root AND root holds no authorized key
  ssh_password_auth: disabled  # should be disabled
  ssh_session_policy: enabled  # empty passwords, X11 forwarding, dead-peer keepalive
  kernel_hardening: enabled    # the drydocc sysctl drop-in holds live
  file_integrity: enabled      # an integrity database is initialised, its hash recorded
  name_responders: disabled    # no LLMNR/mDNS responder answers beyond loopback
  tailscale: joined            # joined | deferred | unknown
remote_ide:
  vscode_server: compatible      # compatible | incompatible | unknown
  reason: ""                     # short note when incompatible (empty otherwise)
  probed: 2026-05-18             # ISO date the probe last ran (or null)
secrets:
  required_env:
    - name: DOCCAP_TS_KEY
      purpose: tailscale auth key for tailnet join
      phase: bootstrap
      keepass_entry: "drydocc://tailscale"
      optional: false
    - name: DOCCAP_NTFY_ADMIN_TOPIC
      purpose: ntfy topic the admin's phone subscribes to (node runtime secret)
      phase: runtime
      keepass_entry: "drydocc://ntfy"
      optional: true
    - name: DOCCAP_NTFY_AGENT_TOPIC
      purpose: ntfy topic agents poll (node runtime secret)
      phase: runtime
      keepass_entry: "drydocc://ntfy"
      optional: true
created: 2026-05-18
last_bootstrapped: null
```

<!-- /fact-vocabulary -->

### Field Descriptions

Enums and defaults are in the schema block above; this section covers only what
a field's name and enum do not convey.

- **`name`** — Must equal the directory name (kebab-case). The primary identifier, used by
  `/bootstrap-node`, `/onboard-node`, and journal scopes. It is also the
  node-name of the node's KeePassXC lookup URLs, so it must never equal a
  global credential's name (`tailscale`, `ntfy`, …) — `references/secrets.md`
  § "URL convention".
- **`distro`** — What the box reports about itself (`/etc/os-release` `ID=`).
  It **records and corroborates; it never selects behaviour.** Every dialect
  decision turns on a `system_facts` key instead. A skill that branches on
  `distro` is a bug.
- **`distro_version`** — Quoted, to preserve leading zeros and stay comparable.
- **`arch`** — Recorded **verbatim as `uname -m` reports it** (`x86_64`,
  `aarch64`, `armv7l`), never normalised to a package-format spelling. Comparisons
  normalise before matching and warn on a genuine mismatch; they do not refuse on
  a spelling difference.
- **`system_facts`** — The tool-dialect ledger: concrete facts **discovered by a
  live probe** during `/bootstrap-node` / `/onboard-node` OS discovery — never
  inferred from `distro`, never assumed by a generic skill. Written alongside
  `distro`/`distro_version`/`arch` and re-checked warn-on-drift on re-runs.
  The values listed in the schema block make facts greppable; they **do not close
  the set**. Any value a probe returns is legal — record it verbatim and journal
  it. A **missing block** means every fact is `unknown`: a skill that needs one
  must probe and write it before acting, never assume. Rules governing this block
  — including which candidate facts were rejected and why — live in
  `references/distro-agnosticism.md`.
  - **`package_manager`** — A distro's package tooling can change generation
    across its own releases, so this is probed, never inferred from
    `distro_version`. Detection: `bootstrap-node/references/hardening-recipes.md`
    § "Package installation".
  - **`service_manager`** — The one hard capability gate. Quadlets are a systemd
    feature, so a node reporting anything else refuses at bootstrap
    with that reason stated. Refusal turns on this fact, never on `distro`.
  - **`container_engine`** — `/onboard-node` can adopt a box already running
    docker; the framework converges on podman but must know what it found.
  - **`firewall`** — A group, because the tool alone is not enough to act.
    `rule_model` distinguishes a flat rule list from zone- or ruleset-shaped
    tools, which differ in *where* a rule goes, not just in syntax.
    `tag_mechanism` is how a rule carries the use-case slug so
    `/audit-use-case` and `/remove-use-case` can read it back; `none` means rules
    cannot be attributed and the admin has been told.
  - **`rootless_ready`** — Whether rootless podman actually works for
    `doccap_user`: subuid/subgid ranges present *and* cgroup delegation
    effective. A **verdict from a live check**, not a deduction from either
    ingredient — the whole use-case surface rests on it.
  - **`capabilities`** — Open map for probe-on-demand tool presence. A key is
    added when a skill first needs it; an **absent key means never probed**,
    which is distinct from `absent`. Use this rather than growing new top-level
    keys for tools that are merely present or missing.
  - **`detected`** — ISO date these facts were last (re)detected.
    `/bootstrap-node` audit-&-repair re-probes when `detected` predates a
    `distro_version` change and warns on drift. The periodic staleness sweep
    belongs to `/node-maintenance`'s `node-facts-fresh` posture item, which
    re-probes when `detected` ages past the run's staleness window and writes
    back only the keys that actually changed.
- **`identity`** — Machine identity, **discovered by a live probe** during
  `/bootstrap-node` / `/onboard-node` OS discovery exactly as `system_facts`
  is, and re-checked warn-on-drift on re-runs. Distinct from
  `ssh.public_key_fingerprint`, which is the **admin's client** key; these two
  fields describe the *box*. Read by `/apply-use-case` rebuild mode as
  corroborating evidence: it **corroborates, never triggers**,
  because a machine id also changes for a VM cloned from a template or a
  re-flashed SBC, and can stay equal across a clone that is a different box. A
  **missing block** means `unknown` and degrades the rebuild confirmation
  prompt, never refuses.
  - **`machine_id_hash`** — Hashed because a raw host identifier has no
    business sitting in git in clear; a hash still compares.
  - **`first_seen`** — ISO date the block was first written. Never rewritten
    on a re-run; drift is reported against the other two fields.
- **`hostname`** — Often equals `name`, but may differ (e.g. a DNS CNAME alias).
- **`doccap_user`** — Owns rootless podman, systemd user services, and agent
  operations. Skills run commands as this user.
- **`ssh_fallback_port`** — Key-based access to `doccap_user` when Tailscale is
  unavailable; primary access is `tailscale ssh`. Declared in the node's
  firewall rules (per `system_facts.firewall.tool`) and the bootstrap journal.
- **`tailscale_hostname`** — The node's name on the tailnet. Often equals
  `hostname`; used to craft `tailscale ssh` commands.
- **`roles`** — Free-form tags for the admin's mental model. Not interpreted by
  skills. Seeded from the free-text description passed to `/bootstrap-node`.
- **`provisioning`** — How the node came to exist.
  - **`source`** — `running` = an already-reachable box (flashed SBC, rented
    vServer). `vm` = a qemu/kvm VM `/bootstrap-node` provisioned itself.
  - `hypervisor` / `libvirt_uri` / `arch` / `vcpus` / `memory_mb` / `disk_gb` /
    `base_image` / `vm_name` / `reach` are present **only** when `source: vm`.
    `vm_name` is the libvirt domain name (defaults to `<node>`); the VM-exists
    probe is keyed on it.
  - **`libvirt_uri`** — The libvirt connection the domain was defined on: the
    rootless per-user session connection where the host allows it, else the
    system one. A domain is invisible on the other connection, so resume,
    teardown and every later hypervisor call use this value. Absent on VMs
    provisioned before the field existed — treat those as the system
    connection.
  - **`arch`** — The guest CPU architecture *requested* at provision time, in
    `uname -m` spelling. The top-level `arch` is the probed evidence; the two
    must agree once `/bootstrap-node` Phase 2 has run.
  - **`reach`** — How the host dials the VM before any tailnet exists:
    `host` plus a `ports` map from guest port to host port. Under the session
    connection `host` is a loopback address of the VM's own and guest 22 maps
    to a high host port; under the system connection `host` is the NAT lease
    and ports map to themselves. `/bootstrap-node` Phase 2 connects through
    it.
  - **`status`** — The single persisted resume marker for VM creation, because
    a half-installed VM cannot be safely re-probed. `provisioned` means the VM
    installed and became SSH-reachable.
- **`ssh`** — SSH access established during onboarding. **Metadata
  only** — no private key material ever lives here.
  - **`server`** — Drives key-algorithm negotiation. Discovered on the node, the
    original precedent for the whole `system_facts` ledger; it stays
    here because its satellite fields belong with it.
  - **`config_include_dir`** — Where a drop-in config fragment is placed, probed
    rather than assumed. **`null` means the server has no drop-in mechanism** and
    config must be edited in place — a materially different and riskier path that
    the lockdown recipe branches on.
  - **`effective_config_cmd`** — The command that dumps the server's *effective*
    configuration, used to verify a lockdown actually took. **`null` means the
    server cannot report it**, so verification falls back to a live connection
    attempt. Never assume a server offers this.
  - **`key_name`** — Conventionally `doccap-at-<node>`.
  - **`key_algorithm`** — Negotiated against `server` (ed25519 default;
    rsa-4096 fallback for dropbear/older servers).
  - **`key_source`** — `generated` (bootstrap created it) or `keepass-agent`
    (admin supplied it via a KeePassXC ssh-agent).
  - **`public_key_fingerprint`** — The private key lives on the admin laptop +
    KeePass, never here.
- **`hardening.*`** — The node's security posture after `/bootstrap-node`
  (including audit-&-repair re-runs) or `/onboard-node`. These fields say
  *whether* a protection is on — **never which tool provides it**; that is
  `system_facts`'s job. `unknown` means not yet verified.
  - **`tailscale`** — Set to `joined` only after `tailscale up` succeeds *and*
    the node's own status entry reports it online under that hostname.
  - **`ssh_root_login`** — `disabled` when the server's effective config
    refuses root **and** root holds no authorized key. A key nobody can use
    today is one config edit away from being usable again. Bootstrap moves
    root's keys aside rather than deleting them.
  - **`name_responders`** — `disabled` when no link-local name responder
    (LLMNR, mDNS) answers beyond loopback. A port a use-case declares for
    such a responder is the recorded exception. The daemon that owned the
    responder is recorded under `system_facts.capabilities`, not here.
  - **`ssh_session_policy`** — `enabled` when the server's effective config
    refuses empty passwords, refuses X11 forwarding, requires public-key
    authentication, and drops dead peers through keepalive probes. Idle
    sessions are **not** dropped: Remote-SSH and `node-exec` sessions sit idle
    for a long time on purpose.
  - **`kernel_hardening`** — `enabled` when every key in the drydocc sysctl
    drop-in reads back live at its derived value. Packet forwarding left on
    for a consumer the probe found (a container engine that needs it, a node
    advertising tailnet routes) still counts as `enabled`, because the
    derived value for that node is "on".
  - **`file_integrity`** — `enabled` when a file-integrity database has been
    initialised on the hardened box and its hash recorded off the node (in the
    journal). The tool that provides it is recorded under
    `system_facts.capabilities`, not here.
  - **Absent field** — a profile written before a field existed reads it as
    `unknown`. The next `/bootstrap-node` audit-&-repair run probes the field
    and offers to establish it, so older nodes catch up on their own.
  - **`fail2ban`** names a tool, which breaks the rule above. The name
    predates the rule and stays, because renaming a field under live nodes
    would orphan their profiles. New fields never follow it.
  - Future flags (e.g. `firewall_default_deny_inbound`, `mac_enforcing`) take
    tool-agnostic names, with the concrete tool recorded in `system_facts`.
- **`remote_ide`** — Whether the node can host an in-IDE remote backend. Probed
  by `/bootstrap-node` (Phase 4) and `/onboard-node` (finalize) once key access
  exists, refreshed on re-runs, and used to tell the admin which surface to
  prefer. A **missing block** means `vscode_server: unknown`.
  - **`vscode_server`** — The probe checks CPU arch (`uname -m` ∈ {`x86_64`,
    `aarch64`}; 32-bit arm such as `armv7l` is `incompatible`), C library
    (`musl`-only → `incompatible`), and outbound reachability of
    `https://update.code.visualstudio.com` (unreachable → `incompatible` if the
    box is clearly offline, `unknown` if merely transient). When `compatible`,
    `on_client` skills suggest connecting via VS Code Remote-SSH instead.
  - **`reason`** — Short note when `incompatible` (e.g. `"musl libc"`,
    `"32-bit arm unsupported"`); empty string otherwise.
  - **`probed`** — ISO date (YYYY-MM-DD) the probe last ran, or `null` if never
    probed.
- **`secrets.required_env`** — List of node/bootstrap-level secret **environment-variable names** the agent must be able to resolve (per-use-case secret names live in their `<slug>.secrets.yaml` manifests, not here). Written at scaffold time and read before any later interaction (e.g. use-case setup/audit) so the agent can confirm required secrets are available. Each entry:
  - **`name`** — The env-var name resolved env-var-first per `secrets` (e.g. `DOCCAP_TS_KEY`).
  - **`purpose`** — One-line description.
  - **`phase`** — Free-form note on when the var is needed (e.g. `bootstrap`). `runtime` marks a node runtime secret — one that unattended jobs on the node need, installed under `/etc/drydocc/secrets/_<purpose>.env` rather than held only for a session (see `references/secrets.md` § "Node runtime secrets").
  - **`keepass_entry`** — The KeePassXC lookup URL (`drydocc://…`) the `@max-win-at/keepassxc-secrets` skill fetches from, per `references/secrets.md` § "URL convention". Used only when the value isn't already in the environment.
  - **`optional`** — Boolean; `false` blocks the run when unresolved, `true` is skipped (e.g. `DOCCAP_NTFY_ADMIN_TOPIC` on a node without notifications). Fork access is not listed here: a node pushes with its own deploy key, generated on the node, not with a secret the admin supplies.
- **`created`** — ISO date (YYYY-MM-DD) when `nodes/<node>/` was first scaffolded by `/bootstrap-node` (Phase 0).
- **`last_bootstrapped`** — ISO datetime (YYYY-MM-DDTHH:MM:SSZ, UTC) when the node first reached the `drydocc` baseline. Set by `/bootstrap-node` on its first successful run, **or** by `/onboard-node` iff every `hardening.*` field reached baseline AND cut-over verification passed during that run. `null` otherwise. Allows the agent to detect newly-bootstrapped nodes and to refuse `/onboard-node` on a node that already crossed the baseline. Re-running `/bootstrap-node` on a node where this is set triggers an interactive audit-&-repair confirmation.

## Box-bound fields

Some fields describe **the machine**, not the node's role: they are true of one
box and false of any other, even a box built to do the same job. Moving a node's
declarations to a different box — a lab prototype extracted as a runbook bundle
(`references/runbook-bundle.md`) — keeps everything else and treats these as
follows:

| Field | On another box |
|---|---|
| `distro`, `distro_version`, `arch`, `system_facts` | removed — `/bootstrap-node` probes them |
| `identity` | removed — it *is* the box |
| `ssh` | removed — the server is probed, and the admin's key is made for the new box |
| `remote_ide` | removed — probed |
| `provisioning` | `{source: running}` — the new box already exists |
| `hardening.*` | `unknown` — a posture is only what was verified on this box |
| `last_bootstrapped` | `null` |
| `created` | the date the node's directory was scaffolded for the new box |
| `name`, `hostname`, `tailscale_hostname` | the new node's name |

Everything this table does not name is **portable** and carries over unchanged
— `doccap_user`, `ssh_fallback_port`, `roles`, `secrets.required_env` (a
node-scoped `keepass_entry`, `drydocc://<node>/…`, follows the new name), and any
field added later unless it is added here. A new field that describes the
machine must be added to this table in the same change.

## Lifecycle

Which command writes this file, and when. Phase detail lives in each command's
SKILL.md — not here.

1. **`/bootstrap-node <slug>`** (fresh boxes) — scaffolds the stub in Phase 0
   (`hardening.*: unknown`, `last_bootstrapped: null`), then fills
   `distro`/`distro_version`/`arch`/`system_facts.*`/`identity.*`/`ssh.*` from
   **live probes** in Phase 2, `hardening.*` in Phase 3, and
   `provisioning.*`/`remote_ide`/`last_bootstrapped` in Phase 6. Never from
   admin claims — always discovered.
2. **`/onboard-node <slug>`** (existing, externally-configured boxes) — writes
   the same fields, including `identity.*`, but every `hardening.*` change is
   gated on per-item admin approval, and discovered containers become
   `status: planned` use-case docs. Sets `last_bootstrapped` only if
   every field reaches baseline AND cut-over verification passes.
3. **Audit** — re-running `/bootstrap-node` on a node with `last_bootstrapped`
   set triggers audit-&-repair: live state, including `identity.*`,
   is compared to the declared values, drift is warned rather than
   auto-corrected, and `node.yaml` is updated honestly. `/audit-use-case` never
   writes this file.
4. **`/node-maintenance`** — writes `system_facts` and **nothing else**,
   when its `node-facts-fresh` item re-probes stale facts. It never
   writes `hardening.*`: that block describes protections whose repair belongs
   to audit-&-repair above, and recording a corrected value without restoring
   the protection would make this file claim a posture nobody re-established.
   A discrepancy there is reported, with `/bootstrap-node <node>` emitted.

## Example

See `../assets/example-vps/node.yaml` for a complete example.
