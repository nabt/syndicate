"""Per-user installer and release tests (bin/syndicate-install, bin/syndicate_release.py).

Every test uses disposable HOME/XDG/CODEX_HOME/CLAUDE_CONFIG_DIR/TMPDIR
directories and local fixture Git repositories (a bare "upstream" with tags
created here). Nothing touches the real ~/.local, ~/.syndicate, ~/.codex,
~/.claude, a tmux server or GitHub.

The fixture releases are built from the current worktree content (tracked and
untracked, not ignored) committed into a fixture repository, so they exercise
whatever runtime the branch currently carries.
"""
import fcntl
import hashlib
import http.client
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "bin" / "syndicate-install"
sys.path.insert(0, str(REPO / "bin"))
import syndicate_release as rel  # noqa: E402

MARKER = "Fixture release marker: {}"
FIXTURE = {}


def clean_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SYNDICATE_", "TMUX", "GIT_", "CODEX_", "CLAUDE_"))
           and k not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, PYTHONDONTWRITEBYTECODE="1",
               GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
               GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
    env.update(extra)
    return env


def run_git(repo, *args, env=None):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                            env=env or FIXTURE["env"])
    if result.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def setUpModule():
    base = Path(tempfile.mkdtemp(prefix="syndicate-install-fixture-"))
    home = base / "home"
    home.mkdir()
    env = clean_env(HOME=str(home))
    FIXTURE.update(base=base, env=env)
    src = base / "source checkout"
    upstream = base / "upstream repo.git"
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True, env=env)
    listed = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                            capture_output=True, check=True, env=env).stdout.split(b"\0")
    for raw in listed:
        name = raw.decode()
        origin = REPO / name
        if name and origin.is_file() and not origin.is_symlink():
            target = src / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, target)
    role = src / "roles" / "backend-engineer.md"
    original_role = role.read_text()

    def commit(message):
        run_git(src, "add", "-A")
        run_git(src, "commit", "-q", "-m", message)
        return run_git(src, "rev-parse", "HEAD")

    role.write_text(original_role + "\n" + MARKER.format("A") + "\n")
    a = commit("release A")
    run_git(src, "tag", "-a", "v1.0.0", "-m", "release A")
    run_git(src, "tag", "v1.0.1")  # lightweight, same commit
    role.write_text(original_role + "\n" + MARKER.format("B") + "\n")
    b = commit("release B")
    run_git(src, "tag", "-a", "v1.1.0", "-m", "release B")
    run_git(src, "branch", "v8.0.0")  # a branch that looks like a tag
    run_git(src, "checkout", "-q", "-b", "feature")
    (src / "README.md").write_text((src / "README.md").read_text() + "\nfeature only\n")
    side = commit("side branch release")
    run_git(src, "tag", "-a", "v9.0.0", "-m", "not on main")
    run_git(src, "checkout", "-q", "main")
    inbox = src / "skills" / "syndicate-inbox" / "SKILL.md"
    inbox_text = inbox.read_text()
    inbox.unlink()
    missing = commit("drop inbox skill")
    run_git(src, "tag", "v2.0.0")
    inbox.write_text(inbox_text)
    run_git(src, "add", "-A")
    run_git(src, "update-index", "--chmod=-x", "hooks/require-review.sh")
    run_git(src, "commit", "-q", "-m", "hook not executable")
    run_git(src, "tag", "v2.0.1")
    (src / "hooks" / "require-review.sh").chmod(0o755)
    docs = src / "docs" / "configuration.md"
    docs_text = docs.read_text()
    docs.write_text(docs_text + "\nSee [missing](no-such-page.md).\n")
    commit("broken doc link")
    run_git(src, "tag", "v2.0.2")
    docs.write_text(docs_text)
    c = commit("release C")
    run_git(src, "tag", "-a", "v1.2.0", "-m", "release C")
    subprocess.run(["git", "init", "-q", "--bare", str(upstream)], check=True, env=env)
    run_git(src, "push", "-q", str(upstream), "main", "feature", "v8.0.0", "--tags")
    FIXTURE.update(src=src, upstream=upstream, a=a, b=b, c=c, side=side, missing=missing)


def tearDownModule():
    base = FIXTURE.get("base")
    if base:
        rel.make_writable(base)
        shutil.rmtree(base, ignore_errors=True)


class InstallCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="syndicate-install-"))
        self.addCleanup(self._cleanup)
        for name in ("home", "config", "data home", "cache", "codex", "claude", "tmp", "tmux", "elsewhere", "fakebin"):
            (self.tmp / name).mkdir()
        self.env = clean_env(
            HOME=str(self.tmp / "home"), XDG_CONFIG_HOME=str(self.tmp / "config"),
            XDG_DATA_HOME=str(self.tmp / "data home"), XDG_CACHE_HOME=str(self.tmp / "cache"),
            CODEX_HOME=str(self.tmp / "codex"), CLAUDE_CONFIG_DIR=str(self.tmp / "claude"),
            TMPDIR=str(self.tmp / "tmp"), TMUX_TMPDIR=str(self.tmp / "tmux"),
            PATH=str(self.tmp / "fakebin") + os.pathsep + os.environ.get("PATH", ""))
        self.root = self.tmp / "data home" / "syndicate"
        self.paths = rel.Paths(self.root, self.tmp / "home" / ".local" / "bin",
                               self.tmp / "cache" / "syndicate" / "source.git")
        self.shim = self.paths.shim

    def _cleanup(self):
        rel.make_writable(self.tmp)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def installer(self, *args, ok=True, **kwargs):
        result = subprocess.run([sys.executable, "-B", str(INSTALLER), *args], cwd=self.tmp / "elsewhere",
                                env=self.env, capture_output=True, text=True, timeout=300, **kwargs)
        if ok and result.returncode:
            self.fail(f"syndicate-install {' '.join(args)} failed:\n{result.stderr}")
        if ok is False and result.returncode == 0:
            self.fail(f"syndicate-install {' '.join(args)} unexpectedly succeeded:\n{result.stdout}")
        return result

    def install(self, tag, *extra, **kwargs):
        return self.installer("install", "--tag", tag, "--upstream", str(FIXTURE["upstream"]), *extra, **kwargs)

    def current(self):
        return rel.current_commit(self.paths)

    def records(self):
        return rel.read_jsonl(self.paths.log)

    def release_entries(self):
        return sorted(p.name for p in self.paths.releases.iterdir()) if self.paths.releases.is_dir() else []

    def shim_run(self, *args, cwd=None, env=None, **kwargs):
        return subprocess.run([str(self.shim), *args], cwd=cwd or self.tmp / "elsewhere", env=env or self.env,
                              capture_output=True, text=True, timeout=120, **kwargs)

    def assert_unchanged(self, before_current, before_entries):
        self.assertEqual(self.current(), before_current)
        self.assertEqual(self.release_entries(), before_entries)


class StagingAndInventory(InstallCase):
    def test_install_ships_exact_allowlisted_inventory_with_modes(self):
        self.install("v1.0.0")
        commit = FIXTURE["a"]
        release = self.root / "releases" / commit
        self.assertEqual(os.readlink(self.root / "current"), f"releases/{commit}")
        tracked = run_git(FIXTURE["src"], "ls-tree", "-r", "--name-only", commit).splitlines()
        expected = {p for p in tracked if rel.allowed(p)}
        self.assertIn("bin/syndicate_runtime.py", expected)
        self.assertFalse(any(p.startswith("tests/") or p == ".gitignore" for p in expected))
        manifest = json.loads((release / "RELEASE.json").read_text())
        self.assertEqual(manifest["format"], 1)
        self.assertEqual(manifest["commit"], commit)
        self.assertEqual(manifest["python_min"], "3.11")
        self.assertEqual(set(manifest["files"]), expected)
        for path in expected:
            blob = subprocess.run(["git", "-C", str(FIXTURE["src"]), "cat-file", "blob", f"{commit}:{path}"],
                                  capture_output=True, check=True, env=FIXTURE["env"]).stdout
            self.assertEqual(manifest["files"][path], hashlib.sha256(blob).hexdigest(), path)
        for path in ("roles/orchestrator.md", "skills/syndicate-inbox/SKILL.md", "web/dashboard.html",
                     "portfolio/chief-of-staff.md", "README.md", "docs/configuration.md",
                     "resources/codex/rules/common.rules", "bin/syndicate-install"):
            self.assertIn(path, manifest["files"])
        for module in (REPO / "bin").glob("syndicate_*.py"):
            self.assertIn(f"bin/{module.name}", manifest["files"])
        for path in ("bin/syndicate", "hooks/require-review.sh", "bin/syndicate-install"):
            self.assertTrue(os.access(release / path, os.X_OK), path)
        self.assertFalse(os.stat(release / "README.md").st_mode & 0o111)
        for directory, dirs, names in os.walk(release):
            self.assertFalse(os.stat(directory).st_mode & 0o222, directory)
            for name in names:
                self.assertFalse(os.stat(Path(directory) / name).st_mode & 0o222, name)
        (record,) = self.records()
        self.assertEqual(set(record) - {"action"}, {"at", "commit", "tag", "source_ref", "upstream", "archive_digest",
                                                    "manifest_sha256", "release_dir", "interpreter", "previous_commit"})
        self.assertEqual((record["commit"], record["tag"], record["source_ref"], record["previous_commit"]),
                         (commit, "v1.0.0", "refs/tags/v1.0.0", None))
        self.assertEqual(record["manifest_sha256"], hashlib.sha256((release / "RELEASE.json").read_bytes()).hexdigest())
        self.assertEqual(record["upstream"], str(FIXTURE["upstream"]))
        self.assertEqual(record["interpreter"], os.path.abspath(sys.executable))
        self.assertEqual((self.root / "interpreters" / commit).read_text(), record["interpreter"] + "\n")
        self.assertTrue(rel.is_managed_shim(self.shim))
        self.assertTrue(os.access(self.shim, os.X_OK))
        self.assertEqual(self.installer("verify").returncode, 0)
        self.assertFalse([p for p in self.paths.releases.iterdir() if p.name.startswith(".")])

    def test_staging_uses_only_committed_source(self):
        src = FIXTURE["src"]
        launcher = src / "bin" / "syndicate"
        original = launcher.read_bytes()
        rogue = src / "roles" / "rogue.md"
        self.addCleanup(lambda: (launcher.write_bytes(original), rogue.unlink(missing_ok=True),
                                 run_git(src, "reset", "-q")))
        launcher.write_bytes(original + b"\n# uncommitted edit\n")
        rogue.write_text("---\nname: rogue\n---\nUntracked role\n")
        run_git(src, "add", "roles/rogue.md")  # staged but not committed
        # A dirty checkout is never an upstream (agents in it could move main and tags)...
        result = self.installer("install", "--tag", "v1.2.0", "--upstream", str(src), ok=False)
        self.assertIn("not a bare repository", result.stderr)
        self.assertEqual(self.release_entries(), [])
        # ...and from the bare upstream only committed objects are used.
        self.install("v1.2.0")
        release = self.root / "releases" / FIXTURE["c"]
        self.assertEqual((release / "bin" / "syndicate").read_bytes(), original)
        self.assertFalse((release / "roles" / "rogue.md").exists())
        self.assertNotIn("roles/rogue.md", json.loads((release / "RELEASE.json").read_text())["files"])

    def test_reinstalling_same_commit_via_another_tag_reuses_release(self):
        self.install("v1.0.0")
        release = self.root / "releases" / FIXTURE["a"]
        inode = os.stat(release / "bin" / "syndicate").st_ino
        result = self.install("v1.0.1")
        self.assertIn("reusing verified identical release", result.stderr)
        self.assertEqual(os.stat(release / "bin" / "syndicate").st_ino, inode)
        first, second = self.records()
        self.assertEqual((second["tag"], second["commit"], second["previous_commit"]),
                         ("v1.0.1", FIXTURE["a"], FIXTURE["a"]))
        self.assertEqual(first["manifest_sha256"], second["manifest_sha256"])


class SourcePolicy(InstallCase):
    def test_non_tag_refs_are_rejected(self):
        for ref in ("main", "feature", FIXTURE["a"], FIXTURE["a"][:12], "v1.0", "refs/tags/v1.0.0",
                    "v1.0.0^{}", "v01.0.0", "--upload-pack=touch", "1.0.0"):
            with self.subTest(ref=ref):
                result = self.installer("install", "--tag=" + ref, "--upstream", str(FIXTURE["upstream"]), ok=False)
                self.assertIn("not an approved release tag", result.stderr)
        # Matches the pattern but is a branch, not a tag, upstream.
        result = self.install("v8.0.0", ok=False)
        self.assertIn("not found", result.stderr)
        self.assertIsNone(self.current())
        self.assertEqual(self.release_entries(), [])
        self.assertFalse(self.shim.exists())

    def test_tag_not_reachable_from_upstream_main_is_rejected(self):
        self.install("v1.0.0")
        result = self.install("v9.0.0", ok=False)
        self.assertIn("not reachable from upstream main", result.stderr)
        self.assert_unchanged(FIXTURE["a"], [FIXTURE["a"]])

    def test_install_requires_a_fresh_fetch(self):
        upstream = self.tmp / "upstream copy.git"
        shutil.copytree(FIXTURE["upstream"], upstream)
        self.installer("install", "--tag", "v1.0.0", "--upstream", str(upstream))
        # The tag disappears upstream: the cached copy must not be used.
        run_git(upstream, "tag", "-d", "v1.1.0")
        result = self.installer("install", "--tag", "v1.1.0", ok=False)
        self.assertIn("not found", result.stderr)
        # Upstream unreachable: neither a new nor an already fetched tag installs.
        moved = upstream.with_name("moved away.git")
        upstream.rename(moved)
        for tag in ("v1.2.0", "v1.0.0"):
            result = self.installer("install", "--tag", tag, ok=False)
            self.assertIn("cannot fetch main", result.stderr)
        self.assert_unchanged(FIXTURE["a"], [FIXTURE["a"]])
        self.assertEqual(len(self.records()), 1)

    def test_upstream_is_required_on_first_use(self):
        result = self.installer("install", "--tag", "v1.0.0", ok=False)
        self.assertIn("--upstream", result.stderr)
        result = self.installer("install", "--tag", "v1.0.0", "--upstream=--upload-pack=touch", ok=False)
        self.assertIn("invalid upstream", result.stderr)

    def test_defaults_follow_home_and_xdg(self):
        paths = rel.Paths.from_env(environ={"HOME": "/h", "XDG_DATA_HOME": "/d", "XDG_CACHE_HOME": "/c"})
        self.assertEqual((paths.root, paths.bin_dir, paths.cache),
                         (Path("/d/syndicate"), Path("/h/.local/bin"), Path("/c/syndicate/source.git")))
        paths = rel.Paths.from_env(environ={"HOME": "/h"})
        self.assertEqual((paths.root, paths.cache),
                         (Path("/h/.local/share/syndicate"), Path("/h/.cache/syndicate/source.git")))


