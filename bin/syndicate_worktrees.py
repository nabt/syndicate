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
import shlex
import shutil
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


class RegistryError(WorktreeError):
    """The registry file exists but cannot be read or is not a valid registry."""


BRANCH_CHARS = re.compile(r"[A-Za-z0-9._/-]+")
# Series names appear in lock reasons and shell commands: one short printable line.
SERIES_CHARS = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._/:@+-]{0,99}")
OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


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


def q(value) -> str:
    """Shell-quote a value interpolated into a command shown to the user."""
    return shlex.quote(str(value))


def verify_base(repo, base: str) -> str:
    """Return base when it names a commit in repo; never let it act as a git option."""
    if not isinstance(base, str) or not base or base.startswith("-") or not base.isprintable():
        raise WorktreeError(f"Base {base!r} is not a valid revision")
    result = git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{base}^{{commit}}", check=False)
    if result.returncode != 0:
        raise WorktreeError(f"Base {base} does not name a commit in {repo}")
    return base


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
    """The registry, or an empty one when the file does not exist.

    An unreadable or invalid file raises RegistryError; callers that write must
    never replace it, since it may hold the only record of a locked worktree."""
    try:
        text = REGISTRY.read_text()
    except FileNotFoundError:
        return {"worktrees": {}}
    except (OSError, UnicodeDecodeError) as exc:
        raise RegistryError(_registry_problem(f"cannot be read: {exc}")) from exc
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise RegistryError(_registry_problem("is not valid JSON")) from exc
    if not isinstance(data, dict) or not isinstance(data.get("worktrees"), dict):
        raise RegistryError(_registry_problem('has no "worktrees" object'))
    return data


def _registry_problem(why: str) -> str:
    return (f"Task worktree registry {REGISTRY} {why}. It was left unchanged. Inspect it; if it cannot "
            f"be repaired, move it aside (for example `mv {q(REGISTRY)} {q(str(REGISTRY) + '.bad')}`) and "
            "rerun `syndicate tasks worktree` for each task worktree to register it again.")


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
        data = read_registry()  # raises RegistryError rather than overwrite an invalid file
        if data["worktrees"].pop(str(Path(path).resolve()), None) is not None:
            write_registry(data)


def series_task_ids(entry: dict) -> list[str]:
    """Task IDs of every registered worktree sharing entry's source and series."""
    key = (entry.get("source"), entry.get("series"))
    ids: list[str] = []
    for other in read_registry()["worktrees"].values():
        if isinstance(other, dict) and (other.get("source"), other.get("series")) == key:
            ids.extend(t for t in other.get("task_ids") or [] if isinstance(t, str) and t not in ids)
    return ids


def delete_snapshots(repo, branch: str) -> list[tuple[str, str]]:
    """Delete branch's handoff snapshot refs; return (ref, old commit) pairs deleted.

    Refs are deleted with compare-and-delete against the listed commit, and only
    when the snapshot commit names this branch (slugs of distinct branches can collide)."""
    prefix = f"{SNAPSHOT_REF}/{slug(branch)}/"
    listing = git(repo, "for-each-ref", "--format=%(refname)%00%(objectname)%00%(contents:subject)",
                  "--end-of-options", prefix).stdout
    deleted = []
    for line in listing.splitlines():
        ref, oid, subject = (line.split("\0") + ["", ""])[:3]
        mine = re.fullmatch(rf"Syndicate handoff snapshot \S+ of {re.escape(branch)} \(task [^)]*\)", subject)
        if not ref.startswith(prefix) or not OID.fullmatch(oid) or not mine:
            continue
        if git(repo, "update-ref", "-d", ref, oid, check=False).returncode == 0:
            deleted.append((ref, oid))
    return deleted


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
    stderr = result.stderr or ""
    # Git before 2.37 lacks `add --reason` and reports: error: unknown option `reason'
    if result.returncode == 129 and "unknown option" in stderr and "reason" in stderr:
        # Older Git lacks `add --reason`: add, then lock immediately.
        git(repo, "worktree", "add", *args)
        lock_now(repo, reason, target)
        return
    if result.returncode != 0:
        raise WorktreeError(f"git worktree add failed: {result.stderr.strip() or result.stdout.strip()}")


