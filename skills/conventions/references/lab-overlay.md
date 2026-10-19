# Lab overlay — `.drydocc/lab.yaml`

A **lab run** is a fork driven by `scripts/lab/lab.py`: its nodes are local
VMs and its `origin` is a local lab remote. A test run is throwaway and its
admin is a headless agent; a **prototype** is kept, and an admin works in it
(the `lab` skill). The lab overlay is the one file that tells a skill it is
running in one, whether anybody is watching, and what an absent admin would
have answered. It lives at the fork root
as `.drydocc/lab.yaml`, is written by the lab driver, and is **never
committed** (`.drydocc/` is gitignored). Its absence means a normal run: none
of the rules below apply.

## Schema

```yaml
lab:
  run: web-smoke-01            # run id
  mode: test                   # test | prototype
  attended: false              # true: an admin is at the keyboard
  upstream_ref: main@6b89a6d   # the framework ref this fork was cut from
  remote_url: git://127.0.0.1/web-smoke-01.git   # the fork's origin, as the laptop sees it
  tailnet: off                 # off | ephemeral
  time_scale: 1                # multiply every wait/poll; >1 for emulated guests
nodes:
  web:                         # one entry per lab node, keyed by node name
    initial: {ip: 127.0.101.2, port: 2222}   # where its initial root SSH answers
    arch: x86_64
answers:                       # decision id → the admin's answer
  bootstrap.source: running
  bootstrap.key-source: generate
```

- **`lab.attended`** — `true` only in a prototype while an admin works in it.
  Whenever a headless agent drives a step — in any run — it is `false`, and
  § "Unattended" and § "Decisions" apply.
- **`lab.remote_url`** — `origin` as the laptop resolves it. A node resolves
  the same URL to the lab remote too (the lab maps it on each VM), so the URL
  is handed to a node unchanged.
- **`lab.tailnet`** — `off`: no node joins a tailnet (§ "Tailnet off").
  `ephemeral`: nodes join a real tailnet with an ephemeral key from the
  environment (§ "Ephemeral tailnet").
- **`nodes.<node>.initial`** — the initial root connection for a node the lab
  presents as `source: running`. The root password is never in this file: it
  is in the environment as `DOCCAP_ROOT_PW`, set per step by the lab driver.
- **`answers`** — see § "Decisions".

## Unattended

Applies when `lab.attended` is `false`. Nobody resumes a lab session once it
ends — there is no next turn. Never end the session waiting on something to
happen later (a background poller, a "will report back"): wait for it within
the session, up to the overlay's `time_scale`-adjusted bounds, and then
finish — successfully, or by stopping the step with the reason recorded in
the journal as a re-runnable `partial`.

## Decisions

Every place a skill asks the admin something that a lab scenario must be able
to answer carries a stable **decision id**, written next to the question as
`[decision: <id>]`. In an unattended lab run (`lab.attended: false`):

1. Look up the id in `answers`. If present, use that answer exactly as the
   admin's reply and say so in the journal (`answered by lab overlay`).
2. If the question has **no** id, or the id has **no** answer, do **not**
   guess, pick a default, or infer intent. Write `.drydocc/lab-blocked.yaml`
   and stop the step:

   ```yaml
   skill: bootstrap-node
   step: "17"
   decision: bootstrap.key-source      # null when the question had no id
   question: "Generate a key or import one?"
   ```

   The lab driver reads this file — never the agent's prose — to tell a
   blocked run from a failed one.
3. Confirmations that would protect a real box ("destroy and rebuild?") are
   decisions like any other: answered only when the scenario says so.

Ids are `<skill>.<topic>`, kebab-case, and never reused for a different
question. Ids defined so far:

