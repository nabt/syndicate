"""Per-user Syndicate release installer: stage, verify, activate, roll back.

Normal installs and upgrades come only from an explicitly approved release tag
whose commit is reachable from a freshly fetched upstream main. A release is
built with ``git archive`` from a release-source cache outside any project
worktree, so a mutable checkout never becomes release input.

Layout (XDG data home, default ~/.local/share):

    syndicate/releases/<commit>/        immutable release root (contains bin/syndicate, RELEASE.json)
    syndicate/current -> releases/<c>    active release, switched with os.replace
    syndicate/interpreters/<commit>      absolute interpreter the shim uses for that release
    syndicate/activations.jsonl          append-only activation log
    syndicate/entrypoints.jsonl          append-only record of replaced entry points
    syndicate/entrypoint-backups/        copies of replaced regular-file entry points
    syndicate/activation.lock            flock serializing publish/activate/prune/hook migration
    syndicate/in-use/<commit>/<pid>      liveness markers written by installed launchers
    syndicate/releases/.trash-<pid>-<c>  a release being removed by prune (finished by the next prune)

The stable entry point ($HOME/.local/bin/syndicate) is a POSIX sh shim that
resolves ``current`` once and execs the recorded interpreter with -I -B.

Standard library only. See docs/user-install.md.
"""
from __future__ import annotations

import argparse
import ast
import fcntl
import grp
import hashlib
import io
import json
import os
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

FORMAT = 1
PYTHON_MIN = (3, 11)
PYTHON_MIN_TEXT = "3.11"
TAG_PATTERN = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
MAIN_BRANCH = "main"
REMOTE = "upstream"
FETCHED_MAIN = "refs/syndicate/upstream/main"
FETCHED_TAGS = "refs/syndicate/upstream/tags/"
MANIFEST = "RELEASE.json"
SHIM_MARKER = "# syndicate-install managed shim v1"
STAGING_PREFIX = ".staging-"
TRASH_PREFIX = ".trash-"
PROC = Path("/proc")
# A release path anywhere in a command line or environment value. Matched
# without the install root, so a symlinked or differently spelled root (or the
# release path inside a longer argument) still counts; over-matching only keeps
# a release longer.
RELEASE_REFERENCE = re.compile(rb"releases/([0-9a-f]{40})(?![0-9a-f])")
SMOKE_TIMEOUT = 120

# Release allowlist. Directories are shipped recursively; bin modules are
# globbed at the tag so new sibling modules ship without editing this list.
ALLOWED_DIRS = ("roles/", "skills/", "hooks/", "web/", "docs/", "resources/")
ALLOWED_FILES = ("bin/syndicate", "bin/syndicate-install", "portfolio/chief-of-staff.md", "README.md")
BIN_MODULE = re.compile(r"^bin/syndicate_[A-Za-z0-9_]+\.py$")
# Required runtime assets beyond "every shipped role/skill is well formed".
REQUIRED_FILES = (
    "bin/syndicate",
    "hooks/require-review.sh",
    "web/dashboard.html",
    "portfolio/chief-of-staff.md",
    "README.md",
    "roles/orchestrator.md",
    "roles/code-reviewer.md",
    "skills/syndicate-project/SKILL.md",
    "skills/syndicate-handoff/SKILL.md",
    "skills/syndicate-closeout/SKILL.md",
    "skills/syndicate-inbox/SKILL.md",
)


class ReleaseError(Exception):
    pass


# --- paths ---------------------------------------------------------------

class Paths:
    """Every location the installer touches; all overridable for tests."""

    def __init__(self, root: Path, bin_dir: Path, cache: Path, default_cache: bool = False):
        # Canonical once: the shim, launcher and markers all use the real path,
        # so a symlinked HOME or XDG_DATA_HOME must compare equal to them.
        self.root = Path(os.path.realpath(root))
        self.bin_dir = Path(os.path.abspath(bin_dir))
        self.cache = Path(os.path.abspath(cache))
        # Only Syndicate's own cache directory ($XDG_CACHE_HOME/syndicate) is
        # tightened when it already exists; the parent of a --source-cache the
        # operator chose is checked, never chmodded.
        self.default_cache = default_cache

    @classmethod
    def from_env(cls, root=None, bin_dir=None, cache=None, environ=None) -> "Paths":
        env = os.environ if environ is None else environ
        home = Path(env.get("HOME") or Path.home())
        data = Path(env["XDG_DATA_HOME"]) if env.get("XDG_DATA_HOME") else home / ".local" / "share"
        cache_home = Path(env["XDG_CACHE_HOME"]) if env.get("XDG_CACHE_HOME") else home / ".cache"
        return cls(Path(root).expanduser() if root else data / "syndicate",
                   Path(bin_dir).expanduser() if bin_dir else home / ".local" / "bin",
                   Path(cache).expanduser() if cache else cache_home / "syndicate" / "source.git",
                   default_cache=not cache)

    releases = property(lambda self: self.root / "releases")
    current = property(lambda self: self.root / "current")
    interpreters = property(lambda self: self.root / "interpreters")
    log = property(lambda self: self.root / "activations.jsonl")
    entrypoints = property(lambda self: self.root / "entrypoints.jsonl")
    entrypoint_backups = property(lambda self: self.root / "entrypoint-backups")
    lock = property(lambda self: self.root / "activation.lock")
    in_use = property(lambda self: self.root / "in-use")
    shim = property(lambda self: self.bin_dir / "syndicate")

    def release(self, commit: str) -> Path:
        if not COMMIT_PATTERN.match(commit):
            raise ReleaseError(f"not a full commit id: {commit!r}")
        return self.releases / commit


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def log(message: str) -> None:
    print(f"syndicate-install: {message}", file=sys.stderr, flush=True)


# --- git -----------------------------------------------------------------

def git_env() -> dict:
    # A caller inside a Git hook must not redirect us to its repository.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    for key in ("GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


# Every command against the release-source cache: object replacement
# (refs/replace/*), the commit-graph and hooks are ignored, so what ls-tree and
# archive read is exactly the object rev-parse names; nothing writes a commit
# graph or runs automatic maintenance.
CACHE_GIT_OPTIONS = ("--no-replace-objects", "-c", "core.commitGraph=false", "-c", "core.hooksPath=/dev/null",
                     "-c", "fetch.writeCommitGraph=false", "-c", "gc.auto=0", "-c", "maintenance.auto=false")
# The only configuration a cache may carry, each key at most once: what
# `git init --bare --template=` and `git remote add` write. Anything else
# (remote.upstream.uploadpack, proxy or vcs, url.*.insteadOf, core.sshCommand,
# include.path, ...) could change what a fetch runs or where it reads from.
# Keys are compared exactly as `git config --list` prints them (section and
# name lowercased, the subsection as written). Fetches name the recorded URL
# explicitly rather than the remote, so remote.upstream.* is only a record.
CACHE_CONFIG = {
    "core.repositoryformatversion": None, "core.filemode": None, "core.bare": ("true",),
    "core.ignorecase": None, "core.precomposeunicode": None, "core.symlinks": None,
    "core.logallrefupdates": None, "extensions.objectformat": ("sha1",),
    "extensions.refstorage": ("files", "reftable"),
    f"remote.{REMOTE}.url": None, f"remote.{REMOTE}.fetch": (f"+refs/heads/*:refs/remotes/{REMOTE}/*",),
}
# Files that make Git read history other than the objects named: grafts and a
# shallow list rewrite parents; alternates add object stores.
CACHE_FORBIDDEN_FILES = ("info/grafts", "shallow", "objects/info/alternates", "objects/info/http-alternates")
# Entries that point Git at another directory's objects, refs, configuration,
# grafts, shallow list and alternates: commondir and gitdir (as in a linked
# worktree's administrative directory), and a .git, which repository discovery
# prefers to the bare directory itself.
CACHE_REDIRECT_ENTRIES = ("commondir", "gitdir", ".git")
DIGITS = re.compile(r"[0-9]+")


def git(repo: Path, *args: str, binary: bool = False, check: bool = True, options: tuple = ()):
    result = subprocess.run(["git", *options, "-C", str(repo), *args], capture_output=True, env=git_env(),
                            stdin=subprocess.DEVNULL)
    if check and result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        name = next((a for a in args if not a.startswith("-") and "=" not in a), args[0])
        raise ReleaseError(f"git {name} failed: {detail}")
    if binary:
        return result
    result.stdout = result.stdout.decode(errors="replace")
    return result


def cache_git(cache: Path, *args: str, **kwargs):
    """git against the release-source cache, with CACHE_GIT_OPTIONS."""
    return git(cache, *args, options=CACHE_GIT_OPTIONS, **kwargs)


def tighten(path: Path, mask: int) -> None:
    """Remove permission bits outside mask from a directory this user owns; never add any."""
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise ReleaseError(f"{path} is a symlink; refusing to use it")
    if st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) & ~mask:
        os.chmod(path, stat.S_IMODE(st.st_mode) & mask)


