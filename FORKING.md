# Forking `drydocc`

You are about to set up an infrastructure. Each step below is something
**you (the admin) do** in VS Code, with an AI coding agent extension active
— for example **Claude Code** or **Mistral Vibe**.

## 0. Prerequisites

Everything `drydocc` needs is listed in the README's
[What you need](README.md#what-you-need). For this walkthrough, also note:

- **Windows needs one extra step before cloning** — see §1. On Linux and macOS
  nothing further is needed.
- The VS Code **Docker** extension (configured to use `podman` if you also run
  containers locally) is handy for looking at a node's containers.
- Install the recommended `keepassxc-secrets` skill before you start; the
  commands below only suggest it when it is missing. Without it, secrets come
  from a `.env` file you paste them into (§4).

## 1. Create your private fork

Your fork will hold your fleet: node profiles, hostnames, use-case
declarations, and the journal of every change. Keep it **private**. A fork
made with GitHub's *Fork* button is always public, so don't use it — make a
private copy that still tracks the framework instead:

1. Create an **empty, private** repository on GitHub (no README, license, or
   `.gitignore`), e.g. `<you>/<infra-name>`.
2. Clone the framework into it:

   ```bash
   git clone https://github.com/hiemalis-at/drydocc.git <infra-name>
   cd <infra-name>
   git remote rename origin upstream
   git remote add origin git@github.com:<you>/<infra-name>.git
   git push -u origin main
   code .
   ```

`origin` is now your fork; `upstream` is the framework, which you pull
updates from (see [Staying current](#staying-current)).

Your coding agent extension picks up the framework's instructions
automatically — no setup needed.

### Windows: one extra step before cloning

The repo contains symlinks. Linux and macOS check them out correctly with
no configuration; Windows `git` writes them as ordinary text files unless
you enable symlink support first — and a fork checked out that way is
subtly broken, because your coding agent will not find the framework's
instructions or commands.

Do both of these **before** the `git clone` above:

1. **Let Windows create symlinks without elevation** — turn on
   *Settings → System → For developers → Developer Mode*. (Alternative:
   run the clone from an Administrator terminal.)
2. **Tell git to use them** —
   ```bash
   git config --global core.symlinks true
   ```
   Git for Windows also offers this as an *"Enable symbolic links"*
   checkbox during installation.

**Already cloned without it?** The simplest fix is to enable both, delete
the clone, and clone again. To repair in place instead:

```bash
git config core.symlinks true
git rm --cached -r .
git reset --hard
```

**Or sidestep it entirely:** clone into **WSL** and open the folder with
VS Code's *WSL* extension. WSL is a Linux filesystem, so the Windows
symlink restrictions do not apply.

## 2. Decide on a node name

Pick a kebab-case slug (e.g., `vps-fra-01`, `pi-home-01`). There is no
separate registration step — `/bootstrap-node <slug>` scaffolds
`nodes/<slug>/` in its first phase. Distro / version / arch are
**discovered over SSH**, not asked up front.

## 3. Have a box (or let bootstrap make one)

`/bootstrap-node` supports two sources, which it asks you to choose:

- **`running`** — an already-reachable box. Flash the SBC or rent the
  vServer, and have its **temporary root password** ready (plus IP/port).
  The password is used once, to install your key and lock SSH down, then
  becomes irrelevant.
- **`vm`** — let bootstrap provision a fresh **qemu/kvm** VM first via
  `/provision-vm`. You'll be asked for RAM / vCPUs / disk and a base image —
  a curated option or a URL/path to your own ISO. Requires a qemu/kvm host on
  your laptop.

### 3a. If your box already exists

If you are adopting a Linux box that has been running for a while
(custom sshd config, existing firewall rules, an admin user, possibly
containers already running), skip `/bootstrap-node` and
run `/onboard-node` instead:

> /onboard-node vps-fra-01

The command runs from your laptop, SSHes to the box over its existing
credentials, inventories live state, and walks you through reconciling
each hardening drift item with per-item **apply / skip / defer**.
Running containers are reverse-engineered into `status: planned`
use-case docs.

After onboarding finishes, continue from §6 (Reconnect via Remote-SSH).
For each scaffolded `status: planned` use-case, run
`/apply-use-case <slug>` on the node. That is the step that cuts the live
container (or host service) over to a `drydocc`-controlled quadlet, and it
keeps the thing it replaces recoverable until you confirm the cutover.

## 4. Prepare bootstrap secrets

`/bootstrap-node` needs one session secret, resolved **just-in-time**
(not upfront) via the env-var-first protocol defined in the `secrets`
skill:

1. **Check the environment** — if `$DOCCAP_TS_KEY` is already set, it's
   used directly.
2. **`@max-win-at/keepassxc-secrets` agentskill** — if installed, fetches
   the value from KeePassXC automatically. Create a KeePass entry
   (any title or group — only its URL field is matched) with URL
   `drydocc://tailscale` and
   store the auth key in the Password field.
3. **`.env` fallback** — if neither of the above works, the agent names
   the missing variable and prints a command that lets you paste it into
   `./.env` without it showing on screen. It then offers to pause so you can
   load that file and resume with `/continue-last-command`.

The secret:

- **`DOCCAP_TS_KEY`** (required) — a Tailscale auth key. Generate at
  https://login.tailscale.com/admin/settings/keys. Use a **reusable**
  key (you'll register multiple nodes over time).

Access to this fork is **not** a secret you prepare. Each node commits and
pushes its own changes, so during bootstrap it generates its own SSH key and
shows you the public half. Add it to the fork as a **deploy key with write
access** (on GitHub: *Settings → Deploy keys → Add deploy key → Allow write
access*), titled after the node. The private key never leaves the node; if
the node is ever lost or compromised, deleting that one deploy key cuts it
off.

See [`skills/secrets/SKILL.md`](skills/secrets/SKILL.md)
and [`skills/conventions/references/secrets.md`](skills/conventions/references/secrets.md)
for the full secrets contract.

## 5. Bootstrap (from your laptop, NOT remote-ssh)

In your coding agent, **on your local VS Code session**:

> /bootstrap-node vps-fra-01 "edge web node for the blog"

The command will:
- scaffold `nodes/vps-fra-01/` and ask the provisioning **source**
  (`running` or `vm`);
- for `vm`, ask RAM / vCPUs / disk + base image and provision the VM
  (via `/provision-vm`);
- connect as root over the temporary password, discover the OS + SSH
  server, create the `doccap` user, then **generate** a dedicated key
  `doccap-at-vps-fra-01` (or **import** one from a KeePassXC ssh-agent);
- verify key + sudo, lock SSH down (root off, password off, port 42022),
  then install the stack, join the tailnet, and — once you have added the
  node's deploy key — clone this fork with push access.

It is resumable: if anything fails, fix the cause and re-run
`/bootstrap-node vps-fra-01` — completed steps are skipped. Re-running a
fully-bootstrapped node prompts for an audit-&-repair pass. Watch the
output; the agent halts on failure with a recovery message.

When it finishes, you'll have:
- a hardened SSH endpoint at `doccap@<ip>:42022` (key-only, no root,
  no password)
- the node joined to your tailnet as `<tailscale_hostname>`
- this repo cloned at `/home/doccap/<repo>` on the node
- `/etc/drydocc/node-name` written so `node-context` binds
  `<self>` correctly on future Remote-SSH sessions
- a journal entry under `nodes/<name>/journal/`
- a commit + push describing the run

## 6. Reconnect via Remote-SSH

Close (or just leave) your laptop's VS Code session. Open a new VS Code
window and use **Remote-SSH → Connect to Host** with
`doccap@<tailscale_hostname>` (or `doccap@<ip>` on port 42022 if
you're not using Tailscale's SSH).

Open the folder `/home/doccap/<repo>` and your coding agent takes over
node-side. `/list-use-cases` should print an empty table — confirming
identity binding, node scoping, and repo presence all work end-to-end.

The provider's bare ssh stays as a fallback on
`ssh_fallback_port` (default `42022`), keyed to `doccap` only.

## 7. Add use-cases

Declaring and running a service is two commands:

> /create-use-case nextcloud "Nextcloud + Postgres + eturnal TURN"

Walk through the prompts. This writes `nextcloud.md` with a `spec:` block,
commits it as `status: planned`, and then hands straight over to
`/apply-use-case nextcloud` — what puts it on the node: volumes, secrets,
quadlets, services, firewall ports. The apply shows you the rendered
quadlets and asks before it starts any service; decline there if you want to
read and correct the document first. The committed `planned` document is
the resume point either way — after editing it, or after a failed apply,
run:

> /apply-use-case nextcloud

Repeat for each service the node runs.

Later changes go through `/modify-use-case nextcloud "<what to change>"`,
which re-renders and restarts only what actually changed.

If the use-case has any volumes, run `/backup-use-case nextcloud` next — it
proposes a backup strategy for you to tune, then proves it restorable before
scheduling it (§9 covers what to do with it if the node ever dies).

## 8. Ongoing

`/list-use-cases`, `/audit-use-case`, `/modify-use-case`,
`/remove-use-case`, `/backup-use-case`, `/node-maintenance` as needed.

Run `/node-maintenance <node> --report-only` first on any node you have not
swept before — it is read-only, enables nothing, and tells you what the box
owes. It derives its findings from the use-cases actually installed, the
backup strategies actually declared, and the security stance actually
recorded, so its report is about this node rather than a generic checklist.
Once a node has a few days of history, the same sweep also judges every
container against its own learned baseline — memory growth, sustained CPU, a
rise in logged errors, restarts — without running any monitoring daemon.

Optionally, get pushed rather than having to look: install the ntfy app on
your phone and run `/node-notify <node> --setup`. It generates two random
topic names for you to store in a KeePassXC entry with URL
`drydocc://ntfy`, subscribes you to one, and sends a test
message. From then on failed timer runs, telemetry anomalies, and missed runs —
including a node that has gone silent — reach your phone. Treat the topic
names like passwords: anyone who knows one can read it.

Run `/audit-use-case [<slug>]` periodically — it is read-only against the
node, so there is no risk in it. Each finding comes with a direction and a
remedy:

| Finding | Means | Do |
|---|---|---|
| `doc-ahead` | the node drifted from the declaration | `/modify-use-case <slug>` to reconcile |
| `node-ahead` | someone changed the node directly | accept the audit's offer to capture it back into the doc |
| `lost on rebuild` / `not rebuildable` | config exists that no declaration covers | act on it — see below |

That last row is the one worth caring about. Configuration you made through a
service's *own* admin UI or CLI is invisible to the declaration, and if it
sits in the container's writable layer a rebuild wipes it. Declare such paths
in the optional `<slug>.audit.yaml` manifest and every audit fingerprints and
diffs them.

## 9. If a node dies

Recovery is a rebuild plus a restore, not a rollback — there is no image to
revert to; the fork is the recovery artifact.

**Run `/rebuild-node <slug>` from your laptop.** It checks what survived, then
walks the whole node back:

- It brings the base back, bootstrapping a replacement box when needed.
- It rebuilds every active use-case in dependency order.
- It restores each use-case's data wherever the disaster or the rebuild
  lost it, before anything that depends on it starts.
- It re-arms backups, maintenance and alarms.
- It ends with a report that names every volume that did **not** get its
  data back.

You confirm the plan once. Use `/rebuild-node <slug> --report-only` first to
see the plan without changing anything. A run that stops partway, on a
missing secret or a failed restore, is resumed by running it again.

Under the hood it runs the same three commands you can also run by hand,
against a fresh box:

1. **`/bootstrap-node <slug>`** with the same slug. The existing
   `nodes/<slug>/` is reused, not re-scaffolded; because the node was already
   bootstrapped once, it asks *"Re-bootstrap now (audit & repair)?"* — say
   yes.
2. **`/apply-use-case <uc>`** for each one. It detects that the declaration
   says `active` with no runtime footprint left on the box at all, confirms
   with you (showing what it found, or didn't), and classifies the run as a
   **rebuild** — no manual editing of `status`/`last_applied`
   needed. It states outright that volumes came up empty.
3. **`/restore-use-case <uc>`** for each one that has a `<uc>.backup.yaml` —
   this is the step `/apply-use-case`'s rebuild summary names next. It
   restores from the use-case's declared backup target, verifies the result
   against the snapshot's own seal, and brings the use-case back up.

Two things the fork cannot hand you, so keep them safe yourself:

- **Your KeePass database.** Secret *values* are not in git. Without KeePass
  the node is not rebuildable.
- **An S3 backup target's encryption passphrase**, if you use one — losing
  it makes every snapshot on that target unrecoverable, exactly like losing
  KeePass.

A clean audit is what tells you the rebuild half of this path will actually
work. Treat `not rebuildable` findings as a broken recovery plan, because
that is what they are — and treat a stale or missing
`/restore-use-case --test` result the same way for the data half: a backup
nobody has restored is a hypothesis, not a backup.

## Staying current

Framework updates are published as releases of `hiemalis-at/drydocc`, each one
a single commit on its `main` branch, with the changes listed in
`CHANGELOG.md`. To take one, on your laptop:

```bash
git pull upstream main
git push
```

A release touches only framework files — `skills/`, `scripts/lab/`, `lab/`, `docs/`, the
top-level guides — and nothing under `nodes/` but its empty placeholder, so it
merges cleanly into a fork. If you changed a framework file yourself, git asks
you to resolve that file, as with any merge. Then `git pull` on each node's
working copy as well.

## Things to know

- **Secrets** are never in git. Only manifests (key names + purposes) are
  committed as `<slug>.secrets.yaml`. Values live in KeePassXC and on the
  node at `/etc/drydocc/secrets/<slug>.env`.
- **The journal** under each `nodes/<node>/journal/` is the audit trail. It
  is append-only — don't edit it manually.
- **Every command journals and commits**, including when it fails. If a run
  breaks, the fork already records how far it got; fix the cause and run the
  same command again.
- **If the agent itself drops out mid-run** — its server is unreachable, the
  credits run out, it crashes, or you close the window — there was no chance
  to journal. Start the agent again and run `/continue-last-command`. It finds
  the unfinished run, shows what it left behind on the node and in the fork,
  and resumes it or records it as abandoned.
- **Each command's full contract** is its `skills/<name>/SKILL.md`. How the
  framework is built, and the rules for changing it, is
  [`docs/architecture.md`](docs/architecture.md).