| id | skill · step | answers |
|---|---|---|
| `provision-vm.reprovision` | provision-vm · 2 | `reprovision` \| `exit` |
| `provision-vm.specs` | provision-vm · 6 | `{memory_mb, vcpus, disk_gb, arch, vm_name}` |
| `provision-vm.image` | provision-vm · 7 | a distro name, or a direct image URL |
| `bootstrap.rebootstrap` | bootstrap-node · 3 | `yes` \| `no` |
| `bootstrap.source` | bootstrap-node · 4 | `running` \| `vm` |
| `bootstrap.initial-connection` | bootstrap-node · 13 | `from-overlay` — use `nodes.<node>.initial` and `DOCCAP_ROOT_PW` |
| `bootstrap.key-source` | bootstrap-node · 17 | `generate` \| `import` |
| `bootstrap.fork-key-registered` | bootstrap-node · 30 | `lab-remote` — no deploy key exists to register (§ "Lab remote") |
| `create-use-case.continue-over-ssh` | create-use-case · 1b | `yes` \| `no` |
| `apply-use-case.continue-over-ssh` | apply-use-case · 1b | `yes` \| `no` |
| `apply-use-case.confirm-rebuild` | apply-use-case · 3c | `yes` \| `no` |
| `apply-use-case.confirm-adoption` | apply-use-case · 5 | `yes` \| `no` |
| `apply-use-case.confirm-render` | apply-use-case · 7 | `yes` \| `no` — the rendered quadlets (and any port-floor change) are accepted |
| `modify-use-case.continue-over-ssh` | modify-use-case · 1b | `yes` \| `no` |
| `modify-use-case.confirm-diff` | modify-use-case · 4 | `yes` \| `no` |
| `modify-use-case.confirm-render` | modify-use-case · 7 | `yes` \| `no` |
| `remove-use-case.continue-over-ssh` | remove-use-case · 1 | `yes` \| `no` |
| `remove-use-case.confirm` | remove-use-case · 2 | `yes` \| `no` |
| `audit-use-case.capture-back` | audit-use-case · 9 | a menu key per finding, e.g. `n` for every finding, or `i writable_layer/web, n the rest` |
| `backup.target-scope` | backup targets walkthrough · 1 | `fork` \| `node` |
| `backup.target-fields` | backup targets walkthrough · 2 | the target's type and fields, e.g. `rsync: name vault, host vault, port 42022, user doccap, root /srv/backups` |
| `backup.authorize-target-key` | backup targets walkthrough · 4, backup-use-case · 3.3 | an admin action (§ "Admin actions") |
| `backup.rebuild-guard` | backup-use-case · 2b | `yes` (start fresh) \| `no` |
| `backup.strategy` | backup-use-case · 3.2 | `accept` — take the proposal as shown — or the tuning to apply |
| `backup.on-demand-run` | backup-use-case · 4.5 | `yes` \| `no` |
| `restore.image-mismatch` | restore-use-case · 3 | `yes` \| `no` |
| `restore.confirm-live` | restore-use-case · 5 | `yes` \| `no` |
| `rebuild.replacement` | rebuild-node · 8 | `running` \| `vm` \| `comes-back` |
| `rebuild.confirm-plan` | rebuild-node · 13 | `all`, a list of use-case slugs, or `abort` |
| `onboard.initial-connection` | onboard-node · 3 | `from-overlay` — `nodes.<node>.initial` as root, `DOCCAP_ROOT_PW` from the environment; for the pubkey, generate `doccap-at-<node>` and offer its public half |
| `onboard.pin-image` | onboard-node · 10 | the pinned image per repository, e.g. `docker.io/library/nginx:1.27.3-alpine` |
| `onboard.accept-doc` | onboard-node · 11 | `accept` \| `skip` — applies to every proposed document |
| `onboard.reconcile` | onboard-node · 14 | per item `apply` \| `skip` \| `defer`; one answer may cover all items with exceptions, e.g. `apply every item except tailscale, which is defer` |
| `onboard.forwarding-consumer` | onboard-node · 14 | `none`, or what else routes packets on the box |
| `onboard.land-repo` | onboard-node · 18 | `yes` \| `no` |
| `maintenance.act` | node-maintenance · 7 | the menu keys to act on, e.g. `s file-integrity-verified` or `l, f`, or `n` (act on nothing) |
| `maintenance.reboot` | node-maintenance · 7 | `yes` \| `no` |
| `maintenance.promote` | node-maintenance · 7 | `no`, or the ids to promote to `auto` |
| `maintenance.enable-sampler` | node-maintenance · 8 | `yes` \| `no` |
| `node-context.replace-unfinished` | node-context · Resume pointer 1 | `yes` (drop the unfinished run) \| `no` |
| `continue-last-command.still-running` | continue-last-command · 4.1 | `yes` \| `no` |
| `continue-last-command.leftovers` | continue-last-command · 4.3 | `keep` \| `discard` |
| `continue-last-command.resume` | continue-last-command · 4.4 | `resume` \| `abandon` |

An open-ended question — "anything unclear?" — deliberately has no id: in a
lab run it blocks, because the scenario's description was ambiguous and
should say more.

## Attended sessions

