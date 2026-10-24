"""Lingering long-running node steps over SSH.

The scriptable form of `node-exec`'s ``NODE_RUN_LINGER`` contract
(``references/lingering.md``): launch a step detached on the node — in a
named tmux session, or via nohup + a PID file when tmux is absent — then poll
until the step's ``__DONE_<rc>__`` sentinel appears, and clean up. The launch
is idempotent: a re-run re-attaches to an in-flight job instead of relaunching
it.

Every command runs through the connection block `node-context` resolved,
built from ``node.yaml`` by ``Connection.from_node_yaml``. The functions
here build and issue the SSH invocations; they never print a secret, and the
session/log names derive from the slug + step label only.

The remote payloads are built as ONE shell string per invocation, exactly as
the recipe in ``references/lingering.md`` writes them. That is not cosmetic:
``tmux new -d -s <name>`` takes a single ``shell-command`` argument. Passing
the command and its redirections as separate argv entries makes tmux ≥ 3.0 run
the first argument directly with no shell — the redirections are never applied,
nothing is logged, and the sentinel never appears. The whole job body is one
single-quoted string on the remote side.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

SENTINEL = re.compile(r"^__DONE_(\d+)__$")


class LingerError(Exception):
    """The SSH transport failed, or the connection block is incomplete."""


@dataclass(frozen=True)
class Connection:
    host: str
    user: str = "doccap"
    port: int | None = None
    key: str | None = None

    def ssh_base(self) -> list[str]:
        if not self.host:
            raise LingerError("connection block has no host")
        cmd = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes"]
        if self.key:
            cmd += ["-i", self.key]
        if self.port:
            cmd += ["-p", str(self.port)]
        cmd.append(f"{self.user}@{self.host}")
        return cmd

    @classmethod
    def from_node_yaml(cls, data: dict) -> "Connection":
        """Build from a node.yaml connection block (tailscale_hostname,
        doccap_user, ssh_fallback_port, ssh.key_name -> ~/.ssh/<key_name>)."""
        import os

        host = data.get("tailscale_hostname")
        if not host:
            raise LingerError("node.yaml has no tailscale_hostname")
        key_name = (data.get("ssh") or {}).get("key_name")
        key = os.path.expanduser(f"~/.ssh/{key_name}") if key_name else None
        port = data.get("ssh_fallback_port")
        return cls(
            host=str(host),
            user=str(data.get("doccap_user") or "doccap"),
            port=int(port) if port else None,
            key=key,
        )


def session_names(slug: str, step: str) -> dict[str, str]:
    """The tmux session and log names for a lingering step."""
    return {"session": f"dd-{slug}-{step}", "log": f"/tmp/dd-{slug}-{step}.log"}


def pid_file(slug: str, step: str) -> str:
    """The nohup fallback's PID file."""
    return f"/tmp/dd-{slug}-{step}.pid"


def shquote(text: str) -> str:
    """POSIX single-quote a string for embedding in a remote shell command."""
    return "'" + text.replace("'", "'\\''") + "'"


def _run(conn: Connection, remote: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        conn.ssh_base() + [remote],
        capture_output=True,
        text=True,
    )


def _job_body(command: str, log: str) -> str:
    """The one shell string the detached job runs: the command, its output
    redirected to the log, then the exit-code sentinel appended to the log.

    Built as a single string because ``tmux new -d -s <name>`` executes its
    argument as one ``shell-command`` — separate argv entries are not run
    through a shell (tmux ≥ 3.0 runs the first directly), so the redirections
    and the sentinel would never happen. The command is grouped in ``{ … }``
    so a compound command (``a; b``) is logged whole, not just its last part;
    the newline before ``}`` keeps a command that ends in a comment intact.
    """
    return f'{{ {command}\n}} >>"{log}" 2>&1; echo __DONE_$?__ >> "{log}"'


def backend_from_facts(node_yaml: dict) -> str | None:
    """The backend ``system_facts.capabilities.tmux`` already records, if any.

    ``present`` → ``tmux``, ``absent`` → ``nohup``; an unrecorded fact gives
    ``None`` and the caller probes (``detect_backend``).
    """
    caps = ((node_yaml or {}).get("system_facts") or {}).get("capabilities") or {}
    return {"present": "tmux", "absent": "nohup"}.get(str(caps.get("tmux") or ""))


def detect_backend(conn: Connection) -> str:
    """Probe the node for the detached-job backend: ``tmux`` or ``nohup``.

    `bootstrap-node` / `onboard-node` install tmux as part of the baseline
    stack, so a drydocc node normally has it; an older or onboarded box may
    not. The probe is a single ``command -v tmux`` over SSH.
    """
    proc = _run(conn, "command -v tmux >/dev/null 2>&1 && echo tmux || echo nohup")
    if proc.returncode != 0:
        raise LingerError(f"ssh failed (rc={proc.returncode}): {proc.stderr.strip()}")
    out = proc.stdout.strip().splitlines()
    backend = out[-1].strip() if out else "nohup"
    if backend not in ("tmux", "nohup"):
        backend = "nohup"
    return backend


def launch(conn: Connection, slug: str, step: str, command: str, backend: str | None = None) -> dict:
    """Launch the step detached on the node; re-attach if already running.

    Returns ``{"status": "launched"|"attached", "backend": "tmux"|"nohup",
    "session"|"pid_file": ..., "log": ...}``. When ``backend`` is None the
    node is probed once (``detect_backend``); pass it explicitly to reuse a
    probe across several steps of one run.
    """
    if backend is None:
        backend = detect_backend(conn)
    if backend == "tmux":
        return _launch_tmux(conn, slug, step, command)
    return _launch_nohup(conn, slug, step, command)


