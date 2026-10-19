# Lab commands

Every invocation the `lab` skill uses. All run from the root of the admin's
checkout; `scripts/lab/lab.py --help` and `<verb> --help` are the full flag
reference. Exit status is 0 on success; `run` and `verify-runbook` exit 2 on a
`blocked` verdict and 1 on any other non-pass.

## Base layers

```sh
# a provisioned, shut-down VM becomes the root of a clone tree
virsh -c qemu:///session dumpxml <vm> > /tmp/<vm>.xml
scripts/lab/lab.py layer-import --kind base --name <distro><version>-<arch> \
  --disk <vm disk> --domain-xml /tmp/<vm>.xml [--nvram <vm NVRAM file>] \
  --distro <os-release ID> --version <VERSION_ID> --arch <uname -m> --pin
scripts/lab/lab.py tree
```

`--nvram` is required for a UEFI guest (every emulated aarch64 guest): the boot
entry lives there.

## Agents

```sh
scripts/lab/lab.py agents        # every adapter: ready or not, paid or free, model, qualification
```

Every agent-driving verb (`run`, `qualify`, `up`, `verify-runbook`, `agent`)
takes `--adapter <name>`; the default is `$DRYDOCC_LAB_ADAPTER`, else `claude`.
The model is `--model` on `run`, else `$DRYDOCC_LAB_MODEL`, else the
adapter's own. A `pi` model may be a comma-separated fallback chain. Setting
up an adapter — the free `pi` one included — is in `scripts/lab/agents/README.md`.

## Qualification

```sh
scripts/lab/lab.py qualify lab/scenarios/whoami.yaml lab/scenarios/web-db.yaml \
  --adapter pi --models <model>,<model> [--repeat 2]
scripts/lab/lab.py qualify … --resume qualify-<YYYYmmdd-HHMMSS>   # after a stop
```

One pinned model per run. Writes `results/qualify-*/matrix.md` in the lab store.
A model that errs (overloaded, withdrawn) gets one more try, then the matrix
moves on; errors on two models in a row — usually a daily cap — stop it. Either
way it exits 1, and `--resume` runs only the cells still missing judged runs.
It starts only when no other run's fork exists, since an agent could read one
and copy its work (`--allow-visible-runs` overrides), and it destroys each of
its own runs once recorded; reports and transcripts stay under `results/`.

## Test runs

```sh
scripts/lab/lab.py run lab/scenarios/<name>.yaml [--repeat N] [--keep] [--ref <ref>] [--adapter <name>] [--model <id>]
scripts/lab/lab.py runs
scripts/lab/lab.py shell <run>                      # look around a kept run
scripts/lab/lab.py gc [--failed [--older-than 7d]] [--budget 200G] [--dry-run]
```

## Ephemeral tailnet

A scenario with `tailnet: ephemeral` needs, once, from the admin:

1. **A tag** for lab nodes, owned by the admin, and **access rules** that let
   lab nodes reach only each other, and let the admin's devices reach them —
   including Tailscale SSH as `doccap`, which the bootstrap's tailnet check
   uses. In the tailnet policy file:

   ```jsonc
   "tagOwners": { "tag:drydocc-lab": ["autogroup:admin"] },
   "grants": [
     { "src": ["tag:drydocc-lab"], "dst": ["tag:drydocc-lab"], "ip": ["*"] },
     { "src": ["autogroup:member"], "dst": ["tag:drydocc-lab"], "ip": ["*"] }
   ],
   "ssh": [
     { "action": "accept", "src": ["autogroup:member"], "dst": ["tag:drydocc-lab"],
       "users": ["doccap", "root"] }
   ]
   ```

   Merge these into the existing policy; a policy that still allows
   everything (the default `*` → `*` rule) also lets lab nodes reach every
   other device.
2. **An auth key**: reusable, ephemeral, pre-approved, tagged
   `tag:drydocc-lab`, with a short expiry.
3. **The key on the laptop**, readable only by the admin — never in a fork,
   a scenario or a chat:

   ```sh
   install -d -m 700 ~/.config/drydocc-lab
   (umask 077; cat > ~/.config/drydocc-lab/tailnet-authkey)   # paste, then Ctrl-D
   ```

   or `export DRYDOCC_LAB_TS_AUTHKEY=…` for one shell.

Where the laptop's own uplink is narrower than a tailnet packet in its
WireGuard wrapping (WSL2's often is: 1280), the lab gives each node a smaller
tailnet MTU so full-size packets are not silently dropped.

A key made **without** the tag joins nodes as the admin's own devices, beside
every real one. The lab checks after every step and, on finding an untagged
node on the tailnet, logs it out at once and ends the run with an error.

## Prototypes

```sh
scripts/lab/lab.py up <proto> --node web [--node db=base] [--node web=<restore point>] \
  --distro <os-release ID> --version <VERSION_ID> [--arch aarch64] [--ttl 7d]
scripts/lab/lab.py shell <proto> --agent <adapter>  # the chosen agent, inside the prototype
scripts/lab/lab.py shell <proto> [--node <n>] [-- <command>]
scripts/lab/lab.py answer <proto> <decision id>=<answer> …     # before a headless step
scripts/lab/lab.py agent <proto> "<prompt>" --node <n> [--timeout <s>]
scripts/lab/lab.py checkpoint <proto> <node> --name <name>
scripts/lab/lab.py reset <proto> <node>
scripts/lab/lab.py ttl <proto> <age>                # e.g. 14d, from now
scripts/lab/lab.py destroy <proto>
```

Inside a prototype's session the run's `PATH` carries `lab`, bound to that
run: `lab checkpoint <node> --name <name>`, `lab reset <node>`, `lab ls`,
`lab tree`. Nothing else is reachable from inside.

## Runbook bundles

```sh
scripts/lab/lab.py extract <proto> <node> --as <target> [--arch <uname -m>]
scripts/lab/lab.py verify-runbook <bundle> [--ref <framework ref>] [--keep]
scripts/lab/lab.py promote <bundle> [--ref <branch point>] [--unverified]
```

`<bundle>` is a bundle id from `ls` (`<target>-<YYYYmmdd-HHMMSS>`), a unique
prefix of one, or its path.

## What the lab keeps

```sh
scripts/lab/lab.py ls                 # prototypes, runs, restore points, bundles
scripts/lab/lab.py tree               # the layer lineage, with age and size
scripts/lab/lab.py layer-rm <layer>   # an unwanted restore point (nothing may depend on it)
scripts/lab/lab.py pin <layer> [--off]
```