class Verification(InstallCase):
    def test_missing_or_broken_assets_are_rejected_before_activation(self):
        self.install("v1.0.0")
        before = (FIXTURE["a"], [FIXTURE["a"]])
        for tag, message in (("v2.0.0", "skills/syndicate-inbox/SKILL.md"),
                             ("v2.0.1", "hooks/require-review.sh is not executable"),
                             ("v2.0.2", "no-such-page.md")):
            with self.subTest(tag=tag):
                result = self.install(tag, ok=False)
                self.assertIn(message, result.stderr)
                self.assert_unchanged(*before)
        self.assertEqual(len(self.records()), 1)

    def test_tampered_or_malformed_release_fails_verification(self):
        self.install("v1.0.0")
        self.install("v1.1.0")
        release = self.root / "releases" / FIXTURE["a"]
        role = release / "roles" / "orchestrator.md"
        os.chmod(role.parent, 0o755)
        os.chmod(role, 0o644)
        role.write_text(role.read_text() + "tampered\n")
        result = self.installer("verify", FIXTURE["a"][:10], ok=False)
        self.assertIn("roles/orchestrator.md does not match", result.stderr)
        result = self.installer("rollback", ok=False)
        self.assertIn("failed verification", result.stderr)
        self.assertEqual(self.current(), FIXTURE["b"])
        # A tampered release is never silently reused or replaced.
        result = self.install("v1.0.0", ok=False)
        self.assertIn("failed verification", result.stderr)
        self.assertEqual(self.current(), FIXTURE["b"])
        extra = release / "roles" / "extra.md"
        extra.write_text("x")
        self.assertIn("unexpected file roles/extra.md", self.installer("verify", FIXTURE["a"], ok=False).stderr)
        os.chmod(release, 0o755)
        manifest = release / "RELEASE.json"
        os.chmod(manifest, 0o644)
        for text, message in (("{not json", "not valid JSON"),
                              (json.dumps({"format": 2, "commit": FIXTURE["a"], "files": {}}), "malformed"),
                              (json.dumps({"format": 1, "commit": "abc", "files": {"a": "b"},
                                           "python_min": "3.11"}), "malformed")):
            manifest.write_text(text)
            self.assertIn(message, self.installer("verify", FIXTURE["a"], ok=False).stderr)
            self.assertIn(message, self.installer("rollback", "--to", FIXTURE["a"], ok=False).stderr)
        self.assertEqual(self.current(), FIXTURE["b"])
        self.assertEqual(self.installer("verify").returncode, 0)

    def test_bad_interpreters_are_rejected_before_staging(self):
        old = self.tmp / "fakebin" / "python3.10"
        old.write_text("#!/bin/sh\necho 3.10.14\n")
        old.chmod(0o755)
        broken = self.tmp / "fakebin" / "broken-python"
        broken.write_text("#!/bin/sh\nexit 1\n")
        broken.chmod(0o755)
        plain = self.tmp / "fakebin" / "not-executable"
        plain.write_text("")
        for interpreter, message in (("python3", "absolute path"), (str(self.tmp / "missing"), "not an executable"),
                                     (str(plain), "not an executable"), (str(old), "requires 3.11"),
                                     (str(broken), "did not report a Python version")):
            with self.subTest(interpreter=interpreter):
                result = self.install("v1.0.0", "--python", interpreter, ok=False)
                self.assertIn(message, result.stderr)
                self.assertIsNone(self.current())
                self.assertEqual(self.release_entries(), [])

    def test_paths_with_a_closing_parenthesis_are_refused(self):
        for bad_root, char in ((self.tmp / "store (old)" / "syndicate", "("),
                               (self.tmp / "store old)" / "syndicate", ")"), (self.tmp / "store*" / "syndicate", "*")):
            result = self.install("v1.0.0", "--install-root", str(bad_root), ok=False)
            self.assertIn(f"contains {char!r}", result.stderr)
            self.assertFalse(bad_root.exists())
        python = self.tmp / "py)thon"
        python.symlink_to(os.path.abspath(sys.executable))
        result = self.install("v1.0.0", "--python", str(python), ok=False)
        self.assertIn("interpreter", result.stderr)
        self.assertIn("contains ')'", result.stderr)
        self.assertEqual((self.current(), self.release_entries()), (None, []))
        self.install("v1.0.0")
        self.install("v1.1.0")
        result = self.installer("rollback", "--python", str(python), ok=False)
        self.assertIn("contains ')'", result.stderr)
        self.assertEqual(self.current(), FIXTURE["b"])

    def test_rollback_rejects_an_interpreter_that_is_no_longer_usable(self):
        python = self.tmp / "fakebin" / "python-link"
        python.symlink_to(os.path.abspath(sys.executable))
        self.install("v1.0.0", "--python", str(python))
        self.install("v1.1.0")
        python.unlink()
        result = self.installer("rollback", ok=False)
        self.assertIn("not an executable", result.stderr)
        self.assertEqual(self.current(), FIXTURE["b"])
        self.installer("rollback", "--python", os.path.abspath(sys.executable))
        self.assertEqual(self.current(), FIXTURE["a"])


