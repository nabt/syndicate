import contextlib
import io
import shlex
import subprocess
import shutil
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_syndicate import ProjectCase, sy

REAL_TMUX_ENABLED = sy.tmux_enabled


class TeamTasks(ProjectCase):
    def launch(self, *extra):
        return sy.main(['team', 'task', '--project', str(self.project),
                        '-x', 'codex', *extra, 'Build $(touch /tmp/unsafe); a new feature'])

    def test_task_launches_quoted_command_in_named_tmux(self):
        store = Mock()
        store.snapshot.return_value = {'sessions': []}
        with patch.object(sy, 'tmux_enabled', return_value=True), \
             patch.object(sy, 'coordination_store', return_value=store), \
             patch.object(sy, 'execution_project', return_value=self.project), \
             patch.object(sy.shutil, 'which', return_value='/usr/bin/tmux'), \
             patch.object(sy.subprocess, 'run', side_effect=[
                 subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0, '', '')]) as run, \
             contextlib.redirect_stdout(io.StringIO()), \
             patch.object(sy.sys.stdin, 'isatty', return_value=False):
            self.assertEqual(self.launch(), 0)
        command = run.call_args.args[0]
        self.assertEqual(command[:7], ['tmux', 'new-session', '-d', '-s',
                                      self.project.name + '-work', '-c', str(self.project)])
        argv = shlex.split(command[-1])
        self.assertEqual(argv[:5], [sy.sys.executable, str(sy.SCRIPT), '_in-tmux', 'team', 'task'])
        self.assertEqual(argv[-1], 'Build $(touch /tmp/unsafe); a new feature')

    def test_live_team_is_reused(self):
        store = Mock()
        store.snapshot.return_value = {'sessions': [dict(
            live=True, source_path=str(self.project), id='existing', task_id='task')]}
        with patch.object(sy, 'tmux_enabled', return_value=True), \
             patch.object(sy, 'coordination_store', return_value=store), \
             patch.object(sy, 'launch_tmux') as launch, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.launch(), 0)
            launch.assert_not_called()

    def test_global_off_runs_directly(self):
        with patch.object(sy, 'cmd_run', return_value=0) as run, \
             patch.object(sy, 'launch_tmux') as launch:
            self.assertEqual(self.launch(), 0)
            run.assert_called_once()
            launch.assert_not_called()
            self.assertEqual(run.call_args.args[0].dir, str(self.project))

    def test_existing_tmux_session_is_not_overwritten(self):
        with patch.object(sy.shutil, 'which', return_value='/usr/bin/tmux'), \
             patch.object(sy.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            with self.assertRaisesRegex(sy.SyndicateError, 'already exists'):
                sy.launch_tmux([], self.project, 'example-work')
            self.assertEqual(run.call_count, 1)

    def test_dead_chief_tmux_panes_are_recovered(self):
        with patch.object(sy.shutil, 'which', return_value='/usr/bin/tmux'), \
             patch.object(sy.subprocess, 'run', side_effect=[
                 subprocess.CompletedProcess([], 0),
                 subprocess.CompletedProcess([], 0, '1\n1\n', ''),
                 subprocess.CompletedProcess([], 0, '', ''),
                 subprocess.CompletedProcess([], 0, '', '')]) as run, \
             patch.object(sy.sys.stdin, 'isatty', return_value=False), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sy.launch_tmux([], self.project, 'chief-of-staff-work'), 0)
        self.assertEqual(run.call_args_list[2].args[0],
                         ['tmux', 'kill-session', '-t', '=chief-of-staff-work'])
        self.assertEqual(run.call_args_list[3].args[0][1], 'new-session')

    def test_live_chief_tmux_pane_is_never_removed(self):
        with patch.object(sy.shutil, 'which', return_value='/usr/bin/tmux'), \
             patch.object(sy.subprocess, 'run', side_effect=[
                 subprocess.CompletedProcess([], 0),
                 subprocess.CompletedProcess([], 0, '1\n0\n', '')]) as run:
            with self.assertRaisesRegex(sy.SyndicateError, 'already exists'):
                sy.launch_tmux([], self.project, 'chief-of-staff-work')
        self.assertEqual(run.call_count, 2)

    def test_internal_execution_does_not_nest_tmux(self):
        with patch.object(sy, 'tmux_enabled', return_value=True), \
             patch.object(sy, 'cmd_run', return_value=0) as run, \
             patch.object(sy, 'launch_tmux') as launch:
            self.assertEqual(sy.main(['_in-tmux', 'team', 'task', '--project', str(self.project), 'Task']), 0)
            run.assert_called_once()
            launch.assert_not_called()

    def test_chief_uses_tmux_and_dry_run_does_not(self):
        with patch.object(sy, 'tmux_enabled', return_value=True), \
             patch.object(sy, 'launch_tmux', return_value=0) as launch:
            self.assertEqual(sy.main(['-x', 'codex']), 0)
            self.assertEqual(launch.call_args.args[2], 'chief-of-staff-work')
            launch.reset_mock()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(sy.main(['chief-of-staff', '--dry-run']), 0)
            launch.assert_not_called()

    def test_headless_output_and_exit_code_are_preserved(self):
        def execute(argv, **kwargs):
            if argv[1] == 'has-session':
                return subprocess.CompletedProcess(argv, 1)
            if argv[1] == 'new-session':
                tokens = shlex.split(shlex.split(argv[-1])[-1])
                # Fake the tmux runner's files, without launching a provider.
                output = next(t[1:] for t in tokens if t.startswith('>'))
                directory = Path(output).parent
                (directory / 'stdout').write_text('result\n')
                (directory / 'stderr').write_text('diagnostic\n')
                (directory / 'exit').write_text('7')
            return subprocess.CompletedProcess(argv, 0, '', '')
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sy.shutil, 'which', return_value='/usr/bin/tmux'), \
             patch.object(sy.subprocess, 'run', side_effect=execute), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(sy.launch_tmux(['-p', 'Task'], self.project, 'headless', headless=True), 7)
        self.assertEqual(out.getvalue(), 'result\n')
        self.assertEqual(err.getvalue(), 'diagnostic\n')


class TmuxConfig(ProjectCase):
    def test_global_default_and_override(self):
        roles, _ = sy.load_roles(self.project)
        self.assertTrue(REAL_TMUX_ENABLED())
        self.user_config('tmux = off\n')
        self.assertFalse(REAL_TMUX_ENABLED())
        self.assertEqual(sy.parse_config_file(sy.user_config_path(), roles)['tmux'], 'off')
        self.user_config('tmux = invalid\n')
        with self.assertRaisesRegex(sy.SyndicateError, 'on or off'):
            sy.parse_config_file(sy.user_config_path(), roles)
        self.config('tmux = off\n')
        with self.assertRaisesRegex(sy.SyndicateError, 'global-only'):
            sy.parse_config_file(sy.project_config_path(self.project), roles)


class TmuxIntegration(ProjectCase):
    @unittest.skipUnless(shutil.which('tmux'), 'tmux unavailable')
    def test_real_headless_tmux_preserves_output(self):
        self.user_config('tmux = on\n')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = sy.launch_tmux(['config', '--global'], self.project,
                                    'syndicate-test-' + sy.uuid.uuid4().hex, headless=True)
        self.assertEqual(result, 0)
        self.assertIn('tmux = on', out.getvalue())