def check_cache(paths: Paths) -> dict[str, str]:
    """Refuse a release-source cache another account can change, or one whose
    layout, configuration or history redirection could make it read something
    other than the fetched objects (an entry redirecting it to another
    directory, a symlink, replace refs, grafts, a shallow list, alternates,
    unexpected or repeated configuration). Returns its configuration."""
    cache = paths.cache
    tighten(cache, 0o700)
    for path in (cache.parent, cache):
        problem = writable_by_others(path)
        if problem:
            raise ReleaseError(f"release-source cache directory {path} is {problem}; refusing to use it")
    remove = f"remove the cache ({cache}) and install again"
    uid = os.getuid()
    for entry in os.scandir(cache):
        if entry.name in CACHE_REDIRECT_ENTRIES:
            raise ReleaseError(f"release-source cache {cache} has {entry.name}, which points Git at another "
                               f"directory's objects, refs and configuration; {remove}")
        owner = entry.stat(follow_symlinks=False).st_uid
        if owner != uid:
            raise ReleaseError(f"release-source cache entry {entry.path} is owned by uid {owner}, not by you; {remove}")
    # Git never creates symlinks in a bare repository; one (objects, refs,
    # info, config, packed-refs, a pack directory, ...) would let content
    # outside the cache stand in for it.
    for directory, dirs, names in os.walk(cache):
        for name in dirs + names:
            if os.path.islink(os.path.join(directory, name)):
                raise ReleaseError(f"release-source cache {cache} has a symlink "
                                   f"{os.path.relpath(os.path.join(directory, name), cache)}; {remove}")
    for name in CACHE_FORBIDDEN_FILES:
        if os.path.lexists(cache / name):
            raise ReleaseError(f"release-source cache {cache} has {name}, which changes what Git reads; {remove}")
    replaced = cache_git(cache, "for-each-ref", "--format=%(refname)", "refs/replace/").stdout.split()
    if replaced or os.path.lexists(cache / "refs" / "replace") and any((cache / "refs" / "replace").iterdir()):
        raise ReleaseError(f"release-source cache {cache} has replace refs ({', '.join(replaced) or 'refs/replace/'}), "
                           f"which substitute objects; {remove}")
    listing = cache_git(cache, "config", "--local", "--no-includes", "--list", "-z", binary=True).stdout
    config: dict[str, str] = {}
    for item in listing.split(b"\0"):
        if not item:
            continue
        key, _, value = item.decode(errors="replace").partition("\n")
        allowed_values = CACHE_CONFIG.get(key, ())
        if key not in CACHE_CONFIG or (allowed_values is not None and value not in allowed_values):
            raise ReleaseError(f"release-source cache {cache} has unexpected configuration {key}={value!r}; "
                               f"only {', '.join(sorted(CACHE_CONFIG))} (with their default values) are allowed. "
                               f"Remove the cache ({cache}) and install again")
        if key in config:
            raise ReleaseError(f"release-source cache {cache} sets {key} more than once, so Git commands could "
                               f"disagree about its value; {remove}")
        config[key] = value
    return config


def check_upstream_form(upstream: str) -> None:
    """An upstream is an absolute path or a URL (scheme:// or scp-like host:path),
    never something Git could read as an option or a remote name."""
    if (not upstream or upstream.startswith("-") or any(c in upstream for c in "\n\r\0")
            or not (upstream.startswith("/") or ":" in upstream)):
        raise ReleaseError(f"invalid upstream {upstream!r}: use an absolute path or a URL")


def local_upstream_path(upstream: str) -> str | None:
    """The filesystem path of a local upstream (a path or file:// URL), else None."""
    if upstream.startswith("file://"):
        return upstream[len("file://"):] or None
    if "://" not in upstream and os.path.exists(upstream):
        return upstream
    return None


def check_local_upstream(upstream: str) -> None:
    """A local upstream must be a bare repository with no linked worktrees.

    A working repository (or one with linked worktrees) has a checked-out main
    and tags that agents working in it can move, so it is not a trusted source."""
    path = local_upstream_path(upstream)
    if path is None:
        return
    bare = git(Path(path), "rev-parse", "--is-bare-repository", check=False)
    if bare.returncode or bare.stdout.strip() != "true":
        raise ReleaseError(f"local upstream {upstream} is not a bare repository; agents working in a checkout "
                           "can move its main and tags. Use a bare mirror or the remote URL")
    common = git(Path(path), "rev-parse", "--git-common-dir", check=False).stdout.strip()
    worktrees = Path(path, common or ".", "worktrees")
    if worktrees.is_dir() and any(worktrees.iterdir()):
        raise ReleaseError(f"local upstream {upstream} has linked worktrees ({worktrees}); "
                           "their branches and tags can move under agents' hands. Use a bare mirror without worktrees")


def ensure_cache(paths: Paths, upstream: str | None, change_upstream: bool = False) -> tuple[str, str | None]:
    """Create the release-source cache; return (upstream URL, previous URL if it changed).

    The returned URL is what the caller fetches from, by URL: the operator's
    --upstream, or the single recorded remote.upstream.url compared exactly."""
    if upstream is not None:
        if upstream and os.path.isdir(upstream):
            upstream = os.path.abspath(upstream)
        check_upstream_form(upstream)
    cache = paths.cache
    if not os.path.lexists(cache / "HEAD"):
        if not upstream:
            raise ReleaseError(f"no release-source cache at {cache}; pass --upstream URL|PATH")
        if os.path.lexists(cache) and (cache.is_symlink() or not cache.is_dir() or any(cache.iterdir())):
            raise ReleaseError(f"{cache} exists but is not a release-source cache; "
                               "name a new or empty directory with --source-cache")
        # No template: a configured init.templateDir could add hooks, grafts or config.
        result = subprocess.run(["git", *CACHE_GIT_OPTIONS, "init", "--quiet", "--bare", "--template=",
                                 str(cache)], capture_output=True, text=True, env=git_env(),
                                stdin=subprocess.DEVNULL)
        if result.returncode:
            raise ReleaseError(f"cannot create release-source cache: {result.stderr.strip()}")
        os.chmod(cache, 0o700)
    configured = check_cache(paths).get(f"remote.{REMOTE}.url")
    if upstream:
        check_local_upstream(upstream)
        if configured is None:
            cache_git(cache, "remote", "add", REMOTE, upstream)
        elif configured != upstream:
            if not change_upstream:
                raise ReleaseError(f"the recorded trusted upstream is {configured!r}; refusing to switch to "
                                   f"{upstream!r} without --change-upstream")
            log(f"changing trusted upstream from {configured} to {upstream}")
            cache_git(cache, "remote", "set-url", REMOTE, upstream)
            check_cache(paths)
            return upstream, configured
        check_cache(paths)
        return upstream, None
    if not configured:
        raise ReleaseError(f"release-source cache {cache} has no upstream; pass --upstream URL|PATH")
    check_upstream_form(configured)
    check_local_upstream(configured)
    return configured, None


def check_tag_name(tag: str) -> None:
    if not TAG_PATTERN.match(tag or ""):
        raise ReleaseError(f"{tag!r} is not an approved release tag name (expected vMAJOR.MINOR.PATCH); "
                           "branches, commit ids and other refs are not installable. "
                           "Use 'syndicate dev --checkout PATH' to run an unmerged checkout.")


def resolve_approved_tag(paths: Paths, tag: str, upstream: str | None, change_upstream: bool = False) -> dict:
    """Fetch upstream main and the named tag afresh; return the approved commit.

    Approved means: the operator named the tag exactly; it matches vMAJOR.MINOR.PATCH;
    it exists upstream right now; it peels to a commit; and that commit is an
    ancestor of (or equal to) upstream main as fetched by this same invocation.
    """
    check_tag_name(tag)
    with source_lock(paths):
        return _resolve_approved_tag(paths, tag, upstream, change_upstream)


def fetch_release_source(paths: Paths, tag: str, upstream: str | None, change_upstream: bool = False,
                         allow_retag: bool = False) -> tuple[dict, dict, bytes]:
    """Approve the tag and build its archive under one source lock.

    Holding the lock through `git archive` keeps a concurrent installer's fetch
    (which deletes and rewrites the fetched refs) from making the approved
    commit's objects unreachable, and so collectable, while it is read."""
    check_tag_name(tag)
    with source_lock(paths):
        approved = _resolve_approved_tag(paths, tag, upstream, change_upstream)
        check_retag(paths, approved, allow_retag)
        entries = tree_entries(paths.cache, approved["commit"])
        archive = build_archive(paths.cache, approved["commit"], entries)
    return approved, entries, archive


def check_retag(paths: Paths, approved: dict, allow_retag: bool) -> None:
    """Refuse a tag name the log recorded at another commit (a moved tag) unless allowed."""
    earlier = sorted({r.get("commit") for r in read_jsonl(paths.log)
                      if r.get("tag") == approved["tag"] and r.get("commit") != approved["commit"]
                      and isinstance(r.get("commit"), str)})
    if not earlier:
        return
    if not allow_retag:
        raise ReleaseError(f"tag {approved['tag']} now names {approved['commit']}, but it was installed earlier as "
                           f"{', '.join(earlier)}; a moved release tag is refused without --allow-retag")
    approved["retagged_from"] = earlier
    log(f"tag {approved['tag']} moved from {', '.join(earlier)} to {approved['commit']} (--allow-retag)")


@contextmanager
def source_lock(paths: Paths):
    """Serialize fetches into the shared release-source cache."""
    prepare_cache_parent(paths)
    with open_lock(paths.cache.with_name(paths.cache.name + ".lock")) as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _resolve_approved_tag(paths: Paths, tag: str, upstream: str | None, change_upstream: bool = False) -> dict:
    url, previous_url = ensure_cache(paths, upstream, change_upstream)
    tag_ref = FETCHED_TAGS + tag
    # Forget previous fetch results first so a failed fetch can never fall back to stale refs.
    for ref in (FETCHED_MAIN, tag_ref):
        cache_git(paths.cache, "update-ref", "-d", ref, check=False)
    # Fetch from the trusted URL itself, never through the remote's
    # configuration, so no cache setting can redirect it. --refmap= : only the
    # refspecs named here are written.
    fetched = cache_git(paths.cache, "-c", "fetch.fsckObjects=true", "fetch", "--quiet", "--no-tags", "--refmap=",
                        "--no-write-fetch-head", "--", url, f"+refs/heads/{MAIN_BRANCH}:{FETCHED_MAIN}", check=False)
    if fetched.returncode:
        raise ReleaseError(f"cannot fetch {MAIN_BRANCH} from upstream {url}: {fetched.stderr.strip()}; "
                           "nothing was installed (a fresh fetch is required)")
    fetched = cache_git(paths.cache, "-c", "fetch.fsckObjects=true", "fetch", "--quiet", "--no-tags", "--refmap=",
                        "--no-write-fetch-head", "--", url, f"+refs/tags/{tag}:{tag_ref}", check=False)
    if fetched.returncode:
        raise ReleaseError(f"tag {tag} was not found at upstream {url}: {fetched.stderr.strip()}")
    if check_cache(paths).get(f"remote.{REMOTE}.url") != url:
        raise ReleaseError(f"the release-source cache's recorded upstream changed during the fetch from {url}; "
                           "nothing was installed")
    kind = cache_git(paths.cache, "cat-file", "-t", tag_ref).stdout.strip()
    peeled = cache_git(paths.cache, "rev-parse", "--verify", "--quiet", f"{tag_ref}^{{commit}}", check=False)
    commit = peeled.stdout.strip()
    if peeled.returncode or not COMMIT_PATTERN.match(commit):
        raise ReleaseError(f"tag {tag} does not point at a commit")
    main = cache_git(paths.cache, "rev-parse", "--verify", f"{FETCHED_MAIN}^{{commit}}").stdout.strip()
    reachable = cache_git(paths.cache, "merge-base", "--is-ancestor", commit, main, check=False)
    if reachable.returncode == 1:
        raise ReleaseError(f"tag {tag} ({commit}) is not reachable from upstream {MAIN_BRANCH} ({main}); "
                           "only tags on upstream main are installable")
    if reachable.returncode:
        raise ReleaseError(f"cannot check that {commit} is on upstream {MAIN_BRANCH}: {reachable.stderr.strip()}")
    approved = {"tag": tag, "commit": commit, "upstream": url, "source_ref": f"refs/tags/{tag}",
                "tag_kind": "annotated" if kind == "tag" else "lightweight", "upstream_main": main}
    if previous_url:
        approved["upstream_changed_from"] = previous_url
    return approved