class Activation(InstallCase):
    def test_upgrade_rollback_and_repeat_reuse(self):
        a, b = FIXTURE["a"], FIXTURE["b"]
        self.install("v1.0.0")
        inode = os.stat(self.root / "releases" / a / "RELEASE.json").st_ino
        self.install("v1.1.0")
        self.assertEqual(self.current(), b)
        self.install("v1.0.0")  # A -> B -> A reuses A
        self.assertEqual(os.stat(self.root / "releases" / a / "RELEASE.json").st_ino, inode)
        self.assertEqual([(r["commit"], r["previous_commit"]) for r in self.records()],
                         [(a, None), (b, a), (a, b)])
        self.installer("rollback")
        self.assertEqual(self.current(), b)
        rollback = self.records()[-1]
        self.assertEqual((rollback["action"], rollback["previous_commit"], rollback["tag"]), ("rollback", a, "v1.1.0"))
        self.installer("rollback", "--to", a[:12])
        self.assertEqual(self.current(), a)
        result = self.installer("rollback", "--to", FIXTURE["c"], ok=False)
        self.assertIn("never activated", result.stderr)
        result = self.installer("rollback", "--to", a, ok=False)
        self.assertIn("already the current release", result.stderr)
        self.assertEqual(json.loads(self.installer("--json", "status").stdout)["previous"], b)
        listing = json.loads(self.installer("list", "--json").stdout)
        self.assertEqual({r["commit"]: r["marks"] for r in listing}, {a: ["current"], b: ["previous"]})

    def test_rollback_without_history_fails(self):
        self.assertIn("no retained known-good release", self.installer("rollback", ok=False).stderr)
        self.install("v1.0.0")
        self.assertIn("no retained known-good release", self.installer("rollback", ok=False).stderr)

    def test_prune_keeps_current_previous_and_in_use(self):
        a, b, c = FIXTURE["a"], FIXTURE["b"], FIXTURE["c"]
        for tag in ("v1.0.0", "v1.1.0", "v1.2.0"):
            self.install(tag)
        marker = self.root / "in-use" / a / str(os.getpid())
        marker.parent.mkdir(parents=True)
        marker.touch()
        result = json.loads(self.installer("prune", "--json").stdout)
        self.assertEqual(result["removed"], [])
        self.assertEqual(set(result["kept"]), {a, b, c})
        marker.unlink()
        result = json.loads(self.installer("prune", "--dry-run", "--json").stdout)
        self.assertEqual(result["removed"], [a])
        self.assertTrue((self.root / "releases" / a).is_dir())
        result = json.loads(self.installer("prune", "--json").stdout)
        self.assertEqual(result["removed"], [a])
        self.assertEqual(self.release_entries(), sorted([b, c]))
        self.assertFalse((self.root / "interpreters" / a).exists())
        self.assertEqual(self.current(), c)
        self.installer("rollback")
        self.assertEqual(self.current(), b)

    def test_prune_ignores_and_removes_markers_of_gone_processes(self):
        a, b, c = FIXTURE["a"], FIXTURE["b"], FIXTURE["c"]
        for tag in ("v1.0.0", "v1.1.0", "v1.2.0"):
            self.install(tag)
        dead = subprocess.Popen([sys.executable, "-B", "-c", ""])
        dead.wait()
        stale = self.root / "in-use" / a / str(dead.pid)
        stale.parent.mkdir(parents=True)
        stale.write_text("{}")
        live = self.root / "in-use" / b / str(os.getpid())
        live.parent.mkdir(parents=True)
        live.write_text("{}")
        result = json.loads(self.installer("prune", "--dry-run", "--json").stdout)
        self.assertEqual(result["removed"], [a])
        self.assertTrue(stale.exists())  # a dry run changes nothing
        result = json.loads(self.installer("prune", "--json").stdout)
        self.assertEqual(result["removed"], [a])
        self.assertFalse(stale.parent.exists())
        self.assertTrue(live.exists())

    def test_exception_during_staging_keeps_previous_release(self):
        self.install("v1.0.0")
        quiet = mock.patch.object(rel, "log")
        quiet.start()
        self.addCleanup(quiet.stop)
        with mock.patch.object(rel, "smoke_test", side_effect=rel.ReleaseError("injected smoke failure")), \
             mock.patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}):
            with self.assertRaisesRegex(rel.ReleaseError, "injected"):
                rel.install(self.paths, "v1.1.0", str(FIXTURE["upstream"]), os.path.abspath(sys.executable))
        with mock.patch.object(rel, "switch_current", side_effect=KeyboardInterrupt), \
             mock.patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}):
            with self.assertRaises(KeyboardInterrupt):
                rel.install(self.paths, "v1.1.0", str(FIXTURE["upstream"]), os.path.abspath(sys.executable))
        # Interrupted before the switch: B may be published but A stays selected.
        self.assertEqual(self.current(), FIXTURE["a"])
        self.assertFalse([p for p in self.paths.releases.iterdir() if p.name.startswith(".")])
        self.assertEqual(len(self.records()), 1)
        self.install("v1.1.0")
        self.assertEqual(self.current(), FIXTURE["b"])

    def test_killed_staging_keeps_previous_release_and_prune_clears_it(self):
        self.install("v1.0.0")
        mark = self.tmp / "smoke-reached"
        wrapper = self.tmp / "fakebin" / "stalling-python"
        wrapper.write_text("#!/bin/sh\nfor a in \"$@\"; do case \"$a\" in */bin/syndicate) "
                           f": > '{mark}'; exec sleep 600;; esac; done\n"
                           f"exec '{os.path.abspath(sys.executable)}' \"$@\"\n")
        wrapper.chmod(0o755)
        proc = subprocess.Popen([sys.executable, "-B", str(INSTALLER), "install", "--tag", "v1.1.0",
                                 "--python", str(wrapper)], cwd=self.tmp / "elsewhere", env=self.env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            deadline = time.time() + 120
            while not mark.exists():
                self.assertIsNone(proc.poll(), "installer exited before reaching the smoke test")
                self.assertLess(time.time(), deadline)
                time.sleep(0.05)
        finally:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        stale = [p.name for p in self.paths.releases.iterdir() if p.name.startswith(".staging-")]
        self.assertEqual(len(stale), 1)
        self.assertEqual(self.current(), FIXTURE["a"])
        self.assertFalse((self.root / "releases" / FIXTURE["b"]).exists())
        self.assertEqual(self.shim_run("--help").returncode, 0)
        result = json.loads(self.installer("prune", "--json").stdout)
        self.assertEqual(result["stale_staging"], stale)
        self.assertEqual(self.release_entries(), [FIXTURE["a"]])
        self.install("v1.1.0")
        self.assertEqual(self.current(), FIXTURE["b"])

    def test_concurrent_activations_serialize_and_current_stays_valid(self):
        self.install("v1.0.0")
        failures, stop = [], threading.Event()

        def watch():
            while not stop.is_set():
                try:
                    target = os.readlink(self.root / "current")
                    commit = Path(target).name
                    if not (self.root / target / "RELEASE.json").is_file():
                        failures.append(f"current -> {target} has no manifest")
                    if not (self.root / "interpreters" / commit).read_text().strip():
                        failures.append(f"no interpreter for {commit}")
                except OSError as error:
                    failures.append(repr(error))
                time.sleep(0.001)

        logs = [self.tmp / "first.err", self.tmp / "second.err"]
        procs = []
        with open(self.paths.lock, "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            for tag, log_path in zip(("v1.1.0", "v1.2.0"), logs):
                with open(log_path, "w") as err:
                    procs.append(subprocess.Popen(
                        [sys.executable, "-B", str(INSTALLER), "install", "--tag", tag],
                        cwd=self.tmp / "elsewhere", env=self.env, stdout=subprocess.DEVNULL, stderr=err))
                    self.addCleanup(lambda proc=procs[-1]: proc.poll() is None and (proc.kill(), proc.wait()))
            deadline = time.time() + 240
            while not all("waiting for activation lock" in p.read_text() for p in logs):
                self.assertTrue(all(p.poll() is None for p in procs), [p.read_text() for p in logs])
                self.assertLess(time.time(), deadline)
                time.sleep(0.05)
            watcher = threading.Thread(target=watch)
            watcher.start()
            fcntl.flock(held, fcntl.LOCK_UN)
        try:
            for proc in procs:
                self.assertEqual(proc.wait(timeout=240), 0, [p.read_text() for p in logs])
        finally:
            stop.set()
            watcher.join()
        self.assertEqual(failures, [])
        first, second = self.records()[1:]
        self.assertEqual(first["previous_commit"], FIXTURE["a"])
        self.assertEqual(second["previous_commit"], first["commit"])
        self.assertEqual(self.current(), second["commit"])
        self.assertEqual({first["commit"], second["commit"]}, {FIXTURE["b"], FIXTURE["c"]})


class EntryPoint(InstallCase):
    def test_existing_symlink_entrypoint_is_recorded_and_restorable(self):
        self.shim.parent.mkdir(parents=True)
        checkout_launcher = FIXTURE["src"] / "bin" / "syndicate"
        self.shim.symlink_to(checkout_launcher)
        self.install("v1.0.0")
        self.assertTrue(rel.is_managed_shim(self.shim))
        (record,) = rel.read_jsonl(self.paths.entrypoints)
        self.assertEqual((record["kind"], record["target"]), ("symlink", str(checkout_launcher)))
        self.install("v1.1.0")  # the managed shim is not recorded again
        self.assertEqual(len(rel.read_jsonl(self.paths.entrypoints)), 1)
        self.installer("restore-entrypoint")
        self.assertEqual(os.readlink(self.shim), str(checkout_launcher))

    def test_unknown_file_entrypoint_is_backed_up_not_lost(self):
        self.shim.parent.mkdir(parents=True)
        self.shim.write_text("#!/bin/sh\necho custom launcher\n")
        self.shim.chmod(0o750)
        self.install("v1.0.0")
        (record,) = rel.read_jsonl(self.paths.entrypoints)
        self.assertEqual(record["kind"], "file")
        self.assertEqual(Path(record["backup"]).read_text(), "#!/bin/sh\necho custom launcher\n")
        self.installer("restore-entrypoint")
        self.assertEqual(self.shim.read_text(), "#!/bin/sh\necho custom launcher\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.shim).st_mode), 0o750)
        # Restore never overwrites something that is not the managed shim.
        self.assertIn("not the managed shim", self.installer("restore-entrypoint", ok=False).stderr)

    def test_no_shim_leaves_entrypoint_alone(self):
        self.install("v1.0.0", "--no-shim")
        self.assertFalse(self.shim.exists())
        self.installer("install-shim")
        self.assertTrue(rel.is_managed_shim(self.shim))

    def test_shim_reports_missing_release(self):
        self.installer("install-shim")
        result = self.shim_run("--help")
        self.assertEqual(result.returncode, 127)
        self.assertIn("no active release", result.stderr)


class Boot(InstallCase):
    """Run the activated release through the shim with the source checkout gone."""

    def setUp(self):
        super().setUp()
        self.install("v1.0.0")
        self.release = Path(os.path.realpath(self.root / "releases" / FIXTURE["a"]))
        src, upstream = FIXTURE["src"], FIXTURE["upstream"]
        hidden_src, hidden_up = src.with_name("hidden checkout"), upstream.with_name("hidden upstream.git")
        src.rename(hidden_src)
        upstream.rename(hidden_up)
        self.addCleanup(upstream.with_name("hidden upstream.git").rename, upstream)
        self.addCleanup(hidden_src.rename, src)

    def assert_no_checkout_paths(self, text):
        self.assertNotIn(str(FIXTURE["src"]), text)
        self.assertNotIn(str(REPO), text)

    def test_shim_boots_release_from_unrelated_cwd(self):
        result = self.shim_run("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: syndicate", result.stdout)
        result = self.shim_run("chief-of-staff", "--dry-run", "-x", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        launch = json.loads(result.stdout)
        instructions = next(a for a in launch["argv"] if a.startswith("developer_instructions="))
        self.assertIn("# Chief of Staff", instructions)
        self.assertIn(str(self.release), instructions)
        self.assert_no_checkout_paths(result.stdout)
        hook = self.release / "hooks" / "require-review.sh"
        self.assertTrue(os.access(hook, os.X_OK))
        if shutil.which("jq") and shutil.which("bash"):
            gate = subprocess.run([str(hook)], input=json.dumps({"stop_hook_active": True}), env=self.env,
                                  capture_output=True, text=True, cwd=self.tmp / "elsewhere", timeout=60)
            self.assertEqual((gate.returncode, gate.stdout), (0, ""))
        self.assertEqual(list(self.release.rglob("__pycache__")), [])
        self.assertEqual(rel.verify_release(self.release)["commit"], FIXTURE["a"])

    def test_version_reports_release_identity(self):
        if not rel.supports_version(self.release):
            self.skipTest("phase 1: release has no side-effect-free 'syndicate --version' yet")
        result = self.shim_run("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(FIXTURE["a"], result.stdout)
        self.assertIn(str(self.release), result.stdout)

    def test_dashboard_serves_from_release(self):
        # --no-monitor: the dashboard never spawns a background _limits-monitor
        # in this disposable home, at launch or when limits are fetched.
        state = self.tmp / "home" / ".syndicate" / "limits"
        env = dict(self.env, PYTHONUNBUFFERED="1")
        proc = subprocess.Popen([str(self.shim), "web", "--port", "0", "--no-monitor"], cwd=self.tmp / "elsewhere",
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            line = proc.stdout.readline()
            self.assertIn("http://127.0.0.1:", line, proc.stderr.read() if proc.poll() is not None else line)
            port = int(line.split("http://127.0.0.1:", 1)[1].split("/", 1)[0])
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            connection.request("GET", "/")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), (self.release / "web" / "dashboard.html").read_bytes())
            connection.request("GET", "/api/status")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIsInstance(json.loads(response.read()), list)
            connection.request("GET", "/api/limits")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("summary", json.loads(response.read()))
            connection.close()
        finally:
            proc.terminate()
            proc.wait(timeout=30)
            proc.stdout.close()
            proc.stderr.close()
        self.assertFalse((state / "monitor.lock").exists(), "web --no-monitor started the limits monitor")
        self.assertFalse((state / "monitor.json").exists())
        self.assertEqual(list(self.release.rglob("__pycache__")), [])

class NonDefaultInstallRoot(InstallCase):
    def test_version_finds_activation_log_under_a_non_default_install_root(self):
        root, bin_dir = self.tmp / "custom store" / "syn", self.tmp / "custom bin"
        self.installer("install", "--tag", "v1.0.1", "--upstream", str(FIXTURE["upstream"]),
                       "--install-root", str(root), "--bin-dir", str(bin_dir))
        release = Path(os.path.realpath(root / "releases" / FIXTURE["a"]))
        # XDG_DATA_HOME names another store whose log says something else.
        other = self.tmp / "data home" / "syndicate" / "activations.jsonl"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text(json.dumps({"commit": FIXTURE["a"], "tag": "v0.0.9", "at": "2000-01-01T00:00:00Z"}) + "\n")
        result = subprocess.run([str(bin_dir / "syndicate"), "--version", "--json"], cwd=self.tmp / "elsewhere",
                                env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.assertEqual((info["mode"], info["commit"], info["root"]), ("release", FIXTURE["a"], str(release)))
        self.assertEqual(info["tag"], "v1.0.1")
        self.assertEqual(info["activation"]["at"], rel.read_jsonl(root / "activations.jsonl")[-1]["at"])


FAKE_CODEX = r'''
import json, os, re, shlex, subprocess, sys, time
from pathlib import Path

LOG = os.environ["FAKE_CODEX_LOG"]


def ancestors():
    chain, pid = [], os.getppid()
    while pid > 1 and len(chain) < 12:
        try:
            args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            parent = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except OSError:
            break
        chain.append([a.decode() for a in args if a])
        pid = parent
    return chain


def record(kind, **data):
    with open(LOG, "a") as handle:
        handle.write(json.dumps(dict(kind=kind, argv=sys.argv[1:], ancestors=ancestors(), **data)) + "\n")


argv = sys.argv[1:]
raw = next((a.split("=", 1)[1] for a in argv if a.startswith("developer_instructions=")), '""')
instructions = json.loads(raw)
IN_USE = Path(os.environ["FAKE_CODEX_IN_USE"])
ENV_KEYS = ("PYTHONDONTWRITEBYTECODE", "SYNDICATE_USER_PYTHONDONTWRITEBYTECODE", "SYNDICATE_RELEASE_ROOT")


def markers():
    return {d.name: sorted(m.name for m in d.iterdir()) for d in IN_USE.iterdir()} if IN_USE.is_dir() else {}


if argv[:1] == ["exec"]:
    record("specialist", instructions=instructions, brief=argv[-1], pid=os.getpid(), ppid=os.getppid(),
           markers=markers(), env={k: os.environ.get(k) for k in ENV_KEYS})
    print("specialist finished")
    sys.exit(0)

project = argv[argv.index("-C") + 1]
configs = [json.loads(a.split("=", 1)[1]) for a in argv if re.match(r"agents\.[^.=]+\.config_file=", a)]
before = {c: Path(c).read_text() for c in configs}
record("orchestrator", configs=before, env={k: os.environ.get(k) for k in ENV_KEYS})
deadline = time.time() + 240
while not os.path.exists(os.environ["FAKE_CODEX_GO"]):
    if time.time() > deadline:
        sys.exit(3)
    time.sleep(0.05)
prefix = shlex.split(re.search(r"Syndicate executable prefix: (.+)", instructions).group(1))
run = json.loads(Path(project, ".syndicate", "run.json").read_text())
results = {}


def call(name, *args):
    done = subprocess.run(prefix + list(args), capture_output=True, text=True, cwd=project, timeout=180)
    results[name] = dict(rc=done.returncode, out=done.stdout, err=done.stderr)


call("version", "--version")
call("live", "live", "status", "-C", project)
call("run_role", "team", "run-role", "backend-engineer", "--project", project, "delayed specialist brief")
completion = re.search(r"Syndicate specialist completed: backend-engineer codex (\w+)", results["run_role"]["out"])
call("close", "tasks", "close", run["task_id"], "-C", project, "--session", run["session_id"], "--state", "done",
     "--progress", "fixture progress", "--message", "fixture closeout",
     "--implementation", "backend-engineer", "codex", completion.group(1) if completion else "missing",
     "--review", "code-reviewer", "codex", "fixture-reviewer")
record("orchestrator-done", prefix=prefix, results=results,
       configs_after={c: Path(c).read_text() if Path(c).exists() else None for c in configs})
'''


@unittest.skipUnless(Path("/proc/self/cmdline").exists(), "needs /proc to identify the running release")
class ActiveSessionAcrossActivation(InstallCase):
    def test_a_session_keeps_release_a_after_b_activates(self):
        a, b = FIXTURE["a"], FIXTURE["b"]
        self.install("v1.0.0")
        release_a = Path(os.path.realpath(self.root / "releases" / a))
        fake = self.tmp / "fakebin" / "codex"
        fake.write_text(f"#!{os.path.abspath(sys.executable)}\n{FAKE_CODEX}")
        fake.chmod(0o755)
        log_path, go = self.tmp / "codex.jsonl", self.tmp / "go"
        config = self.tmp / "config" / "syndicate" / "syndicate.conf"
        config.parent.mkdir(parents=True)
        config.write_text("tmux = off\ndefault = codex\nhandoff = off\n")
        project = self.tmp / "pro ject"
        project.mkdir()
        run_git(project, "init", "-q", "-b", "main")
        run_git(project, "commit", "-q", "--allow-empty", "-m", "init")
        # The caller has not set PYTHONDONTWRITEBYTECODE: provider CLIs must not get it.
        env = {k: v for k, v in self.env.items() if k != "PYTHONDONTWRITEBYTECODE"}
        env.update(FAKE_CODEX_LOG=str(log_path), FAKE_CODEX_GO=str(go),
                   FAKE_CODEX_IN_USE=str(Path(os.path.realpath(self.root)) / "in-use"))
        session = subprocess.Popen([str(self.shim), "team", "task", "--project", str(project), "fixture task"],
                                   cwd=self.tmp / "elsewhere", env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.time() + 120
            while not (log_path.exists() and '"orchestrator"' in log_path.read_text()):
                self.assertIsNone(session.poll(), session.stderr.read() if session.poll() is not None else "")
                self.assertLess(time.time(), deadline)
                time.sleep(0.05)
            self.install("v1.1.0")
            self.assertEqual(self.current(), b)
            # B generates its own per-run settings in the same project while A is live
            # (the project lock stops a second team, so B's code path runs directly).
            started = next(json.loads(line) for line in log_path.read_text().splitlines()
                           if json.loads(line)["kind"] == "orchestrator")
            run_a = Path(next(iter(started["configs"]))).parent.parent
            before_a = {str(p): p.read_bytes() for p in run_a.rglob("*") if p.is_file()}
            generated_b = self.generate_with(Path(os.path.realpath(self.root / "releases" / b)), project)
            # A second upgrade: A is now neither current nor previous, only live.
            self.install("v1.2.0")
            self.assertEqual(self.current(), FIXTURE["c"])
            kept = json.loads(self.installer("prune", "--dry-run", "--json").stdout)["kept"]
            self.assertIn(a, kept)
            # Without markers, /proc still finds the session: the CLI the launcher
            # exec'd into names the release only inside longer arguments and in
            # its SYNDICATE_RELEASE_ROOT environment.
            hidden = self.root / "in-use hidden"
            (self.root / "in-use").rename(hidden)
            try:
                kept = json.loads(self.installer("prune", "--dry-run", "--json").stdout)["kept"]
            finally:
                hidden.rename(self.root / "in-use")
            self.assertTrue(kept.get(a, "").startswith("process "), kept)
            go.touch()
            out, err = session.communicate(timeout=300)
        finally:
            if session.poll() is None:
                session.kill()
                session.communicate()
        self.assertEqual(session.returncode, 0, err)
        records = [json.loads(line) for line in log_path.read_text().splitlines()]
        orchestrator = next(r for r in records if r["kind"] == "orchestrator")
        done = next(r for r in records if r["kind"] == "orchestrator-done")
        specialist = next(r for r in records if r["kind"] == "specialist")
        launcher_a = str(release_a / "bin" / "syndicate")
        self.assertIn(launcher_a, done["prefix"])
        results = done["results"]
        for name in ("live", "run_role", "close"):
            self.assertEqual(results[name]["rc"], 0, (name, results[name]))
        # Delayed specialist launch ran A's code and A's role text.
        self.assertTrue(any(launcher_a in args for args in specialist["ancestors"]), specialist["ancestors"])
        self.assertIn(MARKER.format("A"), specialist["instructions"])
        self.assertNotIn(MARKER.format("B"), specialist["instructions"])
        self.assertEqual(specialist["brief"], "delayed specialist brief")
        # The relay (run-role) marked itself and the specialist CLI it started as using A.
        self.assertIn(str(specialist["pid"]), specialist["markers"].get(a, []), specialist["markers"])
        self.assertIn(str(specialist["ppid"]), specialist["markers"].get(a, []), specialist["markers"])
        # Provider CLIs get the caller's bytecode setting back (unset here) and name their release.
        for launched in (orchestrator, specialist):
            self.assertEqual(launched["env"], {"PYTHONDONTWRITEBYTECODE": None,
                                               "SYNDICATE_USER_PYTHONDONTWRITEBYTECODE": None,
                                               "SYNDICATE_RELEASE_ROOT": str(release_a)})
        self.assertIn("Syndicate specialist completed: backend-engineer codex", results["run_role"]["out"])
        self.assertIn("fixture closeout", results["close"]["out"])
        if rel.supports_version(release_a):
            self.assertEqual(results["version"]["rc"], 0, results["version"])
            self.assertIn(a, results["version"]["out"])
        # A's generated per-run settings survived B's activation and A's delayed work.
        self.assertTrue(orchestrator["configs"])
        self.assertEqual(done["configs_after"], orchestrator["configs"])
        for text in orchestrator["configs"].values():
            self.assertNotIn(MARKER.format("B"), text)
        # B's generation used its own run directory and left A's files byte-identical.
        run_b = Path(generated_b["dir"])
        self.assertEqual(run_b.parent, run_a.parent)
        self.assertNotEqual(run_b, run_a)
        self.assertEqual({str(p): p.read_bytes() for p in run_a.rglob("*") if p.is_file()}, before_a)
        self.assertTrue((run_a / "owner.json").is_file())
        b_toml = (run_b / "codex" / "backend-engineer.toml").read_text()
        self.assertIn(MARKER.format("B"), b_toml)
        self.assertIn(MARKER.format("A"), (run_a / "codex" / "backend-engineer.toml").read_text())
        b_settings = json.loads((run_b / "claude-settings.json").read_text())
        self.assertIn(generated_b["release"], json.dumps(b_settings))
        self.assertEqual(json.loads((run_b / "owner.json").read_text())["release"]["commit"], b)
        # New invocations through the shim run B.
        self.assertEqual(self.shim_run("--help").returncode, 0)
        self.assertEqual(list(release_a.rglob("__pycache__")), [])

    def generate_with(self, release: Path, project: Path) -> dict:
        """Build Codex and Claude settings for project with release's own code, in a new process."""
        code = ("import importlib.machinery, importlib.util, json, sys\n"
                "from pathlib import Path\n"
                "loader = importlib.machinery.SourceFileLoader('syndicate', sys.argv[1])\n"
                "spec = importlib.util.spec_from_loader('syndicate', loader)\n"
                "sy = importlib.util.module_from_spec(spec); loader.exec_module(sy)\n"
                "project = Path(sys.argv[2])\n"
                "team = sy.resolve_team(project)\n"
                "sy.build_codex(team, True, ['b task'])\n"
                "sy.build_claude(team, True, ['b task'])\n"
                "print(json.dumps({'dir': str(sy.generated(project)), 'release': str(sy.identity.ROOT)}))\n")
        result = subprocess.run([os.path.abspath(sys.executable), "-B", "-c", code, str(release / "bin" / "syndicate"),
                                 str(project)], cwd=self.tmp / "elsewhere", env=self.env, capture_output=True,
                                text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["release"], str(release))
        return data


class CodexHookMigration(InstallCase):
    def setUp(self):
        super().setUp()
        self.install("v1.0.0")
        self.codex = self.tmp / "codex"
        self.hooks = self.codex / "hooks.json"
        self.checkout_hook = FIXTURE["src"] / "hooks" / "require-review.sh"
        self.release_hook = Path(os.path.realpath(self.root / "releases" / FIXTURE["a"])) / "hooks" / "require-review.sh"

    def write_hooks(self, stop, **other):
        data = {"hooks": {"Stop": stop, **other}, "unrelated": {"keep": True}}
        self.hooks.write_text(json.dumps(data, indent=2) + "\n")
        self.hooks.chmod(0o600)
        return data

    def migrate(self, *extra, ok=True):
        return self.installer("migrate-codex-hook", "--codex-home", str(self.codex), "--json", *extra, ok=ok)

    def test_migrates_only_the_matching_hook_and_preserves_siblings(self):
        old = rel.shlex.quote(str(self.checkout_hook))
        new = rel.shlex.quote(str(self.release_hook))
        data = self.write_hooks(
            [{"hooks": [{"type": "command", "command": "/usr/local/bin/other-stop"}]},
             {"matcher": "*", "hooks": [{"type": "command", "command": "echo sibling"},
                                         {"type": "command", "command": old, "timeout": 30}]}],
            SessionStart=[{"hooks": [{"type": "command", "command": "echo start"}]}])
        original = self.hooks.read_bytes()
        preview = json.loads(self.migrate("--dry-run").stdout)
        self.assertEqual(preview["changes"][0]["to"], new)
        self.assertEqual(self.hooks.read_bytes(), original)
        self.assertEqual(list(self.codex.glob("hooks.json.syndicate-backup-*")), [])
        result = json.loads(self.migrate().stdout)
        self.assertIn("checkout", result["changes"][0]["provenance"])
        self.assertEqual(Path(result["backup"]).read_bytes(), original)
        self.assertEqual(stat.S_IMODE(os.stat(self.hooks).st_mode), 0o600)
        migrated = json.loads(self.hooks.read_text())
        expected = json.loads(json.dumps(data))
        expected["hooks"]["Stop"][1]["hooks"][1]["command"] = new
        self.assertEqual(migrated, expected)
        again = json.loads(self.migrate().stdout)
        self.assertEqual((again["changed"], again["reason"]), (False, "already registered"))
        self.assertEqual(len(list(self.codex.glob("hooks.json.syndicate-backup-*"))), 1)

    def test_duplicates_collapse_to_one_release_hook(self):
        old = rel.shlex.quote(str(self.checkout_hook))
        new = rel.shlex.quote(str(self.release_hook))
        self.write_hooks([{"hooks": [{"type": "command", "command": new}]},
                          {"hooks": [{"type": "command", "command": old}]},
                          {"hooks": [{"type": "command", "command": "echo keep"}, {"type": "command", "command": new}]}])
        self.migrate()
        stop = json.loads(self.hooks.read_text())["hooks"]["Stop"]
        self.assertEqual(stop, [{"hooks": [{"type": "command", "command": new}]},
                                {"hooks": [{"type": "command", "command": "echo keep"}]}])

    def test_upgrade_moves_hook_between_releases(self):
        self.write_hooks([{"hooks": [{"type": "command", "command": rel.shlex.quote(str(self.release_hook))}]}])
        self.install("v1.1.0")
        result = json.loads(self.migrate().stdout)
        self.assertIn(f"release {FIXTURE['a']} manifest", result["changes"][0]["provenance"])
        (entry,) = json.loads(self.hooks.read_text())["hooks"]["Stop"]
        self.assertIn(FIXTURE["b"], entry["hooks"][0]["command"])
        # A release still registered as the hook is kept by prune.
        self.write_hooks([{"hooks": [{"type": "command", "command": rel.shlex.quote(str(self.release_hook))}]}])
        self.install("v1.2.0")
        kept = json.loads(self.installer("prune", "--codex-home", str(self.codex), "--json").stdout)["kept"]
        self.assertEqual(kept.get(FIXTURE["a"]), "registered Codex hook")

    def test_unverifiable_hooks_are_left_untouched(self):
        fake_checkout = self.tmp / "copy" / "hooks"
        fake_checkout.mkdir(parents=True)
        (fake_checkout / "require-review.sh").write_text("#!/bin/sh\nexit 0\n")
        modified = self.tmp / "repo"
        shutil.copytree(FIXTURE["src"], modified)
        (modified / "hooks" / "require-review.sh").write_text("#!/bin/sh\necho changed\n")
        old = rel.shlex.quote(str(self.checkout_hook))
        new = rel.shlex.quote(str(self.release_hook))
        for path, message in ((fake_checkout / "require-review.sh", "neither in a Git checkout"),
                              (modified / "hooks" / "require-review.sh", "differs from"),
                              (self.tmp / "gone" / "hooks" / "require-review.sh", "cannot be read")):
            with self.subTest(path=path):
                other = rel.shlex.quote(str(path))
                # Alone: nothing to migrate, the file is unchanged and no backup is made.
                self.write_hooks([{"hooks": [{"type": "command", "command": other}]}])
                original = self.hooks.read_bytes()
                result = self.migrate()
                self.assertIn(message, result.stderr)
                data = json.loads(result.stdout)
                self.assertFalse(data["changed"])
                self.assertEqual([s["command"] for s in data["skipped"]], [other])
                self.assertEqual(self.hooks.read_bytes(), original)
                self.assertEqual(list(self.codex.glob("hooks.json.syndicate-backup-*")), [])
                # Beside a verified Syndicate hook: that one migrates, the other is preserved.
                self.write_hooks([{"hooks": [{"type": "command", "command": other, "timeout": 5}]},
                                  {"hooks": [{"type": "command", "command": old}]}])
                data = json.loads(self.migrate().stdout)
                self.assertTrue(data["changed"])
                self.assertEqual([c.get("from") for c in data["changes"]], [old])
                stop = json.loads(self.hooks.read_text())["hooks"]["Stop"]
                self.assertEqual(stop, [{"hooks": [{"type": "command", "command": other, "timeout": 5}]},
                                        {"hooks": [{"type": "command", "command": new}]}])
                for backup in self.codex.glob("hooks.json.syndicate-backup-*"):
                    backup.unlink()

    def test_a_changed_release_hook_stops_the_migration(self):
        self.install("v1.1.0")
        hook = self.release_hook
        os.chmod(hook.parent, 0o755)
        os.chmod(hook, 0o755)
        hook.write_text("#!/bin/sh\nexit 0\n")
        self.write_hooks([{"hooks": [{"type": "command", "command": rel.shlex.quote(str(hook))}]},
                          {"hooks": [{"type": "command", "command": rel.shlex.quote(str(self.checkout_hook))}]}])
        original = self.hooks.read_bytes()
        result = self.migrate(ok=False)
        self.assertIn("does not match its release manifest", result.stderr)
        self.assertEqual(self.hooks.read_bytes(), original)
        self.assertEqual(list(self.codex.glob("hooks.json.syndicate-backup-*")), [])

    def test_never_adds_a_hook(self):
        self.assertIn("does not exist", self.migrate().stdout)
        self.assertFalse(self.hooks.exists())
        self.write_hooks([{"hooks": [{"type": "command", "command": "echo other"}]}])
        original = self.hooks.read_bytes()
        result = json.loads(self.migrate().stdout)
        self.assertFalse(result["changed"])
        self.assertEqual(self.hooks.read_bytes(), original)


class LivenessAndPrune(InstallCase):
    def test_symlinked_install_root_matches_markers_processes_hooks_and_prune(self):
        a, b, c = FIXTURE["a"], FIXTURE["b"], FIXTURE["c"]
        real = self.tmp / "real data"
        real.mkdir()
        link = self.tmp / "data link"
        link.symlink_to(real)
        self.env["XDG_DATA_HOME"] = str(link)
        for tag in ("v1.0.0", "v1.1.0", "v1.2.0"):
            self.install(tag)
        paths = rel.Paths.from_env(environ=self.env)
        self.assertEqual(paths.root, real / "syndicate")
        linked_release = link / "syndicate" / "releases" / a
        # The shim embeds the canonical root.
        self.assertIn(f"root={rel.shlex.quote(str(real / 'syndicate'))}", self.shim.read_text())
        # A hook registered through the symlinked spelling is still release A's.
        hook = linked_release / "hooks" / "require-review.sh"
        self.assertEqual(rel.release_of(paths, rel.shlex.quote(str(hook))), a)
        self.assertEqual(rel.hook_provenance(paths, hook), f"release {a} manifest")
        codex = self.tmp / "codex"
        (codex / "hooks.json").write_text(json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": rel.shlex.quote(str(hook))}]}]}}))
        result = json.loads(self.installer("migrate-codex-hook", "--codex-home", str(codex), "--json").stdout)
        self.assertEqual(result["changes"][0]["provenance"], f"release {a} manifest")
        self.assertIn(c, json.loads((codex / "hooks.json").read_text())["hooks"]["Stop"][0]["hooks"][0]["command"])
        # A process naming A through the symlinked root, inside a longer argument.
        holder = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(120)",
                                   f"--launcher={linked_release}/bin/syndicate"])
        self.addCleanup(lambda: (holder.kill(), holder.wait()))
        self.assertIn(a, rel.releases_in_use(paths))
        self.assertIn(a, json.loads(self.installer("prune", "--dry-run", "--json").stdout)["kept"])
        holder.kill()
        holder.wait()
        # A marker under the canonical root (where the launcher writes it) keeps A.
        marker = real / "syndicate" / "in-use" / a / str(os.getpid())
        marker.parent.mkdir(parents=True)
        marker.touch()
        self.assertEqual(rel.releases_in_use(paths).get(a), f"in-use marker pid {os.getpid()}")
        marker.unlink()
        result = json.loads(self.installer("prune", "--json").stdout)
        self.assertEqual(result["removed"], [a])
        self.assertEqual(sorted(p.name for p in (real / "syndicate" / "releases").iterdir()), sorted([b, c]))

    def test_without_proc_or_markers_prune_needs_force(self):
        a = FIXTURE["a"]
        for tag in ("v1.0.0", "v1.1.0", "v1.2.0"):
            self.install(tag)
        quiet = mock.patch.object(rel, "log")
        quiet.start()
        self.addCleanup(quiet.stop)
        with mock.patch.object(rel, "PROC", self.tmp / "no proc"):
            preview = rel.prune(self.paths, dry_run=True)
            self.assertTrue(preview["liveness_unverified"])
            self.assertEqual(preview["removed"], [a])
            with self.assertRaisesRegex(rel.ReleaseError, "without --force"):
                rel.prune(self.paths)
            self.assertIn(a, self.release_entries())
            # A live marker proves only its own release is in use: still refused.
            marker = self.root / "in-use" / a / str(os.getpid())
            marker.parent.mkdir(parents=True)
            marker.touch()
            with self.assertRaisesRegex(rel.ReleaseError, "without --force"):
                rel.prune(self.paths)
            # With --force, a release with a live marker is still kept.
            result = rel.prune(self.paths, force=True)
            self.assertEqual((result["removed"], result["liveness_unverified"]), ([], True))
            self.assertEqual(result["kept"][a], f"in-use marker pid {os.getpid()}")
            marker.unlink()
            marker.parent.rmdir()
            self.assertEqual(rel.prune(self.paths, force=True)["removed"], [a])

    def test_prune_reports_foreign_entries_and_finishes_interrupted_removals(self):
        a, b, c = FIXTURE["a"], FIXTURE["b"], FIXTURE["c"]
        for tag in ("v1.0.0", "v1.1.0", "v1.2.0"):
            self.install(tag)
        (self.paths.releases / "notes.txt").write_text("not a release")
        (self.paths.releases / "scratch").mkdir()
        # An earlier prune was killed after renaming a release to its trash name.
        trash = self.paths.releases / f".trash-999999-{a}"
        shutil.copytree(self.paths.releases / a, trash)
        rel.set_read_only(trash)
        preview = json.loads(self.installer("prune", "--dry-run", "--json").stdout)
        self.assertEqual(preview["ignored"], ["notes.txt", "scratch"])
        self.assertEqual(preview["stale_trash"], [trash.name])
        self.assertTrue(trash.exists())
        real_rename = os.rename
        renamed = []

        def watch_rename(src, dst):
            renamed.append((Path(src).name, Path(dst).name))
            return real_rename(src, dst)

        quiet = mock.patch.object(rel, "log")
        quiet.start()
        self.addCleanup(quiet.stop)
        with mock.patch.object(rel.os, "rename", side_effect=watch_rename):
            result = rel.prune(self.paths)
        self.assertEqual(result["removed"], [a])
        self.assertEqual(renamed, [(a, f".trash-{os.getpid()}-{a}")])
        self.assertEqual(sorted(p.name for p in self.paths.releases.iterdir()), sorted([b, c, "notes.txt", "scratch"]))

    def test_rollback_skips_damaged_candidates_and_refuses_a_rewritten_manifest(self):
        a, b, c = FIXTURE["a"], FIXTURE["b"], FIXTURE["c"]
        for tag in ("v1.0.0", "v1.1.0", "v1.2.0"):
            self.install(tag)
        # B (the newest candidate) is damaged: rollback skips it and selects A.
        role = self.paths.releases / b / "roles" / "orchestrator.md"
        os.chmod(role.parent, 0o755)
        os.chmod(role, 0o644)
        role.write_text("damaged\n")
        result = self.installer("rollback")
        self.assertIn(f"skipping rollback candidate {b}", result.stderr)
        self.assertEqual(self.current(), a)
        # C is rewritten consistently (file and manifest digest both changed): it
        # verifies against itself but not against the activation log.
        release = self.paths.releases / c
        os.chmod(release, 0o755)
        os.chmod(release / "roles", 0o755)
        target = release / "roles" / "orchestrator.md"
        os.chmod(target, 0o644)
        target.write_text("rewritten\n")
        manifest = json.loads((release / "RELEASE.json").read_text())
        manifest["files"]["roles/orchestrator.md"] = hashlib.sha256(b"rewritten\n").hexdigest()
        os.chmod(release / "RELEASE.json", 0o644)
        (release / "RELEASE.json").write_text(json.dumps(manifest, indent=2) + "\n")
        self.assertEqual(rel.verify_release(release)["commit"], c)
        result = self.installer("rollback", "--to", c, ok=False)
        self.assertIn("differs from the manifest recorded", result.stderr)
        result = self.installer("migrate-codex-hook", "--codex-home", str(self.tmp / "codex"), "--target", c, ok=False)
        self.assertIn("differs from the manifest recorded", result.stderr)
        self.assertEqual(self.current(), a)


class SourceTrust(InstallCase):
    def upstream_copy(self, name="upstream copy.git"):
        copy = self.tmp / name
        shutil.copytree(FIXTURE["upstream"], copy)
        return copy

    def test_moved_tag_needs_allow_retag(self):
        upstream = self.upstream_copy()
        self.installer("install", "--tag", "v1.0.0", "--upstream", str(upstream))
        run_git(upstream, "tag", "-f", "v1.0.0", FIXTURE["b"])
        result = self.installer("install", "--tag", "v1.0.0", ok=False)
        self.assertIn("--allow-retag", result.stderr)
        self.assertEqual(self.current(), FIXTURE["a"])
        self.installer("install", "--tag", "v1.0.0", "--allow-retag")
        self.assertEqual(self.current(), FIXTURE["b"])
        self.assertEqual(self.records()[-1]["retagged_from"], [FIXTURE["a"]])

    def test_changing_or_untrusted_upstreams_are_refused(self):
        self.install("v1.0.0")
        other = self.upstream_copy()
        result = self.installer("install", "--tag", "v1.1.0", "--upstream", str(other), ok=False)
        self.assertIn("--change-upstream", result.stderr)
        self.installer("install", "--tag", "v1.1.0", "--upstream", str(other), "--change-upstream")
        self.assertEqual(self.records()[-1]["upstream_changed_from"], str(FIXTURE["upstream"]))
        self.assertEqual(self.records()[-1]["upstream"], str(other))
        # A working checkout, or a bare repository with linked worktrees, can have
        # its main and tags moved by agents working in it.
        result = self.installer("install", "--tag", "v1.2.0", "--upstream", str(FIXTURE["src"]),
                                "--change-upstream", ok=False)
        self.assertIn("not a bare repository", result.stderr)
        run_git(other, "worktree", "add", "-q", str(self.tmp / "linked"), "main")
        result = self.installer("install", "--tag", "v1.2.0", ok=False)
        self.assertIn("linked worktrees", result.stderr)
        self.assertEqual(self.current(), FIXTURE["b"])

    def test_tree_entries_refuse_unsafe_or_non_utf8_paths(self):
        def listing(name: bytes):
            blob = "0" * 40
            out = b"100644 blob " + blob.encode() + b"\tbin/syndicate\0100644 blob " + blob.encode() + b"\t" + name + b"\0"
            return subprocess.CompletedProcess([], 0, stdout=out, stderr=b"")

        for name, message in ((b"roles/../x.md", "unsafe path"), (b"roles/./x.md", "unsafe path"),
                              (b"roles//x.md", "unsafe path"), (b"docs/.git/config", "unsafe path"),
                              (b"docs/.GIT/config", "unsafe path"), (b"roles/\xff.md", "not a UTF-8 path")):
            with self.subTest(name=name), mock.patch.object(rel, "git", return_value=listing(name)):
                with self.assertRaisesRegex(rel.ReleaseError, message):
                    rel.tree_entries(self.paths.cache, FIXTURE["a"])


class SourceCacheIntegrity(InstallCase):
    """The release-source cache cannot be made to archive a tree other than the approved commit's."""

    def setUp(self):
        super().setUp()
        self.install("v1.0.0")
        self.cache = self.paths.cache
        self.before = (self.current(), self.release_entries())

    def refused(self, message, tag="v1.1.0"):
        result = self.install(tag, ok=False)
        self.assertIn(message, result.stderr)
        self.assert_unchanged(*self.before)
        return result

    def side_objects(self):
        """Put the off-main side commit's objects into the cache, as an attacker with cache access could."""
        run_git(self.cache, "fetch", "-q", str(FIXTURE["upstream"]), "+refs/heads/feature:refs/attacker/feature")
        return run_git(self.cache, "rev-parse", "refs/attacker/feature")

    def test_replace_ref_is_refused_and_never_followed(self):
        b, side = FIXTURE["b"], self.side_objects()
        run_git(self.cache, "replace", b, side)
        # Plain Git now reads the side commit's tree for B; the installer's Git does not.
        self.assertIn("feature only", run_git(self.cache, "show", f"{b}:README.md"))
        entries = rel.tree_entries(self.cache, b)
        self.assertEqual(entries["README.md"][1],
                         run_git(FIXTURE["upstream"], "rev-parse", f"{b}:README.md"))
        archive = rel.build_archive(self.cache, b, entries)
        stage = self.tmp / "stage"
        stage.mkdir()
        rel.extract(archive, entries, stage)
        self.assertNotIn("feature only", (stage / "README.md").read_text())
        self.refused("replace refs")
        run_git(self.cache, "replace", "-d", b)
        self.install("v1.1.0")
        self.assertNotIn("feature only", (self.paths.releases / b / "README.md").read_text())

    def test_grafts_shallow_and_alternates_are_refused(self):
        b, side = FIXTURE["b"], self.side_objects()
        for name, content in (("info/grafts", f"{b} {side}\n"), ("shallow", f"{b}\n"),
                              ("objects/info/alternates", str(FIXTURE["upstream"] / "objects") + "\n")):
            with self.subTest(name=name):
                path = self.cache / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
                self.refused(name)
                path.unlink()
        self.install("v1.1.0")

    def test_unexpected_cache_configuration_is_refused(self):
        pwned = self.tmp / "PWNED"
        for key, value in (("remote.upstream.uploadpack", f"touch {pwned}; git-upload-pack"),
                           ("remote.upstream.receivepack", "true"),
                           ("remote.upstream.proxy", "http://127.0.0.1:9"),
                           ("remote.upstream.vcs", "evil"),
                           ("remote.upstream.fetch", "+refs/heads/*:refs/replace/*"),
                           (f"url.{self.tmp}/elsewhere.insteadof", str(FIXTURE["upstream"])),
                           ("core.sshcommand", f"touch {pwned}"),
                           ("include.path", str(self.tmp / "extra")),
                           ("core.bare", "false")):
            with self.subTest(key=key):
                original = (self.cache / "config").read_bytes()
                run_git(self.cache, "config", "--add", key, value)
                self.refused(f"unexpected configuration {key}")
                (self.cache / "config").write_bytes(original)
        self.assertFalse(pwned.exists())
        self.install("v1.1.0")

    def test_cache_permissions_are_private_and_only_tightened(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.cache).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.cache.parent).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.cache.with_name("source.git.lock")).st_mode), 0o600)
        self.cache.chmod(0o777)
        self.cache.parent.chmod(0o777)
        self.install("v1.1.0")
        self.assertEqual(stat.S_IMODE(os.stat(self.cache).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.cache.parent).st_mode), 0o700)
        # Another account's cache directory is refused (simulated: ownership cannot be changed here).
        with mock.patch.object(rel, "writable_by_others",
                               side_effect=lambda p: "owned by uid 4242" if Path(p) == self.cache else None):
            with self.assertRaisesRegex(rel.ReleaseError, "owned by uid 4242"):
                rel.check_cache(self.paths)


