"""Runtime boundary: pinned self-invocation, resource lookup, identity, dev mode.

Every subprocess runs with a disposable HOME, XDG directories, provider homes,
TMPDIR and TMUX_TMPDIR, and without inherited SYNDICATE_* or TMUX variables.
"""
import contextlib
import io
import json
import os
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from test_syndicate import ProjectCase, sy

identity = sy.identity
runtime = sy.runtime
live = sy.live
dev = sy.syndicate_dev
CHECKOUT = Path(sy.SCRIPT).parent.parent
COMMIT = "a" * 40
OTHER = "b" * 40
# The suite also runs from an extracted release tree, which has no .git.
CHECKOUT_IS_GIT = identity._git(CHECKOUT, "rev-parse", "--show-toplevel") == str(CHECKOUT)
NOT_GIT_REASON = f"{CHECKOUT} is not a Git checkout (an extracted release tree); checkout-mode identity needs Git"


def copy_runtime(destination: Path) -> None:
    """Copy this checkout's runtime files: dev.stage_snapshot from Git, else a plain tree copy.

    Either way files are read-only, directories writable, and no .git, bytecode,
    project state or release manifest is copied."""
    if CHECKOUT_IS_GIT:
        dev.stage_snapshot(CHECKOUT, destination)
        return
    shutil.copytree(CHECKOUT, destination, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "*.pyo", ".syndicate",
                                                  "RELEASE.json"))
    for path in [destination, *destination.rglob("*")]:
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        path.chmod(mode | 0o700 if path.is_dir() else mode & ~0o222)


def disposable_env(base: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("SYNDICATE_") and k not in ("TMUX", "TMUX_PANE", "PYTHONDONTWRITEBYTECODE")}
    for name in ("home", "config", "data", "cache", "codex", "claude", "tmp", "tmux"):
        (base / name).mkdir(parents=True, exist_ok=True)
    env.update(HOME=str(base / "home"), XDG_CONFIG_HOME=str(base / "config"),
               XDG_DATA_HOME=str(base / "data"), XDG_CACHE_HOME=str(base / "cache"),
               CODEX_HOME=str(base / "codex"), CLAUDE_CONFIG_DIR=str(base / "claude"),
               TMPDIR=str(base / "tmp"), TMUX_TMPDIR=str(base / "tmux"))
    env.update(extra)
    return env


def tree(root: Path) -> dict:
    """Relative path -> (size, mtime_ns) for every entry under root."""
    result = {}
    for directory, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(directory) / name
            info = path.lstat()
            result[str(path.relative_to(root))] = (info.st_size, info.st_mtime_ns)
    return result


def caches(root: Path) -> list[str]:
    return [str(p) for p in root.rglob("*") if p.name == "__pycache__" or p.suffix in (".pyc", ".pyo")]


def git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


