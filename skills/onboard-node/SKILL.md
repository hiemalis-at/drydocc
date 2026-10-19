---
name: onboard-node
description: Adopt an existing, already-configured Linux box into the framework. Inventories live state, then reverse-engineers running containers and host-native services into planned use-case docs BEFORE it classifies and reconciles hardening drift per-item with admin approval, so the firewall reconcile can preserve and tag the ports the adopted services need. Hands each scaffolded slug to /apply-use-case. Use for a Linux machine with systemd and SSH that already runs services.
compatibility: Runs inside a drydocc fork; needs a shell and file edits.
metadata:
  skillset: drydocc
  requires: bootstrap-node conventions secrets node-context podman-quadlets apply-use-case
---

# /onboard-node

## Contract

- **Args:** `<slug>` (required; kebab-case node name). Optional flags:
  `--dry-run` (classify + scaffold only; perform no remediation; journal
  `outcome: dry-run`), `--skip-services` (skip Phases 4–5: service
  discovery, triage, and use-case scaffolding).
- **Runs on:** the admin's client laptop, NOT the node — the box has no fork
  yet, so the client-orchestrated model applies. The
  preferred on-node apply surface kicks in only for the post-onboard
  `/apply-use-case <slug>` runs.
- **Reads:**
  - `nodes/<self>/node.yaml`.
  - Live state on the box (read-only sweep per the probe matrix in
    `references/onboard-recipes.md` § "Inventory probes").
  - `DOCCAP_TS_KEY` resolved env-var-first (`secrets` § "Session secrets")
    only if the tailscale-join remediation is approved — keepass skill or a
    `.env`.
