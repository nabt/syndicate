"""Release identity, pinned self-invocation and runtime resource lookup.

Everything here is derived from the real path of this file, resolved once at
import. The running launcher is bin/syndicate beside it, so a release, a
checkout and a dev snapshot each use only their own code and assets. The
`current` release symlink is never re-resolved after startup.

Release layout (written by the installer, read here):
  <data>/syndicate/releases/<commit>/RELEASE.json   release mode marker/manifest
  <data>/syndicate/current -> releases/<commit>
  <data>/syndicate/activations.jsonl                one JSON record per activation
<data> is $XDG_DATA_HOME or ~/.local/share.

Nothing in this module writes files or creates directories.
"""
from __future__ import annotations

import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

# Every bin/ module the launcher imports. The installer's allowlist must ship
# all of them beside bin/syndicate.
RUNTIME_MODULES = (
    "syndicate_completion.py",
    "syndicate_dev.py",
    "syndicate_git.py",
    "syndicate_identity.py",
    "syndicate_live.py",
    "syndicate_portfolio.py",
    "syndicate_runtime.py",
    "syndicate_worktrees.py",
)

BIN = Path(__file__).resolve().parent
ROOT = BIN.parent
LAUNCHER = BIN / "syndicate"
LIVE_MODULE = BIN / "syndicate_live.py"
MANIFEST = "RELEASE.json"
ACTIVATIONS = "activations.jsonl"
COMMIT = re.compile(r"[0-9a-f]{40}")

# Set by `syndicate dev` for the launched snapshot and inherited by its children.
DEV_ENV = "SYNDICATE_DEV"
DEV_CHECKOUT_ENV = "SYNDICATE_DEV_CHECKOUT"
DEV_COMMIT_ENV = "SYNDICATE_DEV_COMMIT"
DEV_BRANCH_ENV = "SYNDICATE_DEV_BRANCH"
DEV_DIRTY_ENV = "SYNDICATE_DEV_DIRTY"
DEV_PRODUCTION_HOME_ENV = "SYNDICATE_DEV_PRODUCTION_HOME"
DEV_TMUX_SESSION_ENV = "SYNDICATE_TMUX_SESSION"
PRODUCTION_TMUX_SESSION = "the-syndicate"
ACTIVATION_SCAN_BYTES = 1 << 20  # newest records are at the end; read at most this much


def interpreter() -> str:
    """Absolute path of the running interpreter, kept as invoked (venv symlinks stay)."""
    executable = sys.executable or shutil.which("python3") or "python3"
    return os.path.abspath(executable)


def resource(*parts: str) -> Path:
    """A bundled asset of this release or checkout, independent of cwd and PATH."""
    return ROOT.joinpath(*parts)


def python_argv(script: str | os.PathLike) -> list[str]:
    """Run a bundled Python file with the pinned interpreter and bytecode disabled."""
    return [interpreter(), "-B", str(script)]


def pinned_argv(script: str | os.PathLike | None = None) -> list[str]:
    """The one invocation prefix for re-entering Syndicate: interpreter, -B, real launcher path."""
    launcher = Path(script).resolve() if script is not None else LAUNCHER
    return python_argv(launcher)


def pinned_prefix(script: str | os.PathLike | None = None) -> str:
    """Shell rendering of pinned_argv(); every token is shlex-quoted, so spaces are safe."""
    return shlex.join(pinned_argv(script))


def pinned_command(*args: str) -> str:
    """Shell rendering of the pinned prefix followed by literal Syndicate arguments."""
    return shlex.join([*pinned_argv(), *args])


def child_env(env: dict | None = None) -> dict:
    """Environment for a Python child: bytecode writes disabled."""
    result = dict(os.environ if env is None else env)
    result["PYTHONDONTWRITEBYTECODE"] = "1"
    return result