class UmaskAndModes(InstallCase):
    def test_state_gets_explicit_modes_under_a_permissive_umask(self):
        def loose():
            os.umask(0o002)

        self.installer("install", "--tag", "v1.0.0", "--upstream", str(FIXTURE["upstream"]), preexec_fn=loose)
        paths = (self.root, self.paths.releases, self.paths.interpreters, self.paths.lock, self.paths.cache,
                 self.paths.cache.parent, self.paths.cache.with_name("source.git.lock"),
                 self.paths.interpreters / FIXTURE["a"], self.paths.log)
        modes = {str(path): oct(stat.S_IMODE(os.stat(path).st_mode)) for path in paths}
        self.assertEqual(list(modes.values()),
                         ["0o755", "0o755", "0o755", "0o600", "0o700", "0o700", "0o600", "0o644", "0o600"], modes)
        # The launcher's in-use directories and markers, written under the same umask.
        release = Path(os.path.realpath(self.paths.releases / FIXTURE["a"]))
        code = ("import importlib.machinery, importlib.util, json, os, sys\n"
                "os.umask(0o002)\n"
                "loader = importlib.machinery.SourceFileLoader('syndicate', sys.argv[1])\n"
                "spec = importlib.util.spec_from_loader('syndicate', loader)\n"
                "sy = importlib.util.module_from_spec(spec); loader.exec_module(sy)\n"
                "sy.mark_release_in_use()\n"
                "marker = sy._release_markers[0]\n"
                "print(json.dumps([oct(os.stat(p).st_mode & 0o777)"
                " for p in (marker.parent.parent, marker.parent, marker)]))\n")
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(release / "bin" / "syndicate")],
                                cwd=self.tmp / "elsewhere", env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["0o700", "0o700", "0o600"])

    def test_existing_private_root_is_never_loosened(self):
        self.root.mkdir(parents=True)
        self.root.chmod(0o700)
        self.install("v1.0.0")
        self.assertEqual(stat.S_IMODE(os.stat(self.root).st_mode), 0o700)
        # A group- or world-writable directory of this user's is tightened, not refused.
        self.paths.interpreters.chmod(0o777)
        self.install("v1.1.0")
        self.assertEqual(stat.S_IMODE(os.stat(self.paths.interpreters).st_mode), 0o755)


