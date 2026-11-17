import argparse
import contextlib
import io
import http.client
import http.server
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

sys.dont_write_bytecode = True
BIN = Path(__file__).resolve().parent.parent / "bin" / "syndicate"
loader = importlib.machinery.SourceFileLoader("syndicate", str(BIN))
spec = importlib.util.spec_from_loader("syndicate", loader)
sy = importlib.util.module_from_spec(spec)
loader.exec_module(sy)


class ProjectCase(unittest.TestCase):
    def setUp(self):
        tmux_patch = unittest.mock.patch.object(sy, 'tmux_enabled', return_value=False)
        tmux_patch.start()
        self.addCleanup(tmux_patch.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name).resolve()
        # Launch arguments default to '.', so a test must never run from the real
        # checkout: an earlier generic-task test wrote tasks (PID 2 from a sandbox,
        # project_path /tmp/.../generic-tasks) into the repo's own status.json.
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.project)
        registry = unittest.mock.patch.object(sy, "REGISTRY_PATH", self.project / "registry" / "projects.json")
        registry.start()
        self.addCleanup(registry.stop)
        self.xdg = self.project / "xdg"
        env = unittest.mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.xdg)})
        env.start()
        self.addCleanup(env.stop)
        (self.project / ".syndicate" / "roles").mkdir(parents=True)

    def config(self, text):
        (self.project / ".syndicate" / "syndicate.conf").write_text(text)

    def user_config(self, text):
        path = sy.user_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def role_file(self, name, text):
        (self.project / ".syndicate" / "roles" / name).write_text(text)


class RoleResolution(ProjectCase):
    def test_global_roles_load_with_tools(self):
        team = sy.resolve_team(self.project)
        self.assertIn("orchestrator", team.roles)
        self.assertEqual(team.roles["code-reviewer"].sandbox, "read-only")
        self.assertEqual(team.roles["backend-engineer"].sandbox, "workspace-write")
        self.assertEqual(team.roles["backend-engineer"].sources, ["global"])

    def test_override_replaces_body_but_inherits_missing_frontmatter(self):
        self.role_file("code-reviewer.md", "---\nname: code-reviewer\n---\nCUSTOM BODY\n")
        role = sy.resolve_team(self.project).roles["code-reviewer"]
        self.assertEqual(role.body, "CUSTOM BODY")
        self.assertIn("independently review", role.description)
        self.assertEqual(role.tools, ["Read", "Grep", "Glob", "Bash"])
        self.assertEqual(role.sources, ["project override"])

    def test_override_frontmatter_wins(self):
        self.role_file("code-reviewer.md", "---\ntools: Read\n---\nBODY\n")
        self.assertEqual(sy.resolve_team(self.project).roles["code-reviewer"].tools, ["Read"])

    def test_append_extends_global_body(self):
        self.role_file("backend-engineer.append.md", "This project uses Django.\n")
        role = sy.resolve_team(self.project).roles["backend-engineer"]
        self.assertIn("# Backend Engineer", role.body)
        self.assertTrue(role.body.endswith("This project uses Django."))
        self.assertIn("## Project-Specific Guidance", role.body)
        self.assertEqual(role.sources, ["global", "appended"])

    def test_append_applies_on_top_of_override(self):
        self.role_file("backend-engineer.md", "---\ndescription: mine\n---\nBASE\n")
        self.role_file("backend-engineer.append.md", "EXTRA\n")
        role = sy.resolve_team(self.project).roles["backend-engineer"]
        self.assertEqual(role.description, "mine")
        self.assertTrue(role.body.startswith("BASE"))
        self.assertTrue(role.body.endswith("EXTRA"))

    def test_project_only_role(self):
        self.role_file("data-engineer.md", "---\ndescription: Pipelines\ntools: Read, Bash\n---\nDo data.\n")
        role = sy.resolve_team(self.project).roles["data-engineer"]
        self.assertEqual(role.sources, ["project"])
        self.assertEqual(role.sandbox, "read-only")

    def test_dangling_append_and_bad_names_warn(self):
        self.role_file("nonexistent.append.md", "x")
        self.role_file("Bad_Name.md", "---\n---\nx")
        team = sy.resolve_team(self.project)
        self.assertEqual(len(team.warnings), 2)
        self.assertNotIn("Bad_Name", team.roles)


