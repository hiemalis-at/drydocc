#!/usr/bin/env python3
"""drydocc lab driver — the deterministic substrate under the lab.

The lab keeps a store of sealed VM layers (immutable qcow2 disks plus their
domain definition and, for UEFI guests, their NVRAM), clones them into runs as
linked overlays, reseals each clone's identity offline before its first boot,
and garbage-collects runs and layers. Everything here is a plain script on
purpose: when an agent under test crashes, the substrate must still be able to
judge and clean up.

Store root: $DRYDOCC_LAB_HOME, else ${XDG_DATA_HOME:-~/.local/share}/drydocc-lab

  layers/<key>/   disk.qcow2  domain.xml  [nvram.qcow2]  meta.json
  runs/<run>/     run.json  ssh_config  known_hosts  keys/  secrets/  bin/
                  disks/  reseal/
  bundles/<id>/   nodes/<target>/ — a runbook bundle extracted from a prototype
  results/<run>/  report.json  report.md  transcripts/

Host tools: qemu-img, virsh (libvirt session connection), virt-customize.
"""

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path

LIBVIRT_URI = "qemu:///session"
SSH_FALLBACK_PORT = 42022
INITIAL_HOST_PORT = 2222  # host side of guest port 22; unprivileged users can't bind 22
MAX_RUNS = 150            # loopback block 127.0.(100+n).0/24 per run
MAX_NODES = 200
LABNET_PREFIX = "192.0.2."  # RFC 5737: never routed, so a lab segment can't collide with a real one
DOMAIN_PREFIX = "lab-"
PROTOTYPE_TTL = "7d"        # a prototype is collected this long after it was last (re)armed
SLUG_RE = r"[a-z][a-z0-9-]{0,30}[a-z0-9]"   # a drydocc node slug

LABNET_SCRIPT = """#!/bin/sh
# drydocc lab: address the labnet NIC, selected by MAC so NIC naming never matters.
. /etc/drydocc/labnet.env
for d in /sys/class/net/*; do
  if [ "$(cat "$d/address")" = "$LABNET_MAC" ]; then
    ip link set "${d##*/}" up && ip addr replace "$LABNET_ADDR" dev "${d##*/}" && exit 0
  fi
done
exit 1
"""

# Logic lives in the script above, not inline: systemd expands ${...} in ExecStart itself.
LABNET_UNIT = """[Unit]
Description=drydocc lab: address the labnet NIC (matched by MAC)
After=network-pre.target
Before=network.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/etc/drydocc/labnet.sh

[Install]
WantedBy=multi-user.target
"""


LABGIT_SCRIPT = """#!/bin/sh
# drydocc lab: the laptop serves the lab remote on its own loopback, which this
# VM reaches through its default gateway. Map the laptop-side URL onto it so a
# fork URL handed over from the laptop works here unchanged.
gw=$(ip route show default | awk '{print $3; exit}')
[ -n "$gw" ] || exit 1
printf '[url "git://%s/"]\\n\\tinsteadOf = git://127.0.0.1/\\n[url "git://%s:"]\\n\\tinsteadOf = git://127.0.0.1:\\n' "$gw" "$gw" > /etc/gitconfig
"""

LABGIT_UNIT = """[Unit]
Description=drydocc lab: reach the laptop's lab git remote through the gateway
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/etc/drydocc/labgit.sh

[Install]
WantedBy=multi-user.target
"""

GIT_DAEMON_PORT = int(os.environ.get("DRYDOCC_LAB_GIT_PORT", "9418"))
LAB_GIT_IDENTITY = ["-c", "user.name=drydocc lab", "-c", "user.email=lab@drydocc.invalid"]
FORK_GUIDE_HEADING = "# `drydocc` fork agent guide"


class LabError(Exception):
    pass


# --------------------------------------------------------------------- store

def store_root() -> Path:
    env = os.environ.get("DRYDOCC_LAB_HOME")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(base) / "drydocc-lab"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def expiry(ttl: str) -> str:
    return (dt.datetime.now(dt.timezone.utc).replace(microsecond=0) + parse_age(ttl)).isoformat()


def human_age(seconds: float) -> str:
    s = int(abs(seconds))
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    return f"{d}d{h}h" if d else f"{h}h{m}m" if h else f"{m}m"


def age_of(iso: str) -> str:
    return human_age((dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(iso)).total_seconds())


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


@contextmanager
def store_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with open(root / "lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def allocated_bytes(path: Path) -> int:
    """Bytes actually allocated on disk (sparse qcow2 files count as their blocks)."""
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_blocks * 512
    total = 0
    for p in path.rglob("*"):
        if p.is_file() and not p.is_symlink():
            total += p.stat().st_blocks * 512
    return total


def human(n: int) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n}"


def parse_size(text: str) -> int:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?)i?B?\s*", text, re.I)
    if not m:
        raise LabError(f"cannot parse size: {text!r}")
    mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[m.group(2).upper()]
    return int(float(m.group(1)) * mult)


def parse_age(text: str) -> dt.timedelta:
    m = re.fullmatch(r"(\d+)([smhd])", text)
    if not m:
        raise LabError(f"cannot parse age (use e.g. 30m, 12h, 7d): {text!r}")
    unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[m.group(2)]
    return dt.timedelta(**{unit: int(m.group(1))})


TAILNET_TUN_MTU = 1280     # tailscale's own default for its interface
WIREGUARD_OVERHEAD = 80    # outer IPv6 + UDP + WireGuard headers


def host_uplink_mtu() -> int | None:
    """MTU of the interface the host's default route leaves through."""
    try:
        for line in Path("/proc/net/route").read_text().splitlines()[1:]:
            f = line.split()
            if f[1] == "00000000":
                return int(Path(f"/sys/class/net/{f[0]}/mtu").read_text())
    except (OSError, ValueError, IndexError):
        pass
    return None


def guest_tailnet_mtu(uplink: int | None) -> int | None:
    """The tailnet MTU a lab guest needs, or None when tailscale's default fits.

    A guest's tailnet packets leave through the host's uplink, wrapped in
    WireGuard. Where that uplink is narrower than tailscale's default plus
    the wrapping — a WSL2 host's is often 1280 — full-size packets are
    dropped and only small ones arrive: SSH stalls in its key exchange and
    bulk data never comes.
    """
    if uplink is None or uplink - WIREGUARD_OVERHEAD >= TAILNET_TUN_MTU:
        return None
    return uplink - WIREGUARD_OVERHEAD


def tailnet_key_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "drydocc-lab" / "tailnet-authkey"


def tailnet_authkey() -> str:
    """The admin's auth key for an ephemeral lab tailnet (lab-overlay.md § "Ephemeral tailnet").

    From $DRYDOCC_LAB_TS_AUTHKEY, else a file only its owner can read. The lab
    never stores it: it reaches agents through the environment only, and a
    run's leak check covers it.
    """
    key = os.environ.get("DRYDOCC_LAB_TS_AUTHKEY", "").strip()
    f = tailnet_key_file()
    if not key and f.is_file():
        if f.stat().st_mode & 0o077:
            raise LabError(f"{f} is readable by others — chmod 600 it")
        key = f.read_text().strip()
    if not key:
        raise LabError(f"tailnet: ephemeral needs an auth key — set DRYDOCC_LAB_TS_AUTHKEY or write it to {f} "
                       "(mode 600); see the conventions skill's references/lab-overlay.md § \"Ephemeral tailnet\"")
    if not key.startswith("tskey-"):
        raise LabError("the tailnet auth key does not look like one (expected tskey-…)")
    return key


# -------------------------------------------------------------------- layers

def layer_key(identity: dict) -> str:
    """Content key of a layer: what it was built from, never when or where."""
    canon = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def load_layers(root: Path) -> dict:
    layers = {}
    for meta in sorted((root / "layers").glob("*/meta.json")):
        data = read_json(meta)
        layers[data["key"]] = data
    return layers


def resolve_layer(root: Path, ref: str) -> dict:
    """A layer by key, key prefix, or name."""
    layers = load_layers(root)
    hits = [m for k, m in layers.items() if k.startswith(ref) or m.get("name") == ref]
    if len(hits) != 1:
        raise LabError(f"layer {ref!r}: {'no match' if not hits else 'ambiguous'}")
    return hits[0]


def layer_dir(root: Path, key: str) -> Path:
    return root / "layers" / key


def import_layer(root: Path, args) -> dict:
    """Seal an existing, shut-off VM disk as a layer (the way a base layer enters the store)."""
    disk = Path(args.disk).resolve()
    xml = Path(args.domain_xml).resolve()
    for p in (disk, xml):
        if not p.is_file():
            raise LabError(f"not a file: {p}")
    identity = {
        "kind": args.kind, "parent": args.parent, "distro": args.distro,
        "version": args.version, "arch": args.arch, "iso_sha256": args.iso_sha256,
        "skills_digest": args.skills_digest,
    }
    key = layer_key(identity)
    dest = layer_dir(root, key)
    if dest.exists():
        raise LabError(f"layer {key} already exists ({read_json(dest / 'meta.json').get('name')})")
    dest.mkdir(parents=True)
    try:
        # convert, not copy: drops the source's backing chain and compacts free space
        run_cmd(["qemu-img", "convert", "-O", "qcow2", str(disk), str(dest / "disk.qcow2")])
        shutil.copyfile(xml, dest / "domain.xml")
        if args.nvram:
            shutil.copyfile(Path(args.nvram).resolve(), dest / "nvram.qcow2")
        for f in dest.iterdir():
            f.chmod(0o444)
        meta = dict(identity, key=key, name=args.name or key,
                    created=now_iso(), last_used=now_iso(), pinned=bool(args.pin),
                    built_by_commit=args.commit, root_password_file=None)
        if args.root_password_file:
            pw = dest / "root-password"
            shutil.copyfile(args.root_password_file, pw)
            pw.chmod(0o400)
            meta["root_password_file"] = "root-password"
        write_json(dest / "meta.json", meta)
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return meta


def layer_children(layers: dict, key: str) -> list:
    return [k for k, m in layers.items() if m.get("parent") == key]


def layer_in_use(root: Path, key: str) -> list:
    users = []
    for rj in (root / "runs").glob("*/run.json"):
        run = read_json(rj)
        if any(n["layer"] == key for n in run["nodes"].values()):
            users.append(run["run"])
    return users


# ---------------------------------------------------------------------- runs

def runs_dir(root: Path) -> Path:
    return root / "runs"


def load_run(root: Path, run_id: str) -> dict:
    path = runs_dir(root) / run_id / "run.json"
    if not path.is_file():
        raise LabError(f"no such run: {run_id}")
    return read_json(path)


def save_run(root: Path, run: dict) -> None:
    write_json(runs_dir(root) / run["run"] / "run.json", run)


def next_free(used: set, start: int, stop: int) -> int:
    for n in range(start, stop):
        if n not in used:
            return n
    raise LabError("allocation space exhausted")


def loopback_addr(run_index: int, node_index: int) -> str:
    return f"127.0.{100 + run_index}.{node_index}"


def labnet_addr(node_index: int) -> str:
    return f"{LABNET_PREFIX}{10 + node_index}"


def labnet_mac(run_index: int, node_index: int) -> str:
    return f"52:54:00:dd:{run_index:02x}:{node_index:02x}"


def create_run(root: Path, run_id: str, mode: str, *, repo: Path | None = None,
               ref: str = "HEAD", tailnet: str = "off", ttl: str | None = None) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}", run_id):
        raise LabError("run id: lowercase letters, digits, hyphens; at most 31 chars")
    if tailnet not in ("off", "ephemeral"):
        raise LabError(f"tailnet: off | ephemeral, not {tailnet!r}")
    if tailnet == "ephemeral":
        tailnet_authkey()  # fail before anything is built, not mid-bootstrap
    rdir = runs_dir(root) / run_id
    if rdir.exists():
        raise LabError(f"run {run_id} already exists")
    used = {read_json(p)["index"] for p in runs_dir(root).glob("*/run.json")}
    index = next_free(used, 1, MAX_RUNS + 1)
    for sub in ("keys", "secrets", "bin", "disks", "reseal", "home", "transcripts"):
        (rdir / sub).mkdir(parents=True)
    for sub in ("keys", "secrets", "home"):
        (rdir / sub).chmod(0o700)
    key = rdir / "keys" / "lab_ed25519"
    run_cmd(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"drydocc-lab-{run_id}", "-f", str(key)])
    (rdir / "known_hosts").touch()
    run = {"run": run_id, "index": index, "mode": mode, "status": "active",
           "created": now_iso(), "mcast_port": 42000 + index,
           "mcast_group": f"239.255.42.{index}", "nodes": {},
           "tailnet": tailnet, "answers": {}, "fork": None, "steps": []}
    if mode == "prototype":  # kept on purpose — but never forever
        run["expires"] = expiry(ttl or PROTOTYPE_TTL)
    save_run(root, run)
    write_shims(root, run)
    write_ssh_config(root, run)
    if repo is not None:
        try:
            run["fork"] = create_fork(root, run, repo, ref)
        except Exception:
            shutil.rmtree(rdir, ignore_errors=True)
            raise
        save_run(root, run)
        write_overlay(root, run)
    return run


# ------------------------------------------------------------ fork + remote

def git(repo: Path, *args, capture=True, check=True):
    return run_cmd(["git", "-C", str(repo)] + list(args), capture=capture, check=check)


def remote_url(run_id: str) -> str:
    port = "" if GIT_DAEMON_PORT == 9418 else f":{GIT_DAEMON_PORT}"
    return f"git://127.0.0.1{port}/{run_id}.git"


def fork_agents_md(text: str) -> str:
    """AGENTS.md as a fork receives it: from the fork agent guide heading on.

    Anything above that heading is the development checkout's own preamble. The
    release build and every lab fork apply this one cut, so an agent in a lab
    run reads exactly the instructions an agent in a real fork does.
    """
    lines = text.split("\n")
    if FORK_GUIDE_HEADING not in lines:
        raise LabError(f"AGENTS.md has no line '{FORK_GUIDE_HEADING}' to start a fork's copy at")
    start = lines.index(FORK_GUIDE_HEADING)
    return text if start == 0 else "\n".join(lines[start:]).rstrip("\n") + "\n"


def stage_fresh_fork(wt: Path) -> None:
    """Stage what makes a checkout of the ref a fresh fork: an empty fleet, and
    AGENTS.md as a fork receives it."""
    tracked = [f for f in git(wt, "ls-files", "nodes").split("\n") if f and f != "nodes/.gitkeep"]
    if tracked:
        git(wt, "rm", "-q", "-r", "--", *tracked)
    agents = wt / "AGENTS.md"
    if agents.is_file() and not agents.is_symlink():
        text = agents.read_text(encoding="utf-8")
        cut = fork_agents_md(text)
        if cut != text:
            agents.write_text(cut, encoding="utf-8")
            git(wt, "add", "AGENTS.md")


def create_fork(root: Path, run: dict, repo: Path, ref: str) -> dict:
    """A standalone clone of `ref` in the lab store, set up as a complete,
    independent fork.

    Standalone, not a worktree: a worktree shares the repository's refs, so an
    agent in one run could read every other run's branch — and copied another
    model's work from one. It also sits outside the admin's checkout, so nothing
    an agent reads from a parent directory is the checkout's. The clone's only
    remote is origin, a per-run bare repository served by the lab git daemon;
    upstream is the ref under test (recorded, not configured); the fleet starts
    empty, and AGENTS.md is cut to what a fork receives. Lab commits never flow
    back.
    """
    repo = Path(git(repo, "rev-parse", "--show-toplevel").strip())
    ref_sha = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    run_id = run["run"]
    branch = f"lab/{run_id}"
    wt = root / "forks" / run_id
    bare = root / "remotes" / f"{run_id}.git"
    if wt.exists() or bare.exists():
        raise LabError(f"fork leftovers exist for {run_id}: {wt if wt.exists() else bare}")
    # the ref under test may be any commit: a transient ref makes it fetchable
    pin = f"refs/drydocc-lab/{run_id}"
    git(repo, "update-ref", pin, ref_sha)
    try:
        wt.parent.mkdir(parents=True, exist_ok=True)
        run_cmd(["git", "init", "-q", "-b", branch, str(wt)])
        git(wt, "fetch", "-q", "--no-tags", str(repo), pin)
        git(wt, "reset", "-q", "--hard", "FETCH_HEAD")
        # commits an agent makes carry the identity they would in the checkout
        for key in ("user.name", "user.email"):
            value = git(repo, "config", "--get", key, check=False).strip()
            if value:
                git(wt, "config", key, value)
        # fresh fork: an empty fleet, whatever the ref's nodes/ held, and no
        # development preamble in AGENTS.md
        stage_fresh_fork(wt)
        git(wt, *LAB_GIT_IDENTITY, "commit", "-q", "--allow-empty", "-m",
            f"lab: fresh fork {run_id} of {ref} ({ref_sha[:12]})")
        bare.parent.mkdir(parents=True, exist_ok=True)
        run_cmd(["git", "init", "-q", "--bare", str(bare)])
        git(wt, "push", "-q", str(bare), f"{branch}:refs/heads/{branch}")
        run_cmd(["git", "-C", str(bare), "symbolic-ref", "HEAD", f"refs/heads/{branch}"])
        url = remote_url(run_id)
        git(wt, "remote", "add", "origin", url)
        ensure_git_daemon(root)
        git(wt, "fetch", "-q", "origin")
        git(wt, "branch", "-q", f"--set-upstream-to=origin/{branch}")
        resolved = git(wt, "remote", "get-url", "origin").strip()
        if resolved != url:
            raise LabError(f"fork origin resolves to {resolved}, expected the lab remote {url}")
    except Exception:
        remove_fork(repo, wt, branch, bare)
        raise
    finally:
        git(repo, "update-ref", "-d", pin, check=False)
    return {"repo": str(repo), "worktree": str(wt), "branch": branch, "remote": str(bare),
            "remote_url": url, "ref": ref, "ref_sha": ref_sha}


def remove_fork(repo: Path, wt: Path, branch: str, bare: Path) -> None:
    if (wt / ".git").is_dir():  # a standalone clone: nothing of it is in the admin's repository
        shutil.rmtree(wt, ignore_errors=True)
    else:  # a worktree fork, from before forks were standalone clones
        git(repo, "worktree", "remove", "--force", str(wt), check=False)
        git(repo, "worktree", "prune", check=False)
        git(repo, "branch", "-q", "-D", branch, check=False)
        # the tracking ref the fork's fetches left in the shared namespace
        git(repo, "update-ref", "-d", f"refs/remotes/origin/{branch}", check=False)
    shutil.rmtree(bare, ignore_errors=True)


def daemon_pid(root: Path) -> int | None:
    pidfile = root / "gitd.pid"
    try:
        pid = int(pidfile.read_text().strip())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
    except (OSError, ValueError):
        return None
    return pid if b"git-daemon" in cmdline or b"daemon" in cmdline else None


