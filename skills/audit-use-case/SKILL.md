---
name: audit-use-case
description: Compare declared use-case state against live node state and produce a directional drift report. Detects unmanaged mutation that a rebuild would destroy, and offers to capture undeclared reality back into the doc. Read-only against the node. Use to check a node for drift or before a rebuild.
compatibility: Runs inside a drydocc fork; needs a shell and file edits.
metadata:
  skillset: drydocc
  requires: conventions podman-quadlets node-context node-exec
---

# /audit-use-case

Answers two questions about a use-case: **does reality match the declaration**,
and **could I rebuild this from the declaration alone**. The second is the one
that bites — configuration made through an application's own surfaces (admin UI,
settings menu, service CLI) is invisible to `spec:`.

The probe matrix, comparison table, and classification rules are in
`references/audit-recipes.md`. The manifest schema and this command's
repo-only write authority are in the `conventions` skill's
`references/audit-manifest.md` § "Who may write it".

## Contract

- **Args:**
  - On the node: `[<slug>]` — omitted audits **every** `status: active`
    use-case. Optional `[--report-only]`.
  - On the commissioning client: `<node> [<slug>] [--report-only]` — the target
    node is the **positional first argument** (required in client mode;
    `node-context` refuses without it).
- **Surface:** runs from either surface, resolved by `node-context`:
  - **`on_node`** (preferred) — VS Code Remote-SSH terminal or node-local
    `claude` CLI; probes run locally as written in `references/audit-recipes.md`.
  - **`on_client`** — the admin laptop drives the node over SSH; every probe
    routes through `node-exec` as a `NODE_RUN` one-shot. Nothing here lingers.
- **Reads:** `nodes/<self>/use-cases/<slug>.{md,secrets.yaml,audit.yaml}`; live
  node state (containers, units, quadlets, networks, bind dirs, ports, firewall
  rules, env file **key names only**).
- **Writes (in-repo):**
  - `nodes/<self>/use-cases/<slug>.audit.yaml` — `last_audited`,
    `last_audited_commit`, `fingerprints`; and `config_surfaces`/`ignore` when
    the admin chooses `[d]`/`[i]`.
  - With explicit per-finding confirmation only: `<slug>.md` (`ports:`, `spec:`,
    or a `## Runbook` note) and `<slug>.secrets.yaml` (`keys[]`).
  - The journal entry.
  - Under `--report-only`, nothing at all is written.
- **On-node side effects:** **none.** Every probe is read-only. Live
  reconciliation is delegated to `/modify-use-case`; node hardening to
  `/bootstrap-node`.
- **Journal scope:** `use-case/<slug>` — one entry per audited use-case.

## Procedure

1. **Bind `<self>` + surface** via `node-context`. It returns the surface
   (`on_node` / `on_client`) and, in client mode, the node connection block.
   **Every probe in steps 4–7 runs through `node-exec`:** issued locally when
   `on_node`, SSH-wrapped when `on_client`. The snippets in
   `references/audit-recipes.md` are the `on_node` form.

1b. **Surface nudge (client only).** When `surface == on_client`, read
   `nodes/<self>/node.yaml:remote_ide`. If `vscode_server: compatible`, mention
   that `<self>` can host the VS Code Remote Server — but **proceed without
   prompting** (an audit is read-only; no confirmation gate). If
   `incompatible`, note the reason and proceed. If
   `unknown`, suggest re-running `/bootstrap-node <self>` to probe it.

2. **Resolve the target set.**
   - `<slug>` given → audit that one. Refuse if
     `nodes/<self>/use-cases/<slug>.md` does not exist, naming the slug and
     suggesting `/list-use-cases`.
   - `<slug>` omitted → glob `nodes/<self>/use-cases/*.md` (NOT `*.secrets.yaml`,
     NOT `*.audit.yaml`, NOT `_retired/*`) and select every doc with
     `status: active`. If none, print
     *"No active use-cases on `<self>` to audit."* and stop.
   - Steps 3–10 run **per use-case**. A failure on one never aborts the others.