# Bare `syndicate SUBCOMMAND` in bundled roles and skills addresses this exact
# release. Commands a person types to watch tmux keep the stable name.
_USER_FACING = ("show", "attach", "detach", "list", "capture", "completion")
_BARE_COMMAND = re.compile(
    r"(?m)(`|^[ \t]*(?:\$ )?)syndicate (?=[a-z-])(?!(?:%s)\b)" % "|".join(_USER_FACING))


def pin_commands(text: str, prefix: str | None = None) -> str:
    """Render bundled-instruction commands with the pinned prefix for session operations."""
    prefix = pinned_prefix() if prefix is None else prefix
    return _BARE_COMMAND.sub(lambda m: f"{m.group(1)}{prefix} ", text)


# --- identity ---

def dev_mode() -> bool:
    return os.environ.get(DEV_ENV) == "1"


def read_manifest(root: Path = ROOT) -> dict | None:
    """RELEASE.json as a dict, {} when present but unreadable, None when absent."""
    path = root / MANIFEST
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def mode(root: Path = ROOT) -> str:
    if dev_mode():
        return "dev"
    return "release" if (root / MANIFEST).is_file() else "checkout"


def data_root(root: Path = ROOT) -> Path:
    """<data>/syndicate for this release: its releases/ grandparent, else XDG_DATA_HOME."""
    if root.parent.name == "releases":
        return root.parent.parent
    base = os.environ.get("XDG_DATA_HOME", "").strip()
    return (Path(base).expanduser() if base else Path.home() / ".local" / "share") / "syndicate"


def latest_activation(commit: str, root: Path = ROOT) -> dict | None:
    """Newest well-formed activation record for commit; missing or garbled logs yield None."""
    path = data_root(root) / ACTIVATIONS
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - ACTIVATION_SCAN_BYTES))
            raw = handle.read()
    except OSError:
        return None
    found = None
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("commit") == commit:
            found = record
    return found


def manifest_problem(manifest: dict, installed_root: Path | None) -> str | None:
    """Why RELEASE.json does not match the installer's contract
    ({format: 1, commit, files, python_min}); None when it does. For an
    installed release the commit must also be its releases/<commit> name."""
    commit = manifest.get("commit")
    if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
        return "does not name a valid commit"
    if (manifest.get("format") != 1 or not isinstance(manifest.get("files"), dict)
            or not isinstance(manifest.get("python_min"), str)):
        return "is malformed (expected format 1 with commit, files and python_min)"
    if installed_root is not None and installed_root.name != commit:
        return f"names commit {commit}, not its release directory {installed_root.name}"
    return None


def current_release(install_root: Path) -> Path | None:
    """The release <install_root>/current points at, resolved; None if absent or broken."""
    link = install_root / "current"
    try:
        return link.resolve(strict=True) if link.is_symlink() else None
    except (OSError, RuntimeError):
        return None


def _git(root: Path, *args: str) -> str | None:
    # No optional locks: reading identity must never refresh or write the index.
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    try:
        result = subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args],
                                capture_output=True, text=True, env=env, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def checkout_state(root: Path) -> dict:
    """Commit, branch and dirty flag of a Git checkout, read without writing anything."""
    commit = _git(root, "rev-parse", "HEAD")
    branch = _git(root, "symbolic-ref", "--short", "-q", "HEAD")
    status = _git(root, "status", "--porcelain", "--untracked-files=normal")
    return {"commit": commit if commit and COMMIT.fullmatch(commit) else None,
            "branch": branch or None,
            "dirty": None if status is None else bool(status)}


