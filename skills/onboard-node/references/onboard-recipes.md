
# `drydocc` Onboard

Reusable bits that the `onboard-node` skill leans on. The orchestration
sequence lives in the skill's `SKILL.md`; this reference holds the parts
that are shaped by what's already on the box: how to probe live state,
how to classify it against the `drydocc` baseline, what to show the admin
when reconciling, and how to reverse-engineer a running container into a
use-case doc's `spec:` block.

**This skill does not duplicate fix commands.** Every hardening
remediation links to its anchor in `bootstrap-node`. Every spec
target links to `podman-quadlets` `## Render from spec`.

## Prerequisites

- Admin laptop has `ssh` and (for the cut-over routine, when an sshd
  remediation is applied) the `tailscale` CLI authenticated to the same
  tailnet the node will join.
- `nodes/<self>/node.yaml` exists with `distro`, `distro_version`,
  `ssh_fallback_port`, `tailscale_hostname`, `doccap_user` — or
  `/onboard-node` scaffolds a stub for `<slug>` in Phase 1 and fills
  `distro`/`distro_version` from the live probe.
- Onboarding-time secrets resolve lazily and env-var-first per
  `secrets` (keepass skill or a user-supplied `.env`) — only
  the remediation choices the admin approves trigger a resolution.
  `DOCCAP_TS_KEY` is required only if `hardening.tailscale: unknown` and the
  admin opts to join the tailnet during this run. The Phase 8 fork clone needs
  no secret: the node generates its own deploy key.

## Inventory probes

The probe matrix is one bundled SSH session — `ssh <initial> bash -s
<<'PROBE' … PROBE'` — that runs every check in a single round-trip, on any
box regardless of distro. Output lines are stable shape `<key>=<value>` so
the agent can parse each into the inventory record; the probe only reads —
nothing on the box is written here.

**Goal.** After one round trip the agent knows, even for a box nobody has
described in this repository before: every `system_facts` dialect fact
(`package_manager`, `service_manager`, `container_engine`, `mac_framework`,
`firewall.{tool,rule_model,tag_mechanism}`, `rootless_ready`,
`capabilities.{python3,rsync,tmux}`), the machine-identity pair
(`identity.*`), the current `doccap` user/sudoers/pubkey/linger state, the
sshd config in force, every running container, every listening port, and
every enabled systemd unit the framework doesn't already account for.

**Fact.** Every fact above is `unknown` until this probe runs — none is ever
inferred from `distro`. Three of them (`package_manager`, `mac_framework`,
`firewall.*`) can have more than one live candidate on a box nobody has
described yet and need a try-each-in-order loop; the rest are closed enough
(a capability check, or a plain presence check for the open `capabilities:`
map) to resolve directly. `$PUBKEY`, `$SSHD_EFFECTIVE_CONFIG_CMD`,
`$PM_PROBES`, `$MAC_PROBES`, and `$FW_PROBES` are templates the agent fills
in before sending the session — the candidate-loop ones from § "Observed
dialects" below (or freshly derived and then appended there), the same way
`$PUBKEY` is already filled from the admin's pasted key. The probe itself:

