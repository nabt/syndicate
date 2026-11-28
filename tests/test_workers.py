import contextlib
import io
import json
import subprocess
import time
import unittest
from unittest.mock import patch

from test_syndicate import ProjectCase, sy


class Workers(ProjectCase):
    def tmux(self, commands):
        def execute(argv, **kwargs):
            kind = argv[1]
            if kind not in commands:
                raise AssertionError(argv)
            reply = commands[kind]
            return reply(argv, **kwargs) if callable(reply) else reply
        return patch.object(sy.subprocess, 'run', side_effect=execute)

    def test_list_reports_a_missing_session(self):
        commands = {'has-session': subprocess.CompletedProcess([], 1)}
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sy.shutil, 'which', return_value='/usr/bin/tmux'), \
             self.tmux(commands), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(sy.main(['list', 'workers']), 0)
            self.assertEqual(sy.main(['list', 'workers', '--json']), 0)
        self.assertIn('not running', out.getvalue())
        self.assertIn('not running', err.getvalue())
        self.assertIn('[]', out.getvalue())

    def test_list_shows_live_windows_and_view_panes(self):
        commands = {
            'has-session': subprocess.CompletedProcess([], 0),
            'list-windows': subprocess.CompletedProcess(
                [], 0, '@1\tchief-of-staff\t1\t0\tclaude\t/portfolio\n@2\tsyndicate-view\t0\t0\tsleep\t/tmp\n', ''),
            'list-panes': subprocess.CompletedProcess([], 0, '%4\tapi\t0\tcodex\t/src/api\n', ''),
        }
        out = io.StringIO()
        with patch.object(sy.shutil, 'which', return_value='/usr/bin/tmux'), \
             self.tmux(commands), contextlib.redirect_stdout(out):
            self.assertEqual(sy.main(['list', 'workers']), 0)
            self.assertEqual(sy.main(['list', 'workers', '--json']), 0)
        text = out.getvalue()
        self.assertIn('* chief-of-staff', text)
        self.assertIn('live', text)
        self.assertIn('api', text)
        self.assertIn('side-by-side', text)
        self.assertNotIn('syndicate-view  ', text)
        records = json.loads(text[text.index('['):])
        self.assertEqual([row['name'] for row in records], ['chief-of-staff', 'api'])
        self.assertEqual(records[1]['state'], 'side-by-side')

    def test_show_rejects_duplicates_and_unknown_workers(self):
        commands = {
            'has-session': subprocess.CompletedProcess([], 0),
            'wait-for': subprocess.CompletedProcess([], 0),
            'list-windows': subprocess.CompletedProcess([], 0, '@1\tchief-of-staff\t1\t0\tclaude\t/portfolio\n', ''),
            'set-hook': subprocess.CompletedProcess([], 0, '', ''),
            'set-option': subprocess.CompletedProcess([], 0, '', ''),
        }
        with patch.object(sy.shutil, 'which', return_value='/usr/bin/tmux'), self.tmux(commands):
            with self.assertRaisesRegex(sy.SyndicateError, 'once'):
                sy.show_workers(['api', 'api'])
            with self.assertRaisesRegex(sy.SyndicateError, 'no worker'):
                sy.show_workers(['api'])

    def test_close_refuses_the_chief_without_force(self):
        commands = {'has-session': subprocess.CompletedProcess([], 0)}
        err = io.StringIO()
        with patch.object(sy.shutil, 'which', return_value='/usr/bin/tmux'), \
             self.tmux(commands) as run, contextlib.redirect_stderr(err):
            self.assertEqual(sy.main(['close', 'chief-of-staff']), 1)
            with self.assertRaisesRegex(sy.SyndicateError, '--force'):
                sy.close_worker('chief-of-staff')
        self.assertIn('--force', err.getvalue())
        self.assertNotIn('kill-window', [call.args[0][1] for call in run.call_args_list])

    def test_close_kills_only_that_window(self):
        commands = {
            'has-session': subprocess.CompletedProcess([], 0),
            'wait-for': subprocess.CompletedProcess([], 0),
            'list-windows': subprocess.CompletedProcess([], 0, '@3\tapi\t0\t0\tcodex\t/src/api\n', ''),
            'kill-window': subprocess.CompletedProcess([], 0, '', ''),
        }
        out = io.StringIO()
        with patch.object(sy.shutil, 'which', return_value='/usr/bin/tmux'), \
             self.tmux(commands) as run, contextlib.redirect_stdout(out):
            self.assertEqual(sy.main(['close', 'api']), 0)
        killed = [call.args[0] for call in run.call_args_list if call.args[0][1] == 'kill-window']
        self.assertEqual(killed, [['tmux', 'kill-window', '-t', '@3']])
        self.assertIn('Closed api.', out.getvalue())

    def test_message_types_literal_text_and_submits(self):
        commands = {
            'has-session': subprocess.CompletedProcess([], 0),
            'list-windows': subprocess.CompletedProcess([], 0, '@3\tapi\t0\t0\tcodex\t/src/api\n', ''),
            'send-keys': subprocess.CompletedProcess([], 0, '', ''),
        }
        out = io.StringIO()
        with patch.object(sy.shutil, 'which', return_value='/usr/bin/tmux'), \
             self.tmux(commands) as run, contextlib.redirect_stdout(out):
            self.assertEqual(sy.main(['message', 'api', 'review', 'the', 'Enter']), 0)
        sent = [call.args[0] for call in run.call_args_list if call.args[0][1] == 'send-keys']
        self.assertEqual(sent, [
            ['tmux', 'send-keys', '-t', 'the-syndicate:api', '-l', '--', 'review the Enter'],
            ['tmux', 'send-keys', '-t', 'the-syndicate:api', 'Enter'],
        ])
        self.assertIn('Sent to api.', out.getvalue())

    def test_message_can_skip_enter_and_refuses_an_exited_worker(self):
        live = {
            'has-session': subprocess.CompletedProcess([], 0),
            'list-windows': subprocess.CompletedProcess([], 0, '@3\tapi\t0\t0\tcodex\t/src/api\n', ''),
            'send-keys': subprocess.CompletedProcess([], 0, '', ''),
        }
        with patch.object(sy.shutil, 'which', return_value='/usr/bin/tmux'), self.tmux(live) as run:
            sy.message_worker('api', 'draft', submit=False)
        sent = [call.args[0] for call in run.call_args_list if call.args[0][1] == 'send-keys']
        self.assertEqual(sent, [['tmux', 'send-keys', '-t', 'the-syndicate:api', '-l', '--', 'draft']])
        exited = {
            'has-session': subprocess.CompletedProcess([], 0),
            'list-windows': subprocess.CompletedProcess([], 0, '@3\tapi\t0\t1\tzsh\t/src/api\n', ''),
        }
        with patch.object(sy.shutil, 'which', return_value='/usr/bin/tmux'), self.tmux(exited) as run:
            with self.assertRaisesRegex(sy.SyndicateError, 'not running'):
                sy.message_worker('api', 'hello')
        self.assertNotIn('send-keys', [call.args[0][1] for call in run.call_args_list])

    def test_capture_prints_pane_output(self):
        commands = {
            'has-session': subprocess.CompletedProcess([], 0),
            'list-windows': subprocess.CompletedProcess([], 0, '@3\tapi\t0\t0\tcodex\t/src/api\n', ''),
            'capture-pane': subprocess.CompletedProcess([], 0, 'hello\n', ''),
        }
        out = io.StringIO()
        with patch.object(sy.shutil, 'which', return_value='/usr/bin/tmux'), \
             self.tmux(commands) as run, contextlib.redirect_stdout(out):
            self.assertEqual(sy.main(['capture', 'api', '--lines', '20']), 0)
        command = next(call.args[0] for call in run.call_args_list if call.args[0][1] == 'capture-pane')
        self.assertEqual(command[:5], ['tmux', 'capture-pane', '-p', '-J', '-t'])
        self.assertIn('-20', command)
        self.assertEqual(out.getvalue(), 'hello\n')

    def test_detach_restores_a_view(self):
        commands = {
            'has-session': subprocess.CompletedProcess([], 0),
            'detach-client': subprocess.CompletedProcess([], 0, '', ''),
            'wait-for': subprocess.CompletedProcess([], 0),
            'list-windows': subprocess.CompletedProcess([], 0, '', ''),
            'set-hook': subprocess.CompletedProcess([], 0, '', ''),
            'set-option': subprocess.CompletedProcess([], 0, '', ''),
        }
        out = io.StringIO()
        with patch.object(sy.shutil, 'which', return_value='/usr/bin/tmux'), \
             self.tmux(commands) as run, contextlib.redirect_stdout(out):
            self.assertEqual(sy.main(['detach']), 0)
        self.assertIn('detach-client', [call.args[0][1] for call in run.call_args_list])
        self.assertIn('Detached from the-syndicate.', out.getvalue())

    def test_launch_refuses_a_worker_open_in_the_view(self):
        commands = {
            'has-session': subprocess.CompletedProcess([], 0),
            'list-windows': subprocess.CompletedProcess([], 0, '@2\tsyndicate-view\t0\t0\tsleep\t/tmp\n', ''),
            'list-panes': subprocess.CompletedProcess([], 0, '%8\texample\t0\tcodex\t/src\n', ''),
        }
        with patch.object(sy.shutil, 'which', return_value='/usr/bin/tmux'), self.tmux(commands) as run:
            with self.assertRaisesRegex(sy.SyndicateError, 'side-by-side'):
                sy._release_replaced_windows('example')
        self.assertNotIn('kill-window', [call.args[0][1] for call in run.call_args_list])

    def test_show_one_switches_the_current_client(self):
        commands = {
            'has-session': subprocess.CompletedProcess([], 0),
            'wait-for': subprocess.CompletedProcess([], 0),
            'list-windows': subprocess.CompletedProcess([], 0, '@1\tapi\t0\t0\tcodex\t/src/api\n', ''),
            'set-hook': subprocess.CompletedProcess([], 0, '', ''),
            'set-option': subprocess.CompletedProcess([], 0, '', ''),
            'list-keys': subprocess.CompletedProcess([], 1, '', ''),
            'bind-key': subprocess.CompletedProcess([], 0, '', ''),
            'switch-client': subprocess.CompletedProcess([], 0, '', ''),
        }
        with patch.object(sy.shutil, 'which', return_value='/usr/bin/tmux'), \
             self.tmux(commands) as run, \
             patch.dict('os.environ', {'TMUX': '/tmp/tmux/default,1,0'}):
            self.assertEqual(sy.show_workers(['api']), 0)
        switched = next(call.args[0] for call in run.call_args_list if call.args[0][1] == 'switch-client')
        self.assertEqual(switched, ['tmux', 'switch-client', '-t', 'the-syndicate:api'])
        bound = next(call.args[0] for call in run.call_args_list if call.args[0][1] == 'bind-key')
        self.assertEqual(bound[-2:], ['detach-client', 'send-keys F12'])
        self.assertIn('#{session_name},the-syndicate', bound[8])

    def test_leaving_tmux_clears_mouse_tracking(self):
        class Tty:
            def __init__(self):
                self.buffer = io.BytesIO()

            def isatty(self):
                return True

            def fileno(self):
                return 0

        fake = Tty()
        saved = ['original']
        environment = {key: value for key, value in sy.os.environ.items() if key != 'TMUX'}
        with patch.object(sy, '_arm_detach_binding'), \
             patch.object(sy.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run, \
             patch.object(sy.sys, 'stdin', fake), \
             patch.object(sy.sys, 'stdout', fake), \
             patch.object(sy.runtime.termios, 'tcgetattr', return_value=saved), \
             patch.object(sy.runtime.termios, 'tcsetattr') as restore, \
             patch.dict(sy.os.environ, environment, clear=True):
            self.assertEqual(sy.focus_target('the-syndicate:example'), 0)
        restore.assert_called_once_with(fake, sy.runtime.termios.TCSADRAIN, saved)
        self.assertEqual(fake.buffer.getvalue(), sy.runtime.TERMINAL_MODES_OFF)
        self.assertEqual(run.call_args.args[0], ['tmux', 'attach-session', '-t', 'the-syndicate:example'])


class WorkerIntegration(ProjectCase):
    def socket(self):
        name = 'syndicate-workers-' + sy.uuid.uuid4().hex
        self.addCleanup(subprocess.run, ['tmux', '-L', name, 'kill-server'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return name

    @unittest.skipUnless(__import__('shutil').which('tmux'), 'tmux unavailable')
    def test_real_side_by_side_view_restores_windows(self):
        socket = self.socket()
        sy._start_tmux_window(self.project, 'chief-of-staff', 'sleep 60', socket=socket)
        sy._start_tmux_window(self.project, 'example', 'sleep 60', socket=socket)
        self.assertEqual(sorted(row['name'] for row in sy.worker_records(socket)),
                         ['chief-of-staff', 'example'])
        sy.open_worker_view(['chief-of-staff', 'example'], socket=socket)
        windows = subprocess.run(
            ['tmux', '-L', socket, 'list-windows', '-t', sy.TMUX_SESSION, '-F', '#{window_name} #{window_layout}'],
            capture_output=True, text=True, check=True)
        self.assertEqual([line.split()[0] for line in windows.stdout.splitlines()], ['syndicate-view'])
        self.assertIn('{', windows.stdout)
        panes = subprocess.run(
            ['tmux', '-L', socket, 'list-panes', '-t', f'{sy.TMUX_SESSION}:{sy.VIEW_WINDOW}',
             '-F', '#{pane_left} #{@syndicate-worker}'],
            capture_output=True, text=True, check=True)
        placed = [line.split() for line in panes.stdout.splitlines()]
        self.assertEqual(sorted(item[1] for item in placed), ['chief-of-staff', 'example'])
        self.assertEqual(len({item[0] for item in placed}), 2)
        with self.assertRaisesRegex(sy.SyndicateError, 'side-by-side'):
            sy._release_replaced_windows('example', socket=socket)
        sy.restore_worker_view(socket, force=True)
        self.assertEqual(sorted(row['name'] for row in sy.worker_records(socket)),
                         ['chief-of-staff', 'example'])
        self.assertEqual(sy.capture_worker('example', 5, socket=socket).strip(), '')
        sy.close_worker('example', socket=socket)
        self.assertEqual([row['name'] for row in sy.worker_records(socket)], ['chief-of-staff'])
        sy._arm_detach_binding(socket)
        keys = subprocess.run(['tmux', '-L', socket, 'list-keys', '-T', 'root', 'F12'],
                              capture_output=True, text=True, check=True)
        self.assertIn('detach-client', keys.stdout)
        self.assertIn('the-syndicate', keys.stdout)
        alive = subprocess.run(
            ['tmux', '-L', socket, 'list-panes', '-t', f'{sy.TMUX_SESSION}:chief-of-staff', '-F', '#{pane_dead}'],
            capture_output=True, text=True, check=True)
        self.assertEqual(alive.stdout.strip(), '0')
        sy._start_tmux_window(self.project, 'inbox', 'cat', socket=socket)
        sy.message_worker('inbox', 'hello workers', socket=socket)
        time.sleep(0.2)
        self.assertIn('hello workers', sy.capture_worker('inbox', 20, socket=socket))
