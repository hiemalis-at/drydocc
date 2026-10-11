# Distro-agnosticism — the derivation contract

Authoritative rules for keeping drydocc portable to systems nobody has written
down yet. Enforced as invariant 5 in `AGENTS.md`; the blocklist at the end of
this file is machine-readable, so a tool can enforce it too.

## The premise

The **target shape** is universal: a podman surface, use-cases as OCI containers,
an append-only markdown journal, node state in git. The **dialect** that gets a
given box there is not universal, is not enumerable at authoring time, and is
therefore never written down in advance. It is discovered on the node, verified,
persisted to `node.yaml:system_facts`, and journaled.

Three passes failed because they tried to enumerate distros. Do not enumerate
distros. Teach the agent to derive, and give it a way to check its own work.

## Rule 1 — the generic layer names no tool

The generic layer is: the repository root files, `docs/`, every SKILL.md body, and
every `references/` file that is not a `*-recipes.md`. It never names a distro, a
package manager, a firewall tool, an init system, or a MAC framework — not as a
command, not as an example, and not as a schema field name or enum value
(`hardening.firewall`, never `hardening.ufw`).

Refer instead to the fact: *"the node's firewall tool
(`system_facts.firewall.tool`)"*. Blocked terms are listed at the end of this file.

## Rule 2 — recipe sections are outcome contracts

Concrete syntax lives only in a workflow skill's `references/*-recipes.md`. Every
section there takes this shape:

```markdown
## <Outcome, stated as an end state — never a tool name>

**Goal.** The invariant end state, distro-neutral. What is true after this step,
phrased so it stays true on a system whose tooling nobody has seen.

**Fact.** Which `system_facts` key selects the dialect, and the probe to run when
that key is absent or `unknown`. Probes ask the system what it has; they never
infer from `distro`.

**Deriving an unseen dialect.** The properties any implementation must have, so an
agent meeting an unknown tool can recognise the equivalent. Name the *shape* of
the answer, not an answer. Say what to do when nothing on the box has those
properties.

**Verification.** The command that proves the goal, independent of how it was
reached. **Do not proceed on an unverified derivation.**

**Record.** Write the discovered fact to `node.yaml:system_facts`, journal it, and
append the verified idiom to Observed dialects below.

### Observed dialects — evidence, not the supported list
| fact value | command | verified on | date |
|---|---|---|---|
```

All five parts are mandatory. A section missing **Verification** is incomplete and
must not ship: verification is the entire safety argument for letting an agent
derive commands against a live box. It converts guessing into a bounded loop —
propose, verify, record, or refuse.

## Rule 3 — Observed dialects are evidence, not a supported list

The tables hold commands that were verified on a real node, annotated with where
and when. They are a head start, not an authority:

- An agent **may** reuse a row whose fact value matches the node — after running
  the section's Verification step, never instead of it.
- An agent **must** extend the table when it derives and verifies a new dialect.
  That is how the repository learns.
- A missing row means *nobody has been there yet*, never *unsupported*.
- A row that fails verification is corrected in place, with its provenance
  updated. A distro's own tooling drifts across its releases; a stale row is a
  normal outcome, not an incident.

Distro and tool names are permitted **inside these tables only**, where they are
provenance for an observation rather than a branch condition.

## Rule 4 — refusal turns on capability, never identity

Never refuse a box for what it calls itself. Refuse when:

- an outcome cannot be **verified** after a derivation attempt; or
- a capability the target shape genuinely requires is **absent**.

There is exactly one hard capability requirement: `service_manager: systemd`,
because quadlets are a systemd feature. Everything else is derivable.
State the reason and the failing check in the refusal, so the admin can act:

> `Refusing: quadlets require systemd; system_facts.service_manager is 'openrc'.`

not

> `Refusing: <distro> not supported.`

## Rule 5 — facts are open vocabularies

`system_facts` values are documented to make facts greppable, **not to close the
set**. Any value a probe returns is legal — record it verbatim and journal it.
Write enum comments so this is unmistakable:

```yaml
package_manager: apt   # apt | dnf | dnf5 | zypper | pacman | apk | unknown
```
preceded by the block comment that governs every one of them:
```yaml
# Common values are listed to make facts greppable, NOT to close the set.
# Any other value a probe returns is legal — record it verbatim and journal it.
```

Adding support for a new dialect must never require editing the generic layer. If
it does, the fact was modelled wrongly.

## Rule 6 — a fact earns its slot