def lock_now(repo: Path, reason: str, target: Path) -> None:
    git(repo, "worktree", "lock", "--reason", reason, "--", str(target))


def recovery_commands(repo, path, task_id, source, branch, series, snapshot_ref=None) -> list[str]:
    """Shell commands that recreate a lost task worktree, locked, at the same path."""
    commands = [f"git -C {q(repo)} worktree unlock {q(path)} && git -C {q(repo)} worktree prune",
                f"syndicate tasks worktree {q(task_id)} -C {q(source)} --repo {q(repo)} "
                f"--branch {q(branch)} --series {q(series)}"]
    if snapshot_ref:
        commands.append(f"git -C {q(path)} restore --source {q(snapshot_ref)} --worktree -- .")
    return commands


def ensure_worktree(source, repo, task_id: str, branch: str, series: str | None = None,
                    base: str | None = None) -> dict:
    """Create or re-adopt the durable, locked worktree for branch; return its registry entry."""
    source = Path(source).expanduser().resolve()
    repo = Path(repo).expanduser().resolve()
    if not task_id or not re.fullmatch(r"[A-Za-z0-9._:-]+", task_id):
        raise WorktreeError("A task ID of letters, digits, '.', '_', ':' or '-' is required")
    if not branch or branch.startswith("-") or branch.startswith("refs/") or branch in ("main", "master", "HEAD"):
        raise WorktreeError("A task branch other than main is required (for example work/<task>)")
    if not BRANCH_CHARS.fullmatch(branch):
        raise WorktreeError("A task branch may contain only letters, digits, '.', '_', '/' and '-'")
    git(repo, "check-ref-format", "--branch", branch)
    if series is not None and not SERIES_CHARS.fullmatch(series):
        raise WorktreeError("A series name must be one line of at most 100 letters, digits, spaces or "
                            "'._/:@+-', starting with a letter or digit")
    if base is not None:
        verify_base(repo, base)
    common = common_dir(repo)
    target = durable_path(common, branch)
    require_durable(target)
    task_ref = f"refs/heads/{branch}"
    with registry_lock():
        data = read_registry()  # raises RegistryError rather than overwrite an invalid file
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
                    f"then move it with `git -C {q(repo)} worktree move {q(held)} {q(target)}` (run "
                    f"`git -C {q(repo)} worktree unlock {q(held)}` first if it is locked) and rerun this command.")
            if "prunable" in holder or not target.is_dir():
                unprune, readd = recovery_commands(repo, target, task_id, source, branch, series)
                raise WorktreeError(
                    f"Registered worktree {target} is missing. Run `{unprune}`, then rerun this command "
                    f"(`{readd}`) to add and lock it again, and restore the latest snapshot with "
                    f"`git -C {q(target)} restore --source <ref> --worktree -- .`, taking <ref> from the "
                    f"handoff or `git -C {q(repo)} for-each-ref {SNAPSHOT_REF}/{slug(branch)}/`.")
            if "locked" not in holder:
                lock_now(repo, reason, target)
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
                base = verify_base(repo, base or default_base(repo))
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