With `lab.attended: true` an admin is present: ask every question as usual
and take their answer — `answers` and the blocked file are for headless steps
only. Everything else in this file still holds: the tailnet is off, `origin`
is a lab remote, and the vault is out of reach, so a secret not in the
environment is one the admin supplies by a `secrets` fallback, never from the
vault. After each successful `/apply-use-case` or `/audit-use-case`, follow
the `lab` skill's § "Prototype session".

## Admin actions

Some decisions are not a word but a thing the admin does outside the
framework — authorizing a public key on a backup target, for one. In a lab,
the targets of those actions are lab VMs, and the lab performs them: an
answer of the form `lab-admin <action> <args>` means *run exactly that
command* (the run's `PATH` provides `lab-admin`) as the admin would have
done the step, feeding it what the step would have shown the admin — a
public key goes to its standard input. Then continue as if the admin had
confirmed. A non-zero exit is the admin refusing: stop the step and record
it.

| action | does |
|---|---|
| `lab-admin authorize-key <node> <user>` | appends the public key read from standard input to `<user>`'s authorized keys on lab node `<node>` |

## Tailnet off

With `lab.tailnet: off`, every step that would use the tailnet takes its
documented non-tailnet path instead, and records why:

- **Join** — skipped. `hardening.tailscale` is recorded as `deferred`: the
  node is hardened, but its tailnet join is still owed. A runbook extracted
  from the lab lists the join as a step for the real box.
- **Reachability checks** — the tailnet check is skipped; every check that
  has a keyed-SSH form on the fallback port runs in that form and must pass.
- **Commands addressed as `tailscale ssh doccap@<tailscale_hostname>`** — run
  as keyed SSH on the fallback port to the same host name instead. The lab's
  SSH configuration resolves node names, so the host name stays the same.
- **Firewall** — rules that name the tailnet interface are still written, so
  the box's posture matches a real node.

## Ephemeral tailnet

With `lab.tailnet: ephemeral`, nodes join a real tailnet, so the join and
everything after it run as on a real node — with three differences:

- **The key** is in the environment as `DOCCAP_TS_KEY`, put there by the lab
  driver. It is the admin's ephemeral, pre-approved, tagged key: a node using
  it leaves the tailnet once it goes offline, and the lab logs every node out
  when it destroys or replaces the box. Resolve it from the environment only
  (§ "Secrets in a lab run"); it is never written anywhere.
- **The laptop's own tailnet client is out of reach** of a lab run — `tailscale
  ssh` from the laptop fails as it does when the client's daemon is
  unreachable. Take the documented fallback: reach the node at the tailnet
  address the node itself reports, never by a tailnet name, which on a real
  tailnet could belong to another device. In a lab run this fallback is the
  expected path, not a sign of a broken Tailscale SSH listener.
- **Peers** still resolve each other's names over the lab network.

Everything else holds: `hardening.tailscale` becomes `joined` once the node is
up and reachable over the tailnet, and a node joined to a tailnet is never
sealed into a layer or a restore point — every clone would be the same device.

## Lab remote

When `origin` resolves to `lab.remote_url`, the fork's origin is a lab remote:
a local repository the lab serves read/write to its own VMs, with no
authentication and no git host behind it. Hand the URL to a node unchanged
(never convert it to another URL form), and skip every git-host step — there
is no deploy key to register, so the node's own key is still generated, but
registration is answered by `bootstrap.fork-key-registered: lab-remote`.
Read and write verification still run and must pass.

## Checks from off the node

Some verifications must be made from off the node: a published port answers,
a closed one does not. A lab node's ports are not reachable from the client;
the lab forwards only SSH. When the run has another node, make the off-node
check from that peer over the lab network. When it has none, run the on-node
half of the check and name the off-node half in the journal entry as *not
verifiable in a lab run*. It does not lower the step's outcome, which is what
the checks that could run say. Never skip such a check silently.

## The hypervisor is out of reach

A lab node is reached the way a real one is: over SSH, as the skills say. The
host that runs its VM is not part of the infrastructure under test, so the
virtualization and disk-image tools are unavailable in a lab run. A node that
stops answering SSH — a lockout, a firewall rule, a ban — is a finding.
Record it as a real admin would see it (the journal entry's `outcome:
failed`, with what was last changed) and end the step. Never repair the node
from the host; a real box has no host to repair it from.

## Secrets in a lab run

Secrets come from the environment only. The vault client is unavailable in a
lab run by design, so a secret that is not in the environment is a missing
answer (§ "Decisions"), not a prompt for the admin. Lab secret values are
never written to the fork, the journal, or a runbook.