```bash
# Identity + user
id doccap &>/dev/null && echo doccap_user=present || echo doccap_user=absent
test -f /etc/sudoers.d/doccap \
  && echo "sudoers_doccap=$(cat /etc/sudoers.d/doccap | tr -d '\n')" \
  || echo sudoers_doccap=absent
grep -qxF "$PUBKEY" ~doccap/.ssh/authorized_keys 2>/dev/null \
  && echo pubkey=present || echo pubkey=absent
test -f /etc/drydocc/node-name \
  && echo "node_name_file=$(cat /etc/drydocc/node-name)" \
  || echo node_name_file=absent
loginctl show-user doccap --property=Linger --value 2>/dev/null \
  | awk '{print "linger="$1}'

# Machine identity (node.yaml:identity) — never emit the raw machine id
MID_RAW="$( (cat /etc/machine-id 2>/dev/null || cat /var/lib/dbus/machine-id 2>/dev/null) | tr -d '[:space:]')"
if [ -n "$MID_RAW" ]; then
  echo "machine_id_hash=sha256:$(printf '%s' "$MID_RAW" | sha256sum | awk '{print $1}')"
else
  echo machine_id_hash=unknown
fi
unset MID_RAW
HOST_KEY_PUB="$(ls /etc/ssh/ssh_host_ed25519_key.pub 2>/dev/null \
  || ls /etc/ssh/ssh_host_*_key.pub 2>/dev/null | head -1)"
if [ -n "$HOST_KEY_PUB" ]; then
  echo "host_key_fingerprint=$(ssh-keygen -lf "$HOST_KEY_PUB" | awk '{print $2}')"
else
  echo host_key_fingerprint=unknown
fi

# sshd config (effective, not just a drop-in dir). ssh.effective_config_cmd
# may legally be null — a server that cannot dump its own effective config —
# so only run it when the agent has one to substitute; never assume one
# particular server's dump command exists.
if [ -n "$SSHD_EFFECTIVE_CONFIG_CMD" ]; then
  sudo -n sh -c "$SSHD_EFFECTIVE_CONFIG_CMD" 2>/dev/null | awk '
    $1=="permitrootlogin"       {print "sshd_root="$2}
    $1=="passwordauthentication"{print "sshd_passauth="$2}
    $1=="port"                   {print "sshd_port="$2}'
else
  printf 'sshd_root=unknown\nsshd_passauth=unknown\nsshd_port=unknown\n'
fi

# Package manager. $PM_PROBES: newline-separated "<value>|<binary>|<confirm-cmd>"
# triples — <value> is what gets recorded, and is not always just the binary
# name: a manager's own generation can change what value is correct even
# when the binary doesn't (see § "Observed dialects" below and
# bootstrap-node/references/hardening-recipes.md's own generation detection
# for a worked case). A candidate must be *confirmed managing this install*,
# never merely present — a rescue or secondary manager can be co-installed
# without being the one in charge.
PM_FOUND=""
while IFS='|' read -r PM_VAL PM_BIN PM_CONFIRM; do
  [ -n "$PM_BIN" ] || continue
  command -v "$PM_BIN" >/dev/null 2>&1 || continue
  PM_OUT="$(eval "$PM_CONFIRM" 2>/dev/null)"
  if [ -n "$PM_OUT" ]; then
    echo "package_manager=$PM_VAL"
    PM_FOUND=1
    break
  fi
done <<PMPROBE
$PM_PROBES
PMPROBE
[ -n "$PM_FOUND" ] || echo package_manager=unknown

# Service manager — the one hard capability gate (quadlets are a
# systemd feature). A live tree is the confirmation, never a distro guess.
if [ -d /run/systemd/system ]; then
  echo service_manager=systemd
elif command -v rc-status >/dev/null 2>&1; then
  echo service_manager=openrc
elif command -v sv >/dev/null 2>&1 && [ -d /etc/runit ]; then
  echo service_manager=runit
else
  echo service_manager=unknown
fi

# MAC framework — enforcing, not merely installed. $MAC_PROBES: newline
# "<value>|<status-cmd>|<enforcing-regex>" triples, filled the same way as
# $PM_PROBES.
if [ -n "$MAC_PROBES" ]; then
  MAC_FOUND=""
  while IFS='|' read -r MAC_VAL MAC_CMD MAC_PATTERN; do
    [ -n "$MAC_VAL" ] || continue
    MAC_OUT="$(eval "$MAC_CMD" 2>/dev/null)"
    if printf '%s' "$MAC_OUT" | grep -qiE "$MAC_PATTERN"; then
      echo "mac_framework=$MAC_VAL"
      MAC_FOUND=1
      break
    fi
  done <<MACPROBE
$MAC_PROBES
MACPROBE
  [ -n "$MAC_FOUND" ] || echo mac_framework=none
else
  echo mac_framework=unknown
fi

# Firewall — tool, rule model, and tag mechanism resolve together (they are
# one fact group; see conventions/references/distro-agnosticism.md's worked
# example). $FW_PROBES: newline "<tool>|<rule_model>|<tag_mechanism>|
# <status-cmd>|<enabled-unit>" quintuples. A candidate must be confirmed
# managing the live ruleset, never merely installed.
FW_TOOL=""; FW_UNIT=""
if [ -n "$FW_PROBES" ]; then
  while IFS='|' read -r FW_T FW_RM FW_TM FW_CMD FW_U; do
    [ -n "$FW_T" ] || continue
    command -v "$FW_T" >/dev/null 2>&1 || continue
    FW_OUT="$(eval "$FW_CMD" 2>/dev/null)"
    if [ -n "$FW_OUT" ]; then
      echo "firewall_tool=$FW_T"
      echo "firewall_rule_model=$FW_RM"
      echo "firewall_tag_mechanism=$FW_TM"
      echo "firewall_status=$(printf '%s' "$FW_OUT" | head -1)"
      FW_TOOL="$FW_T"; FW_UNIT="$FW_U"
      break
    fi
  done <<FWPROBE
$FW_PROBES
FWPROBE
fi
if [ -z "$FW_TOOL" ]; then
  printf 'firewall_tool=unknown\nfirewall_rule_model=unknown\nfirewall_tag_mechanism=unknown\nfirewall_status=unknown\n'
fi

# Rootless readiness — one verified verdict, never a deduction from either
# ingredient alone: subuid/subgid ranges present AND cgroup delegation
# actually in effect for doccap (a systemd version can ship without
# delegating even when the ranges are configured).
DOCCAP_UID="$(id -u doccap 2>/dev/null)"
if [ -z "$DOCCAP_UID" ]; then
  echo rootless_ready=unknown
elif grep -q "^doccap:" /etc/subuid 2>/dev/null && grep -q "^doccap:" /etc/subgid 2>/dev/null; then
  CTRLS="$(cat "/sys/fs/cgroup/user.slice/user-${DOCCAP_UID}.slice/cgroup.controllers" 2>/dev/null)"
  if printf '%s' "$CTRLS" | grep -q memory && printf '%s' "$CTRLS" | grep -q pids; then
    echo rootless_ready=true
  else
    echo rootless_ready=false
  fi
else
  echo rootless_ready=false
fi

# Open capabilities map — probe-on-demand tool presence, load-bearing
# elsewhere: python3 (this probe's own per-container inspect below),
# rsync (/apply-use-case's irreversible cut-over), tmux (node-exec
# lingering; absent falls back to nohup).
for CAP in python3 rsync tmux; do
  command -v "$CAP" >/dev/null 2>&1 \
    && echo "capabilities_${CAP}=present" \
    || echo "capabilities_${CAP}=absent"
done

# fail2ban
systemctl is-active fail2ban 2>/dev/null \
  | awk '{print "fail2ban="$0}'

# Tailscale
tailscale status --json 2>/dev/null \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print("tailscale="+d["Self"]["HostName"])' \
  2>/dev/null \
  || echo tailscale=absent

# Containers
command -v podman >/dev/null \
  && podman ps --all --format '{{.Names}}|{{.Image}}|{{.Ports}}|{{.Status}}' \
       | sed 's/^/podman_container=/' \
  || echo podman=absent
command -v docker >/dev/null \
  && sudo -n docker ps --all --format '{{.Names}}|{{.Image}}|{{.Ports}}|{{.Status}}' \
       | sed 's/^/docker_container=/' \
  || echo docker=absent

# Container engine — the verdict from the checks just above, surfaced as its
# own fact (/onboard-node must know what it found before it can
# converge the box on podman).
if command -v podman >/dev/null 2>&1; then
  echo container_engine=podman
elif command -v docker >/dev/null 2>&1; then
  echo container_engine=docker
else
  echo container_engine=unknown
fi

# Listening ports
sudo -n ss -tulpnH 2>/dev/null \
  | awk '{print "port="$1"|"$5"|"$7}'

# Custom systemd units (system) — strip vendor / framework noise. The
# firewall unit is whatever the confirmed tool mapped to above ($FW_UNIT);
# an unconfirmed or unmapped tool leaves it empty, so nothing extra is
# excluded rather than guessing a name.
NOISE='^(ssh|sshd|fail2ban|tailscaled|cron|systemd-)'
[ -n "$FW_UNIT" ] && NOISE="${NOISE%)}|${FW_UNIT})"
systemctl list-unit-files --state=enabled --no-legend --no-pager \
  | awk '{print $1}' \
  | grep -Ev "$NOISE" \
  | sed 's/^/systemd_unit=/'
```