3. **Read the declaration.** Parse `<slug>.md` frontmatter, plus
   `<slug>.secrets.yaml` and `<slug>.audit.yaml` if present. Validate against
   the `conventions` schemas. A malformed doc is `outcome: failed` for that
   use-case — report it and move on; do not guess at intent.

   Read the **body** too, not only the frontmatter: when the doc carries a
   `## Migration` section, take its `unit:` line — that is the host unit the
   Tier 1 probe tests and the Status coherence row judges. Its labelled-line
   schema is the `conventions` skill's `references/use-case-doc.md` § Migration.
   No section means no migration unit; that is the ordinary case.

   Check the **static invariants** now, since they need no node access:
   `containers`/`networks`/`volumes` must equal the `spec.*` keys, and every
   `spec.containers.*.image` must carry a pinned tag (`:latest` is a validation
   error). These are reported even if every subsequent probe fails.

4. **Run the Tier 1 probe.** Issue the bundled probe script from
   `references/audit-recipes.md` § "The bundled probe" as a single `NODE_RUN`,
   substituting from step 3: the slug, the declared container/network/volume
   names, the migration unit (empty when the doc declares none), and the node's
   `system_facts.firewall.tool`. Each substitution is one of the probe's own
   header assignments — that block is the list, so fill every line of it. Parse
   the `<key>=<value>` lines into an in-memory inventory. Capture the full
   session for the journal.

   If the node is unreachable, mark the use-case `outcome: failed`, journal the
   connection error verbatim, and move to the next.

5. **Re-render and compare quadlets.** Emit the inline `python3 -c '...'`
   renderer implementing `podman-quadlets` `## Render from spec`, apply it to the
   doc's `spec:`, and diff the result against the `quadlet=` blocks from step 4.
   Journal the snippet verbatim — the admin must be able to replay the
   comparison without Claude. This step requires `system_facts.capabilities.python3: present`.
   Classification rules are in
   `references/audit-recipes.md` § "The quadlet comparison".

6. **Classify Tier 1.** Walk the comparison table in
   `references/audit-recipes.md`. Each finding gets a verdict
   (`OK` / `DRIFT` / `UNKNOWN`) **and**, when it is `DRIFT`, a direction
   (`doc-ahead` / `node-ahead`). A failed probe marks its category `UNKNOWN` with
   the reason recorded; it never halts the run.

7. **Run Tier 3 — unmanaged mutation.** Per
   `references/audit-recipes.md` § "Tier 3 — unmanaged mutation":
   - `podman diff` per running container, minus paths under declared mount
     targets → writable-layer mutation, recovery `lost on rebuild`. Universal;
     needs no manifest. A stopped container yields `UNKNOWN`, never "clean".
   - Digest each `config_surfaces[]` entry and compare against `fingerprints`.
     Honour `ignore[]`. A first sighting is recorded, not alarmed. A **stale
     baseline** — `<slug>.md:last_applied_commit` newer than the manifest's
     `last_audited_commit` — is silently re-baselined; the use-case was
     re-applied since, so a digest change proves nothing.
   - Unrendered `<slug>-*` quadlets, firewall rules tagged `<slug>` (per
     `system_facts.firewall.tool`; tagging idiom in the recipes) with no
     matching `ports:` entry, env key names absent from the secrets manifest, and
     `<slug>-*` containers outside the declaration. If
     `system_facts.firewall.tag_mechanism` is `none`, undeclared firewall rules
     cannot be detected and will not appear in the audit.

8. **Render the drift report.** One section per category, one line per finding,
   marked `OK` / `DRIFT` / `UNKNOWN` with its direction and recovery meaning.
   See `## Example output`. Lead with the summary line so a clean audit is one
   glance.

9. **Offer capture-back.** Skip this step entirely under `--report-only`.

   For each `node-ahead` finding, render the menu from
   `references/audit-recipes.md` § "Capture-back menu copy" and apply the
   admin's choice. `[p] promote` is offered only where the doc catching up
   makes doc and node consistent with no further action; after promoting,
   re-run the affected check and revert the edit if the finding does not clear.

   For each `doc-ahead` finding, and for every `lost on rebuild` finding,
   emit the delegated `/modify-use-case` invocation instead — this command
   never touches the node.

   Then write `<slug>.audit.yaml`: `last_audited` (UTC now),
   `last_audited_commit` (short `HEAD`), and the refreshed `fingerprints`.
   Create the manifest if it does not exist. Carry any `telemetry:` block over
   **verbatim** — it is written by `/node-maintenance`, not by this command.

