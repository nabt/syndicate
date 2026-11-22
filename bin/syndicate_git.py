"""Verified GitHub post-merge cleanup for the orchestrator closeout workflow."""
from __future__ import annotations

import json
import os
import re
import subprocess
import shlex
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Callable, ContextManager
from urllib.parse import urlsplit

MAIN = "refs/heads/main"
SYNDICATE_LOCK = "syndicate:"
ORIGIN_MAIN = "refs/remotes/origin/main"
OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
GIT_ROUTING_ENV = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CEILING_DIRECTORIES", "GIT_COMMON_DIR",
    "GIT_CONFIG", "GIT_DIR", "GIT_DISCOVERY_ACROSS_FILESYSTEM", "GIT_GRAFT_FILE",
    "GIT_IMPLICIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_NAMESPACE", "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX", "GIT_REPLACE_REF_BASE", "GIT_SHALLOW_FILE", "GIT_SSH",
    "GIT_SSH_COMMAND", "GIT_WORK_TREE",
}
# Keep the branch/head check at the same process boundary as the fast-forward.
# Git does not atomically bind a symbolic HEAD and the worktree update.
FAST_FORWARD_MAIN = """
branch=$(git --no-replace-objects symbolic-ref --quiet HEAD) || exit 1
head=$(git --no-replace-objects rev-parse --verify HEAD) || exit 1
if [ "$branch" != refs/heads/main ] || [ "$head" != "$1" ]; then
    echo 'Local main checkout changed before fast-forward' >&2
    exit 1
fi
exec git --no-replace-objects -c submodule.recurse=false merge --ff-only \
    --no-autostash --no-overwrite-ignore -- "$2"
"""


class CleanupError(Exception):
    """An operation was unsafe or could not be verified."""


def _run(path: Path, command: tuple[str, ...], expected=(0,)) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, cwd=path, text=True, errors="surrogateescape", stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CleanupError(f"Could not run {command[0]} {command[1]}: {exc}") from exc
    if result.returncode not in expected:
        detail = result.stderr.strip() or result.stdout.strip()
        raise CleanupError(f"{command[0]} {command[1]} failed: {detail or result.returncode}")
    return result


def _git(path: Path, *args: str, expected=(0,)) -> subprocess.CompletedProcess:
    return _run(path, ("git", "--no-replace-objects", *args), expected)


def _github_repository(url: str) -> str:
    if "://" in url:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise CleanupError("Invalid origin URL") from exc
        host, path = parsed.hostname, parsed.path
        if parsed.scheme not in ("https", "ssh"):
            raise CleanupError("Origin must use a GitHub HTTPS or SSH URL")
        if parsed.password is not None or (parsed.scheme == "https" and parsed.username is not None):
            raise CleanupError("Origin URL contains embedded credentials")
        if port is not None or parsed.query or parsed.fragment:
            raise CleanupError("Origin URL must use a standard GitHub endpoint without a port, query or fragment")
    else:
        match = re.fullmatch(r"(?:[^@/:]+@)?([^/:]+):(.+)", url)
        if not match:
            raise CleanupError("Origin must identify a GitHub repository")
        host, path = match.groups()
    path = path.strip("/").removesuffix(".git")
    if not host or not re.fullmatch(r"[A-Za-z0-9.-]+", host) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path
    ):
        raise CleanupError("Origin must identify a GitHub repository")
    return f"{host}/{path}"


def _worktrees(repo: Path) -> list[dict[str, str]]:
    records = _git(repo, "worktree", "list", "--porcelain", "-z").stdout.split("\0\0")
    return [dict(tuple(field.split(" ", 1)) if " " in field else (field, "")
                 for field in record.split("\0") if field)
            for record in records if record]


def _ancestor(repo: Path, earlier: str, later: str) -> bool:
    return _git(repo, "merge-base", "--is-ancestor", earlier, later,
                expected=(0, 1)).returncode == 0