Each line is `key=value`; the agent parses on the first `=`. Multi-value
keys (`podman_container=`, `port=`, `systemd_unit=`) are accumulated.

Per-container detail follows once the container list is known:

```bash
for name in $CONTAINER_NAMES; do
  podman inspect "$name" --format '{{json .}}' \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); print(json.dumps({
        "name":     d["Name"].lstrip("/"),
        "image":    d["ImageName"],
        "image_id": d["Image"],
        "env_names":[e.split("=",1)[0] for e in d["Config"]["Env"]],
        "mounts":   [{"type":m["Type"],"source":m["Source"],"target":m["Destination"],"name":m.get("Name","")} for m in d["Mounts"]],
        "networks": list(d["NetworkSettings"]["Networks"].keys()),
        "ports":    d["HostConfig"]["PortBindings"] or {},
        "restart":  d["HostConfig"]["RestartPolicy"]["Name"],
        "cmd":      d["Config"]["Cmd"] or [],
        "labels":   d["Config"]["Labels"] or {},
      }))'
done
```

The right-hand-side of `Config.Env` is split-on-`=` and **discarded
immediately**; only the variable names cross into the inventory record.

**Deriving an unseen dialect.**

- `package_manager` — no candidate list closes over every distro's tooling;
  treat `$PM_PROBES` as a growing set of leads, not a ceiling. For a binary
  not yet in the table: confirm it with a command that only succeeds when
  that binary is *managing this install* — asking it to report its own
  package count or database contents is more reliable than presence alone,
  because a rescue or secondary manager can be co-installed without being
  the one in charge. A single binary can also answer as more than one
  recorded `value` across its own releases — see
  `bootstrap-node/references/hardening-recipes.md`'s own package-manager
  generation detection for a worked case — so probe the confirmed binary's
  own version/help banner for the generation marker and let the row's
  `<value>`, not the binary name, be what gets recorded.
- `mac_framework` — a framework can be installed and inert; *enforcing*
  means its own purpose-built status query says so, never a process grep.
  Record `none` only after every candidate you tried came back not-enforcing
  — record `unknown` when there was no candidate to try in the first place.
  Don't collapse the two; an admin acts on them differently.