def ensure_git_daemon(root: Path) -> None:
    """One shared daemon for every run's lab remote, bound to loopback only.

    Tracked by PID file — never found by pattern: a pattern match can hit the
    very shell that is looking for it.
    """
    if daemon_pid(root):
        return
    base = root / "remotes"
    base.mkdir(parents=True, exist_ok=True)
    log = open(root / "gitd.log", "a")
    proc = subprocess.Popen(
        ["git", "daemon", "--listen=127.0.0.1", f"--port={GIT_DAEMON_PORT}", f"--base-path={base}",
         "--export-all", "--enable=receive-pack", "--reuseaddr", "--informative-errors", str(base)],
        stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    (root / "gitd.pid").write_text(f"{proc.pid}\n")
    for _ in range(50):  # listening is asynchronous: wait for the port, up to 5 s
        try:
            socket.create_connection(("127.0.0.1", GIT_DAEMON_PORT), timeout=0.2).close()
            return
        except OSError:
            if proc.poll() is not None:
                raise LabError(f"git daemon exited ({proc.returncode}); see {root / 'gitd.log'}")
            time.sleep(0.1)
    raise LabError(f"git daemon not listening on 127.0.0.1:{GIT_DAEMON_PORT} after 5 s")


def stop_git_daemon(root: Path) -> None:
    pid = daemon_pid(root)
    if pid:
        os.kill(pid, 15)
    (root / "gitd.pid").unlink(missing_ok=True)


def yaml_scalar(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(str(v))  # a JSON string is a valid YAML scalar


def to_yaml(data, indent=0) -> str:
    pad = "  " * indent
    out = []
    for k, v in data.items():
        if isinstance(v, dict):
            if not v:
                out.append(f"{pad}{k}: {{}}")
            else:
                out.append(f"{pad}{k}:")
                out.append(to_yaml(v, indent + 1))
        else:
            out.append(f"{pad}{k}: {yaml_scalar(v)}")
    return "\n".join(out)


def overlay_data(run: dict) -> dict:
    fork = run.get("fork") or {}
    scale = max([15 if n.get("emulated") else 1 for n in run["nodes"].values()] or [1])
    return {
        # attended: an admin is at the keyboard (a prototype session) — false
        # whenever a headless agent is driving, whatever the mode
        "lab": {"run": run["run"], "mode": run["mode"],
                "attended": run["mode"] == "prototype" and not run.get("headless"),
                "upstream_ref": f"{fork.get('ref')}@{(fork.get('ref_sha') or '')[:12]}",
                "remote_url": fork.get("remote_url"), "tailnet": run.get("tailnet", "off"),
                "time_scale": scale},
        "nodes": {name: {"initial": {"ip": n["reach"]["host"], "port": n["reach"]["ports"]["22"]},
                         "arch": n.get("arch")}
                  for name, n in sorted(run["nodes"].items())},
        "answers": dict(sorted(run.get("answers", {}).items())),
    }


def write_overlay(root: Path, run: dict) -> None:
    fork = run.get("fork")
    if not fork:
        return
    d = Path(fork["worktree"]) / ".drydocc"
    d.mkdir(exist_ok=True)
    (d / "lab.yaml").write_text("# drydocc lab overlay — generated by scripts/lab/lab.py; never committed\n"
                                + to_yaml(overlay_data(run)) + "\n")


def skills_digest(repo: Path, rev: str, skill: str) -> str:
    """Digest of a skill and every skill its `metadata.requires` names, at `rev`.

    Uses git tree ids, so it changes exactly when a file under one of those
    skill directories changes.
    """
    seen, todo, parts = set(), [skill], []
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        tree = git(repo, "rev-parse", f"{rev}:skills/{name}", check=False)
        if not tree:
            continue
        parts.append(f"{name}:{tree.strip()}")
        body = git(repo, "show", f"{rev}:skills/{name}/SKILL.md", check=False) or ""
        m = re.search(r"^metadata:[ \t]*\n(?:[ \t]+.*\n)*?[ \t]+requires:[ \t]*(.+)$", body, re.M)
        if m:
            todo += m.group(1).strip().strip("'\"").split()
    return hashlib.sha256("\n".join(sorted(parts)).encode()).hexdigest()[:16]


# ----------------------------------------------------------- domain cloning

def clone_domain_xml(src_xml: str, *, name: str, disk: str, nvram: str | None,
                     reach_host: str, labnet_mac_addr: str, mcast_group: str,
                     mcast_port: int, serial_log: str) -> str:
    """A layer's domain definition, rewritten for one clone.

    Keeps the machine definition (so PCI slots, and with them NIC names, stay
    stable); replaces identity (name, uuid, MACs), storage, forwards and the
    labnet NIC; drops install-time leftovers (kernel/initrd/cmdline, cdroms).
    """
    dom = ET.fromstring(src_xml)
    dom.find("name").text = name
    uuid_el = dom.find("uuid")
    if uuid_el is not None:
        uuid_el.text = str(uuid.uuid4())
    os_el = dom.find("os")
    for tag in ("kernel", "initrd", "cmdline"):
        for el in os_el.findall(tag):
            os_el.remove(el)
    nv = os_el.find("nvram")
    if nv is not None:
        if not nvram:
            raise LabError("layer defines NVRAM but no NVRAM file was provided")
        nv.text = nvram
    devices = dom.find("devices")
    disks = [d for d in devices.findall("disk") if d.get("device") != "cdrom"]
    for d in devices.findall("disk"):
        if d.get("device") == "cdrom":
            devices.remove(d)
    if len(disks) != 1:
        raise LabError(f"expected exactly one disk in the layer definition, found {len(disks)}")
    src = disks[0].find("source")
    src.attrib.clear()
    src.set("file", disk)
    for bs in disks[0].findall("backingStore"):
        disks[0].remove(bs)
    for tag in ("serial", "console"):
        for el in devices.findall(tag):
            for lg in el.findall("log"):
                el.remove(lg)
            src_el = el.find("source")
            if src_el is not None and src_el.get("path", "").startswith("/dev/pts"):
                el.remove(src_el)
            if "tty" in el.attrib:
                del el.attrib["tty"]
    serial = devices.find("serial")
    if serial is not None:
        ET.SubElement(serial, "log", file=serial_log, append="on")
    # the user-mode NIC: fresh MAC, forwards rebuilt for this clone's loopback address
    user_nics = [i for i in devices.findall("interface") if i.get("type") == "user"]
    for i in devices.findall("interface"):
        if i.get("type") != "user":
            devices.remove(i)  # any previous labnet NIC
    if len(user_nics) != 1:
        raise LabError(f"expected one user-mode NIC in the layer definition, found {len(user_nics)}")
    nic = user_nics[0]
    for tag in ("mac", "alias", "portForward", "address"):
        for el in nic.findall(tag):
            nic.remove(el)
    pf = ET.SubElement(nic, "portForward", proto="tcp", address=reach_host)
    ET.SubElement(pf, "range", start=str(INITIAL_HOST_PORT), to="22")
    ET.SubElement(pf, "range", start=str(SSH_FALLBACK_PORT))
    lab = ET.SubElement(devices, "interface", type="mcast")
    ET.SubElement(lab, "mac", address=labnet_mac_addr)
    ET.SubElement(lab, "source", address=mcast_group, port=str(mcast_port))
    ET.SubElement(lab, "model", type="virtio")
    # live-state leftovers libvirt adds to a running domain's XML: device
    # aliases (rejected on define unless user-prefixed), source indexes, the id
    for parent in list(dom.iter()):
        for al in parent.findall("alias"):
            parent.remove(al)
        if parent.tag == "source" and "index" in parent.attrib:
            del parent.attrib["index"]
    if "id" in dom.attrib:
        del dom.attrib["id"]
    return ET.tostring(dom, encoding="unicode")


def reseal_args(rdir: Path, node: str, lab_pub: Path, root_pw_file: Path | None,
                labnet_env: str, *, identity: bool = True, doccap_pub: Path | None = None,
                tailnet_mtu: int | None = None) -> list:
    """virt-customize operations that give a clone its own identity.

    File operations only — nothing executes guest binaries, so the same
    reseal works on a guest of another CPU architecture. A first-boot relabel
    is scheduled for boxes whose MAC framework labels files.

    identity=False keeps the box's identity (a restore point cloned under its
    own name); only the run-specific wiring (labnet address) changes.
    """
    work = rdir / "reseal" / node
    work.mkdir(parents=True, exist_ok=True)
    (work / "node-name").write_text(node + "\n")
    (work / "labnet.env").write_text(labnet_env)
    (work / "labnet.sh").write_text(LABNET_SCRIPT)
    (work / "drydocc-labnet.service").write_text(LABNET_UNIT)
    (work / "labgit.sh").write_text(LABGIT_SCRIPT)
    (work / "drydocc-labgit.service").write_text(LABGIT_UNIT)
    unit = "/etc/systemd/system/drydocc-labnet.service"
    gunit = "/etc/systemd/system/drydocc-labgit.service"
    ops = []
    if identity:
        ops += ["--hostname", node,
                "--truncate", "/etc/machine-id",
                "--delete", "/etc/ssh/ssh_host_*",
                "--firstboot-command", "ssh-keygen -A && systemctl restart sshd"]
    if root_pw_file is not None:
        ops += ["--root-password", f"file:{root_pw_file}"]
    if doccap_pub is None:
        # a base clone: the lab reaches it as root until /bootstrap-node runs
        ops += ["--ssh-inject", f"root:file:{lab_pub}"]
    else:
        # a commissioned clone: reached as doccap; root keeps no key, as
        # bootstrap left it (hardening-recipes.md, lockdown Step 7)
        ops += ["--ssh-inject", f"doccap:file:{doccap_pub}"]
    if tailnet_mtu is not None:
        # applies once the tailnet daemon is installed; harmless before
        (work / "lab-mtu.conf").write_text(f"[Service]\nEnvironment=TS_DEBUG_MTU={tailnet_mtu}\n")
        ops += ["--mkdir", "/etc/systemd/system/tailscaled.service.d",
                "--upload", f"{work / 'lab-mtu.conf'}:/etc/systemd/system/tailscaled.service.d/lab-mtu.conf"]
    return ops + [
        "--mkdir", "/etc/drydocc",
        "--upload", f"{work / 'node-name'}:/etc/drydocc/node-name",
        "--upload", f"{work / 'labnet.env'}:/etc/drydocc/labnet.env",
        "--upload", f"{work / 'labnet.sh'}:/etc/drydocc/labnet.sh",
        "--chmod", "0755:/etc/drydocc/labnet.sh",
        "--upload", f"{work / 'drydocc-labnet.service'}:{unit}",
        "--link", f"{unit}:/etc/systemd/system/multi-user.target.wants/drydocc-labnet.service",
        "--upload", f"{work / 'labgit.sh'}:/etc/drydocc/labgit.sh",
        "--chmod", "0755:/etc/drydocc/labgit.sh",
        "--upload", f"{work / 'drydocc-labgit.service'}:{gunit}",
        "--link", f"{gunit}:/etc/systemd/system/multi-user.target.wants/drydocc-labgit.service",
        "--touch", "/.autorelabel",
    ]


def clone_node(root: Path, run_id: str, node: str, layer_ref: str, start: bool,
               index: int | None = None) -> dict:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}[a-z0-9]", node):
        raise LabError(f"node name {node!r} is not a valid drydocc slug")
    run = load_run(root, run_id)
    if node in run["nodes"]:
        raise LabError(f"node {node} already exists in run {run_id}")
    layer = resolve_layer(root, layer_ref)
    ldir = layer_dir(root, layer["key"])
    rdir = runs_dir(root) / run_id
    kind = layer.get("kind", "base")
    origin_node = layer.get("node")
    if kind != "base":
        if not run.get("fork"):
            raise LabError(f"a {kind} layer carries fork state — clone it into a run created with --fork")
        if kind == "checkpoint" and node != origin_node:
            raise LabError(f"checkpoint {layer['name']} is a restore point of {origin_node!r}: clone it "
                           f"under that name; moving it to another name is extraction, not cloning")
    used = {n["index"] for n in run["nodes"].values()}
    if index is not None and index in used:
        raise LabError(f"node index {index} is taken in run {run_id}")
    idx = index if index is not None else next_free(used, 2, MAX_NODES)
    domain = f"{DOMAIN_PREFIX}{run_id}-{node}"
    disk = rdir / "disks" / f"{node}.qcow2"
    nvram = rdir / "disks" / f"{node}_VARS.qcow2" if (ldir / "nvram.qcow2").exists() else None
    reach_host = loopback_addr(run["index"], idx)
    mac = labnet_mac(run["index"], idx)
    pw_file = None
    if kind == "base":  # root login is still the way in; later layers have it disabled
        pw_file = rdir / "secrets" / f"{node}.root-password"
        pw_file.write_text(secrets.token_urlsafe(18))
        pw_file.chmod(0o600)
    doccap_pub = None
    if kind != "base":
        doccap_pub = install_node_keys(rdir, ldir, origin_node, node)
    entry = {"layer": layer["key"], "index": idx, "domain": domain,
             "reach": {"host": reach_host, "ports": {"22": INITIAL_HOST_PORT,
                                                      str(SSH_FALLBACK_PORT): SSH_FALLBACK_PORT}},
             "labnet": {"addr": labnet_addr(idx), "mac": mac},
             "arch": layer.get("arch"), "created": now_iso(),
             "emulated": layer.get("arch") != os.uname().machine,
             "kind": kind, "cloned_from": origin_node}
    try:
        run_cmd(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2",
                 "-b", str(ldir / "disk.qcow2"), str(disk)])
        if nvram:
            shutil.copyfile(ldir / "nvram.qcow2", nvram)
            nvram.chmod(0o600)
        labnet_env = f"LABNET_MAC={mac}\nLABNET_ADDR={labnet_addr(idx)}/24\n"
        ops = reseal_args(rdir, node, rdir / "keys" / "lab_ed25519.pub", pw_file, labnet_env,
                          identity=(kind != "checkpoint"), doccap_pub=doccap_pub,
                          tailnet_mtu=guest_tailnet_mtu(host_uplink_mtu())
                          if run.get("tailnet") == "ephemeral" else None)
        run_cmd(["virt-customize", "-q", "-a", str(disk)] + ops,
                env=dict(os.environ, LIBGUESTFS_BACKEND="direct"))
        xml = clone_domain_xml((ldir / "domain.xml").read_text(), name=domain, disk=str(disk),
                               nvram=str(nvram) if nvram else None, reach_host=reach_host,
                               labnet_mac_addr=mac, mcast_group=run["mcast_group"],
                               mcast_port=run["mcast_port"],
                               serial_log=str(rdir / "disks" / f"{node}-serial.log"))
        xml_path = rdir / "disks" / f"{node}.xml"
        xml_path.write_text(xml)
        virsh(["define", str(xml_path)], capture=True)
        if run.get("mode") == "prototype":
            # the resealed, never-booted disk: what `reset` returns this node to
            shutil.copyfile(disk, rdir / "disks" / f"{node}.pristine.qcow2")
            entry["pristine"] = {"disk": f"{node}.pristine.qcow2", "commit": None}
    except Exception:
        for p in (disk, nvram):
            if p and p.exists():
                p.unlink()
        raise
    run["nodes"][node] = entry
    save_run(root, run)
    write_ssh_config(root, run)
    write_overlay(root, run)
    meta_path = ldir / "meta.json"
    meta = read_json(meta_path)
    meta["last_used"] = now_iso()
    meta_path.chmod(0o644)
    write_json(meta_path, meta)
    meta_path.chmod(0o444)
    if kind != "base":
        import_node_state(root, run_id, node, layer)
    if entry.get("pristine") and run.get("fork"):
        # the fork side of the restore point: the node's files right after cloning
        run = load_run(root, run_id)
        run["nodes"][node]["pristine"]["commit"] = git(Path(run["fork"]["worktree"]), "rev-parse", "HEAD").strip()
        save_run(root, run)
    if start:
        virsh(["start", domain], capture=True)
        if kind != "base":
            finish_clone(root, run_id, node, layer)
    return entry


# --------------------------------------------- cloning commissioned layers

def install_node_keys(rdir: Path, ldir: Path, old: str, new: str) -> Path:
    """The laptop-side doccap key for the clone, in the run's isolated home.

    Same name: the layer's key, as is. New name: a fresh key named for the
    new node; its public half is injected into the clone during reseal.
    """
    ssh_dir = rdir / "home" / ".ssh"
    ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if new == old:
        for f in (ldir / "keys").glob(f"doccap-at-{old}*"):
            shutil.copyfile(f, ssh_dir / f.name)
            (ssh_dir / f.name).chmod(0o644 if f.name.endswith(".pub") else 0o600)
    else:
        key = ssh_dir / f"doccap-at-{new}"
        run_cmd(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"doccap-at-{new}", "-f", str(key)])
    return ssh_dir / f"doccap-at-{new}.pub"


def set_yaml_value(text: str, path: str, value: str) -> str:
    """Replace one scalar in block-style YAML, addressed as `key` or `parent.key`.

    Node profiles are block-style with two-space indents; anything else is an
    error rather than a guess.
    """
    parts = path.split(".")
    lines = text.split("\n")
    if len(parts) == 1:
        start, end, indent = 0, len(lines), ""
    elif len(parts) == 2:
        try:
            start = lines.index(f"{parts[0]}:") + 1
        except ValueError:
            raise LabError(f"no block {parts[0]!r} in node profile")
        end = next((i for i in range(start, len(lines)) if lines[i] and not lines[i].startswith(" ")), len(lines))
        indent = "  "
    else:
        raise LabError(f"unsupported path {path!r}")
    for i in range(start, end):
        m = re.match(rf"^{indent}{re.escape(parts[-1])}:(\s.*)?$", lines[i])
        if m:
            lines[i] = f"{indent}{parts[-1]}: {value}"
            return "\n".join(lines)
    raise LabError(f"no key {path!r} in node profile")


def journal_path(jdir: Path, slug: str) -> Path:
    """nodes/<node>/journal/YYYY-MM-DD-HHMM-<slug>.md, never overwriting an entry."""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d-%H%M")
    path, n = jdir / f"{stamp}-{slug}.md", 2
    while path.exists():
        path, n = jdir / f"{stamp}-{slug}-{n}.md", n + 1
    return path


def journal_entry(trigger: str, before: str, after: str, done: list, notes: str) -> str:
    return "\n".join([
        "---", f"timestamp: {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "actor: lab/lab.py", f"trigger: {trigger}", "scope: node", "outcome: success",
        f"commit_before: {before}", f"commit_after: {after}", "---", "",
        "## What was done", "", *[f"- {d}" for d in done], "",
        "## Commands", "", "Run by the lab driver (`scripts/lab/lab.py`), not by a skill; "
        "see the run's report for the exact invocations.", "",
        "## Notes / drift detected", "", notes, ""])


def import_node_state(root: Path, run_id: str, node: str, layer: dict) -> None:
    """Bring the layer's node directory into the run's fork, renamed if needed."""
    run = load_run(root, run_id)
    wt = Path(run["fork"]["worktree"])
    ldir = layer_dir(root, layer["key"])
    old = layer["node"]
    ref = f"refs/lab/layer-{layer['key']}"
    heads = run_cmd(["git", "bundle", "list-heads", str(ldir / "fork.bundle")], capture=True).split()
    git(wt, "fetch", "-q", str(ldir / "fork.bundle"), f"{heads[1]}:{ref}")
    git(wt, "checkout", ref, "--", f"nodes/{old}")
    git(wt, "update-ref", "-d", ref)
    if node != old:
        git(wt, "mv", f"nodes/{old}", f"nodes/{node}")
        prof = wt / "nodes" / node / "node.yaml"
        text = prof.read_text()
        for key in ("name", "hostname", "tailscale_hostname"):
            text = set_yaml_value(text, key, node)
        text = set_yaml_value(text, "ssh.key_name", f"doccap-at-{node}")
        fp = run_cmd(["ssh-keygen", "-lf", str(runs_dir(root) / run_id / "home" / ".ssh" / f"doccap-at-{node}.pub")],
                     capture=True).split()[1]
        text = set_yaml_value(text, "ssh.public_key_fingerprint", json.dumps(fp))
        # roles describe what a node is for — the layer's are about the layer, not this node
        text = re.sub(r"^roles:.*(\n[ -].*)*", "roles: []", text, count=1, flags=re.M)
        prof.write_text(text)
        git(wt, "add", f"nodes/{node}/node.yaml")
    what = f"clone {node} from layer {layer['key']} ({layer['name']})" + (f", was {old}" if node != old else "")
    git(wt, *LAB_GIT_IDENTITY, "commit", "-q", "-m", f"lab: {what}")
    git(wt, "push", "-q", "origin", "HEAD")