def _ref_oid(repo: Path, ref: str) -> str | None:
    if _git(repo, "symbolic-ref", "-q", ref, expected=(0, 1)).returncode == 0:
        raise CleanupError(f"Ref {ref} is symbolic")
    if _git(repo, "show-ref", "--quiet", "--verify", ref, expected=(0, 1)).returncode == 1:
        return None
    return _git(repo, "show-ref", "--hash", "--verify", ref).stdout.strip()


def _clean(path: Path, label: str, *, ignored: bool = False) -> None:
    args = ("--ignored=matching",) if ignored else ()
    if _git(path, "--no-optional-locks", "status", "--porcelain", "--untracked-files=all",
            "--ignore-submodules=none", *args).stdout:
        kinds = "tracked, untracked or ignored" if ignored else "tracked or untracked"
        raise CleanupError(f"{label} is dirty ({kinds} changes): {path}")
    entries = _git(path, "ls-files", "-v", "-z").stdout.split("\0")
    if any(entry and (entry[0].islower() or entry[0] == "S") for entry in entries):
        raise CleanupError(f"{label} has assume-unchanged or skip-worktree index entries: {path}")
    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
                 "rebase-merge", "rebase-apply", "sequencer", "BISECT_START"):
        state = _git(path, "rev-parse", "--path-format=absolute", "--git-path", name)
        if Path(state.stdout.rstrip("\n")).exists():
            raise CleanupError(f"{label} has a Git operation in progress: {name}")


def _check_tip(repo: Path, tip: str | None, pr_head: str, main_tip: str, label: str) -> None:
    if tip and (not _ancestor(repo, tip, main_tip) or not _ancestor(repo, tip, pr_head)):
        raise CleanupError(f"{label} tip {tip} cannot be proven part of the merged PR history")


def _delete_remote_branch(repo: Path, push_url: str, task_ref: str, remote_tip: str) -> None:
    # Use a temporary remote name without fetch mappings. A named origin push
    # can automatically delete a mapped local ref when deleting a remote ref.
    # An explicit pushurl also avoids applying pushInsteadOf to a bare URL.
    target = f"syndicate-cleanup-{uuid.uuid4().hex}"
    options = ("-c", f"remote.{target}.url={push_url}",
               "-c", f"remote.{target}.pushurl={push_url}")
    # The equal url and explicit pushurl undergo the same insteadOf rewrite.
    # ls-remote recognizes a command-scoped remote and resolves it without I/O.
    effective_url = _git(repo, *options, "ls-remote", "--get-url", target).stdout.rstrip("\n")
    if effective_url != push_url:
        raise CleanupError("Cleanup push URL changed after applying Git URL rewrites")
    # The lease guards a deletion-only refspec against concurrent branch updates.
    _git(repo, *options, "push", "--no-follow-tags", "--recurse-submodules=no",
         f"--force-with-lease={task_ref}:{remote_tip}", target, f":{task_ref}")


def _check_main(path: Path, target: str) -> str:
    if _git(path, "symbolic-ref", "--quiet", "HEAD", expected=(0, 1)).stdout.strip() != MAIN:
        raise CleanupError(f"Local main is no longer checked out at {path}")
    _clean(path, "Local main checkout")
    head = _git(path, "rev-parse", "--verify", "HEAD").stdout.strip()
    if not _ancestor(path, head, target):
        raise CleanupError(f"Local main at {head} cannot fast-forward to origin/main at {target}")
    return head


def _check_task_worktree(repo: Path, path: Path, task_ref: str,
                         worktrees: list[dict[str, str]]) -> bool:
    if repo.resolve().is_relative_to(path) or Path.cwd().is_relative_to(path):
        raise CleanupError("Run cleanup from a checkout outside the task worktree")
    primary = Path(worktrees[0]["worktree"]).resolve()
    if path == primary:
        raise CleanupError("The task worktree must be an isolated linked worktree")
    task_record = next((wt for wt in worktrees if Path(wt["worktree"]).resolve() == path), None)
    registered = [wt for wt in worktrees if wt.get("branch") == task_ref]
    if task_record:
        # Syndicate locks every task worktree until its series is complete; accept only that lock.
        foreign_lock = "locked" in task_record and not task_record["locked"].startswith(SYNDICATE_LOCK)
        if registered != [task_record] or foreign_lock or "prunable" in task_record:
            raise CleanupError(f"Recorded task worktree is locked by another tool, prunable, "
                               f"or does not own {task_ref}")
        _clean(path, "Task worktree", ignored=True)
    elif registered or path.exists():
        raise CleanupError("Recorded task worktree is not registered to the task branch")
    return task_record is not None