# --- artifact ------------------------------------------------------------

def allowed(path: str) -> bool:
    if "__pycache__" in path.split("/") or path.endswith((".pyc", ".pyo")):
        return False
    return path in ALLOWED_FILES or path.startswith(ALLOWED_DIRS) or bool(BIN_MODULE.match(path))


def tree_entries(cache: Path, commit: str) -> dict[str, tuple[str, str]]:
    """{path: (mode, blob id)} for allowlisted paths at the commit."""
    out = cache_git(cache, "ls-tree", "-r", "-z", "--full-tree", commit, binary=True).stdout
    entries = {}
    for record in out.split(b"\0"):
        if not record:
            continue
        meta, _, name = record.partition(b"\t")
        try:
            mode, kind, obj = meta.decode("ascii").split()
            path = name.decode("utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError):
            raise ReleaseError(f"commit {commit} has a tree entry {name!r} that is not a UTF-8 path; "
                               "releases ship UTF-8 paths only") from None
        parts = path.split("/")
        if any(part in ("", ".", "..") or part.lower() == ".git" for part in parts):
            raise ReleaseError(f"commit {commit} has an unsafe path {path!r} ('.', '..', '.git' or an empty "
                               "component); refusing to build a release from it")
        if not allowed(path):
            continue
        if kind != "blob" or mode not in ("100644", "100755"):
            raise ReleaseError(f"{path} at {commit} is a {kind} with mode {mode}; releases ship regular files only")
        entries[path] = (mode, obj)
    if "bin/syndicate" not in entries:
        raise ReleaseError(f"commit {commit} has no bin/syndicate; it is not a Syndicate release")
    return entries


def git_blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def executable_rule(path: str) -> bool:
    """Files that must be executable in every release."""
    return path in ("bin/syndicate", "bin/syndicate-install") or (path.startswith("hooks/") and path.endswith(".sh"))


def build_archive(cache: Path, commit: str, entries: dict) -> bytes:
    # Literal pathspecs: a shipped name with glob characters selects only itself.
    result = cache_git(cache, "--literal-pathspecs", "archive", "--format=tar", commit, "--", *sorted(entries),
                       binary=True)
    return result.stdout


def extract(archive: bytes, entries: dict, stage: Path) -> dict[str, str]:
    """Extract exactly the expected regular files; return {path: sha256}."""
    files = {}
    try:
        _extract(archive, entries, stage, files)
    except tarfile.TarError as error:
        raise ReleaseError(f"the release archive is not a valid tar file: {error}") from None
    missing = sorted(set(entries) - set(files))
    if missing:
        raise ReleaseError(f"archive is missing {', '.join(missing)} (export-ignore attributes are not supported)")
    return files


def _extract(archive: bytes, entries: dict, stage: Path, files: dict) -> None:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        for member in tar:
            name = member.name.rstrip("/")
            if member.isdir():
                continue
            if not member.isreg():
                raise ReleaseError(f"archive member {name} is not a regular file")
            if name not in entries or name in files:
                raise ReleaseError(f"unexpected archive member {name}")
            data = tar.extractfile(member).read()
            mode, blob = entries[name]
            if git_blob_id(data) != blob:
                # export-subst or a filter changed the bytes; the release must equal the commit.
                raise ReleaseError(f"archived {name} differs from the committed blob")
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(target, 0o755 if mode == "100755" else 0o644)
            files[name] = sha256_bytes(data)


def manifest_bytes(commit: str, files: dict) -> bytes:
    data = {"format": FORMAT, "commit": commit, "files": dict(sorted(files.items())), "python_min": PYTHON_MIN_TEXT}
    return (json.dumps(data, indent=2, sort_keys=False) + "\n").encode()


LINK = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def verify_assets(stage: Path, files: dict) -> None:
    """Required assets, executable modes, imports and internal documentation links."""
    problems = []
    for path in REQUIRED_FILES:
        if path not in files:
            problems.append(f"missing required {path}")
    for path in files:
        if executable_rule(path) and not os.access(stage / path, os.X_OK):
            problems.append(f"{path} is not executable")
    for path in files:
        if path.startswith("roles/") and path.endswith(".md") and not (stage / path).read_text(encoding="utf-8").strip():
            problems.append(f"role {path} is empty")
    skill_dirs = {p.split("/")[1] for p in files if p.startswith("skills/") and p.count("/") >= 2}
    for skill in sorted(skill_dirs):
        if f"skills/{skill}/SKILL.md" not in files:
            problems.append(f"skill {skill} has no SKILL.md")
    # Every sibling module the launcher imports, and every module the identity module lists.
    launcher = (stage / "bin" / "syndicate").read_text(encoding="utf-8") if "bin/syndicate" in files else ""
    for module in sorted(set(re.findall(r"^\s*import (syndicate_[A-Za-z0-9_]+)", launcher, re.M))
                         | set(re.findall(r"^\s*from (syndicate_[A-Za-z0-9_]+) import", launcher, re.M))
                         | set(runtime_modules(stage))):
        if f"bin/{module}.py" not in files:
            problems.append(f"bin/{module}.py is imported by the launcher but not shipped")
    for path in files:
        if not path.endswith(".md"):
            continue
        text = (stage / path).read_text(encoding="utf-8")
        for target in LINK.findall(text):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("#"):
                continue
            relative = target.split("#", 1)[0]
            if not relative or relative.startswith("/"):
                continue
            resolved = os.path.normpath(os.path.join(os.path.dirname(path), relative))
            if resolved.startswith("..") or not (resolved in files or any(f.startswith(resolved + "/") for f in files)):
                problems.append(f"{path} links to {target}, which is not in the release")
    if problems:
        raise ReleaseError("release assets failed verification:\n  " + "\n  ".join(problems))


def runtime_modules(root: Path) -> list[str]:
    """RUNTIME_MODULES from bin/syndicate_identity.py, read without executing it."""
    path = root / "bin" / "syndicate_identity.py"
    if not path.is_file():
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as error:
        raise ReleaseError(f"bin/syndicate_identity.py does not parse: {error}") from None
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == "RUNTIME_MODULES" for t in targets) and node.value is not None:
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                return []
            names = []
            for item in value:
                name = Path(str(item)).name
                names.append(name[:-3] if name.endswith(".py") else name)
            return [n for n in names if n.startswith("syndicate_")]
    return []


def set_read_only(root: Path) -> None:
    for directory, dirs, names in os.walk(root, topdown=False):
        for name in names:
            path = Path(directory) / name
            os.chmod(path, 0o555 if os.stat(path).st_mode & 0o100 else 0o444)
        os.chmod(directory, 0o555)


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_tree(root: Path) -> None:
    """fsync every directory of a staged tree (its files were fsynced as written)."""
    for directory, _, _ in os.walk(root, topdown=False):
        fsync_dir(Path(directory))


def make_writable(root: Path) -> None:
    if not root.exists() or root.is_symlink():
        return
    os.chmod(root, 0o700)
    for directory, dirs, names in os.walk(root):
        for name in dirs:
            path = Path(directory) / name
            if not path.is_symlink():
                os.chmod(path, 0o700)


def remove_tree(root: Path) -> None:
    if root.is_symlink():
        root.unlink()
        return
    if root.exists():
        make_writable(root)
        shutil.rmtree(root)


def read_manifest(release: Path) -> tuple[dict, bytes]:
    path = release / MANIFEST
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ReleaseError(f"cannot read {path}: {error}") from None
    try:
        data = json.loads(raw)
    except ValueError as error:
        raise ReleaseError(f"{path} is not valid JSON: {error}") from None
    if (not isinstance(data, dict) or data.get("format") != FORMAT
            or not isinstance(data.get("commit"), str) or not COMMIT_PATTERN.match(data["commit"])
            or not isinstance(data.get("files"), dict) or not data["files"]
            or not isinstance(data.get("python_min"), str)
            or not all(isinstance(k, str) and isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v)
                       for k, v in data["files"].items())):
        raise ReleaseError(f"{path} is malformed (expected format {FORMAT} with commit, files and python_min)")
    return data, raw


def verify_release(release: Path, commit: str | None = None) -> dict:
    """Recheck a release directory against its RELEASE.json. Returns the manifest."""
    if release.is_symlink():
        raise ReleaseError(f"{release} is a symlink, not a release directory")
    data, raw = read_manifest(release)
    if commit and data["commit"] != commit:
        raise ReleaseError(f"{release} holds commit {data['commit']}, expected {commit}")
    if release.name != data["commit"] and not release.name.startswith(STAGING_PREFIX):
        raise ReleaseError(f"{release} is not named after its commit {data['commit']}")
    problems = []
    present = set()
    for directory, dirs, names in os.walk(release):
        for name in dirs + names:
            path = Path(directory) / name
            rel = path.relative_to(release).as_posix()
            if path.is_symlink():
                problems.append(f"{rel} is a symlink")
            elif path.is_file():
                present.add(rel)
            elif not path.is_dir():
                problems.append(f"{rel} is not a regular file")
    expected = set(data["files"]) | {MANIFEST}
    for rel in sorted(present - expected):
        problems.append(f"unexpected file {rel}")
    for rel in sorted(expected - present):
        problems.append(f"missing {rel}")
    for rel, digest in sorted(data["files"].items()):
        if rel in present and sha256_file(release / rel) != digest:
            problems.append(f"{rel} does not match its recorded sha256")
        if rel in present and executable_rule(rel) and not os.access(release / rel, os.X_OK):
            problems.append(f"{rel} is not executable")
    if problems:
        raise ReleaseError(f"release {release} failed verification:\n  " + "\n  ".join(problems))
    data["manifest_sha256"] = sha256_bytes(raw)
    return data