def _valid_revision(path, revision) -> bool:
    """True when a registry-derived revision names a commit and cannot act as an option."""
    if not isinstance(revision, str) or not revision or revision.startswith("-") or not revision.isprintable():
        return False
    return bool(_text(path, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{revision}^{{commit}}"))


def _seed_index(path: Path, index: Path, head: str | None, env: dict, copy: bool = True) -> bool:
    """Seed the temporary index from a copy of the worktree's index (fast: stat data
    is reused), falling back to HEAD's tree. The worktree's own index is only read."""
    real = _text(path, "rev-parse", "--path-format=absolute", "--git-path", "index") if copy else None
    if real:
        try:
            shutil.copyfile(real, index)
            return True
        except OSError:
            pass
    try:
        index.unlink()
    except OSError:
        pass
    if head:
        return _capture(path, "read-tree", head, env=env) is not None
    return True


def _snapshot(path: Path, scratch: Path, branch: str, run_id: str, task_id: str) -> tuple[str | None, str | None]:
    """Commit tracked and untracked (non-ignored) state without touching the worktree, index or HEAD."""
    index = scratch / "snapshot.index"
    env = git_env(GIT_INDEX_FILE=str(index), GIT_AUTHOR_NAME="Syndicate handoff",
                  GIT_AUTHOR_EMAIL="syndicate@localhost", GIT_COMMITTER_NAME="Syndicate handoff",
                  GIT_COMMITTER_EMAIL="syndicate@localhost")
    try:
        head = _text(path, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
        if head and not OID.fullmatch(head):
            return None, None
        if not _seed_index(path, index, head, env):
            return None, None
        if _capture(path, "add", "-A", "--", ".", env=env) is None:
            # A copied index the temporary location cannot use (for example a split
            # index): start again from HEAD.
            if not _seed_index(path, index, head, env, copy=False) or (
                    _capture(path, "add", "-A", "--", ".", env=env) is None):
                return None, None
        tree = _text(path, "write-tree", env=env)
        if not tree or not OID.fullmatch(tree):
            return None, None
        message = f"Syndicate handoff snapshot {run_id} of {branch} (task {task_id})"
        parents = ("-p", head) if head else ()
        commit = _text(path, "commit-tree", "--no-gpg-sign", *parents, "-m", message, tree, env=env)
        if not commit or not OID.fullmatch(commit):
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


def capture_task_worktrees(target: Path, project: Path, task_ids, run_id: str) -> str:
    """Write worktrees/<slug>/ evidence and snapshots; return the handoff.md section.

    task_ids is the task ID or IDs this run worked on (launcher record first)."""
    if isinstance(task_ids, str):
        task_ids = [task_ids]
    task_ids = [t for t in dict.fromkeys(task_ids or []) if isinstance(t, str) and t]
    lines = ["## Task worktrees", ""]
    if not task_ids:
        return "\n".join(lines + ["No current task ID was recorded, so no task worktree was captured. "
                                  "Check the checkpoint for the task's worktree and branch.", ""])
    if len(task_ids) > 1:
        lines += [f"The launcher recorded task {task_ids[0]} but status.json names "
                  f"{', '.join(task_ids[1:])}; worktrees of all of them are captured.", ""]
    entries: dict[str, dict] = {}
    try:
        for task_id in task_ids:
            for entry in entries_for_task(task_id, project):
                entries.setdefault(str(entry.get("path")), entry)
    except RegistryError as error:  # never abort a handoff on registry trouble
        return "\n".join(lines + [f"WARNING: the task worktree registry is unreadable, so no task worktree "
                                  f"was captured. {error}", ""])
    except Exception as error:  # noqa: BLE001
        return "\n".join(lines + [f"WARNING: the task worktree registry could not be read ({error}); "
                                  "no task worktree was captured.", ""])
    if not entries:
        return "\n".join(lines + [f"No task worktree is registered for task {' or '.join(task_ids)}. Diffs "
                                  "above cover only the launch directory. Create one with `syndicate tasks "
                                  "worktree` before further edits.", ""])
    lines.append("Continue the task in its worktree below, not in the launch directory. "
                 "Keep it locked; never unlock or remove it before the series is verified complete.")
    lines.append("")
    for entry in sorted(entries.values(), key=lambda e: str(e.get("path", ""))):
        owner = next((t for t in task_ids if t in (entry.get("task_ids") or [])), task_ids[0])
        try:
            lines.extend(_capture_one(target, entry, owner, run_id, project))
        except Exception as error:  # noqa: BLE001 - a handoff must still be written
            lines.append(f"- {entry.get('path')}: capture failed ({error})")
    lines.append("")
    return "\n".join(lines)


def _evidence_name(branch: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", slug(branch)).lstrip(".")
    return name or "worktree"


def _capture_one(target: Path, entry: dict, task_id: str, run_id: str, project: Path) -> list[str]:
    path = Path(entry.get("path", ""))
    branch = entry.get("branch") if isinstance(entry.get("branch"), str) else "unknown"
    valid_branch = bool(BRANCH_CHARS.fullmatch(branch)) and not branch.startswith("-")
    out = target / "worktrees" / _evidence_name(branch)
    suffix = 1
    while out.exists():  # the same branch name in another repository
        suffix += 1
        out = target / "worktrees" / f"{_evidence_name(branch)}-{suffix}"
    out.mkdir(parents=True, mode=0o700)
    repo = Path(entry.get("repo") or entry.get("git_common_dir") or path)
    records = []
    try:
        records = list_worktrees(repo)
    except WorktreeError:
        pass
    record = next((wt for wt in records if Path(wt.get("worktree", "")).resolve() == path.resolve()), None)
    exists = path.is_dir()
    series = entry.get("series") or branch
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
        if not upstream and valid_branch and _text(path, "rev-parse", "--verify", "--quiet", "--end-of-options",
                                                   f"refs/remotes/origin/{branch}"):
            upstream = f"origin/{branch}"
        base = entry.get("base")
        compare = upstream if upstream and _valid_revision(path, upstream) else None
        note = ""
        if not compare:
            note = " (no upstream; compared with base)"
            compare = base if _valid_revision(path, base) else None
        if compare:
            log = _capture(path, "log", "--oneline", "--no-decorate", "--end-of-options", f"{compare}..HEAD")
            write("unpushed.log", None if log is None else
                  f"# Commits on HEAD not in {compare}{note}\n".encode() + log)
        else:
            write("unpushed.log", f"# No upstream, and the registered base {base!r} is not a valid "
                                  "revision; unpushed commits were not computed.\n".encode())
        if valid_branch:
            ref, commit = _snapshot(path, out, branch, run_id, task_id)
            info.update(snapshot_ref=ref, snapshot_commit=commit)
    else:
        if valid_branch:
            info["head"] = _text(repo, "rev-parse", "--verify", "--quiet", "--end-of-options",
                                 f"refs/heads/{branch}")
        note = (f"Worktree directory {path} is missing"
                f"{'; git reports it prunable' if info['prunable'] else ''}. Its uncommitted work is not "
                "recoverable from here; check earlier snapshot refs.\n").encode()
        for name in ("git-status.txt", "worktree.diff", "staged.diff", "untracked.txt", "unpushed.log"):
            write(name, note)
    _atomic_json(out / "info.json", info)
    lines = [f"- Path: {path}", f"  Branch: {branch} (series {info['series']}, base {info['base']})",
             f"  HEAD: {info['head'] or 'unavailable'}; locked: {'yes' if info['locked'] else 'no'}",
             f"  Evidence: worktrees/{out.name}/"]
    if exists and record is not None and not info["locked"]:
        lines.append(f"  WARNING: this task worktree is NOT locked, so `git worktree prune` could drop it. "
                     f"Lock it again with `syndicate tasks worktree {q(task_id)} -C "
                     f"{q(entry.get('source') or project)} --repo {q(repo)} --branch {q(branch)} "
                     f"--series {q(series)}`.")
    commands = recovery_commands(repo, path, task_id, entry.get("source") or project, branch, series,
                                 info["snapshot_ref"] or f"{SNAPSHOT_REF}/{slug(branch)}/<id>")
    if not exists:
        lines.append(f"  MISSING: the directory does not exist{' (prunable)' if info['prunable'] else ''}. "
                     f"Recover it locked at the same path with `{commands[0]}`, then `{commands[1]}`, then, "
                     f"if `git -C {q(repo)} for-each-ref {SNAPSHOT_REF}/{slug(branch)}/` lists a snapshot, "
                     f"`git -C {q(path)} restore --source <latest snapshot ref> --worktree -- .`.")
    elif info["snapshot_ref"]:
        lines.append(f"  Snapshot: {info['snapshot_ref']} = {info['snapshot_commit']} "
                     "(tracked and untracked, non-ignored files)")
        lines.append(f"  If the directory is lost: `{commands[0]}`, then `{commands[1]}` (adds and locks it "
                     f"again), then `{commands[2]}`.")
    else:
        lines.append("  Snapshot: unavailable")
    return lines