@lru_cache(maxsize=None)
def _identity(root: Path, running_mode: str) -> tuple:
    info = {"mode": running_mode, "root": str(root), "commit": None}
    if running_mode == "release":
        manifest = read_manifest(root) or {}
        commit = manifest.get("commit")
        info["commit"] = commit if isinstance(commit, str) and COMMIT.fullmatch(commit) else None
        installed = root.parent.name == "releases"
        if installed:
            info["install_root"] = str(root.parent.parent)
            info["current"] = current_release(root.parent.parent) == root
        problem = manifest_problem(manifest, root if installed else None)
        if problem:
            info["error"] = f"{root / MANIFEST} {problem}"
        if info["commit"] is not None:
            record = latest_activation(info["commit"], root)
            if record:
                info["activation"] = {k: record.get(k) for k in
                                      ("at", "tag", "source_ref", "upstream", "previous_commit")
                                      if record.get(k) is not None}
                if isinstance(record.get("tag"), str) and record["tag"]:
                    info["tag"] = record["tag"]
    elif running_mode == "dev":
        commit = os.environ.get(DEV_COMMIT_ENV, "")
        info.update(commit=commit if COMMIT.fullmatch(commit) else None,
                    branch=os.environ.get(DEV_BRANCH_ENV) or None,
                    dirty=os.environ.get(DEV_DIRTY_ENV) == "1",
                    checkout=os.environ.get(DEV_CHECKOUT_ENV) or None)
    else:
        state = checkout_state(root)
        info.update(commit=state["commit"], branch=state["branch"], dirty=state["dirty"])
    return tuple(info.items())


def identity(root: Path = ROOT) -> dict:
    """Mode, commit, root and related facts about the running code. Cached per process."""
    info = json.loads(json.dumps(dict(_identity(root, mode(root)))))
    info["interpreter"] = interpreter()
    info["launcher"] = str(root / "bin" / "syndicate")
    return info


def run_identity() -> dict:
    """The release identity recorded in run diagnostics (run.json, monitor state)."""
    info = identity()
    keep = ("mode", "commit", "dirty", "tag", "root", "interpreter", "checkout")
    return {key: info[key] for key in keep if info.get(key) is not None}


def render_version(info: dict) -> str:
    commit = info.get("commit") or "unknown"
    dirty = " (dirty)" if info.get("dirty") else ""
    lines = [f"syndicate {commit[:12]}{dirty}",
             f"mode: {info['mode']}",
             f"commit: {commit}{' dirty' if info.get('dirty') else ''}"]
    if info.get("branch"):
        lines.append(f"branch: {info['branch']}")
    if info.get("tag"):
        lines.append(f"tag: {info['tag']}")
    activation = info.get("activation") or {}
    if activation.get("at"):
        lines.append(f"activated: {activation['at']}")
    if info.get("checkout"):
        lines.append(f"checkout: {info['checkout']}")
    if info.get("install_root"):
        lines.append(f"install root: {info['install_root']}"
                     f"{'' if info.get('current') else ' (not the current release)'}")
    if info.get("error"):
        lines.append(f"warning: {info['error']}")
    lines += [f"interpreter: {info['interpreter']}", f"root: {info['root']}"]
    return "\n".join(lines) + "\n"


# --- dev-mode isolation guard ---

def production_home() -> Path:
    """The account's real home: the passwd entry, or the HOME `syndicate dev` was called with."""
    recorded = os.environ.get(DEV_PRODUCTION_HOME_ENV)
    if recorded:
        return Path(recorded)
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _same_or_inside(path: Path, root: Path) -> bool:
    try:
        path, root = path.resolve(), root.resolve()
    except OSError:
        return True
    return path == root or root in path.parents