class InterpreterTrust(InstallCase):
    def test_virtualenv_default_is_refused_but_explicit_choice_is_allowed(self):
        venv = self.tmp / "venv"
        subprocess.run([sys.executable, "-B", "-m", "venv", "--without-pip", str(venv)], check=True,
                       env=self.env, capture_output=True, timeout=120)
        python = venv / "bin" / "python"
        result = subprocess.run([str(python), "-B", str(INSTALLER), "install", "--tag", "v1.0.0", "--upstream",
                                 str(FIXTURE["upstream"])], cwd=self.tmp / "elsewhere", env=self.env,
                                capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("virtual environment", result.stderr)
        self.assertIsNone(self.current())
        with self.assertRaisesRegex(rel.ReleaseError, "virtual environment"):
            rel.preflight_interpreter(str(python), explicit=False)
        self.assertEqual(rel.preflight_interpreter(str(python), explicit=True), str(python))

    def test_symlinks_on_the_interpreter_path_are_checked_but_recorded_unresolved(self):
        python = os.path.abspath(sys.executable)
        open_dir = self.tmp / "open dir"
        open_dir.mkdir()
        open_dir.chmod(0o777)
        self.addCleanup(open_dir.chmod, 0o755)
        # A symlink in a directory anyone can write could be replaced after installation.
        link = open_dir / "python3"
        link.symlink_to(python)
        with self.assertRaisesRegex(rel.ReleaseError, "world-writable"):
            rel.preflight_interpreter(str(link))
        # Also when reached through a trusted-looking link elsewhere.
        safe = self.tmp / "safe bin"
        safe.mkdir()
        (safe / "python3").symlink_to(link)
        with self.assertRaisesRegex(rel.ReleaseError, "open dir.*world-writable"):
            rel.preflight_interpreter(str(safe / "python3"))
        result = self.install("v1.0.0", "--python", str(safe / "python3"), ok=False)
        self.assertIn("world-writable", result.stderr)
        # A sticky world-writable directory (like /tmp) is fine for an entry this user owns.
        open_dir.chmod(0o1777)
        self.assertEqual(rel.preflight_interpreter(str(safe / "python3")), str(safe / "python3"))
        self.install("v1.0.0", "--python", str(safe / "python3"))
        self.assertEqual((self.paths.interpreters / FIXTURE["a"]).read_text(), f"{safe / 'python3'}\n")

    def test_writable_or_worktree_interpreters_are_refused(self):
        shared = self.tmp / "shared bin"
        shared.mkdir()
        wrapper = shared / "python3"
        wrapper.write_text(f"#!/bin/sh\nexec '{os.path.abspath(sys.executable)}' \"$@\"\n")
        wrapper.chmod(0o755)
        self.assertEqual(rel.preflight_interpreter(str(wrapper)), str(wrapper))
        wrapper.chmod(0o757)
        with self.assertRaisesRegex(rel.ReleaseError, "world-writable"):
            rel.preflight_interpreter(str(wrapper))
        wrapper.chmod(0o755)
        shared.chmod(0o777)
        self.addCleanup(shared.chmod, 0o755)
        with self.assertRaisesRegex(rel.ReleaseError, "world-writable"):
            rel.preflight_interpreter(str(wrapper))
        shared.chmod(0o755)
        project = self.tmp / "project"
        (project / ".venv" / "bin").mkdir(parents=True)
        (project / ".git").mkdir()
        (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        inside = project / ".venv" / "bin" / "python3"
        inside.symlink_to(wrapper)
        with self.assertRaisesRegex(rel.ReleaseError, "Git work tree"):
            rel.preflight_interpreter(str(inside))
        result = self.install("v1.0.0", "--python", str(inside), ok=False)
        self.assertIn("Git work tree", result.stderr)
        # An empty .git placeholder (Codex's sandbox leaves them on the host) is not a work tree.
        (self.tmp / ".git").mkdir()
        self.assertEqual(rel.preflight_interpreter(str(wrapper)), str(wrapper))
        (self.tmp / ".git").rmdir()


class EnvironmentIsolation(InstallCase):
    def test_project_modules_on_pythonpath_never_run_inside_syndicate(self):
        self.install("v1.0.0")
        release = Path(os.path.realpath(self.root / "releases" / FIXTURE["a"]))
        project = self.tmp / "hostile project"
        project.mkdir()
        hit = self.tmp / "imported"
        for module in ("shlex", "json", "syndicate_identity", "sitecustomize", "usercustomize"):
            (project / f"{module}.py").write_text(
                f"import pathlib\npathlib.Path({str(hit)!r}).write_text({module!r})\nraise SystemExit(99)\n")
        env = dict(self.env, PYTHONPATH=str(project) + os.pathsep + ".", PYTHONSTARTUP=str(project / "shlex.py"),
                   PYTHONUSERBASE=str(project))
        for argv in (["--version"], ["chief-of-staff", "--dry-run", "-x", "codex"]):
            result = self.shim_run(*argv, cwd=project, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
        # The pinned prefix a session hands its agents is isolated too.
        launch = json.loads(self.shim_run("chief-of-staff", "--dry-run", "-x", "codex", cwd=project, env=env).stdout)
        instructions = json.loads(next(a for a in launch["argv"] if a.startswith("developer_instructions="))
                                  .split("=", 1)[1])
        prefix = rel.shlex.split(instructions.split("Syndicate executable prefix: ", 1)[1].split("\n", 1)[0])
        self.assertEqual(prefix[1:], ["-I", "-B", str(release / "bin" / "syndicate")])
        result = subprocess.run([*prefix, "--version"], cwd=project, env=env, capture_output=True, text=True,
                                timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        # The channel MCP server finds its sibling modules under -I.
        endpoint = self.tmp / "live.json"
        endpoint.write_text(json.dumps({"active": False}))
        channel = subprocess.run([*prefix[:3], str(release / "bin" / "syndicate_live.py"), "channel", str(endpoint)],
                                 input="", cwd=project, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(channel.returncode, 0, channel.stderr)
        self.assertFalse(hit.exists(), hit.read_text() if hit.exists() else "")
        self.assertIn('exec "$interpreter" -I -B "$release/bin/syndicate" "$@"', self.shim.read_text())


class InstallerEntry(InstallCase):
    def test_installer_runs_isolated_however_it_is_started(self):
        hostile = self.tmp / "hostile"
        hostile.mkdir()
        hit = self.tmp / "imported"
        (hostile / "json.py").write_text(f"import pathlib\npathlib.Path({str(hit)!r}).write_text('json')\n"
                                         "raise SystemExit(99)\n")
        env = dict(self.env, PYTHONPATH=str(hostile))
        for argv in ([str(INSTALLER)], [sys.executable, str(INSTALLER)], [sys.executable, "-B", str(INSTALLER)]):
            with self.subTest(argv=argv):
                result = subprocess.run([*argv, "status", "--json"], cwd=hostile, env=env, capture_output=True,
                                        text=True, timeout=120)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["root"], str(self.root))
                self.assertFalse(hit.exists())
        first = INSTALLER.read_text().splitlines()[:2]
        self.assertEqual(first, ["#!/bin/sh", "''''exec python3 -I -B -- \"$0\" \"$@\" # '''"])

    def test_whitespace_in_rule_paths_is_warned_about(self):
        result = self.install("v1.0.0")
        self.assertIn("contains whitespace", result.stderr)
        self.assertIn("each relay asks for approval", result.stderr)


class ReleaseInputs(InstallCase):
    def test_corrupt_archive_is_a_release_error(self):
        stage = self.tmp / "stage"
        stage.mkdir()
        for archive in (b"not a tar file" * 100, b"\xff" * 1024):
            with self.subTest(archive=archive[:8]):
                with self.assertRaises(rel.ReleaseError):
                    rel.extract(archive, {"bin/syndicate": ("100755", "0" * 40)}, stage)

    def test_reuse_requires_the_same_manifest_and_refuses_a_symlink(self):
        a = FIXTURE["a"]
        self.install("v1.0.0")
        release = self.paths.releases / a
        manifest = release / "RELEASE.json"
        os.chmod(release, 0o755)
        os.chmod(manifest, 0o644)
        original = manifest.read_bytes()
        # Same files, different RELEASE.json bytes: not the release this installer would build.
        manifest.write_text(json.dumps(json.loads(original)) + "\n")
        result = self.install("v1.0.1", ok=False)
        self.assertIn("RELEASE.json sha256", result.stderr)
        manifest.write_bytes(original)
        self.install("v1.0.1")
        self.assertEqual(self.records()[-1]["tag"], "v1.0.1")
        # A symlink where the release directory belongs is refused, never followed.
        elsewhere = self.tmp / "elsewhere release"
        shutil.copytree(release, elsewhere, symlinks=True)
        rel.remove_tree(release)
        release.symlink_to(elsewhere)
        result = self.install("v1.0.0", ok=False)
        self.assertIn("is a symlink", result.stderr)
        with self.assertRaisesRegex(rel.ReleaseError, "is a symlink"):
            rel.verify_release(release, a)

    def test_marker_and_staging_names_must_be_ascii_digits(self):
        for tag in ("v1.0.0", "v1.1.0", "v1.2.0"):
            self.install(tag)
        odd = self.root / "in-use" / FIXTURE["a"] / "²"  # str.isdigit() is True; int() fails
        odd.parent.mkdir(parents=True)
        odd.touch()
        staging = self.paths.releases / ".staging-²-x"
        staging.mkdir()
        used, _, _ = rel.scan_in_use(self.paths)
        self.assertNotIn(FIXTURE["a"], used)
        with mock.patch.object(rel, "log"):
            result = rel.prune(self.paths)
        self.assertEqual(result["removed"], [FIXTURE["a"]])
        self.assertEqual(result["stale_staging"], [staging.name])

    def test_smoke_environment_is_an_allowlist(self):
        base = self.tmp / "smoke"
        base.mkdir()
        with mock.patch.dict(os.environ, {"LANG": "C.UTF-8", "UNRELATED_TOKEN": "secret", "SSH_AUTH_SOCK": "/s",
                                          "SYNDICATE_X": "1", "PYTHONPATH": "/p"}):
            env = rel.disposable_env(base)
        self.assertEqual(env["LANG"], "C.UTF-8")
        self.assertEqual(env["PATH"], os.environ["PATH"])
        for key in ("UNRELATED_TOKEN", "SSH_AUTH_SOCK", "SYNDICATE_X", "PYTHONPATH"):
            self.assertNotIn(key, env)
        self.assertEqual(env["HOME"], str(base / "home"))


class Durability(InstallCase):
    def test_permissions_partial_failures_and_shim_ownership(self):
        self.install("v1.0.0")
        self.assertEqual(stat.S_IMODE(os.stat(self.root).st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(self.root / "releases").st_mode), 0o755)
        # A shim directory anyone can write is refused before anything changes.
        self.shim.parent.chmod(0o777)
        result = self.install("v1.1.0", ok=False)
        self.assertIn("world-writable", result.stderr)
        self.assertEqual(self.current(), FIXTURE["a"])
        self.shim.parent.chmod(0o755)
        # The entry point cannot be written: activation is reported with what failed.
        self.shim.unlink()
        self.shim.mkdir()
        result = self.install("v1.1.0", ok=False)
        self.assertIn(f"activated {FIXTURE['b']}; shim not installed", result.stderr)
        self.assertIn("completed before the failure", result.stderr)
        self.assertIn("switched current", result.stderr)
        self.assertEqual(self.current(), FIXTURE["b"])
        self.shim.rmdir()
        self.installer("install-shim")
        # Another install root's managed shim is replaced only with --force, and recorded.
        other_root = self.tmp / "other store" / "syndicate"
        result = self.installer("install-shim", "--install-root", str(other_root), ok=False)
        self.assertIn("--force", result.stderr)
        self.assertIn(f"root={rel.shlex.quote(str(self.paths.root))}", self.shim.read_text())
        self.installer("install-shim", "--install-root", str(other_root), "--force")
        self.assertIn(f"root={rel.shlex.quote(str(other_root))}", self.shim.read_text())
        (record,) = rel.read_jsonl(other_root / "entrypoints.jsonl")
        self.assertEqual((record["kind"], record["previous_root"], record["forced"]),
                         ("file", str(self.paths.root), True))
        self.assertEqual(stat.S_IMODE(os.stat(other_root / "entrypoint-backups").st_mode), 0o700)
        # restore-entrypoint from the other root's log puts the first shim back.
        self.installer("restore-entrypoint", "--install-root", str(other_root))
        self.assertIn(f"root={rel.shlex.quote(str(self.paths.root))}", self.shim.read_text())

    def test_restore_entrypoint_reports_damaged_records_and_backups(self):
        self.shim.parent.mkdir(parents=True)
        self.shim.write_text("#!/bin/sh\necho custom\n")
        self.install("v1.0.0")
        (record,) = rel.read_jsonl(self.paths.entrypoints)
        Path(record["backup"]).unlink()
        result = self.installer("restore-entrypoint", ok=False)
        self.assertIn("cannot be read", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        with open(self.paths.entrypoints, "ab") as handle:
            handle.write(b'{"path": "\xff"}\n')
        result = self.installer("restore-entrypoint", ok=False)
        self.assertIn("line 2 is not a JSON object", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertTrue(rel.is_managed_shim(self.shim))


class HookFileSafety(InstallCase):
    def setUp(self):
        super().setUp()
        self.install("v1.0.0")
        self.codex = self.tmp / "codex"
        self.checkout_hook = rel.shlex.quote(str(FIXTURE["src"] / "hooks" / "require-review.sh"))

    def test_symlinked_hooks_json_stays_a_symlink_and_backup_is_private(self):
        dotfiles = self.tmp / "dotfiles"
        dotfiles.mkdir()
        real = dotfiles / "codex-hooks.json"
        real.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command",
                                                                  "command": self.checkout_hook}]}]}}))
        real.chmod(0o644)
        (self.codex / "hooks.json").symlink_to(real)
        result = json.loads(self.installer("migrate-codex-hook", "--codex-home", str(self.codex), "--json").stdout)
        self.assertTrue((self.codex / "hooks.json").is_symlink())
        self.assertIn(FIXTURE["a"], json.loads(real.read_text())["hooks"]["Stop"][0]["hooks"][0]["command"])
        self.assertEqual(stat.S_IMODE(os.stat(real).st_mode), 0o644)
        backup = Path(result["backup"])
        self.assertEqual(backup.parent, dotfiles)
        self.assertEqual(stat.S_IMODE(os.stat(backup).st_mode), 0o600)

    def test_duplicate_keys_are_refused_without_changes(self):
        hooks = self.codex / "hooks.json"
        hooks.write_text('{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": %s}]}]},'
                         ' "hooks": {}}' % json.dumps(self.checkout_hook))
        original = hooks.read_bytes()
        result = self.installer("migrate-codex-hook", "--codex-home", str(self.codex), ok=False)
        self.assertIn("duplicate key 'hooks'", result.stderr)
        self.assertEqual(hooks.read_bytes(), original)
        self.assertEqual(list(self.codex.glob("hooks.json.syndicate-backup-*")), [])

    def test_migration_waits_for_the_activation_lock(self):
        hooks = self.codex / "hooks.json"
        hooks.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command",
                                                                   "command": self.checkout_hook}]}]}}))
        err = self.tmp / "migrate.err"
        with open(self.paths.lock, "a") as held, open(err, "w") as stderr:
            fcntl.flock(held, fcntl.LOCK_EX)
            proc = subprocess.Popen([sys.executable, "-B", str(INSTALLER), "migrate-codex-hook", "--codex-home",
                                     str(self.codex)], cwd=self.tmp / "elsewhere", env=self.env,
                                    stdout=subprocess.DEVNULL, stderr=stderr)
            self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
            deadline = time.time() + 60
            while "waiting for activation lock" not in err.read_text():
                self.assertIsNone(proc.poll(), err.read_text())
                self.assertLess(time.time(), deadline)
                time.sleep(0.05)
            self.assertNotIn(FIXTURE["a"], hooks.read_text())
            fcntl.flock(held, fcntl.LOCK_UN)
        self.assertEqual(proc.wait(timeout=60), 0, err.read_text())
        self.assertIn(FIXTURE["a"], hooks.read_text())



