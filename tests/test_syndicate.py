import importlib.machinery
import importlib.util
import json
import subprocess
import sys
import tempfile
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
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name).resolve()
        (self.project / ".syndicate" / "roles").mkdir(parents=True)

    def config(self, text):
        (self.project / ".syndicate" / "config").write_text(text)

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
        self.config("review-gate = off\n")
        self.assertFalse(sy.resolve_team(self.project).gate)


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
        (self.project / ".syndicate" / "config").write_text("")
        subprocess.run(["git", "-C", str(self.project), "add", "-f", ".syndicate/config"], check=True)
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
        self.assertEqual(settings["permissions"]["allow"], [f"Bash({sy.SCRIPT} run-role *)"])

    def test_gate_off_removes_hook(self):
        self.config("review-gate = off\n")
        argv = sy.build_claude(sy.resolve_team(self.project), False, [])
        self.assertNotIn("--settings", argv)

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

    def test_codex_native_reviewer_is_read_only(self):
        self.config("default = codex\n")
        sy.build_codex(sy.resolve_team(self.project, "codex"), False, [])
        gen = self.project / ".syndicate" / ".generated" / "codex"
        self.assertIn('sandbox_mode = "read-only"', (gen / "code-reviewer.toml").read_text())
        self.assertIn('sandbox_mode = "workspace-write"', (gen / "backend-engineer.toml").read_text())

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


if __name__ == "__main__":
    unittest.main()