def dev_violations(state_dirs: list[Path], config_file: Path, tmux_session: str) -> list[str]:
    """Reasons this dev run could touch production state; empty when isolated."""
    homes = {production_home(), Path(pwd.getpwuid(os.getuid()).pw_dir)}
    problems = []
    if any(Path.home().resolve() == home.resolve() for home in homes):
        problems.append(f"HOME is the production home {Path.home()}")
    for home in homes:
        protected = [home / ".syndicate", home / ".config" / "syndicate",
                     home / ".local" / "share" / "syndicate"]
        for path in [*state_dirs, config_file]:
            if any(_same_or_inside(path, root) for root in protected):
                problems.append(f"{path} is production Syndicate state")
    if tmux_session == PRODUCTION_TMUX_SESSION:
        problems.append(f"tmux session name {PRODUCTION_TMUX_SESSION} belongs to production")
    tmux_dir = os.environ.get("TMUX_TMPDIR", "")
    if not tmux_dir or Path(tmux_dir).resolve() == Path("/tmp").resolve():
        problems.append("TMUX_TMPDIR must name a disposable directory so the production tmux server is not used")
    socket = os.environ.get("TMUX", "").split(",", 1)[0]
    if socket and (not tmux_dir or not _same_or_inside(Path(socket), Path(tmux_dir))):
        problems.append(f"TMUX points at a tmux server outside {tmux_dir or 'TMUX_TMPDIR'}")
    return problems


class DevTargetError(Exception):
    """A dev run was pointed at production state, or the target could not be proven safe."""


def _git_common_dir(directory: Path) -> Path | None:
    """Absolute Git common dir of directory's repository; None outside a repository.

    Raises DevTargetError when Git cannot be run, so the caller fails closed."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_CEILING_DIRECTORIES")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        result = subprocess.run(["git", "--no-optional-locks", "-C", str(directory), "rev-parse", "--git-common-dir"],
                                capture_output=True, text=True, env=env, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DevTargetError(f"cannot run git to check {directory}: {error}") from None
    if result.returncode:
        return None
    value = result.stdout.strip()
    if not value:
        raise DevTargetError(f"git reported no common dir for {directory}")
    return (directory / value).resolve() if not os.path.isabs(value) else Path(value).resolve()


def _existing_dir(path: Path) -> Path:
    """path itself when it is a directory, else its nearest existing ancestor directory."""
    while not path.is_dir():
        if path.parent == path:
            break
        path = path.parent
    return path


def production_projects() -> list[Path]:
    """Paths (projects and generic task groups) in the production registry, read only.

    A missing registry lists nothing; an unreadable or malformed one raises, so
    a dev run cannot proceed without proving its target is not registered."""
    path = production_home() / ".syndicate" / "projects.json"
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return []
    except OSError as error:
        raise DevTargetError(f"cannot read the production registry {path}: {error}") from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise DevTargetError(f"the production registry {path} is not valid JSON") from None
    entries = data.get("projects", []) if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise DevTargetError(f"the production registry {path} has an unexpected shape")
    paths = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for key in ("path", "group_path"):
            value = entry.get(key)
            if isinstance(value, str) and os.path.isabs(value):
                paths.append(Path(value))
    return paths


def dev_target_violation(target: str | os.PathLike) -> str | None:
    """Why a dev run must not use target as a project, source, repo or worktree; None when safe.

    Refuses a target that is, or is inside, production Syndicate state or a
    production-registered project (or generic task group), or whose Git
    repository shares its common dir with a registered project (a linked task
    worktree, or a subdirectory of one). Any doubt is a refusal."""
    try:
        path = Path(os.path.abspath(Path(target).expanduser())).resolve()
    except (OSError, RuntimeError, ValueError) as error:
        return f"cannot resolve {target}: {error}"
    homes = {production_home(), Path(pwd.getpwuid(os.getuid()).pw_dir)}
    for home in homes:
        for root in (home / ".syndicate", home / ".config" / "syndicate", home / ".local" / "share" / "syndicate"):
            if _same_or_inside(path, root):
                return f"{path} is production Syndicate state ({root})"
    try:
        registered = production_projects()
        for project in registered:
            if _same_or_inside(path, project):
                return f"{path} is the production project {project} or inside it"
        common = _git_common_dir(_existing_dir(path))
        if common is not None:
            for project in registered:
                if project.is_dir() and _git_common_dir(project) == common:
                    return (f"{path} shares the Git repository {common} of the production project {project} "
                            "(a linked worktree or subdirectory of it)")
    except DevTargetError as error:
        return str(error)
    return None
