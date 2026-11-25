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
    syndicate/activation.lock            flock serializing publish/activate/prune

The stable entry point ($HOME/.local/bin/syndicate) is a POSIX sh shim that
resolves ``current`` once and execs the recorded interpreter with -B.

Standard library only. See docs/user-install.md.
"""
from __future__ import annotations

import argparse
import ast
import fcntl
import hashlib
import io
import json
import os
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

    def __init__(self, root: Path, bin_dir: Path, cache: Path):
        self.root = Path(os.path.abspath(root))
        self.bin_dir = Path(os.path.abspath(bin_dir))
        self.cache = Path(os.path.abspath(cache))

    @classmethod
    def from_env(cls, root=None, bin_dir=None, cache=None, environ=None) -> "Paths":
        env = os.environ if environ is None else environ
        home = Path(env.get("HOME") or Path.home())
        data = Path(env["XDG_DATA_HOME"]) if env.get("XDG_DATA_HOME") else home / ".local" / "share"
        cache_home = Path(env["XDG_CACHE_HOME"]) if env.get("XDG_CACHE_HOME") else home / ".cache"
        return cls(Path(root).expanduser() if root else data / "syndicate",
                   Path(bin_dir).expanduser() if bin_dir else home / ".local" / "bin",
                   Path(cache).expanduser() if cache else cache_home / "syndicate" / "source.git")

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


def git(repo: Path, *args: str, binary: bool = False, check: bool = True):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env=git_env(),
                            stdin=subprocess.DEVNULL)
    if check and result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise ReleaseError(f"git {args[0]} failed: {detail}")
    if binary:
        return result
    result.stdout = result.stdout.decode(errors="replace")
    return result


def ensure_cache(paths: Paths, upstream: str | None) -> str:
    """Create the release-source cache and return its configured upstream URL."""
    if not (paths.cache / "HEAD").is_file():
        if not upstream:
            raise ReleaseError(f"no release-source cache at {paths.cache}; pass --upstream URL|PATH")
        paths.cache.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(["git", "init", "--quiet", "--bare", str(paths.cache)],
                                capture_output=True, text=True, env=git_env())
        if result.returncode:
            raise ReleaseError(f"cannot create release-source cache: {result.stderr.strip()}")
    if upstream is not None and (not upstream or upstream.startswith("-") or "\n" in upstream):
        raise ReleaseError(f"invalid upstream {upstream!r}")
    configured = git(paths.cache, "config", "--get", f"remote.{REMOTE}.url", check=False).stdout.strip()
    if upstream:
        if os.path.isdir(upstream):
            upstream = os.path.abspath(upstream)
        if not configured:
            git(paths.cache, "remote", "add", REMOTE, upstream)
        elif configured != upstream:
            log(f"changing trusted upstream from {configured} to {upstream}")
            git(paths.cache, "remote", "set-url", REMOTE, upstream)
        return upstream
    if not configured:
        raise ReleaseError(f"release-source cache {paths.cache} has no upstream; pass --upstream URL|PATH")
    return configured


def resolve_approved_tag(paths: Paths, tag: str, upstream: str | None) -> dict:
    """Fetch upstream main and the named tag afresh; return the approved commit.

    Approved means: the operator named the tag exactly; it matches vMAJOR.MINOR.PATCH;
    it exists upstream right now; it peels to a commit; and that commit is an
    ancestor of (or equal to) upstream main as fetched by this same invocation.
    """
    if not TAG_PATTERN.match(tag or ""):
        raise ReleaseError(f"{tag!r} is not an approved release tag name (expected vMAJOR.MINOR.PATCH); "
                           "branches, commit ids and other refs are not installable. "
                           "Use 'syndicate dev --checkout PATH' to run an unmerged checkout.")
    with source_lock(paths):
        return _resolve_approved_tag(paths, tag, upstream)


@contextmanager
def source_lock(paths: Paths):
    """Serialize fetches into the shared release-source cache."""
    paths.cache.parent.mkdir(parents=True, exist_ok=True)
    with open(paths.cache.with_name(paths.cache.name + ".lock"), "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _resolve_approved_tag(paths: Paths, tag: str, upstream: str | None) -> dict:
    url = ensure_cache(paths, upstream)
    tag_ref = FETCHED_TAGS + tag
    # Forget previous fetch results first so a failed fetch can never fall back to stale refs.
    for ref in (FETCHED_MAIN, tag_ref):
        git(paths.cache, "update-ref", "-d", ref, check=False)
    fetched = git(paths.cache, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", REMOTE,
                  f"+refs/heads/{MAIN_BRANCH}:{FETCHED_MAIN}", check=False)
    if fetched.returncode:
        raise ReleaseError(f"cannot fetch {MAIN_BRANCH} from upstream {url}: {fetched.stderr.strip()}; "
                           "nothing was installed (a fresh fetch is required)")
    fetched = git(paths.cache, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", REMOTE,
                  f"+refs/tags/{tag}:{tag_ref}", check=False)
    if fetched.returncode:
        raise ReleaseError(f"tag {tag} was not found at upstream {url}: {fetched.stderr.strip()}")
    kind = git(paths.cache, "cat-file", "-t", tag_ref).stdout.strip()
    peeled = git(paths.cache, "rev-parse", "--verify", "--quiet", f"{tag_ref}^{{commit}}", check=False)
    commit = peeled.stdout.strip()
    if peeled.returncode or not COMMIT_PATTERN.match(commit):
        raise ReleaseError(f"tag {tag} does not point at a commit")
    main = git(paths.cache, "rev-parse", "--verify", f"{FETCHED_MAIN}^{{commit}}").stdout.strip()
    reachable = git(paths.cache, "merge-base", "--is-ancestor", commit, main, check=False)
    if reachable.returncode == 1:
        raise ReleaseError(f"tag {tag} ({commit}) is not reachable from upstream {MAIN_BRANCH} ({main}); "
                           "only tags on upstream main are installable")
    if reachable.returncode:
        raise ReleaseError(f"cannot check that {commit} is on upstream {MAIN_BRANCH}: {reachable.stderr.strip()}")
    return {"tag": tag, "commit": commit, "upstream": url, "source_ref": f"refs/tags/{tag}",
            "tag_kind": "annotated" if kind == "tag" else "lightweight", "upstream_main": main}


# --- artifact ------------------------------------------------------------

def allowed(path: str) -> bool:
    if "__pycache__" in path.split("/") or path.endswith((".pyc", ".pyo")):
        return False
    return path in ALLOWED_FILES or path.startswith(ALLOWED_DIRS) or bool(BIN_MODULE.match(path))


def tree_entries(cache: Path, commit: str) -> dict[str, tuple[str, str]]:
    """{path: (mode, blob id)} for allowlisted paths at the commit."""
    out = git(cache, "ls-tree", "-r", "-z", "--full-tree", commit, binary=True).stdout
    entries = {}
    for record in out.split(b"\0"):
        if not record:
            continue
        meta, _, name = record.partition(b"\t")
        mode, kind, obj = meta.decode().split()
        path = name.decode("utf-8", errors="strict")
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
    result = git(cache, "archive", "--format=tar", commit, "--", *sorted(entries), binary=True)
    return result.stdout


def extract(archive: bytes, entries: dict, stage: Path) -> dict[str, str]:
    """Extract exactly the expected regular files; return {path: sha256}."""
    files = {}
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
            os.chmod(target, 0o755 if mode == "100755" else 0o644)
            files[name] = sha256_bytes(data)
    missing = sorted(set(entries) - set(files))
    if missing:
        raise ReleaseError(f"archive is missing {', '.join(missing)} (export-ignore attributes are not supported)")
    return files


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
        if path.startswith("roles/") and path.endswith(".md") and not (stage / path).read_text().strip():
            problems.append(f"role {path} is empty")
    skill_dirs = {p.split("/")[1] for p in files if p.startswith("skills/") and p.count("/") >= 2}
    for skill in sorted(skill_dirs):
        if f"skills/{skill}/SKILL.md" not in files:
            problems.append(f"skill {skill} has no SKILL.md")
    # Every sibling module the launcher imports, and every module the identity module lists.
    launcher = (stage / "bin" / "syndicate").read_text() if "bin/syndicate" in files else ""
    for module in sorted(set(re.findall(r"^\s*import (syndicate_[A-Za-z0-9_]+)", launcher, re.M))
                         | set(re.findall(r"^\s*from (syndicate_[A-Za-z0-9_]+) import", launcher, re.M))
                         | set(runtime_modules(stage))):
        if f"bin/{module}.py" not in files:
            problems.append(f"bin/{module}.py is imported by the launcher but not shipped")
    for path in files:
        if not path.endswith(".md"):
            continue
        text = (stage / path).read_text()
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
        tree = ast.parse(path.read_text())
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

def preflight_interpreter(interpreter: str | os.PathLike) -> str:
    text = str(interpreter)
    if not os.path.isabs(text) or "\n" in text:
        raise ReleaseError(f"interpreter {text!r} must be an absolute path")
    if not (os.path.isfile(text) and os.access(text, os.X_OK)):
        raise ReleaseError(f"interpreter {text} is not an executable file")
    try:
        result = subprocess.run([text, "-I", "-B", "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
                                capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ReleaseError(f"interpreter {text} did not run: {error}") from None
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)\s*", result.stdout)
    if result.returncode or not match:
        raise ReleaseError(f"interpreter {text} did not report a Python version "
                           f"(exit {result.returncode}): {result.stderr.strip()[:200]}")
    version = tuple(int(part) for part in match.groups())
    if version[:2] < PYTHON_MIN:
        raise ReleaseError(f"interpreter {text} is Python {'.'.join(map(str, version))}; "
                           f"Syndicate requires {PYTHON_MIN_TEXT}+")
    return text


def disposable_env(base: Path) -> dict:
    """A child environment that cannot reach real user state, tmux or a checkout."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SYNDICATE_", "TMUX", "PYTHON", "GIT_", "CODEX_", "CLAUDE_"))}
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
                and "--version" in (root / "bin" / "syndicate").read_text())
    except OSError:
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
                result = subprocess.run([interpreter, "-B", script, *argv], cwd=cwd, env=env,
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

@contextmanager
def activation_lock(paths: Paths):
    paths.root.mkdir(parents=True, exist_ok=True)
    with open(paths.lock, "a") as handle:
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
        os.write(fd, (json.dumps(record, sort_keys=False) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def read_jsonl(path: Path) -> list[dict]:
    records = []
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
    directory = os.open(paths.root, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def render_shim(paths: Paths) -> str:
    root = shlex.quote(str(paths.root))
    return f"""#!/bin/sh
{SHIM_MARKER}
# Stable Syndicate entry point. It resolves the active release once, then
# execs that release's recorded interpreter, so later activations never change
# a running invocation. Regenerate with: syndicate-install install-shim
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
PYTHONDONTWRITEBYTECODE=1
export PYTHONDONTWRITEBYTECODE
exec "$interpreter" -B "$release/bin/syndicate" "$@"
"""


def is_managed_shim(path: Path) -> bool:
    try:
        if path.is_symlink() or not path.is_file():
            return False
        with open(path, "rb") as handle:
            return SHIM_MARKER.encode() in handle.read(4096)
    except OSError:
        return False


def install_shim(paths: Paths) -> str:
    """Install the stable entry point, recording whatever it replaces."""
    shim = paths.shim
    content = render_shim(paths).encode()
    paths.bin_dir.mkdir(parents=True, exist_ok=True)
    if is_managed_shim(shim):
        if shim.read_bytes() == content:
            return "unchanged"
    elif shim.is_symlink() or shim.exists():
        record = {"at": now_iso(), "path": str(shim)}
        if shim.is_symlink():
            record.update(kind="symlink", target=os.readlink(shim))
        elif shim.is_file():
            paths.entrypoint_backups.mkdir(parents=True, exist_ok=True)
            backup = paths.entrypoint_backups / f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}-syndicate"
            shutil.copy2(shim, backup)
            record.update(kind="file", backup=str(backup), mode=stat.S_IMODE(os.stat(shim).st_mode),
                          sha256=sha256_file(shim))
        else:
            raise ReleaseError(f"{shim} exists and is not a file or symlink; refusing to replace it")
        append_jsonl(paths.entrypoints, record)
        log(f"recorded previous entry point {shim} ({record['kind']}) in {paths.entrypoints}")
    atomic_write(shim, content, 0o755)
    return "installed"


def restore_entrypoint(paths: Paths) -> dict:
    records = [r for r in read_jsonl(paths.entrypoints) if r.get("path") == str(paths.shim)]
    if not records:
        raise ReleaseError(f"no recorded previous entry point for {paths.shim}")
    record = records[-1]
    shim = paths.shim
    if (shim.exists() or shim.is_symlink()) and not is_managed_shim(shim):
        raise ReleaseError(f"{shim} is not the managed shim; refusing to overwrite it")
    if record["kind"] == "symlink":
        temp = shim.with_name(f".{shim.name}.{os.getpid()}")
        os.symlink(record["target"], temp)
        os.replace(temp, shim)
    else:
        backup = Path(record["backup"])
        if sha256_file(backup) != record["sha256"]:
            raise ReleaseError(f"backup {backup} no longer matches its recorded sha256")
        atomic_write(shim, backup.read_bytes(), record.get("mode", 0o755))
    return record


# --- install, rollback, prune ---------------------------------------------

def stage_release(paths: Paths, approved: dict, interpreter: str) -> tuple[Path, dict, str]:
    """Build and verify a release in a staging sibling; return (stage, manifest, archive digest)."""
    commit = approved["commit"]
    paths.releases.mkdir(parents=True, exist_ok=True)
    entries = tree_entries(paths.cache, commit)
    archive = build_archive(paths.cache, commit, entries)
    stage = Path(tempfile.mkdtemp(prefix=f"{STAGING_PREFIX}{os.getpid()}-", dir=paths.releases))
    try:
        files = extract(archive, entries, stage)
        raw = manifest_bytes(commit, files)
        (stage / MANIFEST).write_bytes(raw)
        verify_assets(stage, files)
        set_read_only(stage)
        smoke_test(stage, interpreter, commit)
        manifest = verify_release(stage, commit)
    except BaseException:
        remove_tree(stage)
        raise
    return stage, manifest, sha256_bytes(archive)


def publish(paths: Paths, stage: Path, manifest: dict) -> tuple[Path, bool]:
    """Move a verified stage into releases/<commit>, or reuse an identical one. Caller holds the lock."""
    target = paths.release(manifest["commit"])
    if target.exists() or target.is_symlink():
        existing = verify_release(target, manifest["commit"])
        if existing["files"] != manifest["files"]:
            raise ReleaseError(f"{target} exists with different contents; remove it after investigation")
        remove_tree(stage)
        return target, True
    os.rename(stage, target)
    return target, False


def activate(paths: Paths, commit: str, interpreter: str, record: dict, *, shim: bool) -> dict:
    """Select a published release. Caller holds the lock."""
    release = paths.release(commit)
    previous = current_commit(paths)
    atomic_write(paths.interpreters / commit, (interpreter + "\n").encode(), 0o644)
    switch_current(paths, commit)
    entry = {"at": now_iso(), "commit": commit, "tag": record.get("tag"), "source_ref": record.get("source_ref"),
             "upstream": record.get("upstream"), "archive_digest": record.get("archive_digest"),
             "manifest_sha256": record.get("manifest_sha256"), "release_dir": str(release),
             "interpreter": interpreter, "previous_commit": previous, "action": record.get("action", "install")}
    append_jsonl(paths.log, entry)
    if shim:
        install_shim(paths)
    return entry


def check_rule_paths(paths: Paths, interpreter: str) -> None:
    """Refuse paths that cannot appear in Claude's relay allow rule.

    Sessions grant relays with `Bash(<interpreter> -B <release>/bin/syndicate team run-role *)`;
    a ')' in either path would end that rule early, so relays would be denied or
    the rule would match the wrong command."""
    for label, value in (("install root", str(paths.root)), ("install root", os.path.realpath(paths.root)),
                         ("interpreter", interpreter)):
        if ")" in value:
            raise ReleaseError(f"{label} {value} contains ')', which Claude's Bash() permission rule for "
                               "Syndicate relays cannot express; choose a path without ')'")


def install(paths: Paths, tag: str, upstream: str | None = None, interpreter: str | None = None,
            shim: bool = True) -> dict:
    interpreter = preflight_interpreter(interpreter or os.path.abspath(sys.executable))
    check_rule_paths(paths, interpreter)
    approved = resolve_approved_tag(paths, tag, upstream)
    log(f"approved {tag} -> {approved['commit']} (on upstream {MAIN_BRANCH} {approved['upstream_main'][:12]})")
    stage, manifest, digest = stage_release(paths, approved, interpreter)
    try:
        with activation_lock(paths):
            release, reused = publish(paths, stage, manifest)
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
        current = current_commit(paths)
        if to:
            matches = [c for c in known_good(paths, None) if c.startswith(to)] if len(to) < 40 else [to]
            if len(matches) != 1:
                raise ReleaseError(f"{to!r} does not name exactly one previously activated release")
            target = matches[0]
            if target not in known_good(paths, None):
                raise ReleaseError(f"{target} was never activated; only known-good releases can be selected")
        else:
            candidates = [c for c in known_good(paths, current) if paths.release(c).is_dir()]
            if not candidates:
                raise ReleaseError("no retained known-good release to roll back to")
            target = candidates[0]
        if target == current:
            raise ReleaseError(f"{target} is already the current release")
        manifest = verify_release(paths.release(target), target)
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


def releases_in_use(paths: Paths) -> dict[str, str]:
    """{commit: reason} for releases a live process or marker still uses.

    Markers (in-use/<commit>/<pid>) are the primary signal: the launcher writes
    one for itself and for its supervised CLI child in release mode. Scanning
    /proc command lines is the fallback for processes without a marker (older
    releases, short commands, a marker that could not be written); it finds
    nothing where /proc is absent."""
    used: dict[str, str] = {}
    if paths.in_use.is_dir():
        for marker in paths.in_use.glob("*/*"):
            if marker.name.isdigit() and pid_alive(int(marker.name)):
                used.setdefault(marker.parent.name, f"in-use marker pid {marker.name}")
    prefix = str(paths.releases) + "/"
    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                args = (entry / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            for arg in args:
                text = arg.decode(errors="replace")
                if text.startswith(prefix):
                    commit = text[len(prefix):].split("/", 1)[0]
                    used.setdefault(commit, f"process {entry.name}")
    return used


def hook_releases(paths: Paths, codex_home: Path | None) -> set[str]:
    if codex_home is None:
        return set()
    found = set()
    for command in hook_commands(codex_home / "hooks.json"):
        commit = release_of(paths, command)
        if commit:
            found.add(commit)
    return found


def remove_stale_markers(paths: Paths) -> None:
    """Remove in-use markers whose PID is gone, then empty marker directories."""
    if not paths.in_use.is_dir() or paths.in_use.is_symlink():
        return
    for directory in paths.in_use.iterdir():
        if directory.is_symlink() or not directory.is_dir():
            continue
        for marker in directory.iterdir():
            if marker.name.isdigit() and not marker.is_dir() and not pid_alive(int(marker.name)):
                marker.unlink(missing_ok=True)
        try:
            directory.rmdir()
        except OSError:
            pass


def prune(paths: Paths, dry_run: bool = False, codex_home: Path | None = None) -> dict:
    with activation_lock(paths):
        if not dry_run:
            remove_stale_markers(paths)
        current = current_commit(paths)
        previous = next(iter([c for c in known_good(paths, current) if paths.release(c).is_dir()]), None)
        used = releases_in_use(paths)
        hooked = hook_releases(paths, codex_home)
        removed, kept, stale = [], {}, []
        for entry in sorted(paths.releases.iterdir()) if paths.releases.is_dir() else []:
            if entry.name.startswith(STAGING_PREFIX):
                pid = entry.name[len(STAGING_PREFIX):].split("-", 1)[0]
                if not (pid.isdigit() and pid_alive(int(pid))):
                    stale.append(entry.name)
                    if not dry_run:
                        remove_tree(entry)
                continue
            commit = entry.name
            reason = ("current" if commit == current else "previous (last known-good)" if commit == previous
                      else used.get(commit) or ("registered Codex hook" if commit in hooked else None))
            if reason:
                kept[commit] = reason
                continue
            removed.append(commit)
            if not dry_run:
                remove_tree(entry)
                (paths.interpreters / commit).unlink(missing_ok=True)
        return {"removed": removed, "kept": kept, "stale_staging": stale, "dry_run": dry_run}


# --- Codex Stop hook migration --------------------------------------------

def hook_commands(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text())
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
    path = gate_path(command)
    if path is None:
        return None
    try:
        relative = Path(os.path.abspath(path)).relative_to(paths.releases)
    except ValueError:
        return None
    parts = relative.parts
    if len(parts) == 3 and COMMIT_PATTERN.match(parts[0]) and parts[1:] == ("hooks", "require-review.sh"):
        return parts[0]
    return None


def hook_provenance(paths: Paths, path: Path) -> str:
    """Why this hook script is a genuine Syndicate gate, or raise."""
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ReleaseError(f"registered hook {path} cannot be read ({error}); "
                           "its provenance cannot be verified, so hooks.json was not changed") from None
    commit = release_of(paths, shlex.quote(str(path)))
    if commit:
        manifest, _ = read_manifest(paths.release(commit))
        if manifest["commit"] == commit and manifest["files"].get("hooks/require-review.sh") == sha256_bytes(content):
            return f"release {commit} manifest"
        raise ReleaseError(f"registered hook {path} does not match its release manifest")
    checkout = path.parent.parent
    top = git(checkout, "rev-parse", "--show-toplevel", check=False)
    if top.returncode == 0 and os.path.realpath(top.stdout.strip()) == os.path.realpath(checkout):
        head = git(checkout, "rev-parse", "--verify", "HEAD^{commit}", check=False).stdout.strip()
        committed = git(checkout, "cat-file", "blob", "HEAD:hooks/require-review.sh", binary=True, check=False)
        if committed.returncode == 0 and committed.stdout == content:
            return f"checkout {checkout} HEAD {head}"
        raise ReleaseError(f"registered hook {path} differs from {checkout} HEAD:hooks/require-review.sh; "
                           "hooks.json was not changed")
    raise ReleaseError(f"registered hook {path} is neither in a Git checkout nor in a retained release; "
                       "hooks.json was not changed")


def migrate_codex_hook(paths: Paths, codex_home: Path, target: str | None = None, dry_run: bool = False) -> dict:
    """Point the registered Syndicate Stop hook at a release's gate script.

    Touches only hooks whose command is exactly one verified Syndicate
    require-review.sh path; preserves every other entry, sibling and field.
    """
    hooks_path = codex_home / "hooks.json"
    commit = target or current_commit(paths)
    if not commit:
        raise ReleaseError("no current release; install one or pass --target COMMIT")
    if len(commit) < 40:
        matches = [c.name for c in paths.releases.glob(commit + "*") if COMMIT_PATTERN.match(c.name)]
        if len(matches) != 1:
            raise ReleaseError(f"{commit!r} does not name exactly one retained release")
        commit = matches[0]
    release = paths.release(commit)
    verify_release(release, commit)
    new_path = Path(os.path.realpath(release)) / "hooks" / "require-review.sh"
    new_command = shlex.quote(str(new_path))
    if not hooks_path.is_file():
        return {"changed": False, "reason": f"{hooks_path} does not exist; no Syndicate hook is registered"}
    raw = hooks_path.read_bytes()
    try:
        data = json.loads(raw)
    except ValueError as error:
        raise ReleaseError(f"{hooks_path} is not valid JSON: {error}") from None
    stop = data.get("hooks", {}).get("Stop") if isinstance(data, dict) and isinstance(data.get("hooks"), dict) else None
    if not isinstance(stop, list):
        return {"changed": False, "reason": "no Stop hooks are registered"}
    changes, provenance = [], {}
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
            provenance[command] = hook_provenance(paths, path)
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
        reason = "already registered" if seen_new else "no Syndicate Stop hook is registered (not adding one)"
        return {"changed": False, "reason": reason, "command": new_command}
    result = {"changed": not dry_run, "dry_run": dry_run, "changes": changes, "command": new_command,
              "path": str(hooks_path)}
    if dry_run:
        return result
    data["hooks"]["Stop"] = new_stop
    backup = hooks_path.with_name(f"hooks.json.syndicate-backup-{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}")
    mode = stat.S_IMODE(os.stat(hooks_path).st_mode)
    with open(backup, "xb") as handle:
        handle.write(raw)
    os.chmod(backup, mode)
    atomic_write(hooks_path, (json.dumps(data, indent=2) + "\n").encode(), mode)
    result["backup"] = str(backup)
    return result


# --- status ---------------------------------------------------------------

def status(paths: Paths) -> dict:
    current = current_commit(paths)
    record = last_record(paths, current) if current else {}
    try:
        interpreter = (paths.interpreters / current).read_text().strip() if current else None
    except OSError:
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
        if entry.name.startswith("."):
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
        p.add_argument("--upstream", help="trusted upstream URL or path (remembered in the source cache)")
        p.add_argument("--python", help="absolute Python 3.11+ interpreter (default: the one running this)")
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
    sub.add_parser("install-shim", parents=[common], help="(re)write the stable entry point")
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = Paths.from_env(getattr(args, "install_root", None), getattr(args, "bin_dir", None),
                           getattr(args, "source_cache", None))
    codex_default = Path(os.environ["CODEX_HOME"]) if os.environ.get("CODEX_HOME") else Path.home() / ".codex"
    try:
        if args.command in ("install", "upgrade"):
            record = install(paths, args.tag, args.upstream, args.python, shim=not args.no_shim)
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
            emit(args, {"release": str(release), "commit": manifest["commit"], "ok": True,
                        "files": len(manifest["files"])}, f"ok: {release} ({len(manifest['files'])} files)")
        elif args.command == "prune":
            codex_home = Path(args.codex_home) if args.codex_home else codex_default
            result = prune(paths, args.dry_run, codex_home)
            emit(args, result, "\n".join([f"{'would remove' if args.dry_run else 'removed'}: {c}" for c in result["removed"]]
                                         + [f"kept {c}: {why}" for c, why in result["kept"].items()]
                                         + [f"stale staging: {s}" for s in result["stale_staging"]]))
        elif args.command == "install-shim":
            with activation_lock(paths):
                state = install_shim(paths)
            emit(args, {"shim": str(paths.shim), "state": state}, f"{state}: {paths.shim}")
        elif args.command == "restore-entrypoint":
            with activation_lock(paths):
                record = restore_entrypoint(paths)
            emit(args, record, f"restored {record['path']} ({record['kind']})")
        elif args.command == "migrate-codex-hook":
            codex_home = Path(args.codex_home) if args.codex_home else codex_default
            result = migrate_codex_hook(paths, codex_home, args.target, args.dry_run)
            lines = [f"{'would change' if args.dry_run else 'changed'}: {c}" for c in result.get("changes", [])]
            if result.get("backup"):
                lines.append(f"backup: {result['backup']}")
            if result.get("changes") and not args.dry_run:
                lines.append("Open 'codex' and approve the changed hook in the normal hook review prompt.")
            emit(args, result, "\n".join(lines) or result.get("reason", "nothing to do"))
    except ReleaseError as error:
        print(f"syndicate-install: {error}", file=sys.stderr)
        return 1
    return 0