- `firewall.{tool, rule_model, tag_mechanism}` — this is exactly the worked
  example in `conventions/references/distro-agnosticism.md`; re-read it
  before deriving a new tool. In short: confirm the tool is managing the
  live ruleset (not just installed), classify its rule storage as a flat
  list vs. a zone/ruleset shape (`rule_model`), and find its nearest
  free-text carrier for a tag (`tag_mechanism: none` if it has none — never
  invent a silent convention `/remove-use-case` can't read back).
- `rootless_ready` — both ingredients, verified live, never inferred from
  either alone: an entry in the subuid/subgid maps proves the *range
  exists*; only the cgroup controller file under the user's own slice proves
  delegation is *actually in effect*. If `doccap` doesn't exist yet, there is
  nothing to verify — record `unknown`, not `false`.
- `service_manager` / `container_engine` / `capabilities.*` — comparatively
  closed: a live `/run/systemd/system` tree is unambiguous, podman/docker
  are the only two container engines the framework acts on today,
  and `capabilities` is a plain presence check. None needs a candidate loop
  — but an answer outside the documented enum is still legal (Rule 5):
  record it verbatim rather than forcing it to `unknown`.

**Verification.** Every derivation above is self-verifying: presence
(`command -v`) is never sufficient by itself — the paired confirm/status
command must also succeed before a value is recorded, exactly like the
machine-identity and host-key fallbacks above, which already terminate in an
explicit `unknown` rather than guessing. `rootless_ready` positively
confirms both ingredients rather than trusting either. A fact this probe
cannot confirm is written `unknown`; Phase 2 and Phase 6 (hardening
classification) treat `unknown` as *not yet known*, never as a default to
act on.

**Record.** Every value this probe returns — including `unknown` — is
written verbatim to `node.yaml:system_facts` (plus `.detected: <today>`) in
Phase 2, and the whole session is journaled per the `conventions` skill's
`references/journal.md`. When a probe above derives and verifies a dialect
that isn't already in a table below, append the row before moving on — that
is how the next onboarding run against a similar box gets a head start
instead of starting from `command -v` again.

#### Observed dialects — evidence, not the supported list

`$PM_PROBES`, `$MAC_PROBES`, and `$FW_PROBES` above are built by joining the
rows below, one `<field>|<field>|…` line per row. Reuse a row whose
binary/status command matches the node — after running Verification above,
never instead of it (Rule 3).

**`package_manager`**

| value | binary | confirm command | verified on |
|---|---|---|---|
| apt | apt | `test -d /var/lib/dpkg && apt list --installed >/dev/null 2>&1 && echo ok` | this file's earlier single-dialect version |
| dnf | dnf | `dnf --version 2>/dev/null \| head -1 \| grep -qvi '^Dnf5' && dnf --version >/dev/null 2>&1 && echo ok` | carried from `bootstrap-node/references/hardening-recipes.md`'s package-installation detection — not yet independently confirmed by `/onboard-node` |
| dnf5 | dnf | `dnf --version 2>/dev/null \| head -1 \| grep -qi '^Dnf5' && echo ok` | carried from `bootstrap-node/references/hardening-recipes.md`'s package-installation detection — not yet independently confirmed by `/onboard-node` |

**`mac_framework`**

| value | status command | enforcing regex | verified on |
|---|---|---|---|
| selinux | `getenforce 2>/dev/null` | `^Enforcing$` | not yet verified anywhere in this repo — confirm before trusting |
| apparmor | `aa-status --enabled 2>&1; echo $?` | `^0$` | not yet verified anywhere in this repo — confirm before trusting |

**`firewall`**

| tool | rule_model | tag_mechanism | status command | active pattern | enabled unit | verified on |
|---|---|---|---|---|---|---|
| ufw | rule-list | comment | `ufw status` | `Status: active` | ufw | this file's earlier single-dialect version |
| firewalld | zone | log-prefix | `firewall-cmd --state` | `^running$` | firewalld | `conventions/references/distro-agnosticism.md`'s worked example, test-02, 2026-06 |

## Idempotency idioms — inverse table

Each row pairs the bootstrap idiom (which writes state) with the
onboarding probe (which reads state). For remediation, onboard runs the
bootstrap idiom verbatim — never a re-implementation — with one exception:
`firewall` runs the adoption variant in
`bootstrap-node/references/hardening-recipes.md` § "Adoption variant"
instead of the plain idiom linked below, because the plain idiom resets the
rule set (see § "Hardening classification rules" for what the variant
preserves instead).

| State                                | Bootstrap idiom anchor                          | Onboard probe (read half)                                                                                |
| ------------------------------------ | ----------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `doccap` user                       | `bootstrap-node/references/hardening-recipes.md#create-doccap-via-root-password`                | `id doccap &>/dev/null && echo present \|\| echo absent`                                                |
| sudoers entry                        | `bootstrap-node/references/hardening-recipes.md#create-doccap-via-root-password`              | `test -f /etc/sudoers.d/doccap && cat /etc/sudoers.d/doccap \|\| echo absent`                          |
| Admin pubkey installed               | `bootstrap-node/references/hardening-recipes.md#install-pubkey-via-password`             | `grep -qxF "$PUBKEY" ~doccap/.ssh/authorized_keys && echo present \|\| echo absent`                     |
| sshd drop-in (port + lockdown)       | `bootstrap-node/references/hardening-recipes.md#ssh-lockdown-ordering-dual-port-transitional`| `sshd_root=` / `sshd_passauth=` / `sshd_port=` from § "Inventory probes" above (via `ssh.effective_config_cmd`) |
| firewall enabled + default-deny rules | `bootstrap-node/references/hardening-recipes.md#firewall`                  | `firewall_status=` + the tagged-rule read-back for `system_facts.firewall.tool`, both from § "Inventory probes" above |
| fail2ban sshd jail                   | `bootstrap-node/references/hardening-recipes.md#fail2ban`                   | `systemctl is-active fail2ban` + `test -f /etc/fail2ban/jail.d/sshd.local`                               |
| Tailscale joined as `<hostname>`     | `bootstrap-node/references/hardening-recipes.md#tailscale-up`               | that section's `ts_self_up` — the node's own status entry, never a grep of the peer table                 |
| Lingering enabled for doccap        | `bootstrap-node/references/hardening-recipes.md#lingering--identity-file`                  | `loginctl show-user doccap --property=Linger --value`                                                   |
| `/etc/drydocc/node-name` written    | `bootstrap-node/references/hardening-recipes.md#lingering--identity-file`              | `test -f /etc/drydocc/node-name && cat /etc/drydocc/node-name \|\| echo absent`                        |

When the admin chooses **apply** for a row, the agent composes the full
SSH invocation around the bootstrap idiom (filling templates from
`node.yaml`) — never a custom command. This keeps remediation
behaviour identical to a clean bootstrap.

## Hardening classification rules

For each of the five `hardening.*` fields, the probe output maps to one
of `aligned | drift | unknown`. Every row classifies the **probe's emitted
key**, not a tool's output text, so a node whose firewall or SSH server is one
nobody has written down still classifies — the § "Inventory probes" contract
already resolved which tool answered, and `unknown` is always a legal result.

| Field                 | `aligned` ⇔                                               | `drift` ⇔                                               | `unknown` ⇔                              |
| --------------------- | --------------------------------------------------------- | ------------------------------------------------------- | ---------------------------------------- |
| `firewall`            | `firewall_status=` reports the rule set enforcing         | reports it present but not enforcing                    | `firewall_tool=unknown`, no probe for the tool, or sudo denied |
| `fail2ban`            | `systemctl is-active fail2ban == active` AND the jail drop-in exists | `inactive`/`failed`, OR active but the jail drop-in absent | unit-not-found / probe error             |
| `ssh_root_login`      | `sshd_root=no`                                            | `yes` OR `prohibit-password` (`drydocc` baseline is `no`) | `sshd_root=unknown` (`ssh.effective_config_cmd` is `null` or failed) |
| `ssh_password_auth`   | `sshd_passauth=no`                                        | `yes`                                                   | `sshd_passauth=unknown`                   |
| `tailscale`           | `tailscale status` lists `<tailscale_hostname>` as active | installed but logged-out / wrong hostname                | tailscale not installed                  |

Two `unknown` causes are worth distinguishing in the report, because they call
for different admin action: **the probe was refused** (sudo denied, command
failed) versus **the tool is one this recipe has no read-back for**. The first is
a permissions problem on a known setup; the second means a dialect needs deriving
and recording per `references/distro-agnosticism.md`. Never collapse the second
into "firewall not enabled" — an unreadable rule set is not an absent one.

On an adopted box the `firewall` row carries extra state into Phase 7: the
count of pre-existing rules, and `at_risk_ports` computed per
`bootstrap-node/references/hardening-recipes.md` § "Adoption variant". A box
whose firewall is *inactive* still has at-risk ports — enabling default-deny
blocks live listeners even though no rule was removed.

The matching value to write back into `node.yaml:hardening.*` follows
the standard enum:

- `firewall`, `fail2ban` → `enabled` / `disabled` / `unknown`.
- `ssh_root_login`, `ssh_password_auth` → `disabled` (= `drydocc`
  baseline reached) / `enabled` (= still permitted) / `unknown`.
- `tailscale` → `joined` / `unknown`.

When admin **applies** the remediation and it succeeds, the agent
writes the post-apply value. When admin **skips**, the agent writes the
**current observed value honestly** (e.g., `enabled` for root SSH if
that's what's running). When admin **defers**, the agent writes
`unknown` so a future `/bootstrap-node --rebootstrap` can retry.

## Reconciliation menu copy

Render this verbatim per drift/unknown item so admins see a consistent
UI:

```
[<field>]  <classification>
  current:  <probe-observed value, one line>
  desired:  <drydocc baseline value>
  fix:      see bootstrap-node/references/hardening-recipes.md#<anchor>
  needs:    <sudo on the node | tailscale cli locally | none>

  [a] apply   [s] skip   [d] defer-to-bootstrap-node   [v] view fix
```

Special cases:

- `doccap` user is **composite**: the menu offers a single
  apply/skip/defer that covers user creation + sudoers + pubkey install
  + linger together. Splitting them is footgun-prone.
- `ssh_root_login` and `ssh_password_auth` apply choices are grouped at
  reload time: the sshd drop-in is written once after the last
  sshd-affecting apply, then `systemctl reload sshd` runs, then the
  cut-over verification routine from `bootstrap-node` runs **once**.
  If cut-over verification fails, both fields revert to `unknown` in
  the node.yaml delta (the box state is what it is, but our record
  honestly says we don't know if the change stuck).
- `tailscale = unknown` and admin chooses `apply`: the agent resolves
  `DOCCAP_TS_KEY` via `secrets` § "Session secrets" at that moment
  (lazy, env-var-first — admins who skip never trigger a resolution).
- `firewall` renders a preamble **above** the standard menu block, listing the
  `at_risk_ports` computed per `bootstrap-node/references/hardening-recipes.md`
  § "Adoption variant":

  ```
  [hardening.firewall]  current: <active | inactive>, <N> existing rule(s)
    These ports are reachable now and would be blocked after hardening:
      <port>/<proto>   → <owning-slug | adopted-unclassified>
      ...
    apply will: keep every existing rule, set default-deny, add the framework
    rules, and add one tagged allow per port listed above. It will NOT reset.
  ```

## Service discovery rules

Cluster discovered containers into use-case groups by, in order:

1. **Compose project label.** Containers sharing
   `com.docker.compose.project` or `io.podman.compose.project` go into
   one use-case named after the project (kebab-cased). Most
   docker-compose / podman-compose stacks land here cleanly.
2. **Shared name prefix.** Container names sharing a leading prefix of
   ≥ 3 chars terminated by `-` group together (e.g., `nextcloud-app`,
   `nextcloud-db`, `nextcloud-cron` → `nextcloud`). The 3-char minimum
   avoids spurious grouping.
3. **Singleton.** Standalone containers with no shared prefix get their
   own use-case named after the container.

Host-native services — systemd units that map to no container — are **not**
clustered, so their slug comes from the unit rather than from a cluster name.
**Slug = the unit stem**, normalised in this order — the order is load-bearing:

1. Drop the unit-type suffix (`.service`, `.socket`, …), lowercase, and fold any
   `@<instance>` into the stem as a hyphen.
2. Replace every remaining run of characters outside `[a-z0-9]` with a single
   `-`, then strip leading and trailing `-`.
3. Prefix `svc-` if the result starts with a digit or is shorter than two
   characters — including the empty string an all-punctuation stem leaves.
4. Truncate to 32 characters.
5. Strip any trailing `-`, whether step 3 or step 4 left it.

Prefixing **before** truncating is what keeps a 32-character digit-leading stem
inside the length bound, and the closing strip is what keeps a bare `svc-` (from
an empty stem) or a truncation landing on a hyphen out of the pattern's
trailing-hyphen trap. `postgresql@15-main.service` → `postgresql-15-main`;
`nginx.service` → `nginx`. The result must satisfy
`^[a-z][a-z0-9-]{0,30}[a-z0-9]$`; `edit-key` overrides it, subject to the same
pattern. This rule is what makes a re-run pick the same slug — which names the
document, tags the firewall rule the reconcile preserves for it, and keys the
Phase 5 collision guard.

Each is triaged per `native-services.md` into exactly one outcome:

1. **Tier 0, node infrastructure** — journal it as node infrastructure covered
   by `/bootstrap-node`. No document.
2. **Tier 1, mapped daemon** — emission contract per `native-services.md` §
   Tier 1. This file's own rule on top of that: the resulting document goes
   through the same cluster accept menu as a container cluster — adoption
   doesn't get a separate approval path.
3. **Tier 2, capture** — emission contract per `native-services.md` § Tier 2;
   the refusal condition on its empty `spec:` block is defined in
   `conventions/references/use-case-doc.md`. This file's own rule on top of
   that: it goes through the same cluster accept menu as the two cases above.
   Nothing onboarding writes is written without an `accept`. Its spec-derived
   menu lines render empty; `edit-key` still applies to its slug and summary,
   and the slug matters — it is what tags the document's ports when the
   firewall reconcile preserves them.

## Spec reverse-engineering rules

For each container in a cluster, derive `spec.containers.<name>` from
its `podman inspect` JSON. The output must conform to
`podman-quadlets` `## Render from spec` — see that section for
the full target schema. The reverse mapping:

| `spec` field   | Inspect source                                                                                   | Reverse rule                                                                                                                                                                                            |
| -------------- | ------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `image`        | `.ImageName` + `podman image inspect <id> --format '{{json .RepoDigests}}'`                       | **Pinned tag REQUIRED.** If the running tag is unpinned (`:latest`, no tag, or a moving tag), the agent prompts the admin: pick the `RepoDigest` form OR a semantic tag the admin pastes. |
| `network`      | `.NetworkSettings.Networks` keys                                                                 | If exactly one network: rename to `<slug>-<existing>` (or `<slug>-net` for default `bridge`). Multiple networks: warn, propose `<slug>-net` as a single merged network — admin approves cluster-level. |
| `env`          | `.Config.Env` filtered: names NOT matching the secret regex                                       | Preserve names + values. Skipped names go to `env_from`.                                                                                                                                              |
| `env_from`     | Set to `/etc/drydocc/secrets/<slug>.env` iff any env name matches `^[A-Z0-9_]*(_KEY\|_TOKEN\|_SECRET\|_PASS[A-Z0-9_]*\|_PASSWORD)$` | Emit a sibling `<slug>.secrets.yaml` listing matching names with `purpose: "adopted from existing container; verify"` and `rotates: never`. **Values are never recorded.** |
| `mounts`       | `.Mounts[*]`                                                                                     | All entries become bind mounts under `/var/lib/drydocc/volumes/<slug>/<volume>/` — no podman-managed named volumes. For each, propose `volume: <derived-name>` (basename of target or label-driven), `target: <existing-target>`. **Data migration required**: record the current `.Source` so `/apply-use-case` can copy from it during the cut-over. Nothing is copied or moved at onboarding time, and the admin pre-migrates nothing. Journaled per mount. |
| `ports`        | `.HostConfig.PortBindings`                                                                       | Translate to `<host>:<container>/<proto>`. Also propagate to top-level `ports:` summary list (validator-enforced equality).                                                                            |
| `depends_on`   | not derivable                                                                                    | Leave `[]`; journal "depends_on unknown — admin should populate before apply."                                                                                                                        |
| `restart`      | `.HostConfig.RestartPolicy.Name`                                                                 | Map `always` / `unless-stopped` → `always`; `on-failure` → `on-failure`; `no` / empty → `on-failure` (`drydocc` default).                                                                              |
| `exec`         | `.Config.Cmd`                                                                                    | Omit if matches the image's default; otherwise emit as a space-joined string. The raw array is journaled for traceability.                                                                            |

For cluster-level `spec.networks.<name>` and `spec.volumes.<name>`:
emit one entry per referenced name (the validator enforces that the
top-level `networks:`/`volumes:` lists exactly equal the keys under
`spec.*`). Networks get `internal: false` unless the discovered network
has no IPv4 gateway. Volumes get default `mode: "0750"` and
`owner: "doccap:doccap"`.

**Top-level frontmatter** computed last:
- `name` = the document's slug (equals filename stem) — the cluster slug for a
  container cluster, the derived unit slug (§ "Service discovery rules") for a
  host-native service.
- `status: planned`, `owner: doccap`, `created: <today YYYY-MM-DD>`.
- `last_applied: null`, `last_applied_commit: null`.
- `summary`, per document kind. Editable in the accept menu:
  - container cluster — generated from cluster shape (e.g., `"Adopted: 3
    containers (nextcloud-app, nextcloud-db, eturnal) on nextcloud-net"`).
  - Tier 1 mapped daemon — `"Adopted: host unit <unit>, to run as container
    <image>"`.
  - Tier 2 capture — `"Captured: host unit <unit> — recorded, not yet
    rebuildable"`.
- `depends_on: []` always.
- `containers` / `networks` / `volumes` / `ports` computed from
  `spec.*` (invariant per the `conventions` skill (`references/use-case-doc.md`)).
- `has_secrets` = true iff any container emitted `env_from`.

The doc body must include the four required sections, plus the optional
`## Migration` section when adopting a host-native service, per
the `conventions` skill (`references/use-case-doc.md`). Three of the four
required sections are filled **per document kind** — a container cluster, a
Tier 1 mapped daemon, and a Tier 2 capture describe different things and offer
different next steps, and boilerplate written for one is false on the others.

`## Intent`
- container cluster — `"Adopted via /onboard-node on YYYY-MM-DD from running
  containers on <node>. Original purpose unknown; admin to fill in."`
- Tier 1 — `"Adopted via /onboard-node on YYYY-MM-DD from host unit <unit> on
  <node>. Original purpose unknown; admin to fill in."`
- Tier 2 — `"Captured via /onboard-node on YYYY-MM-DD from host unit <unit> on
  <node>. Recorded so the service is visible to the framework; this document
  cannot rebuild it as it stands."`

`## Architecture`
- container cluster and Tier 1 — auto-populated bullet list of containers,
  networks, ports, volume targets. Bind mounts to be migrated land under a
  "Data migration required" sub-bullet listing
  `source-on-box → /var/lib/drydocc/volumes/<slug>/<vol>/`.
- Tier 2 — there is no declared architecture, and the section says so. List
  instead what the probe found: the unit, its fragment path and `ExecStart`,
  its owning package, and its listening ports — the only record of the
  service's shape.

`## Runbook`
- container cluster — `"Containers <names> are currently running outside
  `drydocc` control. Run /apply-use-case <slug> on the node to
  adopt them. The cut-over is that command's job — do not stop anything or
  pre-migrate any data first."`
- Tier 1 — `"Host unit <unit> currently serves this outside `drydocc` control.
  Run /apply-use-case <slug> on the node to cut over to the
  container declared in `spec:`: it disables <unit>, copies the `## Migration`
  config and data paths into the declared volumes, and starts the container.
  The cut-over is that command's job — do not disable the unit or pre-migrate
  any data first."`
- Tier 2 — `"Host unit <unit> currently serves this outside `drydocc` control,
  and this document is not rebuildable as it stands: it declares no containers,
  and /apply-use-case refuses it for exactly that reason. The next step is
  human work — author a `spec:` block (a pinned image, its volumes, its ports)
  from the `## Migration` section below, then run /apply-use-case <slug>. Until
  then the unit keeps running, untouched."`

`## Migration` — emitted **only** for an adopted host-native service (Tier 1 or
Tier 2 per `native-services.md`), never for a container cluster. Labelled-line
schema per the `conventions` skill's `references/use-case-doc.md`.

`## Quadlets` — the standard pointer from the `conventions` skill
(`references/use-case-doc.md`), on every document kind.

## Cluster accept menu copy

Render this per proposed document after the spec is computed — container
cluster, Tier 1 mapped daemon, or Tier 2 capture alike, per § "Service
discovery rules":

```
[<slug>]  <source>
  image pin status:  <ok | unpinned: <which> | mixed | n/a>
  networks:          <list>
  volumes:           <name → target>  [<M> bind mount(s) need data migration]
  ports:             <list>
  has_secrets:       <true | false>   [<K> env var(s) routed via env_from]

  [a] accept       — scaffold doc + secrets stub
  [e] edit-key     — override image / summary / slug / depends_on
  [s] skip         — note in journal, no doc written
  [v] view-full    — print the full proposed YAML, then re-show menu
```

`<source>` names where the document came from, one of:

- `reverse-engineered from <N> container(s): <names>` — container cluster.
- `adopted from host unit <unit>` — Tier 1 mapped daemon.
- `captured from host unit <unit> — not yet rebuildable` — Tier 2 capture. It
  renders `image pin status: n/a` with empty `networks` and `volumes`; `ports`
  and `has_secrets` still carry what the probe found.

`edit-key` prompts for which key, then for the new value. `slug`
override renames the proposed document and all derived names (network
defaults, volume bind paths, env_from path, secrets file). On any unpinned
image in the document — a cluster's containers or a Tier 1 daemon's single
image alike — the agent forces a pinned-tag prompt before `accept` is offered.

## Failure handling

`/onboard-node` does **not** halt on a single non-zero exit (deliberate
divergence from `/bootstrap-node`). For each remediation:

1. Capture exit code + stderr + stdout (trimmed).
2. On non-zero exit: record the item's outcome as `apply-failed`,
   leave its `hardening.*` field as `unknown` in the node.yaml delta,
   journal stderr verbatim, continue to the next item.
3. On Phase 2 (pre-flight) or Phase 1 refusals: the run does halt —
   these are prerequisite failures, not item failures.
4. The whole-run `outcome` field is computed at finalization:
   `success` iff every item is `aligned` or `applied` AND every document
   offered at the accept menu — container cluster or adopted host-native
   service alike — was `accepted`; `partial` otherwise; `failed` only on a
   Phase 1/2 hard refusal mid-flight; `dry-run` if `--dry-run` was
   passed.

The initial SSH session is not held open across phases — onboard's
remediations are individually idempotent. If sshd remediation cut-over
fails, the existing session is left alive (per the bootstrap routine)
and both sshd fields revert to `unknown` in the delta.

## What this skill does NOT do

- It does not execute commands itself. It is reference material the
  agent reads when composing the next probe or apply.
- It does not hold remediation commands — those live in
  `bootstrap-node`. Apply means: read the linked anchor, fill
  templates, run.
- It does not fetch secrets — that contract is in `secrets`.
- It does not implement drift detection for use-cases — that is `/audit-use-case`. It
  only inventories at onboarding time and emits `status: planned`
  docs.
- It does not migrate data — neither a container's bind mounts nor a
  Tier 1 mapped daemon's config/data paths. That is `/apply-use-case`'s
  job. Onboarding only records where the live sources are; the admin
  pre-migrates nothing.
- It does not write quadlet files. Quadlets are
  rendered at apply time from `spec:` and live only on the node.