# --- interpreter and smoke test ------------------------------------------

def _private_group(gid: int) -> bool:
    """True when no account other than the current user belongs to group gid."""
    uid = os.getuid()
    try:
        me = pwd.getpwuid(uid).pw_name
        members = grp.getgrgid(gid).gr_mem
        if any(member != me for member in members):
            return False
        return not any(user.pw_gid == gid and user.pw_uid != uid for user in pwd.getpwall())
    except (KeyError, OSError):
        return False


def writable_by_others(path: str | os.PathLike) -> str | None:
    """Why someone other than this user (or root) could change path; None when only they can."""
    st = os.stat(path)
    mode = stat.S_IMODE(st.st_mode)
    if st.st_uid not in (0, os.getuid()):
        return f"owned by uid {st.st_uid}"
    if mode & 0o002:
        return "world-writable"
    if mode & 0o020 and not _private_group(st.st_gid):
        return "group-writable"
    return None


def redirect_problem(path: str, lookups: list[str] | None = None) -> str | None:
    """Why another account could make path name a different file; None when none can.

    Follows path one component at a time, through every symlink, and checks each
    directory an entry is looked up in: one another account can write could
    replace that entry, unless sticky_protects it (as in /tmp). Then the final
    file itself must be writable only by this user or root. Each (symlink-free)
    directory an entry is looked up in is appended to lookups, when given."""
    pending = list(reversed(path.split("/")))
    current, links = "/", 0
    while pending:
        part = pending.pop()
        if part in ("", "."):
            continue
        if part == "..":
            current = os.path.dirname(current)
            continue
        entry = os.path.join(current, part)
        if lookups is not None and current not in lookups:
            lookups.append(current)
        problem = writable_by_others(current)
        if problem and not sticky_protects(current, entry):
            return f"{current} (which holds {entry}) is {problem}"
        if os.path.islink(entry):
            links += 1
            if links > 40:
                return f"{path} has too many levels of symbolic links"
            target = os.readlink(entry)
            if target.startswith("/"):
                current = "/"
            pending.extend(reversed(target.split("/")))
            continue
        current = entry
    problem = writable_by_others(current)
    return f"{current} is {problem}" if problem else None


def is_git_marker(path: Path) -> bool:
    """True when path is a real .git: a directory with HEAD, or a 'gitdir:' file.

    An empty .git directory is not a repository; Codex's sandbox creates such
    placeholders (for example /tmp/.git) on the host while a sandboxed command
    runs, and they must not make every path below them look like a work tree."""
    try:
        if path.is_dir():
            return os.path.lexists(path / "HEAD")
        if path.is_file():
            with open(path, "rb") as handle:
                return handle.read(7) == b"gitdir:"
    except OSError:
        return True  # unreadable: assume it is one
    return False


def inside_git_worktree(path: str) -> str | None:
    """The Git work tree (or repository) directory containing path, if any."""
    directory = Path(path)
    for candidate in (directory, *directory.parents):
        if is_git_marker(candidate / ".git"):
            return str(candidate)
    return None


def preflight_interpreter(interpreter: str | os.PathLike, explicit: bool = True) -> str:
    """Check an interpreter before it is recorded for a release.

    Refuses relative paths, non-executables, interpreters (or their directory)
    that another account can modify, anything inside a Git work tree, Python
    older than PYTHON_MIN, and -- unless named explicitly with --python -- a
    virtual environment, whose contents can change after installation."""
    text = str(interpreter)
    if not os.path.isabs(text) or "\n" in text:
        raise ReleaseError(f"interpreter {text!r} must be an absolute path")
    if not (os.path.isfile(text) and os.access(text, os.X_OK)):
        raise ReleaseError(f"interpreter {text} is not an executable file")
    real = os.path.realpath(text)
    # Every directory and symlink on the way from the recorded path (which the
    # shim execs, unresolved, so a venv or a package-manager link keeps working)
    # to the real file, not just the real file's own directory.
    lookups: list[str] = []
    problem = redirect_problem(text, lookups)
    if problem:
        raise ReleaseError(f"interpreter {text} resolves to {real}, and {problem}; "
                           "use an interpreter only you or root can modify")
    # Every directory the path passes through, including the targets of
    # intermediate symlinks (x/link -> repo/sub makes x/link/bin/python depend
    # on repo's work tree even when the final file lies outside it).
    for path in (os.path.dirname(os.path.abspath(text)), os.path.dirname(real), *lookups):
        worktree = inside_git_worktree(path)
        if worktree:
            raise ReleaseError(f"interpreter {text} is inside the Git work tree {worktree}, which agents may edit; "
                               "use a system or otherwise stable interpreter")
    probe = ("import sys; print('%d.%d.%d' % sys.version_info[:3]); "
             "print(int(sys.prefix != sys.base_prefix)); print(getattr(sys, '_base_executable', '') or '')")
    try:
        result = subprocess.run([text, "-I", "-B", "-c", probe],
                                capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ReleaseError(f"interpreter {text} did not run: {error}") from None
    lines = result.stdout.splitlines()
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)\s*", lines[0] if lines else "")
    if result.returncode or not match:
        raise ReleaseError(f"interpreter {text} did not report a Python version "
                           f"(exit {result.returncode}): {result.stderr.strip()[:200]}")
    version = tuple(int(part) for part in match.groups())
    if version[:2] < PYTHON_MIN:
        raise ReleaseError(f"interpreter {text} is Python {'.'.join(map(str, version))}; "
                           f"Syndicate requires {PYTHON_MIN_TEXT}+")
    if len(lines) > 1 and lines[1].strip() == "1" and not explicit:
        base = lines[2].strip() if len(lines) > 2 else ""
        raise ReleaseError(f"the installer runs in a virtual environment ({text}), which can change after "
                           f"installation; pass --python with a stable interpreter"
                           f"{f' such as {base}' if base else ''} (or name this one explicitly to accept it)")
    return text


# The only inherited variables a smoke test sees; everything else (Syndicate,
# tmux, Python, Git, provider and unrelated variables) is dropped.
SMOKE_ENV_KEEP = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "USER", "LOGNAME", "TERM")


def disposable_env(base: Path) -> dict:
    """A child environment whose HOME, XDG, Codex, Claude, tmp and tmux locations are
    disposable directories, inheriting only SMOKE_ENV_KEEP. This redirects
    well-behaved code; it is not a sandbox."""
    env = {k: v for k, v in os.environ.items() if k in SMOKE_ENV_KEEP}
    for name in ("home", "config", "data", "cache", "codex", "claude", "tmp", "tmux"):
        (base / name).mkdir(mode=0o700)
    env.update(HOME=str(base / "home"), XDG_CONFIG_HOME=str(base / "config"), XDG_DATA_HOME=str(base / "data"),
               XDG_CACHE_HOME=str(base / "cache"), CODEX_HOME=str(base / "codex"),
               CLAUDE_CONFIG_DIR=str(base / "claude"), TMPDIR=str(base / "tmp"), TMUX_TMPDIR=str(base / "tmux"),
               PYTHONDONTWRITEBYTECODE="1")
    return env