def with_uid(path, uid):
    """An os.stat replacement that reports path as owned by uid (ownership cannot be changed here)."""
    real_stat = os.stat
    target = os.path.abspath(path)

    def fake(name, *args, **kwargs):
        result = real_stat(name, *args, **kwargs)
        if isinstance(name, (str, os.PathLike)) and os.path.abspath(name) == target:
            values = list(result[:10])
            values[4] = uid
            return os.stat_result(values)
        return result

    return fake


class CacheRedirection(InstallCase):
    """Batch 4: nothing in the cache can redirect the fetch or what Git reads."""

    def setUp(self):
        super().setUp()
        self.install("v1.0.0")
        self.cache = self.paths.cache
        self.config = self.cache / "config"
        self.original_config = self.config.read_bytes()
        self.before = (self.current(), self.release_entries())

    def refused(self, message, *extra):
        result = self.install("v1.1.0", *extra, ok=False)
        self.assertIn(message, result.stderr)
        self.assert_unchanged(*self.before)
        return result

    def evil_upstream(self):
        """A bare repository whose main and v1.1.0 name the off-main side commit."""
        evil = self.tmp / "evil.git"
        shutil.copytree(FIXTURE["upstream"], evil)
        run_git(evil, "update-ref", "refs/heads/main", FIXTURE["side"])
        run_git(evil, "tag", "-f", "v1.1.0", FIXTURE["side"])
        return evil

    def test_a_duplicate_upstream_url_is_refused_in_either_order(self):
        evil = self.evil_upstream()
        good_line = next(line for line in self.config.read_text().splitlines() if line.strip().startswith("url ="))
        evil_line = f'\turl = "{evil}"'
        for name, lines in (("evil first", [evil_line, good_line]), ("evil last", [good_line, evil_line])):
            with self.subTest(order=name):
                self.config.write_text(self.config.read_text().replace(good_line, "\n".join(lines)))
                self.refused("sets remote.upstream.url more than once")
                self.refused("sets remote.upstream.url more than once", "--upstream", str(FIXTURE["upstream"]))
                self.config.write_bytes(self.original_config)
        self.install("v1.1.0")
        self.assertEqual(self.current(), FIXTURE["b"])

    def test_any_repeated_key_or_other_remote_spelling_is_refused(self):
        for addition, message in (("\tfetch = +refs/heads/*:refs/remotes/upstream/*\n", "remote.upstream.fetch more than once"),
                                  ("[core]\n\tbare = true\n", "core.bare more than once"),
                                  (f'[remote "UPSTREAM"]\n\turl = "{self.tmp}/elsewhere"\n', "remote.UPSTREAM.url")):
            with self.subTest(addition=addition):
                text = self.original_config.decode()
                if addition.startswith("\t"):
                    text = text.replace('[remote "upstream"]\n', '[remote "upstream"]\n' + addition)
                else:
                    text += addition
                self.config.write_text(text)
                self.refused(message)
                self.config.write_bytes(self.original_config)

    def test_recorded_upstream_is_compared_exactly_and_must_be_absolute_or_a_url(self):
        run_git(self.cache, "config", "remote.upstream.url", str(FIXTURE["upstream"]) + " ")
        self.refused("--change-upstream", "--upstream", str(FIXTURE["upstream"]))
        self.config.write_bytes(self.original_config)
        result = self.install("v1.1.0", "--upstream", "upstream", "--change-upstream", ok=False)
        self.assertIn("absolute path or a URL", result.stderr)
        run_git(self.cache, "config", "remote.upstream.url", "upstream")
        result = self.installer("install", "--tag", "v1.1.0", ok=False)
        self.assertIn("absolute path or a URL", result.stderr)
        self.assert_unchanged(*self.before)
        self.config.write_bytes(self.original_config)
        self.install("v1.1.0")

    def test_fetches_name_the_trusted_url_not_the_remote(self):
        calls = []
        real_git = rel.git

        def spy(repo, *args, **kwargs):
            if "fetch" in args:
                calls.append(args)
            return real_git(repo, *args, **kwargs)

        with mock.patch.object(rel, "git", side_effect=spy):
            approved = rel.resolve_approved_tag(self.paths, "v1.1.0", None)
        self.assertEqual(approved["commit"], FIXTURE["b"])
        self.assertEqual(len(calls), 2)
        for args in calls:
            index = args.index("--")
            self.assertEqual(args[index + 1], str(FIXTURE["upstream"]))
            self.assertNotIn(rel.REMOTE, args)

    def test_commondir_gitdir_and_dot_git_entries_are_refused(self):
        # A graft in another repository would make the off-main side commit look
        # like it is on main; the redirecting entry is refused before Git reads it.
        other = self.tmp / "other.git"
        shutil.copytree(self.cache, other)
        run_git(other, "fetch", "-q", str(FIXTURE["upstream"]), "+refs/heads/feature:refs/attacker/feature")
        (other / "info").mkdir(exist_ok=True)
        (other / "info" / "grafts").write_text(f"{FIXTURE['c']} {FIXTURE['side']}\n")
        for name, content in (("commondir", f"{other}\n"), ("gitdir", f"{other}\n"), (".git", f"gitdir: {other}\n")):
            with self.subTest(name=name):
                path = self.cache / name
                path.write_text(content)
                self.refused(f"has {name}, which points Git at another directory")
                path.unlink()
        (self.cache / ".git").mkdir()
        self.refused("has .git")
        (self.cache / ".git").rmdir()
        self.install("v1.1.0")

    def test_symlinks_inside_the_cache_are_refused(self):
        for name in ("refs", "objects", "config", "HEAD", "objects/pack"):
            with self.subTest(name=name):
                path = self.cache / name
                moved = path.with_name(path.name + "-real")
                path.rename(moved)
                path.symlink_to(moved.name)
                self.refused(f"has a symlink {name}")
                path.unlink()
                moved.rename(path)
        self.install("v1.1.0")

    def test_cache_entries_owned_by_another_account_are_refused(self):
        real_scandir = os.scandir
        head = str(self.cache / "HEAD")

        class Entry:
            def __init__(self, entry):
                self._entry, self.name, self.path = entry, entry.name, entry.path

            def stat(self, follow_symlinks=True):
                result = self._entry.stat(follow_symlinks=follow_symlinks)
                if self.path == head:
                    values = list(result[:10])
                    values[4] = 4242
                    return os.stat_result(values)
                return result

        def scandir(path="."):
            return [Entry(e) for e in real_scandir(path)]

        with mock.patch.object(rel.os, "scandir", side_effect=scandir):
            with self.assertRaisesRegex(rel.ReleaseError, "HEAD is owned by uid 4242"):
                rel.check_cache(self.paths)