- **Writes (on the box, via SSH, only when admin approves a
  remediation):**
  - Surface is the same as `/bootstrap-node`'s: `/etc/drydocc/node-name`,
    `/home/doccap/.ssh/authorized_keys`, `/etc/sudoers.d/doccap`,
    `/etc/fail2ban/jail.d/sshd.local`, the sshd drop-in fragment at
    `node.yaml:ssh.config_include_dir` (an in-place edit of the main sshd
    config when that fact is `null` — see `bootstrap-node`'s SSH lockdown
    recipe), the firewall tool's persistent store (per
    `system_facts.firewall.tool`), `/etc/sysctl.d/90-drydocc-hardening.conf`,
    root's authorized keys (moved aside, never deleted), the name resolver's
    drop-in or a responder-only daemon's unit state, and the file-integrity
    tool's exclusions and database (installing the tool
    through `system_facts.package_manager` when it is absent). Nothing is
    written on the box unless an `apply` choice elects it.
  - `/home/doccap/.ssh/drydocc-fork{,.pub}` and `/home/doccap/<repo>`
    (doccap:doccap; the node's own deploy key and the fork cloned with it)
    iff Phase 8 is opted into.
- **Writes (in the repo):**
  - Scaffolds `nodes/<slug>/` if absent (Phase 1 step 2).
  - Updates `nodes/<self>/node.yaml`: `system_facts` and `identity` from the
    Phase 2 probe, `hardening.*` per reconcile delta, and `remote_ide` from the
    Phase 9 probe. `last_bootstrapped` is set ONLY if every `hardening.*` field
    reaches baseline AND cut-over verification passes during this run.
  - Adds one `nodes/<self>/use-cases/<slug>.md` (status: planned) per accepted
    cluster and per adopted host-native service, plus optional sibling
    `<slug>.secrets.yaml` stubs. No `<slug>.quadlets/` directory — quadlets are rendered on apply.
  - Appends one journal entry under `nodes/<self>/journal/` with
    `scope: onboarding`.
- **Journal scope:** `onboarding` — one entry covering the whole run,
  per-item sub-headings inside `## Commands`. See
  the `conventions` skill's `references/journal.md`.

## Prerequisites the admin must satisfy

- An existing Linux box reachable from the admin's laptop over SSH
  (initial credentials may be the long-standing admin user the box was
  set up with; sudo access required for the hardening probes that need
  it).
- A `<slug>` is supplied; onboarding scaffolds `nodes/<slug>/` if absent.
- `last_bootstrapped` is `null` on that node profile.
- A way to supply `DOCCAP_TS_KEY` (`secrets` § "Session secrets") — needed
  only if the admin opts into the tailscale-join remediation. It resolves
  lazily.
- Admin rights on the fork's git host, to add the node's deploy key with
  write access if the Phase 8 clone is opted into.

## Procedure

Every shell command runs over SSH from the admin's client; every
command is captured (cmd, exit code, stdout summary, stderr trimmed)
for the journal in Phase 9.
As each phase starts, set its number as `phase` in the resume pointer
(`continue-last-command` § "Resume pointer"), so an interrupted run shows where
it stopped.

### Phase 1 — Bind & gather

1. Invoke `node-context`. On the admin's laptop,
   `/etc/drydocc/node-name` does not exist; `<self>` is the `<slug>`
   argument. Validate `<slug>` against `^[a-z][a-z0-9-]{0,30}[a-z0-9]$`.
2. If `nodes/<self>/` does not exist, **scaffold** it (a `node.yaml` stub
   with `name`, `doccap_user`, `ssh_fallback_port`, `tailscale_hostname`,
   `hardening.*: unknown`, `created: <today>`, `last_bootstrapped: null`,
   plus empty `journal/` and `use-cases/`) — as `/bootstrap-node` Phase 0
   does. Then read `nodes/<self>/node.yaml`.
   Refuse if `last_bootstrapped` is set:
   - Print: `Node <self> already has last_bootstrapped set (<date>). Re-run /bootstrap-node <self> for an audit-&-repair pass instead — onboarding is for boxes that have never been bootstrapped.`
3. Prompt the admin interactively `[decision: onboard.initial-connection]`:
   - `<initial-host>`: defaults to `node.yaml:tailscale_hostname` if the
     box is already on the tailnet (test with `tailscale status | grep`),
     otherwise admin pastes an IP/hostname.
   - `<initial-port>` (default `22`).
   - `<initial-user>` (default `root`; many existing boxes use a
     long-standing admin user instead — admin pastes that).
   - Auth method: ssh-agent (default), `key-path`, or `password`
     (passed via `sshpass` or interactive prompt, never logged).
   - `pubkey`: admin pastes a public key string or path to a local
     `*.pub` file. Stored for the `install-pubkey` remediation if the
     admin chooses to apply it.
4. Do NOT fetch any bootstrap secrets yet — Phase 7 fetches lazily as
   remediation choices need them.

### Phase 2 — Pre-flight

5. Verify reachability and record OS/dialect facts:
   ```bash
   ssh <initial-creds> 'cat /etc/os-release && echo --- && uname -m'
   ```
   Parse `/etc/os-release` defensively: `ID=` and `VERSION_ID=` may be
   quoted or absent, and a derivative distro may set only `ID_LIKE=` — fall
   back to `ID_LIKE`'s first token when `ID` is missing, and record
   `distro_version: unknown` rather than failing the parse when
   `VERSION_ID` is absent. Compare the parsed `distro`/`distro_version` to
   `node.yaml:distro`/`distro_version`.

   `arch` is recorded **verbatim** from `uname -m`, never normalised
   (`references/node-profile.md`). Compare it to `node.yaml:arch` by
   normalising both sides first (e.g. folding `amd64` and `x86_64` to one
   canonical form) — a spelling difference between probe and schema is not
   a mismatch and must not trip the refusal below.

   On a genuine distro/version/arch mismatch — the box is plainly not the
   node the profile describes — refuse with a diff:
   - Print: `Distro mismatch — node.yaml says <X> <V> <A>, box reports <Y> <W> <B>. Refusing.`

   Then probe and write the rest of the `system_facts` ledger —
   `package_manager`, `service_manager`, `container_engine`, `mac_framework`,
   `firewall.{tool,rule_model,tag_mechanism}`, `rootless_ready`,
   `capabilities.{python3,rsync,tmux}`, and `.detected` — discovered facts,
   per the `conventions` skill's `references/node-profile.md`, the same way
   `bootstrap-node` does at its OS-discovery step. Never infer these from
   `distro` alone. The same bundled probe (§ "Inventory probes")
   also emits `machine_id_hash` / `host_key_fingerprint`; write these plus
   `identity.first_seen: <today>` to `node.yaml:identity`. This is
   always a first write here — `/onboard-node` refuses re-runs outright
   (step 2) rather than auditing drift.

   Refuse on capability, never on identity: quadlets are a systemd feature,
   the one hard requirement. If the probe
   reports `system_facts.service_manager` as anything other than `systemd`,
   refuse:
   - Print: `Refusing: quadlets require systemd; system_facts.service_manager is '<value>'.`

   Every other dialect difference is derived from the facts just recorded at
   apply time — it is never a refusal condition.
6. Verify outbound network from the box (only the endpoints onboarding
   might need): `https://github.com` (Phase 8 clone) and
   `https://login.tailscale.com` (tailscale-join remediation). A
   non-2xx response is logged as a warning, not a refusal — onboarding
   may still be useful even if the box is offline (admin can skip
   network-bound remediations).

### Phase 3 — Inventory

7. Run the bundled probe matrix from `references/onboard-recipes.md` §
   "Inventory probes" as a single `ssh <initial-creds> "bash -s" <<'PROBE'
   … PROBE'` invocation. Parse `<key>=<value>` lines into an in-memory
   inventory record. Capture the full session for the journal.
8. For each discovered podman/docker container, run the per-container
   `inspect` probe from the same skill section, as the container's owner. That
   is the connecting user, or the lingering user whose inventory listed it.
   Another user's rootless containers are invisible to everyone else's
   `podman`. **Container env values
   are split on `=` and the right-hand sides are discarded
   immediately** — only variable names cross into the inventory.

### Phase 4 — Discover & triage services

Discovery and scaffolding are read-only against the box: clustering uses data
Phase 3 already collected, and the per-unit probes triage adds
(`references/native-services.md` § "Probes") only read. The sole writes are
in-memory repo deltas. They run before hardening so that every use-case slug
exists by the time the firewall reconcile needs to tag a preserved rule with
its owner.

9. If `--skip-services` was passed, skip Phases 4 and 5 and continue at
   Phase 6. Otherwise, cluster the discovered containers per
   `references/onboard-recipes.md` § "Service discovery rules" (compose label →
   shared prefix → singleton).

   Host-native services are not clustered. They are triaged per
   `references/native-services.md` into its three outcomes;
   `references/onboard-recipes.md` § "Service discovery rules" says, per
   outcome, which produce a document and how it is accepted.
   `--skip-services` skips all of it.

### Phase 5 — Reverse-engineer and scaffold use-case docs

10. For each cluster, and for each host-native service that produces a
    document, compute the proposed `spec:` block + frontmatter
    per `references/onboard-recipes.md` § "Spec reverse-engineering rules". If any
    container's image is unpinned, prompt the admin for a pinned tag
    or `RepoDigest` choice `[decision: onboard.pin-image]` **before** rendering the accept menu (a doc
    with `:latest` is a validation error).
11. Render the accept menu (`references/onboard-recipes.md` § "Cluster
    accept menu copy") once per proposed document `[decision: onboard.accept-doc]`. Which ones go through it —
    container cluster, Tier 1 mapped daemon, Tier 2 capture — is ruled by that
    file's § "Service discovery rules". Branch:
    - **accept**: write `nodes/<self>/use-cases/<slug>.md` to the
      in-memory file delta with `status: planned`,
      `last_applied: null`, `last_applied_commit: null`, the computed
      `spec:` block, and the body sections `references/onboard-recipes.md`
      § "Spec reverse-engineering rules" specifies. If `has_secrets: true`,
      also write
      `nodes/<self>/use-cases/<slug>.secrets.yaml` listing the
      matched env names (purpose: `"adopted from existing container;
      verify"`, rotates: `never`). **Values never appear.**
    - **edit-key**: prompt for which key, apply override, re-render
      menu.
    - **skip**: journal the proposed document's discovered facts under
      `## Notes / drift detected` so the admin can scaffold manually
      later via `/create-use-case`.
12. If a use-case doc with the proposed slug already exists in the
    repo, skip writing and journal `"drift detected: <slug> is live on
    the box but its use-case doc already exists — left untouched"`.

### Phase 6 — Classify hardening

13. For each of the nine `hardening.*` fields, apply the rules from
    `references/onboard-recipes.md` § "Hardening classification rules" against the
    probe output. Build a `drift_report` array; each entry carries
    `{field, current, desired, classification, bootstrap_anchor,
    needs}`. Print the drift report table to the admin (aligned items
    listed but not actionable; drift/unknown items are the agenda for
    Phase 7).

### Phase 7 — Reconcile hardening (per-item, interactive)

14. For each `drift_report` entry where `classification != aligned`:
    render the menu from `references/onboard-recipes.md` § "Reconciliation menu
    copy" `[decision: onboard.reconcile]`. Branch on the admin's choice:
    - **apply**: read the bootstrap idiom at `bootstrap_anchor`, fill
      templates from `node.yaml`, compose the full `ssh <initial-creds>
      "<command>"` invocation, run it, capture output. On success: write
      the post-apply value to the in-memory `node.yaml` delta. On
      non-zero exit: write `unknown` to the delta, journal
      `apply-failed` with stderr, continue to the next item (no
      global halt).

      For `hardening.firewall`, the anchor is
      `bootstrap-node/references/hardening-recipes.md` § "Adoption variant" —
      **not** § "Full hardening". Onboarding never resets an adopted box's rule
      set; it preserves every existing rule and pre-allows `at_risk_ports`,
      each tagged with its owning use-case slug.
    - **skip**: write the **observed current value** honestly to the
      delta. Journal as `skipped (admin declined)`.
    - **defer**: write `unknown` to the delta. Journal as
      `deferred to /bootstrap-node`.
    - **view**: print the filled-in command, re-show the menu.
15. Composite doccap-user item (user + sudoers + pubkey + linger) is
    a single menu choice. Treat all four bootstrap anchors as one
    transaction: any non-zero exit during the sequence flips the
    composite outcome to `apply-failed` for that item.
      For `hardening.kernel_hardening`, the forwarding verdict comes from the
      probe, and an admin answer can add to it. Forwarding is never turned off
      under a consumer the probe or the admin named. The menu preamble shows
      the verdict before the admin chooses, and asks whether anything else
      routes packets `[decision: onboard.forwarding-consumer]`.

      For `hardening.file_integrity`, an `apply` is **queued, not run**. The
      database is initialised in step 18c, after everything else this run
      changes on the box.
16. If any sshd-affecting apply ran (`ssh_root_login`, `ssh_password_auth`
    or `ssh_session_policy`), perform the cut-over routine from
    `bootstrap-node/references/hardening-recipes.md`
    § "SSH-key cut-over routine (Phase 2)" once, after
    the last sshd apply. On failure: both sshd fields revert to
    `unknown` in the delta; the initial session is left intact; journal
    the failure and continue with the rest of the run.
17. If `tailscale` drift was `apply`-chosen, resolve `DOCCAP_TS_KEY` per
    `secrets` § "Session secrets" (env-var-first) at this point
    (lazy — admins who skip never trigger a resolution). Run the
    `tailscale up …` idiom and poll for online status.

### Phase 8 — Land repo on node (recommended)

18. If the admin opts in `[decision: onboard.land-repo]`, give the node write access to the fork and clone
    it, over the existing SSH session with every command run as doccap
    (`ssh <initial-creds> "sudo -u doccap -H bash -s"`), per
    `bootstrap-node/references/hardening-recipes.md` § "Fork write access":
    the node generates its own deploy key, the admin registers it **with
    write access**, and the clone, its git identity, and a read *and* write
    verification follow. An existing working copy whose `origin` is already
    the SSH URL and whose verification passes is left as is; any other is
    re-pointed, not re-cloned. A failed verification is journaled as an apply-failed
    item and the run continues — the node cannot push until it is fixed.
    If the admin declines, print the manual steps from that section and
    continue.
    Without the fork on the node, the post-onboard
    `/apply-use-case <slug>` apply path won't work — but onboarding
    itself still completes.
### Phase 9 — Finalize

18b. **Probe VS Code Remote Server compatibility**, over the
    established SSH session — same read-only sweep as `/bootstrap-node`
    § "Phase 4" step 29b (`uname -m`, `ldd --version`, outbound reach of
    `https://update.code.visualstudio.com`). Classify
    `remote_ide.vscode_server` (`compatible | incompatible | unknown`) + a
    `reason` for `incompatible`. Read-only; never halts the run. Note: an
    adopted box may lack `tmux` (which backs `node-exec` lingering) — the
    nohup fallback applies, or the admin installs it via
    `system_facts.package_manager`.

18c. **Last on the box.** If a `file_integrity` apply was queued in Phase 7,
    initialise it now: after Phase 8's clone and step 18b's probe, before
    anything else touches the node,
    per `bootstrap-node/references/hardening-recipes.md` § "File integrity",
    so the database records the box as this run leaves it. Hold the
    `sha256:` hash for the journal. A non-zero exit sets the field to
    `unknown` and is journaled `apply-failed`, the same as any Phase 7 item.

19. Update `nodes/<self>/node.yaml`:
    - For each `hardening.*` field: write the value from the reconcile
      delta (per-item resolution from Phase 7).
    - Write the `remote_ide` block from the step-18b probe (`vscode_server`,
      `reason`, `probed: <today>`).
    - Set `last_bootstrapped: <ISO8601 UTC timestamp>` **only if**
      every `hardening.*` field is now at baseline (`firewall: enabled`,
      `fail2ban: enabled`, `ssh_root_login: disabled`,
      `ssh_password_auth: disabled`, `ssh_session_policy: enabled`,
      `kernel_hardening: enabled`, `file_integrity: enabled`,
      `name_responders: disabled`, `tailscale: joined`) AND the
      cut-over verification routine passed during this run. Otherwise
      leave `last_bootstrapped: null` so the lifecycle is honest about
      the box's state.
20. Append a journal entry at
    `nodes/<self>/journal/YYYY-MM-DD-HHMM-onboard.md` per
    the `conventions` skill's `references/journal.md`:
    - frontmatter: `scope: onboarding`, `outcome: success | partial |
      failed | dry-run`, `trigger: /onboard-node[+ flags]`,
      `commit_before` / `commit_after`, current actor model id.
    - `## What was done` — grouped bullet list: "Aligned (no action):
      …", "Applied: …", "Skipped: …", "Deferred: …", "Apply-failed: …",
      "Use-cases scaffolded: …", "Use-cases skipped: …".
    - `## Commands` — every probe, every applied fix, every cut-over
      check, every accept-menu preview, captured verbatim with exit codes
      and trimmed stdout/stderr. Sub-headed by item:
      `### Probe: hardening.firewall`, `### Apply: hardening.ssh_root_login`,
      `### Apply: hardening.file_integrity` (path and `sha256:` hash),
      `### Cut-over verification`, `### Cluster: nextcloud`,
      `### Service: nginx.service`, etc.
    - `## Notes / drift detected` — narrative for every drift item the
      admin skipped, every cluster and every host-native service the admin
      skipped at the accept menu (with discovered facts), every host-native
      service triaged as node infrastructure — no document is produced for
      those, so the journal is their only record — every lingering user's
      user timers (`user_timer=`), every user whose containers could not be
      listed (`podman_user=…|no-runtime-dir`), and every non-hardening
      anomaly observed (docker + podman coexisting, bind sources that will
      need rsync at apply time).
    - **NEVER include `$DOCCAP_TS_KEY` or any container env value.** Redact
      `--authkey ` lines when capturing commands.
21. Commit and push:
    ```bash
    git add nodes/<self>/node.yaml nodes/<self>/use-cases/ nodes/<self>/journal/<entry>.md
    git commit -m "node(<self>): onboard existing box"
    git pull --rebase && git push
    ```
    (`conventions` `references/journal.md` § "Committing".) If Phase 8
    cloned the fork, bring the node's working copy up to this commit:
    `git -C ~doccap/<repo> pull --ff-only`, run as doccap.
22. Unset `DOCCAP_TS_KEY` from the agent's environment.
23. Print summary:
    - Reconcile outcome counts (aligned / applied / skipped / deferred
      / apply-failed).
    - List of scaffolded slugs, and of the clusters and host-native
      services the admin skipped.
    - The connection target the admin should use for the on-node apply:
      `tailscale ssh doccap@<tailscale_hostname>` if tailscale was
      joined this run, or the existing SSH endpoint otherwise.
    - Next command per scaffolded slug, choosing the surface from the
      `remote_ide` verdict: if `vscode_server: compatible`,
      `/apply-use-case <slug>` from a VS Code Remote-SSH terminal on the node
      (preferred); otherwise drive it from this laptop with the node
      as the first arg: `/apply-use-case <node> <slug>`.
      `/apply-use-case` performs the cut-over itself, reversibly; the admin does
      not stop anything or pre-migrate any data first.
    - For any capture document (empty `spec.containers`), the next step is
      human work: turn its `## Migration` section into a `spec:` block, then
      apply.

## Failure handling

Per-item: a non-zero exit on any single remediation step does NOT halt
the run (deliberate divergence from `/bootstrap-node`). The item is
journaled as `apply-failed`, its `hardening.*` field stays `unknown`,
and the run continues to the next item. The whole-run `outcome` is
computed per `references/onboard-recipes.md` § "Failure handling".

Whole-run: Phase 1 or Phase 2 refusals halt before any state change.
Mid-run unrecoverable errors (SSH connection drops, secret resolution
cancelled) halt with `outcome: failed`, the journal entry still gets committed
documenting the partial state, and the admin re-runs `/onboard-node`
(idempotent — already-applied items become aligned on the next probe).

## Recovery checklist (when cut-over verification fails)

The initial SSH session is still alive at this point. Common causes
match `/bootstrap-node`'s recovery checklist — see
`skills/bootstrap-node/SKILL.md` § "Recovery checklist". The
difference for onboarding: the run does not stop. Both sshd fields
revert to `unknown` in the delta, the journal records the failure,
and Phase 7 continues with the next item.

## What this command does NOT do

- Require a VS Code Remote-SSH connection — everything runs from the admin's
  laptop over plain SSH.
- Write any secret value to the box's disk or to the journal. Container env
  values are discarded after split-on-`=`; only names reach the manifest.
- Migrate container data or stop anything that is running. Both are
  `/apply-use-case`'s job, which does them reversibly.
- Reset the firewall rule set. Pre-existing rules survive onboarding; ports
  that would be blocked by enabling default-deny are pre-allowed and tagged.
- Turn a Tier 2 capture document into a rebuildable use-case — that is human
  work, and the document says so.
- Restart or modify any adopted container or systemd user unit. Only the
  remediation anchors the admin approves are executed (sshd, firewall,
  fail2ban, tailscaled, kernel hardening, name responders, file integrity,
  doccap-user setup).
- Retroactively materialize quadlets — scaffolded docs are
  `status: planned`; apply moves to the node.
- Modify existing use-case docs. A slug collision leaves the doc untouched and
  notes the live state under `## Notes / drift detected`.
- Fix non-hardening drift (sysctl keys outside the drydocc hardening drop-in,
  unattended-upgrades, kernel modules,
  …) — findings land in `## Notes / drift detected` and stop there.
- Onboard a box whose `system_facts.service_manager` is not `systemd` —
  refuses on that missing capability (quadlets require systemd). Every
  other dialect is derived from `system_facts` at run time rather than
  refused on.
- Set `last_bootstrapped` unless every hardening field reaches baseline AND
  cut-over verification passes during this run.

Remediation idioms the apply choices invoke live in
`skills/bootstrap-node/references/hardening-recipes.md`.
