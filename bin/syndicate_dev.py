"""`syndicate dev --checkout PATH`: run an unmerged checkout in isolation.

The checkout is copied (tracked plus untracked, not ignored, files; never .git
or bytecode) into a temporary snapshot, so editing the checkout cannot change
a run already in progress. The snapshot's launcher runs with Python -B in an
environment with a disposable HOME, XDG directories, TMPDIR and tmux server,
a namespaced tmux session, and disposable provider config homes unless
--share-provider-config is given. <devroot>/bin leads PATH with a `syndicate`
that runs the snapshot, so a bare `syndicate` in the run never reaches the
installed release. All Syndicate state (registry, portfolio,
limits monitor and its lock, worktree registry, user config) derives from HOME
and the XDG directories, so it lands in the disposable home. The launched code
refuses to start if any of it would resolve to production state.
"""
from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import syndicate_identity as identity

REQUIRED = ("bin/syndicate", "bin/syndicate_runtime.py", "roles/orchestrator.md",
            "portfolio/chief-of-staff.md")
MONITOR_STOP_SECONDS = 5


class DevError(Exception):
    pass


def validate_checkout(path: Path) -> Path:
    checkout = path.expanduser().resolve()
    if not checkout.is_dir():
        raise DevError(f"{path} is not a directory")
    missing = [name for name in REQUIRED if not (checkout / name).is_file()]
    if missing:
        raise DevError(f"{checkout} is not a Syndicate checkout (missing {', '.join(missing)})")
    top = identity._git(checkout, "rev-parse", "--show-toplevel")
    if not top or Path(top).resolve() != checkout:
        raise DevError(f"{checkout} is not the top of a Git checkout")
    return checkout


def snapshot_files(checkout: Path) -> list[str]:
    """Tracked and untracked-but-not-ignored paths that exist; no .git or bytecode."""
    result = subprocess.run(["git", "--no-optional-locks", "-C", str(checkout), "ls-files", "-z",
                             "--cached", "--others", "--exclude-standard"],
                            capture_output=True, env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))
    if result.returncode:
        raise DevError(f"git ls-files failed in {checkout}: {result.stderr.decode(errors='replace').strip()}")
    names = []
    for raw in sorted(set(result.stdout.split(b"\0"))):
        name = os.fsdecode(raw)
        parts = Path(name).parts
        if (not name or ".git" in parts or "__pycache__" in parts or name.endswith((".pyc", ".pyo"))
                or any(part in ("", ".", "..") for part in parts)):
            continue
        if os.path.lexists(checkout / name):
            names.append(name)
    return names


def stage_snapshot(checkout: Path, destination: Path) -> list[str]:
    """Copy the checkout's files into destination; files are made read-only."""
    names = snapshot_files(checkout)
    destination.mkdir(parents=True)
    for name in names:
        source, target = checkout / name, destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            os.symlink(os.readlink(source), target)
        elif source.is_file():
            shutil.copy2(source, target)
            target.chmod(target.stat().st_mode & ~0o222)
    if not (destination / "bin" / "syndicate").is_file():
        raise DevError("the snapshot has no bin/syndicate; is it tracked or untracked-not-ignored?")
    return names


def write_wrapper(devroot: Path, launcher: Path) -> Path:
    """<devroot>/bin/syndicate: exec the snapshot's pinned prefix, so a bare
    `syndicate` inside the dev run (PATH puts this first) never reaches the
    production release or checkout."""
    target = devroot / "bin" / "syndicate"
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_text("#!/bin/sh\n# syndicate dev: run the staged snapshot, never the installed release.\n"
                      "PYTHONDONTWRITEBYTECODE=1\nexport PYTHONDONTWRITEBYTECODE\n"
                      f"exec {identity.pinned_prefix(launcher)} \"$@\"\n")
    target.chmod(0o555)
    return target


