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
        # Even with the dirty checkout itself as upstream, only committed objects are used.
        self.installer("install", "--tag", "v1.2.0", "--upstream", str(src))
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
        bad_root = self.tmp / "store (old)" / "syndicate"
        result = self.install("v1.0.0", "--install-root", str(bad_root), ok=False)
        self.assertIn("contains ')'", result.stderr)
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
if argv[:1] == ["exec"]:
    record("specialist", instructions=instructions, brief=argv[-1])
    print("specialist finished")
    sys.exit(0)

project = argv[argv.index("-C") + 1]
configs = [json.loads(a.split("=", 1)[1]) for a in argv if re.match(r"agents\.[^.=]+\.config_file=", a)]
before = {c: Path(c).read_text() for c in configs}
record("orchestrator", configs=before)
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
        env = dict(self.env, FAKE_CODEX_LOG=str(log_path), FAKE_CODEX_GO=str(go))
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

    def test_unverifiable_hook_is_refused_without_changes(self):
        fake_checkout = self.tmp / "copy" / "hooks"
        fake_checkout.mkdir(parents=True)
        (fake_checkout / "require-review.sh").write_text("#!/bin/sh\nexit 0\n")
        modified = self.tmp / "repo"
        shutil.copytree(FIXTURE["src"], modified)
        (modified / "hooks" / "require-review.sh").write_text("#!/bin/sh\necho changed\n")
        for path, message in ((fake_checkout / "require-review.sh", "neither in a Git checkout"),
                              (modified / "hooks" / "require-review.sh", "differs from"),
                              (self.tmp / "gone" / "hooks" / "require-review.sh", "cannot be read")):
            with self.subTest(path=path):
                self.write_hooks([{"hooks": [{"type": "command", "command": rel.shlex.quote(str(path))}]}])
                original = self.hooks.read_bytes()
                result = self.migrate(ok=False)
                self.assertIn(message, result.stderr)
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


if __name__ == "__main__":
    unittest.main()