10. **Journal.** One entry per audited use-case at
    `nodes/<self>/journal/YYYY-MM-DD-HHMM-audit-use-case-<slug>.md`, scope
    `use-case/<slug>`:
    - `outcome: success` if every finding is `OK`; `partial` if any `DRIFT` or
      `UNKNOWN`; `failed` only if the audit could not be performed at all (node
      unreachable, doc unparseable).
    - `## What was done` — grouped: "OK: …", "Drift (doc-ahead): …", "Drift
      (node-ahead): …", "Unknown: …", "Promoted: …", "Declared: …", "Ignored: …",
      "Noted: …".
    - `## Commands` — every probe and the render snippet verbatim, as actually
      issued (including the SSH wrapper in client mode, per `node-exec`).
    - `## Notes / drift detected` — narrative for every finding the admin left
      unresolved, every `lost on rebuild` mutation with its recommended
      `/modify-use-case` invocation, and every `UNKNOWN` with why the probe
      failed.
    - **Never** record a secret value, an env-file value, or `expectations[]`
      probe output that could contain one.

11. **Commit + push.** The manifest, any confirmed doc/secrets edits, and the
    journal entry, in one commit. `--report-only` commits nothing — it is a pure
    read. After an `on_client` run, remind the admin to `git pull` on the node's
    working copy.

## Example output

```
Audit: nextcloud on vps-fra-01 — DRIFT (3 findings, 1 unknown)

Containers      OK        nextcloud-app, nextcloud-db, eturnal running
Image pins      OK        all 3 match declared tags
Health          OK        nextcloud-app healthy; 2 define no healthcheck
Units           OK        4 enabled + active, 0 restarts
Quadlets        DRIFT     nextcloud-app.container: PublishPort differs
                node-ahead — on-disk has 8443:443/tcp, spec: has 443:443/tcp
Networks        OK        nextcloud-net present
Volumes         OK        2 bind dirs, owner + mode match
Ports           DRIFT     firewall allows 9980/tcp tagged 'nextcloud', not in ports:
                node-ahead — not rebuildable
Secrets         UNKNOWN   /etc/drydocc/secrets/nextcloud.env unreadable (sudo denied)
Status          OK        active, containers running
Invariants      OK        lists match spec:, all tags pinned

Unmanaged mutation
  nextcloud-app  DRIFT    C /etc/php/conf.d/zz-tuning.ini
                 node-ahead — lost on rebuild
                 recovery: /modify-use-case nextcloud "mount /etc/php/conf.d
                           in nextcloud-app from a new volume so this
                           configuration survives a rebuild"
  config surface OK       nextcloud-data/config/config.php unchanged since a501e0b
```

## What this command does NOT do

- **Change anything on the node.** No restarts, no firewall edits, no quadlet
  writes, no `podman` mutation. Reconciliation is `/modify-use-case`'s job.
- **Audit node hardening.** `sshd -T`, fail2ban, and global firewall state
  belong to `/bootstrap-node`'s audit-&-repair pass. This command is
  use-case-scoped and never writes `node.yaml`.
- **Track resource or log telemetry.** CPU/memory baselines and log error
  counts are `/node-maintenance`'s `resources-within-baseline` and
  `logs-within-baseline` items. This command never writes the `telemetry:`
  block of `<slug>.audit.yaml` and preserves it verbatim when rewriting its
  own half.
- **Bump `last_applied_commit`.** Audit timestamps live in `<slug>.audit.yaml`.
- **Back anything up.** Identifying a rebuild-fatal path is the audit's job;
  making it survive a rebuild's re-materialization is `/modify-use-case`'s;
  making the data itself durable off-node is `/backup-use-case`'s.

## See also

- `onboard-node` `references/onboard-recipes.md` § "Spec reverse-engineering
  rules" — the inspect → `spec` mapping used when promoting an undeclared container.
