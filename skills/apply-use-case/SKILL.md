---
name: apply-use-case
description: Materialize a declared use-case on the node — volumes, secrets, quadlets, services, firewall — and adopt whatever it replaces. Use for the first apply of a use-case whose status is planned, including the containers (another user's rootless quadlets too) and host services picked up by /onboard-node, or to recover an active use-case whose runtime footprint is entirely gone from the box (disaster recovery).
compatibility: Runs inside a drydocc fork; needs a shell and file edits.
metadata:
  skillset: drydocc
  requires: conventions secrets podman-quadlets node-context node-exec
---

# /apply-use-case

## Contract

- **Args:**
  - On the node: `<slug>` (kebab-case, required).
  - On the commissioning client: `<node>` `<slug>` — the target node is the
    **positional first argument** (required in client mode; `node-context`
    refuses without it).
- **Surface:** runs from either surface, resolved by `node-context`:
  - **`on_node`** (preferred) — VS Code Remote-SSH terminal or node-local
    `claude` CLI; commands run locally as written below.
  - **`on_client`** — the admin laptop drives the node over SSH; every
    node-touching command routes through `node-exec` (long steps linger).
- **Reads:** `nodes/<self>/use-cases/<slug>.md` and siblings
  (`<slug>.secrets.yaml`, `<slug>.audit.yaml`); live container and
  systemd-unit state on the node.
- **Writes (in-repo):** `last_applied`, `last_applied_commit`, and
  `status: planned → active` in the doc (on a rebuild run, `active → active`
  — a refresh, not a new transition); one journal entry with
  `mode: greenfield | adoption | rebuild`.
- **On-node side effects:**
  - Host directories under `/var/lib/drydocc/volumes/<slug>/<vol>/` — empty on
    a rebuild run.
  - The secrets env file at the manifest's `target_path`.
  - Quadlets at `~/.config/containers/systemd/<slug>-*.{container,network}`.
  - Firewall rules (node's `system_facts.firewall.tool`) per `ports:` entry.
  - The unprivileged port floor, lowered and persisted under
    `/etc/sysctl.d/` only when a declared host port is below it.
  - In an adoption run only, per legacy object: an unmanaged container
    stopped and renamed `<name>-preadopt`; a quadlet-managed container's unit
    stopped and its quadlet file moved to `<file>.preadopt`; a host unit, or
    another user's unit, disabled. For another owner, these are made in
    that owner's podman and manager.
- **Journal scope:** `use-case/<slug>`.

## Refusals

Check these five in this order, before any node read or write. A sixth check —
`last_applied` not null — is **deferred to step 3**: it can only be resolved
after classification, since a non-null `last_applied` is refused *unless* the
run classifies as rebuild.

1. **Doc missing** — `No use-case <slug> on <self>. Author one with /create-use-case.`
2. **`status: retired`** — `<slug> is retired. Author a fresh use-case instead of reviving it.`
3. **`spec.containers` empty** — `<slug> is a capture-only document — it declares no containers. Work its ## Migration section up into a spec: block first, then re-run.`
4. **Frontmatter invalid** per `conventions` `references/use-case-doc.md` — the
   top-level `containers`/`networks`/`volumes` lists must equal the keys under
   `spec.*`, every image must be pinned, and every container must pass the
   validation rules in `podman-quadlets` § "Render from spec" (e.g.
   `network: host` with published ports, or `network` and `networks` both
   set). Print the offending container and field.
5. **A shared volume's owner is not active** — for every `from:
   <owner-slug>/<vol>` mount: the owner must be listed in this document's
   `depends_on`, its document must exist with `status: active`, and it must
   declare `<vol>` with `shared: true`. Otherwise:
   `<slug> mounts <owner-slug>/<vol>, but <reason>. Apply <owner-slug> first, or fix the mount.`

**Deferred refusal — `last_applied` not null:** `<slug> was applied at <ts>.
Use /modify-use-case to change a live use-case.` Fires in step 3 for every run
that does not classify as rebuild. Never relaxed to a warning — a live but
degraded use-case must go through `/modify-use-case`, not be re-materialized
from scratch.

## Procedure

1. **Bind `<self>` + surface** via `node-context`. **All on-node reads and side
   effects below run through `node-exec`** — locally when `on_node`,
   SSH-wrapped when `on_client`. The snippets are the `on_node` form.

1b. **Surface nudge (client only).** When `surface == on_client`, read
   `nodes/<self>/node.yaml:remote_ide` and, if `vscode_server: compatible`,
   suggest connecting via VS Code Remote-SSH to `doccap@<tailscale_hostname>`
   and running `/apply-use-case` on the node instead; proceed over SSH only on
   confirmation `[decision: apply-use-case.continue-over-ssh]`. If `incompatible`, note SSH is the only surface (`<reason>`)
   and proceed. If `unknown`/absent, proceed and suggest re-running
   `/bootstrap-node <self>` to refresh the probe.

2. **Read and validate** the doc and siblings; apply refusal checks 1–5 above.
   The deferred `last_applied` refusal is checked in step 3, after
   classification.

3. **Classify the run.** Read the doc's `## Migration` section, if any
   (`conventions` `references/use-case-doc.md` § Migration): `owner:` (default
   `doccap`), the `containers:` pairs (a spec key without a pair adopts a
   legacy container of its own name), and `units:` (a single `unit:` is a
   one-item list). Then, for each `spec.containers.<name>` and its legacy
   name, look in **the owner's** podman:

   ```bash
   # owner doccap: run as is. Any other owner: as that user, in its runtime dir
   OUID=$(id -u <owner>)
   sudo -u <owner> XDG_RUNTIME_DIR=/run/user/$OUID \
     podman ps -a --format '{{.Names}}|{{.Labels.PODMAN_SYSTEMD_UNIT}}'
   ```

   Owner `root` is the node's rootful podman: run its commands with plain
   `sudo` and its units in the system manager. Another owner's rootless podman
   answers only while that user's manager runs, so
   `/run/user/<uid>` must exist. If it does not, say so and stop: the owner
   needs lingering (`loginctl enable-linger <owner>`), or a session open,
   before anything can be adopted from it. Never read an empty list from a
   missing runtime directory as "no legacy containers".

   Match on the legacy name **exactly as written** — the spec key, or the
   right-hand side of its `containers:` pair. That is the name the container
   carries on the box today, and the name `/onboard-node` recorded. Do **not**
   normalise it to `<slug>-<name>`: that is the `ContainerName` this
   framework's own quadlets render (`podman-quadlets` § "Render from spec"),
   so on a first apply nothing carries it yet, and normalising would classify
   every adoption as greenfield. A `<slug>-<name>` hit in doccap's podman
   means quadlets from an earlier apply are already on the box, not a legacy
   object. Step 8's `name=<slug>-` filter reads the framework form
   deliberately — by then this command has rendered those quadlets itself.

   A matching row is a **legacy container** of one of two kinds:
   - **unmanaged** — its unit label is empty (started by hand or by a compose
     tool);
   - **quadlet-managed** — its unit label names a unit, and that unit is not
     this use-case's own `<slug>-<name>.service` in doccap's manager. Record
     the unit, and the quadlet file behind it:

     ```bash
     sudo systemctl --user -M <owner>@ show <unit> -p SourcePath --value
     ```

     (For owner `doccap`, plain `systemctl --user`.) An empty answer means the
     unit has no quadlet file to move aside: report it and stop, rather than
     cutting over something that would return at the next boot.

   Then test every `units:` entry: a bare name in the system manager, and
   `<user>/<unit>` in that user's manager:

   ```bash
   systemctl is-enabled <unit>; systemctl is-active <unit>
   sudo systemctl --user -M <user>@ is-enabled <unit>; sudo systemctl --user -M <user>@ is-active <unit>
   ```

   An enabled or active unit is a **legacy host unit**. Any legacy object makes
   this an **adoption** run; otherwise it is **greenfield**.

3b. **Check for rebuild** (only when the doc declares `status: active` or
   carries a non-null `last_applied`, and step 3 found no legacy object — a
   legacy object means adoption, not rebuild). Rebuild requires **all four** of
   the declaration's runtime footprint to be totally absent. Issue
   the four probes as **one bundled `bash -s` heredoc in a single `NODE_RUN`**
   — the same pattern `audit-use-case`'s bundled probe uses, so an `on_client`
   run costs one round trip, not four:

   ```bash
   SLUG=<slug>

   # 1. No quadlets — an unmatched glob yields the literal pattern, which -f rejects
   for f in ~/.config/containers/systemd/"$SLUG"-*.container \
            ~/.config/containers/systemd/"$SLUG"-*.network; do
     [ -f "$f" ] && echo "quadlet=$f"
   done

   # 2. No containers — anchored + --all: a *stopped* <slug>-app is not absence
   podman ps --all --filter "name=^$SLUG-" --format '{{.Names}}' 2>/dev/null \
     | sed 's/^/seen_container=/'

   # 3. No declared volumes — path is derived, never declared on the volume itself.
   # Split existence from readability: unlike audit-use-case's drift-only probe,
   # a false "absent" here gates a destructive-adjacent action, so a permission
   # failure on a volume that still exists must never collapse into "absent".
   for v in <space-separated spec.volumes names>; do
     if [ -e "/var/lib/drydocc/volumes/$SLUG/$v" ]; then
       stat -c "volume=$v|%U:%G|%a" "/var/lib/drydocc/volumes/$SLUG/$v" 2>/dev/null \
         || echo "volume=$v|unknown"
     else
       echo "volume=$v|absent|"
     fi
   done

   # 4. No legacy object — empty systemctl output is unknown, never "not present".
   # One line per units: entry; <user>/<unit> is asked of that user's manager.
   for u in <space-separated units: entries, empty if the doc has no ## Migration section>; do
     case "$u" in */*) sc="sudo systemctl --user -M ${u%%/*}@"; unit="${u#*/}" ;; *) sc="systemctl"; unit="$u" ;; esac
     enabled=$($sc is-enabled "$unit" 2>/dev/null || true); [ -z "$enabled" ] && enabled="unknown"
     active=$($sc is-active "$unit" 2>/dev/null || true); [ -z "$active" ] && active="unknown"
     echo "migration_unit=$u|$enabled|$active"
   done
   ```

   **Absence must be total. Partial absence is drift, not recovery** — one
   surviving container or quadlet sends this to the deferred `last_applied`
   refusal, and drift belongs to `/audit-use-case` then `/modify-use-case`. A
   rebuild that fired on partial absence would re-materialize over a
   half-broken live service, which is exactly the destructive outcome the
   refusal exists to prevent.

   **`unknown` is not absence, for every condition.** Condition 3's `unknown`
   line (path exists but `stat` couldn't read it — an ownership or permission
   drift) is exactly this case: it must never read as "volume absent", or a
   permission failure on a volume that still holds data would misclassify as
   rebuild and re-materialize an empty container on top of it. Condition 4
   gets the same treatment via the three-class rule
   `audit-use-case/references/audit-recipes.md` § "Status coherence" uses:
   **live** (either field reports its positive state) beats **indeterminate**
   (neither is live but either field is `unknown`) beats **not live** (both
   fields are known and neither is live), applied to every
   `migration_unit=` line. Only all lines **not live** — or no line at all —
   counts as absent. If any of the four
   probes reports `unknown` or is indeterminate, the run does **not** classify
   as rebuild; it falls through to the deferred `last_applied` refusal rather
   than guessing.

   All four absent (conditions 1–2 emit no lines; condition 3 says `absent`
   for every declared volume; every condition-4 line is not live) → the run is
   **rebuild**. Otherwise, apply the deferred `last_applied` refusal from the
   Refusals section, unchanged.

3c. **Corroborate with machine identity, then confirm** (rebuild runs only)
   `[decision: apply-use-case.confirm-rebuild]`.
   Read `nodes/<self>/node.yaml:identity` and compare `machine_id_hash` /
   `host_key_fingerprint` against a fresh probe of the node (the same probe
   `bootstrap-node/references/hardening-recipes.md` § "Machine identity probe"
   defines). A missing block reads as `identity not recorded` — this **never**
   blocks the run, it only weakens the evidence shown next. Identity
   **corroborates, it does not trigger**: a machine id can legitimately change
   across a cloned VM template or a re-flashed SBC, and can stay equal across a
   clone that is nonetheless a different box, so absence remains the only test.

   Rebuild mode never proceeds silently. Print the evidence and require an
   explicit yes:

   > `<slug>` declares `active` since `<last_applied>` (commit
   > `<last_applied_commit>`), but this node has none of its containers,
   > quadlets, or volumes. Machine identity `<changed since that apply |
   > unchanged | not recorded>`; node last bootstrapped `<ts>`. Treat as
   > disaster recovery and re-apply from the declaration?

   On decline, stop here with no node write and no journal entry (nothing has
   changed yet).

   **Carried confirmation.** When `/rebuild-node` invokes this command for a
   use-case the admin accepted in its recovery plan, that acceptance is the
   explicit yes — **provided** this run's own step 3b probe classifies rebuild
   and its identity comparison reads the same as the evidence the plan showed.
   Any difference (a container that has since appeared, an identity verdict
   that changed) voids it: print the prompt above and ask.

4. **Resolve every secret before anything destructive.** If `has_secrets`,
   resolve each `keys[].name` via `secrets` (env-var-first) and assemble the
   env file body in memory. If any key fails to resolve on an **adoption**
   run, **halt before the cut-over** — the values are still readable from the
   legacy object while it runs. Print the recovery command and stop:

   ```bash
   podman inspect <legacy-container> --format '{{range .Config.Env}}{{println .}}{{end}}'
   ```

   For another owner's container, run it as that owner, exactly as in step 3
   (`sudo -u <owner> XDG_RUNTIME_DIR=/run/user/<uid> podman inspect …`). A
   quadlet-managed container usually reads its values from an
   `EnvironmentFile=` named in its quadlet file, which is the same place to
   look.

   On a **rebuild** run there is no legacy object to fall back on — nothing on
   the box has been touched yet, so halting costs nothing. Tell the admin to
   store the missing values per `secrets` and re-run. Never echo a resolved
   value, and never write one to the journal.

5. **Bind every copy source, then confirm the adoption plan** (adoption runs
   only) `[decision: apply-use-case.confirm-adoption]`. A source is never guessed and never defaulted. Each one is read off
   the legacy object and joined to a declared volume **on the mount target**:

   - **Legacy container.** For each `spec.containers.<name>.mounts[]` entry, the
     source is the `.Source` of the inspect entry whose `.Destination` equals
     that entry's `target`; the copy destination is that entry's `volume`.

     ```bash
     podman inspect <legacy-container> --format '{{json .Mounts}}'   # as the owner, as in step 3
     ```

     **Read it now, before anything stops.** A quadlet-managed container is
     deleted the moment its unit stops (quadlets run it with `--rm`), so step 6
     has nothing left to inspect. Its named volumes survive, at the `.Source`
     paths recorded here. Record with each source whether it lies inside the
     owner's rootless storage (`podman info --format
     '{{.Store.GraphRoot}}'`, run as the owner) or not; step 6 copies the two
     kinds differently.

     That iteration walks the *declared* mounts, so walk the inspect output the
     other way too: every bind entry whose `.Destination` matches no declared
     `target` is reported in the plan as `<source> → undeclared (<destination>)`
     and **not** copied — nothing the legacy container mounts is dropped in
     silence.

   - **Legacy host unit.** The sources are the paths on the doc's `## Migration`
     `config:` and `data:` lines (schema per the `conventions` skill's
     `references/use-case-doc.md` § Migration). Join each to the declared volume
     whose mount `target` equals that path.

     When the path is instead a strict **parent** of a mount `target`, never
     bind the parent — the copy lands the whole tree one level too deep, so the
     container finds its payload nested one directory below the mount root
     instead of at it. Narrow it:
     append the target's trailing components to the path (path `/srv/foo` with
     target `/srv/foo/data` → candidate `/srv/foo/data`) and bind that candidate
     only if it exists on the host (`test -e`). If it does not, the volume is
     **unbound** and the plan must say why — `source: none — depth mismatch:
     <path> is a parent of target <target>, and <candidate> does not exist` —
     so the admin sees the mismatch before approving rather than after the
     container fails to start.

   **A files volume is never a copy destination**: its content is the git tree
   `<slug>.files/<vol>/` (`podman-quadlets` § "Files volumes"). When a legacy
   source joins to one, do not bind it. Compare the two trees by sha256
   instead, and list the volume in the plan as `<source> → replaced by
   <slug>.files/<vol>/ from git` with the count of files that differ, are
   missing from git, or exist only in git. Differences mean the declaration
   is not yet faithful to the live configuration. The admin should decline,
   commit the difference into the files tree, and re-run.

   A `from:` mount is not a volume of this use-case and never binds a source.
   Its legacy counterpart is reported as `<source> → read from
   <owner-slug>/<vol>`.

   A declared volume that no source resolves to is not an error — it is created
   empty in step 7 — but it must appear in the plan as `source: none — volume
   created empty`, so an adoption can never silently start an empty container
   over an empty volume. A resolved source that joins to no declared volume is
   reported and **not** copied (see `## What this command does NOT do`).

   Then print the plan and require an explicit yes:
   - every unmanaged legacy container to be stopped and renamed
     `<name>-preadopt`, as its owner;
   - every quadlet-managed legacy container's unit to be stopped in its
     owner's manager, and its quadlet file moved aside to `<file>.preadopt`;
   - every legacy host unit to be disabled and stopped, in its own manager;
   - every bound copy as `<source> → /var/lib/drydocc/volumes/<slug>/<vol>/`,
     with the source's size from `du -sh <source>`;
   - every declared volume with no bound source, each with its reason —
     `volume created empty`, `populated from git`, or the depth mismatch
     above;
   - every resolved source that joined to no declared volume, on either path;
   - the reminder that sources are copied, never moved, and nothing is deleted.

5b. **Pre-pull every pinned image** (all three modes). This runs **before**
   the cut-over on purpose: pulling is read-only, a bad pin or an unreachable
   registry is the likeliest first-apply failure, and step 4's principle —
   nothing destructive until everything readable is resolved — covers images the
   same way it covers secrets. On a **rebuild** run every image is guaranteed
   absent on the fresh box, making this the slowest step and the one that most
   needs lingering.

   ```bash
   podman pull <pinned-image>   # once per spec.containers.*.image
   ```

   When `on_client`, run each as `NODE_RUN_LINGER` (`dd-<slug>-pull`) so a
   dropped SSH connection cannot abort a large download. If any pull fails, halt
   here and report which image: on an adoption or rebuild run nothing has been
   stopped yet, so the box is still exactly as it was.

6. **Cut-over** (adoption runs only — **skipped entirely** on a rebuild run;
   there is nothing legacy to stop, rename, or disable), in this order:

   ```bash
   # per unmanaged legacy container — as its owner (prefix for another owner:
   # sudo -u <owner> XDG_RUNTIME_DIR=/run/user/<ouid>)
   podman stop --time 30 <name>
   podman rename <name> <name>-preadopt

   # per quadlet-managed legacy container — in its owner's manager; the unit's
   # --rm deletes the container, its named volumes and quadlet file remain
   sudo systemctl --user -M <owner>@ stop <unit>
   sudo mv <quadlet-file> <quadlet-file>.preadopt
   sudo systemctl --user -M <owner>@ daemon-reload

   # per legacy host unit — a bare name in the system manager,
   # <user>/<unit> in that user's
   sudo systemctl disable --now <unit>
   sudo systemctl --user -M <user>@ disable --now <unit>

   # per declared volume, before its copy (container-form owner: podman-quadlets § "Volumes")
   sudo install -d -o <owner> -g <group> -m <mode> /var/lib/drydocc/volumes/<slug>/<vol>
   ```

   Then copy each (source → volume) pair bound in step 5. Sources are copied,
   never moved, and never modified. Which copy depends on who owns the source,
   because ids only keep their meaning inside the namespace that wrote them:

   ```bash
   # a. a legacy container of doccap's own: same id mapping on both sides
   sudo rsync -aHAX --numeric-ids <source>/ /var/lib/drydocc/volumes/<slug>/<vol>/

   # b. a legacy container of another rootless owner: through both user namespaces, so
   #    every file keeps its id *as the containers see it*
   sudo -u <owner> XDG_RUNTIME_DIR=/run/user/<ouid> podman unshare \
       tar --numeric-owner --xattrs --acls -C <source> -cpf - . \
     | podman unshare tar --numeric-owner --xattrs --acls \
         -C /var/lib/drydocc/volumes/<slug>/<vol> -xpf -

   # c. a legacy host unit's config: or data: path, or a rootful (owner root)
   #    legacy container's source: host ids (root, a system
   #    daemon's user) mean nothing in a rootless container, so the copy takes
   #    the volume's declared owner throughout
   sudo rsync -aHAX <source>/ /var/lib/drydocc/volumes/<slug>/<vol>/
   sudo chown -R <owner>:<group> /var/lib/drydocc/volumes/<slug>/<vol>   # host form
   podman unshare chown -R <uid>:<gid> /var/lib/drydocc/volumes/<slug>/<vol>   # container form
   ```

   Trailing slashes matter, and there is never a `--delete`. After a copy of
   kind a or b, set the volume root itself back to the declared owner and
   mode: the copy carries the source root's. The adoption plan (step 5)
   already showed both, so a mismatch is a declaration to fix, not a
   surprise. A source root owned by container uid 33, under a volume declared
   `doccap:doccap`, wants `owner: container:33:33`.

   This step requires `system_facts.capabilities.rsync: present` (verify before
   the cut-over; rsync is not present on every minimal image). Long copies run
   as `NODE_RUN_LINGER` (`dd-<slug>-rsync`) when `on_client`. Verify each copy
   with `du -sb` on both sides (kind b: inside each namespace, `podman unshare
   du -sb`) and record both numbers.

7. **Bring-up** (all three modes):
   - Create any declared volume directory not already created in step 6, with
     declared mode/owner (defaults `0750`, `doccap:doccap`; `0755` for a
     files volume; a `container:<uid>:<gid>` owner per `podman-quadlets`
     § "Volumes"). On a **rebuild** run step 6 created none, so **every**
     declared volume is created here. Every one is empty except the files
     volumes. Record the list of empty ones; step 13 names it in the closing
     warning.
   - Sync every files volume from git, in all three modes, per
     `podman-quadlets` § "Files volumes", and record its file count and
     digest. A files volume is never empty after this step, rebuild
     included.
   - If `has_secrets`, write the env file assembled in step 4:
     `sudo install -m 600 -o doccap -g doccap /dev/stdin <target_path>` (piped
     stdin; `node-exec` transports the body unchanged and never echoes it).
   - Render quadlets in-memory with an inline `python3 -c '...'` snippet
     implementing `podman-quadlets` § "Render from spec". Journal the snippet
     verbatim. Show the admin the rendered files and get confirmation
     `[decision: apply-use-case.confirm-render]`. If
     any declared host port — published, or a host-network container's
     `listens:` — is below the node's unprivileged port floor, list the floor
     change in the same confirmation.
   - Lower the unprivileged port floor if a declared host port (published or
     `listens:`) needs it, per
     `podman-quadlets` § "Privileged ports" — otherwise rootless podman
     cannot publish a port below 1024 and the container fails to start.
   - Write each file atomically:
     `printf '%s\n' "<ini-text>" | install -m 644 /dev/stdin ~/.config/containers/systemd/<slug>-<name>.<kind>`
   - `systemctl --user daemon-reload`
   - Start network units first, then containers in topological order of
     `depends_on`: `systemctl --user start <unit>`. Quadlet units are
     generated and cannot be `enable`d; start-at-boot comes from the
     quadlet's `[Install]` section (`podman-quadlets` § "Reload procedure").
     A `oneshot` container is not started here: only its job starts it.
   - If `spec.jobs` is non-empty, render each job's script and units, create
     `/var/lib/drydocc/jobs/` if absent, and enable each timer, per
     `podman-quadlets` § "Jobs". Show the rendered script in the same
     `confirm-render` confirmation as the quadlets.

8. **Verify.** `podman ps --filter name=<slug>- --format json`; assert every
   declared container except the `oneshot` ones is `Up`. Then start each job
   once by hand (`systemctl --user start drydocc-job-<slug>-<name>.service`)
   and assert its run log ends in `rc=0`. A job that fails now would fail
   unattended later. On failure, capture
   `journalctl --user -u <unit> -n 50`, mark `outcome: partial`, and go to
   step 11 — the failure is journaled like any other outcome — without
   touching the firewall or the doc's status.

9. **Firewall.** For each top-level `ports:` entry — a single port or a range
   — add a rule tagged `<slug>` via the node's `system_facts.firewall.tool`,
   using
   `bootstrap-node/references/hardening-recipes.md` § "Use-case port rules" →
   **Add**. On an adopted node the rule may already exist from onboarding's
   adoption variant — the template is identical, so the add is a no-op. If
   `node.yaml:hardening.firewall` is `unknown`, update it to `enabled`. If
   `system_facts.firewall.tag_mechanism` is `none`, rules cannot be attributed
   to this use-case and later `/remove-use-case` runs will not be able to
   identify and clean them up.

10. **Update the doc**: `status: active` (on a rebuild run this is a
    self-transition — the doc already said `active`), `last_applied: <UTC
    now>`, `last_applied_commit: <commit being created>`. Two-step commit: the
    first commit stages doc + journal with a placeholder SHA, the second
    rewrites the placeholder to the first commit's SHA. Both are journaled.

11. **Journal** per the `conventions` skill's `references/journal.md`, with
    `mode: greenfield | adoption | rebuild` from step 3/3b's classification.
    `outcome: success` if step 8 verified, `partial` otherwise. For an adoption
    run, `## What was done` names every legacy object, its owner and its
    disposition, and every copy with its kind (step 6) and both byte counts. For a **rebuild** run, `## What was
    done` names the absence evidence from step 3b, the identity comparison
    from step 3c, and every volume created empty (files volumes, populated
    from git, are listed apart). **Never journal a secret
    value, and never the raw machine id — the hash only.**

12. **Commit + push** the doc, any sibling that changed, `node.yaml` (if the
    firewall state flipped), and the journal entry. Quadlets are not committed
    — they are rendered on the node from `spec:`. After an `on_client` apply, remind the admin to `git pull` on
    the node's working copy.

13. **Print the summary.** Verified containers, ports opened, and — for an
    adoption run — the **prune list**, with the commands to remove each entry:
    - every `<name>-preadopt` container;
    - every `<file>.preadopt` quadlet file, with the owner's named volumes it
      used (`podman volume rm`, as the owner) and the env files it named;
    - every network quadlet of the owner that only those files referenced —
      left in place so § Rollback can start them again;
    - every source path bound in step 5.

    Removing them is the admin's decision, never this command's. Until they
    are removed, § Rollback works.

    For a **rebuild** run, state outright — never bury it — that **volumes
    were created empty and no application data was restored**, naming every
    empty volume from step 7 (files volumes were repopulated from git and are
    not data loss). This is why rebuild is a distinct mode rather than a
    relaxed refusal: services come up healthy and the data is gone, and
    without this line a successful rebuild reads identically to a successful
    first apply until someone opens the application. If
    `<slug>.backup.yaml` exists, name `/restore-use-case <slug>` as the very
    next command to run.

## Rollback

Until the admin prunes, every adoption is reversible. Print this recipe
whenever step 8 fails on an adoption run:

```bash
# stop what was just started, and keep it from returning at the next boot
systemctl --user stop <slug>-<container>.service
rm -f ~/.config/containers/systemd/<slug>-*.container ~/.config/containers/systemd/<slug>-*.network
systemctl --user daemon-reload
# restore an unmanaged legacy container (as its owner)
podman rename <name>-preadopt <name>
podman start <name>
# restore a quadlet-managed legacy container (in its owner's manager)
sudo mv <quadlet-file>.preadopt <quadlet-file>
sudo systemctl --user -M <owner>@ daemon-reload
sudo systemctl --user -M <owner>@ start <unit>
# restore a legacy host unit (a user's unit: sudo systemctl --user -M <user>@ …)
sudo systemctl enable --now <unit>
```

Stop the new containers **before** restarting the legacy ones: they hold the
same ports, and a host-network service holds them on the node itself.

The copied data under `/var/lib/drydocc/volumes/<slug>/` is inert once the
quadlet units are stopped; the original sources were never modified.

A **rebuild** run has nothing to roll back *to* — there was no prior live
state on this box, only empty containers on top of empty volumes. A failed
verify (step 8) leaves those stopped; there is no legacy object to restore.

## What this command does NOT do

- Author a use-case document — that is `/create-use-case`.
- Change a use-case that has already been applied — that is
  `/modify-use-case`, **except** when the runtime footprint is entirely
  absent, which is rebuild mode.
- Apply a capture-only document (empty `spec.containers`); it refuses and says
  so.
- Delete a legacy container, a legacy quadlet file, another user's named
  volumes, a legacy unit's packages, or any original data path. It stops,
  renames, moves quadlet files aside, disables, and copies — pruning is the
  admin's.
- Adopt from another user's podman while that user's manager is not running
  (no `/run/user/<uid>`): it stops and says how to enable lingering. Nor does
  it move a service to run as anyone but `doccap`.
- Migrate data for a bind source the document does not declare as a volume.
- Restore volume data on a rebuild run — it re-creates empty volumes and says
  so outright. Backup/restore is `/backup-use-case` / `/restore-use-case`.
- Journal any secret value, or the raw machine id.
