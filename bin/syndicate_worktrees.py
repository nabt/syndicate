"""Durable, locked task worktrees and their recovery snapshots.

Every project task works in a linked Git worktree under a durable root
(default ``~/.syndicate/worktrees``). Worktrees are locked against
``git worktree prune`` from the moment they are created until post-merge
cleanup proves the whole task series complete. A machine-wide registry records
each worktree so handoffs can snapshot it and cleanup can apply the series rule.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

STATE_DIR = Path.home() / ".syndicate"
# Worktrees live under ROOT unless SYNDICATE_WORKTREE_ROOT names another durable directory.
ROOT = STATE_DIR / "worktrees"
ROOT_ENV = "SYNDICATE_WORKTREE_ROOT"
# One registry per machine: worktrees of any repository, found by task ID from any project.
REGISTRY = ROOT / "registry.json"
LOCK_PREFIX = "syndicate:"
MOUNTS = Path("/proc/self/mounts")
VOLATILE_FSTYPES = ("tmpfs", "ramfs")
VOLATILE_DIRS = ("/tmp", "/var/tmp", "/dev/shm", "/run")
# Test seam: roots explicitly exempted from the volatile-location check. Production
# code never adds to this; tests add their own temporary directory.
VOLATILE_EXEMPT: list[Path] = []
GIT_TIMEOUT = 60
SNAPSHOT_REF = "refs/syndicate/snapshots"
# Inherited routing variables would point git at another repository or index.
GIT_ROUTING_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                   "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                   "GIT_NAMESPACE", "GIT_PREFIX")


class WorktreeError(Exception):
    """A task worktree could not be created, adopted, or verified."""


def git_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in GIT_ROUTING_ENV}
    env.update(extra)
    return env


def git(path, *args, check=True, env=None, timeout=GIT_TIMEOUT) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(("git", "-C", str(path), *args), text=True, errors="surrogateescape",
                                capture_output=True, timeout=timeout, env=env or git_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeError(f"Could not run git {args[0]}: {exc}") from exc
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or result.returncode
        raise WorktreeError(f"git {args[0]} failed: {detail}")
    return result


def lock_reason(series: str, task_id: str) -> str:
    return f"{LOCK_PREFIX} task series {series} (task {task_id}); do not prune until series complete"


def slug(branch: str) -> str:
    return branch.replace("/", "-")


# ---------------------------------------------------------------- locations

def _unescape_mount(field: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def mount_fstype(path: Path, mounts: Path | None = None) -> str | None:
    """Filesystem type of the longest mount point containing path, or None."""
    try:
        lines = (mounts or MOUNTS).read_text(errors="replace").splitlines()
    except OSError:
        return None
    best, fstype = None, None
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        point = Path(_unescape_mount(fields[1]))
        if path == point or path.is_relative_to(point):
            if best is None or len(point.parts) >= len(best.parts):
                best, fstype = point, fields[2]
    return fstype


def volatile_reason(path) -> str | None:
    """Why path is not durable across a restart, or None when it is."""
    path = Path(path).expanduser().resolve()
    if any(path.is_relative_to(Path(root).resolve()) for root in VOLATILE_EXEMPT):
        return None
    candidates = {Path(p) for p in VOLATILE_DIRS}
    candidates.add(Path(tempfile.gettempdir()))
    for name in ("TMPDIR", "XDG_RUNTIME_DIR"):
        if os.environ.get(name):
            candidates.add(Path(os.environ[name]))
    for candidate in sorted(candidates):
        if not candidate.is_absolute():
            continue
        for form in {candidate, candidate.resolve()}:
            if form != Path(form.anchor) and path.is_relative_to(form):
                return f"it is under the temporary directory {form}"
    fstype = mount_fstype(path)
    if fstype in VOLATILE_FSTYPES:
        return f"it is on a {fstype} filesystem that does not survive a restart"
    return None


def worktree_root() -> Path:
    override = os.environ.get(ROOT_ENV)
    root = Path(override).expanduser() if override else ROOT
    if not root.is_absolute():
        raise WorktreeError(f"{ROOT_ENV} must be an absolute path")
    return root.resolve()


def require_durable(path: Path) -> None:
    reason = volatile_reason(path)
    if reason:
        raise WorktreeError(
            f"Refusing to use {path} for a task worktree: {reason}. Task worktrees must survive "
            f"restarts; use the default ~/.syndicate/worktrees or set {ROOT_ENV} to a durable directory.")


def common_dir(repo: Path) -> Path:
    out = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    return Path(os.path.realpath(out))


def repo_key(common: Path) -> str:
    name = common.parent.name if common.name == ".git" else common.name.removesuffix(".git")
    return f"{name}-{hashlib.sha1(str(common).encode()).hexdigest()[:8]}"


def durable_path(common: Path, branch: str, root: Path | None = None) -> Path:
    return (root or worktree_root()) / repo_key(common) / slug(branch)


# ----------------------------------------------------------------- registry

@contextmanager
def registry_lock():
    REGISTRY.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (REGISTRY.parent / ".registry.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _atomic_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, allow_nan=False)
            f.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_registry() -> dict:
    try:
        data = json.loads(REGISTRY.read_text())
    except (OSError, ValueError, RecursionError):
        return {"worktrees": {}}
    if not isinstance(data, dict) or not isinstance(data.get("worktrees"), dict):
        return {"worktrees": {}}
    return data


def write_registry(data: dict) -> None:
    _atomic_json(REGISTRY, data)


def entry_for_path(path) -> dict | None:
    entry = read_registry()["worktrees"].get(str(Path(path).resolve()))
    return entry if isinstance(entry, dict) else None


def entries_for_task(task_id: str, source=None) -> list[dict]:
    """Registered worktrees of task_id, plus others in the same series and source."""
    entries = [e for e in read_registry()["worktrees"].values() if isinstance(e, dict)]
    source = str(source) if source is not None else None

    def same_source(entry):
        return source is None or entry.get("source") in (None, source)

    own = [e for e in entries if task_id in (e.get("task_ids") or []) and same_source(e)]
    series = {(e.get("source"), e.get("series")) for e in own}
    extra = [e for e in entries if e not in own and (e.get("source"), e.get("series")) in series]
    return sorted(own + extra, key=lambda e: e.get("path", ""))


def drop_entry(path) -> None:
    with registry_lock():
        data = read_registry()
        if data["worktrees"].pop(str(Path(path).resolve()), None) is not None:
            write_registry(data)


# ----------------------------------------------------------------- creation

def list_worktrees(repo: Path) -> list[dict]:
    records = git(repo, "worktree", "list", "--porcelain", "-z").stdout.split("\0\0")
    return [dict(tuple(field.split(" ", 1)) if " " in field else (field, "")
                 for field in record.split("\0") if field)
            for record in records if record]


def _ref_exists(repo: Path, ref: str) -> bool:
    return git(repo, "show-ref", "--verify", "--quiet", ref, check=False).returncode == 0


def default_base(repo: Path) -> str:
    for ref, name in (("refs/remotes/origin/main", "origin/main"), ("refs/heads/main", "main")):
        if _ref_exists(repo, ref):
            return name
    return "HEAD"


def _add_locked(repo: Path, reason: str, target: Path, args: list[str]) -> None:
    result = git(repo, "worktree", "add", "--lock", "--reason", reason, *args, check=False)
    if result.returncode == 129 and "--reason" in (result.stderr or ""):
        # Older Git lacks `add --reason`: add, then lock immediately.
        git(repo, "worktree", "add", *args)
        git(repo, "worktree", "lock", "--reason", reason, "--", str(target))
        return
    if result.returncode != 0:
        raise WorktreeError(f"git worktree add failed: {result.stderr.strip() or result.stdout.strip()}")


def ensure_worktree(source, repo, task_id: str, branch: str, series: str | None = None,
                    base: str | None = None) -> dict:
    """Create or re-adopt the durable, locked worktree for branch; return its registry entry."""
    source = Path(source).expanduser().resolve()
    repo = Path(repo).expanduser().resolve()
    if not task_id or not re.fullmatch(r"[A-Za-z0-9._:-]+", task_id):
        raise WorktreeError("A task ID of letters, digits, '.', '_', ':' or '-' is required")
    if not branch or branch.startswith("-") or branch.startswith("refs/") or branch in ("main", "master", "HEAD"):
        raise WorktreeError("A task branch other than main is required (for example work/<task>)")
    git(repo, "check-ref-format", "--branch", branch)
    if series is not None and (not series.strip() or "\n" in series):
        raise WorktreeError("A series name must be a single non-empty line")
    common = common_dir(repo)
    target = durable_path(common, branch)
    require_durable(target)
    task_ref = f"refs/heads/{branch}"
    with registry_lock():
        data = read_registry()
        previous = data["worktrees"].get(str(target))
        previous = previous if isinstance(previous, dict) else None
        if previous and series and previous.get("series") != series:
            raise WorktreeError(f"{target} already belongs to task series {previous.get('series')}; "
                                "use that series name")
        series = series or (previous or {}).get("series") or branch
        worktrees = list_worktrees(repo)
        primary = Path(worktrees[0]["worktree"]).resolve()
        holder = next((wt for wt in worktrees if wt.get("branch") == task_ref), None)
        at_target = next((wt for wt in worktrees if Path(wt["worktree"]).resolve() == target), None)
        reason = lock_reason(series, task_id)
        created = False
        if holder is not None:
            held = Path(holder["worktree"]).resolve()
            if held == primary:
                raise WorktreeError(
                    f"Branch {branch} is checked out in the main checkout {held}. Commit its work, switch "
                    "the main checkout to another branch, and rerun this command; task work belongs in "
                    "its own worktree.")
            if held != target:
                why = volatile_reason(held)
                where = f"a volatile path ({why})" if why else "a path outside the durable worktree root"
                raise WorktreeError(
                    f"Branch {branch} is already checked out at {held}, {where}. Commit or save its work, "
                    f"then move it with `git -C {repo} worktree move {held} {target}` (run "
                    f"`git worktree unlock {held}` first if it is locked) and rerun this command.")
            if "prunable" in holder or not target.is_dir():
                raise WorktreeError(
                    f"Registered worktree {target} is missing. Recover from the latest snapshot ref under "
                    f"{SNAPSHOT_REF}/{slug(branch)}/ (see the handoff), then run `git -C {repo} worktree "
                    f"unlock {target}` and `git -C {repo} worktree prune` before rerunning this command.")
            if "locked" not in holder:
                git(repo, "worktree", "lock", "--reason", reason, "--", str(target))
            locked_reason = holder.get("locked") if "locked" in holder else reason
        else:
            if at_target is not None or target.exists():
                raise WorktreeError(f"{target} already exists and does not hold branch {branch}; "
                                    "inspect it before rerunning")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if _ref_exists(repo, task_ref):
                _add_locked(repo, reason, target, ["--", str(target), branch])
                base = (previous or {}).get("base") or base or default_base(repo)
            elif _ref_exists(repo, f"refs/remotes/origin/{branch}"):
                _add_locked(repo, reason, target, ["--track", "-b", branch, "--", str(target),
                                                    f"origin/{branch}"])
                base = base or f"origin/{branch}"
            else:
                base = base or default_base(repo)
                if git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", check=False).returncode:
                    raise WorktreeError(f"Base {base} does not name a commit in {repo}")
                _add_locked(repo, reason, target, ["--no-track", "-b", branch, "--", str(target), base])
            created = True
            locked_reason = reason
        entry = dict(previous or {})
        task_ids = list(entry.get("task_ids") or [])
        if task_id not in task_ids:
            task_ids.append(task_id)
        entry.update(path=str(target), repo=str(primary), git_common_dir=str(common), branch=branch,
                     series=series, base=entry.get("base") or base, task_ids=task_ids,
                     source=entry.get("source") or str(source), lock_reason=locked_reason)
        entry.setdefault("created_at", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        entry["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        data["worktrees"][str(target)] = entry
        write_registry(data)
    return {**entry, "created": created}


# ---------------------------------------------------------------- handoffs

UNAVAILABLE = b"Git snapshot unavailable.\n"


def _capture(path, *args, env=None) -> bytes | None:
    try:
        # --no-optional-locks: status and diff must not refresh the worktree's own index.
        result = subprocess.run(("git", "--no-optional-locks", "-C", str(path), *args), capture_output=True,
                                timeout=GIT_TIMEOUT, env=env or git_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _text(path, *args, env=None) -> str | None:
    out = _capture(path, *args, env=env)
    return out.decode(errors="replace").strip() if out is not None else None


def _snapshot(path: Path, scratch: Path, branch: str, run_id: str, task_id: str) -> tuple[str | None, str | None]:
    """Commit tracked and untracked (non-ignored) state without touching the worktree, index or HEAD."""
    index = scratch / "snapshot.index"
    env = git_env(GIT_INDEX_FILE=str(index), GIT_AUTHOR_NAME="Syndicate handoff",
                  GIT_AUTHOR_EMAIL="syndicate@localhost", GIT_COMMITTER_NAME="Syndicate handoff",
                  GIT_COMMITTER_EMAIL="syndicate@localhost")
    try:
        head = _text(path, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
        if head and _capture(path, "read-tree", head, env=env) is None:
            return None, None
        if _capture(path, "add", "-A", "--", ".", env=env) is None:
            return None, None
        tree = _text(path, "write-tree", env=env)
        if not tree:
            return None, None
        message = f"Syndicate handoff snapshot {run_id} of {branch} (task {task_id})"
        parents = ("-p", head) if head else ()
        commit = _text(path, "commit-tree", "--no-gpg-sign", *parents, "-m", message, tree, env=env)
        if not commit:
            return None, None
        ref = f"{SNAPSHOT_REF}/{slug(branch)}/{run_id}"
        if _capture(path, "update-ref", "-m", message, ref, commit) is None:
            return None, None
        return ref, commit
    finally:
        for leftover in (index, Path(str(index) + ".lock")):
            try:
                leftover.unlink()
            except OSError:
                pass


def capture_task_worktrees(target: Path, project: Path, task_id: str | None, run_id: str) -> str:
    """Write worktrees/<slug>/ evidence and snapshots; return the handoff.md section."""
    lines = ["## Task worktrees", ""]
    if not task_id:
        return "\n".join(lines + ["No current task ID was recorded, so no task worktree was captured. "
                                  "Check the checkpoint for the task's worktree and branch.", ""])
    try:
        entries = entries_for_task(task_id, project)
    except Exception:  # never abort a handoff on registry trouble
        entries = []
    if not entries:
        return "\n".join(lines + [f"No task worktree is registered for task {task_id}. Diffs above cover "
                                  "only the launch directory. Create one with `syndicate tasks worktree` "
                                  "before further edits.", ""])
    lines.append("Continue the task in its worktree below, not in the launch directory. "
                 "Keep it locked; never unlock or remove it before the series is verified complete.")
    lines.append("")
    for entry in entries:
        try:
            lines.extend(_capture_one(target, entry, task_id, run_id))
        except Exception as error:  # noqa: BLE001 - a handoff must still be written
            lines.append(f"- {entry.get('path')}: capture failed ({error})")
    lines.append("")
    return "\n".join(lines)


def _capture_one(target: Path, entry: dict, task_id: str, run_id: str) -> list[str]:
    path = Path(entry.get("path", ""))
    branch = entry.get("branch") or "unknown"
    out = target / "worktrees" / slug(branch)
    suffix = 1
    while out.exists():  # the same branch name in another repository
        suffix += 1
        out = target / "worktrees" / f"{slug(branch)}-{suffix}"
    out.mkdir(parents=True, mode=0o700)
    repo = Path(entry.get("repo") or entry.get("git_common_dir") or path)
    records = []
    try:
        records = list_worktrees(repo)
    except WorktreeError:
        pass
    record = next((wt for wt in records if Path(wt.get("worktree", "")).resolve() == path.resolve()), None)
    exists = path.is_dir()
    info = {"path": str(path), "branch": branch, "base": entry.get("base"), "series": entry.get("series"),
            "task_ids": entry.get("task_ids"), "repo": entry.get("repo"), "exists": exists,
            "registered": record is not None,
            "locked": bool(record and "locked" in record), "lock_reason": (record or {}).get("locked"),
            "prunable": bool(record and "prunable" in record), "head": None,
            "snapshot_ref": None, "snapshot_commit": None}

    def write(name, data):
        (out / name).write_bytes(UNAVAILABLE if data is None else data)

    if exists:
        info["head"] = _text(path, "rev-parse", "--verify", "--quiet", "HEAD")
        write("git-status.txt", _capture(path, "status", "--porcelain=v1", "--branch", "--untracked-files=all"))
        write("worktree.diff", _capture(path, "diff", "--binary", "HEAD"))
        write("staged.diff", _capture(path, "diff", "--cached", "--binary"))
        untracked = _capture(path, "ls-files", "--others", "--exclude-standard", "-z")
        write("untracked.txt", None if untracked is None else
              b"".join(name + b"\n" for name in untracked.split(b"\0") if name))
        upstream = _text(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
        if not upstream and _text(path, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"):
            upstream = f"origin/{branch}"
        compare = upstream or entry.get("base")
        log = _capture(path, "log", "--oneline", "--no-decorate", f"{compare}..HEAD") if compare else None
        write("unpushed.log", None if log is None else
              f"# Commits on HEAD not in {compare}{' (no upstream; compared with base)' if not upstream else ''}\n"
              .encode() + log)
        ref, commit = _snapshot(path, out, branch, run_id, task_id)
        info.update(snapshot_ref=ref, snapshot_commit=commit)
    else:
        info["head"] = _text(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        note = (f"Worktree directory {path} is missing"
                f"{'; git reports it prunable' if info['prunable'] else ''}. Its uncommitted work is not "
                "recoverable from here; check earlier snapshot refs.\n").encode()
        for name in ("git-status.txt", "worktree.diff", "staged.diff", "untracked.txt", "unpushed.log"):
            write(name, note)
    _atomic_json(out / "info.json", info)
    lines = [f"- Path: {path}", f"  Branch: {branch} (series {info['series']}, base {info['base']})",
             f"  HEAD: {info['head'] or 'unavailable'}; locked: {'yes' if info['locked'] else 'no'}",
             f"  Evidence: worktrees/{out.name}/"]
    if not exists:
        lines.append(f"  MISSING: the directory does not exist{' (prunable)' if info['prunable'] else ''}. "
                     f"Recover with `git -C {entry.get('repo')} worktree unlock {path}`, `git worktree prune`, "
                     f"`git worktree add {path} {branch}`, then restore the latest snapshot under "
                     f"{SNAPSHOT_REF}/{slug(branch)}/ if one exists.")
    elif info["snapshot_ref"]:
        lines.append(f"  Snapshot: {info['snapshot_ref']} = {info['snapshot_commit']} (tracked and untracked files)")
        lines.append(f"  If the directory is lost: `git -C {entry.get('repo')} worktree add {path} {branch}`, then "
                     f"`git -C {path} restore --source {info['snapshot_ref']} --worktree -- .` "
                     f"(or `git checkout {info['snapshot_ref']} -- .`).")
    else:
        lines.append("  Snapshot: unavailable")
    return lines