class ConfigParsing(ProjectCase):
    def test_cli_options_merge_and_preserve_types_and_quoted_comments(self):
        self.user_config('codex-model_verbosity = high\ncodex-tui.show_tooltips = true\n'
                         'claude-model = "sonnet#chosen" # comment\nclaude-verbose = true\n')
        self.config('codex-model_verbosity = low\nclaude-verbose = false\n')
        team = sy.resolve_team(self.project)
        codex = sy.cli_options('codex', team.config)
        self.assertIn('model_verbosity="low"', codex)
        self.assertIn('tui.show_tooltips=true', codex)
        self.assertNotIn('--model', codex)
        self.assertEqual(sy.cli_options('claude', team.config), ['--model', 'sonnet#chosen'])
        self.assertEqual(team.config_sources['codex-model_verbosity'], 'project')

    def test_cli_values_are_separate_arguments_without_shell_evaluation(self):
        value = '$(touch /tmp/never-run); "quoted" # literal'
        self.config('codex-custom = ' + json.dumps(value) + '\n'
                    'claude-system-prompt = ' + json.dumps(value) + '\n'
                    'codex-features.example = true\ncodex-example.count = 3\n'
                    'codex-example.items = ["one", "two"]\n')
        team = sy.resolve_team(self.project)
        argv = sy.build_codex(team, False, ['task'])
        self.assertIn('custom=' + json.dumps(value), argv)
        for encoded in ('features.example=true', 'example.count=3', 'example.items=["one", "two"]'):
            self.assertIn(encoded, argv)
        claude = sy.build_claude(team, False, ['task'])
        self.assertEqual(claude[claude.index('--system-prompt') + 1], value)
        self.assertNotIn('--system-prompt', argv)

    def test_bad_cli_option_names_and_claude_collections_rejected(self):
        for text in ('codex- = low', 'claude---model = low', 'codex-a b = low',
                     'claude-model = ["sonnet"]'):
            self.config(text + '\n')
            with self.assertRaises(sy.SyndicateError):
                sy.resolve_team(self.project)

    def test_chief_cli_options_use_only_user_layer_and_selected_provider(self):
        self.user_config('codex-model_verbosity = low\nclaude-model = sonnet\n')
        self.config('codex-model_verbosity = high\nclaude-model = opus\n')
        for headless in (False, True):
            codex = sy.build_chief_of_staff('codex', self.project, headless, 'Brief me')
            self.assertIn('model_verbosity="low"', codex)
            self.assertNotIn('model_verbosity="high"', codex)
            self.assertNotIn('--model', codex)
            claude = sy.build_chief_of_staff('claude', self.project, headless, 'Brief me')
            self.assertEqual(claude[claude.index('--model') + 1], 'sonnet')
            self.assertFalse(any('model_verbosity=' in arg for arg in claude))

    def test_specialist_launch_uses_its_runtime_options(self):
        self.config('codex-model_verbosity = low\nclaude-model = sonnet\n')
        for provider in ('claude', 'codex'):
            with unittest.mock.patch.object(sy, 'require_clis'), \
                 unittest.mock.patch.object(sy, 'execution_project', return_value=self.project), \
                 unittest.mock.patch.object(sy, 'register_workspace', side_effect=lambda p: p), \
                 unittest.mock.patch.object(sy.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
                self.assertEqual(sy.main(['team', 'run-role', 'code-reviewer', '--project',
                                         str(self.project), '--runtime', provider, 'Review']), 0)
            argv = run.call_args.args[0]
            self.assertEqual(argv[-2:], ['--', 'Review'])
            if provider == 'codex':
                self.assertIn('model_verbosity="low"', argv)
                self.assertNotIn('--model', argv)
            else:
                self.assertEqual(argv[argv.index('--model') + 1], 'sonnet')

    def test_default_and_per_role(self):
        self.config("default = claude\ncode-reviewer = codex  # cheaper\n")
        team = sy.resolve_team(self.project)
        self.assertEqual(team.roles["code-reviewer"].cli, "codex")
        self.assertEqual(team.roles["backend-engineer"].cli, "claude")
        self.assertEqual(team.host, "claude")

    def test_orchestrator_cli_sets_host_and_flag_overrides(self):
        self.config("orchestrator = codex\n")
        self.assertEqual(sy.resolve_team(self.project).host, "codex")
        self.assertEqual(sy.resolve_team(self.project, "claude").host, "claude")

    def test_project_config_beats_env_default(self):
        self.config("default = codex\n")
        with unittest.mock.patch.dict("os.environ", {"SYNDICATE_RUNTIME": "claude"}):
            self.assertEqual(sy.resolve_team(self.project).roles["qa-engineer"].cli, "codex")

    def test_errors(self):
        for text in ("code-reviewer = gemini\n", "no-such-role = codex\n", "just a line\n", "review-gate = maybe\n"):
            self.config(text)
            with self.assertRaises(sy.SyndicateError, msg=text):
                sy.resolve_team(self.project)

    def test_review_gate_off(self):
        self.config("review-gate = off\nhandoff = off\n")
        self.assertFalse(sy.resolve_team(self.project).gate)

    def test_user_file_fills_keys_the_project_omits(self):
        self.user_config("handoff-reserve = 25\ncode-reviewer = codex\ndefault = codex\n")
        self.config("review-gate = off\ncode-reviewer = claude\n")
        team = sy.resolve_team(self.project)
        self.assertEqual(team.config["handoff-reserve"], "25")
        self.assertEqual(team.config_sources["handoff-reserve"], "user")
        self.assertEqual(team.config["default"], "codex")
        self.assertEqual(team.config_sources["default"], "user")
        self.assertEqual(team.config["review-gate"], "off")
        self.assertEqual(team.config_sources["review-gate"], "project")
        self.assertEqual(team.roles["code-reviewer"].cli, "claude")
        self.assertEqual(team.config_sources["code-reviewer"], "project")
        self.assertEqual(team.config_sources["handoff"], "default")
        self.assertEqual(team.roles["backend-engineer"].cli, "codex")

    def test_user_default_beats_environment_and_project_beats_both(self):
        self.user_config("default = codex\n")
        with unittest.mock.patch.dict(os.environ, {"SYNDICATE_RUNTIME": "claude"}):
            team = sy.resolve_team(self.project)
        self.assertEqual(team.config["default"], "codex")
        self.assertEqual(team.config_sources["default"], "user")
        self.config("default = claude\n")
        with unittest.mock.patch.dict(os.environ, {"SYNDICATE_RUNTIME": "codex"}):
            team = sy.resolve_team(self.project)
        self.assertEqual(team.roles["qa-engineer"].cli, "claude")
        self.assertEqual(team.config_sources["default"], "project")

    def test_environment_applies_when_neither_file_sets_default(self):
        with unittest.mock.patch.dict(os.environ, {"SYNDICATE_RUNTIME": "codex"}):
            team = sy.resolve_team(self.project)
        self.assertEqual(team.roles["qa-engineer"].cli, "codex")
        self.assertEqual(team.config_sources["default"], "environment")

    def test_absent_user_file_keeps_builtin_defaults(self):
        team = sy.resolve_team(self.project)
        self.assertEqual(team.config["handoff-reserve"], "10")
        self.assertEqual(team.config_sources["handoff"], "default")
        self.assertFalse(sy.user_config_path().exists())

    def test_inbox_config_merges_and_cli_wins(self):
        self.user_config('inbox-interval = 10\ninbox-transport = poll\n')
        self.config('inbox-interval = 15\n')
        team = sy.resolve_team(self.project)
        self.assertEqual(team.config['inbox-interval'], '15')
        self.assertEqual(team.config['inbox-transport'], 'poll')
        args = sy.build_parsers()[0].parse_args(['--inbox-interval', '30', '--inbox-transport', 'auto'])
        self.assertEqual(sy.inbox_options(args, team.config), dict(interval=30, transport='auto'))

    def test_invalid_inbox_config_is_rejected(self):
        for content in ('inbox-interval = 0', 'inbox-interval = 7', 'inbox-interval = nan', 'inbox-transport = invalid'):
            self.config(content + '\n')
            with self.assertRaises(sy.SyndicateError):
                sy.resolve_team(self.project)

    def test_user_file_errors_include_its_path(self):
        self.user_config("review-gate = maybe\n")
        with self.assertRaises(sy.SyndicateError) as caught:
            sy.resolve_team(self.project)
        self.assertIn(str(sy.user_config_path()), str(caught.exception))

    def test_user_config_path_follows_xdg_and_empty_falls_back(self):
        self.assertEqual(sy.user_config_path(), self.xdg / "syndicate" / "syndicate.conf")
        with unittest.mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "  "}):
            self.assertEqual(sy.user_config_path(), Path.home() / ".config" / "syndicate" / "syndicate.conf")

    def test_config_groups_winning_values_by_source_file(self):
        self.user_config("handoff-reserve = 25\ndefault = codex\n")
        self.config("review-gate = off\ncode-reviewer = codex\ndefault = claude\n")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(sy.main(["config", "-C", str(self.project)]), 0)
        text = output.getvalue()
        user = str(sy.user_config_path())
        project = str(self.project / ".syndicate" / "syndicate.conf")
        self.assertTrue(text.startswith(f"project: {self.project}\n"))
        self.assertLess(text.index("# built-in defaults"), text.index(f"# {user}"))
        self.assertLess(text.index(f"# {user}"), text.index(f"# {project}"))
        self.assertNotIn("*", text)
        defaults, user_body, project_body = text.split("\n\n")[1:]
        self.assertIn("handoff = on", defaults)
        self.assertNotIn("default =", defaults)
        self.assertNotIn("review-gate =", defaults)
        self.assertEqual(user_body.splitlines()[1:], ["handoff-reserve = 25"])
        self.assertEqual(project_body.splitlines()[1:], ["review-gate = off", "code-reviewer = codex", "default = claude"])

    def test_local_config_shows_overridden_layers_and_marks_project_file(self):
        self.user_config("handoff-reserve = 25\ndefault = codex\n")
        self.config("default = claude\n")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(sy.main(["config", "-C", str(self.project), "--local"]), 0)
        text = output.getvalue()
        project = str(self.project / ".syndicate" / "syndicate.conf")
        self.assertIn(f"# * {project}", text)
        self.assertIn("default = codex  # overridden by " + project, text)
        self.assertIn("handoff-reserve = 25", text)
        self.assertIn("handoff = on", text)
        self.assertIn("# built-in defaults", text)
        self.assertLess(text.index("# built-in defaults"), text.index(f"# * {project}"))

    def test_config_environment_is_its_own_group_until_a_file_replaces_it(self):
        with unittest.mock.patch.dict(os.environ, {"SYNDICATE_RUNTIME": "codex"}):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(sy.main(["config", "-C", str(self.project)]), 0)
        text = output.getvalue()
        self.assertIn("# $SYNDICATE_RUNTIME\ndefault = codex", text)
        self.assertNotIn("default = claude", text)
        self.config("default = claude\n")
        with unittest.mock.patch.dict(os.environ, {"SYNDICATE_RUNTIME": "codex"}):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(sy.main(["config", "-C", str(self.project), "--local"]), 0)
        local = output.getvalue()
        project = str(self.project / ".syndicate" / "syndicate.conf")
        self.assertIn("default = codex  # overridden by " + project, local)
        self.assertIn(f"# * {project}\ndefault = claude", local)

    def _registered_status(self, name, config, mtime):
        path = self.project / name
        local = path / ".syndicate"
        local.mkdir(parents=True)
        if config is not None:
            (local / "syndicate.conf").write_text(config)
        status = local / "status.json"
        status.write_text("{}\n")
        os.utime(status, (mtime, mtime))
        sy.register_project(path)
        return path

    def test_config_defaults_to_the_project_with_the_newest_status(self):
        older = self._registered_status("older", "review-gate = off\n", 1_000)
        newer = self._registered_status("newer", "handoff = off\n", 2_000)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(sy.main(["config"]), 0)
        text = output.getvalue()
        self.assertTrue(text.startswith(f"project: {newer}\n"))
        self.assertIn("handoff = off", text)
        self.assertNotIn("review-gate = off", text)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(sy.main(["config", "-C", str(older)]), 0)
        chosen = output.getvalue()
        self.assertTrue(chosen.startswith(f"project: {older}\n"))
        self.assertIn("review-gate = off", chosen)

    def test_local_marks_a_missing_project_file(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(sy.main(["config", "-C", str(self.project), "--local"]), 0)
        text = output.getvalue()
        project = str(self.project / ".syndicate" / "syndicate.conf")
        self.assertIn(f"# * {project}\n# absent", text)
        self.assertIn("handoff = on", text)

    def test_config_without_a_status_file_asks_for_a_project(self):
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(sy.main(["config"]), 1)
        self.assertIn("pass -C", error.getvalue())

    def test_global_prints_only_the_user_file(self):
        self.user_config("# shared\nhandoff-reserve = 25\n")
        self.config("review-gate = off\n")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(sy.main(["config", "--global"]), 0)
        text = output.getvalue()
        self.assertEqual(text, f"# {sy.user_config_path()}\n# shared\nhandoff-reserve = 25\n")
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(sy.main(["config", "--global", "--local"]), 1)
        self.assertIn("--global and --local", error.getvalue())

    def test_global_reports_a_missing_user_file(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(sy.main(["config", "--global"]), 0)
        self.assertEqual(output.getvalue(), f"# {sy.user_config_path()}\n# absent\n")

    def test_legacy_project_config_is_renamed(self):
        legacy = self.project / ".syndicate" / "config"
        legacy.write_text("review-gate = off\n")
        team = sy.resolve_team(self.project)
        self.assertEqual(team.config["review-gate"], "off")
        self.assertEqual(team.config_sources["review-gate"], "project")
        self.assertFalse(legacy.exists())
        self.assertEqual((self.project / ".syndicate" / "syndicate.conf").read_text(), "review-gate = off\n")

    def test_init_writes_a_template_without_copying_user_settings(self):
        self.user_config("handoff-reserve = 25\n")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sy.main(["team", "init", "--project", str(self.project)]), 0)
        text = (self.project / ".syndicate" / "syndicate.conf").read_text()
        self.assertNotIn("handoff-reserve = 25", text)
        self.assertIn("syndicate.conf", text)


class GitExclusion(ProjectCase):
    def test_excludes_locally_without_touching_gitignore(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        self.assertIsNone(sy.ensure_excluded(self.project))
        self.assertIsNone(sy.ensure_excluded(self.project))
        exclude = (self.project / ".git" / "info" / "exclude").read_text()
        self.assertEqual(exclude.splitlines().count("/.syndicate/"), 1)
        self.assertFalse((self.project / ".gitignore").exists())
        status = subprocess.run(["git", "-C", str(self.project), "status", "--short"],
                                capture_output=True, text=True).stdout
        self.assertEqual(status.strip(), "")

    def test_project_in_repo_subdirectory(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        sub = self.project / "apps" / "web"
        (sub / ".syndicate").mkdir(parents=True)
        sy.ensure_excluded(sub)
        exclude = (self.project / ".git" / "info" / "exclude").read_text()
        self.assertIn("/apps/web/.syndicate/", exclude.splitlines())

    def test_warns_when_already_tracked(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        (self.project / ".syndicate" / "syndicate.conf").write_text("")
        subprocess.run(["git", "-C", str(self.project), "add", "-f", ".syndicate/syndicate.conf"], check=True)
        self.assertIn("tracked", sy.ensure_excluded(self.project))

    def test_non_git_directory(self):
        self.assertIn("not a git repository", sy.ensure_excluded(self.project))


class LaunchCommands(ProjectCase):
    def test_claude_all_native(self):
        team = sy.resolve_team(self.project)
        argv = sy.build_claude(team, headless=True, rest=["do it"])
        agents = json.loads(argv[argv.index("--agents") + 1])
        self.assertEqual(argv[:3], ["claude", "--agent", "orchestrator"])
        self.assertEqual(agents["code-reviewer"]["tools"], ["Read", "Grep", "Glob", "Bash"])
        self.assertNotIn("SYNDICATE_BRIEF", agents["code-reviewer"]["prompt"])
        self.assertEqual(argv[-2:], ["-p", "do it"])
        settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
        self.assertIn("Stop", settings["hooks"])
        self.assertNotIn("permissions", settings)

    def test_claude_host_with_codex_reviewer_gets_a_relay(self):
        self.config("code-reviewer = codex\n")
        team = sy.resolve_team(self.project)
        argv = sy.build_claude(team, headless=False, rest=[])
        agents = json.loads(argv[argv.index("--agents") + 1])
        reviewer = agents["code-reviewer"]
        self.assertEqual(reviewer["tools"], ["Bash"])
        self.assertIn("run-role code-reviewer", reviewer["prompt"])
        self.assertIn("SYNDICATE_BRIEF", reviewer["prompt"])
        self.assertIn("Read", agents["backend-engineer"]["tools"])
        settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
        self.assertEqual(settings["permissions"]["allow"], [f"Bash({sy.SCRIPT} team run-role *)"])

    def test_gate_off_removes_hook(self):
        self.config("review-gate = off\nhandoff = off\n")
        argv = sy.build_claude(sy.resolve_team(self.project), False, [])
        self.assertNotIn("--settings", argv)

    def test_claude_orchestrator_can_run_portfolio_commands(self):
        argv = sy.build_claude(sy.resolve_team(self.project), False, [])
        agents = json.loads(argv[argv.index("--agents") + 1])
        self.assertIn("Bash", agents["orchestrator"]["tools"])
        self.assertIn("decisions acknowledge", agents["orchestrator"]["prompt"])

    def test_project_codex_portfolio_root_is_writable_in_both_modes(self):
        self.config("default = codex\n")
        for headless in (False, True):
            argv = sy.build_codex(sy.resolve_team(self.project), headless, ["task"])
            self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")
            roots = [argv[index + 1] for index, flag in enumerate(argv) if flag == "--add-dir"]
            self.assertEqual(roots, [str(sy.REGISTRY_PATH.parent / "portfolio")])
            self.assertTrue(Path(roots[0]).is_dir())
            self.assertNotIn(str(sy.REGISTRY_PATH.parent), roots)
            self.assertNotIn("danger-full-access", argv)

    def test_codex_host_with_claude_role_gets_a_relay(self):
        self.config("orchestrator = codex\ndefault = codex\ncode-reviewer = claude\n")
        team = sy.resolve_team(self.project)
        argv = sy.build_codex(team, headless=True, rest=["task"])
        self.assertEqual(argv[:6], ["codex", "exec", "-C", str(self.project), "-s", "workspace-write"])
        self.assertEqual(argv[-1], "task")
        gen = self.project / ".syndicate" / ".generated" / "codex"
        self.assertIn("run-role code-reviewer", (gen / "code-reviewer.toml").read_text())
        self.assertIn('sandbox_mode = "workspace-write"', (gen / "code-reviewer.toml").read_text())
        self.assertIn('sandbox_mode = "workspace-write"', (gen / "qa-engineer.toml").read_text())
        self.assertIn("agents.backend-engineer.config_file=", " ".join(argv))
        self.assertFalse((gen / "orchestrator.toml").exists())

    def test_codex_network_opened_only_when_a_relay_exists(self):
        self.config("default = codex\n")
        native = sy.build_codex(sy.resolve_team(self.project, "codex"), False, [])
        self.assertNotIn("sandbox_workspace_write.network_access=true", native)
        self.config("default = codex\ncode-reviewer = claude\n")
        mixed = sy.build_codex(sy.resolve_team(self.project, "codex"), False, [])
        self.assertIn("sandbox_workspace_write.network_access=true", mixed)

    def test_codex_native_reviewer_uses_isolated_relay(self):
        self.config("default = codex\n")
        sy.build_codex(sy.resolve_team(self.project, "codex"), False, [])
        gen = self.project / ".syndicate" / ".generated" / "codex"
        self.assertIn("run-role code-reviewer", (gen / "code-reviewer.toml").read_text())
        import tomllib
        reviewer = tomllib.loads((gen / "code-reviewer.toml").read_text())
        self.assertEqual(reviewer["sandbox_mode"], "read-only")
        self.assertNotIn("sandbox_workspace_write", reviewer)
        self.assertIn("outside the sandbox", reviewer["developer_instructions"])
        self.assertIn("request normal approval", reviewer["developer_instructions"])
        writer = tomllib.loads((gen / "backend-engineer.toml").read_text())
        self.assertEqual(writer["sandbox_mode"], "workspace-write")
        self.assertEqual(writer["sandbox_workspace_write"]["writable_roots"],
                         [str(sy.REGISTRY_PATH.parent / "portfolio")])

    def test_codex_removes_stale_agent_files(self):
        self.config("orchestrator = codex\n")
        gen = self.project / ".syndicate" / ".generated" / "codex"
        gen.mkdir(parents=True)
        (gen / "gone.toml").write_text("x")
        sy.build_codex(sy.resolve_team(self.project), False, [])
        self.assertFalse((gen / "gone.toml").exists())

    def test_toml_strings_survive_quotes_and_newlines(self):
        import tomllib
        self.role_file("data-engineer.md", '---\ndescription: say "hi"\n---\nline1\n"quoted" \\ back\n')
        self.config("default = codex\n")
        sy.build_codex(sy.resolve_team(self.project, "codex"), False, [])
        toml = tomllib.loads((self.project / ".syndicate/.generated/codex/data-engineer.toml").read_text())
        self.assertEqual(toml["developer_instructions"], 'line1\n"quoted" \\ back')

    def test_reported_outcome_rule_reaches_both_providers(self):
        team = sy.resolve_team(self.project)
        claude = sy.build_claude(team, False, [])
        prompt = json.loads(claude[claude.index("--agents") + 1])["orchestrator"]["prompt"]
        codex = sy.build_codex(team, False, [])
        codex_prompt = next(a for a in codex if a.startswith("developer_instructions="))
        for payload in (prompt, codex_prompt):
            for rule in ("Requester-reported external outcomes", "gh pr view NUMBER",
                         "before confirming", "If either durable write fails",
                         "matching task's status/progress", "remains unverified"):
                self.assertIn(rule, payload)

    def test_task_closure_write_failure_is_explicit(self):
        output = io.StringIO()
        with unittest.mock.patch.object(sy, "registry_lock", side_effect=PermissionError("denied")), contextlib.redirect_stderr(output):
            sy.persist_task_closures([("source", "task", {})])
        self.assertIn("could not persist task closures", output.getvalue())
        self.assertIn("outside the sandbox with approval", output.getvalue())

    def test_codex_scratch_denial_explains_approval_path(self):
        self.config("default = codex\n")
        args = argparse.Namespace(dir=str(self.project), role="code-reviewer", runtime=None, brief="review")
        with unittest.mock.patch.object(sy, "require_clis"), \
             unittest.mock.patch.object(sy.tempfile, "TemporaryDirectory", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(sy.SyndicateError, "outside the sandbox with approval"):
                sy.cmd_run_role(args)

    def test_codex_reviewer_can_write_only_scratch(self):
        self.config("default = codex\n")
        args = argparse.Namespace(dir=str(self.project), role="code-reviewer", runtime=None, brief="review")
        def run(argv, **kwargs):
            cwd = Path(kwargs["cwd"])
            self.assertNotEqual(cwd, self.project)
            self.assertEqual(cwd.parent, sy.REGISTRY_PATH.parent / "portfolio/scratch")
            self.assertTrue(cwd.is_dir())
            self.assertEqual(argv[argv.index("-C") + 1], str(cwd))
            roots = [argv[i+1] for i, a in enumerate(argv) if a == "--add-dir"]
            self.assertEqual(roots, [str(cwd)])
            self.assertEqual(kwargs["env"]["TMPDIR"], str(cwd))
            self.assertIn("sandbox_workspace_write.exclude_slash_tmp=true", argv)
            self.assertIn("sandbox_workspace_write.exclude_tmpdir_env_var=true", argv)
            instructions = next(a for a in argv if a.startswith("developer_instructions="))
            self.assertIn(str(self.project), instructions)
            for rule in ("AGENTS.md", "AGENTS.override.md", "ancestors and root", "nested instruction files"):
                self.assertIn(rule, instructions)
            (cwd / "fixture").write_text("test")
            return subprocess.CompletedProcess(argv, 0)
        # execution_project invokes git; only mock the specialist subprocess.
        real_run = subprocess.run
        def dispatch(argv, **kwargs):
            return run(argv, **kwargs) if argv[0] == "codex" else real_run(argv, **kwargs)
        with unittest.mock.patch.object(sy, "require_clis"), unittest.mock.patch.object(sy.subprocess, "run", side_effect=dispatch):
            self.assertEqual(sy.cmd_run_role(args), 0)
        self.assertEqual(list((sy.REGISTRY_PATH.parent / "portfolio/scratch").iterdir()), [])


class StatusAndRegistry(ProjectCase):
    def setUp(self):
        super().setUp()
        self._reg_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._reg_tmp.cleanup)
        patcher = unittest.mock.patch.object(sy, "REGISTRY_PATH", Path(self._reg_tmp.name) / "projects.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_register_project_dedupes_and_puts_latest_first(self):
        other = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(other, ignore_errors=True))
        sy.register_project(other)
        sy.register_project(self.project)
        sy.register_project(other)  # re-registering moves it back to the front
        paths = [p["path"] for p in sy.load_registry()]
        self.assertEqual(paths, [str(other), str(self.project)])

    def test_workspace_uses_git_root_and_generic_groups(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        child = self.project / "nested"
        child.mkdir()
        self.assertEqual(sy.register_workspace(child), self.project)
        self.assertEqual(len(sy.load_registry()), 1)
        with tempfile.TemporaryDirectory() as outside:
            other = Path(outside).resolve()
            sy.register_workspace(other)
            sy.write_status_stub(other, os.getpid(), "non repo task")
            generic = next(r for r in sy.gather_statuses() if r["name"] == "Generic tasks")
            self.assertEqual(generic["tasks"][0]["summary"], "non repo task")
            self.assertEqual(generic["tasks"][0]["source_path"], str(other))

    def test_assign_generic_task_preserves_live_source(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        with tempfile.TemporaryDirectory() as outside:
            source = Path(outside).resolve()
            sy.register_workspace(source)
            sy.write_status_stub(source, os.getpid(), "draft docs")
            data = json.loads(sy.status_path(source).read_text())
            task_id = data["current"]["task_id"]
            before = sy.status_path(source).read_bytes()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(sy.main(["tasks", "edit", task_id, "--project", str(self.project)]), 0)
            self.assertEqual(sy.status_path(source).read_bytes(), before)
            data["current"]["summary"] = "still writing"
            sy.status_path(source).write_text(json.dumps(data))
            rows = sy.gather_statuses()
            repo = next(r for r in rows if r["path"] == str(self.project))
            self.assertEqual(repo["tasks"][0]["progress"], "still writing")
            self.assertFalse(repo["tasks"][0]["needs_project"])
            generic = next(r for r in rows if r["name"] == "Generic tasks")
            self.assertEqual(generic["unfinished_count"], 0)
            self.assertEqual(generic["project_status"], "No tasks")

    def test_launch_failure_still_records_explicit_directory_task(self):
        args = sy.build_parsers()[0].parse_args(["--project", str(self.project), "new task"])
        with unittest.mock.patch.object(sy, "require_clis", side_effect=sy.SyndicateError("missing CLI")):
            with self.assertRaises(sy.SyndicateError):
                sy.cmd_run(args)
        row = sy.gather_statuses()[0]
        self.assertEqual(row["name"], self.project.name)
        self.assertEqual(row["project_status"], "Needs attention")
        self.assertFalse(row["tasks"][0]["needs_project"])

    def test_explicit_project_uses_repo_without_prompt(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        args = sy.build_parsers()[0].parse_args(["--project", str(self.project), "new task"])
        with unittest.mock.patch.object(sy, "run_session", return_value=0):
            self.assertEqual(sy.cmd_run(args), 0)
        self.assertEqual(args.project, str(self.project))
        task = sy.gather_statuses()[0]["tasks"][0]
        self.assertFalse(task["needs_project"])

    def test_relay_run_does_not_take_registry_lock(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        args = argparse.Namespace(dir=str(self.project), role="backend-engineer", runtime=None, brief="work")
        with unittest.mock.patch.object(sy, "require_clis", side_effect=sy.SyndicateError("missing CLI")):
            with self.assertRaises(sy.SyndicateError):
                sy.cmd_run_role(args)
        self.assertEqual(sy.load_registry(), [])

    def test_handoff_resumes_existing_task(self):
        sy.register_workspace(self.project)
        sy.write_status_stub(self.project, 2**30, "original request")
        snapshot = self.project / "snapshot"
        snapshot.mkdir()
        (snapshot / "status.json").write_bytes(sy.status_path(self.project).read_bytes())
        prior = json.loads(sy.status_path(self.project).read_text())
        args = sy.build_parsers()[0].parse_args(["--project", str(self.project), "--handoff", str(snapshot)])
        with unittest.mock.patch.object(sy, "run_session", return_value=0):
            sy.cmd_run(args)
        after = json.loads(sy.status_path(self.project).read_text())
        self.assertEqual(after["current"]["task_id"], prior["current"]["task_id"])
        self.assertEqual(len(after["tasks"]), 1)

    def test_generic_selection_does_not_ask_or_change_directory(self):
        launch_cwd = Path(self._reg_tmp.name) / "launch-cwd"
        launch_cwd.mkdir()
        os.chdir(launch_cwd)
        args = sy.build_parsers()[0].parse_args(["--project", "generic", "request"])
        with unittest.mock.patch.object(sy, "run_session", return_value=0):
            sy.cmd_run(args)
        # Regression: generic runs once wrote their task into the launch cwd (the repo).
        self.assertFalse((launch_cwd / ".syndicate").exists())
        self.assertTrue(sy.status_path(sy.REGISTRY_PATH.parent / "generic-tasks").is_file())
        row = sy.gather_statuses()[0]
        self.assertEqual(row["name"], "Generic tasks")
        self.assertFalse(row["tasks"][0]["needs_project"])
        self.assertEqual(args.dir, str(sy.REGISTRY_PATH.parent / "generic-tasks"))

    def test_task_state_edit_controls_completion_and_ambiguous_ids_fail(self):
        sy.register_project(self.project)
        sy.write_status_stub(self.project, 2**30, "interrupted")
        task_id = sy.gather_statuses()[0]["tasks"][0]["id"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sy.main(["tasks", "edit", task_id, "--state", "done"]), 0)
        row = sy.gather_statuses()[0]
        self.assertEqual(row["project_status"], "Complete")
        self.assertEqual(row["last_completed"]["id"], task_id)
        self.assertEqual(sy.main(["tasks", "edit", "missing", "--state", "done"]), 1)

    def test_write_status_stub_preserves_and_caps_history(self):
        sy.write_status_stub(self.project, 111, "first run")
        data = json.loads(sy.status_path(self.project).read_text())
        self.assertEqual(data["pid"], 111)
        self.assertEqual(data["state"], "running")
        self.assertEqual(data["history"], [])

        data["history"] = [{"round": f"r{i}"} for i in range(250)]
        sy.status_path(self.project).write_text(json.dumps(data))
        sy.write_status_stub(self.project, 222, "second run")
        data2 = json.loads(sy.status_path(self.project).read_text())
        self.assertEqual(data2["pid"], 222)
        self.assertEqual(len(data2["history"]), sy.MAX_STATUS_HISTORY)
        self.assertEqual(data2["history"][-1]["round"], "r249")  # most recent kept

    def test_unfinished_tasks_survive_new_run(self):
        sy.register_project(self.project)
        sy.write_status_stub(self.project, os.getpid(), "first request")
        status = json.loads(sy.status_path(self.project).read_text())
        status["tasks"][0]["state"] = status["state"] = "blocked"
        sy.status_path(self.project).write_text(json.dumps(status))
        sy.write_status_stub(self.project, os.getpid(), "second request")
        row = sy.gather_statuses()[0]
        self.assertEqual(row["unfinished_count"], 2)
        self.assertEqual(row["project_status"], "Needs attention")
        self.assertEqual(row["tasks"][0]["summary"], "first request")
        self.assertEqual(row["tasks"][0]["state"], "blocked")

    def test_superseded_running_task_is_ended_not_stale_or_done(self):
        sy.register_project(self.project)
        sy.write_status_stub(self.project, 2**30, "first request")
        sy.write_status_stub(self.project, os.getpid(), "second request")
        current = json.loads(sy.status_path(self.project).read_text())["current"]["task_id"]
        row = sy.gather_statuses()[0]
        self.assertEqual(row["unfinished_count"], 1)
        self.assertEqual(row["project_status"], "Working")
        self.assertIsNone(row["last_completed"])
        ended = row["ended_tasks"][0]
        self.assertEqual((ended["summary"], ended["state"], ended["last_state"]), ("first request", "ended", "running"))
        self.assertEqual(ended["end_reason"], f"superseded by task {current}")

    def test_completed_fallback_and_new_request(self):
        sy.register_project(self.project)
        sy.write_status_stub(self.project, os.getpid(), "finished request")
        data = json.loads(sy.status_path(self.project).read_text())
        data["state"] = "done"
        sy.status_path(self.project).write_text(json.dumps(data))
        row = sy.gather_statuses()[0]
        self.assertEqual(row["project_status"], "Complete")
        self.assertEqual(row["tasks"], [])
        self.assertEqual(row["last_completed"]["summary"], "finished request")
        sy.write_status_stub(self.project, os.getpid(), "new request")
        row = sy.gather_statuses()[0]
        self.assertEqual(row["unfinished_count"], 1)
        self.assertEqual(row["last_completed"]["summary"], "finished request")

    def test_queued_tasks_and_latest_completion(self):
        sy.register_project(self.project)
        sy.status_path(self.project).write_text(json.dumps({"tasks": [
            {"id": "old", "summary": "older", "state": "done", "completed_at": "2026-01-01"},
            {"id": "new", "summary": "newer", "state": "done", "completed_at": "2026-02-01"},
            {"id": "next", "summary": "next", "state": "queued"},
        ]}))
        row = sy.gather_statuses()[0]
        self.assertEqual(row["project_status"], "Queued")
        self.assertEqual(row["last_completed"]["summary"], "newer")
        self.assertEqual(row["unfinished_count"], 1)

    def test_no_tasks_is_distinct_from_corrupt_status(self):
        sy.register_project(self.project)
        self.assertEqual(sy.gather_statuses()[0]["project_status"], "No tasks")
        sy.status_path(self.project).write_text("invalid")
        self.assertEqual(sy.gather_statuses()[0]["project_status"], "Needs attention")

    def test_gather_statuses_unknown_when_no_status_file(self):
        sy.register_project(self.project)
        rows = sy.gather_statuses()
        self.assertEqual(rows[0]["state"], "unknown")
        self.assertIsNone(rows[0]["current"])

    def test_gather_statuses_does_not_guess_dead_from_bare_pid(self):
        sy.register_project(self.project)
        sy.write_status_stub(self.project, 2**30, "stub")  # pid that can't exist: no identity recorded
        rows = sy.gather_statuses()
        self.assertEqual(rows[0]["state"], "running")
        self.assertEqual(rows[0]["liveness"], "unknown")
        self.assertEqual(rows[0]["tasks"][0]["liveness"], "unknown")
        self.assertFalse((sy.REGISTRY_PATH.parent / "task-edits.json").exists())

    def test_gather_statuses_running_waiting_done(self):
        sy.register_project(self.project)
        sy.write_status_stub(self.project, os.getpid(), "starting")
        self.assertEqual(sy.gather_statuses()[0]["state"], "running")

        sy.status_path(self.project).write_text(json.dumps({
            "pid": os.getpid(), "state": "waiting_on_user",
            "current": {"round": "r1", "role": "security-engineer", "summary": "pick an option"},
            "history": [{"at": "t", "round": "r0", "outcome": "approved", "summary": "done"}],
        }))
        row = sy.gather_statuses()[0]
        self.assertEqual(row["state"], "waiting_on_user")
        self.assertEqual(row["current"]["role"], "security-engineer")
        self.assertEqual(len(row["history"]), 1)
        self.assertIsNotNone(row["updated_at"])

        sy.status_path(self.project).write_text(json.dumps({"pid": None, "state": "done", "current": {}, "history": []}))
        self.assertEqual(sy.gather_statuses()[0]["state"], "done")

    def test_gather_statuses_malformed_file_is_unknown_not_a_crash(self):
        sy.register_project(self.project)
        sy.status_path(self.project).write_text("not json")
        self.assertEqual(sy.gather_statuses()[0]["state"], "unknown")

    def test_render_dashboard_empty_and_populated(self):
        self.assertIn("no registered projects", sy.render_dashboard([]))
        rows = [{"name": "proj", "state": "running",
                 "current": {"role": ["security-engineer", "code-reviewer"], "summary": "reviewing"}}]
        out = sy.render_dashboard(rows, details=True)
        self.assertIn("proj", out)
        self.assertIn("security-engineer, code-reviewer", out)
        self.assertIn("reviewing", out)

    def test_dashboard_width_color_and_task_details(self):
        rows = [{"name": "a-very-long-project-name", "path": "/tmp/" + "long/" * 25,
                 "project_status": "Needs attention", "unfinished_count": 2,
                 "tasks": [{"state": "blocked", "summary": "First task " * 20,
                            "progress": "Waiting for input"},
                           {"state": "queued", "summary": "Second task"}]}]
        for width in (24, 32, 40, 69, 70, 80, 120):
            plain = sy.render_dashboard(rows, width=width, details=True)
            self.assertTrue(all(len(line) <= width for line in plain.splitlines()), width)
            self.assertNotIn("\x1b", plain)
            self.assertIn("Second task", plain)
        self.assertIn("\x1b[", sy.render_dashboard(rows, color=True))
        rows[0]["tasks"][0]["summary"] = "unsafe\x1b[2J\ntext"
        self.assertNotIn("\x1b", sy.render_dashboard(rows))

    def test_dashboard_details_are_opt_in(self):
        rows = [{"name": "proj", "path": "/tmp/project-path", "project_status": "Working",
                 "tasks": [{"summary": "Main task", "progress": "Extended progress"},
                           {"summary": "Extra task"}]}]
        for enabled in (False, True):
            args = sy.build_parsers()[1].parse_args(
                ["dashboard", "--once"] + (["--details"] if enabled else []))
            with unittest.mock.patch.object(sy, "gather_statuses", return_value=rows), \
                 unittest.mock.patch("sys.stdout", new=io.StringIO()) as output:
                sy.cmd_dashboard(args)
            text = output.getvalue()
            self.assertIn("proj", text)
            for detail in ("/tmp/project-path", "Extended progress", "Extra task"):
                self.assertEqual(detail in text, enabled)

    def test_dashboard_quit_key_and_timeout(self):
        import select
        for key in (b"q", b"Q"):
            with unittest.mock.patch.object(select, "select", return_value=([7], [], [])), \
                 unittest.mock.patch.object(sy.os, "read", return_value=key):
                self.assertTrue(sy.dashboard_wait(30, 7))
        with unittest.mock.patch.object(select, "select", return_value=([], [], [])):
            self.assertFalse(sy.dashboard_wait(0.01, 7))
        with unittest.mock.patch.object(sy.time, "sleep") as sleep:
            self.assertFalse(sy.dashboard_wait(2, None))
            sleep.assert_called_once_with(2)

    def test_dashboard_restores_terminal_on_exit(self):
        import termios
        import tty
        for failure in (False, True):
            with unittest.mock.patch.object(sy.sys, "stdin") as stdin, \
                 unittest.mock.patch.object(termios, "tcgetattr", return_value=["original"]), \
                 unittest.mock.patch.object(termios, "tcsetattr") as restore, \
                 unittest.mock.patch.object(tty, "setcbreak") as cbreak:
                stdin.isatty.return_value = True
                stdin.fileno.return_value = 7
                try:
                    with sy.dashboard_keyboard(True) as fd:
                        self.assertEqual(fd, 7)
                        if failure:
                            raise KeyboardInterrupt
                except KeyboardInterrupt:
                    pass
                cbreak.assert_called_once_with(7)
                restore.assert_called_once_with(7, termios.TCSADRAIN, ["original"])

    def test_dashboard_color_policy(self):
        with unittest.mock.patch.object(sy.sys.stdout, "isatty", return_value=True), \
             unittest.mock.patch.dict(sy.os.environ, {"TERM": "xterm"}, clear=True):
            self.assertTrue(sy.dashboard_color())
            with unittest.mock.patch.dict(sy.os.environ, {"NO_COLOR": ""}):
                self.assertFalse(sy.dashboard_color())
                self.assertTrue(sy.dashboard_color("always"))
            self.assertFalse(sy.dashboard_color("never"))
        with unittest.mock.patch.object(sy.sys.stdout, "isatty", return_value=False):
            self.assertFalse(sy.dashboard_color())

    def test_cmd_run_registers_and_writes_stub_before_exec(self):
        self.config("handoff = off\n")
        with unittest.mock.patch.object(sy, "require_clis"), \
             unittest.mock.patch.object(sy, "os") as mock_os, \
             unittest.mock.patch.object(sy.shutil, "which", return_value="/bin/true"):
            mock_os.environ = {}
            mock_os.getpid.return_value = 4242
            mock_os.execvp.side_effect = SystemExit  # never actually exec
            args = argparse.Namespace(dir=str(self.project), runtime=None, print=True, rest=["do the thing please"])
            with self.assertRaises(SystemExit):
                sy.cmd_run(args)
        data = json.loads(sy.status_path(self.project).read_text())
        self.assertEqual(data["pid"], 4242)
        self.assertIn("do the thing please", data["current"]["summary"])
        self.assertEqual([p["path"] for p in sy.load_registry()], [str(self.project)])



def spawn_owner():
    """A real short-lived process whose identity is recorded while it is alive."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    return proc, sy.portfolio.process_identity(proc.pid)


def reap(proc):
    proc.kill()
    proc.wait()


class StaleSessions(ProjectCase):
    def setUp(self):
        super().setUp()
        registry = tempfile.TemporaryDirectory()
        self.addCleanup(registry.cleanup)
        patcher = unittest.mock.patch.object(sy, "REGISTRY_PATH", Path(registry.name) / "projects.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def edits_path(self):
        return sy.REGISTRY_PATH.parent / "task-edits.json"

    def current_task_id(self):
        return json.loads(sy.status_path(self.project).read_text())["current"]["task_id"]

    def test_normal_exit_closes_owned_running_task_as_ended(self):
        sy.register_project(self.project)
        sy.write_status_stub(self.project, os.getpid(), "supervised work")
        task_id = self.current_task_id()
        with sy.project_session(self.project, "codex") as session:
            self.assertTrue(sy.coordination_store().snapshot()["sessions"][0]["live"])
        status = json.loads(sy.status_path(self.project).read_text())
        task = status["tasks"][-1]
        self.assertEqual(status["state"], "ended")
        self.assertEqual((task["id"], task["state"], task["last_state"], task["end_reason"]),
                         (task_id, "ended", "running", "session exited"))
        self.assertTrue(task["ended_at"])
        self.assertNotIn("completed_at", task)
        self.assertEqual(sy.coordination_store().read()["sessions"][0]["state"], "ended")
        row = sy.gather_statuses()[0]
        self.assertEqual((row["unfinished_count"], row["last_completed"], row["project_status"]), (0, None, "No tasks"))
        self.assertEqual(row["ended_tasks"][0]["id"], task_id)
        self.assertEqual(row["ended_tasks"][0]["session_id"], session["id"])

    def test_normal_exit_preserves_agent_written_final_states(self):
        sy.register_project(self.project)
        for final in ("done", "failed", "blocked", "waiting_on_user"):
            with self.subTest(final=final):
                sy.write_status_stub(self.project, os.getpid(), f"{final} work")
                with sy.project_session(self.project, "claude"):
                    status = json.loads(sy.status_path(self.project).read_text())
                    status["state"] = final
                    sy.status_path(self.project).write_text(json.dumps(status))
                after = json.loads(sy.status_path(self.project).read_text())
                self.assertEqual(after["state"], final)
                self.assertNotIn("end_reason", after["tasks"][-1])

    def test_launch_failure_is_not_closed_as_ended(self):
        sy.write_status_stub(self.project, os.getpid(), "launch fails")
        with self.assertRaises(RuntimeError):
            with sy.project_session(self.project, "codex"):
                raise RuntimeError("launch error")
        self.assertEqual(json.loads(sy.status_path(self.project).read_text())["state"], "running")

    def isolate_launch(self):
        """Keep launches away from the real ~/.syndicate (limits state, HOME)."""
        home = Path(self._tmp.name) / "home"
        home.mkdir(exist_ok=True)
        for patcher in (unittest.mock.patch.dict(os.environ, {"HOME": str(home)}),
                        unittest.mock.patch.object(sy.runtime, "STATE", home / ".syndicate" / "limits"),
                        unittest.mock.patch.object(sy, "require_clis"),
                        unittest.mock.patch.object(sy.runtime, "start_monitor")):
            patcher.start()
            self.addCleanup(patcher.stop)

    @unittest.skipUnless(Path("/proc").is_dir(), "needs /proc process identities")
    def test_supervised_run_never_passes_project_lock_and_records_cli_owner(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        self.isolate_launch()
        lock_path = self.project / ".syndicate" / "session.lock"
        child, identity = spawn_owner()
        self.addCleanup(reap, child)
        seen = {}

        def supervised(*_, **kwargs):
            fd = args.session_lock_fd
            seen.update(inheritable=os.get_inheritable(fd), pass_fds=tuple(kwargs.get("pass_fds", ())),
                        inode=os.fstat(fd).st_ino, probe=sy.portfolio.probe_lock(lock_path))
            kwargs["on_start"](child.pid)
            status = json.loads(sy.status_path(self.project).read_text())
            session = sy.coordination_store().read()["sessions"][-1]
            seen.update(status_child=status["cli_owner"], task_child=status["tasks"][-1]["cli_owner"],
                        session_child=session["cli_owner"], lock_owned=status["lock_owned"])
            return 0, None
        args = sy.build_parsers()[0].parse_args(["--project", str(self.project), "supervised task"])
        with unittest.mock.patch.object(sy.runtime, "supervise", side_effect=supervised), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sy.cmd_run(args), 0)
        self.assertFalse(seen["inheritable"])
        self.assertNotIn(args.session_lock_fd, seen["pass_fds"])
        self.assertEqual(seen["pass_fds"], ())
        self.assertEqual(seen["probe"], "held")
        self.assertTrue(seen["lock_owned"])
        for key in ("status_child", "task_child", "session_child"):
            self.assertEqual((seen[key]["pid"], seen[key]["process_identity"]), (child.pid, identity))
        status = json.loads(sy.status_path(self.project).read_text())
        self.assertEqual(status["state"], "ended")
        self.assertEqual(status["lock_inode"], [lock_path.stat().st_dev, seen["inode"]])
        self.assertEqual(status["process_identity"], sy.portfolio.process_identity(os.getpid()))
        self.assertEqual(sy.portfolio.probe_lock(lock_path), "free")

    def test_exec_path_never_inherits_project_lock_and_relies_on_identity(self):
        import fcntl
        self.config("handoff = off\n")
        self.isolate_launch()
        lock_path = self.project / ".syndicate" / "session.lock"
        seen = {}

        def execvp(*_):
            fd = args.session_lock_fd
            status = json.loads(sy.status_path(self.project).read_text())
            seen.update(inheritable=os.get_inheritable(fd), inode=os.fstat(fd).st_ino,
                        cloexec=bool(fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC),
                        lock_owned=status["lock_owned"], task_lock_owned=status["tasks"][-1]["lock_owned"],
                        pid=status["pid"])
            raise SystemExit(0)
        args = sy.build_parsers()[0].parse_args(["--project", str(self.project), "-p", "exec task"])
        with unittest.mock.patch.object(sy.os, "execvp", side_effect=execvp), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                sy.cmd_run(args)
        self.assertEqual(seen, dict(inheritable=False, cloexec=True, inode=lock_path.stat().st_ino, lock_owned=False,
                                    task_lock_owned=False, pid=os.getpid()))

    def test_launch_retries_past_a_momentary_lock_probe(self):
        import fcntl
        lock_path = self.project / ".syndicate" / "session.lock"
        lock_path.touch()
        probe = lock_path.open("rb")
        self.addCleanup(probe.close)
        fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
        sleeps = []

        def release(delay):  # deterministic: the probe ends during the first retry wait
            sleeps.append(delay)
            fcntl.flock(probe, fcntl.LOCK_UN)
        args = sy.build_parsers()[0].parse_args(["--project", str(self.project), "task"])
        with unittest.mock.patch.object(sy, "run_session", return_value=0), \
             unittest.mock.patch.object(sy.time, "sleep", side_effect=release):
            self.assertEqual(sy.cmd_run(args), 0)
        self.assertEqual(sleeps, [0.05])

    def test_held_lock_error_explains_leftover_holders(self):
        import fcntl
        lock_path = self.project / ".syndicate" / "session.lock"
        lock_path.touch()
        holder = lock_path.open("rb")
        self.addCleanup(holder.close)
        fcntl.flock(holder, fcntl.LOCK_EX)
        args = sy.build_parsers()[0].parse_args(["--project", str(self.project), "task"])
        with unittest.mock.patch.object(sy, "run_session", return_value=0) as run, \
             unittest.mock.patch.object(sy.time, "sleep"):
            with self.assertRaises(sy.SyndicateError) as raised:
                sy.cmd_run(args)
        run.assert_not_called()
        message = str(raised.exception)
        self.assertIn("another Syndicate orchestrator already owns this project", message)
        self.assertIn("leftover process may still hold it", message)
        self.assertIn("fuser -v", message)
        self.assertIn("lsof", message)
        self.assertIn(".syndicate/session.lock", message)

    @unittest.skipUnless(Path("/proc").is_dir(), "needs /proc process identities")
    def test_abrupt_exit_is_swept_with_requests_failed_and_idempotent(self):
        sy.register_project(self.project)
        proc, identity = spawn_owner()
        try:
            sy.write_status_stub(self.project, proc.pid, "abrupt work")
            task_id = self.current_task_id()
            store = sy.coordination_store()
            session = store.start_session(str(self.project), task_id, "codex", proc.pid)
            sy.runtime.atomic_json(sy.status_path(self.project), {
                **json.loads(sy.status_path(self.project).read_text()),
                "current": {"task_id": task_id, "session_id": session["id"], "summary": "abrupt work"}})
            request = store.send_request(source_path=str(self.project), project_path=str(self.project),
                                         task_id=task_id, key="k", message="Review")
            self.assertEqual(request["session_id"], session["id"])
            self.assertEqual(sy.gather_statuses()[0]["tasks"][0]["liveness"], "alive")
        finally:
            reap(proc)  # SIGKILL: no finally block in the owner ever runs
        status_before = sy.status_path(self.project).read_bytes()
        row = sy.gather_statuses()[0]
        data = store.read()
        self.assertEqual((data["sessions"][0]["state"], data["sessions"][0]["reason"]), ("ended", "owner process exited"))
        self.assertEqual((data["requests"][0]["state"], data["requests"][0]["reason"]),
                         ("failed", "Target session ended before acknowledgment"))
        task = row["ended_tasks"][0]
        self.assertEqual((task["id"], task["last_state"], task["end_reason"]), (task_id, "running", "owner process exited"))
        self.assertEqual(row["state"], "ended")
        self.assertEqual(sy.status_path(self.project).read_bytes(), status_before)  # agent-owned file untouched
        edits, coordination = self.edits_path().read_bytes(), store.path.read_bytes()
        sy.gather_statuses()
        sy.reconcile_stale()
        self.assertEqual(self.edits_path().read_bytes(), edits)
        self.assertEqual(store.path.read_bytes(), coordination)

    @unittest.skipUnless(Path("/proc").is_dir(), "needs /proc process identities")
    def test_resumed_task_ignores_closure_for_its_previous_owner(self):
        sy.register_project(self.project)
        proc, _ = spawn_owner()
        sy.write_status_stub(self.project, proc.pid, "resumable")
        reap(proc)
        status = json.loads(sy.status_path(self.project).read_text())
        self.assertEqual(sy.gather_statuses()[0]["ended_tasks"][0]["end_reason"], "owner process exited")
        status.update(sy.owner_identity(os.getpid()))  # a handoff resume takes over the same task
        sy.status_path(self.project).write_text(json.dumps(status))
        row = sy.gather_statuses()[0]
        self.assertEqual(row["tasks"][0]["state"], "running")

    @unittest.skipUnless(Path("/proc").is_dir(), "needs /proc process identities")
    def test_pid_reuse_mismatch_is_dead(self):
        sy.register_project(self.project)
        sy.write_status_stub(self.project, os.getpid(), "reused pid")
        status = json.loads(sy.status_path(self.project).read_text())
        status["process_identity"] = sy.portfolio.boot_id() + ":1"  # same live PID, different start time
        sy.status_path(self.project).write_text(json.dumps(status))
        task = sy.gather_statuses()[0]["ended_tasks"][0]
        self.assertEqual(task["end_reason"], "owner process exited")
        session = sy.coordination_store().start_session(str(self.project), "other", "codex", os.getpid())
        data = sy.coordination_store().read()
        data["sessions"][0]["process_identity"] = sy.portfolio.boot_id() + ":1"
        sy.runtime.atomic_json(sy.coordination_store().path, data)
        sy.gather_statuses()
        self.assertEqual(sy.coordination_store().read()["sessions"][0]["state"], "ended", session["id"])

    def test_legacy_bare_pid_is_not_probed_but_superseded_by_lock_proof(self):
        sy.register_project(self.project)
        legacy = {"id": "legacy-request", "summary": "request", "state": "running", "pid": 2,
                  "project_path": "/tmp/tmpfjkj4dv8/generic-tasks", "updated_at": "2026-11-08T19:48:16Z"}
        sy.status_path(self.project).write_text(json.dumps({"pid": 2, "state": "running",
            "current": {"task_id": "legacy-request", "summary": "request"}, "tasks": [legacy]}))
        # PID 2 is usually kthreadd on the host and the test runner inside a
        # sandbox: a bare PID probe proves nothing either way.
        row = next(r for r in sy.gather_statuses() if r["tasks"])
        self.assertEqual((row["tasks"][0]["state"], row["tasks"][0]["liveness"]), ("running", "unknown"))
        self.assertFalse(self.edits_path().exists())
        # The test runner itself can be PID 2 in a sandbox. Use a child so
        # supersession really has a different PID from the legacy owner.
        proc, _ = spawn_owner()
        try:
            sy.write_status_stub(self.project, proc.pid, "later launch")
            rows = sy.gather_statuses()
        finally:
            reap(proc)
        ended = next(t for r in rows for t in r["ended_tasks"] if t["id"] == "legacy-request")
        self.assertEqual(ended["end_reason"], f"superseded by task {self.current_task_id()}")
        self.assertEqual(ended["last_state"], "running")

    def test_same_sandbox_pid_with_new_identity_is_superseded(self):
        sy.register_project(self.project)
        sy.write_status_stub(self.project, 2, "first sandboxed run")
        status = json.loads(sy.status_path(self.project).read_text())
        status["process_identity"] = status["tasks"][0]["process_identity"] = sy.portfolio.boot_id() + ":100"
        sy.status_path(self.project).write_text(json.dumps(status))
        sy.write_status_stub(self.project, 2, "second sandboxed run")
        status = json.loads(sy.status_path(self.project).read_text())
        status["process_identity"] = status["tasks"][1]["process_identity"] = sy.portfolio.boot_id() + ":200"
        status["process_namespace"] = status["tasks"][1]["process_namespace"] = "pid:[sandbox]"
        sy.status_path(self.project).write_text(json.dumps(status))
        row = sy.gather_statuses()[0]
        self.assertEqual([t["summary"] for t in row["ended_tasks"]], ["first sandboxed run"])
        self.assertEqual(row["tasks"][0]["liveness"], "unknown")

    def test_superseded_waiting_task_ends_but_current_question_is_kept(self):
        sy.register_project(self.project)
        sy.write_status_stub(self.project, 2**30, "needs an answer")
        status = json.loads(sy.status_path(self.project).read_text())
        status["state"] = "waiting_on_user"
        sy.status_path(self.project).write_text(json.dumps(status))
        decision = sy.decision_store().request(source_path=str(self.project), project_path=str(self.project),
                                               task_id=status["current"]["task_id"], key="q", question="Which?")
        store = sy.coordination_store()
        store.end_session(store.start_session(str(self.project), status["current"]["task_id"], "codex", os.getpid())["id"])
        row = sy.gather_statuses()[0]
        self.assertEqual(row["tasks"][0]["state"], "waiting_on_user")  # current question kept after session end
        self.assertFalse(self.edits_path().exists())
        sy.write_status_stub(self.project, os.getpid(), "new work")
        ended = sy.gather_statuses()[0]["ended_tasks"][0]
        self.assertEqual((ended["last_state"], ended["state"]), ("waiting_on_user", "ended"))
        self.assertEqual(sy.decision_store().read(), [decision])  # decision inbox untouched

    def test_manual_state_edit_wins_and_ended_is_an_edit_choice(self):
        sy.register_project(self.project)
        sy.write_status_stub(self.project, 2**30, "first")
        first = self.current_task_id()
        sy.write_status_stub(self.project, os.getpid(), "second")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sy.main(["tasks", "edit", first, "--state", "done"]), 0)
        row = sy.gather_statuses()[0]
        self.assertEqual(row["last_completed"]["id"], first)
        self.assertEqual(row["ended_tasks"], [])
        second = self.current_task_id()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sy.main(["tasks", "edit", second, "--state", "ended"]), 0)
        ended = sy.gather_statuses()[0]["ended_tasks"][0]
        self.assertEqual((ended["id"], ended["end_reason"], ended["last_state"]), (second, "closed manually", "running"))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(sy.main(["tasks", "list"]), 0)
        self.assertIn("[ended]", output.getvalue())
        self.assertIn("ended: closed manually (was running)", output.getvalue())

    def foreign_owner(self):
        """Owner recorded in another PID namespace (e.g. launched inside a sandbox)."""
        sy.register_project(self.project)
        proc, identity = spawn_owner()
        reap(proc)
        sy.write_status_stub(self.project, proc.pid, "sandboxed work")
        task_id = self.current_task_id()
        status = json.loads(sy.status_path(self.project).read_text())
        status.update(process_identity=identity, process_namespace="pid:[1]")
        sy.status_path(self.project).write_text(json.dumps(status))
        store = sy.coordination_store()
        store.start_session(str(self.project), task_id, "codex", os.getpid())
        store.send_request(source_path=str(self.project), project_path=str(self.project),
                           task_id=task_id, key="k", message="Review")
        data = store.read()
        data["sessions"][0].update(pid=proc.pid, process_identity=identity, process_namespace="pid:[1]")
        sy.runtime.atomic_json(store.path, data)
        return store

    def assert_unknown_without_writes(self, store):
        before = store.path.read_bytes()
        row = sy.gather_statuses()[0]
        self.assertEqual((row["state"], row["liveness"], row["project_status"]), ("running", "unknown", "Working"))
        self.assertEqual(row["tasks"][0]["liveness"], "unknown")
        self.assertFalse(self.edits_path().exists())
        self.assertEqual(store.path.read_bytes(), before)
        briefing = sy.project_briefing()
        self.assertEqual(briefing["sessions"][0]["liveness"], "unknown")
        self.assertEqual(briefing["requests"][0]["delivery_state"], "unverified")
        args = sy.build_parsers()[1].parse_args(["dashboard", "--once", "--details", "--color", "never"])
        with unittest.mock.patch("sys.stdout", new=io.StringIO()) as output:
            self.assertEqual(sy.cmd_dashboard(args), 0)
        text = output.getvalue()
        self.assertNotIn("stale", text)
        self.assertIn("running (unverified)", text)
        self.assertIn("unverified", text)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(sy.main(["briefing"]), 0)
            self.assertEqual(sy.main(["sessions"]), 0)
        self.assertIn("[unverified]", output.getvalue())
        self.assertEqual(store.path.read_bytes(), before)

    def test_foreign_namespace_is_unknown_never_stale_and_writes_nothing(self):
        store = self.foreign_owner()
        self.assert_unknown_without_writes(store)
        with self.assertRaisesRegex(sy.portfolio.PortfolioError, "PID namespace"):
            store.snapshot()  # ownership commands keep their explicit error

    def test_unreadable_proc_is_unknown_and_writes_nothing(self):
        store = self.foreign_owner()
        with unittest.mock.patch.object(sy.portfolio, "process_namespace", return_value=None):
            self.assert_unknown_without_writes(store)

    def test_lock_released_proves_exit_from_another_namespace(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        args = sy.build_parsers()[0].parse_args(["--project", str(self.project), "locked task"])
        with unittest.mock.patch.object(sy, "run_session", return_value=0):
            sy.cmd_run(args)
        import fcntl
        with unittest.mock.patch.object(sy.portfolio, "process_namespace", return_value="pid:[sandbox]"):
            with (self.project / ".syndicate" / "session.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                self.assertEqual(sy.gather_statuses()[0]["tasks"][0]["liveness"], "unknown")
                self.assertFalse(self.edits_path().exists())
            ended = sy.gather_statuses()[0]["ended_tasks"][0]
        self.assertEqual(ended["end_reason"], "owner process exited (project lock released)")

    @unittest.skipUnless(Path("/proc").is_dir(), "needs /proc process identities")
    def test_held_project_lock_defers_closing_the_current_task(self):
        import fcntl
        sy.register_project(self.project)
        proc, _ = spawn_owner()
        sy.write_status_stub(self.project, proc.pid, "relaunching")
        sy.coordination_store().start_session(str(self.project), self.current_task_id(), "codex", proc.pid)
        reap(proc)
        with (self.project / ".syndicate" / "session.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            row = sy.gather_statuses()[0]
            self.assertEqual(row["tasks"][0]["liveness"], "unknown")
            self.assertEqual(sy.coordination_store().read()["sessions"][0]["state"], "active")
        self.assertEqual(sy.gather_statuses()[0]["ended_tasks"][0]["end_reason"], "owner process exited")

    # --- Safe lock handling (FIFO, symlinks, replaced inode) -----------------

    def run_with_timeout(self, fn, seconds=10):
        """Run fn in a daemon thread; fail (instead of hanging the suite) if it blocks."""
        result = {}

        def target():
            try:
                result["value"] = fn()
            except BaseException as error:  # surfaced to the test below
                result["error"] = error
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(seconds)
        self.assertFalse(thread.is_alive(), f"{fn} blocked for more than {seconds}s")
        if "error" in result:
            raise result["error"]
        return result.get("value")

    def locked_launch(self):
        """A completed cmd_run whose recorded launcher identity is unverifiable
        (as from a sandbox), so only lock and CLI-child evidence remains."""
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        args = sy.build_parsers()[0].parse_args(["--project", str(self.project), "locked task"])
        with unittest.mock.patch.object(sy, "run_session", return_value=0):
            sy.cmd_run(args)
        status = json.loads(sy.status_path(self.project).read_text())
        status["process_namespace"] = status["tasks"][-1]["process_namespace"] = "pid:[sandbox]"
        sy.status_path(self.project).write_text(json.dumps(status))
        return status

    def test_fifo_session_lock_never_blocks_sweeps_listing_or_launch(self):
        self.locked_launch()
        lock_path = self.project / ".syndicate" / "session.lock"
        lock_path.unlink()
        os.mkfifo(lock_path)
        self.assertEqual(self.run_with_timeout(lambda: sy.portfolio.probe_lock(lock_path)), "unknown")
        row = self.run_with_timeout(sy.gather_statuses)[0]
        self.assertEqual((row["tasks"][0]["state"], row["tasks"][0]["liveness"]), ("running", "unknown"))
        self.assertFalse(self.edits_path().exists())

        def listing():
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(sy.main(["tasks", "list"]), 0)
            return output.getvalue()
        self.assertIn("[running (unverified)]", self.run_with_timeout(listing))
        self.run_with_timeout(sy.reconcile_stale)
        args = sy.build_parsers()[0].parse_args(["--project", str(self.project), "next"])
        with unittest.mock.patch.object(sy, "run_session", return_value=0) as run:
            with self.assertRaisesRegex(sy.SyndicateError, "not a regular file"):
                self.run_with_timeout(lambda: sy.cmd_run(args))
        run.assert_not_called()
        self.assertFalse(self.edits_path().exists())

    def test_symlinked_session_lock_is_refused_and_its_target_never_opened_for_write(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        victim = Path(self._tmp.name) / "bashrc"
        victim.write_text("export SAFE=1\n")
        victim.chmod(0o644)
        before = (victim.read_bytes(), victim.stat().st_mtime_ns, victim.stat().st_mode)
        lock_path = self.project / ".syndicate" / "session.lock"
        lock_path.symlink_to(victim)
        opened, real_open = [], os.open

        def tracking_open(path, flags, *rest, **kwargs):
            opened.append((os.fspath(path), flags))
            return real_open(path, flags, *rest, **kwargs)
        writes = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_TRUNC
        args = sy.build_parsers()[0].parse_args(["--project", str(self.project), "task"])
        with unittest.mock.patch.object(os, "open", side_effect=tracking_open), \
             unittest.mock.patch.object(sy, "run_session", return_value=0) as run:
            with self.assertRaisesRegex(sy.SyndicateError, "symbolic link"):
                sy.cmd_run(args)
            self.assertEqual(sy.portfolio.probe_lock(lock_path), "unknown")
        run.assert_not_called()
        lock_opens = [(path, flags) for path, flags in opened if path in ("session.lock", str(lock_path), str(victim))]
        self.assertTrue(lock_opens)
        self.assertFalse([entry for entry in lock_opens if entry[1] & writes])
        self.assertTrue(all(flags & os.O_NOFOLLOW for path, flags in lock_opens))
        self.assertEqual((victim.read_bytes(), victim.stat().st_mtime_ns, victim.stat().st_mode), before)
        self.assertTrue(lock_path.is_symlink())

    def test_symlinked_syndicate_directory_is_refused(self):
        project = self.project / "linked"
        project.mkdir()
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        elsewhere = self.project / "elsewhere"
        elsewhere.mkdir()
        (project / ".syndicate").symlink_to(elsewhere, target_is_directory=True)
        args = sy.build_parsers()[0].parse_args(["--project", str(project), "task"])
        with unittest.mock.patch.object(sy, "run_session", return_value=0) as run:
            with self.assertRaisesRegex(sy.SyndicateError, "symbolic link"):
                sy.cmd_run(args)
        run.assert_not_called()
        self.assertFalse((elsewhere / "session.lock").exists())
        (elsewhere / "session.lock").touch()
        self.assertEqual(sy.portfolio.probe_lock(project / ".syndicate" / "session.lock"), "unknown")

    def test_hard_linked_or_foreign_lock_is_refused(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        lock_path = self.project / ".syndicate" / "session.lock"
        lock_path.touch()
        os.link(lock_path, self.project / "second-name")
        args = sy.build_parsers()[0].parse_args(["--project", str(self.project), "task"])
        with unittest.mock.patch.object(sy, "run_session", return_value=0):
            with self.assertRaisesRegex(sy.SyndicateError, "hard links"):
                sy.cmd_run(args)
        self.assertEqual(sy.portfolio.probe_lock(lock_path), "unknown")
        (self.project / "second-name").unlink()
        with unittest.mock.patch.object(sy.portfolio.os, "getuid", return_value=os.getuid() + 1):
            self.assertEqual(sy.portfolio.probe_lock(lock_path), "unknown")
            with self.assertRaisesRegex(sy.SyndicateError, "not owned by the current user"):
                sy.cmd_run(args)

    def test_replaced_lock_inode_is_unknown_not_released(self):
        status = self.locked_launch()
        lock_path = self.project / ".syndicate" / "session.lock"
        self.assertEqual(status["lock_inode"], [lock_path.stat().st_dev, lock_path.stat().st_ino])
        replacement = self.project / ".syndicate" / "replacement"
        replacement.touch()  # created while the original exists: a distinct inode
        os.replace(replacement, lock_path)
        self.assertNotEqual(status["lock_inode"][1], lock_path.stat().st_ino)
        self.assertEqual(sy.portfolio.probe_lock(lock_path), "free")
        self.assertEqual(sy.portfolio.probe_lock(lock_path, expect=status["lock_inode"]), "unknown")
        row = sy.gather_statuses()[0]
        self.assertEqual((row["tasks"][0]["state"], row["tasks"][0]["liveness"]), ("running", "unknown"))
        self.assertFalse(self.edits_path().exists())

    @unittest.skipUnless(Path("/proc").is_dir(), "needs /proc process identities")
    def test_supervisor_crash_free_lock_defers_to_the_recorded_cli_child(self):
        self.locked_launch()
        child, _ = spawn_owner()
        self.addCleanup(reap, child)
        status = json.loads(sy.status_path(self.project).read_text())
        status["cli_owner"] = status["tasks"][-1]["cli_owner"] = sy.owner_identity(child.pid)
        sy.status_path(self.project).write_text(json.dumps(status))
        task = sy.gather_statuses()[0]["tasks"][0]
        self.assertEqual((task["state"], task["liveness"]), ("running", "alive"))  # lock free, child alive
        self.assertFalse(self.edits_path().exists())
        foreign = {**status["cli_owner"], "process_namespace": "pid:[other]"}
        sy.status_path(self.project).write_text(json.dumps({**status, "cli_owner": foreign}))
        self.assertEqual(sy.gather_statuses()[0]["tasks"][0]["liveness"], "unknown")  # child unverifiable
        self.assertFalse(self.edits_path().exists())
        sy.status_path(self.project).write_text(json.dumps(status))
        reap(child)
        ended = sy.gather_statuses()[0]["ended_tasks"][0]
        self.assertEqual(ended["end_reason"], "owner process exited (project lock released)")

    @unittest.skipUnless(Path("/proc").is_dir(), "needs /proc process identities")
    def test_supervisor_crash_keeps_coordination_session_while_cli_child_lives(self):
        sy.register_project(self.project)
        launcher, _ = spawn_owner()
        child, _ = spawn_owner()
        self.addCleanup(reap, child)
        sy.write_status_stub(self.project, launcher.pid, "supervised")
        task_id = self.current_task_id()
        store = sy.coordination_store()
        session = store.start_session(str(self.project), task_id, "codex", launcher.pid)
        store.send_request(source_path=str(self.project), project_path=str(self.project),
                           task_id=task_id, key="k", message="Review")
        sy.record_cli_owner(self.project, session, child.pid)
        status = json.loads(sy.status_path(self.project).read_text())
        status["current"]["session_id"] = session["id"]
        sy.status_path(self.project).write_text(json.dumps(status))
        reap(launcher)  # the supervisor crashed; its CLI child survives
        row = sy.gather_statuses()[0]
        self.assertEqual(row["tasks"][0]["liveness"], "alive")
        self.assertEqual(store.read()["sessions"][0]["state"], "active")
        self.assertEqual(store.read()["requests"][0]["state"], "queued")
        status["cli_owner"] = None  # even if the agent drops its copy, the session's copy counts
        sy.status_path(self.project).write_text(json.dumps(status))
        self.assertEqual(sy.gather_statuses()[0]["tasks"][0]["liveness"], "alive")
        with self.assertRaisesRegex(sy.SyndicateError, rf"may still run.*ps -o pid,args -p {child.pid}"):
            sy.cmd_run(sy.build_parsers()[0].parse_args(["--project", str(self.project), "again"]))
        reap(child)
        row = sy.gather_statuses()[0]
        self.assertEqual(row["ended_tasks"][0]["end_reason"], "owner process exited")
        self.assertEqual(store.read()["sessions"][0]["state"], "ended")
        self.assertEqual(store.read()["requests"][0]["state"], "failed")

    @unittest.skipUnless(Path("/proc").is_dir(), "needs /proc process identities")
    def test_forged_status_cli_owner_does_not_block_launch_but_store_copy_does(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        sy.register_project(self.project)
        victim, _ = spawn_owner()  # any live process an agent could name
        self.addCleanup(reap, victim)
        sy.write_status_stub(self.project, 2**30, "agent-written")
        status = json.loads(sy.status_path(self.project).read_text())
        status["cli_owner"] = status["tasks"][-1]["cli_owner"] = sy.owner_identity(victim.pid)
        sy.status_path(self.project).write_text(json.dumps(status))
        parse = sy.build_parsers()[0].parse_args
        with unittest.mock.patch.object(sy, "run_session", return_value=0) as run:
            self.assertEqual(sy.cmd_run(parse(["--project", str(self.project), "launch"])), 0)
        run.assert_called_once()
        # A child recorded by the launcher in the coordination store still refuses.
        launcher, _ = spawn_owner()
        store = sy.coordination_store()
        session = store.start_session(str(self.project), self.current_task_id(), "codex", launcher.pid)
        store.record_cli_owner(session["id"], sy.owner_identity(victim.pid))
        reap(launcher)
        with unittest.mock.patch.object(sy, "run_session", return_value=0) as run:
            with self.assertRaisesRegex(sy.SyndicateError, rf"ls -l /proc/{victim.pid}/cwd"):
                sy.cmd_run(parse(["--project", str(self.project), "again"]))
        run.assert_not_called()

    def owner_write_fails(self, store_fails):
        sy.write_status_stub(self.project, os.getpid(), "supervised")
        status = json.loads(sy.status_path(self.project).read_text())
        status["lock_owned"] = status["tasks"][-1]["lock_owned"] = True
        sy.status_path(self.project).write_text(json.dumps(status))
        session = sy.coordination_store().start_session(str(self.project), self.current_task_id(), "codex", os.getpid())
        real, writes = sy.runtime.atomic_json, []

        def flaky(*args, **kwargs):
            if Path(args[0]) == sy.status_path(self.project):
                writes.append(args[0])
                if len(writes) == 1:
                    raise OSError("read-only")  # only the first status write fails
            return real(*args, **kwargs)
        failing = unittest.mock.patch.object(sy.portfolio.CoordinationStore, "record_cli_owner",
                                             side_effect=sy.portfolio.PortfolioError("unavailable"))
        with (failing if store_fails else contextlib.nullcontext()), \
             unittest.mock.patch.object(sy.runtime, "atomic_json", side_effect=flaky), \
             contextlib.redirect_stderr(io.StringIO()):
            sy.record_cli_owner(self.project, session, os.getpid())
        store = sy.coordination_store()
        recorded = next(s for s in store.read()["sessions"] if s["id"] == session["id"])
        store.end_session(session["id"])
        return json.loads(sy.status_path(self.project).read_text()), recorded

    def test_unrecorded_cli_owner_withdraws_the_lock_as_proof_of_death(self):
        status, session = self.owner_write_fails(store_fails=True)
        self.assertNotIn("cli_owner", session)
        for record in (status, status["tasks"][-1]):
            self.assertIs(record["lock_owned"], False)
            self.assertIsNone(record["cli_owner"])
        # One stored copy is enough: the status keeps its lock evidence.
        status, session = self.owner_write_fails(store_fails=False)
        self.assertEqual(session["cli_owner"]["pid"], os.getpid())
        self.assertIs(status["lock_owned"], True)

    # --- Handoff, resume, and claimed tasks ---------------------------------

    def test_handoff_exit_keeps_the_task_resumable(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        self.isolate_launch()
        handoff = self.project / ".syndicate" / "handoffs" / "run"

        def supervised(*_, **__):
            return 75, handoff  # status left 'running', as when no handoff state was written
        args = sy.build_parsers()[0].parse_args(["--project", str(self.project), "-p", "interrupted work"])
        with unittest.mock.patch.object(sy.runtime, "supervise", side_effect=supervised), \
             unittest.mock.patch.object(sy.runtime, "choose_target", return_value=None), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sy.cmd_run(args), 75)
        status = json.loads(sy.status_path(self.project).read_text())
        self.assertEqual(status["state"], "running")
        self.assertNotIn("end_reason", status["tasks"][-1])
        self.assertEqual(sy.coordination_store().read()["sessions"][0]["state"], "ended")
        sy.write_status_stub(self.project, os.getpid(), "direct")
        task_id = self.current_task_id()
        with sy.project_session(self.project, "codex", {"handoff": True}):
            pass
        status = json.loads(sy.status_path(self.project).read_text())
        self.assertEqual((status["state"], status["current"]["task_id"]), ("running", task_id))
        self.assertNotIn("end_reason", status["tasks"][-1])

    def test_resume_clears_end_fields_of_the_resumed_task(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        sy.write_status_stub(self.project, os.getpid(), "resumable")
        status = json.loads(sy.status_path(self.project).read_text())
        status["state"] = "ended"
        status["tasks"][-1].update(state="ended", last_state="running", ended_at="2026-01-01T00:00:00Z",
                                   end_reason="session exited")
        sy.status_path(self.project).write_text(json.dumps(status))
        handoff = self.project / ".syndicate" / "handoffs" / "run"
        handoff.mkdir(parents=True)
        (handoff / "status.json").write_text(json.dumps(status))
        args = sy.build_parsers()[0].parse_args(["--project", str(self.project), "--handoff", str(handoff)])
        with unittest.mock.patch.object(sy, "run_session", return_value=0):
            self.assertEqual(sy.cmd_run(args), 0)
        after = json.loads(sy.status_path(self.project).read_text())
        task = after["tasks"][-1]
        self.assertEqual((after["state"], task["state"], task["id"]), ("running", "running", status["tasks"][-1]["id"]))
        for key in ("ended_at", "end_reason", "last_state"):
            self.assertNotIn(key, task)

    def test_superseded_closure_does_not_stick_once_the_current_session_claims_the_task(self):
        sy.register_project(self.project)
        sy.write_status_stub(self.project, 2**30, "first")
        first = self.current_task_id()
        sy.write_status_stub(self.project, os.getpid(), "second")
        self.assertEqual(sy.gather_statuses()[0]["ended_tasks"][0]["id"], first)
        store = sy.coordination_store()
        session = store.start_session(str(self.project), self.current_task_id(), "codex", os.getpid())
        status = json.loads(sy.status_path(self.project).read_text())
        status["current"]["session_id"] = session["id"]
        status["tasks"][0].update(state="running", session_id=session["id"])  # the owner resumes it
        sy.status_path(self.project).write_text(json.dumps(status))
        row = sy.gather_statuses()[0]
        claimed = next(t for t in row["tasks"] if t["id"] == first)
        self.assertEqual((claimed["state"], claimed["liveness"]), ("running", "alive"))
        self.assertEqual(row["ended_tasks"], [])
        edits = json.loads(self.edits_path().read_text())[str(self.project)][first]
        self.assertEqual(edits["auto_end"]["owner"]["session_id"], None)  # only the old owner's closure
        store.end_session(session["id"])  # the claiming session ends: the task closes for it
        reclosed = sy.gather_statuses()[0]["ended_tasks"]
        self.assertIn(first, [t["id"] for t in reclosed])

    # --- Sweep scheduling and read-only views ---------------------------------

    def test_cmd_task_sweeps_once_for_many_live_sessions(self):
        store = sy.coordination_store()
        for name in ("one", "two", "three"):
            store.start_session(str(self.project / name), f"task-{name}", "codex", os.getpid())
        calls = []
        real = sy.gather_statuses

        def counted(*args, **kwargs):
            calls.append(kwargs.get("sweep", True))
            return real(*args, **kwargs)
        args = argparse.Namespace(project=str(self.project))
        with unittest.mock.patch.object(sy, "gather_statuses", side_effect=counted), \
             unittest.mock.patch.object(sy, "execution_project", return_value=self.project), \
             unittest.mock.patch.object(sy, "cmd_run", return_value=0) as run:
            self.assertEqual(sy.cmd_task(args), 0)
        run.assert_called_once()
        self.assertEqual(calls, [True])

    def test_tmux_prelaunch_task_check_sweeps_once_for_many_live_sessions(self):
        store = sy.coordination_store()
        for name in ("one", "two", "three"):
            store.start_session(str(self.project / name), f"task-{name}", "codex", os.getpid())
        calls = []
        real = sy.gather_statuses

        def counted(*args, **kwargs):
            calls.append(kwargs.get("sweep", True))
            return real(*args, **kwargs)
        with unittest.mock.patch.object(sy, "gather_statuses", side_effect=counted), \
             unittest.mock.patch.object(sy, "tmux_enabled", return_value=True), \
             unittest.mock.patch.object(sy, "execution_project", return_value=self.project), \
             unittest.mock.patch.object(sy, "launch_tmux", return_value=0) as launch:
            self.assertEqual(sy.main(["team", "task", "--project", str(self.project), "goal"]), 0)
        launch.assert_called_once()
        self.assertEqual(calls, [True])

    def test_failed_sweep_keeps_assignments_and_never_launches_a_duplicate_team(self):
        other = self.project / "source"
        task = dict(id="task-x", source_path=str(other))
        row = dict(path=str(self.project), tasks=[task], completed_tasks=[], ended_tasks=[])
        sy.coordination_store().start_session(str(other), "task-x", "codex", os.getpid())
        calls = []

        def gather(*args, sweep=True):
            calls.append(sweep)
            if sweep:
                raise OSError("sweep failed")
            return [row]
        with unittest.mock.patch.object(sy, "gather_statuses", side_effect=gather), \
             unittest.mock.patch.object(sy, "execution_project", return_value=self.project), \
             unittest.mock.patch.object(sy, "tmux_enabled", return_value=True), \
             unittest.mock.patch.object(sy, "launch_tmux", return_value=0) as launch, \
             unittest.mock.patch.object(sy, "cmd_run", return_value=0) as run, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(sy.cmd_task(argparse.Namespace(project=str(self.project))), 0)
            self.assertEqual(sy.main(["team", "task", "--project", str(self.project), "goal"]), 0)
        run.assert_not_called()
        launch.assert_not_called()
        self.assertEqual(out.getvalue().count("Team already running"), 2)
        self.assertEqual(calls, [True, False, True, False])  # read-only fallback after each failed sweep

    def test_generic_group_liveness_reflects_every_member(self):
        live, foreign = self.project / "live", self.project / "foreign"
        for source in (live, foreign):
            source.mkdir()
            sy.register_project(source, generic=True)
        sy.write_status_stub(live, os.getpid(), "verified")
        sy.write_status_stub(foreign, os.getpid(), "sandboxed")
        status = json.loads(sy.status_path(foreign).read_text())
        status["process_namespace"] = status["tasks"][0]["process_namespace"] = "pid:[sandbox]"
        status["pid"] = status["tasks"][0]["pid"] = 2
        sy.status_path(foreign).write_text(json.dumps(status))
        group = next(r for r in sy.gather_statuses() if r["name"] == "Generic tasks")
        self.assertEqual(len(group["tasks"]), 2)
        self.assertEqual(group["liveness"], "unknown")
        status["process_namespace"] = status["tasks"][0]["process_namespace"] = sy.portfolio.process_namespace()
        status.update(sy.owner_identity(os.getpid()))
        status["tasks"][0].update(sy.owner_identity(os.getpid()))
        sy.status_path(foreign).write_text(json.dumps(status))
        group = next(r for r in sy.gather_statuses() if r["name"] == "Generic tasks")
        self.assertNotIn("liveness", group)

    @unittest.skipUnless(Path("/proc").is_dir(), "needs /proc process identities")
    def test_web_get_handlers_compute_verdicts_without_writing(self):
        sy.register_project(self.project)
        proc, _ = spawn_owner()
        sy.write_status_stub(self.project, proc.pid, "abrupt")
        task_id = self.current_task_id()
        store = sy.coordination_store()
        store.start_session(str(self.project), task_id, "codex", proc.pid)
        store.send_request(source_path=str(self.project), project_path=str(self.project),
                           task_id=task_id, key="k", message="Review")
        reap(proc)
        before = (store.path.read_bytes(), sy.status_path(self.project).read_bytes())
        server = http.server.HTTPServer(("127.0.0.1", 0), sy._DashboardHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        responses = {}
        for path in ("/api/status", "/api/briefing"):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            conn.request("GET", path)
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            responses[path] = json.loads(response.read())
            conn.close()
        self.assertEqual(responses["/api/status"][0]["ended_tasks"][0]["id"], task_id)  # verdict shown
        self.assertEqual(responses["/api/briefing"]["sessions"][0]["liveness"], "dead")
        self.assertFalse(self.edits_path().exists())
        self.assertEqual((store.path.read_bytes(), sy.status_path(self.project).read_bytes()), before)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sy.main(["tasks", "list"]), 0)  # the terminal listing still sweeps
        self.assertTrue(self.edits_path().exists())
        self.assertEqual(store.read()["sessions"][0]["state"], "ended")



class WebDashboard(StatusAndRegistry):
    def test_browser_views_filter_escape_and_completion(self):
        if not sy.shutil.which("node"):
            self.skipTest("Node is required for dashboard rendering checks")
        script = (sy.WEB_DIR / "dashboard.html").read_text().split("<script>")[1].split("</script>")[0]
        harness = """
const assert = require('node:assert/strict');
const elements = {};
global.document = {
 getElementById: id => elements[id] ||= {value:id==='filter'?'all':'',innerHTML:'',textContent:'',addEventListener(){}},
 querySelectorAll: () => []
};
global.location = {hash:''};
global.window = {addEventListener(){}};
global.fetch = () => new Promise(()=>{});
global.setInterval = () => 0;
"""
        checks = """
portfolioData={decisions:[],next_action:'Review projects',chief:{state:'waiting',provider:'codex',report_state:'waiting_on_user',status_updated_at:null,reason:'Saved <script>',checkpoint:{state:'available',summary:'Checkpoint <script>',updated_at:'2026-11-08',truncated:true}}};
renderBriefing('/unrelated-project');
assert.ok($('briefing').innerHTML.includes('waiting'));
assert.ok($('briefing').innerHTML.includes('Checkpoint &lt;script&gt;'));
assert.ok(!$('briefing').innerHTML.includes('<script>'));
assert.ok($('briefing').innerHTML.includes('excerpt truncated'));
portfolioData=null;
rows = [{name:'<repo>',path:'/tmp/a',project_status:'Complete',unfinished_count:0,tasks:[],
 last_completed:{summary:'Finished <script>',state:'done'},history:[]}];
render();
assert.ok($('content').innerHTML.includes('Finished &lt;script&gt;'));
assert.ok(!$('content').innerHTML.includes('<repo>'));
$('filter').value='unfinished';render();
assert.ok($('content').innerHTML.includes('No projects match'));
location.hash='#'+encodeURIComponent('/tmp/a');render();
assert.ok($('content').innerHTML.includes('Last completed task'));
rows[0].tasks=[{summary:'Pending',state:'queued'}];render();
assert.ok($('content').innerHTML.includes('Unfinished tasks (1)'));
assert.ok(!$('content').innerHTML.includes('Finished &lt;script&gt;'));
location.hash='';$('filter').value='all';list=true;render();
assert.ok(!$('content').innerHTML.includes('Current / latest task'));
portfolioData={next_action:'Answer <question>',decisions:[{id:'abc',project_path:'/tmp/a',
 question:'Question <script>',state:'answered',context:'Context <script>',options:['A','B'],
 recommendation:'A',answer:'Use <A>',updated_at:'2026-01-01'}]};
render();
assert.ok($('briefing').innerHTML.includes('Awaiting team acknowledgment'));
assert.ok($('briefing').innerHTML.includes('Question &lt;script&gt;'));
assert.ok($('briefing').innerHTML.includes('Use &lt;A&gt;'));
assert.ok(!$('briefing').innerHTML.includes('<script>'));
location.hash='#'+encodeURIComponent('/tmp/other');render();
assert.ok(!$('briefing').innerHTML.includes('Question &lt;script&gt;'));
location.hash='';
portfolioData.requests=[{id:'request',task_id:'task',project_path:'/tmp/a',message:'Please <review>',
 state:'failed',delivery_state:'failed',note:'Needs <approval>',updated_at:'2026-01-01'}];
portfolioData.sessions=[{id:'owner',task_id:'task',source_path:'/tmp/a',provider:'codex',pid:123,live:false}];
portfolioData.projects=[{name:'repo',path:'/tmp/a',sessions:portfolioData.sessions,brief:{goal:'Ship <it>',priority:'high',
 revision:1,constraints:['Offline'],decisions:[],dependencies:['Other <repo>'],next_steps:['Review'],updated_at:'2026-01-01'}}];
render();
assert.ok($('coordination').innerHTML.includes('Please &lt;review&gt;'));
assert.ok($('coordination').innerHTML.includes('Needs &lt;approval&gt;'));
assert.ok($('coordination').innerHTML.includes('Ship &lt;it&gt;'));
assert.ok($('coordination').innerHTML.includes('Other &lt;repo&gt;'));
assert.ok($('coordination').innerHTML.includes('inactive'));
location.hash='#'+encodeURIComponent('/tmp/other');render();
assert.ok(!$('coordination').innerHTML.includes('Please &lt;review&gt;'));
// Reassigned projects can have one current task per source; attention order is not current ownership.
location.hash='';list=false;
rows=[{name:'grouped',path:'/assigned',project_status:'Needs attention',unfinished_count:2,history:[],tasks:[
 {id:'same-id',source_path:'/old',is_current:true,state:'stale',summary:'Old source interrupted'},
 {id:'same-id',source_path:'/live',is_current:true,state:'running',summary:'Live Codex task'}]}];
portfolioData={next_action:'Review',decisions:[],requests:[],sessions:[],projects:[
 {path:'/assigned',task_id:'same-id',source_path:'/live'}]};
render();
assert.ok($('content').innerHTML.includes('Live Codex task'));
assert.ok(!$('content').innerHTML.includes('Old source interrupted'));
assert.equal(currentTask(rows[0]).source_path,'/live');
// If briefing data is unavailable, prefer running work among multiple reported-current tasks.
portfolioData=null;render();
assert.ok($('content').innerHTML.includes('Live Codex task'));
// Ended tasks leave the unfinished list but stay visible as history; unverified owners are labeled.
rows=[{name:'repo',path:'/tmp/e',project_status:'Working',unfinished_count:1,history:[],last_completed:null,
 tasks:[{id:'live',state:'running',liveness:'unknown',summary:'Sandboxed run',is_current:true}],
 ended_tasks:[{id:'gone',state:'ended',last_state:'running',end_reason:'owner <process> exited',summary:'Abrupt run'}]}];
portfolioData={next_action:'Review',decisions:[],requests:[],projects:[],
 sessions:[{id:'s',task_id:'live',source_path:'/tmp/e',provider:'codex',pid:1,live:false,liveness:'unknown'}]};
render();
assert.ok($('content').innerHTML.includes('running (unverified)'));
assert.ok($('coordination').innerHTML.includes('unverified'));
location.hash='#'+encodeURIComponent('/tmp/e');render();
assert.ok($('content').innerHTML.includes('Unfinished tasks (1)'));
assert.ok($('content').innerHTML.includes('Ended without completion (1)'));
assert.ok($('content').innerHTML.includes('owner &lt;process&gt; exited'));
assert.ok($('content').innerHTML.includes('was running'));
location.hash='';



"""
        subprocess.run(["node"], input=harness + script + checks, text=True, check=True, capture_output=True)

    def _start_server(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), sy._DashboardHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return server.server_address[1]

    def test_api_status_serves_gather_statuses(self):
        sy.register_project(self.project)
        sy.write_status_stub(self.project, os.getpid(), "hello from the web test")
        port = self._start_server()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/status")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "application/json")
        rows = json.loads(resp.read())
        self.assertEqual(rows[0]["current"]["summary"], "hello from the web test")
        conn.close()

    def test_briefing_endpoint_and_corrupt_inbox(self):
        port = self._start_server()
        source = str(self.project)
        record = sy.decision_store().request(source_path=source, project_path=source,
                    task_id="task", key="database", question="Which database?", options=["SQLite"])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/briefing")
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        data = json.loads(response.read())
        self.assertEqual(data["decisions"][0]["id"], record["id"])
        self.assertIn("Answer decision", data["next_action"])
        conn.close()
        sy.decision_store().path.write_text("corrupt")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/briefing")
        response = conn.getresponse()
        self.assertEqual(response.status, 503)
        self.assertIn("error", json.loads(response.read()))
        conn.close()

    def test_coordination_in_briefing_endpoint(self):
        sy.register_project(self.project)
        sy.write_status_stub(self.project, os.getpid(), "Project work")
        task_id = json.loads(sy.status_path(self.project).read_text())["current"]["task_id"]
        store = sy.coordination_store()
        store.update_brief(str(self.project), {"goal": "Ship", "priority": "high"})
        owner = store.start_session(str(self.project), task_id, "codex", os.getpid())
        request = store.send_request(source_path=str(self.project), project_path=str(self.project),
                                    task_id=task_id, key="review", message="Review release")
        port = self._start_server()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/briefing")
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        data = json.loads(response.read())
        self.assertEqual(data["projects"][0]["brief"]["goal"], "Ship")
        self.assertEqual(data["sessions"][0]["id"], owner["id"])
        self.assertTrue(data["sessions"][0]["live"])
        self.assertEqual(data["requests"][0]["id"], request["id"])
        self.assertEqual(data["requests"][0]["delivery_state"], "awaiting_acknowledgment")
        conn.close()
        store.path.write_text("corrupt")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/briefing")
        response = conn.getresponse()
        self.assertEqual(response.status, 503)
        conn.close()

    def test_limits_endpoint_starts_monitor_and_returns_latest_observation(self):
        with unittest.mock.patch.object(sy.runtime, "start_monitor") as start, \
             unittest.mock.patch.object(sy.runtime, "describe_limits", return_value="codex: current, 94% remaining"):
            port = self._start_server()
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/limits")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("94% remaining", json.loads(response.read())["summary"])
            start.assert_called_once_with(sy.SCRIPT)
            conn.close()

    def test_index_serves_dashboard_html(self):
        port = self._start_server()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertIn(b"syndicate", resp.read())
        conn.close()

    def test_unknown_path_is_404(self):
        port = self._start_server()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/nope")
        self.assertEqual(conn.getresponse().status, 404)
        conn.close()


if __name__ == "__main__":
    unittest.main()
