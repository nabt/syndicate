import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_syndicate import ProjectCase, sy

completion = sy.completion
BIN = Path(sy.SCRIPT)


def source(data=None):
    data = data or {}

    def values(kind):
        return data.get(kind, [(f"{kind}-a", f"{kind} A"), (f"{kind}-b", "")])

    return completion.Source(sy.build_parsers()[1], values)


def names(words, data=None):
    items, directive = completion.complete(words, len(words) - 1, source(data))
    return [value for value, _desc in items], directive


class CompletionTree(unittest.TestCase):
    def test_every_subcommand_is_offered(self):
        parser = sy.build_parsers()[1]

        def walk(path, node):
            offered, _directive = names([*path, ""])
            commands = {}
            for action in node._actions:
                if isinstance(action, argparse._SubParsersAction):
                    commands = action.choices
            self.assertFalse(set(commands) - set(offered), path)
            for name, child in commands.items():
                walk([*path, name], child)

        walk(["syndicate"], parser)

    def test_every_flag_is_offered(self):
        parser = sy.build_parsers()[1]

        def walk(path, node):
            offered, _directive = names([*path, "-"])
            expected = {option for action in node._actions for option in action.option_strings
                        if action.help is not argparse.SUPPRESS}
            self.assertFalse(expected - set(offered), (path, expected - set(offered)))
            for action in node._actions:
                if isinstance(action, argparse._SubParsersAction):
                    for name, child in action.choices.items():
                        walk([*path, name], child)

        walk(["syndicate"], parser)

    def test_choices_projects_roles_workers_and_tasks(self):
        offered, directive = names(["syndicate", "team", "task", "--runtime", ""])
        self.assertEqual(offered, ["claude", "codex"])
        self.assertEqual(directive, completion.NO_FILE)

        offered, directive = names(["syndicate", "team", "task", "--runtime="])
        self.assertEqual(offered, ["--runtime=claude", "--runtime=codex"])
        self.assertFalse(directive & completion.DIRS)

        offered, directive = names(["syndicate", "team", "task", "--project", "project-a"])
        self.assertEqual(offered, ["project-a"])
        self.assertTrue(directive & completion.DIRS)

        offered, directive = names(["syndicate", "team", "run-role", ""])
        self.assertEqual(offered, ["role-a", "role-b"])

        offered, _directive = names(["syndicate", "team", "run-role", "role-a", ""])
        self.assertEqual(offered, ["-"])

        offered, _directive = names(["syndicate", "show", ""])
        self.assertEqual(offered, ["worker-a", "worker-b"])
        offered, _directive = names(["syndicate", "show", "worker-a", "worker-a"])
        self.assertEqual(offered, ["worker-a"])

        offered, _directive = names(["syndicate", "tasks", "edit", ""])
        self.assertEqual(offered, ["task-a", "task-b"])
        offered, _directive = names(["syndicate", "tasks", "edit", "task-a", "--state", ""])
        self.assertIn("ended", offered)
        self.assertIn("done", offered)
        self.assertNotIn("task-a", offered)

    def test_flag_forms_exclusion_and_repeatable_flags(self):
        offered, _directive = names(["syndicate", "team", "task", "--pri"])
        self.assertEqual(offered, ["--print"])

        offered, _directive = names(["syndicate", "team", "run-role", "--runtime", "claude", "--"])
        self.assertIn("--project", offered)
        self.assertNotIn("--runtime", offered)
        self.assertNotIn("--backoff-from", offered)

        offered, _directive = names([
            "syndicate", "briefs", "update", "--project", "p", "--constraint", "a", "--",
        ])
        self.assertIn("--constraint", offered)
        self.assertIn("--clear", offered)

        offered, directive = names(["syndicate", "config", "-C", ""])
        self.assertEqual(offered, [])
        self.assertEqual(directive, completion.DIRS)

        offered, _directive = names([
            "syndicate", "tasks", "close", "--implementation", "",
        ])
        self.assertEqual(offered, ["role-a", "role-b"])
        offered, _directive = names([
            "syndicate", "tasks", "close", "--implementation", "role-a", "",
        ])
        self.assertEqual(offered, ["runtime-a", "runtime-b"])
        offered, directive = names([
            "syndicate", "tasks", "close", "--implementation", "role-a", "runtime-a", "",
        ])
        self.assertEqual(offered, [])
        self.assertEqual(directive, completion.NO_FILE)

    def test_chief_of_staff_flags_and_rewrite(self):
        offered, _directive = names(["syndicate", ""])
        self.assertIn("team", offered)
        self.assertIn("completion", offered)
        self.assertNotIn("--dry-run", offered)

        offered, _directive = names(["syndicate", "-"])
        self.assertIn("--dry-run", offered)
        self.assertIn("--runtime", offered)
        self.assertIn("-p", offered)
        self.assertNotIn("team", offered)
        offered, _directive = names(["syndicate", "--dry"])
        self.assertEqual(offered, ["--dry-run"])

        offered, _directive = names(["syndicate", "-x", ""])
        self.assertEqual(offered, ["claude", "codex"])

        offered, _directive = names(["syndicate", "-x", "codex", "--dry"])
        self.assertEqual(offered, ["--dry-run"])

    def test_completion_command_help_has_examples(self):
        parser = sy.build_parsers()[1]
        command = next(action.choices["completion"] for action in parser._actions
                       if isinstance(action, argparse._SubParsersAction))
        text = command.format_help()
        self.assertIn("source <(syndicate completion bash)", text)
        self.assertIn("source <(syndicate completion zsh)", text)
        self.assertIn("Examples:", text)