def wait_for_ssh(root: Path, run_id: str, node: str, *, user: str, port: int, key: Path,
                 timeout: int) -> None:
    rdir = runs_dir(root) / run_id
    deadline = time.time() + timeout
    while time.time() < deadline:
        rc = subprocess.call(driver_ssh(rdir) + ["-i", str(key), "-p", str(port),
                              f"{user}@{node}", "true"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if rc == 0:
            return
        time.sleep(5)
    raise LabError(f"{node}: no SSH as {user} on {port} within {timeout} s")


def doccap_ssh(root: Path, run_id: str, node: str, command: str, *, capture=True):
    rdir = runs_dir(root) / run_id
    key = rdir / "home" / ".ssh" / f"doccap-at-{node}"
    return run_cmd(driver_ssh(rdir) + ["-i", str(key), "-p", str(SSH_FALLBACK_PORT), f"doccap@{node}", command],
                   capture=capture)


def finish_clone(root: Path, run_id: str, node: str, layer: dict) -> None:
    """Online half of cloning a commissioned layer or a checkpoint.

    Re-points the node's own fork clone at this run's lab remote and branch,
    and — for a resealed box — re-probes its identity into the profile, the
    same way the identity probe in bootstrap does, and journals the lineage.
    """
    run = load_run(root, run_id)
    n = run["nodes"][node]
    rdir = runs_dir(root) / run_id
    wt = Path(run["fork"]["worktree"])
    scale = 15 if n.get("emulated") else 1
    wait_for_ssh(root, run_id, node, user="doccap", port=SSH_FALLBACK_PORT,
                 key=rdir / "home" / ".ssh" / f"doccap-at-{node}", timeout=300 * scale)
    url, branch, repo_dir = run["fork"]["remote_url"], run["fork"]["branch"], run_id
    doccap_ssh(root, run_id, node, (
        "set -e; old=$(dirname $(ls -d ~/*/.git | head -1)); "
        f'[ "$old" = "$HOME/{repo_dir}" ] || mv "$old" "$HOME/{repo_dir}"; '
        f"cd ~/{repo_dir}; git remote set-url origin {url}; git fetch -q origin; "
        f"git checkout -q -B {branch} origin/{branch}; "
        f'git config user.name "drydocc {node}"; git config user.email "doccap@{node}.invalid"'))
    if n["kind"] == "checkpoint":
        return  # a restore point keeps its identity
    probe = doccap_ssh(root, run_id, node, (
        "printf 'sha256:%s ' \"$(tr -d '[:space:]' < /etc/machine-id | sha256sum | awk '{print $1}')\"; "
        "ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | awk '{print $2}'")).split()
    prof = wt / "nodes" / node / "node.yaml"
    text = prof.read_text()
    text = set_yaml_value(text, "identity.machine_id_hash", json.dumps(probe[0]))
    text = set_yaml_value(text, "identity.host_key_fingerprint", json.dumps(probe[1]))
    text = set_yaml_value(text, "identity.first_seen", dt.date.today().isoformat())
    prof.write_text(text)
    before = git(wt, "rev-parse", "--short", "HEAD").strip()
    git(wt, "add", f"nodes/{node}/node.yaml")
    git(wt, *LAB_GIT_IDENTITY, "commit", "-q", "-m", f"lab: {node} identity after reseal")
    after = git(wt, "rev-parse", "--short", "HEAD").strip()
    entry = journal_path(wt / "nodes" / node / "journal", "lab-clone")
    origin_note = (f"Renamed from `{layer['node']}`: name, hostname, tailscale_hostname and the doccap key "
                   f"(`doccap-at-{node}`) follow the new name; roles start empty." if layer["node"] != node else
                   "Kept its node name.")
    entry.write_text(journal_entry(
        f"lab clone {run_id} {node} {layer['key']}", before, after,
        [f"Cloned from lab layer `{layer['key']}` (`{layer['name']}`, {layer['kind']}).",
         "Resealed offline before first boot: new machine id, host keys, hostname and node name.",
         f"Identity re-probed: machine id hash `{probe[0][:19]}…`, host key `{probe[1]}`.",
         f"Fork clone on the node re-pointed to this run's lab remote and branch `{branch}`."],
        origin_note + " Journal entries before this one describe the layer's source box."))
    git(wt, "add", str(entry.relative_to(wt)))
    git(wt, *LAB_GIT_IDENTITY, "commit", "-q", "-m", f"lab: journal {node} clone")
    git(wt, "push", "-q", "origin", "HEAD")
    doccap_ssh(root, run_id, node, f"git -C ~/{repo_dir} pull -q --ff-only")


# ------------------------------------------------------- isolation surfaces

def write_ssh_config(root: Path, run: dict) -> None:
    rdir = runs_dir(root) / run["run"]
    lines = [f"# drydocc lab run {run['run']} — generated; do not edit", ""]
    for node, n in sorted(run["nodes"].items()):
        lines += [f"Host {node}",
                  f"  HostName {n['reach']['host']}",
                  f"  HostKeyAlias {node}",
                  ""]
    lines += ["Host *",
              f"  UserKnownHostsFile {rdir / 'known_hosts'}",
              "  StrictHostKeyChecking accept-new",
              f"  IdentityFile {rdir / 'keys' / 'lab_ed25519'}",
              "  IdentitiesOnly no",
              "  ConnectTimeout 10", ""]
    (rdir / "ssh_config").write_text("\n".join(lines))


def real_tool(name: str, shim_dir: Path) -> str:
    """The real binary, skipping the run's own shim directory on PATH."""
    path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep)
                           if Path(p).resolve() != shim_dir.resolve())
    found = shutil.which(name, path=path)
    return found or f"/usr/bin/{name}"


# The hypervisor and disk tools under a lab node. A real box has no host to
# repair it from, so an agent in a run gets none (lab-overlay.md § "The
# hypervisor is out of reach"); the driver's own calls bypass the shims.
HYPERVISOR_TOOLS = ("virsh", "virt-install", "virt-customize", "virt-sysprep", "virt-cat", "virt-copy-in",
                    "virt-copy-out", "virt-edit", "virt-ls", "virt-viewer", "guestfish", "guestmount", "qemu-img")
# what an agent's LIBVIRT_DEFAULT_URI is set to: no driver answers it, so even a
# hypervisor client called by absolute path has nothing to connect to
UNREACHABLE_LIBVIRT_URI = "drydocc-lab-out-of-reach:///"


def write_shims(root: Path, run: dict) -> None:
    """PATH shims that keep an agent inside the run.

    OpenSSH resolves ~ from the password database, not $HOME, so an isolated
    HOME alone does not isolate ssh: the shim passes the run's own config.
    """
    rdir = runs_dir(root) / run["run"]
    bindir = rdir / "bin"
    cfg = rdir / "ssh_config"
    kh = rdir / "known_hosts"
    # the driver, called from inside a run, works with the laptop's real tools
    driver = (f'env -u LIBVIRT_DEFAULT_URI PATH="$(printf %s "$PATH" | tr : "\\n" | grep -vxF "{bindir}" '
              f'| paste -sd: -)" DRYDOCC_LAB_HOME="{root}" {sys.executable} "{Path(__file__).resolve()}"')
    shims = {
        "ssh": f'#!/bin/sh\nexec {real_tool("ssh", bindir)} -F "{cfg}" "$@"\n',
        "scp": f'#!/bin/sh\nexec {real_tool("scp", bindir)} -F "{cfg}" "$@"\n',
        "ssh-keygen": (
            "#!/bin/sh\n"
            "# -R edits a known_hosts file: point it at the run's own unless -f is given.\n"
            'case " $* " in *" -R "*) case " $* " in *" -f "*) ;; *) '
            f'exec {real_tool("ssh-keygen", bindir)} -f "{kh}" "$@" ;; esac ;; esac\n'
            f'exec {real_tool("ssh-keygen", bindir)} "$@"\n'),
        # The laptop's own tailnet client is never reachable from a run: off, there
        # is no tailnet; ephemeral, lab nodes are reached by the address they
        # report, never by a name that might be a real device's. Both are the
        # unreachable-daemon fallback path the framework already handles.
        "tailscale": ("#!/bin/sh\n"
                      'echo "failed to connect to local tailscaled; it doesn\'t appear to be running '
                      f'(drydocc lab: {"tailnet off" if run.get("tailnet", "off") == "off" else "the laptop client is out of a lab run reach"})"'
                      ' >&2\nexit 1\n'),
        # the admin's out-of-band steps on lab hosts (lab-overlay.md § "Admin actions")
        "lab-admin": f'#!/bin/sh\nexec {driver} admin "{run["run"]}" "$@"\n',
        # the restore-point verbs of this run, for an admin's session inside it
        # (lab skill § "Prototype session") — bound to this run, nothing else
        "lab": ("#!/bin/sh\n"
                f'lab() {{ exec {driver} "$@"; }}\n'
                'case "$1" in\n'
                f'  checkpoint|reset) v=$1; shift; lab "$v" "{run["run"]}" "$@" ;;\n'
                '  ls|tree) lab "$1" ;;\n'
                '  *) echo "lab: inside a run only checkpoint, reset, ls and tree are available" >&2; exit 2 ;;\n'
                'esac\n'),
        "kpxc-agent": ("#!/bin/sh\n"
                       'echo "drydocc lab: the password vault is not reachable from a lab run; '
                       'lab secrets come from the environment" >&2\nexit 1\n'),
        **{tool: ("#!/bin/sh\n"
                  f'echo "drydocc lab: {tool} is out of reach in a lab run. Reach the node over ssh, as on a real '
                  'box; a node that stopped answering is a finding to record, not something to repair from '
                  'the host." >&2\nexit 126\n') for tool in HYPERVISOR_TOOLS},
    }
    for name, body in shims.items():
        p = bindir / name
        p.write_text(body)
        p.chmod(0o755)


# --------------------------------------------------- driver access to nodes