class SourceCacheLocation(InstallCase):
    def test_chosen_cache_parent_is_checked_never_chmodded(self):
        chosen = self.tmp / "chosen"
        chosen.mkdir()
        chosen.chmod(0o750)
        cache = chosen / "source.git"
        self.install("v1.0.0", "--source-cache", str(cache))
        self.assertEqual(stat.S_IMODE(os.stat(chosen).st_mode), 0o750)
        self.assertEqual(stat.S_IMODE(os.stat(cache).st_mode), 0o700)
        # Another account could write it: refused, and still not chmodded.
        chosen.chmod(0o777)
        self.addCleanup(chosen.chmod, 0o755)
        result = self.installer("install", "--tag", "v1.1.0", "--source-cache", str(cache), ok=False)
        self.assertIn(f"{chosen} is world-writable", result.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(chosen).st_mode), 0o777)
        self.assertEqual(self.current(), FIXTURE["a"])
        chosen.chmod(0o750)
        self.installer("install", "--tag", "v1.1.0", "--source-cache", str(cache))
        self.assertEqual(self.current(), FIXTURE["b"])

    def test_missing_cache_parents_are_created_private(self):
        def loose():
            os.umask(0o002)

        cache = self.tmp / "new" / "deeper" / "source.git"
        self.install("v1.0.0", "--source-cache", str(cache), preexec_fn=loose)
        for path in (cache.parent.parent, cache.parent, cache):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700, path)

    def test_a_cache_directory_that_is_not_a_cache_is_refused(self):
        other = self.tmp / "not a cache"
        other.mkdir()
        (other / "notes.txt").write_text("mine\n")
        result = self.install("v1.0.0", "--source-cache", str(other), ok=False)
        self.assertIn("is not a release-source cache", result.stderr)
        self.assertEqual(sorted(p.name for p in other.iterdir()), ["notes.txt"])

    def test_a_parent_owned_by_another_account_is_a_clean_error(self):
        paths = rel.Paths(self.root, self.paths.bin_dir, self.tmp / "theirs" / "source.git")
        (self.tmp / "theirs").mkdir()
        with mock.patch.object(rel.os, "stat", side_effect=with_uid(self.tmp / "theirs", 4242)):
            with self.assertRaisesRegex(rel.ReleaseError, "theirs is owned by uid 4242"):
                rel.prepare_cache_parent(paths)
        # Unable to create a missing parent (here: a read-only directory).
        locked = self.tmp / "locked"
        locked.mkdir()
        locked.chmod(0o555)
        self.addCleanup(locked.chmod, 0o755)
        paths = rel.Paths(self.root, self.paths.bin_dir, locked / "sub" / "source.git")
        with self.assertRaisesRegex(rel.ReleaseError, "cannot prepare release-source cache directory"):
            rel.prepare_cache_parent(paths)
        result = self.install("v1.0.0", "--source-cache", str(locked / "sub" / "source.git"), ok=False)
        self.assertIn("cannot prepare release-source cache directory", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class InstallRootParent(InstallCase):
    def test_root_parent_is_created_private_and_refused_when_others_can_write_it(self):
        def loose():
            os.umask(0o002)

        root = self.tmp / "store" / "nested" / "syndicate"
        self.install("v1.0.0", "--install-root", str(root), preexec_fn=loose)
        for path in (root.parent.parent, root.parent):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700, path)
        self.assertEqual(stat.S_IMODE(os.stat(root).st_mode), 0o755)
        root.parent.chmod(0o777)
        self.addCleanup(root.parent.chmod, 0o755)
        result = self.install("v1.1.0", "--install-root", str(root), ok=False)
        self.assertIn(f"install root parent {root.parent} is world-writable", result.stderr)
        # A sticky directory owned by you (or root), like /tmp, is fine.
        root.parent.chmod(0o1777)
        self.install("v1.1.0", "--install-root", str(root))
        # ...but not one owned by another account, who could rename the root.
        paths = rel.Paths(root, self.paths.bin_dir, self.paths.cache)
        with mock.patch.object(rel.os, "stat", side_effect=with_uid(root.parent, 4242)):
            with self.assertRaisesRegex(rel.ReleaseError, "install root parent .* owned by uid 4242"):
                rel.prepare_root(paths)


class StickyAndWorktreeLookups(InstallCase):
    def test_sticky_directory_must_be_owned_by_root_or_you(self):
        sticky = self.tmp / "sticky"
        sticky.mkdir()
        sticky.chmod(0o1777)
        self.addCleanup(sticky.chmod, 0o755)
        link = sticky / "python3"
        link.symlink_to(os.path.abspath(sys.executable))
        self.assertIsNone(rel.redirect_problem(str(link)))
        self.assertTrue(rel.sticky_protects(sticky, link))
        # The owner of a sticky directory can rename anyone's entries in it.
        with mock.patch.object(rel.os, "stat", side_effect=with_uid(sticky, 4242)):
            self.assertFalse(rel.sticky_protects(sticky, link))
            with self.assertRaisesRegex(rel.ReleaseError, "sticky .*owned by uid 4242"):
                rel.preflight_interpreter(str(link))

    def test_worktree_reached_through_an_intermediate_symlink_is_refused(self):
        shared = self.tmp / "shared bin"
        shared.mkdir()
        wrapper = shared / "python3"
        wrapper.write_text(f"#!/bin/sh\nexec '{os.path.abspath(sys.executable)}' \"$@\"\n")
        wrapper.chmod(0o755)
        repo = self.tmp / "repo"
        (repo / "sub").mkdir(parents=True)
        (repo / ".git").mkdir()
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (repo / "sub" / "deeper").symlink_to(shared)
        (self.tmp / "x").mkdir()
        (self.tmp / "x" / "link").symlink_to(repo / "sub")
        path = self.tmp / "x" / "link" / "deeper" / "python3"
        # Neither the given path's directory nor the resolved file is in a work tree...
        self.assertIsNone(rel.inside_git_worktree(str(path.parent)))
        self.assertIsNone(rel.inside_git_worktree(os.path.dirname(os.path.realpath(path))))
        # ...but resolving it looks entries up in repo/sub, which an agent in repo can change.
        with self.assertRaisesRegex(rel.ReleaseError, "Git work tree .*repo"):
            rel.preflight_interpreter(str(path))
        self.assertEqual(rel.preflight_interpreter(str(wrapper)), str(wrapper))

    def test_git_marker_forms(self):
        base = self.tmp / "markers"
        cases = {"empty dir": None, "dir with HEAD": "HEAD", "dir with dangling HEAD symlink": "link",
                 "gitdir file": b"gitdir: ../.git/worktrees/x\n", "other file": b"not a gitdir\n"}
        expected = {"empty dir": False, "dir with HEAD": True, "dir with dangling HEAD symlink": True,
                    "gitdir file": True, "other file": False}
        for name, content in cases.items():
            with self.subTest(case=name):
                marker = base / name / ".git"
                marker.parent.mkdir(parents=True)
                if isinstance(content, bytes):
                    marker.write_bytes(content)
                else:
                    marker.mkdir()
                    if content == "HEAD":
                        (marker / "HEAD").write_text("ref: refs/heads/main\n")
                    elif content == "link":
                        (marker / "HEAD").symlink_to(marker / "missing")
                self.assertEqual(rel.is_git_marker(marker), expected[name])
                self.assertEqual(rel.inside_git_worktree(str(marker.parent)) == str(marker.parent), expected[name])


class InUseAndVerifyPermissions(InstallCase):
    def test_launcher_tightens_existing_in_use_directories(self):
        self.install("v1.0.0")
        directory = self.paths.in_use / FIXTURE["a"]
        directory.mkdir(parents=True)
        self.paths.in_use.chmod(0o777)
        directory.chmod(0o775)
        release = Path(os.path.realpath(self.paths.releases / FIXTURE["a"]))
        code = ("import importlib.machinery, importlib.util, json, os, sys\n"
                "loader = importlib.machinery.SourceFileLoader('syndicate', sys.argv[1])\n"
                "spec = importlib.util.spec_from_loader('syndicate', loader)\n"
                "sy = importlib.util.module_from_spec(spec); loader.exec_module(sy)\n"
                "sy.mark_release_in_use()\n"
                "marker = sy._release_markers[0]\n"
                "print(json.dumps([oct(os.stat(p).st_mode & 0o777) for p in (marker.parent.parent, marker.parent)]))\n")
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(release / "bin" / "syndicate")],
                                cwd=self.tmp / "elsewhere", env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["0o700", "0o700"])
        # A symlinked in-use/ is refused with a warning, never written through.
        elsewhere = self.tmp / "elsewhere in-use"
        elsewhere.mkdir()
        rel.remove_tree(self.paths.in_use)
        self.paths.in_use.symlink_to(elsewhere)
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code.replace("marker = sy._release_markers[0]\n", "")
                                 .replace("for p in (marker.parent.parent, marker.parent)", "for p in ()"),
                                 str(release / "bin" / "syndicate")],
                                cwd=self.tmp / "elsewhere", env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("could not write release in-use marker", result.stderr)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_prune_tightens_and_verify_checks_the_install_root(self):
        self.install("v1.0.0")
        self.assertEqual(self.installer("verify").returncode, 0)
        directory = self.paths.in_use / FIXTURE["a"]
        directory.mkdir(parents=True)
        (directory / str(os.getpid())).touch()
        for path in (self.root, self.paths.releases, self.paths.interpreters, self.paths.in_use):
            with self.subTest(path=path):
                path.chmod(0o777)
                result = self.installer("verify", ok=False)
                self.assertIn("matches its manifest, but", result.stderr)
                self.assertIn(f"{path} is world-writable", result.stderr)
                path.chmod(0o755)
        directory.chmod(0o777)
        self.installer("prune")
        for path in (self.paths.in_use, directory):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700, path)
        self.assertEqual(self.installer("verify").returncode, 0)


class PrunedHookRelease(InstallCase):
    def setUp(self):
        super().setUp()
        self.codex = self.tmp / "codex"
        self.hooks = self.codex / "hooks.json"
        self.install("v1.0.0")
        self.install("v1.1.0")
        self.old = rel.shlex.quote(str(Path(os.path.realpath(self.paths.releases / FIXTURE["a"]))
                                       / "hooks" / "require-review.sh"))
        self.checkout = rel.shlex.quote(str(FIXTURE["src"] / "hooks" / "require-review.sh"))

    def write(self, *commands):
        self.hooks.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": c}]}
                                                             for c in commands]}}, indent=2) + "\n")
        return self.hooks.read_bytes()

    def migrate(self, ok=True):
        return self.installer("migrate-codex-hook", "--codex-home", str(self.codex), "--json", ok=ok)

    def test_hook_in_a_pruned_release_is_skipped_not_fatal(self):
        # Prune A while the hook still names it (prune was told about another Codex home).
        self.installer("install", "--tag", "v1.2.0")
        self.installer("prune", "--codex-home", str(self.tmp / "other codex"))
        self.assertFalse((self.paths.releases / FIXTURE["a"]).exists())
        original = self.write(self.old)
        result = self.migrate()
        self.assertIn("no longer retained", result.stderr)
        data = json.loads(result.stdout)
        self.assertFalse(data["changed"])
        self.assertEqual([s["command"] for s in data["skipped"]], [self.old])
        self.assertEqual(self.hooks.read_bytes(), original)
        # Beside a verified hook, that one migrates and the missing one is preserved.
        self.write(self.old, self.checkout)
        data = json.loads(self.migrate().stdout)
        self.assertTrue(data["changed"])
        stop = json.loads(self.hooks.read_text())["hooks"]["Stop"]
        self.assertEqual([e["hooks"][0]["command"] for e in stop], [self.old, data["command"]])

    def test_missing_hook_in_a_retained_release_still_stops_the_migration(self):
        hook = Path(rel.shlex.split(self.old)[0])
        hook.parent.chmod(0o755)
        hook.unlink()
        original = self.write(self.old, self.checkout)
        result = self.migrate(ok=False)
        self.assertIn("cannot be read", result.stderr)
        self.assertEqual(self.hooks.read_bytes(), original)
        self.assertEqual(list(self.codex.glob("hooks.json.syndicate-backup-*")), [])