Add a key to `system_facts` only when a skill **branches on it today** or **breaks
without it**. Speculative facts rot: nothing reads them, nothing maintains them,
and they become misleading. For probe-on-demand tool presence, use the open
`capabilities:` map instead of a new top-level key.

Rejected candidates, recorded so they are not re-litigated:

| Candidate | Verdict |
|---|---|
| lingering mechanism | Derivable from `service_manager`. No slot. |
| user-creation tool | One-shot at bootstrap, never re-read. Handle as an outcome contract (goal: the user exists with a home directory, a shell, and sudo; verify with `id`), not a fact. |
| CA trust store, time-sync daemon, unattended-upgrade mechanism | No consumer today. No slot until one exists. |
| cgroup version, subuid/subgid ranges | Collapsed into the single `rootless_ready` verdict — skills care whether rootless works, not which ingredient was missing. |

## Worked example

The firewall port-allow case, in full. It is the richest example available: it
carries a tag mechanism, two structurally different rule models, and two
documented places where the abstraction genuinely leaks.

```markdown
## Host firewall permits <port>/tcp, tagged <tag>

**Goal.** `<port>` is reachable from off-node, and the rule carries a
machine-readable tag, so `/audit-use-case` and `/remove-use-case` can identify
exactly the rules a given use-case owns and leave everything else alone.

**Fact.** `system_facts.firewall.{tool,rule_model,tag_mechanism}`. When unknown,
probe: `command -v` across candidates, then ask each what it manages — a tool that
is installed but not managing the live ruleset is not the answer. Record `tool:
none` if nothing is filtering, and say so in the journal rather than silently
proceeding.

**Deriving an unseen tool.** A taggable rule needs two properties: (a) a free-text
field that survives a write/read round-trip, and (b) a read-back command that
echoes that field. Check the tool's own help output for a comment or description
attribute first. If it has none, look for its nearest free-text carrier —
firewalld has no comment attribute, which is why the tag rides `log prefix=` and
why every framework firewalld rule is a rich rule rather than a plain
`--add-port`. If no carrier exists at all, record `tag_mechanism: none` and stop;
do not invent a silent convention that `/remove-use-case` cannot read back.

Two exceptions are structural rather than tool-specific, and will recur:
- **Interface allows often cannot carry a tag.** A rule matching an interface
  rather than a port may not accept free text; leave it untagged and note it.
- **Pre-existing baseline rules keep their original wording.** Retagging a rule on
  a live node orphans the old one and can drop the connection you are using.

**Verification.** Both, every time: from off-node the port answers, **and** the
read-back command emits the tag. A rule that exists but cannot be read back is a
rule `/remove-use-case` will later fail to clean up.

**Record.** `system_facts.firewall.*`, the journal entry, and the table below.

### Observed dialects — evidence, not the supported list
| tool | rule_model | add | read back | verified on |
|---|---|---|---|---|
| ufw | rule-list | `ufw allow <port>/tcp comment '<tag>'` | `ufw status numbered` | test-02, 2026-06 |
| firewalld | zone | rich rule carrying `log prefix="<tag> "` | `firewall-cmd --list-rich-rules` | test-02, 2026-06 |
```

## Blocklist

Machine-readable: the fenced block below holds one term per line, matched case-insensitively on word boundaries. Hits are
permitted only inside an `### Observed dialects` table (from that heading to the
next heading of the same or higher level) and inside this file.

```blocklist
ubuntu
debian
fedora
rhel
centos
rocky
almalinux
opensuse
suse
alpine
archlinux
raspbian
apt
apt-get
dpkg
dnf
dnf5
yum
zypper
pacman
apk
rpm
ufw
firewalld
firewall-cmd
nftables
iptables
selinux
semanage
setsebool
restorecon
chcon
apparmor
aa-status
aa-complain
aa-enforce
preseed
kickstart
autoinstall
anaconda
subiquity
debian-installer
useradd
adduser
```

Terms deliberately **not** blocked, with reasons, so the list is not widened by
reflex:

- `systemd`, `systemctl`, `loginctl`, `journalctl` — systemd is a stated hard
  requirement (Rule 4), not a dialect. Naming it is accurate.
- `podman`, `quadlet` — the target shape itself.
- `openssh`, `dropbear`, `sshd` — already modelled as `ssh.server` with satellite
  fields; the SSH server is named in the schema on purpose.
- `arch` as a bare word — collides with CPU architecture (`node.yaml:arch`,
  `uname -m`) far more often than with the distro. `archlinux` is blocked instead;
  reviewers catch the rest.
