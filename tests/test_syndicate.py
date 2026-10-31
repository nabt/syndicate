import argparse
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
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name).resolve()
        registry = unittest.mock.patch.object(sy, "REGISTRY_PATH", self.project / "registry" / "projects.json")
        registry.start()
        self.addCleanup(registry.stop)
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
        self.config("review-gate = off\nhandoff = off\n")
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

    def test_launch_failure_still_records_generic_task(self):
        args = sy.build_parsers()[0].parse_args(["-C", str(self.project), "new task"])
        with unittest.mock.patch.object(sy, "require_clis", side_effect=sy.SyndicateError("missing CLI")):
            with self.assertRaises(sy.SyndicateError):
                sy.cmd_run(args)
        row = sy.gather_statuses()[0]
        self.assertEqual(row["name"], "Generic tasks")
        self.assertEqual(row["project_status"], "Needs attention")
        self.assertTrue(row["tasks"][0]["needs_project"])

    def test_explicit_project_uses_repo_without_prompt(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        args = sy.build_parsers()[0].parse_args(["-C", "/tmp", "--project", str(self.project), "new task"])
        with unittest.mock.patch.object(sy, "run_session", return_value=0):
            self.assertEqual(sy.cmd_run(args), 0)
        self.assertEqual(args.dir, str(self.project))
        task = sy.gather_statuses()[0]["tasks"][0]
        self.assertFalse(task["needs_project"])

    def test_relay_run_registers_before_cli_failure(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        args = argparse.Namespace(dir=str(self.project), role="backend-engineer", runtime=None, brief="work")
        with unittest.mock.patch.object(sy, "require_clis", side_effect=sy.SyndicateError("missing CLI")):
            with self.assertRaises(sy.SyndicateError):
                sy.cmd_run_role(args)
        self.assertEqual(sy.load_registry()[0]["path"], str(self.project))

    def test_handoff_resumes_existing_task(self):
        sy.register_workspace(self.project)
        sy.write_status_stub(self.project, 2**30, "original request")
        snapshot = self.project / "snapshot"
        snapshot.mkdir()
        (snapshot / "status.json").write_bytes(sy.status_path(self.project).read_bytes())
        prior = json.loads(sy.status_path(self.project).read_text())
        args = sy.build_parsers()[0].parse_args(["-C", str(self.project), "--handoff", str(snapshot)])
        with unittest.mock.patch.object(sy, "run_session", return_value=0):
            sy.cmd_run(args)
        after = json.loads(sy.status_path(self.project).read_text())
        self.assertEqual(after["current"]["task_id"], prior["current"]["task_id"])
        self.assertEqual(len(after["tasks"]), 1)

    def test_generic_selection_does_not_ask_or_change_directory(self):
        args = sy.build_parsers()[0].parse_args(["-C", str(self.project), "--project", "generic", "request"])
        with unittest.mock.patch.object(sy, "run_session", return_value=0):
            sy.cmd_run(args)
        row = sy.gather_statuses()[0]
        self.assertEqual(row["name"], "Generic tasks")
        self.assertFalse(row["tasks"][0]["needs_project"])
        self.assertEqual(args.dir, str(self.project))

    def test_task_state_edit_controls_completion_and_ambiguous_ids_fail(self):
        sy.register_project(self.project)
        sy.write_status_stub(self.project, 2**30, "interrupted")
        task_id = sy.gather_statuses()[0]["tasks"][0]["id"]
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

    def test_is_pid_alive(self):
        self.assertTrue(sy.is_pid_alive(os.getpid()))
        self.assertFalse(sy.is_pid_alive(0))
        self.assertFalse(sy.is_pid_alive(-5))
        self.assertFalse(sy.is_pid_alive("not-a-pid"))
        # a pid essentially guaranteed not to exist
        self.assertFalse(sy.is_pid_alive(2**30))

    def test_unfinished_tasks_survive_new_run(self):
        sy.register_project(self.project)
        sy.write_status_stub(self.project, 2**30, "first request")
        sy.write_status_stub(self.project, os.getpid(), "second request")
        row = sy.gather_statuses()[0]
        self.assertEqual(row["unfinished_count"], 2)
        self.assertEqual(row["project_status"], "Needs attention")
        self.assertEqual(row["tasks"][0]["summary"], "first request")
        self.assertEqual(row["tasks"][0]["state"], "stale")

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

    def test_gather_statuses_detects_stale_dead_pid(self):
        sy.register_project(self.project)
        sy.write_status_stub(self.project, 2**30, "stub")  # pid that can't exist
        rows = sy.gather_statuses()
        self.assertEqual(rows[0]["state"], "stale")

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
