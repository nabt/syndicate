import contextlib
import io
import os
import shlex
import signal
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

    def tmux(self, commands):
        def execute(argv, **kwargs):
            kind = argv[1]
            if kind not in commands:
                raise AssertionError(argv)
            reply = commands[kind]
            return reply(argv, **kwargs) if callable(reply) else reply
        return patch.object(sy.subprocess, 'run', side_effect=execute)

    def test_task_launches_quoted_command_in_named_tmux(self):
        store = Mock()
        store.snapshot.return_value = {'sessions': []}
        commands = {
            'has-session': subprocess.CompletedProcess([], 1),
            'new-session': subprocess.CompletedProcess([], 0, '@1\n', ''),
            'list-windows': subprocess.CompletedProcess([], 0, f'@1\t{self.project.name}\n', ''),
        }
        with patch.object(sy, 'tmux_enabled', return_value=True), \
             patch.object(sy, 'coordination_store', return_value=store), \
             patch.object(sy, 'execution_project', return_value=self.project), \
             patch.object(sy.shutil, 'which', return_value='/usr/bin/tmux'), \
             self.tmux(commands) as run, \
             contextlib.redirect_stdout(io.StringIO()), \
             patch.object(sy.sys.stdin, 'isatty', return_value=False):
            self.assertEqual(self.launch(), 0)
        command = next(call.args[0] for call in run.call_args_list if call.args[0][1] == 'new-session')
        self.assertEqual(command[:9], ['tmux', 'new-session', '-d', '-s', sy.TMUX_SESSION, '-n',
                                      self.project.name, '-c', str(self.project)])
        self.assertNotIn('new-window', [call.args[0][1] for call in run.call_args_list])
        argv = shlex.split(command[-1])
        self.assertEqual(argv[:6], [sy.identity.interpreter(), '-B', str(sy.SCRIPT), '_in-tmux', 'team', 'task'])
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

    def test_existing_session_is_reused_for_a_new_window(self):
        commands = {
            'has-session': subprocess.CompletedProcess([], 0),
            'list-windows': subprocess.CompletedProcess([], 0, '@1\tchief-of-staff\n', ''),
            'new-window': subprocess.CompletedProcess([], 0, '@2\n', ''),
        }
        with patch.object(sy.shutil, 'which', return_value='/usr/bin/tmux'), \
             self.tmux(commands) as run, \
             patch.object(sy.sys.stdin, 'isatty', return_value=False), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sy.launch_tmux([], self.project, 'example'), 0)
        created = next(call.args[0] for call in run.call_args_list if call.args[0][1] == 'new-window')
        self.assertEqual(created[:7], ['tmux', 'new-window', '-d', '-t', sy.TMUX_SESSION, '-n', 'example'])
        self.assertNotIn('new-session', [call.args[0][1] for call in run.call_args_list])
        self.assertNotIn('kill-session', [call.args[0][1] for call in run.call_args_list])

    def test_existing_tmux_window_is_not_overwritten(self):
        commands = {
            'has-session': subprocess.CompletedProcess([], 0),
            'list-windows': subprocess.CompletedProcess([], 0, '@7\texample\n', ''),
            'list-panes': subprocess.CompletedProcess([], 0, '0:\n', ''),
        }
        with patch.object(sy.shutil, 'which', return_value='/usr/bin/tmux'), \
             self.tmux(commands) as run:
            with self.assertRaisesRegex(sy.SyndicateError, 'already exists'):
                sy.launch_tmux([], self.project, 'example')
        self.assertNotIn('kill-window', [call.args[0][1] for call in run.call_args_list])
        self.assertNotIn('new-window', [call.args[0][1] for call in run.call_args_list])

    def test_dead_chief_windows_collapse_to_one(self):
        killed = []

        def windows(argv, **kwargs):
            if killed:
                return subprocess.CompletedProcess(argv, 0, '@4\tother\n@9\tchief-of-staff\n', '')
            return subprocess.CompletedProcess(argv, 0, '@2\tchief-of-staff\n@3\tchief-of-staff\n@4\tother\n', '')

        def kill(argv, **kwargs):
            killed.append(argv[-1])
            return subprocess.CompletedProcess(argv, 0, '', '')

        commands = {
            'has-session': subprocess.CompletedProcess([], 0),
            'list-windows': windows,
            'list-panes': subprocess.CompletedProcess([], 0, '1:\n', ''),
            'kill-window': kill,
            'new-window': subprocess.CompletedProcess([], 0, '@9\n', ''),
        }
        with patch.object(sy.shutil, 'which', return_value='/usr/bin/tmux'), \
             self.tmux(commands) as run, \
             patch.object(sy.sys.stdin, 'isatty', return_value=False), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sy.launch_tmux([], self.project, 'chief-of-staff'), 0)
        self.assertEqual(killed, ['@2', '@3'])
        self.assertNotIn('kill-session', [call.args[0][1] for call in run.call_args_list])
        self.assertNotIn('new-session', [call.args[0][1] for call in run.call_args_list])

    def test_replacing_the_only_window_recreates_the_session(self):
        state = {'killed': False, 'created': False}

        def has_session(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1 if state['killed'] else 0)

        def windows(argv, **kwargs):
            if state['created']:
                return subprocess.CompletedProcess(argv, 0, '@8\tchief-of-staff\n', '')
            return subprocess.CompletedProcess(argv, 0, '@2\tchief-of-staff\n', '')

        def kill(argv, **kwargs):
            state['killed'] = True
            return subprocess.CompletedProcess(argv, 0, '', '')

        def create(argv, **kwargs):
            state['created'] = True
            return subprocess.CompletedProcess(argv, 0, '@8\n', '')

        commands = {
            'has-session': has_session,
            'list-windows': windows,
            'list-panes': subprocess.CompletedProcess([], 0, '1:\n', ''),
            'kill-window': kill,
            'new-session': create,
        }
        with patch.object(sy.shutil, 'which', return_value='/usr/bin/tmux'), \
             self.tmux(commands) as run, \
             patch.object(sy.sys.stdin, 'isatty', return_value=False), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sy.launch_tmux([], self.project, 'chief-of-staff'), 0)
        self.assertEqual([call.args[0] for call in run.call_args_list if call.args[0][1] == 'kill-window'],
                         [['tmux', 'kill-window', '-t', '@2']])
        self.assertIn('new-session', [call.args[0][1] for call in run.call_args_list])
        self.assertNotIn('kill-session', [call.args[0][1] for call in run.call_args_list])

    def test_live_chief_tmux_pane_is_never_removed(self):
        commands = {
            'has-session': subprocess.CompletedProcess([], 0),
            'list-windows': subprocess.CompletedProcess([], 0, '@2\tchief-of-staff\n', ''),
            'list-panes': subprocess.CompletedProcess([], 0, '1:1\n0:\n', ''),
        }
        with patch.object(sy.shutil, 'which', return_value='/usr/bin/tmux'), \
             self.tmux(commands) as run:
            with self.assertRaisesRegex(sy.SyndicateError, 'already exists'):
                sy.launch_tmux([], self.project, 'chief-of-staff')
        self.assertNotIn('kill-window', [call.args[0][1] for call in run.call_args_list])

    def test_dead_project_reserve_pane_is_replaced(self):
        state = {'killed': False, 'created': False}

        def windows(argv, **kwargs):
            rows = ['@4\tchief-of-staff']
            if not state['killed']:
                rows.insert(0, '@3\texample')
            if state['created']:
                rows.append('@5\texample')
            return subprocess.CompletedProcess(argv, 0, '\n'.join(rows) + '\n', '')

        def kill(argv, **kwargs):
            state['killed'] = True
            return subprocess.CompletedProcess(argv, 0, '', '')

        def create(argv, **kwargs):
            state['created'] = True
            return subprocess.CompletedProcess(argv, 0, '@5\n', '')

        commands = {
            'has-session': subprocess.CompletedProcess([], 0),
            'list-windows': windows,
            'list-panes': subprocess.CompletedProcess([], 0, '1:1\n', ''),
            'kill-window': kill,
            'new-window': create,
        }
        with patch.object(sy.shutil, 'which', return_value='/usr/bin/tmux'), \
             self.tmux(commands) as run, \
             patch.object(sy.sys.stdin, 'isatty', return_value=False), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sy.launch_tmux([], self.project, 'example'), 0)
        killed = [call.args[0] for call in run.call_args_list if call.args[0][1] == 'kill-window']
        self.assertEqual(killed, [['tmux', 'kill-window', '-t', '@3']])

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
            self.assertEqual(launch.call_args.args[2], 'chief-of-staff')
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
                return subprocess.CompletedProcess(argv, 0, '@1\n', '')
            return subprocess.CompletedProcess(argv, 0, '', '')
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sy.shutil, 'which', return_value='/usr/bin/tmux'), \
             patch.object(sy.subprocess, 'run', side_effect=execute) as run, \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(sy.launch_tmux(['-p', 'Task'], self.project, 'headless', headless=True), 7)
        self.assertEqual(out.getvalue(), 'result\n')
        self.assertEqual(err.getvalue(), 'diagnostic\n')
        self.assertEqual(run.call_args.args[0][:3], ['tmux', 'kill-window', '-t'])

    def test_headless_sighup_closes_only_its_window(self):
        def execute(argv, **kwargs):
            if argv[1] == 'has-session':
                return subprocess.CompletedProcess(argv, 1)
            if argv[1] == 'new-session':
                self.assertTrue(kwargs.get('start_new_session'))
                os.kill(os.getpid(), signal.SIGHUP)
                return subprocess.CompletedProcess(argv, 0, '@42\n', '')
            return subprocess.CompletedProcess(argv, 0, '', '')

        with patch.object(sy.shutil, 'which', return_value='/usr/bin/tmux'), \
             patch.object(sy.subprocess, 'run', side_effect=execute) as run, \
             self.assertRaises(SystemExit) as stopped:
            sy.launch_tmux(['team', 'run-role', 'backend-engineer', 'Task'],
                           self.project, 'headless-role', headless=True)
        self.assertEqual(stopped.exception.code, 128 + signal.SIGHUP)
        self.assertEqual(run.call_args.args[0], ['tmux', 'kill-window', '-t', '@42'])
        self.assertNotIn('kill-session', [call.args[0][1] for call in run.call_args_list])


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
    def socket(self):
        name = 'syndicate-test-' + sy.uuid.uuid4().hex
        self.addCleanup(subprocess.run, ['tmux', '-L', name, 'kill-server'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return name

    @unittest.skipUnless(shutil.which('tmux'), 'tmux unavailable')
    def test_real_headless_tmux_preserves_output(self):
        self.user_config('tmux = on\n')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = sy.launch_tmux(['config', '--global'], self.project,
                                    'syndicate-test-' + sy.uuid.uuid4().hex,
                                    headless=True, socket=self.socket())
        self.assertEqual(result, 0)
        self.assertIn('tmux = on', out.getvalue())

    @unittest.skipUnless(shutil.which('tmux'), 'tmux unavailable')
    def test_real_session_reuses_windows_and_keeps_one_chief(self):
        socket = self.socket()
        chief = sy._start_tmux_window(self.project, 'chief-of-staff', 'sleep 30', socket=socket)
        sy._start_tmux_window(self.project, 'example', 'sleep 30', socket=socket)

        def names():
            listed = subprocess.run(
                ['tmux', '-L', socket, 'list-windows', '-t', sy.TMUX_SESSION, '-F', '#{window_name}'],
                capture_output=True, text=True, check=True)
            return sorted(listed.stdout.split())

        def sessions():
            listed = subprocess.run(
                ['tmux', '-L', socket, 'list-sessions', '-F', '#{session_name}'],
                capture_output=True, text=True, check=True)
            return listed.stdout.split()

        self.assertEqual(names(), ['chief-of-staff', 'example'])
        self.assertEqual(sessions(), ['the-syndicate'])
        with self.assertRaisesRegex(sy.SyndicateError, 'already exists'):
            sy.launch_tmux(['config', '--global'], self.project, 'chief-of-staff', socket=socket)
        self.assertEqual(names(), ['chief-of-staff', 'example'])
        subprocess.run(['tmux', '-L', socket, 'set-option', '-p', '-t', chief, 'remain-on-exit', 'on'],
                       check=True)
        subprocess.run(['tmux', '-L', socket, 'kill-pane', '-t', chief], check=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sy.launch_tmux(['config', '--global'], self.project, 'chief-of-staff', socket=socket), 0)
        self.assertIn('example', names())
        self.assertEqual(sessions(), ['the-syndicate'])
        self.assertLessEqual(names().count('chief-of-staff'), 1)