def driver_ssh(rdir: Path) -> list:
    """ssh as the lab driver itself — with its own known_hosts, never the agent's.

    The agent's known_hosts is part of what is under test (a rebuilt box's new
    host key is the admin's problem to handle); the driver's is not, and it
    forgets a node's key whenever it replaces the box.
    """
    return ["ssh", "-F", str(rdir / "ssh_config"), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", f"UserKnownHostsFile={rdir / 'driver_known_hosts'}"]


def node_shell(root: Path, run_id: str, node: str, command: str, *, stdin: str | None = None):
    """(exit status, output) of a command on a node, as whoever the lab can be there.

    doccap on the fallback port once the node is bootstrapped (root login is
    off by then); root on the initial port before that.
    """
    rdir = runs_dir(root) / run_id
    key = rdir / "home" / ".ssh" / f"doccap-at-{node}"
    base = driver_ssh(rdir)
    attempts = []
    if key.exists():
        attempts.append(base + ["-i", str(key), "-p", str(SSH_FALLBACK_PORT), f"doccap@{node}", command])
    attempts.append(base + ["-p", str(INITIAL_HOST_PORT), f"root@{node}", command])
    res = None
    for round_ in range(4):  # ssh failing outright (255) is a connection hiccup — e.g. sshd
        for cmd in attempts:  # mid-reload — not a result: try each way in, a few times
            res = subprocess.run(cmd, text=True, capture_output=True, input=stdin)
            if res.returncode != 255:
                return res.returncode, res.stdout + res.stderr
        time.sleep(3 * (round_ + 1))
    return res.returncode, res.stdout + res.stderr


def sync_hosts(root: Path, run_id: str) -> None:
    """Every node resolves every peer's name to its labnet address.

    Stands in for the tailnet's name resolution: a node addresses a peer by
    its tailscale_hostname, exactly as it would on a real tailnet.
    """
    run = load_run(root, run_id)
    lines = "".join(f"{n['labnet']['addr']} {name}\n" for name, n in sorted(run["nodes"].items()))
    block = f"# drydocc-lab peers begin\n{lines}# drydocc-lab peers end\n"
    cmd = ("sudo sh -c 'sed -i \"/# drydocc-lab peers begin/,/# drydocc-lab peers end/d\" /etc/hosts "
           "&& cat >> /etc/hosts'")
    for name in run["nodes"]:
        rc, out = node_shell(root, run_id, name, cmd, stdin=block)
        if rc != 0:
            raise LabError(f"{name}: could not write peer names to /etc/hosts: {out.strip()[-200:]}")


def replace_node(root: Path, run_id: str, node: str, layer_ref: str) -> dict:
    """Total loss of a node's box, and a fresh box in its place.

    The box is gone — domain, disk, NVRAM — but everything in git survives,
    exactly the premise of a rebuild. The replacement keeps the node's
    addresses, so peers and the overlay still find it.
    """
    with store_lock(root):
        run = load_run(root, run_id)
        n = run["nodes"].get(node)
        if n is None:
            raise LabError(f"no node {node} in run {run_id}")
        if run.get("tailnet") == "ephemeral":
            # a lost box drops off a real tailnet only when someone removes it; the lab
            # does so at once, so the replacement can join under the same name
            try:
                tailnet_logout(root, run_id, node)
            except (subprocess.TimeoutExpired, LabError):
                pass
        destroy_domain(n["domain"])
        rdir = runs_dir(root) / run_id
        for f in (rdir / "disks").glob(f"{node}*"):
            f.unlink()
        (rdir / "secrets" / f"{node}.root-password").unlink(missing_ok=True)
        idx = n["index"]
        subprocess.call(["ssh-keygen", "-q", "-R", node, "-f", str(rdir / "driver_known_hosts")],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        del run["nodes"][node]
        save_run(root, run)
        entry = clone_node(root, run_id, node, layer_ref, start=True, index=idx)
    layer = resolve_layer(root, layer_ref)
    wait_for_ssh(root, run_id, node, user="root", port=INITIAL_HOST_PORT,
                 key=runs_dir(root) / run_id / "keys" / "lab_ed25519",
                 timeout=300 * (15 if layer.get("arch") != os.uname().machine else 1))
    sync_hosts(root, run_id)
    return entry


def admin_action(root: Path, run_id: str, action: str, args: list, stdin_text: str) -> str:
    if action == "authorize-key":
        if len(args) != 2:
            raise LabError("usage: lab-admin authorize-key <node> <user>  (public key on stdin)")
        node, user = args
        key = stdin_text.strip()
        if not re.fullmatch(r"(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp\d+) [A-Za-z0-9+/=]+( \S+)?", key):
            raise LabError("stdin is not a single OpenSSH public key line")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
            raise LabError(f"not a user name: {user!r}")
        script = (f"sudo -u {user} -H sh -c 'install -d -m 700 ~/.ssh && touch ~/.ssh/authorized_keys && "
                  "chmod 600 ~/.ssh/authorized_keys && k=$(cat) && "
                  "(grep -qxF \"$k\" ~/.ssh/authorized_keys || printf \"%s\\n\" \"$k\" >> ~/.ssh/authorized_keys)'")
        rc, out = node_shell(root, run_id, node, script, stdin=key + "\n")
        if rc != 0:
            raise LabError(f"authorize-key on {node} failed: {out.strip()[-300:]}")
        return f"authorized {key.split()[0]} key for {user} on {node}"
    raise LabError(f"unknown admin action {action!r} (known: authorize-key)")


# -------------------------------------------------------------- agent steps

AGENTS_DIR = Path(__file__).resolve().parent / "agents"


def default_adapter() -> str:
    """The adapter a command uses when neither its flag nor its scenario names one."""
    return os.environ.get("DRYDOCC_LAB_ADAPTER") or "claude"


def adapter_names() -> list:
    return sorted(p.stem for p in AGENTS_DIR.glob("*.sh"))


def adapter_status(adapter: str, model: str | None = None) -> dict:
    """An adapter's own readiness report (`<adapter>.sh --check`), never a model call.

    ready, cost (paid|free), model, detail — run against a throwaway HOME, so a
    check can link credentials but never touches the admin's own agent state.
    """
    script = AGENTS_DIR / f"{adapter}.sh"
    st = {"ready": False, "cost": "?", "model": "", "detail": f"no adapter {script}"}
    if script.is_file():
        with tempfile.TemporaryDirectory(prefix="drydocc-lab-check-") as home:
            env = dict(os.environ, HOME=home, DRYDOCC_LAB_REAL_HOME=str(Path.home()))
            if model:  # cost and readiness are the chosen model's, not the default's
                env["DRYDOCC_LAB_MODEL"] = model
            try:
                out = subprocess.run(["bash", str(script), "--check"], env=env, stdin=subprocess.DEVNULL,
                                     capture_output=True, text=True, timeout=120).stdout
                st.update(json.loads(out.strip().splitlines()[-1]))
            except (subprocess.TimeoutExpired, ValueError, IndexError):
                st["detail"] = "its --check did not answer"
    return dict(st, adapter=adapter)


def qualification(root: Path, adapter: str, model: str) -> str:
    """The latest `qualify` evidence for adapter + model: `<scenario> passed/judged, …`."""
    for d in sorted((root / "results").glob("qualify-*"), reverse=True):
        m = read_json(d / "matrix.json") if (d / "matrix.json").is_file() else {}
        if m.get("adapter") != adapter:
            continue
        cells = [c for c in m.get("cells", []) if c["model"] == model]
        judged = [(c["scenario"], [r for r in c["runs"] if r["verdict"] != "error"]) for c in cells]
        judged = [(s, rs) for s, rs in judged if rs]
        if judged:
            return ", ".join(f"{s} {sum(r['verdict'] == 'pass' for r in rs)}/{len(rs)}" for s, rs in judged)
    return ""


def agent_banner(root: Path, adapter: str, model: str | None) -> list:
    """What a lab command tells the admin about agents before its first step:
    the one it uses and its cost, then every other adapter that is ready — or,
    for a free one, what it still needs."""
    statuses = {a: adapter_status(a) for a in adapter_names()}
    cur = adapter_status(adapter, model) if model else statuses.get(adapter) or adapter_status(adapter)
    model = model or cur["model"] or "its default model"
    q = qualification(root, adapter, model)
    lines = [f"agent: {adapter} / {model} ({cur['cost']})" + (f" — qualified: {q}" if q else "")]
    if not cur["ready"]:
        lines.append(f"  {adapter} reports not ready: {cur['detail']}")
    for a, s in statuses.items():
        if a == adapter:
            continue
        if s["ready"]:
            aq = qualification(root, a, s["model"])
            lines.append(f"  also ready: {a} / {s['model'] or 'its default model'} ({s['cost']})"
                         + (f" — qualified: {aq}" if aq else "")
                         + f" — --adapter {a}, or DRYDOCC_LAB_ADAPTER={a}")
        elif s["cost"] == "free":
            lines.append(f"  free option not set up: {a} — {s['detail']}")
    return lines


def agent_env(root: Path, run_id: str, node: str | None) -> dict:
    """The environment an agent works in inside a run — headless or attended.

    Isolated HOME, the run's shims first on PATH, the run's identity; the
    admin's own DOCCAP_* and lab variables never leak in. A node still on
    root login (cloned from a base layer) gets its root password.
    """
    run = load_run(root, run_id)
    rdir = runs_dir(root) / run_id
    wt = Path(run["fork"]["worktree"])
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DOCCAP_", "DRYDOCC_LAB"))}
    env.update({
        "HOME": str(rdir / "home"),
        "PATH": f"{rdir / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
        "DRYDOCC_LAB": "1", "DRYDOCC_LAB_RUN": str(rdir),
        "DRYDOCC_LAB_OVERLAY": str(wt / ".drydocc" / "lab.yaml"),
        "DRYDOCC_LAB_REAL_HOME": str(Path.home()),
    })
    if os.environ.get("DRYDOCC_LAB_MODEL"):  # the admin's model choice is the one lab variable that passes
        env["DRYDOCC_LAB_MODEL"] = os.environ["DRYDOCC_LAB_MODEL"]
    env["LIBVIRT_DEFAULT_URI"] = UNREACHABLE_LIBVIRT_URI
    if node:
        if node not in run["nodes"]:
            raise LabError(f"no node {node} in run {run_id}")
        pw = rdir / "secrets" / f"{node}.root-password"
        if pw.exists():
            env["DOCCAP_ROOT_PW"] = pw.read_text().strip()
    if run.get("tailnet") == "ephemeral":
        env["DOCCAP_TS_KEY"] = tailnet_authkey()
    return env


def set_headless(root: Path, run_id: str, on: bool) -> None:
    """Mark the overlay unattended while a headless agent drives the run."""
    with store_lock(root):
        run = load_run(root, run_id)
        if on:
            run["headless"] = True
        else:
            run.pop("headless", None)
        save_run(root, run)
        write_overlay(root, run)


def run_agent(root: Path, run_id: str, prompt: str, *, adapter: str, node: str | None,
              timeout: int, env_extra: dict) -> dict:
    """One headless agent step inside a run's fork, judged without trusting its prose.

    blocked — the agent wrote .drydocc/lab-blocked.yaml (an unanswered decision)
    failed  — the adapter exited non-zero (including a timeout)
    done    — neither; whether the step *worked* is for the run's assertions to say
    """
    run = load_run(root, run_id)
    fork = run.get("fork")
    if not fork:
        raise LabError(f"run {run_id} has no fork — create it with run-create --fork")
    script = AGENTS_DIR / f"{adapter}.sh"
    if not script.is_file():
        raise LabError(f"no agent adapter {adapter!r} (looked for {script})")
    rdir = runs_dir(root) / run_id
    wt = Path(fork["worktree"])
    blocked = wt / ".drydocc" / "lab-blocked.yaml"
    blocked.unlink(missing_ok=True)
    n = len(run.get("steps", [])) + 1
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    prompt_file = rdir / "transcripts" / f"{n:02d}-{stamp}.prompt.txt"
    prompt_file.write_text(prompt)
    transcript = rdir / "transcripts" / f"{n:02d}-{stamp}.jsonl"
    env = agent_env(root, run_id, node)
    env.update({"DRYDOCC_LAB_TRANSCRIPT": str(transcript), "DRYDOCC_LAB_TIMEOUT": str(timeout)})
    env.update(env_extra)
    started = now_iso()
    set_headless(root, run_id, True)
    try:
        rc = subprocess.call(["bash", str(script), str(prompt_file)], cwd=str(wt), env=env,
                             stdin=subprocess.DEVNULL)
    finally:
        set_headless(root, run_id, False)
    # 75: the adapter says the agent could not work at all (auth, API outage) —
    # infrastructure, never a verdict on the skill under test
    verdict = ("infra" if rc == 75 else "blocked" if blocked.exists()
               else "failed" if rc != 0 else "done")
    step = {"n": n, "prompt": prompt, "adapter": adapter, "node": node, "rc": rc,
            "verdict": verdict, "started": started, "finished": now_iso(),
            "transcript": transcript.name}
    # optional adapter outputs: the models it used and its request count, and on
    # exit 75 the reason as the last line of its stderr log
    meta = Path(f"{transcript}.meta.json")
    if meta.is_file():
        try:
            step.update({k: v for k, v in read_json(meta).items() if k in ("models", "requests")})
        except ValueError:
            pass
    errlog = Path(f"{transcript}.stderr")
    if verdict == "infra" and errlog.is_file():
        tail = [ln for ln in errlog.read_text(errors="replace").splitlines() if ln.strip()]
        if tail:
            step["reason"] = tail[-1].strip()
    if verdict == "blocked":
        step["blocked"] = blocked.read_text()
    with store_lock(root):
        run = load_run(root, run_id)
        run.setdefault("steps", []).append(step)
        save_run(root, run)
    return step


def seal_node(root: Path, run_id: str, node: str, *, kind: str, name: str | None,
              skill: str | None, pin: bool) -> dict:
    """Shut a run's node down cleanly and keep its disk as a new layer.

    The layer's disk stays a linked overlay on its parent (cheap) until the
    chain grows deep, then it is flattened. The node's fork history travels
    with it as a git bundle, and so do the laptop-side keys made for it.
    """
    run = load_run(root, run_id)
    n = run["nodes"].get(node)
    if n is None:
        raise LabError(f"no node {node} in run {run_id}")
    if run.get("tailnet", "off") != "off":
        raise LabError(f"{node} is on a tailnet: a layer must never carry a tailnet identity — every clone "
                       "would come up as the same device. Seal from a run with the tailnet off.")
    parent = resolve_layer(root, n["layer"])
    rdir = runs_dir(root) / run_id
    fork = run.get("fork") or {}
    digest = None
    if skill and fork:
        digest = skills_digest(Path(fork["repo"]), fork["ref_sha"], skill)
    identity = {"kind": kind, "parent": parent["key"], "distro": parent["distro"],
                "version": parent["version"], "arch": parent["arch"],
                "iso_sha256": parent.get("iso_sha256"), "skills_digest": digest,
                "built_by_skill": skill, "node": node if kind != "base" else None}
    if kind == "checkpoint":
        identity["checkpoint_of"] = f"{run_id}/{node}@{now_iso()}"
    key = layer_key(identity)
    dest = layer_dir(root, key)
    if dest.exists():
        raise LabError(f"layer {key} already exists — this exact build is already sealed")
    virsh(["shutdown", n["domain"]], check=False, capture=True)
    scale = 15 if n.get("emulated") else 1  # an emulated guest shuts down slowly, not wrongly
    for _ in range(120 * scale):
        state = (virsh(["domstate", n["domain"]], check=False, capture=True) or "").strip()
        if state == "shut off":
            break
        time.sleep(2)
    else:
        raise LabError(f"{n['domain']} did not shut down within {4 * scale} minutes")
    src = rdir / "disks" / f"{node}.qcow2"
    dest.mkdir(parents=True)
    try:
        chain = json.loads(run_cmd(["qemu-img", "info", "--backing-chain", "--output=json", str(src)],
                                   capture=True))
        if len(chain) - 1 >= 4:
            run_cmd(["qemu-img", "convert", "-O", "qcow2", str(src), str(dest / "disk.qcow2")])
        else:
            shutil.copyfile(src, dest / "disk.qcow2")
        (dest / "domain.xml").write_text((rdir / "disks" / f"{node}.xml").read_text())
        if (rdir / "disks" / f"{node}_VARS.qcow2").exists():
            shutil.copyfile(rdir / "disks" / f"{node}_VARS.qcow2", dest / "nvram.qcow2")
        if fork:
            git(Path(fork["worktree"]), "bundle", "create", str(dest / "fork.bundle"), fork["branch"])
        keys = dest / "keys"
        keys.mkdir()
        for f in (rdir / "home" / ".ssh").glob(f"doccap-at-{node}*"):
            shutil.copyfile(f, keys / f.name)
        for f in dest.rglob("*"):
            if f.is_file():
                f.chmod(0o400 if "keys" in f.parts else 0o444)
        meta = dict(identity, key=key, name=name or f"{parent['name']}+{kind}", created=now_iso(),
                    last_used=now_iso(), pinned=pin, built_by_commit=fork.get("ref_sha"),
                    root_password_file=None)
        write_json(dest / "meta.json", meta)
        (dest / "meta.json").chmod(0o444)
    except Exception:
        remove_tree(dest)
        raise
    return meta


# ----------------------------------------------------------------- scenarios

def load_yaml(path: Path):
    return parse_yaml_text(path.read_text())


def parse_yaml_text(text: str):
    """Parse YAML with PyYAML when present, else through yq; refuse otherwise.

    Both backends read YAML 1.2 booleans — only true/false — so `tailnet: off`
    or an answer `yes` stays a string whichever one the host has. PyYAML alone
    would follow YAML 1.1 and turn them into False/True.
    """
    try:
        import yaml  # noqa: PLC0415 — optional dependency
    except ImportError:
        yaml = None
    if yaml is not None:
        class Yaml12Loader(yaml.SafeLoader):
            pass
        Yaml12Loader.yaml_implicit_resolvers = {
            first: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"]
            for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()}
        Yaml12Loader.add_implicit_resolver(
            "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))
        return yaml.load(text, Loader=Yaml12Loader)  # noqa: S506 — a SafeLoader subclass
    if shutil.which("yq"):
        res = subprocess.run(["yq", "-o=json", "."], input=text, text=True, capture_output=True)
        if res.returncode != 0:
            raise LabError(f"yq: {res.stderr.strip()[-300:]}")
        return json.loads(res.stdout) if res.stdout.strip() else None
    raise LabError("reading YAML needs PyYAML (python3 module `yaml`) or yq on this host")


def yaml_get(data, dotted: str):
    for part in dotted.split("."):
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def find_layer(root: Path, *, kind: str, distro: str, version: str, arch: str,
               digest: str | None = None) -> dict | None:
    hits = [m for m in load_layers(root).values()
            if m["kind"] == kind and str(m["distro"]) == str(distro)
            and str(m["version"]) == str(version) and m["arch"] == arch
            and (digest is None or m.get("skills_digest") == digest)]
    return max(hits, key=lambda m: m.get("created", "")) if hits else None


BOOTSTRAP_ANSWERS = {"bootstrap.source": "running", "bootstrap.initial-connection": "from-overlay",
                     "bootstrap.key-source": "generate", "bootstrap.fork-key-registered": "lab-remote"}


def build_commissioned(root: Path, *, repo: Path, ref: str, distro: str, version: str, arch: str,
                       log, adapter: str = "claude") -> dict:
    """Grow the clone tree by one commissioned layer, built by the ref under test."""
    base = find_layer(root, kind="base", distro=distro, version=version, arch=arch)
    if base is None:
        raise LabError(f"no base layer for {distro} {version} {arch} — build or import one first")
    run_id = f"build-{secrets.token_hex(3)}"
    log(f"building commissioned {distro} {version} {arch} from base {base['name']} in run {run_id}")
    with store_lock(root):
        create_run(root, run_id, "test", repo=repo, ref=ref)
        clone_node(root, run_id, "seed", base["key"], start=True)
        run = load_run(root, run_id)
        run["answers"].update(BOOTSTRAP_ANSWERS)
        save_run(root, run)
        write_overlay(root, run)
    scale = 15 if arch != os.uname().machine else 1

    def give_up(why: str):
        # a failed build is a kept, failed run — never an active one nobody collects
        with store_lock(root):
            run = load_run(root, run_id)
            run["status"], run["finished"] = "failed", now_iso()
            save_run(root, run)
        raise LabError(f"commissioning run {run_id}: {why} — kept for inspection (gc --failed)")

    step = run_agent(root, run_id, '/bootstrap-node seed "lab commissioned layer"', adapter=adapter,
                     node="seed", timeout=3600 * scale, env_extra={})
    if step["verdict"] != "done":
        give_up(f"bootstrap ended {step['verdict']}")
    wt = Path(load_run(root, run_id)["fork"]["worktree"])
    prof = load_yaml(wt / "nodes" / "seed" / "node.yaml") or {}
    for path, want in (("hardening.firewall", "enabled"), ("hardening.ssh_root_login", "disabled"),
                       ("hardening.ssh_password_auth", "disabled")):
        if yaml_get(prof, path) != want:
            give_up(f"{path} is {yaml_get(prof, path)!r}")
    if not prof.get("last_bootstrapped"):
        give_up("last_bootstrapped unset")
    with store_lock(root):
        meta = seal_node(root, run_id, "seed", kind="commissioned",
                         name=f"{distro}{version}-{arch}-commissioned-{secrets.token_hex(2)}",
                         skill="bootstrap-node", pin=False)
        run = load_run(root, run_id)
        run["status"] = "passed"
        save_run(root, run)
        destroy_run(root, run_id)
    log(f"sealed commissioned layer {meta['key']} ({meta['name']})")
    return meta


def resolve_from(root: Path, spec: dict, *, repo: Path, ref_sha: str, log, adapter: str = "claude") -> dict:
    src = spec.get("from", "commissioned")
    arch = spec.get("arch", os.uname().machine)
    if src not in ("base", "commissioned"):
        return resolve_layer(root, src)
    distro, version = spec.get("distro"), str(spec.get("version"))
    if not distro or not spec.get("version"):
        raise LabError(f"from: {src} needs distro and version")
    if src == "base":
        layer = find_layer(root, kind="base", distro=distro, version=version, arch=arch)
        if layer is None:
            raise LabError(f"no base layer for {distro} {version} {arch}")
        return layer
    digest = skills_digest(repo, ref_sha, "bootstrap-node")
    layer = find_layer(root, kind="commissioned", distro=distro, version=version, arch=arch, digest=digest)
    if layer is None:
        layer = build_commissioned(root, repo=repo, ref=ref_sha, distro=distro, version=version,
                                   arch=arch, log=log, adapter=adapter)
    return layer


def check_assertion(root: Path, run_id: str, step: dict) -> tuple:
    """(passed, detail) for one assertion step; passed is None when it was skipped."""
    run = load_run(root, run_id)
    wt = Path(run["fork"]["worktree"])
    kind, node = step["assert"], step.get("node")
    if node and node not in run["nodes"]:
        return False, f"no node {node} in this run"
    if kind in ("journal_entry", "node_yaml", "use_case"):
        git(wt, "pull", "-q", "--ff-only", check=False)  # pick up anything a node pushed
    if kind == "command":
        rc, out = node_shell(root, run_id, node, step["run"])
        ok = rc == int(step.get("exit", 0)) and step.get("stdout_contains", "") in out
        return ok, f"exit {rc}: {out.strip()[-300:]}"
    if kind == "http":
        status = int(step.get("status", 200))
        rc, out = node_shell(root, run_id, node,
                           f"curl -sS -o /tmp/.lab-http -w '%{{http_code}}' --max-time 15 {json.dumps(step['url'])}; "
                           "echo; cat /tmp/.lab-http; rm -f /tmp/.lab-http")
        code, _, body = out.partition("\n")
        ok = rc == 0 and code.strip() == str(status) and step.get("contains", "") in body
        return ok, f"HTTP {code.strip()}: {body.strip()[:200]}"
    if kind == "journal_entry":
        want_outcome = step.get("outcome", "success")
        for entry in sorted((wt / "nodes" / node / "journal").glob("*.md")):
            fm = entry.read_text().split("---")
            head = fm[1] if len(fm) > 2 else ""
            trig = re.search(r"^trigger:\s*(.*)$", head, re.M)
            outc = re.search(r"^outcome:\s*(\S+)", head, re.M)
            if trig and step["trigger_contains"] in trig.group(1) and outc and outc.group(1) == want_outcome:
                return True, entry.name
        return False, f"no entry with trigger containing {step['trigger_contains']!r} and outcome {want_outcome}"
    if kind == "node_yaml":
        got = yaml_get(load_yaml(wt / "nodes" / node / "node.yaml") or {}, step["path"])
        if step.get("nonempty"):
            return got not in (None, "", [], {}), f"{step['path']} = {got!r}"
        return str(got) == str(step["equals"]), f"{step['path']} = {got!r}"
    if kind == "use_cases":
        want, n_min = step.get("status", "planned"), int(step.get("count_min", 1))
        found = []
        for doc in sorted((wt / "nodes" / node / "use-cases").glob("*.md")):
            st = re.search(r"^status:\s*(\S+)", doc.read_text().split("---")[1], re.M)
            if st and st.group(1) == want:
                found.append(doc.stem)
        return len(found) >= n_min, f"{len(found)} {want}: {', '.join(found) or '-'}"
    if kind == "use_case":
        doc = wt / "nodes" / node / "use-cases" / f"{step['slug']}.md"
        if not doc.exists():
            return False, f"{doc.name} absent"
        st = re.search(r"^status:\s*(\S+)", doc.read_text().split("---")[1], re.M)
        got = st.group(1) if st else None
        return got == step.get("status", "active"), f"status {got}"
    if kind == "checks_pass":
        if not any((wt / "scripts").glob("check-*")):
            # A fork ships no framework checks: nothing ran, so nothing passed.
            return None, "skipped — this checkout has no framework checks to run"
        now = failing_checks(wt)
        base = baseline_failing_checks(root, run_id)
        new = sorted(set(now) - set(base))
        detail = "no new failures" if not new else f"new failures: {', '.join(new)}"
        if base:
            detail += f" (already failing at the ref: {', '.join(sorted(base))})"
        return not new, detail
    return False, f"unknown assertion {kind!r}"


def expand(obj, values: dict):
    """Substitute {name} placeholders in every string of a step."""
    if isinstance(obj, str):
        for k, v in values.items():
            obj = obj.replace("{" + k + "}", v)
        return obj
    if isinstance(obj, dict):
        return {k: expand(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand(v, values) for v in obj]
    return obj


def do_step(root: Path, run_id: str, step: dict, sc: dict, *, repo: Path, ref_sha: str, log) -> tuple:
    """A step the lab itself performs — the world changing under the agent."""
    action, node = step["do"], step.get("node")
    if node and node not in load_run(root, run_id)["nodes"]:
        return False, f"no node {node}"
    if action == "command":
        rc, out = node_shell(root, run_id, node, step["run"])
        return rc == int(step.get("exit", 0)), f"exit {rc}: {out.strip()[-200:]}"
    if action == "replace-node":
        spec = dict((sc.get("nodes") or {}).get(node) or {}, **{k: v for k, v in step.items()
                                                                 if k in ("from", "distro", "version", "arch")})
        spec.setdefault("from", "base")
        layer = resolve_from(root, spec, repo=repo, ref_sha=ref_sha, log=log, adapter=sc.get("adapter") or default_adapter())
        replace_node(root, run_id, node, layer["key"])
        return True, f"{node} is a fresh {layer['kind']} box ({layer['name']}); its git state survives"
    if action in ("reboot", "power-cycle"):
        return fault(root, run_id, action, node), f"{action} {node}"
    return False, f"unknown action {action!r}"


def fault(root: Path, run_id: str, action: str, node: str) -> bool:
    domain = load_run(root, run_id)["nodes"][node]["domain"]
    if action == "reboot":  # the guest is asked to reboot, like an admin's reboot
        return virsh(["reboot", domain], check=False, capture=True) is not None
    # power-cycle: the plug is pulled — no shutdown, nothing flushed
    virsh(["destroy", domain], check=False, capture=True)
    return virsh(["start", domain], check=False, capture=True) is not None


def schedule_fault(root: Path, run_id: str, spec: dict, log):
    """Inject a fault into a node while an agent step is running."""
    import threading  # noqa: PLC0415

    def fire():
        ok = fault(root, run_id, spec["do"], spec["node"])
        log(f"inject: {spec['do']} {spec['node']} after {spec['after']} s — {'done' if ok else 'FAILED'}")
    t = threading.Timer(float(spec["after"]), fire)
    t.daemon = True
    t.start()
    return t


def failing_checks(tree: Path) -> list:
    failed = []
    for c in sorted((tree / "scripts").glob("check-*")):
        if subprocess.call([str(c)], cwd=str(tree), stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) != 0:
            failed.append(c.name)
    return failed


def baseline_failing_checks(root: Path, run_id: str) -> list:
    """Checks already failing at the ref under test, measured once per run."""
    run = load_run(root, run_id)
    if "baseline_failing_checks" not in run:
        fork = run["fork"]
        tmp = runs_dir(root) / run_id / "baseline"
        git(Path(fork["repo"]), "worktree", "add", "-q", "--detach", str(tmp), fork["ref_sha"])
        try:
            failed = failing_checks(tmp)
        finally:
            git(Path(fork["repo"]), "worktree", "remove", "--force", str(tmp), check=False)
        with store_lock(root):
            run = load_run(root, run_id)
            run["baseline_failing_checks"] = failed
            save_run(root, run)
    return run["baseline_failing_checks"]


def run_scenario(root: Path, scenario, *, repo: Path, ref: str | None, keep: bool, log,
                 prepare=None, adapter: str | None = None, model: str | None = None) -> dict:
    """Run a scenario — a file, or one generated as a dict — and judge it.

    prepare(run_id), when given, runs once the fork exists and before any
    node is cloned (verify-runbook installs its bundle there). model, when
    given, is the DRYDOCC_LAB_MODEL of every agent step.
    """
    path = scenario if isinstance(scenario, Path) else None
    sc = load_yaml(path) if path else scenario
    name = sc["scenario"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,23}", name):
        raise LabError("scenario: kebab-case, at most 24 chars")
    ref = ref or sc.get("ref", "HEAD")
    run_id = f"{name}-{secrets.token_hex(3)}"
    results = root / "results" / run_id
    results.mkdir(parents=True)
    sc = dict(sc, adapter=adapter or sc.get("adapter") or default_adapter())
    model = model or os.environ.get("DRYDOCC_LAB_MODEL")
    report = {"scenario": name, "file": str(path) if path else sc.get("file", "(generated)"),
              "adapter": sc["adapter"], "model": model,
              "run": run_id, "ref": ref, "started": now_iso(),
              "steps": [], "verdict": None}
    try:
        ref_sha = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
        report["ref_sha"] = ref_sha
        layers = {n: resolve_from(root, spec or {}, repo=repo, ref_sha=ref_sha, log=log,
                                  adapter=sc["adapter"])
                  for n, spec in (sc.get("nodes") or {}).items()}
        with store_lock(root):
            create_run(root, run_id, "test", repo=repo, ref=ref_sha, tailnet=sc.get("tailnet", "off"))
            if prepare:
                prepare(run_id)
            for node, layer in layers.items():
                log(f"{node}: cloning {layer['name']} ({layer['kind']})")
                clone_node(root, run_id, node, layer["key"], start=True)
        # every node answers before the first step (commissioned clones already waited)
        for node, layer in layers.items():
            if layer["kind"] == "base":
                scale = 15 if layer.get("arch") != os.uname().machine else 1
                wait_for_ssh(root, run_id, node, user="root", port=INITIAL_HOST_PORT,
                             key=runs_dir(root) / run_id / "keys" / "lab_ed25519", timeout=300 * scale)
        if len(layers) > 1:
            sync_hosts(root, run_id)
            log("peers: every node resolves every other node's name over labnet")
        with store_lock(root):
            run = load_run(root, run_id)
            run["answers"].update({k: str(v) if not isinstance(v, bool) else ("yes" if v else "no")
                                   for k, v in (sc.get("answers") or {}).items()})
            save_run(root, run)
            write_overlay(root, run)
        # secrets the scenario's use-cases need: fresh random values per run, kept
        # in the run's 0600 secrets dir and handed to agents only through the env
        secret_env = {}
        for name in sc.get("secrets") or []:
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
                raise LabError(f"secret name must be UPPER_SNAKE_CASE: {name!r}")
            secret_env[name] = secrets.token_urlsafe(24)
        if secret_env:
            sfile = runs_dir(root) / run_id / "secrets" / "scenario.env"
            sfile.write_text("".join(f"{k}={v}\n" for k, v in secret_env.items()))
            sfile.chmod(0o600)
        agent_extra = dict(secret_env, **({"DRYDOCC_LAB_MODEL": model} if model else {}))
        verdict = "pass"
        for i, step in enumerate(sc.get("steps") or [], 1):
            step = expand(step, {"run": run_id})
            label = step.get("name") or step.get("agent") or step.get("assert") or step.get("do")
            if "do" in step:
                ok, detail = do_step(root, run_id, step, sc, repo=repo, ref_sha=ref_sha, log=log)
                log(f"step {i}: do {step['do']} — {'ok' if ok else 'FAILED'}: {detail}")
                entry = {"n": i, "name": label, "type": "do", "do": step["do"], "ok": ok, "detail": detail}
            elif "agent" in step:
                scale = 15 if step.get("node") and load_run(root, run_id)["nodes"][step["node"]].get("emulated") else 1
                log(f"step {i}: agent — {label}")
                injector = schedule_fault(root, run_id, step.get("inject"), log) if step.get("inject") else None
                res = run_agent(root, run_id, step["agent"], adapter=sc["adapter"],
                                node=step.get("node"), timeout=int(step.get("timeout", 3600)) * scale,
                                env_extra=agent_extra)
                if injector:
                    injector.cancel()
                expect = step.get("expect", "done")
                ok = (expect == "any" and res["verdict"] != "infra") or res["verdict"] == expect
                entry = {"n": i, "name": label, "type": "agent", "verdict": res["verdict"],
                         "ok": ok, "transcript": res["transcript"], "blocked": res.get("blocked")}
                entry.update({k: res[k] for k in ("models", "requests", "reason") if k in res})
                if res["verdict"] == "blocked" and not ok:
                    verdict = "blocked"
                if res["verdict"] == "infra":
                    verdict = "error"
                    report["error"] = ("infrastructure: the agent could not run ("
                                       + (res.get("reason") or "authentication or API failure, see the transcript")
                                       + ") — not a result about the code under test")
            else:
                ok, detail = check_assertion(root, run_id, step)
                skipped, ok = ok is None, ok is not False
                log(f"step {i}: assert {step['assert']} — "
                    f"{'skipped' if skipped else 'ok' if ok else 'FAILED'}: {detail}")
                entry = {"n": i, "name": label, "type": "assert", "assert": step["assert"],
                         "ok": ok, "detail": detail}
                if skipped:
                    entry["skipped"] = True
            report["steps"].append(entry)
            if sc.get("tailnet") == "ephemeral" and "assert" not in step:
                exposed = untagged_on_tailnet(root, run_id)
                if exposed:
                    for node in exposed:
                        tailnet_logout(root, run_id, node)
                    verdict = "error"
                    report["error"] = (f"{', '.join(exposed)} joined the tailnet untagged — as the admin's own "
                                       "device, outside the lab fence — and was logged out at once. Make the auth "
                                       "key with the lab tag (skills/lab/references/lab-commands.md).")
                    log(f"error: {report['error']}")
                    break
            if not ok:
                verdict = verdict if verdict in ("blocked", "error") else "fail"
                break
        watched = dict(secret_env)
        if sc.get("tailnet") == "ephemeral":
            watched["DOCCAP_TS_KEY"] = tailnet_authkey()
        if watched and verdict == "pass":
            # implicit last assertion: no secret value reached the fork
            wt = Path(load_run(root, run_id)["fork"]["worktree"])
            git(wt, "pull", "-q", "--ff-only", check=False)
            history = git(wt, "log", "-p", "--all", "--format=") or ""
            leaked = sorted(k for k, v in watched.items() if v in history)
            ok = not leaked
            detail = "no secret value in the fork's history" if ok else f"LEAKED into the fork: {', '.join(leaked)}"
            log(f"implicit: secrets stay out of git — {'ok' if ok else 'FAILED'}: {detail}")
            report["steps"].append({"n": len(report["steps"]) + 1, "name": "secrets stay out of git",
                                    "type": "assert", "assert": "no_secret_in_fork", "ok": ok, "detail": detail})
            if not ok:
                verdict = "fail"
        report["verdict"] = verdict
    except LabError as e:
        report["verdict"] = "error"
        report["error"] = str(e)
        log(f"error: {e}")
    report["finished"] = now_iso()
    rdir = runs_dir(root) / run_id
    if rdir.exists():
        shutil.copytree(rdir / "transcripts", results / "transcripts", dirs_exist_ok=True)
        with store_lock(root):
            run = load_run(root, run_id)
            run["status"] = "passed" if report["verdict"] == "pass" else "failed"
            run["finished"] = now_iso()
            save_run(root, run)
            if report["verdict"] == "pass" and not keep:
                destroy_run(root, run_id)
                report["collected"] = True
    write_json(results / "report.json", report)
    (results / "report.md").write_text(render_report(report))
    return report


def render_report(r: dict) -> str:
    lines = [f"# Lab run {r['run']} — {r['verdict'].upper()}", "",
             f"- scenario: `{r['scenario']}` ({r['file']})",
             f"- ref: `{r['ref']}` ({r.get('ref_sha', '?')[:12]}), agent adapter `{r.get('adapter', 'claude')}`",
             f"- started {r['started']}, finished {r['finished']}"]
    used = list(dict.fromkeys(m for s in r["steps"] for m in s.get("models") or []))
    if used or r.get("model"):
        requests = sum(s.get("requests") or 0 for s in r["steps"])
        lines.append(f"- models: {', '.join(f'`{m}`' for m in used) or '`' + r['model'] + '`'}"
                     + (f", {requests} model requests" if requests else "")
                     + (" — a step switched models, weaker evidence for any one of them"
                        if any(len(s.get("models") or []) > 1 for s in r["steps"]) else ""))
    lines.append("")
    if r.get("error"):
        lines += [f"**Error:** {r['error']}", ""]
    lines += ["| # | step | result | detail |", "|---|---|---|---|"]
    for s in r["steps"]:
        detail = s.get("detail") or s.get("transcript") or ""
        if s.get("blocked"):
            detail = "blocked: " + s["blocked"]
        detail = " ".join(str(detail).split())  # one table row, whatever the output looked like
        lines.append(f"| {s['n']} | {s['name']} | {'skipped' if s.get('skipped') else 'ok' if s['ok'] else 'FAILED'} ({s.get('verdict', s['type'])}) "
                     f"| {str(detail).replace('|', '/')[:160]} |")
    if not r.get("collected"):
        lines += ["", f"The run was kept for inspection: `lab.py runs`, `lab.py gc --failed` when done."]
    return "\n".join(lines) + "\n"


def repeat_verdict(verdicts: list) -> str:
    passed = sum(v == "pass" for v in verdicts)
    if passed == len(verdicts):
        return "pass"
    return "regression" if passed == 0 else "flake"


# ------------------------------------------------------------- qualification

def iso_seconds(a: str, b: str) -> int:
    return int((dt.datetime.fromisoformat(b) - dt.datetime.fromisoformat(a)).total_seconds())


def forked_runs(root: Path) -> list:
    """Runs that still have a fork. A fork is a standalone clone, but it is still
    a directory under the same user: an agent that went looking could read it."""
    d = runs_dir(root)
    return sorted(r.name for r in (d.iterdir() if d.is_dir() else [])
                  if (r / "run.json").is_file() and load_run(root, r.name).get("fork"))


def qualify(root: Path, *, adapter: str, models: list, scenarios: list, repeat: int, repo: Path,
            ref: str | None, log, resume: str | None = None, run=run_scenario,
            allow_visible_runs: bool = False) -> dict:
    """Run every scenario `repeat` times on every model, one pinned model per run,
    and keep the matrix that says which models can run drydocc.

    An infrastructure error is one model's trouble (overloaded, withdrawn) until
    shown otherwise: its cell gets one more try, then the matrix moves on. Two
    errors in a row on different models point at the account (a daily cap, a
    bad key) and stop the qualification. `resume` picks the same matrix up later
    and runs only what still lacks a judged result.

    Runs must not learn from each other: another run's fork is readable from
    every run, so qualification starts only when none is left, and destroys
    each of its own runs once the result is recorded (report and transcripts
    stay under results/).
    """
    visible = forked_runs(root)
    if visible and not allow_visible_runs:
        raise LabError(f"runs whose forks an agent could read: {', '.join(visible)} — a model could copy their "
                       "work. Destroy them (lab.py destroy <run>, gc --failed) or pass --allow-visible-runs")
    if resume:
        qdir = root / "results" / resume
        m = read_json(qdir / "matrix.json")
        if m["adapter"] != adapter:
            raise LabError(f"{resume} qualifies adapter {m['adapter']}, not {adapter}")
    else:
        qdir = root / "results" / f"qualify-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
        qdir.mkdir(parents=True)
        m = {"adapter": adapter, "started": now_iso(), "repeat": repeat, "cells": []}
    last_error_model = None
    for model in models:
        for path in scenarios:
            name = load_yaml(path)["scenario"]
            cell = next((c for c in m["cells"] if c["model"] == model and c["scenario"] == name), None)
            if cell is None:
                cell = {"model": model, "scenario": name, "runs": []}
                m["cells"].append(cell)
            errors_now = 0
            while sum(r["verdict"] != "error" for r in cell["runs"]) < repeat and errors_now < 2:
                log(f"qualify: {model} × {name}, run {len(cell['runs']) + 1}")
                r = run(root, path, repo=repo, ref=ref, keep=False, log=log, adapter=adapter, model=model)
                if (runs_dir(root) / r["run"] / "run.json").is_file():
                    with store_lock(root):
                        destroy_run(root, r["run"])
                    res = root / "results" / r["run"]
                    if (res / "report.json").is_file():  # the report must not promise a kept run
                        rep = dict(read_json(res / "report.json"), collected=True)
                        write_json(res / "report.json", rep)
                        (res / "report.md").write_text(render_report(rep))
                cell["runs"].append({"run": r["run"], "verdict": r["verdict"], "error": r.get("error"),
                                     "requests": sum(s.get("requests") or 0 for s in r["steps"]),
                                     "seconds": iso_seconds(r["started"], r["finished"])})
                write_json(qdir / "matrix.json", m)
                (qdir / "matrix.md").write_text(render_matrix(m, qdir.name))
                if r["verdict"] != "error":
                    last_error_model = None
                    continue
                errors_now += 1
                if last_error_model not in (None, model):
                    m["stopped"] = r.get("error")
                    write_json(qdir / "matrix.json", m)
                    (qdir / "matrix.md").write_text(render_matrix(m, qdir.name))
                    log(f"qualify: stopped — errors on two models in a row ({r.get('error')}); "
                        f"resume with: lab.py qualify --resume {qdir.name} …")
                    return dict(m, id=qdir.name)
                last_error_model = model
                log(f"qualify: {model} × {name}: error ({r.get('error')})"
                    + ("; one more try" if errors_now < 2 else "; moving on"))
    m.pop("stopped", None)
    m.pop("incomplete", None)
    m["finished"] = now_iso()
    if any(sum(r["verdict"] != "error" for r in c["runs"]) < repeat for c in m["cells"]):
        m["incomplete"] = True  # some cells only erred: --resume runs them again
    write_json(qdir / "matrix.json", m)
    (qdir / "matrix.md").write_text(render_matrix(m, qdir.name))
    return dict(m, id=qdir.name)


def render_matrix(m: dict, qid: str) -> str:
    """Pass rate per model × scenario; infrastructure errors are counted apart,
    never as failures."""
    models = list(dict.fromkeys(c["model"] for c in m["cells"]))
    names = list(dict.fromkeys(c["scenario"] for c in m["cells"]))
    lines = [f"# Qualification {qid} — adapter `{m['adapter']}`", "",
             f"Each cell: passed/judged runs, median model requests, median minutes"
             f" (infrastructure errors are not judged). Target: {m['repeat']} judged runs per cell.", ""]
    if m.get("stopped"):
        lines += [f"**Stopped:** {m['stopped']} — resume with `lab.py qualify --resume {qid}`.", ""]
    elif m.get("incomplete"):
        lines += [f"**Incomplete:** some cells ended only in errors — `lab.py qualify --resume {qid}` "
                  "runs them again.", ""]
    lines += ["| model | " + " | ".join(names) + " | overall |", "|---" * (len(names) + 2) + "|"]
    for model in models:
        row, passed, judged = [], 0, 0
        for name in names:
            c = next((c for c in m["cells"] if c["model"] == model and c["scenario"] == name), None)
            rs = [r for r in (c or {}).get("runs", []) if r["verdict"] != "error"]
            errs = len((c or {}).get("runs", [])) - len(rs)
            if not rs:
                row.append("—" + (f" ({errs} error)" if errs else ""))
                continue
            p = sum(r["verdict"] == "pass" for r in rs)
            passed, judged = passed + p, judged + len(rs)
            med = lambda k: sorted(r[k] for r in rs)[len(rs) // 2]  # noqa: E731
            row.append(f"{p}/{len(rs)}, {med('requests')} req, {round(med('seconds') / 60)} min"
                       + (f" ({errs} error)" if errs else ""))
        row.append(f"{passed}/{judged}" if judged else "—")
        lines.append(f"| `{model}` | " + " | ".join(row) + " |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- prototypes

def parse_node_spec(text: str, defaults: dict) -> tuple:
    """`web`, `web=base`, `web=commissioned` or `web=<layer>` → (name, from-spec)."""
    name, _, src = text.partition("=")
    if not re.fullmatch(SLUG_RE, name):
        raise LabError(f"node name {name!r} is not a valid drydocc slug")
    spec = {k: v for k, v in defaults.items() if v is not None}
    if src:
        spec["from"] = src
    return name, spec


def proto_up(root: Path, proto: str, specs: dict, *, repo: Path, ref: str, ttl: str | None, log,
             adapter: str = "claude") -> dict:
    """Start a prototype — a kept lab fork for an admin — or add nodes to one.

    Nothing about a prototype is collected on success: it lives until it is
    destroyed, or until its TTL runs out and `gc` takes it.
    """
    exists = (runs_dir(root) / proto / "run.json").is_file()
    if exists:
        run = load_run(root, proto)
        if run["mode"] != "prototype":
            raise LabError(f"run {proto} is a {run['mode']} run, not a prototype")
        ref_sha = run["fork"]["ref_sha"]
    else:
        ref_sha = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
        run = {"nodes": {}}
    new = {n: s for n, s in specs.items() if n not in run["nodes"]}
    for n in sorted(set(specs) - set(new)):
        log(f"{n}: already in {proto}, left as it is")
    layers = {n: resolve_from(root, s, repo=repo, ref_sha=ref_sha, log=log, adapter=adapter)
              for n, s in new.items()}
    with store_lock(root):
        if not exists:
            create_run(root, proto, "prototype", repo=repo, ref=ref, ttl=ttl)
        for node, layer in layers.items():
            log(f"{node}: cloning {layer['name']} ({layer['kind']})")
            clone_node(root, proto, node, layer["key"], start=True)
    for node, layer in layers.items():
        if layer["kind"] == "base":
            wait_for_ssh(root, proto, node, user="root", port=INITIAL_HOST_PORT,
                         key=runs_dir(root) / proto / "keys" / "lab_ed25519", timeout=300 * time_scale(layer))
    run = load_run(root, proto)
    if new and len(run["nodes"]) > 1:
        sync_hosts(root, proto)
    return run


def time_scale(layer: dict) -> int:
    return 15 if layer.get("arch") != os.uname().machine else 1


def fork_settled(wt: Path, node: str) -> str:
    """Bring the fork up to date with its remote and refuse unfinished work on a node.

    Returns HEAD. A restore point or a bundle must describe one consistent
    state — never a half-written node directory.
    """
    git(wt, "pull", "-q", "--ff-only", check=False)
    dirty = git(wt, "status", "--porcelain", "--", f"nodes/{node}") or ""
    if dirty.strip():
        raise LabError(f"nodes/{node} has uncommitted changes in {wt} — commit or discard them first:\n"
                       + dirty.rstrip())
    return git(wt, "rev-parse", "HEAD").strip()


def checkpoint_node(root: Path, run_id: str, node: str, label: str, *, log) -> dict:
    """Keep a prototype node's current state as a named, pinned restore point.

    The node shuts down, its disk is sealed as a checkpoint layer (with its
    firmware variables, its keys and the fork's history), and it boots again
    on top of that layer — so `reset` from here on returns to this state.
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", label):
        raise LabError("checkpoint name: lowercase letters, digits, hyphens")
    run = load_run(root, run_id)
    if run["mode"] != "prototype":
        raise LabError(f"run {run_id} is a {run['mode']} run — restore points belong to prototypes")
    n = run["nodes"].get(node)
    if n is None:
        raise LabError(f"no node {node} in run {run_id}")
    if any(m.get("name") == label for m in load_layers(root).values()):
        raise LabError(f"a layer named {label!r} already exists — pick another name")
    rdir = runs_dir(root) / run_id
    if not (rdir / "home" / ".ssh" / f"doccap-at-{node}").exists():
        raise LabError(f"{node} is not bootstrapped yet — a restore point starts at the baseline")
    commit = fork_settled(Path(run["fork"]["worktree"]), node)
    log(f"{node}: shutting down to seal restore point {label!r}")
    with store_lock(root):
        meta = seal_node(root, run_id, node, kind="checkpoint", name=label, skill=None, pin=True)
        disk = rdir / "disks" / f"{node}.qcow2"
        disk.unlink()
        run_cmd(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2",
                 "-b", str(layer_dir(root, meta["key"]) / "disk.qcow2"), str(disk)])
        (rdir / "disks" / f"{node}.pristine.qcow2").unlink(missing_ok=True)
        run = load_run(root, run_id)
        run["nodes"][node].update(layer=meta["key"], kind="checkpoint",
                                  pristine={"disk": None, "commit": commit})
        save_run(root, run)
        virsh(["start", n["domain"]], capture=True)
    wait_for_ssh(root, run_id, node, user="doccap", port=SSH_FALLBACK_PORT,
                 key=rdir / "home" / ".ssh" / f"doccap-at-{node}", timeout=300 * time_scale(meta))
    return meta


def restore_node_tree(wt: Path, node: str, commit: str) -> bool:
    """Return nodes/<node>/ to its state at `commit` — all of it but the journal.

    The journal is append-only: entries written since stay, and the reset
    adds one saying the work they describe was discarded.
    """
    prefix, journal = f"nodes/{node}/", f"nodes/{node}/journal/"
    now = [f for f in (git(wt, "ls-files", "--", prefix) or "").splitlines() if f and not f.startswith(journal)]
    then = [f for f in (git(wt, "ls-tree", "-r", "--name-only", commit, "--", prefix) or "").splitlines()
            if f and not f.startswith(journal)]
    if now:
        git(wt, "rm", "-q", "-f", "--", *now)
    if then:
        git(wt, "checkout", commit, "--", *then)
    return bool((git(wt, "status", "--porcelain", "--", prefix) or "").strip())


def reset_node(root: Path, run_id: str, node: str, *, log) -> str:
    """Discard everything a prototype node did since its restore point.

    The restore point is its latest checkpoint, or — before any — the clone
    it started as. Box and fork go back together.
    """
    run = load_run(root, run_id)
    n = run["nodes"].get(node)
    if n is None:
        raise LabError(f"no node {node} in run {run_id}")
    p = n.get("pristine")
    if not p:
        raise LabError(f"{node} has no restore point — only a prototype's nodes can be reset")
    layer = resolve_layer(root, n["layer"])
    ldir = layer_dir(root, layer["key"])
    rdir = runs_dir(root) / run_id
    wt = Path(run["fork"]["worktree"])
    what = (f"restore point {layer['name']}" if n["kind"] == "checkpoint"
            else f"a fresh clone of {layer['name']} ({layer['kind']})")
    log(f"{node}: powering off and returning to {what}")
    virsh(["destroy", n["domain"]], check=False, capture=True)
    with store_lock(root):
        disk = rdir / "disks" / f"{node}.qcow2"
        disk.unlink(missing_ok=True)
        if p["disk"]:
            shutil.copyfile(rdir / "disks" / p["disk"], disk)
        else:
            run_cmd(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2",
                     "-b", str(ldir / "disk.qcow2"), str(disk)])
        if (ldir / "nvram.qcow2").exists():
            nv = rdir / "disks" / f"{node}_VARS.qcow2"
            nv.unlink(missing_ok=True)
            shutil.copyfile(ldir / "nvram.qcow2", nv)
            nv.chmod(0o600)
        if n["kind"] != "checkpoint":  # a fresh clone gets fresh host keys at first boot
            for kh in ("driver_known_hosts", "known_hosts"):
                subprocess.call(["ssh-keygen", "-q", "-R", node, "-f", str(rdir / kh)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git(wt, "pull", "-q", "--ff-only", check=False)
        before = git(wt, "rev-parse", "--short", "HEAD").strip()
        if p["commit"] and restore_node_tree(wt, node, p["commit"]):
            git(wt, "add", "-A", "--", f"nodes/{node}")
            git(wt, *LAB_GIT_IDENTITY, "commit", "-q", "-m", f"lab: reset {node} to {what}")
            after = git(wt, "rev-parse", "--short", "HEAD").strip()
            jdir = wt / "nodes" / node / "journal"
            if jdir.is_dir():
                entry = journal_path(jdir, "lab-reset")
                entry.write_text(journal_entry(
                    f"lab reset {run_id} {node}", before, after,
                    [f"Box returned to {what}: disk and firmware variables replaced, everything done on "
                     "the box since then discarded.",
                     f"Node files other than the journal returned to their state at `{p['commit'][:12]}`."],
                    "Journal entries between that commit and this entry describe work that was discarded; "
                    "they stay because the journal is append-only."))
                git(wt, "add", str(entry.relative_to(wt)))
                git(wt, *LAB_GIT_IDENTITY, "commit", "-q", "-m", f"lab: journal {node} reset")
            git(wt, "push", "-q", "origin", "HEAD")
        virsh(["start", n["domain"]], capture=True)
    if n["kind"] == "base":
        wait_for_ssh(root, run_id, node, user="root", port=INITIAL_HOST_PORT,
                     key=rdir / "keys" / "lab_ed25519", timeout=300 * time_scale(layer))
    else:
        finish_clone(root, run_id, node, layer)
    if len(run["nodes"]) > 1:
        sync_hosts(root, run_id)
    return what


# ------------------------------------------------------- runbook extraction

# conventions/references/node-profile.md § "Box-bound fields" is the authority
# for these; extraction strips exactly what it lists.
BOX_BOUND_REMOVED = ("distro", "distro_version", "arch", "system_facts", "identity", "ssh", "remote_ide")
AUDIT_WRITTEN = ("last_audited", "last_audited_commit", "fingerprints")
TELEMETRY_WRITTEN = ("last_evaluated", "baseline")
BACKUP_WRITTEN = ("automation_rendered_commit", "last_backup", "last_backup_outcome", "last_snapshot",
                  "last_restore_test", "last_restore_test_snapshot")
MAINTENANCE_WRITTEN = ("automation_rendered_commit", "last_maintenance", "last_maintenance_outcome",
                       "verified_auto", "security_baseline")
WRITTEN_EMPTY = {"fingerprints": "{}", "verified_auto": "[]"}
YAML_RESERVED = {"y", "n", "yes", "no", "on", "off", "true", "false", "null", "~"}


def yaml_text(v) -> str:
    """One scalar for a human-read YAML file: plain when unambiguous, quoted otherwise."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, dt.datetime):
        if v.tzinfo is not None and v.utcoffset() == dt.timedelta(0):
            return v.strftime("%Y-%m-%dT%H:%M:%SZ")
        return v.isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    s = str(v)
    if s.lower() not in YAML_RESERVED and re.fullmatch(r"[A-Za-z_/][\w./@+-]*", s):
        return s
    return json.dumps(s, ensure_ascii=False)


def emit_yaml(obj, indent: int = 0) -> str:
    """Block-style YAML for dicts, lists and scalars — readable, and re-parsable."""
    pad, lines = "  " * indent, []
    scalar = lambda x: not isinstance(x, (dict, list))  # noqa: E731
    for k, v in obj.items():
        key = yaml_text(k)
        if isinstance(v, dict) and v:
            lines += [f"{pad}{key}:", emit_yaml(v, indent + 1)]
        elif isinstance(v, list) and v and all(scalar(x) for x in v):
            lines.append(f"{pad}{key}: [{', '.join(yaml_text(x) for x in v)}]")
        elif isinstance(v, list) and v:
            lines.append(f"{pad}{key}:")
            for item in v:
                if isinstance(item, dict) and item:
                    sub = emit_yaml(item, indent + 2).split("\n")
                    lines.append(f"{pad}  - {sub[0].lstrip()}")
                    lines += sub[1:]
                else:
                    lines.append(f"{pad}  - {yaml_text(item) if scalar(item) else json.dumps(item)}")
        else:
            lines.append(f"{pad}{key}: {'{}' if v == {} else '[]' if v == [] else yaml_text(v)}")
    return "\n".join(lines)


def rescope_entry(entry: str, old: str, target: str) -> str:
    """drydocc://[….]<old> → drydocc://[….]<target>; global entries stay. The node is
    the last host label, so only a whole label matches."""
    if entry == f"drydocc://{old}":
        return f"drydocc://{target}"
    if entry.startswith("drydocc://") and entry.endswith(f".{old}"):
        return entry[: -len(old)] + target
    return entry


def portable_profile(profile: dict, old: str, target: str, today) -> dict:
    """A node profile with its box-bound fields stripped: a bootstrap stub for `target`
    that keeps every portable declaration."""
    out = {}
    for k, v in profile.items():
        if k in BOX_BOUND_REMOVED:
            continue
        if k in ("name", "hostname", "tailscale_hostname"):
            v = target
        elif k == "provisioning":
            v = {"source": "running"}
        elif k == "hardening":
            v = {h: "unknown" for h in (v or {})}
        elif k == "last_bootstrapped":
            v = None
        elif k == "created":
            v = today
        elif k == "secrets" and isinstance(v, dict):
            v = dict(v, required_env=[dict(e, keepass_entry=rescope_entry(str(e.get("keepass_entry", "")), old, target))
                                      if e.get("keepass_entry") else dict(e)
                                      for e in v.get("required_env") or []])
        out[k] = v
    out.setdefault("name", target)
    out.setdefault("tailscale_hostname", target)
    out.setdefault("provisioning", {"source": "running"})
    out.setdefault("last_bootstrapped", None)
    return out


def split_frontmatter(text: str) -> tuple:
    if not text.startswith("---\n"):
        raise LabError("no frontmatter block")
    end = text.find("\n---", 4)
    if end < 0:
        raise LabError("unterminated frontmatter block")
    return text[4:end + 1], text[end + 1:]


def planned_doc(text: str) -> str:
    """A use-case document as never applied: status planned, apply bookkeeping cleared."""
    fm, rest = split_frontmatter(text)
    for key, value in (("status", "planned"), ("last_applied", "null"), ("last_applied_commit", "null")):
        try:
            fm = set_yaml_value(fm, key, value)
        except LabError:
            pass
    return "---\n" + fm + rest


def drop_keys(text: str, keys, indent: str = "") -> str:
    """Remove `keys` (and their nested lines) at one indentation level of block YAML."""
    out, skipping = [], False
    for line in text.split("\n"):
        lead = len(line) - len(line.lstrip(" "))
        if skipping and line.strip() and (lead > len(indent) or line.startswith(indent + "- ")):
            continue
        skipping = False
        m = re.match(rf"^{indent}([A-Za-z_][\w-]*):(\s|$)", line)
        if m and m.group(1) in keys:
            skipping = True
            continue
        out.append(line)
    return "\n".join(out)


def written_half(text: str, marker: str, keys) -> str:
    """Re-append a manifest's skill-written half, cleared, under its marker."""
    text = text.rstrip("\n") + "\n"
    if marker not in text:
        text += f"\n# ─── {marker}; do not hand-edit ───\n"
    return text + "\n" + "".join(f"{k}: {WRITTEN_EMPTY.get(k, 'null')}\n" for k in keys)


def portable_audit(text: str) -> str:
    text = drop_keys(drop_keys(text, AUDIT_WRITTEN), TELEMETRY_WRITTEN, indent="  ")
    return written_half(text, "written by /audit-use-case", AUDIT_WRITTEN)


def portable_backup(text: str) -> str:
    return written_half(drop_keys(text, BACKUP_WRITTEN), "written by /backup-use-case / /restore-use-case",
                        BACKUP_WRITTEN)


def portable_maintenance(text: str) -> str:
    """maintenance-manifest.md: the skill-written half describes the lab box — what
    was watched to succeed there, and the security baselines it recorded."""
    return written_half(drop_keys(text, MAINTENANCE_WRITTEN), "written by /node-maintenance",
                        MAINTENANCE_WRITTEN)


def portable_secrets(text: str, old: str, target: str) -> str:
    return re.sub(r"^(keepass_entry:\s*[\"']?)([^\"'\s#]+)",
                  lambda m: m.group(1) + rescope_entry(m.group(2), old, target), text, flags=re.M)


def recovery_order(docs: dict) -> tuple:
    """rebuild-node/references/rebuild-recipes.md § "Recovery order", over extracted docs.

    docs: slug → {status, depends_on, containers}. Returns (order, blocked
    {slug: reason}, not_declarative [slugs], planned [slugs]).
    """
    planned = sorted(s for s, d in docs.items() if d["status"] == "planned")
    active = {s for s, d in docs.items() if d["status"] == "active"}
    not_declarative = sorted(s for s in active if not docs[s]["containers"])
    recoverable = active - set(not_declarative)
    blocked = {}
    changed = True
    while changed:
        changed = False
        for s in sorted(recoverable - set(blocked)):
            for dep in docs[s]["depends_on"]:
                if dep not in recoverable:
                    blocked[s] = f"depends on {dep}, which is not a recoverable use-case on this node"
                elif dep in blocked:
                    blocked[s] = f"depends on {dep}, which is blocked"
                else:
                    continue
                changed = True
                break
    remaining, order = recoverable - set(blocked), []
    while remaining:
        ready = sorted(s for s in remaining if all(d in order for d in docs[s]["depends_on"]))
        if not ready:
            raise LabError(f"use-cases depend on each other in a cycle: {', '.join(sorted(remaining))}")
        order += ready
        remaining -= set(ready)
    return order, blocked, not_declarative, planned


def derive_checks(docs: dict, order: list) -> list:
    """What a replay must show for each recovered use-case — tested on the prototype first."""
    checks = []
    for slug in order:
        d = docs[slug]
        checks.append({"name": f"{slug} is active", "assert": "use_case", "slug": slug, "status": "active"})
        for c in d["containers"]:
            # rendered as ContainerName=<slug>-<container> (podman-quadlets); a name that already
            # carries the slug is seen kept as it is, so accept either
            fmt = "--format '{{.State.Running}}'"
            checks.append({"name": f"container {c} runs", "assert": "command",
                           "run": f"podman container inspect {fmt} {slug}-{c} 2>/dev/null || "
                                  f"podman container inspect {fmt} {c}",
                           "stdout_contains": "true"})
        for port in d["ports"]:
            num, _, proto = str(port).partition("/")
            if proto == "tcp" and num.isdigit():
                checks.append({"name": f"{slug} answers on {num}/tcp", "assert": "command",
                               "run": f"timeout 5 bash -c '</dev/tcp/127.0.0.1/{num}'"})
    return checks


STEP_TIMEOUTS = {"bootstrap": 3600, "apply": 2400, "backup": 3600, "notify": 1800, "maintenance": 3600}


def extract_bundle(root: Path, run_id: str, node: str, target: str, *, arch: str | None, log) -> Path:
    """Export a prototype node as a fork-ready runbook bundle for another box.

    conventions/references/runbook-bundle.md is the contract for what lands
    in it. The prototype's journal, its data and every lab secret stay behind.
    """
    if not re.fullmatch(SLUG_RE, target):
        raise LabError(f"target {target!r} is not a valid drydocc slug")
    run = load_run(root, run_id)
    n = run["nodes"].get(node)
    if n is None:
        raise LabError(f"no node {node} in run {run_id}")
    if not run.get("fork"):
        raise LabError(f"run {run_id} has no fork")
    wt = Path(run["fork"]["worktree"])
    commit = fork_settled(wt, node)
    src = wt / "nodes" / node
    if not (src / "node.yaml").is_file():
        raise LabError(f"nodes/{node}/node.yaml is missing — bootstrap the node first")
    profile = load_yaml(src / "node.yaml") or {}
    if not profile.get("last_bootstrapped"):
        raise LabError(f"{node} has not reached the baseline (last_bootstrapped is unset)")
    layer = resolve_layer(root, n["layer"])
    plat = {"distro": str(layer["distro"]), "version": str(layer["version"]), "arch": arch or layer["arch"]}
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    bundle_id = f"{target}-{stamp}"
    bdir = root / "bundles" / bundle_id
    out = bdir / "nodes" / target
    (out / "journal").mkdir(parents=True)
    (out / "journal" / ".gitkeep").touch()
    (out / "use-cases").mkdir()
    try:
        today = dt.date.today()
        (out / "node.yaml").write_text(emit_yaml(portable_profile(profile, node, target, today)) + "\n")
        docs, secrets_pre, backup_targets, backed_up = {}, [], set(), []
        for e in (profile.get("secrets") or {}).get("required_env") or []:
            secrets_pre.append({"entry": rescope_entry(str(e.get("keepass_entry", "")), node, target),
                                "keys": [e.get("name")], "for": "node", "optional": bool(e.get("optional"))})
        for doc in sorted((src / "use-cases").glob("*.md")):
            slug = doc.stem
            text = doc.read_text()
            fm = parse_yaml_text(split_frontmatter(text)[0]) or {}
            docs[slug] = {"status": fm.get("status"), "depends_on": list(fm.get("depends_on") or []),
                          "containers": list(fm.get("containers") or []), "ports": list(fm.get("ports") or []),
                          "volumes": list(fm.get("volumes") or [])}
            (out / "use-cases" / doc.name).write_text(planned_doc(text))
            sec = src / "use-cases" / f"{slug}.secrets.yaml"
            if sec.exists():
                text = portable_secrets(sec.read_text(), node, target)
                (out / "use-cases" / sec.name).write_text(text)
                m = parse_yaml_text(text) or {}
                secrets_pre.append({"entry": m.get("keepass_entry"), "for": slug, "optional": False,
                                    "keys": [k.get("name") for k in m.get("keys") or []]})
            aud = src / "use-cases" / f"{slug}.audit.yaml"
            if aud.exists():
                (out / "use-cases" / aud.name).write_text(portable_audit(aud.read_text()))
            bak = src / "use-cases" / f"{slug}.backup.yaml"
            if bak.exists():
                text = portable_backup(bak.read_text())
                (out / "use-cases" / bak.name).write_text(text)
                tgt = (parse_yaml_text(text) or {}).get("target") or "default"
                backup_targets.add(tgt)
                backed_up.append((slug, tgt))
        if not docs:
            (out / "use-cases" / ".gitkeep").touch()
        carried = [f for f in ("maintenance.yaml", "notify.yaml") if (src / f).is_file()]
        for f in carried:
            shutil.copyfile(src / f, out / f)
        if "maintenance.yaml" in carried:
            (out / "maintenance.yaml").write_text(portable_maintenance((src / "maintenance.yaml").read_text()))
        order, blocked, not_declarative, planned = recovery_order(docs)
        checks, dropped = [], []
        for c in derive_checks(docs, order):
            ok, detail = check_assertion(root, run_id, dict(c, node=node))
            (checks if ok else dropped).append(c if ok else (c, detail))
        log(f"{len(checks)} checks hold on the prototype" + (f", {len(dropped)} do not and are left out"
                                                              if dropped else ""))
        steps = [{"id": "bootstrap", "kind": "skill",
                  "run": f'/bootstrap-node {target} "{", ".join(profile.get("roles") or []) or "drydocc node"}"',
                  "note": "the box already exists: answer `running` when asked for the source"}]
        steps += [{"id": f"apply-{s}", "kind": "skill", "run": f"/apply-use-case {target} {s}"} for s in order]
        steps += [{"id": f"backup-{s}", "kind": "skill", "run": f"/backup-use-case {target} {s}",
                   "needs": [f"backup-target:{t}"]}
                  for s, t in sorted((b for b in backed_up if b[0] in order), key=lambda b: order.index(b[0]))]
        if "notify.yaml" in carried:
            steps.append({"id": "notify", "kind": "skill", "run": f"/node-notify {target} --setup",
                          "needs": ["notify-channel"]})
        if "maintenance.yaml" in carried:
            steps.append({"id": "maintenance", "kind": "skill", "run": f"/node-maintenance {target} --setup"})
        deltas = [f"Machine: the prototype ran on a generic {layer['arch']} virtual machine ({plat['distro']} "
                  f"{plat['version']}); /bootstrap-node probes the real box's hardware, disks and network afresh."]
        if plat["arch"] != layer["arch"]:
            deltas.append(f"Architecture: prototyped on {layer['arch']}, extracted for {plat['arch']} — every "
                          f"image must publish a {plat['arch']} manifest; /apply-use-case checks the platform "
                          "before it pulls.")
        if (profile.get("hardening") or {}).get("tailscale") != "joined":
            deltas.append("Tailnet: the prototype never joined a tailnet (hardening.tailscale: "
                          f"{(profile.get('hardening') or {}).get('tailscale', 'unknown')}); on the real box "
                          "/bootstrap-node joins it — have its key ready (see Prerequisites).")
        if len(run["nodes"]) > 1:
            deltas.append("Peers: in the lab, nodes found each other by name over a lab network; on real "
                          "hardware they do so over the tailnet.")
        if (src / "backup-targets.yaml").is_file():
            deltas.append("Backup targets: the prototype's node-level targets pointed at lab machines and are "
                          "not exported — declare the real ones in your fork before the backup steps.")
        for f in ("backup-targets.yaml", "maintenance.yaml", "notify.yaml"):
            if (wt / "nodes" / f).is_file():
                deltas.append(f"Fork-wide nodes/{f} of the prototype is not exported — your fork's own applies.")
        deltas += [f"Not applied: {s} — {why}." for s, why in sorted(blocked.items())]
        deltas += [f"Manual: {s} is a capture-only use-case (no containers); its Migration section is the "
                   "worklist." for s in not_declarative]
        deltas += [f"Planned only: {s} was never applied in the prototype; it is carried as planned and this "
                   "runbook does not apply it." for s in planned]
        deltas += [f"Check left out — {c['name']}: it did not hold on the prototype ({detail[:120]})."
                   for c, detail in dropped]
        data = [{"use_case": s, "volume": v} for s in order for v in docs[s]["volumes"]]
        rb = {"schema": 1, "target": target, "platform": plat,
              "source": {"prototype": run_id, "node": node, "layer": layer["key"], "fork_commit": commit[:12],
                         "upstream_ref": overlay_data(run)["lab"]["upstream_ref"], "extracted": now_iso()},
              "prerequisites": {
                  "hardware": f"one {plat['arch']} machine with a fresh {plat['distro']} {plat['version']} "
                              "install, reachable over SSH as root with a password for the first connection",
                  "secrets": [s for s in secrets_pre if s["entry"]],
                  "backup_targets": sorted(backup_targets)},
              "steps": steps, "checks": checks, "deltas": deltas, "data": data, "verification": None}
        write_runbook(out, rb)
    except Exception:
        remove_tree(bdir)
        raise
    return bdir


def write_runbook(out: Path, rb: dict) -> None:
    (out / "runbook.yaml").write_text(
        "# drydocc runbook — generated by scripts/lab/lab.py; schema: the conventions skill's\n"
        "# references/runbook-bundle.md. RUNBOOK.md is generated from this file.\n" + emit_yaml(rb) + "\n")
    (out / "RUNBOOK.md").write_text(render_runbook(rb))


def render_runbook(rb: dict) -> str:
    t, plat, src = rb["target"], rb["platform"], rb["source"]
    v = rb.get("verification") or {}
    if v.get("status") == "replay-verified":
        stamp = (f"**replay-verified** @{v['framework_commit']} on {v['platform']} ({v['at']}, lab run "
                 f"`{v['run']}`)")
    elif v:
        stamp = f"**not verified** — the last replay ended `{v['status']}` ({v['at']}, lab run `{v['run']}`)"
    else:
        stamp = "**not verified** — run `lab.py verify-runbook` on this bundle"
    pre = rb["prerequisites"]
    lines = [f"# Runbook: `{t}`", "",
             f"Generated from `runbook.yaml`; edit that file, not this one. {stamp}.", "",
             f"Prototyped in lab run `{src['prototype']}` as node `{src['node']}` (framework "
             f"{src['upstream_ref']}, fork commit `{src['fork_commit']}`, extracted {src['extracted']}).", "",
             "## Prerequisites", "", f"- **Hardware:** {pre['hardware']}."]
    for s in pre["secrets"]:
        opt = " (optional)" if s.get("optional") else ""
        lines.append(f"- **KeePass entry `{s['entry']}`**{opt} for {s['for']}: "
                     f"{', '.join(f'`{k}`' for k in s['keys'])}. Its URL field is exactly that "
                     "lookup URL (the conventions skill's `references/secrets.md`).")
    for b in pre["backup_targets"]:
        lines.append(f"- **Backup target `{b}`** declared in your fork (`nodes/backup-targets.yaml`).")
    lines += ["", "## Steps", "",
              "Run each from the commissioning client, in this order. Use-cases are applied in their "
              "dependency order (the `rebuild-node` skill's recovery order).", ""]
    for i, s in enumerate(rb["steps"], 1):
        needs = f" Needs: {', '.join(s['needs'])}." if s.get("needs") else ""
        note = f" {s['note'][0].upper()}{s['note'][1:]}." if s.get("note") else ""
        lines += [f"{i}. `{s['id']}`{note}{needs}", "", "   ```", f"   {s['run']}", "   ```", ""]
    lines += ["## Verification", "", "On the node, each of these must hold:", ""]
    for c in rb["checks"]:
        how = f"`{c['run']}`" if c["assert"] == "command" else f"use-case `{c['slug']}` has status `{c['status']}`"
        lines.append(f"- {c['name']} — {how}")
    lines += ["", "## Lab vs. hardware", ""] + [f"- {d}" for d in rb["deltas"]]
    lines += ["", "## Data", ""]
    if rb["data"]:
        lines.append("The prototype's data stays in the lab; these volumes start empty on the real box — "
                     "seed them, or restore them from your own backups:")
        lines += [""] + [f"- `{d['use_case']}`: volume `{d['volume']}`" for d in rb["data"]]
    else:
        lines.append("No volumes.")
    if v.get("skipped"):
        lines += ["", "Not replayed in the lab: " + "; ".join(v["skipped"]) + "."]
    return "\n".join(lines) + "\n"


def resolve_bundle(root: Path, ref: str) -> tuple:
    """(bundle dir, target) for a bundle id, a unique id prefix, or a path."""
    p = Path(ref)
    if not p.is_dir():
        hits = [d for d in sorted((root / "bundles").glob("*")) if d.is_dir() and d.name.startswith(ref)]
        if len(hits) != 1:
            raise LabError(f"bundle {ref!r}: {'no match' if not hits else 'ambiguous'}")
        p = hits[0]
    targets = [d.name for d in (p / "nodes").iterdir() if d.is_dir()] if (p / "nodes").is_dir() else []
    if len(targets) != 1 or not (p / "nodes" / targets[0] / "runbook.yaml").is_file():
        raise LabError(f"{p} is not a runbook bundle")
    return p.resolve(), targets[0]


VERIFY_ANSWERS = dict(BOOTSTRAP_ANSWERS, **{"create-use-case.continue-over-ssh": "yes",
                                            "apply-use-case.continue-over-ssh": "yes",
                                            "apply-use-case.confirm-render": "yes"})


def verify_runbook(root: Path, ref: str, *, repo: Path, fw_ref: str, adapter: str, keep: bool, log) -> dict:
    """Replay a bundle's runbook on a fresh base box and stamp it with the outcome.

    The box is a clone of an empty-OS layer of the bundle's platform, under
    the target's name and presented as `source: running`, exactly what a real
    box is to /bootstrap-node. A headless agent runs each step, the driver
    judges the runbook's checks, and only a pass stamps `replay-verified`.
    """
    bdir, target = resolve_bundle(root, ref)
    rbp = bdir / "nodes" / target / "runbook.yaml"
    rb = load_yaml(rbp)
    plat = rb["platform"]
    steps, skipped = [], []
    for st in rb["steps"]:
        if st.get("needs"):
            skipped.append(f"{st['id']} (needs {', '.join(st['needs'])})")
            continue
        steps.append({"name": st["id"], "agent": st["run"], "node": target,
                      "timeout": STEP_TIMEOUTS.get(st["id"].split("-")[0], 3600)})
    steps += [dict(c, node=target) for c in rb["checks"]]
    steps.append({"name": "the framework checks still pass on the fork", "assert": "checks_pass"})
    sc = {"scenario": "verify", "file": str(rbp), "adapter": adapter, "tailnet": "off",
          "nodes": {target: {"from": "base", "distro": plat["distro"], "version": plat["version"],
                             "arch": plat["arch"]}},
          "answers": VERIFY_ANSWERS,
          "secrets": sorted({k for s in rb["prerequisites"]["secrets"] if s["for"] != "node" for k in s["keys"]}),
          "steps": steps}

    def install(run_id):  # as `promote` would land it in a real fork
        wt = Path(load_run(root, run_id)["fork"]["worktree"])
        shutil.copytree(bdir / "nodes" / target, wt / "nodes" / target)
        git(wt, "add", f"nodes/{target}")
        git(wt, *LAB_GIT_IDENTITY, "commit", "-q", "-m", f"lab: runbook bundle {bdir.name}")
        git(wt, "push", "-q", "origin", "HEAD")

    log(f"replaying {bdir.name} on a fresh {plat['distro']} {plat['version']} {plat['arch']} box named {target}"
        + (f"; not replayed: {', '.join(skipped)}" if skipped else ""))
    report = run_scenario(root, sc, repo=repo, ref=fw_ref, keep=keep, log=log, prepare=install)
    if report["verdict"] != "error":  # an infrastructure error says nothing about the runbook
        rb["verification"] = {
            "status": "replay-verified" if report["verdict"] == "pass" else report["verdict"],
            "framework_commit": report.get("ref_sha", "")[:12],
            "platform": f"{plat['distro']}/{plat['version']}/{plat['arch']}",
            "run": report["run"], "at": now_iso(), "skipped": skipped}
        write_runbook(bdir / "nodes" / target, rb)
    return report


def promote_bundle(root: Path, ref: str, *, repo: Path, base: str, allow_unverified: bool) -> tuple:
    """Copy a bundle into the admin's real fork as nodes/<target>/, on a new branch.

    Never into the working tree the admin is on, never over an existing node,
    never an unverified bundle unless asked. The branch is the admin's to
    review, merge and push.
    """
    bdir, target = resolve_bundle(root, ref)
    rb = load_yaml(bdir / "nodes" / target / "runbook.yaml")
    status = (rb.get("verification") or {}).get("status")
    if status != "replay-verified" and not allow_unverified:
        raise LabError(f"bundle {bdir.name} is not replay-verified ({status or 'never replayed'}) — run "
                       "verify-runbook first, or pass --unverified")
    repo = Path(git(repo, "rev-parse", "--show-toplevel").strip())
    base_sha = git(repo, "rev-parse", "--verify", f"{base}^{{commit}}").strip()
    if git(repo, "cat-file", "-e", f"{base_sha}:nodes/{target}", check=False) is not None:
        raise LabError(f"nodes/{target} already exists at {base} — a promotion never overwrites a node")
    branch = f"promote/{target}"
    if git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False):
        raise LabError(f"branch {branch} already exists — delete it or merge it first")
    wt = repo / ".worktrees" / f"promote-{target}"
    git(repo, "worktree", "add", "-q", "-b", branch, str(wt), base_sha)
    ok = False
    try:
        shutil.copytree(bdir / "nodes" / target, wt / "nodes" / target)
        git(wt, "add", f"nodes/{target}")
        src = rb["source"]
        git(wt, "commit", "-q", "-m", f"nodes/{target}: promote runbook bundle {bdir.name}\n\n"
            f"From lab prototype {src['prototype']}/{src['node']}; verification: {status or 'none'}.")
        before, after = base_sha[:7], git(wt, "rev-parse", "--short", "HEAD").strip()
        entry = journal_path(wt / "nodes" / target / "journal", "lab-promote")
        v = rb.get("verification") or {}
        entry.write_text(journal_entry(
            f"lab promote {bdir.name}", before, after,
            [f"Scaffolded `nodes/{target}/` from runbook bundle `{bdir.name}`, extracted from lab prototype "
             f"`{src['prototype']}` (node `{src['node']}`).",
             "Profile is a bootstrap stub plus the prototype's portable declarations; every use-case is "
             "`planned`; secrets manifests point at this node's own KeePass entries.",
             f"Verification: {v.get('status', 'none')}" + (f" @{v['framework_commit']} on {v['platform']}"
                                                            if v.get("status") == "replay-verified" else "") + "."],
            "Nothing ran on any box: the node does not exist yet. `RUNBOOK.md` in this directory is the way to "
            "build it."))
        git(wt, "add", str(entry.relative_to(wt)))
        git(wt, "commit", "-q", "-m", f"nodes/{target}: journal the promotion")
        ok = True
    finally:
        git(repo, "worktree", "remove", "--force", str(wt), check=False)
        git(repo, "worktree", "prune", check=False)
        if not ok:
            git(repo, "branch", "-q", "-D", branch, check=False)
    return branch, target


def remove_layer(root: Path, ref: str) -> dict:
    layer = resolve_layer(root, ref)
    layers = load_layers(root)
    if layer_children(layers, layer["key"]):
        raise LabError(f"layer {layer['name']} has child layers — remove those first")
    users = layer_in_use(root, layer["key"])
    if users:
        raise LabError(f"layer {layer['name']} is in use by run(s) {', '.join(users)}")
    remove_tree(layer_dir(root, layer["key"]))
    return layer


# ------------------------------------------------------------------ teardown

def destroy_domain(domain: str) -> None:
    if virsh(["dominfo", domain], check=False, capture=True) is None:
        return
    virsh(["destroy", domain], check=False, capture=True)
    if virsh(["undefine", domain, "--nvram"], check=False, capture=True) is None:
        virsh(["undefine", domain], check=False, capture=True)


def tailnet_logout(root: Path, run_id: str, node: str) -> None:
    """Take a lab node off the tailnet now, rather than when its ephemeral key expires it.

    Best effort, one attempt: a box that is already gone is not an error.
    """
    rdir = runs_dir(root) / run_id
    n = load_run(root, run_id)["nodes"][node]
    if (virsh(["domstate", n["domain"]], check=False, capture=True) or "").strip() != "running":
        return
    key = rdir / "home" / ".ssh" / f"doccap-at-{node}"
    who = (["-i", str(key), "-p", str(SSH_FALLBACK_PORT), f"doccap@{node}"] if key.exists()
           else ["-p", str(INITIAL_HOST_PORT), f"root@{node}"])
    subprocess.call(driver_ssh(rdir) + ["-o", "ConnectTimeout=5"] + who
                    + ["sudo -n tailscale logout 2>/dev/null || tailscale logout"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)


def untagged_on_tailnet(root: Path, run_id: str) -> list:
    """Nodes on the tailnet without a tag: joined as the admin's own device, outside the lab fence.

    An ephemeral lab tailnet is safe only because its key tags every node and
    the tailnet's policy fences that tag off (lab-overlay.md § "Ephemeral
    tailnet"). A key made without the tag joins nodes as the admin, beside
    every real device.
    """
    bad = []
    for node in load_run(root, run_id)["nodes"]:
        rc, out = node_shell(root, run_id, node, "tailscale status --json --peers=false 2>/dev/null || true")
        if rc == 0 and '"BackendState": "Running"' in out:
            try:
                tags = (json.loads(out[out.index("{"):]).get("Self") or {}).get("Tags")
            except ValueError:
                tags = None
            if not tags:
                bad.append(node)
    return bad


def destroy_run(root: Path, run_id: str) -> None:
    run = load_run(root, run_id)
    for node, n in run["nodes"].items():
        if run.get("tailnet") == "ephemeral":
            try:
                tailnet_logout(root, run_id, node)
            except (subprocess.TimeoutExpired, LabError):
                pass
        destroy_domain(n["domain"])
    fork = run.get("fork")
    if fork:
        remove_fork(Path(fork["repo"]), Path(fork["worktree"]), fork["branch"], Path(fork["remote"]))
    shutil.rmtree(runs_dir(root) / run_id)
    if not any(runs_dir(root).glob("*/run.json")):
        stop_git_daemon(root)


def list_lab_domains() -> list:
    out = virsh(["list", "--all", "--name"], capture=True) or ""
    return [d for d in out.split() if d.startswith(DOMAIN_PREFIX)]


def gc_plan(root: Path, *, failed_older_than=None, budget=None, include_failed=False) -> dict:
    """What a GC pass would remove. Pure planning; `apply_gc` acts on it."""
    plan = {"runs": [], "layers": [], "orphan_domains": [],
            # a layer dir with no meta.json is a seal or a removal that died midway
            "broken_layers": sorted(p.name for p in (root / "layers").iterdir()
                                    if p.is_dir() and not (p / "meta.json").exists())
            if (root / "layers").exists() else []}
    now = dt.datetime.now(dt.timezone.utc)
    runs = [read_json(p) for p in runs_dir(root).glob("*/run.json")]
    known = {n["domain"] for r in runs for n in r["nodes"].values()}
    plan["orphan_domains"] = [d for d in list_lab_domains() if d not in known]
    for r in runs:
        if r["status"] == "passed":
            plan["runs"].append(r["run"])
        elif r.get("mode") == "prototype" and r["status"] == "active" and r.get("expires") \
                and dt.datetime.fromisoformat(r["expires"]) <= now:
            plan["runs"].append(r["run"])  # a prototype past its TTL: kept, but not forever
        elif r["status"] == "failed" and include_failed:
            age = now - dt.datetime.fromisoformat(r.get("finished") or r["created"])
            if failed_older_than is None or age >= failed_older_than:
                plan["runs"].append(r["run"])
    if budget is not None:
        layers = load_layers(root)
        removing_runs = set(plan["runs"])
        total = allocated_bytes(root / "layers") + sum(
            allocated_bytes(runs_dir(root) / r["run"]) for r in runs if r["run"] not in removing_runs)
        evicted = set()
        # LRU over unpinned leaves; evicting a leaf can turn its parent into one
        while total > budget:
            candidates = [m for k, m in layers.items()
                          if k not in evicted and not m.get("pinned")
                          and not [c for c in layer_children(layers, k) if c not in evicted]
                          and not [u for u in layer_in_use(root, k) if u not in removing_runs]]
            if not candidates:
                break
            victim = min(candidates, key=lambda m: m.get("last_used", ""))
            evicted.add(victim["key"])
            plan["layers"].append(victim["key"])
            total -= allocated_bytes(layer_dir(root, victim["key"]))
    return plan


def apply_gc(root: Path, plan: dict) -> None:
    for d in plan["orphan_domains"]:
        destroy_domain(d)
    for r in plan["runs"]:
        destroy_run(root, r)
    if not any(runs_dir(root).glob("*/run.json")):
        stop_git_daemon(root)  # nothing left for it to serve
    for k in plan["layers"] + plan.get("broken_layers", []):
        remove_tree(layer_dir(root, k))


def remove_tree(d: Path) -> None:
    """Delete a sealed (read-only) tree: make it writable first, dirs before contents."""
    if not d.exists():
        return
    d.chmod(0o700)
    for p in d.rglob("*"):
        if p.is_dir() and not p.is_symlink():
            p.chmod(0o700)
    for p in d.rglob("*"):
        if p.is_file() and not p.is_symlink():
            p.chmod(0o600)
    shutil.rmtree(d)


# ------------------------------------------------------------------- helpers

def run_cmd(cmd: list, *, capture=False, check=True, env=None):
    res = subprocess.run(cmd, text=True, env=env,
                         stdout=subprocess.PIPE if capture else None,
                         stderr=subprocess.PIPE)
    if res.returncode != 0:
        if check:
            raise LabError(f"{' '.join(cmd[:3])} … failed ({res.returncode}): {res.stderr.strip()[-600:]}")
        return None
    return res.stdout if capture else ""


def virsh(args: list, *, check=True, capture=False):
    return run_cmd(["virsh", "-c", LIBVIRT_URI] + args, check=check, capture=capture)


def node_ssh(root: Path, run_id: str, node: str, command: list, *, user="root", port=None) -> int:
    run = load_run(root, run_id)
    n = run["nodes"].get(node)
    if n is None:
        raise LabError(f"no node {node} in run {run_id}")
    rdir = runs_dir(root) / run_id
    key = rdir / "home" / ".ssh" / f"doccap-at-{node}"
    ident = []
    if user != "root" and key.exists():  # past bootstrap: doccap, keyed, on the fallback port
        ident, port = ["-i", str(key)], port or SSH_FALLBACK_PORT
    port = port or n["reach"]["ports"]["22"]
    cmd = driver_ssh(rdir) + ident + ["-p", str(port), f"{user}@{node}"] + command
    return subprocess.call(cmd)


# ----------------------------------------------------------------------- CLI

def cmd_layer_import(root, a):
    with store_lock(root):
        meta = import_layer(root, a)
    print(f"sealed layer {meta['key']} ({meta['name']}): {meta['kind']} {meta['distro']} "
          f"{meta['version']} {meta['arch']}")


def cmd_tree(root, a):
    layers = load_layers(root)

    def show(key, depth):
        m = layers[key]
        size = human(allocated_bytes(layer_dir(root, key)))
        flags = " pinned" if m.get("pinned") else ""
        users = layer_in_use(root, key)
        used = f" in-use:{','.join(users)}" if users else ""
        print(f"{'  ' * depth}{key}  {m['name']}  [{m['kind']} {m['distro']} {m['version']} "
              f"{m['arch']}]  {size}  age {age_of(m['created'])}  last-used {m.get('last_used', '?')}"
              f"{flags}{used}")
        for c in sorted(layer_children(layers, key)):
            show(c, depth + 1)

    roots = [k for k, m in layers.items() if not m.get("parent") or m["parent"] not in layers]
    if not roots:
        print("(no layers)")
    for k in sorted(roots):
        show(k, 0)


def cmd_run_create(root, a):
    with store_lock(root):
        run = create_run(root, a.run, a.mode, tailnet=a.tailnet,
                         repo=Path(a.repo) if a.fork else None, ref=a.ref)
    print(f"run {run['run']}: index {run['index']}, loopback 127.0.{100 + run['index']}.0/24, "
          f"labnet {run['mcast_group']}:{run['mcast_port']}")
    if run.get("fork"):
        f = run["fork"]
        print(f"fork: {f['worktree']} on {f['branch']} from {f['ref']} ({f['ref_sha'][:12]}), "
              f"origin {f['remote_url']}")


def cmd_answer(root, a):
    with store_lock(root):
        run = load_run(root, a.run)
        for kv in a.answers:
            k, sep, v = kv.partition("=")
            if not sep or not re.fullmatch(r"[a-z0-9-]+\.[a-z0-9-]+", k):
                raise LabError(f"answer must be <skill>.<topic>=<value>: {kv!r}")
            run.setdefault("answers", {})[k] = v
        save_run(root, run)
        write_overlay(root, run)
    print(f"run {a.run}: {len(run['answers'])} answer(s)")


def cmd_agent(root, a):
    prompt = Path(a.prompt_file).read_text() if a.prompt_file else a.prompt
    if not prompt:
        raise LabError("give a prompt or --prompt-file")
    env_extra = dict(kv.split("=", 1) for kv in a.env)
    step = run_agent(root, a.run, prompt, adapter=a.adapter, node=a.node,
                     timeout=a.timeout, env_extra=env_extra)
    print(f"step {step['n']}: {step['verdict']} (rc {step['rc']}), transcript {step['transcript']}")
    if step["verdict"] == "blocked":
        print(step["blocked"].rstrip())
    sys.exit({"done": 0, "blocked": 2}.get(step["verdict"], 1))


def log_agents(root, adapter, model, log) -> None:
    for line in agent_banner(root, adapter, model):
        log(line)


def cmd_agents(root, a):
    print(f"{'adapter':10} {'ready':6} {'cost':5} {'model':48} qualified / detail")
    for name in adapter_names():
        s = adapter_status(name)
        q = qualification(root, name, s["model"]) if s["model"] else ""
        print(f"{name + (' *' if name == default_adapter() else ''):10} {'yes' if s['ready'] else 'no':6} "
              f"{s['cost']:5} {s['model'] or '-':48} {q or s['detail']}")
    print("\n* the default (DRYDOCC_LAB_ADAPTER, else claude). Pick per command with --adapter, "
          "the model with --model or DRYDOCC_LAB_MODEL; lab.py qualify measures which models pass.")


def cmd_qualify(root, a):
    log = lambda m: print(f"[lab] {m}", flush=True)  # noqa: E731
    adapter = a.adapter or default_adapter()
    models = [m.strip() for m in a.models.split(",") if m.strip()]
    log_agents(root, adapter, models[0] if models else None, log)
    log(f"qualifying: {', '.join(models)}")
    scenarios = [Path(p).resolve() for p in a.scenario]
    m = qualify(root, adapter=adapter, models=models, scenarios=scenarios, repeat=a.repeat,
                repo=Path(a.repo), ref=a.ref, log=log, resume=a.resume,
                allow_visible_runs=a.allow_visible_runs)
    log(f"matrix: {root / 'results' / m['id'] / 'matrix.md'}")
    print(render_matrix(m, m["id"]))
    sys.exit(1 if m.get("stopped") or m.get("incomplete") else 0)


def cmd_run(root, a):
    log = lambda m: print(f"[lab] {m}", flush=True)  # noqa: E731
    sc_adapter = load_yaml(Path(a.scenario)).get("adapter") if Path(a.scenario).is_file() else None
    log_agents(root, a.adapter or sc_adapter or default_adapter(), a.model, log)
    verdicts = []
    for i in range(a.repeat):
        if a.repeat > 1:
            log(f"repeat {i + 1}/{a.repeat}")
        r = run_scenario(root, Path(a.scenario).resolve(), repo=Path(a.repo), ref=a.ref, keep=a.keep, log=log,
                         adapter=a.adapter, model=a.model)
        verdicts.append(r["verdict"])
        log(f"{r['run']}: {r['verdict']} — report {root / 'results' / r['run'] / 'report.md'}")
    if a.repeat > 1:
        overall = repeat_verdict(verdicts)
        log(f"{verdicts.count('pass')}/{a.repeat} passed — {overall}")
    else:
        overall = verdicts[0]
    sys.exit(0 if overall == "pass" else (2 if overall == "blocked" else 1))


def cmd_admin(root, a):
    try:
        print(admin_action(root, a.run, a.action, a.args, sys.stdin.read() if not sys.stdin.isatty() else ""))
    except LabError as e:
        print(f"lab-admin: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_replace(root, a):
    n = replace_node(root, a.run, a.node, a.layer)
    print(f"{a.node}: replaced by a fresh box, domain {n['domain']}, reach {n['reach']['host']}")


def cmd_pin(root, a):
    with store_lock(root):
        layer = resolve_layer(root, a.layer)
        meta_path = layer_dir(root, layer["key"]) / "meta.json"
        meta = read_json(meta_path)
        meta["pinned"] = not a.off
        meta_path.chmod(0o644)
        write_json(meta_path, meta)
        meta_path.chmod(0o444)
    print(f"layer {layer['key']} ({layer['name']}): {'unpinned' if a.off else 'pinned'}")


def cmd_seal(root, a):
    with store_lock(root):
        meta = seal_node(root, a.run, a.node, kind=a.kind, name=a.name, skill=a.skill, pin=a.pin)
    print(f"sealed layer {meta['key']} ({meta['name']}): {meta['kind']} of {meta['parent']}")


def cmd_clone(root, a):
    with store_lock(root):
        n = clone_node(root, a.run, a.node, a.layer, start=not a.no_start)
    print(f"{a.node}: domain {n['domain']}, reach {n['reach']['host']} "
          f"(22→{n['reach']['ports']['22']}, {SSH_FALLBACK_PORT}→{SSH_FALLBACK_PORT}), "
          f"labnet {n['labnet']['addr']}")


def cmd_runs(root, a):
    rows = sorted((read_json(p) for p in runs_dir(root).glob("*/run.json")), key=lambda r: r["created"])
    if not rows:
        print("(no runs)")
    for r in rows:
        size = human(allocated_bytes(runs_dir(root) / r["run"]))
        print(f"{r['run']}  {r['mode']}  {r['status']}  nodes:{','.join(sorted(r['nodes'])) or '-'}  "
              f"{size}  created {r['created']}")


def cmd_status(root, a):
    with store_lock(root):
        run = load_run(root, a.run)
        run["status"] = a.status
        run["finished"] = now_iso()
        save_run(root, run)
    print(f"run {a.run}: {a.status}")


def cmd_ssh(root, a):
    command = a.command
    sys.exit(node_ssh(root, a.run, a.node, command, user=a.user, port=a.port))


def cmd_env(root, a):
    """Shell exports for a run: shims first on PATH, run identity for adapters."""
    rdir = runs_dir(root) / a.run
    load_run(root, a.run)
    print(f'export DRYDOCC_LAB=1 DRYDOCC_LAB_RUN="{rdir}" PATH="{rdir / "bin"}:$PATH"')


def cmd_destroy(root, a):
    with store_lock(root):
        destroy_run(root, a.run)
    print(f"run {a.run} destroyed")


def cmd_gc(root, a):
    with store_lock(root):
        plan = gc_plan(root, failed_older_than=parse_age(a.older_than) if a.older_than else None,
                       budget=parse_size(a.budget) if a.budget else None,
                       include_failed=a.failed)
        for kind in ("orphan_domains", "runs", "layers", "broken_layers"):
            for item in plan[kind]:
                print(f"{'would remove' if a.dry_run else 'remove'} {kind[:-1]}: {item}")
        if not any(plan.values()):
            print("nothing to collect")
        if not a.dry_run:
            apply_gc(root, plan)


def cmd_up(root, a):
    log = lambda m: print(f"[lab] {m}", flush=True)  # noqa: E731
    defaults = {"from": a.from_, "distro": a.distro, "version": a.version, "arch": a.arch}
    specs = dict(parse_node_spec(s, defaults) for s in a.node)
    log_agents(root, a.adapter, None, log)
    run = proto_up(root, a.proto, specs, repo=Path(a.repo), ref=a.ref, ttl=a.ttl, log=log, adapter=a.adapter)
    print(f"prototype {a.proto}: {', '.join(sorted(run['nodes']))}; fork {run['fork']['worktree']}; "
          f"expires {run['expires']}")
    print(f"work in it: lab.py shell {a.proto} --agent {a.adapter}   (or: lab.py shell {a.proto} for a plain shell;"
          f" lab.py agents lists the others)")


def cmd_shell(root, a):
    run = load_run(root, a.run)
    if not run.get("fork"):
        raise LabError(f"run {a.run} has no fork")
    command = a.command
    env = agent_env(root, a.run, a.node)
    if a.agent:
        script = AGENTS_DIR / f"{a.agent}.sh"
        if not script.is_file():
            raise LabError(f"no agent adapter {a.agent!r} (looked for {script})")
        cmd = ["bash", str(script), "--interactive"] + command
    else:
        cmd = command or [os.environ.get("SHELL") or "/bin/sh"]
    sys.exit(subprocess.call(cmd, cwd=run["fork"]["worktree"], env=env))


def cmd_checkpoint(root, a):
    meta = checkpoint_node(root, a.run, a.node, a.name, log=lambda m: print(f"[lab] {m}", flush=True))
    print(f"{a.node}: restore point {meta['name']} ({meta['key']}), pinned; reset now returns here")


def cmd_reset(root, a):
    what = reset_node(root, a.run, a.node, log=lambda m: print(f"[lab] {m}", flush=True))
    print(f"{a.node}: back at {what}")


def cmd_ttl(root, a):
    with store_lock(root):
        run = load_run(root, a.run)
        if run["mode"] != "prototype":
            raise LabError(f"run {a.run} is not a prototype")
        run["expires"] = expiry(a.ttl)
        save_run(root, run)
    print(f"prototype {a.run}: expires {run['expires']}")


def cmd_ls(root, a):
    """Everything the lab is keeping, and for how long — so intent stays visible."""
    runs = sorted((read_json(p) for p in runs_dir(root).glob("*/run.json")), key=lambda r: r["created"])
    layers = load_layers(root)
    now = dt.datetime.now(dt.timezone.utc)
    print("PROTOTYPES")
    protos = [r for r in runs if r["mode"] == "prototype"]
    for r in protos:
        left = (dt.datetime.fromisoformat(r["expires"]) - now).total_seconds() if r.get("expires") else None
        ttl = "no TTL" if left is None else f"expires in {human_age(left)}" if left > 0 else "EXPIRED (gc takes it)"
        nodes = ", ".join(f"{k} ({layers.get(n['layer'], {}).get('name', n['layer'])})"
                          for k, n in sorted(r["nodes"].items())) or "-"
        print(f"  {r['run']}  age {age_of(r['created'])}  {human(allocated_bytes(runs_dir(root) / r['run']))}"
              f"  {ttl}  nodes: {nodes}")
    if not protos:
        print("  (none)")
    print("RUNS")
    others = [r for r in runs if r["mode"] != "prototype"]
    for r in others:
        print(f"  {r['run']}  {r['status']}  age {age_of(r['created'])}  "
              f"{human(allocated_bytes(runs_dir(root) / r['run']))}")
    if not others:
        print("  (none)")
    print("RESTORE POINTS")
    cps = sorted((m for m in layers.values() if m["kind"] == "checkpoint"), key=lambda m: m["created"])
    for m in cps:
        users = layer_in_use(root, m["key"])
        print(f"  {m['name']}  of {m.get('checkpoint_of', '?').split('@')[0]}  age {age_of(m['created'])}  "
              f"{human(allocated_bytes(layer_dir(root, m['key'])))}{'  pinned' if m.get('pinned') else ''}"
              f"{'  in use by ' + ','.join(users) if users else ''}")
    if not cps:
        print("  (none)")
    print("BUNDLES")
    bundles = sorted(d for d in (root / "bundles").glob("*") if d.is_dir())
    for d in bundles:
        try:
            bdir, target = resolve_bundle(root, str(d))
            rb = load_yaml(bdir / "nodes" / target / "runbook.yaml")
        except LabError as e:
            print(f"  {d.name}  (unreadable: {e})")
            continue
        v = rb.get("verification") or {}
        state = (f"replay-verified @{v['framework_commit']} on {v['platform']}" if v.get("status") == "replay-verified"
                 else f"last replay {v['status']}" if v else "not verified")
        print(f"  {d.name}  → {target}  from {rb['source']['prototype']}/{rb['source']['node']}  {state}")
    if not bundles:
        print("  (none)")


def cmd_extract(root, a):
    bdir = extract_bundle(root, a.run, a.node, a.target, arch=a.arch, log=lambda m: print(f"[lab] {m}", flush=True))
    print(f"bundle {bdir.name}: {bdir / 'nodes' / a.target}")
    print(f"next: lab.py verify-runbook {bdir.name}")


def cmd_verify_runbook(root, a):
    log = lambda m: print(f"[lab] {m}", flush=True)  # noqa: E731
    log_agents(root, a.adapter, None, log)
    r = verify_runbook(root, a.bundle, repo=Path(a.repo), fw_ref=a.ref, adapter=a.adapter, keep=a.keep, log=log)
    log(f"{r['run']}: {r['verdict']} — report {root / 'results' / r['run'] / 'report.md'}")
    sys.exit(0 if r["verdict"] == "pass" else (2 if r["verdict"] == "blocked" else 1))


def cmd_promote(root, a):
    branch, target = promote_bundle(root, a.bundle, repo=Path(a.repo), base=a.ref, allow_unverified=a.unverified)
    print(f"nodes/{target}/ committed on branch {branch} — review it, merge it, push it")


def cmd_layer_rm(root, a):
    with store_lock(root):
        layer = remove_layer(root, a.layer)
    print(f"removed layer {layer['key']} ({layer['name']})")


def cmd_fork_agents_md(root, a):
    path = Path(a.file)
    text = path.read_text(encoding="utf-8")
    cut = fork_agents_md(text)
    if cut != text:
        path.write_text(cut, encoding="utf-8")
        print(f"dropped the {text.split(chr(10)).index(FORK_GUIDE_HEADING)}-line preamble "
              "above its fork agent guide heading")


def build_parser():
    p = argparse.ArgumentParser(prog="lab.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    li = sub.add_parser("layer-import", help="seal a shut-off VM disk as a store layer")
    li.add_argument("--name"); li.add_argument("--disk", required=True)
    li.add_argument("--domain-xml", required=True); li.add_argument("--nvram")
    li.add_argument("--kind", choices=["base", "commissioned", "checkpoint"], required=True)
    li.add_argument("--parent"); li.add_argument("--distro", required=True)
    li.add_argument("--version", required=True); li.add_argument("--arch", required=True)
    li.add_argument("--iso-sha256"); li.add_argument("--skills-digest"); li.add_argument("--commit")
    li.add_argument("--root-password-file"); li.add_argument("--pin", action="store_true")
    li.set_defaults(fn=cmd_layer_import)

    sub.add_parser("tree", help="show the layer lineage").set_defaults(fn=cmd_tree)

    fa = sub.add_parser("fork-agents-md", help="cut an AGENTS.md file in place to what a fork receives "
                                               "(the release build uses this too)")
    fa.add_argument("file")
    fa.set_defaults(fn=cmd_fork_agents_md, store=False)

    rc = sub.add_parser("run-create", help="allocate an isolated run")
    rc.add_argument("run"); rc.add_argument("--mode", choices=["test", "prototype"], default="test")
    rc.add_argument("--fork", action="store_true", help="also cut a lab fork (worktree + lab remote)")
    rc.add_argument("--repo", default=".", help="repository to fork (default: the current one)")
    rc.add_argument("--ref", default="HEAD", help="ref under test (default: HEAD)")
    rc.add_argument("--tailnet", choices=["off", "ephemeral"], default="off")
    rc.set_defaults(fn=cmd_run_create)

    an = sub.add_parser("answer", help="answer admin decisions for a run (<skill>.<topic>=<value>)")
    an.add_argument("run"); an.add_argument("answers", nargs="+")
    an.set_defaults(fn=cmd_answer)

    ag = sub.add_parser("agent", help="run one headless agent step in a run's fork")
    ag.add_argument("run"); ag.add_argument("prompt", nargs="?")
    ag.add_argument("--prompt-file"); ag.add_argument("--adapter", default=default_adapter())
    ag.add_argument("--node", help="node this step targets (its root password goes in the env)")
    ag.add_argument("--timeout", type=int, default=3600, help="seconds")
    ag.add_argument("--env", action="append", default=[], help="extra KEY=VALUE for the agent")
    ag.set_defaults(fn=cmd_agent)

    ru = sub.add_parser("run", help="run a scenario in a fresh lab fork and judge it")
    ru.add_argument("scenario"); ru.add_argument("--repo", default=".")
    ru.add_argument("--ref", help="override the scenario's ref")
    ru.add_argument("--repeat", type=int, default=1)
    ru.add_argument("--keep", action="store_true", help="keep a passing run instead of collecting it")
    ru.add_argument("--adapter", help="agent adapter (scripts/lab/agents/<name>.sh); overrides the scenario's "
                    "(default: DRYDOCC_LAB_ADAPTER, else claude)")
    ru.add_argument("--model", help="model for every agent step, in the adapter's naming "
                    "(default: DRYDOCC_LAB_MODEL, else the adapter's)")
    ru.set_defaults(fn=cmd_run)

    sub.add_parser("agents", help="the agent adapters: ready or not, cost, model, qualification"
                   ).set_defaults(fn=cmd_agents)

    qu = sub.add_parser("qualify", help="run scenarios on each model and keep the pass-rate matrix")
    qu.add_argument("scenario", nargs="+"); qu.add_argument("--models", required=True,
                                                            help="comma-separated, one pinned model per run")
    qu.add_argument("--adapter", help="default: DRYDOCC_LAB_ADAPTER, else claude")
    qu.add_argument("--repeat", type=int, default=2, help="judged runs per model × scenario (default 2)")
    qu.add_argument("--repo", default="."); qu.add_argument("--ref", help="override the scenarios' ref")
    qu.add_argument("--resume", help="a stopped qualification's id (qualify-…): run only what it lacks")
    qu.add_argument("--allow-visible-runs", action="store_true",
                    help="start although other runs' forks exist (their work is readable to the agent)")
    qu.set_defaults(fn=cmd_qualify)

    ad = sub.add_parser("admin", help="perform an admin's out-of-band step on a lab host")
    ad.add_argument("run"); ad.add_argument("action"); ad.add_argument("args", nargs="*")
    ad.set_defaults(fn=cmd_admin)

    rp = sub.add_parser("replace-node", help="destroy a node's box and start a fresh one in its place")
    rp.add_argument("run"); rp.add_argument("node"); rp.add_argument("layer")
    rp.set_defaults(fn=cmd_replace)

    pi = sub.add_parser("pin", help="protect a layer from budget GC (--off to release it)")
    pi.add_argument("layer"); pi.add_argument("--off", action="store_true")
    pi.set_defaults(fn=cmd_pin)

    se = sub.add_parser("seal", help="shut a node down and keep it as a new layer")
    se.add_argument("run"); se.add_argument("node")
    se.add_argument("--kind", choices=["commissioned", "checkpoint"], required=True)
    se.add_argument("--name"); se.add_argument("--skill", help="skill that built it (for the cache key)")
    se.add_argument("--pin", action="store_true")
    se.set_defaults(fn=cmd_seal)

    cl = sub.add_parser("clone", help="clone a layer into a run as a resealed node")
    cl.add_argument("run"); cl.add_argument("node"); cl.add_argument("layer")
    cl.add_argument("--no-start", action="store_true")
    cl.set_defaults(fn=cmd_clone)

    sub.add_parser("runs", help="list runs").set_defaults(fn=cmd_runs)

    st = sub.add_parser("status", help="record a run verdict")
    st.add_argument("run"); st.add_argument("status", choices=["active", "passed", "failed", "kept"])
    st.set_defaults(fn=cmd_status)

    sh = sub.add_parser("ssh", help="run a command on a run's node")
    sh.add_argument("run"); sh.add_argument("node")
    sh.add_argument("--user", default="root"); sh.add_argument("--port", type=int)
    sh.add_argument("command", nargs="*", help="after --: the command to run")
    sh.set_defaults(fn=cmd_ssh)

    en = sub.add_parser("env", help="print shell exports that put a run's shims on PATH")
    en.add_argument("run"); en.set_defaults(fn=cmd_env)

    de = sub.add_parser("destroy", help="destroy a run: domains, overlays, its state")
    de.add_argument("run"); de.set_defaults(fn=cmd_destroy)

    up = sub.add_parser("up", help="start a prototype (a kept lab fork) or add nodes to one")
    up.add_argument("proto")
    up.add_argument("--node", action="append", required=True,
                    help="NAME, or NAME=base|commissioned|<layer> (repeatable)")
    up.add_argument("--from", dest="from_", default="commissioned", help="default source for --node NAME")
    up.add_argument("--distro"); up.add_argument("--version"); up.add_argument("--arch")
    up.add_argument("--repo", default="."); up.add_argument("--ref", default="HEAD")
    up.add_argument("--ttl", help=f"collected by gc this long from now (default {PROTOTYPE_TTL})")
    up.add_argument("--adapter", default=default_adapter(),
                    help="agent adapter that builds a missing commissioned layer")
    up.set_defaults(fn=cmd_up)

    sl = sub.add_parser("shell", help="work inside a run: its fork, shims and keys (--agent to start one)")
    sl.add_argument("run"); sl.add_argument("--node", help="node whose root password goes in the env")
    sl.add_argument("--agent", help="start this adapter's agent interactively, e.g. claude")
    sl.add_argument("command", nargs="*", help="after --: the command to run (default: $SHELL)")
    sl.set_defaults(fn=cmd_shell)

    cp = sub.add_parser("checkpoint", help="keep a prototype node's state as a named, pinned restore point")
    cp.add_argument("run"); cp.add_argument("node"); cp.add_argument("--name", required=True)
    cp.set_defaults(fn=cmd_checkpoint)

    rs = sub.add_parser("reset", help="discard a prototype node's work since its last restore point")
    rs.add_argument("run"); rs.add_argument("node")
    rs.set_defaults(fn=cmd_reset)

    tt = sub.add_parser("ttl", help="re-arm a prototype's TTL from now")
    tt.add_argument("run"); tt.add_argument("ttl")
    tt.set_defaults(fn=cmd_ttl)

    sub.add_parser("ls", help="what the lab keeps: prototypes, runs, restore points, bundles").set_defaults(fn=cmd_ls)

    ex = sub.add_parser("extract", help="export a prototype node as a runbook bundle for another box")
    ex.add_argument("run"); ex.add_argument("node"); ex.add_argument("--as", dest="target", required=True)
    ex.add_argument("--arch", help="the real box's architecture, when it differs from the prototype's")
    ex.set_defaults(fn=cmd_extract)

    vr = sub.add_parser("verify-runbook", help="replay a bundle on a fresh base box and stamp it")
    vr.add_argument("bundle"); vr.add_argument("--repo", default=".")
    vr.add_argument("--ref", default="HEAD", help="framework ref to replay with (default: HEAD)")
    vr.add_argument("--adapter", default=default_adapter())
    vr.add_argument("--keep", action="store_true", help="keep a passing replay instead of collecting it")
    vr.set_defaults(fn=cmd_verify_runbook)

    pr = sub.add_parser("promote", help="copy a verified bundle into the real fork, on a new branch")
    pr.add_argument("bundle"); pr.add_argument("--repo", default=".")
    pr.add_argument("--ref", default="HEAD", help="branch point (default: HEAD)")
    pr.add_argument("--unverified", action="store_true", help="promote without a replay-verified stamp")
    pr.set_defaults(fn=cmd_promote)

    lr = sub.add_parser("layer-rm", help="remove a layer nothing depends on (e.g. an unwanted restore point)")
    lr.add_argument("layer"); lr.set_defaults(fn=cmd_layer_rm)

    gc = sub.add_parser("gc", help="collect passed runs, expired prototypes, orphan domains, and (with a budget) layers")
    gc.add_argument("--failed", action="store_true", help="also collect failed runs")
    gc.add_argument("--older-than", help="with --failed: only runs finished this long ago (e.g. 7d)")
    gc.add_argument("--budget", help="evict unpinned leaf layers, LRU, until the store fits (e.g. 200G)")
    gc.add_argument("--dry-run", action="store_true")
    gc.set_defaults(fn=cmd_gc)
    return p


PASSTHROUGH = ("shell", "ssh")  # verbs whose command after `--` belongs to someone else


def parse_args(argv):
    """Parse a command line; for `shell` and `ssh`, everything after the first `--`
    is the command to run, untouched — argparse alone handles `--` differently
    across Python versions."""
    argv = list(sys.argv[1:] if argv is None else argv)
    tail = None
    if argv[:1] and argv[0] in PASSTHROUGH and "--" in argv:
        i = argv.index("--")
        argv, tail = argv[:i], argv[i + 1:]
    args = build_parser().parse_args(argv)
    if tail is not None:
        args.command = tail
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    if not getattr(args, "store", True):  # a file transform: no lab store involved
        try:
            args.fn(None, args)
        except LabError as e:
            print(f"lab: {e}", file=sys.stderr)
            return 1
        return 0
    root = store_root()
    (root / "layers").mkdir(parents=True, exist_ok=True)
    runs_dir(root).mkdir(parents=True, exist_ok=True)
    try:
        args.fn(root, args)
    except LabError as e:
        print(f"lab: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