if __name__ == "__main__":
    unittest.main()


class UpstreamForms(InstallCase):
    """Batch 5: only absolute paths, allowed URL schemes and scp-like forms; local upstreams exactly as named."""

    def setUp(self):
        super().setUp()
        self.before = (None, [])

    def refused(self, message, *args):
        result = self.installer("install", "--tag", "v1.0.0", *args, ok=False)
        self.assertIn(message, result.stderr)
        self.assert_unchanged(*self.before)
        return result

    def test_accepted_and_refused_forms(self):
        for upstream in ("/srv/git/syndicate.git", "https://github.com/nabt/syndicate.git",
                         "ssh://git@example.com:2222/nabt/syndicate.git", "ssh://[::1]/srv/syndicate.git",
                         "git://example.com/syndicate.git", "file:///srv/git/syndicate.git",
                         "git@github.com:nabt/syndicate.git", "example.com:syndicate.git",
                         "host:path/with:colon"):
            with self.subTest(accepted=upstream):
                rel.check_upstream_form(upstream)
        for upstream, message in (
                ("upstream", "absolute path"), ("../../chk:z", "absolute path"), ("./x:y", "absolute path"),
                ("relative/dir", "absolute path"), ("-upload-pack=x", "absolute path"), ("", "absolute path"),
                ("http://example.com/x.git", "absolute path"), ("ftp://example.com/x.git", "absolute path"),
                ("HTTPS://example.com/x.git", "absolute path"), ("file://relative/x.git", "file://"),
                ("file:///srv/%2e%2e/x.git", "file://"), ("file://", "file://"),
                ("ext::sh -c touch% /tmp/pwned", "<transport>::"), ("fd::0", "<transport>::"),
                ("fd::0,1", "<transport>::"), ("https::example.com/x", "<transport>::"),
                ("host::path", "absolute path"), ("u@-oProxyCommand=x:path", "absolute path"),
                ("[::1]:path", "absolute path"), ("/srv/x\n", "absolute path")):
            with self.subTest(refused=upstream):
                with self.assertRaisesRegex(rel.ReleaseError, message.replace("(", r"\(")):
                    rel.check_upstream_form(upstream)

    def test_a_relative_existing_directory_is_made_absolute(self):
        copy = self.tmp / "relative upstream.git"
        shutil.copytree(FIXTURE["upstream"], copy)
        self.installer("install", "--tag", "v1.0.0", "--upstream", "../relative upstream.git")
        self.assertEqual(self.current(), FIXTURE["a"])
        self.assertEqual(self.records()[-1]["upstream"], str(copy))
        self.assertEqual(run_git(self.paths.cache, "config", "remote.upstream.url"), str(copy))

    def test_suffix_probing_and_colon_paths_are_refused(self):
        # A checkout at chk.git (so Git would find chk.git/.git for "chk"), and
        # a bare repository holding a .git (which upload-pack would prefer).
        run_git(self.tmp, "clone", "-q", str(FIXTURE["upstream"]), str(self.tmp / "chk.git"))
        holder = self.tmp / "holder.git"
        shutil.copytree(FIXTURE["upstream"], holder)
        run_git(self.tmp, "clone", "-q", str(FIXTURE["upstream"]), str(holder / ".git-src"))
        (holder / ".git").write_text(f"gitdir: {holder / '.git-src' / '.git'}\n")
        for upstream, message in ((str(self.tmp / "chk"), "does not exist as a directory"),
                                  (f"file://{self.tmp / 'chk'}", "does not exist as a directory"),
                                  (str(self.tmp / "chk.git"), "not a bare repository"),
                                  (f"file://{self.tmp / 'chk.git'}", "not a bare repository"),
                                  (str(holder), "not a bare repository"),
                                  ("../../chk:z", "absolute path")):
            with self.subTest(upstream=upstream):
                self.refused(message, "--upstream", upstream)
        # A file:// URL naming a bare repository exactly works.
        self.installer("install", "--tag", "v1.0.0", "--upstream", f"file://{FIXTURE['upstream']}")
        self.assertEqual(self.current(), FIXTURE["a"])

    def test_recorded_upstream_gets_the_same_checks(self):
        self.install("v1.0.0")
        self.before = (self.current(), self.release_entries())
        run_git(self.tmp, "clone", "-q", str(FIXTURE["upstream"]), str(self.tmp / "chk.git"))
        for recorded in (str(self.tmp / "chk"), f"file://{self.tmp / 'chk'}", "../../chk:z", "fd::0"):
            with self.subTest(recorded=recorded):
                run_git(self.paths.cache, "config", "remote.upstream.url", recorded)
                result = self.refused("no longer acceptable")
                self.assertIn("--upstream ABSOLUTE-PATH|URL --change-upstream", result.stderr)
        self.installer("install", "--tag", "v1.1.0", "--upstream", str(FIXTURE["upstream"]), "--change-upstream")
        self.assertEqual(self.current(), FIXTURE["b"])

    def test_insteadof_rewrites_are_refused(self):
        self.install("v1.0.0")
        self.before = (self.current(), self.release_entries())
        evil = self.tmp / "evil.git"
        shutil.copytree(FIXTURE["upstream"], evil)
        run_git(evil, "update-ref", "refs/heads/main", FIXTURE["side"])
        run_git(evil, "tag", "-f", "v1.1.0", FIXTURE["side"])
        global_config = self.tmp / "gitconfig"
        for text in (f'[url "{evil}"]\n\tinsteadOf = {FIXTURE["upstream"]}\n',
                     f'[remote "{FIXTURE["upstream"]}"]\n\turl = {evil}\n'):
            with self.subTest(config=text):
                global_config.write_text(text)
                self.env["GIT_CONFIG_GLOBAL"] = str(global_config)
                result = self.installer("install", "--tag", "v1.1.0", ok=False)
                self.assertIn("rewrites upstream", result.stderr)
                self.assert_unchanged(*self.before)
        self.env["GIT_CONFIG_GLOBAL"] = os.devnull
        self.install("v1.1.0")
        self.assertEqual(self.current(), FIXTURE["b"])

    def test_network_git_calls_pin_protocols_and_are_bounded(self):
        self.install("v1.0.0")
        calls = []
        real_git = rel.git

        def spy(repo, *args, **kwargs):
            if "fetch" in args or "ls-remote" in args:
                calls.append((args, kwargs, kwargs.get("options", ())))
            return real_git(repo, *args, **kwargs)

        with mock.patch.object(rel, "git", side_effect=spy):
            rel.resolve_approved_tag(self.paths, "v1.1.0", None)
        self.assertEqual(sorted(sum(("fetch" in a, "ls-remote" in a) for a, _, _ in calls)
                                if False else ["fetch" if "fetch" in a else "ls-remote" for a, _, _ in calls]),
                         ["fetch", "fetch", "ls-remote"])
        for args, kwargs, options in calls:
            command = [*options, *args]
            start = command.index("protocol.allow=never") - 1
            self.assertEqual(command[start:start + len(rel.PROTOCOL_OPTIONS)], list(rel.PROTOCOL_OPTIONS))
            self.assertEqual(kwargs.get("timeout"), rel.GIT_NETWORK_TIMEOUT)
            self.assertIn("safe.directory=", options)


class ObjectPoisoning(InstallCase):
    """Batch 5: content substituted under a genuine object id in the cache is refused."""

    def setUp(self):
        super().setUp()
        self.install("v1.0.0")
        self.cache = self.paths.cache
        self.before = (self.current(), self.release_entries())
        # Explode every pack into loose objects, as a cache that only ever fetched little would be.
        packs = self.cache / "objects" / "pack"
        for pack in sorted(packs.glob("*.pack")):
            moved = self.tmp / pack.name
            shutil.move(pack, moved)
            pack.with_suffix(".idx").unlink()
            for other in pack.parent.glob(pack.stem + ".*"):
                other.unlink()
            with open(moved, "rb") as handle:
                subprocess.run(["git", "-C", str(self.cache), "unpack-objects", "-q"], stdin=handle, check=True,
                               env=FIXTURE["env"])

    def loose(self, oid):
        return self.cache / "objects" / oid[:2] / oid[2:]

    def forge(self, oid, kind, content):
        import zlib
        path = self.loose(oid)
        self.assertTrue(path.is_file(), path)
        path.chmod(0o644)
        path.write_bytes(zlib.compress(b"%s %d\0" % (kind, len(content)) + content))

    def test_a_forged_subtree_under_the_genuine_id_is_refused(self):
        b = FIXTURE["b"]
        bin_tree = run_git(self.cache, "rev-parse", f"{b}:bin")
        evil_blob = subprocess.run(["git", "-C", str(self.cache), "hash-object", "-w", "--stdin"],
                                   input=b"#!/bin/sh\necho forged\n", capture_output=True, check=True,
                                   env=FIXTURE["env"]).stdout.decode().strip()
        listing = run_git(self.cache, "ls-tree", f"{b}:bin")
        forged_listing = "\n".join(line.rsplit(" ", 1)[0].rsplit(" ", 1)[0] + f" blob {evil_blob}\tsyndicate"
                                   if line.endswith("\tsyndicate") else line for line in listing.splitlines())
        forged_tree = subprocess.run(["git", "-C", str(self.cache), "mktree"], input=forged_listing + "\n",
                                     capture_output=True, text=True, check=True, env=FIXTURE["env"]).stdout.strip()
        with open(self.loose(forged_tree), "rb") as handle:
            import zlib
            raw = zlib.decompress(handle.read())
        self.forge(bin_tree, b"tree", raw.split(b"\0", 1)[1])
        # Plain Git now reads the forged script under the genuine commit.
        self.assertEqual(run_git(self.cache, "rev-parse", f"{b}:bin/syndicate"), evil_blob)
        result = self.install("v1.1.0", ok=False)
        self.assertIn(f"object {bin_tree} in the release-source cache does not hash to its id", result.stderr)
        self.assert_unchanged(*self.before)

    def test_forged_commits_and_blobs_are_refused(self):
        b, c = FIXTURE["b"], FIXTURE["c"]
        for oid, kind, mutate in ((c, b"commit", lambda d: d.replace(b"release C", b"release X")),
                                  (run_git(self.cache, "rev-parse", f"{b}:README.md"), b"blob",
                                   lambda d: d + b"forged\n")):
            with self.subTest(kind=kind):
                original = self.loose(oid).read_bytes()
                import zlib
                content = zlib.decompress(original).split(b"\0", 1)[1]
                self.forge(oid, kind, mutate(content))
                result = self.install("v1.1.0", ok=False)
                self.assertIn(f"object {oid} in the release-source cache does not hash to its id", result.stderr)
                self.assert_unchanged(*self.before)
                self.loose(oid).write_bytes(original)
        self.install("v1.1.0")
        self.assertEqual(self.current(), b)

    def test_verified_listing_matches_ls_tree(self):
        approved = rel.resolve_approved_tag(self.paths, "v1.1.0", None)
        verified = rel.verify_objects(self.cache, "sha1", approved["commit"],
                                      run_git(self.cache, "rev-parse", rel.FETCHED_MAIN),
                                      run_git(self.cache, "rev-parse", rel.FETCHED_TAGS + "v1.1.0"))
        entries = rel.tree_entries(self.cache, approved["commit"])
        self.assertEqual(entries, {p: e for p, e in verified.items() if rel.allowed(p)})
        with self.assertRaisesRegex(rel.ReleaseError, "not reachable from upstream main"):
            rel.verify_objects(self.cache, "sha1", FIXTURE["c"], FIXTURE["b"], FIXTURE["c"])


class Batch5Permissions(InstallCase):
    def test_validation_precedes_tightening_of_a_chosen_cache(self):
        project = self.tmp / "project"
        run_git(self.tmp, "init", "-q", str(project))
        git_dir = project / ".git"
        git_dir.chmod(0o755)
        result = self.install("v1.0.0", "--source-cache", str(git_dir), ok=False)
        self.assertIn("unexpected configuration", result.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(git_dir).st_mode), 0o755)

    def test_verify_checks_the_root_parent_and_names_the_fix(self):
        self.install("v1.0.0")
        parent = self.root.parent
        parent.chmod(0o777)
        self.addCleanup(parent.chmod, 0o755)
        result = self.installer("verify", ok=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"install root parent {parent} is world-writable", result.stderr)
        self.assertIn(f"chmod go-w '{parent}'", result.stderr)
        parent.chmod(0o1777)
        self.assertEqual(self.installer("verify").returncode, 0)
        parent.chmod(0o755)
        self.paths.releases.chmod(0o775)
        result = self.installer("verify", ok=False)
        self.assertIn(f"chmod go-w '{self.paths.releases}'", result.stderr)

    def test_launcher_refuses_in_use_directories_of_another_account(self):
        self.install("v1.0.0")
        directory = self.paths.in_use / FIXTURE["a"]
        directory.mkdir(parents=True, mode=0o700)
        release = Path(os.path.realpath(self.paths.releases / FIXTURE["a"]))
        for target in (self.paths.in_use, directory):
            with self.subTest(target=target):
                code = ("import importlib.machinery, importlib.util, os, sys\n"
                        "real = os.lstat\n"
                        "def fake(p, *a, **k):\n"
                        "    r = real(p, *a, **k)\n"
                        "    if os.path.abspath(p) == sys.argv[2]:\n"
                        "        v = list(r[:10]); v[4] = 4242; return os.stat_result(v)\n"
                        "    return r\n"
                        "os.lstat = fake\n"
                        "loader = importlib.machinery.SourceFileLoader('syndicate', sys.argv[1])\n"
                        "spec = importlib.util.spec_from_loader('syndicate', loader)\n"
                        "sy = importlib.util.module_from_spec(spec); loader.exec_module(sy)\n"
                        "sy.mark_release_in_use()\n"
                        "print(len(sy._release_markers))\n")
                result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(release / "bin" / "syndicate"),
                                         str(target)], cwd=self.tmp / "elsewhere", env=self.env,
                                        capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("is owned by uid 4242, not by you", result.stderr)
                self.assertEqual(result.stdout.strip(), "0")
                self.assertEqual(list(directory.iterdir()), [])

    def test_installer_docstring(self):
        code = ("import importlib.machinery, importlib.util, sys\n"
                "loader = importlib.machinery.SourceFileLoader('syndicate_install', sys.argv[1])\n"
                "spec = importlib.util.spec_from_loader('syndicate_install', loader)\n"
                "module = importlib.util.module_from_spec(spec); loader.exec_module(module)\n"
                "print(module.__doc__)\n")
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(INSTALLER)], cwd=self.tmp / "elsewhere",
                                env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("syndicate-install: install, upgrade, roll back"), result.stdout)
        self.assertIn("docs/user-install.md", result.stdout)
        self.assertIn("vX.Y.Z", rel.__doc__ + result.stdout)