def supports_version(root: Path) -> bool:
    """Releases with the runtime identity module answer a side-effect-free --version."""
    try:
        return ((root / "bin" / "syndicate_identity.py").is_file()
                and "--version" in (root / "bin" / "syndicate").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False


def smoke_test(stage: Path, interpreter: str, commit: str) -> None:
    """Boot the staged release from an unrelated cwd in a disposable home."""
    with tempfile.TemporaryDirectory(prefix="syndicate-smoke-") as scratch:
        base = Path(scratch)
        env = disposable_env(base)
        cwd = base / "tmp"
        script = str(stage / "bin" / "syndicate")

        def run(*argv: str) -> str:
            try:
                result = subprocess.run([interpreter, "-I", "-B", script, *argv], cwd=cwd, env=env,
                                        capture_output=True, text=True, timeout=SMOKE_TIMEOUT,
                                        stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                raise ReleaseError(f"smoke test 'syndicate {' '.join(argv)}' timed out") from None
            if result.returncode:
                raise ReleaseError(f"smoke test 'syndicate {' '.join(argv)}' exited {result.returncode}: "
                                   f"{(result.stderr or result.stdout).strip()[-800:]}")
            return result.stdout

        run("--help")
        if supports_version(stage):
            out = run("--version")
            if commit not in out:
                raise ReleaseError(f"smoke test 'syndicate --version' did not report {commit}: {out.strip()[:300]}")
        out = run("chief-of-staff", "--dry-run", "-x", "codex")
        try:
            launch = json.loads(out)
        except ValueError:
            raise ReleaseError("smoke test 'chief-of-staff --dry-run' did not print JSON") from None
        if not isinstance(launch, dict) or "argv" not in launch:
            raise ReleaseError("smoke test 'chief-of-staff --dry-run' printed no launch argv")
    # The stage is read-only and children run with -B; the manifest recheck in
    # stage_release proves the smoke test left the release byte-identical.


# --- locking, records and the entry point ---------------------------------

# Steps the current command completed, reported when a later step fails.
STEPS: list[str] = []


def step(message: str) -> None:
    STEPS.append(message)


def make_dir(path: Path, mode: int) -> None:
    """Create path with exactly mode (whatever the umask), or tighten an existing
    directory to at most mode; never loosen it (a 0700 root stays 0700)."""
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        tighten(path, mode)
        return
    os.chmod(path, mode)


def make_missing_dirs(path: Path) -> bool:
    """Create path and any missing ancestors, each with exactly mode 0700 (as
    the XDG base directory specification asks), whatever the umask. Existing
    directories are left exactly as they are. True when path was created."""
    missing = []
    probe = path
    while not os.path.lexists(probe):
        missing.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            continue  # created concurrently; checked like any existing directory
        os.chmod(directory, 0o700)
    return path in missing


def sticky_protects(directory: str | os.PathLike, entry: str | os.PathLike) -> bool:
    """True when no other account can rename or replace entry although
    directory is writable by others: directory is sticky and owned by root or
    this user (the owner of a sticky directory can still rename any entry), and
    entry belongs to root or this user."""
    uid = os.getuid()
    try:
        holder = os.stat(directory)
        owner = os.lstat(entry).st_uid
    except OSError:
        return False
    return bool(holder.st_mode & stat.S_ISVTX) and holder.st_uid in (0, uid) and owner in (0, uid)


def prepare_cache_parent(paths: Paths) -> None:
    """Create the release-source cache's parent 0700 when it is missing. An
    existing one is tightened to 0700 only when it is Syndicate's own default
    ($XDG_CACHE_HOME/syndicate); the parent of a --source-cache the operator
    chose (possibly $HOME) is never chmodded, only refused if another account
    can write it."""
    parent = paths.cache.parent
    try:
        created = make_missing_dirs(parent)
        if not created and paths.default_cache:
            tighten(parent, 0o700)
    except OSError as error:
        raise ReleaseError(f"cannot prepare release-source cache directory {parent}: {error}") from None
    if not parent.is_dir():
        raise ReleaseError(f"release-source cache directory {parent} is not a directory")
    problem = writable_by_others(parent)
    if problem:
        raise ReleaseError(f"release-source cache directory {parent} is {problem}; refusing to use it")


@contextmanager
def open_lock(path: Path):
    """A 0600 lock file, never followed through a symlink."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "r+b") as handle:
        yield handle


def prepare_root(paths: Paths) -> None:
    """Create the install root, releases/ and interpreters/ with explicit 0755
    modes (existing ones are only ever tightened) and missing parents 0700;
    refuse any another account can write (it could replace `current`, a release
    or a recorded interpreter), and a parent another account can write unless
    sticky_protects the root in it."""
    parent = paths.root.parent
    try:
        make_missing_dirs(parent)
        for path in (paths.root, paths.releases, paths.interpreters):
            make_dir(path, 0o755)
        # Another account that can write the root's parent could replace the
        # whole root, unless the parent is sticky (like /tmp) and owned by root or you.
        problem = writable_by_others(parent)
    except OSError as error:
        raise ReleaseError(f"cannot prepare install root {paths.root}: {error}") from None
    if problem and not sticky_protects(parent, paths.root):
        raise ReleaseError(f"install root parent {parent} is {problem}; refusing to activate releases in {paths.root}")
    for path in (paths.root, paths.releases, paths.interpreters):
        problem = writable_by_others(path)
        if problem:
            raise ReleaseError(f"install root {path} is {problem}; refusing to activate releases there")


def check_bin_dir(paths: Paths) -> None:
    """Refuse a shim directory another account can write (it could replace the entry point)."""
    if paths.bin_dir.exists():
        problem = writable_by_others(paths.bin_dir)
        if problem:
            raise ReleaseError(f"entry point directory {paths.bin_dir} is {problem}; refusing to install the "
                               "stable entry point there (pass --no-shim to skip it)")


@contextmanager
def activation_lock(paths: Paths):
    prepare_root(paths)
    with open_lock(paths.lock) as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log(f"waiting for activation lock {paths.lock}")
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def append_jsonl(path: Path, record: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(record, sort_keys=False) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def read_jsonl(path: Path, strict: bool = False) -> list[dict]:
    """Records of a JSON-lines log. Garbled lines are skipped, or refused when strict."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as error:
        raise ReleaseError(f"cannot read {path}: {error}") from None
    records = []
    for number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line.decode("utf-8"))
        except ValueError:
            record = None
        if isinstance(record, dict):
            records.append(record)
        elif strict:
            raise ReleaseError(f"{path} line {number} is not a JSON object; fix or remove it before relying on it")
    return records


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Replace path atomically. Its directory must already exist (callers create
    it with an explicit mode)."""
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise
    fsync_dir(path.parent)


def current_commit(paths: Paths) -> str | None:
    try:
        target = os.readlink(paths.current)
    except OSError:
        return None
    name = Path(target).name
    return name if COMMIT_PATTERN.match(name) else None


def switch_current(paths: Paths, commit: str) -> None:
    temp = paths.root / f".current.{os.getpid()}.{time.monotonic_ns()}"
    os.symlink(f"releases/{commit}", temp)
    try:
        os.replace(temp, paths.current)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    fsync_dir(paths.root)


def render_shim(paths: Paths) -> str:
    root = shlex.quote(str(paths.root))
    return f"""#!/bin/sh
{SHIM_MARKER}
# Stable Syndicate entry point. It resolves the active release once, then
# execs that release's recorded interpreter, so later activations never change
# a running invocation. -I keeps PYTHONPATH, PYTHONHOME, the user site and the
# current directory out of the release's imports; -B writes no bytecode (the
# launcher sets PYTHONDONTWRITEBYTECODE for its own children). Regenerate with:
# syndicate-install install-shim
root={root}
release=$(CDPATH= cd -P -- "$root/current" 2>/dev/null && pwd -P) || {{
  echo "syndicate: no active release at $root/current (see syndicate-install status)" >&2
  exit 127
}}
interpreter=
IFS= read -r interpreter < "$root/interpreters/${{release##*/}}" 2>/dev/null || :
if [ -z "$interpreter" ]; then
  echo "syndicate: no interpreter recorded for $release" >&2
  exit 127
fi
exec "$interpreter" -I -B "$release/bin/syndicate" "$@"
"""


def is_managed_shim(path: Path) -> bool:
    try:
        if path.is_symlink() or not path.is_file():
            return False
        with open(path, "rb") as handle:
            return SHIM_MARKER.encode() in handle.read(4096)
    except OSError:
        return False


def shim_root(path: Path) -> str | None:
    """The install root a managed shim embeds (its root= line), or None."""
    try:
        text = path.read_bytes()[:8192].decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("root="):
            try:
                words = shlex.split(line[len("root="):])
            except ValueError:
                return None
            return words[0] if len(words) == 1 else None
    return None


def ensure_bin_dir(paths: Paths) -> None:
    if not paths.bin_dir.is_dir():
        paths.bin_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(paths.bin_dir, 0o755)
    check_bin_dir(paths)


def install_shim(paths: Paths, force: bool = False) -> str:
    """Install the stable entry point, recording whatever it replaces.

    A managed shim for another install root is replaced only with force, and
    is then recorded like any other replaced entry point."""
    shim = paths.shim
    content = render_shim(paths).encode()
    ensure_bin_dir(paths)
    managed = is_managed_shim(shim)
    if managed and shim.read_bytes() == content:
        return "unchanged"
    previous_root = shim_root(shim) if managed else None
    record_it = not managed and (shim.is_symlink() or shim.exists())
    if managed and previous_root != str(paths.root):
        if not force:
            raise ReleaseError(f"{shim} is the managed entry point of install root {previous_root}; refusing to "
                               f"point it at {paths.root} without --force (syndicate-install install-shim --force)")
        record_it = True
    if record_it:
        record = {"at": now_iso(), "path": str(shim)}
        if shim.is_symlink():
            record.update(kind="symlink", target=os.readlink(shim))
        elif shim.is_file():
            paths.entrypoint_backups.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(paths.entrypoint_backups, 0o700)
            backup = paths.entrypoint_backups / f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}-syndicate"
            shutil.copy2(shim, backup)
            record.update(kind="file", backup=str(backup), mode=stat.S_IMODE(os.stat(shim).st_mode),
                          sha256=sha256_file(shim))
        else:
            raise ReleaseError(f"{shim} exists and is not a file or symlink; refusing to replace it")
        if managed:
            record.update(previous_root=previous_root, forced=True)
        append_jsonl(paths.entrypoints, record)
        log(f"recorded previous entry point {shim} ({record['kind']}"
            f"{', install root ' + str(previous_root) if managed else ''}) in {paths.entrypoints}")
    atomic_write(shim, content, 0o755)
    return "installed"


def restore_entrypoint(paths: Paths) -> dict:
    records = [r for r in read_jsonl(paths.entrypoints, strict=True) if r.get("path") == str(paths.shim)]
    if not records:
        raise ReleaseError(f"no recorded previous entry point for {paths.shim}")
    record = records[-1]
    shim = paths.shim
    if (shim.exists() or shim.is_symlink()) and not is_managed_shim(shim):
        raise ReleaseError(f"{shim} is not the managed shim; refusing to overwrite it")
    ensure_bin_dir(paths)
    kind = record.get("kind")
    if kind == "symlink":
        target = record.get("target")
        if not isinstance(target, str) or not target:
            raise ReleaseError(f"{paths.entrypoints}: the last record for {shim} has no symlink target")
        temp = shim.with_name(f".{shim.name}.restore.{os.getpid()}.{time.monotonic_ns()}")
        try:
            os.symlink(target, temp)
            os.replace(temp, shim)
        except OSError as error:
            temp.unlink(missing_ok=True)
            raise ReleaseError(f"cannot restore {shim} -> {target}: {error}") from None
    elif kind == "file":
        backup, digest, mode = record.get("backup"), record.get("sha256"), record.get("mode", 0o755)
        if not (isinstance(backup, str) and isinstance(digest, str) and isinstance(mode, int)):
            raise ReleaseError(f"{paths.entrypoints}: the last record for {shim} is malformed (backup, sha256, mode)")
        try:
            data = Path(backup).read_bytes()
        except OSError as error:
            raise ReleaseError(f"backup {backup} of the previous entry point cannot be read: {error}") from None
        if sha256_bytes(data) != digest:
            raise ReleaseError(f"backup {backup} no longer matches its recorded sha256")
        atomic_write(shim, data, mode & 0o7777)
    else:
        raise ReleaseError(f"{paths.entrypoints}: the last record for {shim} has unknown kind {kind!r}")
    return record


# --- install, rollback, prune ---------------------------------------------

def stage_release(paths: Paths, approved: dict, entries: dict, archive: bytes,
                  interpreter: str) -> tuple[Path, dict, str]:
    """Build and verify a release in a staging sibling; return (stage, manifest, archive digest)."""
    commit = approved["commit"]
    prepare_root(paths)
    stage = Path(tempfile.mkdtemp(prefix=f"{STAGING_PREFIX}{os.getpid()}-", dir=paths.releases))
    try:
        files = extract(archive, entries, stage)
        raw = manifest_bytes(commit, files)
        with open(stage / MANIFEST, "xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        verify_assets(stage, files)
        set_read_only(stage)
        fsync_tree(stage)
        smoke_test(stage, interpreter, commit)
        manifest = verify_release(stage, commit)
    except BaseException:
        remove_tree(stage)
        raise
    return stage, manifest, sha256_bytes(archive)


def publish(paths: Paths, stage: Path, manifest: dict) -> tuple[Path, bool]:
    """Move a verified stage into releases/<commit>, or reuse an identical one. Caller holds the lock."""
    target = paths.release(manifest["commit"])
    if target.is_symlink():
        raise ReleaseError(f"{target} is a symlink, not a release directory; remove it after investigation")
    if target.exists():
        existing = verify_release(target, manifest["commit"])
        if (existing["files"] != manifest["files"]
                or existing["manifest_sha256"] != manifest["manifest_sha256"]):
            raise ReleaseError(f"{target} exists with different contents (RELEASE.json sha256 "
                               f"{existing['manifest_sha256']}, expected {manifest['manifest_sha256']}); "
                               "remove it after investigation")
        remove_tree(stage)
        return target, True
    os.rename(stage, target)
    fsync_dir(paths.releases)
    return target, False


def activate(paths: Paths, commit: str, interpreter: str, record: dict, *, shim: bool) -> dict:
    """Select a published release. Caller holds the lock."""
    release = paths.release(commit)
    previous = current_commit(paths)
    atomic_write(paths.interpreters / commit, (interpreter + "\n").encode(), 0o644)
    switch_current(paths, commit)
    step(f"switched current to {commit}")
    entry = {"at": now_iso(), "commit": commit, "tag": record.get("tag"), "source_ref": record.get("source_ref"),
             "upstream": record.get("upstream"), "archive_digest": record.get("archive_digest"),
             "manifest_sha256": record.get("manifest_sha256"), "release_dir": str(release),
             "interpreter": interpreter, "previous_commit": previous, "action": record.get("action", "install")}
    for key in ("upstream_changed_from", "retagged_from"):
        if record.get(key):
            entry[key] = record[key]
    append_jsonl(paths.log, entry)
    step("logged the activation")
    if shim:
        try:
            state = install_shim(paths)
        except (ReleaseError, OSError) as error:
            raise ReleaseError(f"activated {commit}; shim not installed: {error}") from None
        step(f"shim {state}")
    return entry


# Characters Claude's Bash(...) rule syntax gives meaning to. The launcher
# omits the relay allow rule for such paths (relays then ask for approval).
RULE_SYNTAX = "()*"


def check_rule_paths(paths: Paths, interpreter: str) -> None:
    """Refuse paths that cannot appear in Claude's relay allow rule.

    Sessions grant relays with `Bash(<interpreter> -I -B <release>/bin/syndicate team run-role *)`;
    a '(', ')' or '*' in either path would end that rule early or widen it, so
    relays would be denied or the rule would match the wrong command. Whitespace
    is allowed but warned about: the launcher then omits the rule and relays ask
    for approval each time."""
    for label, value in (("install root", str(paths.root)), ("install root", os.path.realpath(paths.root)),
                         ("interpreter", interpreter)):
        for char in RULE_SYNTAX:
            if char in value:
                raise ReleaseError(f"{label} {value} contains {char!r}, which Claude's Bash() permission rule for "
                                   f"Syndicate relays cannot express; choose a path without {RULE_SYNTAX!r}")
        if any(char.isspace() for char in value):
            log(f"warning: {label} {value} contains whitespace; Claude sessions will not pre-approve Syndicate "
                "relays, so each relay asks for approval")


def check_recorded_manifest(paths: Paths, commit: str, manifest: dict) -> None:
    """The release's RELEASE.json must be the one recorded when it was last activated."""
    recorded = last_record(paths, commit).get("manifest_sha256")
    if not isinstance(recorded, str) or not recorded:
        raise ReleaseError(f"no activation of {commit} in {paths.log} records its manifest digest; "
                           "refusing to select a release whose manifest cannot be confirmed")
    if recorded != manifest["manifest_sha256"]:
        raise ReleaseError(f"{paths.release(commit) / MANIFEST} (sha256 {manifest['manifest_sha256']}) differs "
                           f"from the manifest recorded when {commit} was activated ({recorded}); "
                           "the release was changed after installation")


def install(paths: Paths, tag: str, upstream: str | None = None, interpreter: str | None = None,
            shim: bool = True, *, change_upstream: bool = False, allow_retag: bool = False) -> dict:
    check_tag_name(tag)
    interpreter = preflight_interpreter(interpreter or os.path.abspath(sys.executable),
                                        explicit=interpreter is not None)
    check_rule_paths(paths, interpreter)
    prepare_root(paths)
    if shim:
        check_bin_dir(paths)
    approved, entries, archive = fetch_release_source(paths, tag, upstream, change_upstream, allow_retag)
    step(f"approved {tag} -> {approved['commit']}")
    log(f"approved {tag} -> {approved['commit']} (on upstream {MAIN_BRANCH} {approved['upstream_main'][:12]})")
    stage, manifest, digest = stage_release(paths, approved, entries, archive, interpreter)
    step("staged and verified the release")
    try:
        with activation_lock(paths):
            release, reused = publish(paths, stage, manifest)
            step(f"{'reused' if reused else 'published'} {release}")
            if reused:
                log(f"reusing verified identical release {release}")
            return activate(paths, approved["commit"], interpreter, {
                **approved, "archive_digest": digest, "manifest_sha256": manifest["manifest_sha256"],
                "action": "install"}, shim=shim)
    finally:
        if stage.exists():
            remove_tree(stage)


def known_good(paths: Paths, exclude: str | None) -> list[str]:
    """Previously activated commits, most recent first, excluding one."""
    seen, order = set(), []
    for record in reversed(read_jsonl(paths.log)):
        commit = record.get("commit")
        if isinstance(commit, str) and COMMIT_PATTERN.match(commit) and commit != exclude and commit not in seen:
            seen.add(commit)
            order.append(commit)
    return order


def last_record(paths: Paths, commit: str) -> dict:
    return next((r for r in reversed(read_jsonl(paths.log)) if r.get("commit") == commit), {})


def rollback(paths: Paths, to: str | None = None, interpreter: str | None = None, shim: bool = True) -> dict:
    with activation_lock(paths):
        if shim:
            check_bin_dir(paths)
        current = current_commit(paths)
        if to:
            matches = [c for c in known_good(paths, None) if c.startswith(to)] if len(to) < 40 else [to]
            if len(matches) != 1:
                raise ReleaseError(f"{to!r} does not name exactly one previously activated release")
            target = matches[0]
            if target not in known_good(paths, None):
                raise ReleaseError(f"{target} was never activated; only known-good releases can be selected")
            if target == current:
                raise ReleaseError(f"{target} is already the current release")
            manifest = verify_release(paths.release(target), target)
            check_recorded_manifest(paths, target, manifest)
        else:
            target, skipped = None, []
            for candidate in known_good(paths, current):
                if not paths.release(candidate).is_dir():
                    continue
                try:
                    manifest = verify_release(paths.release(candidate), candidate)
                    check_recorded_manifest(paths, candidate, manifest)
                except ReleaseError as error:
                    skipped.append(f"{candidate}: {error}")
                    log(f"skipping rollback candidate {candidate}: {error}")
                    continue
                target = candidate
                break
            if target is None:
                detail = "".join(f"\n  skipped {line}" for line in skipped)
                raise ReleaseError(f"no retained known-good release to roll back to{detail}")
        prior = last_record(paths, target)
        chosen = preflight_interpreter(interpreter or prior.get("interpreter") or "")
        check_rule_paths(paths, chosen)
        return activate(paths, target, chosen, {
            "tag": prior.get("tag"), "source_ref": prior.get("source_ref"), "upstream": prior.get("upstream"),
            "archive_digest": prior.get("archive_digest"), "manifest_sha256": manifest["manifest_sha256"],
            "action": "rollback"}, shim=shim)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def scan_in_use(paths: Paths) -> tuple[dict[str, str], bool, bool]:
    """({commit: reason}, live markers seen, /proc scanned) for releases still in use.

    Markers (in-use/<commit>/<pid>) are the primary signal: the launcher writes
    one for itself and for its supervised CLI child in release mode. /proc is
    the fallback for processes without a marker (older releases, short
    commands, a marker that could not be written, a CLI the launcher exec'd
    into): any `releases/<commit>` inside a command-line argument, or inside a
    SYNDICATE_* environment value (the launcher passes SYNDICATE_RELEASE_ROOT to
    the provider CLI, so its descendants carry it), keeps that release."""
    used: dict[str, str] = {}
    markers = False
    if paths.in_use.is_dir():
        for marker in paths.in_use.glob("*/*"):
            if (COMMIT_PATTERN.match(marker.parent.name) and DIGITS.fullmatch(marker.name)
                    and pid_alive(int(marker.name))):
                markers = True
                used.setdefault(marker.parent.name, f"in-use marker pid {marker.name}")
    scanned = PROC.is_dir() and (PROC / str(os.getpid())).is_dir()
    if scanned:
        for entry in PROC.iterdir():
            if not DIGITS.fullmatch(entry.name):
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            for match in RELEASE_REFERENCE.finditer(cmdline):
                used.setdefault(match.group(1).decode(), f"process {entry.name}")
            try:
                environ = (entry / "environ").read_bytes()
            except OSError:
                continue
            for variable in environ.split(b"\0"):
                if variable.startswith(b"SYNDICATE_"):
                    for match in RELEASE_REFERENCE.finditer(variable):
                        used.setdefault(match.group(1).decode(), f"process {entry.name} environment")
    return used, markers, scanned


def releases_in_use(paths: Paths) -> dict[str, str]:
    """{commit: reason} for releases a live process or marker still uses."""
    return scan_in_use(paths)[0]


def hook_releases(paths: Paths, codex_home: Path | None) -> set[str]:
    """Releases named anywhere in hooks.json. A textual scan, so a file this
    installer could not parse still protects every release it mentions."""
    if codex_home is None:
        return set()
    try:
        raw = Path(os.path.realpath(codex_home / "hooks.json")).read_bytes()
    except OSError:
        return set()
    return {match.group(1).decode() for match in RELEASE_REFERENCE.finditer(raw)}


def remove_stale_markers(paths: Paths) -> None:
    """Remove in-use markers whose PID is gone, then empty marker directories;
    tighten in-use/ and the remaining commit directories to 0700."""
    if not paths.in_use.is_dir() or paths.in_use.is_symlink():
        return
    tighten(paths.in_use, 0o700)
    for directory in paths.in_use.iterdir():
        if directory.is_symlink() or not directory.is_dir():
            continue
        tighten(directory, 0o700)
        for marker in directory.iterdir():
            if DIGITS.fullmatch(marker.name) and not marker.is_dir() and not pid_alive(int(marker.name)):
                marker.unlink(missing_ok=True)
        try:
            directory.rmdir()
        except OSError:
            pass


def prune(paths: Paths, dry_run: bool = False, codex_home: Path | None = None, force: bool = False) -> dict:
    """Remove releases that are not current, previous, in use or hooked.

    Each removal renames releases/<commit> to releases/.trash-<pid>-<commit>
    under the lock before deleting it, so an interrupted prune never leaves a
    half-deleted directory under a commit name; later prunes finish the job."""
    with activation_lock(paths):
        if not dry_run:
            remove_stale_markers(paths)
        used, markers, scanned = scan_in_use(paths)
        # Markers are written only by launchers that know to; without /proc a
        # session with no marker (an older release, a CLI the launcher exec'd
        # into) is invisible, so another session's live marker proves nothing.
        unverified = not scanned
        if unverified and not dry_run and not force:
            raise ReleaseError("cannot tell which releases live sessions use: /proc is unavailable, and in-use "
                               "markers alone cannot show every live session; refusing to prune without --force "
                               "(releases with live markers are kept even then)")
        current = current_commit(paths)
        previous = next(iter([c for c in known_good(paths, current) if paths.release(c).is_dir()]), None)
        hooked = hook_releases(paths, codex_home)
        removed, kept, stale, ignored, trash = [], {}, [], [], []
        for entry in sorted(paths.releases.iterdir()) if paths.releases.is_dir() else []:
            name = entry.name
            if name.startswith(TRASH_PREFIX) and not entry.is_symlink():
                trash.append(name)  # left by an interrupted prune; nothing can select it
                if not dry_run:
                    remove_tree(entry)
                continue
            if name.startswith(STAGING_PREFIX):
                pid = name[len(STAGING_PREFIX):].split("-", 1)[0]
                if not (DIGITS.fullmatch(pid) and pid_alive(int(pid))):
                    stale.append(name)
                    if not dry_run:
                        remove_tree(entry)
                continue
            if not COMMIT_PATTERN.match(name) or entry.is_symlink() or not entry.is_dir():
                ignored.append(name)
                continue
            reason = ("current" if name == current else "previous (last known-good)" if name == previous
                      else used.get(name) or ("registered Codex hook" if name in hooked else None))
            if reason:
                kept[name] = reason
                continue
            removed.append(name)
            if not dry_run:
                doomed = paths.releases / f"{TRASH_PREFIX}{os.getpid()}-{name}"
                os.rename(entry, doomed)
                fsync_dir(paths.releases)
                (paths.interpreters / name).unlink(missing_ok=True)
                remove_tree(doomed)
                fsync_dir(paths.releases)
        return {"removed": removed, "kept": kept, "stale_staging": stale, "stale_trash": trash,
                "ignored": ignored, "liveness_unverified": unverified, "dry_run": dry_run}


# --- Codex Stop hook migration --------------------------------------------

def hook_commands(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    commands = []
    for entry in (data.get("hooks", {}) or {}).get("Stop", []) if isinstance(data, dict) else []:
        for hook in entry.get("hooks", []) if isinstance(entry, dict) else []:
            if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                commands.append(hook["command"])
    return commands


def gate_path(command: str) -> Path | None:
    """The script path when the command is exactly one .../hooks/require-review.sh path."""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if len(words) != 1 or not os.path.isabs(words[0]):
        return None
    path = Path(words[0])
    if path.name != "require-review.sh" or path.parent.name != "hooks":
        return None
    return path


def release_of(paths: Paths, command: str) -> str | None:
    """The retained release whose gate the command names, through any symlinked spelling of the root."""
    path = gate_path(command)
    if path is None:
        return None
    for candidate in (os.path.abspath(path), os.path.realpath(path)):
        try:
            parts = Path(candidate).relative_to(paths.releases).parts
        except ValueError:
            continue
        if len(parts) == 3 and COMMIT_PATTERN.match(parts[0]) and parts[1:] == ("hooks", "require-review.sh"):
            return parts[0]
    return None


def hook_provenance(paths: Paths, path: Path) -> str:
    """Why this hook script is a genuine Syndicate gate, or raise."""
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ReleaseError(f"registered hook {path} cannot be read ({error}), "
                           "so its provenance cannot be verified") from None
    commit = release_of(paths, shlex.quote(str(path)))
    if commit:
        manifest, _ = read_manifest(paths.release(commit))
        if manifest["commit"] == commit and manifest["files"].get("hooks/require-review.sh") == sha256_bytes(content):
            return f"release {commit} manifest"
        raise ReleaseError(f"registered hook {path} does not match its release manifest; the release was changed "
                           "after installation, so hooks.json was not changed")
    checkout = path.parent.parent
    top = git(checkout, "rev-parse", "--show-toplevel", check=False)
    if top.returncode == 0 and os.path.realpath(top.stdout.strip()) == os.path.realpath(checkout):
        head = git(checkout, "rev-parse", "--verify", "HEAD^{commit}", check=False).stdout.strip()
        committed = git(checkout, "cat-file", "blob", "HEAD:hooks/require-review.sh", binary=True, check=False)
        if committed.returncode == 0 and committed.stdout == content:
            return f"checkout {checkout} HEAD {head}"
        raise ReleaseError(f"registered hook {path} differs from {checkout} HEAD:hooks/require-review.sh")
    raise ReleaseError(f"registered hook {path} is neither in a Git checkout nor in a retained release")


def load_json_strict(raw: bytes, label: str):
    """Parse JSON, refusing non-UTF-8 text and duplicate object keys (which other
    readers may resolve differently, so a rewrite could change their meaning)."""
    def pairs(items):
        seen = set()
        for key, _ in items:
            if key in seen:
                raise ReleaseError(f"{label} has a duplicate key {key!r}; refusing to rewrite an ambiguous file")
            seen.add(key)
        return dict(items)

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except UnicodeDecodeError:
        raise ReleaseError(f"{label} is not UTF-8 text") from None
    except ValueError as error:
        raise ReleaseError(f"{label} is not valid JSON: {error}") from None


def migrate_codex_hook(paths: Paths, codex_home: Path, target: str | None = None, dry_run: bool = False) -> dict:
    """Point the registered Syndicate Stop hook at a release's gate script.

    Touches only hooks whose command is exactly one verified Syndicate
    require-review.sh path; preserves every other entry, sibling and field. A
    require-review.sh hook whose provenance cannot be verified (another
    project's script of the same name, a dirty checkout, a missing file,
    including one in a release of this install that is no longer retained) is
    left untouched with a warning; one in a retained release that is missing or
    no longer matches its manifest stops the migration. Runs under the activation lock, so prune cannot remove
    the target meanwhile.
    """
    with activation_lock(paths):
        return _migrate_codex_hook(paths, codex_home, target, dry_run)


def _migrate_codex_hook(paths: Paths, codex_home: Path, target: str | None, dry_run: bool) -> dict:
    hooks_link = codex_home / "hooks.json"
    commit = target or current_commit(paths)
    if not commit:
        raise ReleaseError("no current release; install one or pass --target COMMIT")
    if len(commit) < 40:
        matches = [c.name for c in paths.releases.glob(commit + "*") if COMMIT_PATTERN.match(c.name)]
        if len(matches) != 1:
            raise ReleaseError(f"{commit!r} does not name exactly one retained release")
        commit = matches[0]
    release = paths.release(commit)
    manifest = verify_release(release, commit)
    check_recorded_manifest(paths, commit, manifest)
    new_path = Path(os.path.realpath(release)) / "hooks" / "require-review.sh"
    new_command = shlex.quote(str(new_path))
    # Read, back up and replace the real file, so a symlinked hooks.json
    # (for example into a dotfiles repository) stays a symlink.
    hooks_path = Path(os.path.realpath(hooks_link))
    if not hooks_path.is_file():
        return {"changed": False, "reason": f"{hooks_link} does not exist; no Syndicate hook is registered"}
    raw = hooks_path.read_bytes()
    data = load_json_strict(raw, str(hooks_path))
    stop = data.get("hooks", {}).get("Stop") if isinstance(data, dict) and isinstance(data.get("hooks"), dict) else None
    if not isinstance(stop, list):
        return {"changed": False, "reason": "no Stop hooks are registered"}
    changes, provenance, skipped = [], {}, []
    seen_new = False
    new_stop = []
    for entry in stop:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            new_stop.append(entry)
            continue
        hooks = []
        touched = False
        for hook in entry["hooks"]:
            command = hook.get("command") if isinstance(hook, dict) else None
            path = gate_path(command) if isinstance(command, str) else None
            if path is None:
                hooks.append(hook)
                continue
            if command == new_command or os.path.realpath(path) == str(new_path):
                if seen_new:
                    changes.append({"removed_duplicate": command})
                    touched = True
                    continue
                seen_new = True
                hooks.append(hook)
                continue
            try:
                provenance[command] = hook_provenance(paths, path)
            except ReleaseError as error:
                own = release_of(paths, command)
                if own and (os.path.lexists(path) or os.path.lexists(paths.release(own))):
                    raise  # this install's own release was changed: stop, change nothing
                if own:
                    # A release of this install that is gone (pruned or removed
                    # by hand): a missing file cannot be verified, so it is left
                    # as it is, like any other unverifiable hook.
                    error = ReleaseError(f"release {own} is no longer retained, so {path} is missing and "
                                         "cannot be verified")
                skipped.append({"command": command, "reason": str(error)})
                log(f"warning: leaving Stop hook {command} unchanged: {error}")
                hooks.append(hook)
                continue
            touched = True
            if seen_new:
                changes.append({"removed": command, "provenance": provenance[command]})
                continue
            seen_new = True
            hooks.append({**hook, "command": new_command})
            changes.append({"from": command, "to": new_command, "provenance": provenance[command]})
        if touched and not hooks:
            continue  # the entry held only the replaced Syndicate hook, now carried elsewhere
        new_stop.append({**entry, "hooks": hooks} if touched else entry)
    if not changes:
        reason = ("already registered" if seen_new else
                  "no verifiable Syndicate Stop hook is registered (not adding one)" if skipped else
                  "no Syndicate Stop hook is registered (not adding one)")
        if skipped:
            reason += f"; left {len(skipped)} unverifiable require-review.sh hook(s) unchanged"
        return {"changed": False, "reason": reason, "command": new_command, "skipped": skipped}
    result = {"changed": not dry_run, "dry_run": dry_run, "changes": changes, "command": new_command,
              "path": str(hooks_link), "skipped": skipped}
    if hooks_path != hooks_link:
        result["resolved_path"] = str(hooks_path)
    if dry_run:
        return result
    data["hooks"]["Stop"] = new_stop
    backup = hooks_path.with_name(f"{hooks_path.name}.syndicate-backup-{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}")
    mode = stat.S_IMODE(os.stat(hooks_path).st_mode)
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    step(f"backed up {hooks_path} to {backup}")
    atomic_write(hooks_path, (json.dumps(data, indent=2) + "\n").encode("utf-8"), mode)
    result["backup"] = str(backup)
    return result


# --- status ---------------------------------------------------------------

def install_root_problems(paths: Paths) -> list[str]:
    """Why another account could change what the install root activates or keeps."""
    problems = []
    for path in (paths.root, paths.releases, paths.interpreters, paths.in_use):
        if path != paths.root and path.is_symlink():
            problems.append(f"{path} is a symlink")
        elif path.is_dir():
            problem = writable_by_others(path)
            if problem:
                problems.append(f"{path} is {problem}")
    return problems


def status(paths: Paths) -> dict:
    current = current_commit(paths)
    record = last_record(paths, current) if current else {}
    try:
        interpreter = (paths.interpreters / current).read_text(encoding="utf-8").strip() if current else None
    except (OSError, ValueError):
        interpreter = None
    previous = next(iter([c for c in known_good(paths, current) if paths.release(c).is_dir()]), None)
    shim = ("managed" if is_managed_shim(paths.shim) else "symlink -> " + os.readlink(paths.shim)
            if paths.shim.is_symlink() else "unmanaged file" if paths.shim.exists() else "absent")
    return {"root": str(paths.root), "current": current, "tag": record.get("tag"),
            "activated_at": record.get("at"), "interpreter": interpreter, "previous": previous,
            "shim": str(paths.shim), "shim_state": shim, "source_cache": str(paths.cache)}


def list_releases(paths: Paths) -> list[dict]:
    current = current_commit(paths)
    st = status(paths)
    used = releases_in_use(paths)
    rows = []
    for entry in sorted(paths.releases.iterdir()) if paths.releases.is_dir() else []:
        if not COMMIT_PATTERN.match(entry.name):
            continue
        record = last_record(paths, entry.name)
        marks = [m for m, ok in (("current", entry.name == current), ("previous", entry.name == st["previous"]),
                                 ("in-use", entry.name in used)) if ok]
        rows.append({"commit": entry.name, "tag": record.get("tag"), "last_activated": record.get("at"),
                     "marks": marks})
    return rows


# --- CLI ------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--install-root", default=argparse.SUPPRESS,
                        help="release store (default: $XDG_DATA_HOME/syndicate or ~/.local/share/syndicate)")
    common.add_argument("--bin-dir", default=argparse.SUPPRESS, help="shim directory (default: ~/.local/bin)")
    common.add_argument("--source-cache", default=argparse.SUPPRESS,
                        help="release-source Git cache (default: $XDG_CACHE_HOME/syndicate/source.git)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")
    parser = argparse.ArgumentParser(prog="syndicate-install", parents=[common],
                                     description="Install, upgrade, roll back and verify per-user Syndicate releases.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "upgrade"):
        p = sub.add_parser(name, parents=[common], help="install an approved release tag and activate it")
        p.add_argument("--tag", required=True, help="approved release tag vMAJOR.MINOR.PATCH on upstream main")
        p.add_argument("--upstream", help="trusted upstream URL or bare repository path (remembered in the source cache)")
        p.add_argument("--change-upstream", action="store_true",
                       help="allow --upstream to replace a different recorded upstream")
        p.add_argument("--allow-retag", action="store_true",
                       help="accept a tag name that an earlier activation recorded at another commit")
        p.add_argument("--python", help="absolute Python 3.11+ interpreter (default: the one running this, "
                                        "unless it is a virtual environment)")
        p.add_argument("--no-shim", action="store_true", help="do not install or refresh the stable entry point")
    p = sub.add_parser("rollback", parents=[common], help="select a retained known-good release")
    p.add_argument("--to", help="commit (or unique prefix) of a previously activated release")
    p.add_argument("--python", help="interpreter override (default: the one recorded for that release)")
    p.add_argument("--no-shim", action="store_true")
    sub.add_parser("status", parents=[common], help="show the active release")
    sub.add_parser("list", parents=[common], help="list retained releases")
    p = sub.add_parser("verify", parents=[common], help="recheck a release against its RELEASE.json")
    p.add_argument("release", nargs="?", help="commit, prefix or directory (default: current)")
    p = sub.add_parser("prune", parents=[common], help="remove releases that are not current, previous or in use")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--codex-home", help="also keep releases registered in this CODEX_HOME's hooks.json")
    p.add_argument("--force", action="store_true",
                   help="prune even when liveness cannot be fully checked (no /proc); live markers still keep releases")
    p = sub.add_parser("install-shim", parents=[common], help="(re)write the stable entry point")
    p.add_argument("--force", action="store_true",
                   help="replace a managed entry point that belongs to another install root")
    sub.add_parser("restore-entrypoint", parents=[common], help="restore the entry point the shim replaced")
    p = sub.add_parser("migrate-codex-hook", parents=[common],
                       help="point the registered Syndicate Codex Stop hook at a release (explicit, never automatic)")
    p.add_argument("--codex-home", help="Codex home (default: $CODEX_HOME or ~/.codex)")
    p.add_argument("--target", help="release commit (default: current)")
    p.add_argument("--dry-run", action="store_true")
    return parser


def resolve_release_arg(paths: Paths, value: str | None) -> Path:
    if not value:
        commit = current_commit(paths)
        if not commit:
            raise ReleaseError("no current release")
        return paths.release(commit)
    if os.sep in value:
        return Path(value)
    matches = [p for p in paths.releases.glob(value + "*") if COMMIT_PATTERN.match(p.name)] if paths.releases.is_dir() else []
    if len(matches) != 1:
        raise ReleaseError(f"{value!r} does not name exactly one retained release")
    return matches[0]


def emit(args, data, text: str) -> None:
    print(json.dumps(data, indent=2) if getattr(args, "json", False) else text)


def report_failure(command: str, error: str) -> None:
    print(f"syndicate-install: {error}", file=sys.stderr)
    if STEPS:
        print(f"syndicate-install: {command} completed before the failure: {'; '.join(STEPS)}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    STEPS.clear()
    try:
        paths = Paths.from_env(getattr(args, "install_root", None), getattr(args, "bin_dir", None),
                               getattr(args, "source_cache", None))
        codex_default = Path(os.environ["CODEX_HOME"]) if os.environ.get("CODEX_HOME") else Path.home() / ".codex"
        if args.command in ("install", "upgrade"):
            record = install(paths, args.tag, args.upstream, args.python, shim=not args.no_shim,
                             change_upstream=args.change_upstream, allow_retag=args.allow_retag)
            emit(args, record, f"activated {record['tag']} ({record['commit']}) at {record['release_dir']}\n"
                                f"previous: {record['previous_commit'] or 'none'}")
        elif args.command == "rollback":
            record = rollback(paths, args.to, args.python, shim=not args.no_shim)
            emit(args, record, f"rolled back to {record['commit']} (from {record['previous_commit']})")
        elif args.command == "status":
            data = status(paths)
            emit(args, data, "\n".join(f"{k}: {v}" for k, v in data.items()))
        elif args.command == "list":
            rows = list_releases(paths)
            emit(args, rows, "\n".join(f"{r['commit']}  {r['tag'] or '-':10}  {r['last_activated'] or '-'}  "
                                       f"{' '.join(r['marks'])}".rstrip() for r in rows) or "no releases")
        elif args.command == "verify":
            release = resolve_release_arg(paths, args.release)
            manifest = verify_release(release)
            problems = install_root_problems(paths)
            if problems:
                raise ReleaseError(f"{release} matches its manifest, but another account could replace releases, "
                                   f"recorded interpreters or in-use markers: {'; '.join(problems)}")
            emit(args, {"release": str(release), "commit": manifest["commit"], "ok": True,
                        "files": len(manifest["files"])}, f"ok: {release} ({len(manifest['files'])} files)")
        elif args.command == "prune":
            codex_home = Path(args.codex_home) if args.codex_home else codex_default
            result = prune(paths, args.dry_run, codex_home, force=args.force)
            verb = "would remove" if args.dry_run else "removed"
            emit(args, result, "\n".join([f"{verb}: {c}" for c in result["removed"]]
                                         + [f"kept {c}: {why}" for c, why in result["kept"].items()]
                                         + [f"stale staging: {s}" for s in result["stale_staging"]]
                                         + [f"interrupted prune leftover: {s}" for s in result["stale_trash"]]
                                         + [f"ignored (not a release directory): {s}" for s in result["ignored"]]
                                         + (["warning: liveness unverified (no /proc; only in-use markers checked)"]
                                            if result["liveness_unverified"] else [])))
        elif args.command == "install-shim":
            with activation_lock(paths):
                state = install_shim(paths, force=args.force)
            emit(args, {"shim": str(paths.shim), "state": state}, f"{state}: {paths.shim}")
        elif args.command == "restore-entrypoint":
            with activation_lock(paths):
                record = restore_entrypoint(paths)
            emit(args, record, f"restored {record['path']} ({record['kind']})")
        elif args.command == "migrate-codex-hook":
            codex_home = Path(args.codex_home) if args.codex_home else codex_default
            result = migrate_codex_hook(paths, codex_home, args.target, args.dry_run)
            lines = [f"{'would change' if args.dry_run else 'changed'}: {c}" for c in result.get("changes", [])]
            lines += [f"left unchanged (unverified): {s['command']}: {s['reason']}" for s in result.get("skipped", [])]
            if result.get("backup"):
                lines.append(f"backup: {result['backup']}")
            if result.get("changes") and not args.dry_run:
                lines.append("Open 'codex' and approve the changed hook in the normal hook review prompt.")
            if not result.get("changes"):
                lines.insert(0, result.get("reason", "nothing to do"))
            emit(args, result, "\n".join(lines))
    except ReleaseError as error:
        report_failure(args.command, str(error))
        return 1
    except (OSError, UnicodeError) as error:
        report_failure(args.command, f"{args.command} failed: {error}")
        return 1
    return 0