def dev_environment(base: dict, home: Path, devroot: Path, state: dict, checkout: Path,
                    session: str, share_providers: bool) -> dict:
    production = Path(base.get("HOME") or identity.production_home())
    env = {k: v for k, v in base.items()
           if not k.startswith("SYNDICATE_") and k not in ("TMUX", "TMUX_PANE")}
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_DATA_HOME=str(home / ".local" / "share"),
        XDG_CACHE_HOME=str(home / ".cache"),
        XDG_STATE_HOME=str(home / ".local" / "state"),
        TMPDIR=str(devroot / "tmp"),
        TMUX_TMPDIR=str(devroot / "tmux"),
        PATH=os.pathsep.join([str(devroot / "bin"), *([base["PATH"]] if base.get("PATH") else [])]),
        PYTHONDONTWRITEBYTECODE="1",
        **{identity.DEV_ENV: "1",
           identity.DEV_CHECKOUT_ENV: str(checkout),
           identity.DEV_COMMIT_ENV: state.get("commit") or "",
           identity.DEV_BRANCH_ENV: state.get("branch") or "",
           identity.DEV_DIRTY_ENV: "1" if state.get("dirty") else "0",
           identity.DEV_PRODUCTION_HOME_ENV: str(production),
           identity.DEV_TMUX_SESSION_ENV: session},
    )
    if share_providers:
        env["CODEX_HOME"] = base.get("CODEX_HOME") or str(production / ".codex")
        env["CLAUDE_CONFIG_DIR"] = base.get("CLAUDE_CONFIG_DIR") or str(production / ".claude")
    else:
        env["CODEX_HOME"] = str(home / ".codex")
        env["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
    return env


def _tmux_alive(env: dict, session: str) -> bool:
    if not shutil.which("tmux"):
        return False
    try:
        result = subprocess.run(["tmux", "has-session", "-t", session], env=env,
                                capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _stop_monitor(home: Path) -> bool:
    """Ask a dev limits monitor to stop; True once its lock is free (or it never ran)."""
    state = home / ".syndicate" / "limits"
    lock_path = state / "monitor.lock"
    if not lock_path.exists():
        return True
    (state / "stop").touch()
    deadline = time.monotonic() + MONITOR_STOP_SECONDS
    while True:
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def run(checkout_arg: str, args: list[str], *, home_arg: str | None = None, keep: bool = False,
        share_providers: bool = False) -> int:
    checkout = validate_checkout(Path(checkout_arg))
    state = identity.checkout_state(checkout)
    dirty = {True: "dirty", False: "clean", None: "unknown"}[state["dirty"]]
    print(f"syndicate dev: checkout {checkout}, branch {state['branch'] or '(detached)'}, "
          f"commit {state['commit'] or 'unknown'}, {dirty}", file=sys.stderr)
    production = identity.production_home()
    devroot = Path(tempfile.mkdtemp(prefix="syndicate-dev-"))
    try:
        home = Path(home_arg).expanduser().resolve() if home_arg else devroot / "home"
        if home in (production.resolve(), Path.home().resolve()):
            raise DevError(f"--home {home} is the production home; choose a disposable directory")
        stage_snapshot(checkout, devroot / "checkout")
        write_wrapper(devroot, devroot / "checkout" / "bin" / "syndicate")
        for path in (home, devroot / "tmp", devroot / "tmux", home / "work"):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        session = f"syndicate-dev-{uuid.uuid4().hex[:8]}"
        env = dev_environment(dict(os.environ), home, devroot, state, checkout, session, share_providers)
    except BaseException:
        shutil.rmtree(devroot, ignore_errors=True)
        raise
    launcher = devroot / "checkout" / "bin" / "syndicate"
    print(f"syndicate dev: snapshot {devroot / 'checkout'}, home {home}, tmux session {session}",
          file=sys.stderr)
    try:
        result = subprocess.run([*identity.python_argv(launcher), *args], env=env, cwd=home / "work")
        code = result.returncode
    except KeyboardInterrupt:
        code = 130
    if keep:
        print(f"syndicate dev: kept {devroot}", file=sys.stderr)
        return code
    if _tmux_alive(env, session):
        print(f"syndicate dev: tmux session {session} is still running; kept {devroot}. "
              f"Stop it with: TMUX_TMPDIR={devroot / 'tmux'} tmux kill-server; then remove {devroot}",
              file=sys.stderr)
        return code
    if not _stop_monitor(home):
        print(f"syndicate dev: the dev limits monitor did not stop; kept {devroot}", file=sys.stderr)
        return code
    shutil.rmtree(devroot, ignore_errors=True)
    return code