def _launch_tmux(conn: Connection, slug: str, step: str, command: str) -> dict:
    names = session_names(slug, step)
    s, log = names["session"], names["log"]
    # ONE shell string for tmux's shell-command argument (see _job_body).
    job = _job_body(command, log)
    remote = (
        f"S={shquote(s)}; L={shquote(log)}; "
        f"if tmux has-session -t \"$S\" 2>/dev/null; then "
        f"echo \"ATTACH: $S already running\"; "
        f"else "
        f": >\"$L\"; tmux new -d -s \"$S\" {shquote(job)}; "
        f"echo \"LAUNCHED: $S\"; "
        f"fi"
    )
    proc = _run(conn, remote)
    if proc.returncode != 0:
        raise LingerError(f"ssh failed (rc={proc.returncode}): {proc.stderr.strip()}")
    out = proc.stdout.strip()
    status = "attached" if out.startswith("ATTACH") else "launched"
    return {"status": status, "backend": "tmux", "session": s, "log": log}


def _launch_nohup(conn: Connection, slug: str, step: str, command: str) -> dict:
    names = session_names(slug, step)
    log = names["log"]
    pid = pid_file(slug, step)
    # nohup sh -c takes ONE command string; the sentinel is part of it.
    job = _job_body(command, log)
    remote = (
        f"L={shquote(log)}; P={shquote(pid)}; "
        f"if [ -f \"$P\" ] && kill -0 \"$(cat \"$P\")\" 2>/dev/null; then "
        f"echo \"ATTACH: pid $(cat \"$P\") still running\"; "
        f"else "
        f"nohup sh -c {shquote(job)} >\"$L\" 2>&1 & "
        f"echo $! > \"$P\"; echo \"LAUNCHED: pid $!\"; "
        f"fi"
    )
    proc = _run(conn, remote)
    if proc.returncode != 0:
        raise LingerError(f"ssh failed (rc={proc.returncode}): {proc.stderr.strip()}")
    out = proc.stdout.strip()
    status = "attached" if out.startswith("ATTACH") else "launched"
    return {"status": status, "backend": "nohup", "log": log, "pid_file": pid}


def poll(conn: Connection, slug: str, step: str, backend: str | None = None) -> dict:
    """Poll once. Returns ``{"status": "running", "tail": [...]}`` or
    ``{"status": "done", "rc": <int>}`` / ``{"status": "killed", "tail": [...]}``.

    ``done`` carries the real exit code from the ``__DONE_<rc>__`` sentinel;
    ``killed`` means the job vanished with no sentinel (OOM, reboot,
    ``tmux kill-server``) and is never a success. The backend is probed when
    not given; pass it explicitly to reuse a probe across several polls.
    """
    if backend is None:
        backend = detect_backend(conn)
    if backend == "tmux":
        return _poll_tmux(conn, slug, step)
    return _poll_nohup(conn, slug, step)


def _poll_tmux(conn: Connection, slug: str, step: str) -> dict:
    names = session_names(slug, step)
    s, log = names["session"], names["log"]
    remote = (
        f"S={shquote(s)}; L={shquote(log)}; "
        f"if tmux has-session -t \"$S\" 2>/dev/null; then "
        f"echo RUNNING; tail -n 3 \"$L\" 2>/dev/null; "
        f"else "
        f"tail -n 1 \"$L\" 2>/dev/null; "
        f"fi; exit 0"
    )
    proc = _run(conn, remote)
    if proc.returncode != 0:
        raise LingerError(f"ssh failed (rc={proc.returncode}): {proc.stderr.strip()}")
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    if lines and lines[0].strip() == "RUNNING":
        return {"status": "running", "tail": lines[1:], "session": s, "log": log}
    last = lines[-1].strip() if lines else ""
    m = SENTINEL.match(last)
    if m:
        return {"status": "done", "rc": int(m.group(1)), "session": s, "log": log}
    return {"status": "killed", "tail": lines, "session": s, "log": log}


def _poll_nohup(conn: Connection, slug: str, step: str) -> dict:
    names = session_names(slug, step)
    log = names["log"]
    pid = pid_file(slug, step)
    remote = (
        f"L={shquote(log)}; P={shquote(pid)}; "
        f"if [ -f \"$P\" ] && kill -0 \"$(cat \"$P\")\" 2>/dev/null; then "
        f"echo RUNNING; tail -n 3 \"$L\" 2>/dev/null; "
        f"else "
        f"tail -n 1 \"$L\" 2>/dev/null; "
        f"fi; exit 0"
    )
    proc = _run(conn, remote)
    if proc.returncode != 0:
        raise LingerError(f"ssh failed (rc={proc.returncode}): {proc.stderr.strip()}")
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    if lines and lines[0].strip() == "RUNNING":
        return {"status": "running", "tail": lines[1:], "log": log}
    last = lines[-1].strip() if lines else ""
    m = SENTINEL.match(last)
    if m:
        return {"status": "done", "rc": int(m.group(1)), "log": log}
    return {"status": "killed", "tail": lines, "log": log}


def cleanup(conn: Connection, slug: str, step: str) -> None:
    """Remove the step's log. Leave it in place after a partial outcome."""
    names = session_names(slug, step)
    proc = _run(conn, f"rm -f {shquote(names['log'])}")
    if proc.returncode != 0:
        raise LingerError(f"ssh failed (rc={proc.returncode}): {proc.stderr.strip()}")