def _remove_task_worktree(repo: Path, path: Path) -> None:
    record = next((wt for wt in _worktrees(repo) if Path(wt["worktree"]).resolve() == path), None)
    if record is None:
        raise CleanupError(f"Task worktree {path} is no longer registered; removal skipped")
    if "locked" not in record:
        _git(repo, "worktree", "remove", "--", str(path))
        return
    # Unlock only immediately before removal; restore the lock if removal fails.
    reason = record["locked"]
    _git(repo, "worktree", "unlock", "--", str(path))
    try:
        _git(repo, "worktree", "remove", "--", str(path))
    except CleanupError as error:
        relock = ("git", "-C", str(repo), "worktree", "lock", *(("--reason", reason) if reason else ()),
                  "--", str(path))
        try:
            _git(repo, *relock[3:])
        except CleanupError as lock_error:
            raise CleanupError(
                f"{error}. Re-locking failed too ({lock_error}): task worktree {path} is now UNLOCKED "
                f"and must be locked again before anything prunes it: {shlex.join(relock)}") from error
        raise CleanupError(f"{error}. The task worktree lock was restored") from error


def cleanup_pr(repo: Path, number: int, branch: str, path: Path,
               report: Callable[[str], None],
               series_guard: Callable[[Path], None] | None = None,
               removal_lock: Callable[[], ContextManager] | None = None) -> bool:
    """Clean up recorded task artifacts after proving a same-repository merge.

    series_guard(path) runs after the merge is verified and before any change;
    it raises CleanupError while other tasks of the worktree's series are
    unfinished. It runs again under removal_lock() (the registry lock, which
    task worktree adoption also holds) immediately before the worktree is
    unlocked and removed, so a task added to the series meanwhile stops removal.
    Returns True when cleanup ran, False when the PR is not merged."""
    overrides = sorted(key for key in os.environ
                       if key in GIT_ROUTING_ENV or key.startswith("GIT_CONFIG_"))
    if overrides:
        raise CleanupError(f"Git environment overrides are set: {', '.join(overrides)}")
    if number < 1:
        raise CleanupError("A positive PR number is required")
    if not branch or branch.startswith("-") or branch == "main" or branch.startswith("refs/"):
        raise CleanupError("A task branch name other than main is required")
    task_ref = f"refs/heads/{branch}"
    _git(repo, "check-ref-format", task_ref)
    origin_urls = _git(repo, "remote", "get-url", "--all", "origin").stdout.splitlines()
    if len(origin_urls) != 1:
        raise CleanupError("Expected exactly one origin fetch URL")
    repository = _github_repository(origin_urls[0])
    fields = "state,url,mergedAt,mergeCommit,headRefName,headRefOid,baseRefName,isCrossRepository"
    try:
        pr = json.loads(_run(repo, ("gh", "pr", "view", str(number), "--repo", repository,
                                    "--json", fields)).stdout)
        state = pr["state"]
        url = pr["url"]
    except (ValueError, TypeError, KeyError) as exc:
        raise CleanupError("Could not read PR state from GitHub") from exc
    if state != "MERGED":
        report(f"PR {url} is {state}. Post-merge cleanup skipped.")
        return False
    if pr.get("baseRefName") != "main" or pr.get("headRefName") != branch or (
        pr.get("isCrossRepository") is not False
    ):
        raise CleanupError("Merged PR does not match the recorded same-repository task branch and main base")
    merge = pr.get("mergeCommit")
    commit = merge.get("oid") if isinstance(merge, dict) else None
    pr_head = pr.get("headRefOid")
    if not all(isinstance(oid, str) and OID.fullmatch(oid) for oid in (commit, pr_head)):
        raise CleanupError("Merged PR is missing a valid merge commit or head commit")

    _ref_oid(repo, ORIGIN_MAIN)  # Reject symbolic tracking refs before fetching into them.
    _git(repo, "fetch", "--no-tags", "--no-prune", "--no-prune-tags", "--no-auto-maintenance",
         "--no-recurse-submodules", "--no-write-fetch-head",
         "--refmap=", "origin", f"{MAIN}:{ORIGIN_MAIN}")
    main_tip = _ref_oid(repo, ORIGIN_MAIN)
    if not main_tip or not _ancestor(repo, commit, main_tip):
        raise CleanupError(f"PR merge commit {commit} is absent from fetched origin/main")
    report(f"Verified PR {url} merged at {pr.get('mergedAt')} as {commit} on origin/main.")
    if series_guard is not None:
        series_guard(path.resolve())

    worktrees = _worktrees(repo)
    main_record = next((wt for wt in worktrees if wt.get("branch") == MAIN), None)
    if main_record is None:
        raise CleanupError("No local main checkout is registered")
    main_path = Path(main_record["worktree"])
    main_head = _check_main(main_path, main_tip)
    path = path.resolve()
    task_exists = _check_task_worktree(repo, path, task_ref, worktrees)

    local_tip = _ref_oid(repo, task_ref)
    _check_tip(repo, local_tip, pr_head, main_tip, "Local task branch")
    push_urls = _git(repo, "remote", "get-url", "--push", "--all", "origin").stdout.splitlines()
    if len(push_urls) != 1 or _github_repository(push_urls[0]).casefold() != repository.casefold():
        raise CleanupError("Origin push URL does not match the verified GitHub repository")
    remote = _git(repo, "ls-remote", "--refs", "--exit-code", "origin", task_ref, expected=(0, 2))
    remote_tip = None
    if remote.returncode == 0:
        parts = remote.stdout.strip().split("\t")
        if len(parts) != 2 or not OID.fullmatch(parts[0]) or parts[1] != task_ref:
            raise CleanupError(f"Could not verify remote task branch {task_ref}")
        remote_tip = parts[0]
    _check_tip(repo, remote_tip, pr_head, main_tip, "Remote task branch")

    if remote_tip:
        _delete_remote_branch(repo, push_urls[0], task_ref, remote_tip)
        report(f"Deleted remote task branch {branch}.")
    else:
        report(f"Remote task branch {branch} was already absent.")
    if task_exists:
        if not _check_task_worktree(repo, path, task_ref, _worktrees(repo)) or (
            _ref_oid(repo, task_ref) != local_tip
        ):
            raise CleanupError("Task worktree or branch changed during cleanup")
        with removal_lock() if removal_lock is not None else nullcontext():
            if series_guard is not None:
                series_guard(path)
            _remove_task_worktree(repo, path)
        report(f"Removed task worktree {path}.")
    if local_tip:
        if any(wt.get("branch") == task_ref for wt in _worktrees(repo)):
            raise CleanupError(f"Local task branch {branch} is checked out")
        # Compare-and-delete the inspected ref without relying on its upstream.
        _git(repo, "update-ref", "--no-deref", "-d", task_ref, local_tip)
        report(f"Deleted local task branch {branch}.")
    if _check_main(main_path, main_tip) != main_head or _ref_oid(repo, ORIGIN_MAIN) != main_tip:
        raise CleanupError("Main or origin/main changed during cleanup. Main synchronization skipped")
    if main_head != main_tip:
        _run(main_path, ("sh", "-c", FAST_FORWARD_MAIN, "syndicate-fast-forward", main_head, main_tip))
        report(f"Fast-forwarded local main to {main_tip}.")
    else:
        report(f"Local main is already at {main_tip}.")
    return True