class CompletionCommand(ProjectCase):
    def test_scripts_print_and_unknown_shell_is_rejected(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(sy.main(["completion", "bash"]), 0)
        bash = output.getvalue()
        self.assertIn("complete -F _syndicate syndicate", bash)
        self.assertIn("__complete", bash)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(sy.main(["completion", "zsh"]), 0)
        zsh = output.getvalue()
        self.assertIn("#compdef syndicate", zsh)
        self.assertIn("compdef _syndicate_complete syndicate", zsh)

        error = io.StringIO()
        with contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as caught:
            sy.main(["completion", "fish"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("bash", error.getvalue())
        self.assertIn("zsh", error.getvalue())

    def test_complete_protocol(self):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = sy.main(["__complete", "--shell", "zsh", "--cword", "2", "--", "syndicate", "team", ""])
        self.assertEqual(code, 0)
        self.assertEqual(error.getvalue(), "")
        self.assertIn("task\t", output.getvalue())
        self.assertTrue(output.getvalue().strip().endswith(":2"))

        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertEqual(sy.main(["__complete", "--shell"]), 2)
        self.assertIn("usage", error.getvalue())

    def test_known_values_come_from_readable_state(self):
        status = {"tasks": [{"id": "task-1", "state": "running", "summary": "Ship it"}]}
        (self.project / ".syndicate" / "status.json").write_text(json.dumps(status))
        sy.register_project(self.project)
        self.role_file("custom-role.md", "---\ndescription: Custom role\n---\nBody\n")
        portfolio = sy.portfolio_dir()
        portfolio.mkdir(parents=True)
        (portfolio / "decisions.json").write_text(json.dumps({"version": 1, "decisions": [{
            "id": "dec-1", "state": "open", "source_path": "/src", "project_path": "/src",
            "task_id": "task-1", "key": "k", "question": "Ship it?",
            "created_at": "2020-01-01T00:00:00+00:00", "updated_at": "2020-01-01T00:00:00+00:00",
            "options": [], "context": "", "recommendation": "", "answer": "", "blocking": False,
            "deadline": None, "history": [], "answer_revision": 0,
        }]}))

        def fail(*_args, **_kwargs):
            raise AssertionError("completion must not sweep or write status")

        with patch.object(sy, "gather_statuses", side_effect=fail), \
             patch.object(sy, "register_project", side_effect=fail):
            projects = dict(sy.completion_values("project"))
            roles = dict(sy.completion_values("role"))
            tasks = dict(sy.completion_values("task"))
            decisions = dict(sy.completion_values("decision"))
        self.assertIn("generic", projects)
        self.assertIn(self.project.name, projects)
        self.assertIn("code-reviewer", roles)
        self.assertEqual(roles["custom-role"], "Custom role")
        self.assertIn("running", tasks["task-1"])
        self.assertIn("Ship it", tasks["task-1"])
        self.assertIn("Ship it?", decisions["dec-1"])

        def fake_run(args, **_kwargs):
            self.assertEqual(args[0], "tmux")
            if args[1] == "list-windows":
                return subprocess.CompletedProcess(args, 0, "@1\tchief-of-staff\n@2\tapi\n@3\tsyndicate-view\n", "")
            if args[1] == "list-panes":
                return subprocess.CompletedProcess(args, 0, "held-worker\n\n", "")
            raise AssertionError(args)

        with patch.object(sy.subprocess, "run", side_effect=fake_run):
            workers = [name for name, _desc in sy.completion_values("worker")]
        self.assertEqual(workers, ["chief-of-staff", "api", "held-worker"])


class CompletionScripts(unittest.TestCase):
    def test_scripts_are_valid_shell(self):
        bash = shutil.which("bash")
        zsh = shutil.which("zsh")
        self.assertIsNotNone(bash)
        self.assertIsNotNone(zsh)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            bash_script = directory / "syndicate.bash"
            zsh_script = directory / "_syndicate"
            bash_script.write_text(completion.BASH_SCRIPT)
            zsh_script.write_text(completion.ZSH_SCRIPT)
            subprocess.run([bash, "-n", str(bash_script)], check=True)
            subprocess.run([zsh, "-n", str(zsh_script)], check=True)
            zsh_home = directory / "home"
            zsh_home.mkdir()
            env = {**os.environ, "ZDOTDIR": str(zsh_home)}
            probe = subprocess.run(
                [zsh, "-f", "-c", "autoload -U compinit; compinit -u -C; source \"$1\"; whence -v _syndicate_complete",
                 "zsh", str(zsh_script)],
                check=True, capture_output=True, text=True, env=env, timeout=30,
            )
            self.assertIn("_syndicate_complete", probe.stdout)

            program = f'''
source {sy.shlex.quote(str(bash_script))}
COMP_WORDS=({sy.shlex.quote(str(BIN))} team "")
COMP_CWORD=2
_syndicate
printf '%s\\n' "${{COMPREPLY[@]}}"
'''
            completed = subprocess.run(
                [bash, "--noprofile", "--norc", "-c", program],
                check=True, capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(set(completed.stdout.split()), {"task", "init", "run-role"})