class Fixture:
    """A fake installed release and a throwaway Syndicate Git checkout, built once.

    Paths contain spaces; the release is reached through `current` and a
    launcher symlink, as the installed shim does."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="syndicate boundary ")
        self.base = Path(self.tmp.name).resolve()
        data = self.base / "data home" / "syndicate"
        self.release = data / "releases" / COMMIT
        copy_runtime(self.release)
        (self.release / "RELEASE.json").write_text(json.dumps(
            {"format": 1, "commit": COMMIT, "files": {}, "python_min": "3.11"}))
        (data / "current").symlink_to(Path("releases") / COMMIT)
        (data / "activations.jsonl").write_text("\n".join([
            json.dumps({"at": "2026-01-01T00:00:00Z", "commit": COMMIT, "tag": "v0.1.0"}),
            "{not json",
            json.dumps({"at": "2026-02-01T00:00:00Z", "commit": OTHER, "tag": "v0.2.0"}),
            json.dumps({"at": "2026-03-01T00:00:00Z", "commit": COMMIT, "tag": "v0.1.1",
                        "previous_commit": OTHER}),
        ]) + "\n")
        self.shim = self.base / "local bin" / "syndicate"
        self.shim.parent.mkdir()
        self.shim.symlink_to(data / "current" / "bin" / "syndicate")
        self.data = data
        # A Syndicate checkout with history, for dev mode.
        self.repo = self.base / "dev checkout"
        copy_runtime(self.repo)
        for path in self.repo.rglob("*"):
            if path.is_file() and not path.is_symlink():
                path.chmod(path.stat().st_mode | 0o200)
        (self.repo / ".gitignore").write_text("ignored.txt\n__pycache__/\n")
        git(self.repo, "init", "-q", "-b", "feature")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "fixture")
        self.repo_commit = git(self.repo, "rev-parse", "HEAD")

    def cleanup(self):
        for path in self.base.rglob("*"):
            if not path.is_symlink():
                try:
                    path.chmod(path.stat().st_mode | 0o700 if path.is_dir() else path.stat().st_mode | 0o600)
                except OSError:
                    pass
        self.tmp.cleanup()


class SubprocessCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = Fixture()

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self._scratch = tempfile.TemporaryDirectory(prefix="syndicate env ")
        self.addCleanup(self._scratch.cleanup)
        self.scratch = Path(self._scratch.name).resolve()
        self.env = disposable_env(self.scratch)
        self.foreign = self.scratch / "foreign cwd"
        self.foreign.mkdir()

    def run_cli(self, *argv, launcher=None, python_flags=("-B",), env=None, cwd=None, stdin=None):
        launcher = launcher or self.fixture.shim
        return subprocess.run([sys.executable, *python_flags, str(launcher), *argv],
                              cwd=cwd or self.foreign, env=env or self.env, capture_output=True,
                              text=True, input=stdin, timeout=120)


class Pinning(ProjectCase):
    def prefix(self):
        return shlex.join([identity.interpreter(), "-B", str(sy.SCRIPT)])

    def test_pinned_argv_is_absolute_interpreter_dash_b_real_launcher(self):
        argv = identity.pinned_argv()
        self.assertTrue(os.path.isabs(argv[0]))
        self.assertEqual(argv[1:], ["-B", str(Path(sy.SCRIPT).resolve())])
        self.assertEqual(shlex.split(identity.pinned_prefix()), argv)
        self.assertEqual(identity.LAUNCHER, sy.SCRIPT)

    def test_prefix_quotes_paths_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix="with space ") as tmp:
            launcher = Path(tmp) / "bin dir" / "syndicate"
            launcher.parent.mkdir()
            launcher.write_text("")
            with patch.object(identity, "LAUNCHER", launcher), \
                 patch.object(identity.sys, "executable", "/opt/py thon/bin/python3"):
                prefix = identity.pinned_prefix()
                self.assertEqual(shlex.split(prefix), ["/opt/py thon/bin/python3", "-B", str(launcher)])
                self.assertEqual(shlex.split(identity.pinned_command("team", "run-role", "a b")),
                                 ["/opt/py thon/bin/python3", "-B", str(launcher), "team", "run-role", "a b"])
                self.assertEqual(sy.relay_allow_rule(), f"Bash({prefix} team run-role *)")

    def test_paths_with_a_closing_parenthesis(self):
        # A checkout or dev snapshot can live under a ')' path (the installer refuses
        # one). Shell commands stay correctly quoted; the Claude relay allow rule,
        # which cannot express ')', is omitted so relays ask for approval instead.
        self.config("code-reviewer = codex\n")
        with tempfile.TemporaryDirectory(prefix="paren) ") as tmp:
            launcher = Path(tmp) / "bin (x)" / "syndicate"
            launcher.parent.mkdir()
            launcher.write_text("")
            with patch.object(identity, "LAUNCHER", launcher), \
                 patch.object(identity.sys, "executable", "/opt/py)thon/bin/python3"):
                argv = [identity.interpreter(), "-B", str(launcher)]
                self.assertIsNone(sy.relay_allow_rule())
                team = sy.resolve_team(self.project)
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    built = sy.build_claude(team, headless=False, rest=[])
                self.assertIn("relays will ask for approval", err.getvalue())
                settings = json.loads(Path(built[built.index("--settings") + 1]).read_text())
                self.assertNotIn("permissions", settings)
                self.assertEqual(shlex.split(settings["statusLine"]["command"]), [*argv, "_claude-usage"])
                command = sy.relay_command(team.roles["code-reviewer"], self.project)
                self.assertEqual(shlex.split(command)[:5], [*argv, "team", "run-role"])
                agents = json.loads(built[built.index("--agents") + 1])
                self.assertIn(f"{command} <<'SYNDICATE_BRIEF'\n", agents["code-reviewer"]["prompt"])
                gate = Path(tmp) / "hooks (y)" / "require-review.sh"
                with patch.object(sy, "GATE_SCRIPT", gate), contextlib.redirect_stderr(io.StringIO()):
                    built = sy.build_claude(team, headless=False, rest=[])
                    hook = json.loads(Path(built[built.index("--settings") + 1]).read_text())
                self.assertEqual(shlex.split(hook["hooks"]["Stop"][0]["hooks"][0]["command"]), [str(gate)])

    def test_claude_relay_heredoc_and_allow_rule_are_byte_parity(self):
        self.config("code-reviewer = codex\n")
        team = sy.resolve_team(self.project)
        argv = sy.build_claude(team, headless=False, rest=[])
        agents = json.loads(argv[argv.index("--agents") + 1])
        settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
        rule = settings["permissions"]["allow"][0]
        self.assertTrue(rule.startswith("Bash(") and rule.endswith(" *)"))
        granted = rule[len("Bash("):-len("*)")]
        command = sy.relay_command(team.roles["code-reviewer"], self.project)
        self.assertTrue(command.startswith(granted), (command, granted))
        self.assertEqual(granted, self.prefix() + " team run-role ")
        heredoc = f"{command} <<'SYNDICATE_BRIEF'\n"
        self.assertIn(heredoc, agents["code-reviewer"]["prompt"])
        self.assertEqual(shlex.split(command)[:3], identity.pinned_argv())

    def test_codex_relay_tomls_use_pinned_prefix_in_per_run_directory(self):
        self.config("orchestrator = codex\ndefault = codex\ncode-reviewer = claude\n")
        argv = sy.build_codex(sy.resolve_team(self.project), headless=True, rest=["task"])
        gen = sy.generated(self.project) / "codex"
        self.assertEqual(gen.parent.parent, self.project / ".syndicate" / ".generated")
        self.assertRegex(gen.parent.name, sy.GENERATED_RUN.pattern)
        reviewer = tomllib.loads((gen / "code-reviewer.toml").read_text())
        self.assertIn(f"{self.prefix()} team run-role code-reviewer --project {shlex.quote(str(self.project))} -"
                      " <<'SYNDICATE_BRIEF'", reviewer["developer_instructions"])
        self.assertIn(f"agents.code-reviewer.config_file={sy.jstr(str(gen / 'code-reviewer.toml'))}", argv)

    def test_backoff_and_live_inbox_use_pinned_prefix(self):
        team = sy.resolve_team(self.project)
        body = sy.orchestrator_body(team)
        self.assertIn(f"`{self.prefix()} team run-role ROLE --project {shlex.quote(str(self.project))} "
                      "--backoff-from PROVIDER -`", body)
        self.assertIn(f"Syndicate executable prefix: {self.prefix()}\n", body)

    def test_bundled_session_commands_render_pinned_and_user_commands_stay(self):
        team = sy.resolve_team(self.project)
        body = sy.orchestrator_body(team)
        self.assertIn(f"`{self.prefix()} decisions list --json`", body)
        self.assertIn(f"{self.prefix()} tasks worktree TASK_ID -C SOURCE", body)
        self.assertNotRegex(body, r"(?m)(`|^\s*)syndicate (tasks|decisions|requests|messages|briefs) ")
        chief = sy.chief_instructions(self.project)
        self.assertIn(f"`{self.prefix()} briefing --json`", chief)
        self.assertIn("printed `syndicate show` command", chief)
        self.assertIn(f"Use this exact executable prefix for every Syndicate command: {self.prefix()}\n", chief)
        self.assertIn(".syndicate/status.json", body)  # paths are not commands

    def test_status_line_and_usage_probe_use_pinned_prefix(self):
        argv = sy.build_claude(sy.resolve_team(self.project), False, [])
        settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
        self.assertEqual(settings["statusLine"]["command"], f"{self.prefix()} _claude-usage")
        chief = sy.build_chief_of_staff("claude", self.project, False, "hi")
        chief_settings = json.loads(chief[chief.index("--settings") + 1])
        self.assertEqual(chief_settings["statusLine"]["command"], f"{self.prefix()} _claude-usage")
        probe = json.loads(runtime.claude_usage_probe_command()[-1])["statusLine"]["command"]
        self.assertEqual(shlex.split(probe)[:4], [*identity.pinned_argv(), "_claude-usage"])

    def test_review_gate_hook_resolves_from_release_root(self):
        argv = sy.build_claude(sy.resolve_team(self.project), False, [])
        settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
        hook = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
        self.assertEqual(shlex.split(hook), [str(identity.ROOT / "hooks" / "require-review.sh")])
        with patch.object(Path, "home", return_value=self.project / "home"), \
             patch.dict(os.environ, {"CODEX_HOME": str(self.project / "codex home")}), \
             contextlib.redirect_stdout(io.StringIO()):
            sy.cmd_setup_codex_gate(sy.argparse.Namespace(remove=False))
        hooks = json.loads((self.project / "codex home" / "hooks.json").read_text())
        self.assertEqual(hooks["hooks"]["Stop"][0]["hooks"][0]["command"], hook)
        self.assertFalse((self.project / "home" / ".codex").exists())

    def test_setup_codex_gate_honors_codex_home_like_the_installer(self):
        home, custom = self.project / "home", self.project / "custom codex"
        out = io.StringIO()
        with patch.object(Path, "home", return_value=home), contextlib.redirect_stdout(out):
            with patch.dict(os.environ, {"CODEX_HOME": str(custom)}):
                sy.cmd_setup_codex_gate(sy.argparse.Namespace(remove=False))
                self.assertEqual(sy.codex_home(), custom)
            with patch.dict(os.environ, {}):
                os.environ.pop("CODEX_HOME", None)
                self.assertEqual(sy.codex_home(), home / ".codex")
                sy.cmd_setup_codex_gate(sy.argparse.Namespace(remove=False))
            with patch.dict(os.environ, {"CODEX_HOME": str(custom)}):
                sy.cmd_setup_codex_gate(sy.argparse.Namespace(remove=True))
        self.assertIn(str(custom / "hooks.json"), out.getvalue())
        self.assertNotIn("Stop", json.loads((custom / "hooks.json").read_text())["hooks"])
        default = json.loads((home / ".codex" / "hooks.json").read_text())
        self.assertEqual(len(default["hooks"]["Stop"]), 1)

    def test_monitor_spawn_argv_and_environment(self):
        with patch.object(runtime.subprocess, "Popen") as popen:
            runtime.start_monitor(sy.SCRIPT)
        argv = popen.call_args.args[0]
        self.assertEqual(argv, [*identity.pinned_argv(), "_limits-monitor"])
        self.assertEqual(popen.call_args.kwargs["env"]["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_channel_mcp_argv(self):
        server = live.channel_server(self.project / "live.json")
        self.assertEqual(server["command"], identity.interpreter())
        self.assertEqual(server["args"], ["-B", str((Path(sy.SCRIPT).parent / "syndicate_live.py").resolve()),
                                          "channel", str(self.project / "live.json")])
        self.assertNotIn("env", server)

    def test_tmux_on_wraps_pinned_argv_and_off_keeps_the_same_instructions(self):
        started = []
        with patch.object(sy.shutil, "which", return_value="/usr/bin/tmux"), \
             patch.object(sy, "_release_replaced_windows"), \
             patch.object(sy, "_start_tmux_window", side_effect=lambda d, n, c, **k: started.append(c) or "@1"), \
             patch.object(sy.sys.stdin, "isatty", return_value=False), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sy.launch_tmux(["team", "task", "do it now"], self.project, "w"), 0)
        self.assertEqual(shlex.split(started[0]),
                         [*identity.pinned_argv(), "_in-tmux", "team", "task", "do it now"])
        self.assertIn(f"run-shell {shlex.quote(shlex.join([*identity.pinned_argv(), '_restore-view']))}",
                      sy._view_hook())
        flags = sy._tmux_env_flags()
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", flags)
        team = sy.resolve_team(self.project)
        with patch.object(sy, "tmux_enabled", return_value=True):
            on = sy.orchestrator_body(team)
        off = sy.orchestrator_body(team)
        self.assertEqual(on, off)

    def test_config_precedence_unchanged_from_foreign_cwd(self):
        os.chdir(Path(self._tmp.name) / ".syndicate")
        with patch.dict(os.environ, {"SYNDICATE_RUNTIME": "codex"}):
            team = sy.resolve_team(self.project)
            self.assertEqual(team.config["default"], "codex")
            self.assertEqual(team.config_sources["default"], "environment")
            self.user_config("default = claude\n")
            self.assertEqual(sy.resolve_team(self.project).config["default"], "claude")
            self.config("default = codex\nreview-gate = off\n")
            team = sy.resolve_team(self.project)
        self.assertEqual((team.config["default"], team.config_sources["default"]), ("codex", "project"))
        self.assertEqual(team.config_sources["tmux"], "default")
        self.assertEqual(team.roles["orchestrator"].sources[0], "global")

    def test_resources_resolve_from_root_and_local_overrides_still_win(self):
        root = Path(sy.SCRIPT).parent.parent
        for path in (sy.GLOBAL_ROLES, sy.WEB_DIR, sy.CHIEF_ROLE, sy.GATE_SCRIPT, sy.INBOX_SKILL,
                     sy.PROJECT_SKILL, sy.CLOSEOUT_SKILL, sy.HANDOFF_SKILL):
            self.assertTrue(path.exists(), path)
            self.assertEqual(root, path.parents[len(path.relative_to(root).parts) - 1])
        self.role_file("code-reviewer.md", "---\ndescription: local reviewer\n---\nLocal body.\n")
        roles, _ = sy.load_roles(self.project)
        self.assertEqual(roles["code-reviewer"].body, "Local body.")


class GeneratedSettings(ProjectCase):
    def test_runs_do_not_overwrite_each_others_files(self):
        older = self.project / ".syndicate" / ".generated" / "20200101T000000-1-deadbeef"
        older.mkdir(parents=True)
        (older / "claude-settings.json").write_text("older session")
        legacy = self.project / ".syndicate" / ".generated" / "claude-settings.json"
        legacy.write_text("legacy session")
        argv = sy.build_claude(sy.resolve_team(self.project), False, [])
        path = Path(argv[argv.index("--settings") + 1])
        self.assertEqual(path.parent, sy.generated(self.project))
        self.assertNotEqual(path.parent, older)
        self.assertEqual(legacy.read_text(), "legacy session")
        owner = json.loads((path.parent / "owner.json").read_text())
        self.assertEqual(owner["pid"], os.getpid())
        self.assertIn(owner["release"]["mode"], ("checkout", "release", "dev"))

    def test_prune_removes_only_old_directories_of_gone_owners(self):
        root = self.project / ".syndicate" / ".generated"
        dead = subprocess.Popen([sys.executable, "-B", "-c", ""])
        dead.wait()
        names = {"gone": f"20200101T000000-{dead.pid}-00000001",
                 "alive": f"20200101T000000-{os.getpid()}-00000002",
                 "recent": f"20200101T000000-{dead.pid}-00000003",
                 "other": "not-a-run"}
        for key, name in names.items():
            (root / name).mkdir(parents=True)
            (root / name / "f").write_text(key)
            if key != "recent":
                old = time.time() - sy.GENERATED_MIN_AGE - 60
                os.utime(root / name, (old, old))
        current = sy.generated(self.project)
        self.assertFalse((root / names["gone"]).exists())
        for key in ("alive", "recent", "other"):
            self.assertTrue((root / names[key]).exists(), key)
        self.assertTrue(current.is_dir())


    def old_run(self, name: str, owner: dict | None = None) -> Path:
        path = self.project / ".syndicate" / ".generated" / name
        path.mkdir(parents=True)
        if owner is not None:
            (path / "owner.json").write_text(json.dumps(owner))
        old = time.time() - sy.GENERATED_MIN_AGE - 60
        os.utime(path, (old, old))
        return path

    def dead_pid(self) -> int:
        dead = subprocess.Popen([sys.executable, "-B", "-c", ""])
        dead.wait()
        return dead.pid

    def test_cli_child_recorded_in_owner_keeps_directory_live(self):
        child = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        current = sy.generated(self.project)
        sy.record_cli_owner(self.project, None, child.pid)
        owner = json.loads((current / "owner.json").read_text())
        self.assertEqual((owner["pid"], owner["child_pids"]), (os.getpid(), [child.pid]))
        sy.record_cli_owner(self.project, None, child.pid)  # idempotent
        self.assertEqual(json.loads((current / "owner.json").read_text())["child_pids"], [child.pid])
        gone = self.dead_pid()
        live_child = self.old_run(f"20200101T000000-{gone}-00000001", {"pid": gone, "child_pids": [child.pid]})
        all_gone = self.old_run(f"20200101T000000-{gone}-00000002", {"pid": gone, "child_pids": [gone]})
        sy.prune_generated(self.project / ".syndicate" / ".generated", keep=current)
        self.assertTrue(live_child.is_dir())
        self.assertFalse(all_gone.exists())
        child.kill()
        child.wait()
        sy.prune_generated(self.project / ".syndicate" / ".generated", keep=current)
        self.assertFalse(live_child.exists())
        self.assertTrue(current.is_dir())

    def test_prune_skips_while_another_process_holds_the_session_lock(self):
        import fcntl
        gone = self.dead_pid()
        stale = self.old_run(f"20200101T000000-{gone}-00000003", {"pid": gone})
        root = self.project / ".syndicate" / ".generated"
        with open(sy.project_lock_path(self.project), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with patch.object(sy, "_generation", None):
                fresh = sy.generated(self.project)  # a second launch while the lock is held
            self.assertTrue(stale.is_dir())
            # The lock holder itself (a supervisor pruning its own project) still prunes.
            with patch.object(sy, "_held_session_locks", {str(self.project)}):
                sy.prune_generated(root, keep=fresh)
            self.assertFalse(stale.exists())
        stale = self.old_run(f"20200101T000000-{gone}-00000004", {"pid": gone})
        sy.prune_generated(root, keep=fresh)
        self.assertFalse(stale.exists())


class ReleaseInUseMarkers(ProjectCase):
    def setUp(self):
        super().setUp()
        self.store = self.project / "store" / "syndicate"
        self.release = self.store / "releases" / COMMIT
        self.release.mkdir(parents=True)
        self.addCleanup(sy._release_markers.clear)
        fake = {"mode": "release", "commit": COMMIT}
        for target in (patch.object(identity, "ROOT", self.release),
                       patch.object(identity, "identity", return_value=fake),
                       patch.object(sy.atexit, "register")):
            target.start()
            self.addCleanup(target.stop)

    def test_launcher_and_cli_child_markers_follow_their_processes(self):
        child = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        sy.mark_release_in_use()
        sy.mark_release_in_use()  # once per PID
        sy.record_cli_owner(self.project, None, child.pid)
        markers = self.store / "in-use" / COMMIT
        self.assertEqual(sorted(p.name for p in markers.iterdir()), sorted([str(os.getpid()), str(child.pid)]))
        self.assertEqual(json.loads((markers / str(child.pid)).read_text())["launcher_pid"], os.getpid())
        self.assertEqual(sy.atexit.register.call_count, 1)
        sy.clear_release_markers()  # the live child keeps its marker
        self.assertEqual([p.name for p in markers.iterdir()], [str(child.pid)])
        child.kill()
        child.wait()
        sy.clear_release_markers()
        self.assertFalse((self.store / "in-use").exists())
        self.assertEqual(sy._release_markers, [])

    def test_no_markers_outside_an_installed_release(self):
        with patch.object(identity, "identity", return_value={"mode": "checkout", "commit": COMMIT}):
            sy.mark_release_in_use()
        extracted = self.project / "extracted"
        with patch.object(identity, "ROOT", extracted):
            sy.mark_release_in_use()
        self.assertFalse((self.store / "in-use").exists())
        self.assertFalse((self.project / "in-use").exists())
        self.assertEqual(sy._release_markers, [])

    def test_unwritable_store_warns_and_does_not_stop_the_launch(self):
        (self.store / "in-use").write_text("not a directory")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            sy.mark_release_in_use()
        self.assertIn("could not write release in-use marker", err.getvalue())
        self.assertEqual(sy._release_markers, [])


class RunIdentity(ProjectCase):
    def test_run_record_carries_release_identity_and_older_records_still_read(self):
        runtime.write_run_record(self.project, "task", "claude", 10, None)
        record = json.loads((self.project / ".syndicate" / "run.json").read_text())
        self.assertEqual(record["release"]["root"], str(identity.ROOT))
        self.assertEqual(record["release"]["interpreter"], identity.interpreter())
        self.assertIn(record["release"]["mode"], ("checkout", "release", "dev"))
        del record["release"]
        runtime.atomic_json(self.project / ".syndicate" / "run.json", record)
        state, loaded = sy.read_run_record(self.project)
        self.assertEqual(state, "valid")
        self.assertNotIn("release", loaded)

    def test_monitor_state_records_its_release(self):
        # No provider probe runs: codex and notify-send are reported absent.
        with patch.object(runtime, "MONITOR_RELEASE", {"mode": "release", "commit": COMMIT}), \
             patch.object(runtime.shutil, "which", return_value=None):
            runtime.monitor_tick()
        state = runtime.read_json(runtime.STATE / "monitor.json")
        self.assertEqual(state["release"], {"mode": "release", "commit": COMMIT})


class DevGuard(ProjectCase):
    def test_dev_mode_refuses_production_state(self):
        prod = self.project / "prod"
        env = {identity.DEV_ENV: "1", identity.DEV_PRODUCTION_HOME_ENV: str(prod),
               "TMUX_TMPDIR": str(self.project / "tmux")}
        env.pop("TMUX", None)
        with patch.dict(os.environ, env), patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TMUX", None)
            with patch.object(sy, "REGISTRY_PATH", prod / ".syndicate" / "projects.json"), \
                 patch.object(sy, "TMUX_SESSION", "syndicate-dev-x"):
                problems = sy.dev_guard()
            self.assertTrue(any("production Syndicate state" in p for p in problems), problems)
            with patch.object(sy, "TMUX_SESSION", "the-syndicate"):
                self.assertTrue(any("the-syndicate" in p for p in sy.dev_guard()))
            with patch.object(sy, "TMUX_SESSION", "syndicate-dev-x"), \
                 patch.object(runtime, "STATE", self.project / "dev" / "limits"), \
                 patch.object(sy.syndicate_worktrees, "STATE_DIR", self.project / "dev"):
                self.assertEqual(sy.dev_guard(), [])
                os.environ["TMUX"] = "/tmp/tmux-1000/default,1,0"
                self.assertTrue(any("TMUX points" in p for p in sy.dev_guard()))
                del os.environ["TMUX"]
            registry = prod / ".syndicate" / "projects.json"
            registry.parent.mkdir(parents=True)
            target = self.project / "target"
            registry.write_text(json.dumps({"projects": [{"path": str(target)}]}))
            with self.assertRaisesRegex(sy.SyndicateError, "production project"):
                sy.register_project(target)
            sy.register_project(self.project / "elsewhere")
        self.assertEqual(sy.dev_guard(), [])  # outside dev mode nothing is checked

    def test_dev_target_check_covers_worktrees_subdirectories_and_state(self):
        prod = self.project / "prod home"
        registered = self.project / "registered repo"
        registered.mkdir()
        git(registered, "init", "-q", "-b", "main")
        git(registered, "commit", "-q", "--allow-empty", "-m", "init")
        (registered / "sub" / "dir").mkdir(parents=True)
        linked = self.project / "linked task worktree"
        git(registered, "worktree", "add", "-q", "-b", "work/t", str(linked))
        (linked / "deep").mkdir()
        other = self.project / "other repo"
        other.mkdir()
        git(other, "init", "-q")
        plain = self.project / "plain dir"
        plain.mkdir()
        registry = prod / ".syndicate" / "projects.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(json.dumps({"projects": [{"path": str(registered), "name": "registered"}]}))
        with patch.dict(os.environ, {identity.DEV_PRODUCTION_HOME_ENV: str(prod)}):
            for target in (registered, registered / "sub" / "dir", registered / "sub" / "missing",
                           linked, linked / "deep", prod / ".syndicate" / "worktrees" / "x",
                           prod / ".config" / "syndicate"):
                self.assertIsNotNone(identity.dev_target_violation(target), target)
            self.assertIn("shares the Git repository", identity.dev_target_violation(linked / "deep"))
            for target in (other, plain, self.project / "not yet created"):
                self.assertIsNone(identity.dev_target_violation(target), target)
            # Fail closed: an unreadable registry, or Git that cannot run.
            registry.write_text("{not json")
            self.assertIn("not valid JSON", identity.dev_target_violation(other))
            registry.write_text(json.dumps({"projects": []}))
            with patch.object(identity.subprocess, "run", side_effect=OSError("no git")):
                self.assertIn("cannot run git", identity.dev_target_violation(other))
            # The launcher's resolution helpers route through the same check.
            registry.write_text(json.dumps({"projects": [{"path": str(registered)}]}))
            with patch.dict(os.environ, {identity.DEV_ENV: "1"}):
                for call in (lambda: sy.execution_project(str(linked)), lambda: sy.resolve_project(str(linked / "deep")),
                             lambda: sy.register_workspace(registered / "sub"),
                             lambda: sy.dev_check_args(sy.argparse.Namespace(dir=str(linked), repo=None)),
                             lambda: sy.dev_check_args(sy.argparse.Namespace(worktree=str(linked)))):
                    with self.assertRaisesRegex(sy.SyndicateError, "dev mode refuses"):
                        call()
                self.assertEqual(sy.execution_project(str(other)), other)
            self.assertEqual(sy.execution_project(str(linked)), linked)  # outside dev mode: unchanged

    def test_dev_environment_is_isolated_and_namespaced(self):
        base = {"HOME": "/home/real", "TMUX": "/tmp/tmux-1/default,1,0", "TMUX_PANE": "%1",
                "SYNDICATE_RUNTIME": "codex", "SYNDICATE_RUN_ID": "x", "PATH": "/usr/bin",
                "CODEX_HOME": "/home/real/.codex"}
        home, root = self.project / "h", self.project / "r"
        state = {"commit": COMMIT, "branch": "feature", "dirty": True}
        env = dev.dev_environment(base, home, root, state, self.project, "syndicate-dev-1", False)
        self.assertNotIn("TMUX", env)
        self.assertNotIn("TMUX_PANE", env)
        self.assertNotIn("SYNDICATE_RUNTIME", env)
        self.assertNotIn("SYNDICATE_RUN_ID", env)
        self.assertEqual(env["HOME"], str(home))
        self.assertEqual(env["CODEX_HOME"], str(home / ".codex"))
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(home / ".claude"))
        self.assertEqual(env["TMUX_TMPDIR"], str(root / "tmux"))
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(env[identity.DEV_TMUX_SESSION_ENV], "syndicate-dev-1")
        self.assertEqual(env[identity.DEV_PRODUCTION_HOME_ENV], "/home/real")
        self.assertEqual((env[identity.DEV_COMMIT_ENV], env[identity.DEV_DIRTY_ENV]), (COMMIT, "1"))
        self.assertEqual(env["PATH"].split(os.pathsep), [str(root / "bin"), "/usr/bin"])
        shared = dev.dev_environment(base, home, root, state, self.project, "s", True)
        self.assertEqual(shared["CODEX_HOME"], "/home/real/.codex")
        self.assertEqual(shared["CLAUDE_CONFIG_DIR"], "/home/real/.claude")


class Version(SubprocessCase):
    @unittest.skipUnless(CHECKOUT_IS_GIT, NOT_GIT_REASON)
    def test_checkout_version_is_side_effect_free(self):
        before_home = tree(self.scratch)
        before_git = git(CHECKOUT, "status", "--porcelain", "--ignored")
        result = self.run_cli("--version", launcher=CHECKOUT / "bin" / "syndicate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mode: checkout\n", result.stdout)
        self.assertIn(f"commit: {git(CHECKOUT, 'rev-parse', 'HEAD')}", result.stdout)
        self.assertIn(f"interpreter: {os.path.abspath(sys.executable)}\n", result.stdout)
        self.assertIn(f"root: {CHECKOUT}\n", result.stdout)
        self.assertEqual(tree(self.scratch), before_home)
        self.assertEqual(git(CHECKOUT, "status", "--porcelain", "--ignored"), before_git)

    def test_release_version_through_current_symlink_from_foreign_cwd(self):
        before = tree(self.scratch)
        before_release = tree(self.fixture.release)
        result = self.run_cli("version", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.assertEqual(info["mode"], "release")
        self.assertEqual(info["commit"], COMMIT)
        self.assertEqual(info["tag"], "v0.1.1")  # latest activation of this commit
        self.assertEqual(info["activation"]["previous_commit"], OTHER)
        self.assertEqual(info["root"], str(self.fixture.release))
        self.assertNotIn("current", Path(info["root"]).parts)
        self.assertEqual(info["interpreter"], os.path.abspath(sys.executable))
        text = self.run_cli("--version").stdout
        self.assertIn("mode: release\n", text)
        self.assertIn("tag: v0.1.1\n", text)
        self.assertEqual(tree(self.scratch), before)
        self.assertEqual(tree(self.fixture.release), before_release)

    def test_non_git_tree_reports_checkout_with_unknown_commit(self):
        plain = self.scratch / "plain tree"
        copy_runtime(plain)
        env = dict(self.env, GIT_CEILING_DIRECTORIES=str(self.scratch))
        before = tree(self.scratch)
        result = self.run_cli("--version", "--json", launcher=plain / "bin" / "syndicate", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.assertEqual((info["mode"], info["commit"], info["root"]), ("checkout", None, str(plain)))
        for key in ("error", "install_root", "tag"):
            self.assertNotIn(key, info)
        self.assertEqual((info["branch"], info["dirty"]), (None, None))
        text = self.run_cli("--version", launcher=plain / "bin" / "syndicate", env=env)
        self.assertIn("syndicate unknown\nmode: checkout\n", text.stdout)
        self.assertEqual(tree(self.scratch), before)
        self.assertEqual(caches(plain), [])

    def test_release_reports_install_root_current_and_manifest_mismatch(self):
        info = json.loads(self.run_cli("--version", "--json").stdout)
        self.assertEqual((info["install_root"], info["current"]), (str(self.fixture.data), True))
        self.assertNotIn("error", info)
        # An installed release that is not current, whose manifest names another commit.
        store = self.scratch / "other store" / "syndicate"
        release = store / "releases" / OTHER
        copy_runtime(release)
        release.chmod(0o700)
        (release / "RELEASE.json").write_text(json.dumps(
            {"format": 1, "commit": COMMIT, "files": {}, "python_min": "3.11"}))
        result = self.run_cli("--version", "--json", launcher=release / "bin" / "syndicate")
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.assertEqual((info["mode"], info["install_root"], info["current"]), ("release", str(store), False))
        self.assertIn("not its release directory", info["error"])
        text = self.run_cli("--version", launcher=release / "bin" / "syndicate").stdout
        self.assertIn(f"install root: {store} (not the current release)\n", text)
        (release / "RELEASE.json").write_text(json.dumps({"commit": OTHER}))
        info = json.loads(self.run_cli("--version", "--json", launcher=release / "bin" / "syndicate").stdout)
        self.assertEqual(info["commit"], OTHER)
        self.assertIn("malformed", info["error"])

    def test_release_dashboard_marks_its_release_in_use_until_exit(self):
        markers = self.fixture.data / "in-use"
        env = dict(self.env, PYTHONUNBUFFERED="1")
        proc = subprocess.Popen([sys.executable, "-B", str(self.fixture.shim), "web", "--no-monitor", "--port", "0"],
                                cwd=self.foreign, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            line = proc.stdout.readline()
            self.assertIn("http://127.0.0.1:", line, proc.stderr.read() if proc.poll() is not None else line)
            self.assertEqual([p.name for p in (markers / COMMIT).iterdir()], [str(proc.pid)])
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            proc.stderr.close()
        self.assertFalse(markers.exists())

    def test_release_version_tolerates_missing_activation_log(self):
        log = self.fixture.data / "activations.jsonl"
        saved = log.read_text()
        log.unlink()
        try:
            info = json.loads(self.run_cli("--version", "--json").stdout)
        finally:
            log.write_text(saved)
        self.assertEqual((info["mode"], info["commit"]), ("release", COMMIT))
        self.assertNotIn("tag", info)

    def test_release_resources_and_prefix_are_pinned_to_real_release(self):
        code = ("import importlib.machinery, importlib.util, json, sys\n"
                "loader = importlib.machinery.SourceFileLoader('syndicate', sys.argv[1])\n"
                "spec = importlib.util.spec_from_loader('syndicate', loader)\n"
                "sy = importlib.util.module_from_spec(spec); loader.exec_module(sy)\n"
                "print(json.dumps(dict(roles=str(sy.GLOBAL_ROLES), web=str(sy.WEB_DIR), chief=str(sy.CHIEF_ROLE),"
                " gate=str(sy.GATE_SCRIPT), prefix=sy.identity.pinned_prefix(), rule=sy.relay_allow_rule(),"
                " live=sy.live.channel_server('/x')['args'][1])))\n")
        result = subprocess.run([sys.executable, "-B", "-c", code, str(self.fixture.shim)], cwd=self.foreign,
                                env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        paths = json.loads(result.stdout)
        release = self.fixture.release
        self.assertEqual(paths["roles"], str(release / "roles"))
        self.assertEqual(paths["web"], str(release / "web"))
        self.assertEqual(paths["chief"], str(release / "portfolio" / "chief-of-staff.md"))
        self.assertEqual(paths["gate"], str(release / "hooks" / "require-review.sh"))
        self.assertEqual(paths["live"], str(release / "bin" / "syndicate_live.py"))
        self.assertEqual(shlex.split(paths["prefix"]),
                         [os.path.abspath(sys.executable), "-B", str(release / "bin" / "syndicate")])
        self.assertIn(" ", paths["prefix"])
        self.assertEqual(paths["rule"], f"Bash({paths['prefix']} team run-role *)")


class NoBytecode(SubprocessCase):
    def test_entry_paths_create_no_caches(self):
        release = self.fixture.release
        # Legacy direct entry without -B and without PYTHONDONTWRITEBYTECODE.
        for launcher in (self.fixture.shim, CHECKOUT / "bin" / "syndicate"):
            result = self.run_cli("--version", launcher=launcher, python_flags=())
            self.assertEqual(result.returncode, 0, result.stderr)
        # Status line path, as Claude runs it.
        usage = subprocess.run(shlex.split(f"{shlex.quote(sys.executable)} -B "
                                           f"{shlex.quote(str(self.fixture.shim))} _claude-usage"),
                               input="{}", cwd=self.foreign, env=self.env, capture_output=True, text=True,
                               timeout=60)
        self.assertEqual(usage.returncode, 0, usage.stderr)
        # Channel MCP path: the endpoint is inactive, so the server exits at end of input.
        endpoint = self.scratch / "live.json"
        endpoint.write_text(json.dumps({"active": False}))
        channel = subprocess.run([sys.executable, str(release / "bin" / "syndicate_live.py"), "channel",
                                  str(endpoint)], input="", cwd=self.foreign, env=self.env,
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(channel.returncode, 0, channel.stderr)
        # Relay path: argument validation runs after every import.
        relay = self.run_cli("team", "run-role", "no-such-role", "--project", str(self.foreign), "-",
                             stdin="brief")
        self.assertNotEqual(relay.returncode, 0)
        self.assertEqual(caches(release), [])
        self.assertEqual(caches(CHECKOUT / "bin"), [])
        self.assertEqual(caches(self.scratch), [])


class DevMode(SubprocessCase):
    def test_snapshot_is_isolated_from_later_edits_and_excludes_git_and_ignored(self):
        repo = self.fixture.repo
        (repo / "untracked.md").write_text("untracked")
        (repo / "ignored.txt").write_text("ignored")
        (repo / "bin" / "__pycache__").mkdir(exist_ok=True)
        (repo / "bin" / "__pycache__" / "x.pyc").write_bytes(b"")
        role = repo / "roles" / "orchestrator.md"
        original = role.read_text()
        target = self.scratch / "snapshot"
        try:
            dev.stage_snapshot(repo, target)
            role.write_text(original + "\nEDITED AFTER STAGING\n")
            self.assertEqual((target / "roles" / "orchestrator.md").read_text(), original)
        finally:
            role.write_text(original)
            (repo / "untracked.md").unlink()
            (repo / "ignored.txt").unlink()
            shutil.rmtree(repo / "bin" / "__pycache__")
        self.assertTrue((target / "untracked.md").is_file())
        self.assertFalse((target / "ignored.txt").exists())
        self.assertFalse((target / ".git").exists())
        self.assertEqual(caches(target), [])
        self.assertFalse(os.access(target / "roles" / "orchestrator.md", os.W_OK))
        with self.assertRaisesRegex(dev.DevError, "not a Syndicate checkout"):
            dev.validate_checkout(self.scratch)

    def test_dev_runs_snapshot_in_disposable_environment(self):
        repo = self.fixture.repo
        (repo / "README.md").write_text((repo / "README.md").read_text() + "\ndirty\n")
        try:
            result = self.run_cli("dev", "--checkout", str(repo), "--", "--version", "--json",
                                  launcher=CHECKOUT / "bin" / "syndicate")
            tasks = self.run_cli("dev", "--checkout", str(repo), "--", "tasks", "list", "--json",
                                 launcher=CHECKOUT / "bin" / "syndicate")
        finally:
            git(repo, "checkout", "--", "README.md")
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.assertEqual(info["mode"], "dev")
        self.assertEqual(info["commit"], self.fixture.repo_commit)
        self.assertTrue(info["dirty"])
        self.assertEqual(info["checkout"], str(repo))
        self.assertTrue(Path(info["root"]).name == "checkout" and "syndicate-dev-" in info["root"])
        self.assertIn(f"branch feature, commit {self.fixture.repo_commit}, dirty", result.stderr)
        self.assertFalse(Path(info["root"]).exists())  # removed after exit without --keep
        self.assertEqual(tasks.returncode, 0, tasks.stderr)
        self.assertEqual(json.loads(tasks.stdout), [])
        # Nothing reached the caller's homes; only the temporary dev roots were used and removed.
        for name in ("home", "config", "data", "cache", "codex", "claude", "tmux", "tmp"):
            self.assertEqual(list((self.scratch / name).iterdir()), [], name)
        self.assertEqual(caches(repo), [])

    def test_bare_syndicate_in_a_dev_run_reaches_the_snapshot(self):
        devroot = self.scratch / "devroot"
        production_bin = self.scratch / "production bin"
        production_bin.mkdir()
        fake = production_bin / "syndicate"
        fake.write_text("#!/bin/sh\necho PRODUCTION RELEASE\n")
        fake.chmod(0o755)
        snapshot = self.scratch / "snap shot"
        copy_runtime(snapshot)
        wrapper = dev.write_wrapper(devroot, snapshot / "bin" / "syndicate")
        self.assertEqual(wrapper, devroot / "bin" / "syndicate")
        self.assertTrue(os.access(wrapper, os.X_OK))
        base = dict(self.env, PATH=os.pathsep.join([str(production_bin), self.env.get("PATH", "")]))
        env = dev.dev_environment(base, self.scratch / "home", devroot, {"commit": COMMIT}, self.fixture.repo,
                                  "syndicate-dev-w", False)
        result = subprocess.run(["syndicate", "--version", "--json"], env=env, cwd=self.foreign,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.assertEqual((info["mode"], info["root"]), ("dev", str(snapshot)))
        self.assertEqual(shutil.which("syndicate", path=env["PATH"]), str(wrapper))
        self.assertEqual(caches(snapshot), [])

    def test_dev_option_without_separator_explains_the_separator(self):
        result = self.run_cli("dev", "--checkout", str(self.fixture.repo), "--version",
                              launcher=CHECKOUT / "bin" / "syndicate")
        self.assertEqual(result.returncode, 2)
        self.assertIn("go after --", result.stderr)
        self.assertIn(f"-- --version", result.stderr)

    def dev_env(self, production: Path) -> dict:
        return dict(self.env, **{identity.DEV_ENV: "1", identity.DEV_PRODUCTION_HOME_ENV: str(production),
                                 identity.DEV_TMUX_SESSION_ENV: "syndicate-dev-g"})

    def test_dev_commands_refuse_production_projects_worktrees_and_subdirectories(self):
        production = self.scratch / "production home"
        registered = self.scratch / "registered repo"
        registered.mkdir()
        git(registered, "init", "-q", "-b", "main")
        git(registered, "commit", "-q", "--allow-empty", "-m", "init")
        (registered / "sub").mkdir()
        linked = self.scratch / "linked worktree"
        git(registered, "worktree", "add", "-q", "-b", "work/t", str(linked))
        registry = production / ".syndicate" / "projects.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(json.dumps({"projects": [{"path": str(registered), "name": "registered"}]}))
        env = self.dev_env(production)
        attempts = {
            "tasks worktree -C": ("tasks", "worktree", "t1", "-C", str(linked), "--branch", "work/x"),
            "tasks worktree --repo": ("tasks", "worktree", "t1", "--repo", str(registered / "sub"), "--branch", "work/x"),
            "tasks cleanup-pr --worktree": ("tasks", "cleanup-pr", "1", "-C", str(self.foreign), "--branch", "work/t",
                                            "--worktree", str(linked)),
            "team run-role --project": ("team", "run-role", "backend-engineer", "--project", str(registered / "sub"), "-"),
            "team task --project": ("team", "task", "--project", str(linked), "go"),
            "requests send -C": ("requests", "send", "m", "--key", "k", "-C", str(linked)),
            "decisions request -C": ("decisions", "request", "q", "--key", "k", "-C", str(registered)),
            "messages send -C": ("messages", "send", "m", "--key", "k", "--session", "s", "-C", str(linked)),
            "live status -C": ("live", "status", "-C", str(registered / "sub")),
            "team init": ("team", "init", "--project", str(linked)),
        }
        for name, argv in attempts.items():
            result = self.run_cli(*argv, env=env, stdin="brief")
            self.assertEqual(result.returncode, 1, (name, result.stdout, result.stderr))
            self.assertIn("dev mode refuses this target", result.stderr, name)
        # The default -C . is checked too: a dev command run from inside the linked worktree.
        result = self.run_cli("requests", "send", "m", "--key", "k", env=env, cwd=linked / ".")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("dev mode refuses this target", result.stderr)
        for path in (registered, linked):
            self.assertFalse((path / ".syndicate").exists(), path)
        self.assertEqual(json.loads(registry.read_text())["projects"][0]["path"], str(registered))
        ok = self.run_cli("tasks", "list", "--json", env=env)
        self.assertEqual(ok.returncode, 0, ok.stderr)

    def test_dev_child_refuses_when_isolation_is_missing(self):
        env = dict(self.env, **{identity.DEV_ENV: "1", identity.DEV_PRODUCTION_HOME_ENV: self.env["HOME"],
                                identity.DEV_TMUX_SESSION_ENV: "syndicate-dev-t"})
        result = self.run_cli("tasks", "list", env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("dev mode refuses to touch production state", result.stderr)
        self.assertEqual(list((self.scratch / "home").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
