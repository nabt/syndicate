import contextlib
import fcntl
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from test_syndicate import ProjectCase, sy


class ChiefOfStaff(ProjectCase):
    def setUp(self):
        super().setUp()
        self.workspace = sy.REGISTRY_PATH.parent / 'portfolio' / 'workspace'
        self.state_patch = patch.object(sy.runtime, 'STATE', self.project / 'limits')
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)

    def command(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = sy.main(['chief-of-staff', *args])
        return result, output.getvalue()

    def test_bare_command_opens_interactive_chief_from_any_directory(self):
        outside = self.project / 'outside-repo'
        outside.mkdir()
        for directory in (self.project, outside):
            with self.subTest(directory=directory), \
                 patch.object(sy.os, 'getcwd', return_value=str(directory)), \
                 patch.object(sy, 'require_clis'), \
                 patch.object(sy.runtime, 'start_monitor'), \
                 patch.object(sy.runtime, 'supervise', return_value=(0, None)) as supervise, \
                 patch.object(sy, 'cmd_run') as run_project, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(sy.main([]), 0)
                run_project.assert_not_called()
                self.assertEqual(supervise.call_args.args[1], self.workspace)
                self.assertFalse(supervise.call_args.kwargs['headless'])
                prompt = supervise.call_args.args[0][-1]
                self.assertIn('brief greeting', prompt)
                self.assertIn('current briefing', prompt)
                self.assertIn('Follow up conversationally', prompt)
                self.assertIn('resume existing work', prompt)
        self.assertFalse(sy.status_path(self.project).exists())
        self.assertFalse(sy.REGISTRY_PATH.exists())

    def test_explicit_run_arguments_still_launch_project(self):
        with patch.object(sy, 'resolve_project', return_value=self.project), \
                 patch.object(sy, 'cmd_run', return_value=0) as run_project, \
             patch.object(sy, 'cmd_chief_of_staff') as chief:
            self.assertEqual(sy.main(['team', 'task', '--project', str(self.project), 'Build a feature']), 0)
        chief.assert_not_called()
        self.assertEqual(run_project.call_args.args[0].rest, ['Build a feature'])

    def test_runtime_only_opens_chief_with_opening_prompt(self):
        for flags in (['-x', 'codex'], ['--runtime', 'codex'],
                      ['--runtime=codex'], ['-xcodex'], ['-x', 'claude']):
            with self.subTest(flags=flags), \
                 patch.object(sy, 'require_clis'), \
                 patch.object(sy.runtime, 'start_monitor'), \
                 patch.object(sy.runtime, 'supervise', return_value=(0, None)) as supervise, \
                 patch.object(sy, 'cmd_run') as run_project, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(sy.main(flags), 0)
                run_project.assert_not_called()
                argv = supervise.call_args.args[0]
                self.assertEqual(argv[0], 'claude' if flags[-1] == 'claude' else 'codex')
                self.assertEqual(supervise.call_args.args[1], self.workspace)
                self.assertIn('brief greeting', argv[-1])
                self.assertIn('current briefing', argv[-1])
                self.assertIn('Follow up conversationally', argv[-1])

    def test_runtime_with_project_or_task_still_opens_orchestrator(self):
        for flags in ([], ['-x', 'codex'], ['-x', 'codex', '--project', '.'], ['-x', 'codex', 'Build a feature']):
            with self.subTest(flags=flags), \
                 patch.object(sy, 'resolve_project', return_value=self.project), \
                 patch.object(sy, 'cmd_run', return_value=0) as run_project, \
                 patch.object(sy, 'cmd_chief_of_staff') as chief:
                self.assertEqual(sy.main(['team', 'task', *flags]), 0)
                run_project.assert_called_once()
                chief.assert_not_called()

    def test_top_level_project_options_require_team(self):
        with patch.object(sy, 'cmd_run') as run_project, \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            sy.main(['-C', str(self.project), 'Build a feature'])
        run_project.assert_not_called()

    def test_top_level_task_goes_to_chief(self):
        with patch.object(sy, 'cmd_chief_of_staff', return_value=0) as chief, \
             patch.object(sy, 'cmd_run') as run_project:
            self.assertEqual(sy.main(['-x', 'codex', 'Brief me']), 0)
        self.assertEqual(chief.call_args.args[0].task, 'Brief me')
        self.assertEqual(chief.call_args.args[0].runtime, 'codex')
        run_project.assert_not_called()

    def test_role_is_separate_from_project_rosters(self):
        team = sy.resolve_team(self.project)
        self.assertNotIn('chief-of-staff', team.roles)
        claude_agents = json.loads(sy.build_claude(team, False, [])[4])
        self.assertNotIn('chief-of-staff', claude_agents)
        argv = sy.build_codex(team, False, [])
        self.assertFalse(any('agents.chief-of-staff' in item for item in argv))
        self.assertNotIn('Chief of Staff\n', sy.orchestrator_body(team))

    def test_both_provider_builders_are_isolated(self):
        for provider in ('claude', 'codex'):
            for headless in (False, True):
                argv = sy.build_chief_of_staff(provider, self.workspace, headless, 'Coordinate work')
                self.assertEqual(argv[0], provider)
                self.assertEqual(argv[-2:], ['--', 'Coordinate work'])
                self.assertFalse(any('dangerously' in arg for arg in argv))
                if provider == 'claude':
                    agents = json.loads(argv[argv.index('--agents') + 1])
                    self.assertEqual(set(agents), {'chief-of-staff'})
                    tools = ['Read', 'Bash'] + ([] if headless else sy.SCHEDULER_TOOLS)
                    self.assertEqual(agents['chief-of-staff']['tools'], tools)
                    self.assertEqual(argv[argv.index('--tools') + 1], ','.join(tools))
                    self.assertEqual('-p' in argv, headless)
                    self.assertIn('statusLine', json.loads(argv[argv.index('--settings') + 1]))
                else:
                    self.assertEqual('exec' in argv, headless)
                    self.assertEqual('--skip-git-repo-check' in argv, headless)
                    self.assertEqual(argv[argv.index('-C') + 1], str(self.workspace))
                    self.assertEqual(argv[argv.index('--add-dir') + 1], str(self.workspace.parent))
                    self.assertIn('multi_agent', argv)
                    self.assertIn('multi_agent_v2', argv)
                    self.assertFalse(any('agents.backend-engineer' in item for item in argv))

    def test_dry_run_writes_nothing_and_needs_no_provider(self):
        with patch.object(sy, 'require_clis') as require, patch.object(sy.runtime, 'supervise') as supervise:
            result, output = self.command('-x', 'codex', '--dry-run', 'Brief me')
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output)['cwd'], str(self.workspace))
        require.assert_not_called()
        supervise.assert_not_called()
        self.assertFalse(self.workspace.parent.exists())
        self.assertFalse(sy.REGISTRY_PATH.exists())

    def test_chief_inherits_user_inbox_settings_but_not_project_settings(self):
        self.user_config('inbox-interval = 10\ninbox-transport = poll\n')
        self.config('inbox-interval = 15\n')
        result, output = self.command('-x', 'claude', '--dry-run')
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output)['inbox'], dict(enabled=True, interval=10, transport='poll'))
        result, output = self.command('-x', 'claude', '--inbox-interval', '30', '--dry-run')
        self.assertEqual(json.loads(output)['inbox']['interval'], 30)

    def test_supervised_launch_keeps_project_state_and_registry_untouched(self):
        sy.write_status_stub(self.project, 123, 'Existing project work')
        before = sy.status_path(self.project).read_bytes()
        for provider in ('claude', 'codex'):
            with patch.object(sy, 'require_clis') as require, \
                 patch.object(sy.runtime, 'start_monitor') as monitor, \
                 patch.object(sy.runtime, 'supervise', return_value=(0, None)) as supervise:
                result, _ = self.command('-x', provider, '-p', 'Coordinate existing work')
            self.assertEqual(result, 0)
            require.assert_called_once_with({provider})
            monitor.assert_called_once_with(sy.SCRIPT)
            call = supervise.call_args
            self.assertEqual(call.args[1:3], (self.workspace, {provider}))
            self.assertTrue(call.kwargs['headless'])
            status = sy.runtime.read_json(self.workspace / '.syndicate' / 'status.json')
            self.assertEqual(status['current']['role'], 'chief-of-staff')
            self.assertEqual(status['state'], 'done')
        self.assertEqual(before, sy.status_path(self.project).read_bytes())
        self.assertFalse(sy.REGISTRY_PATH.exists())
        self.assertFalse(sy.coordination_store().path.exists())

    def test_lock_blocks_second_chief_without_changing_state(self):
        self.workspace.mkdir(parents=True)
        local = self.workspace / '.syndicate'
        local.mkdir()
        (local / 'status.json').write_text('{"state":"running"}')
        with (self.workspace.parent / 'chief-of-staff.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(sy, 'require_clis'), patch.object(sy.runtime, 'supervise') as supervise:
                self.assertEqual(self.command('-x', 'codex')[0], 1)
            supervise.assert_not_called()
        self.assertEqual((local / 'status.json').read_text(), '{"state":"running"}')

    def test_pid_is_recorded_while_running_and_cleared_on_exit(self):
        path = self.workspace.parent / 'chief-of-staff.lock'
        def supervised(*args, **kwargs):
            self.assertEqual(path.read_text(), f'{os.getpid()}\n')
            fd, = kwargs['pass_fds']
            self.assertEqual(os.fstat(fd).st_ino, path.stat().st_ino)
            with self.assertRaisesRegex(sy.SyndicateError, f'PID {os.getpid()}'):
                with sy.chief_session_lock():
                    self.fail('second owner acquired the lock')
            return 0, None
        with patch.object(sy, 'require_clis'), patch.object(sy.runtime, 'start_monitor'), \
             patch.object(sy.runtime, 'supervise', side_effect=supervised):
            self.assertEqual(self.command('-x', 'codex')[0], 0)
        self.assertEqual(path.read_text(), '')

    def test_stale_malformed_and_reused_pid_records_do_not_block(self):
        path = self.workspace.parent / 'chief-of-staff.lock'
        path.parent.mkdir(parents=True)
        for record in ('999999999\n', 'not a pid\n', f'{os.getpid()}\n', ''):
            with self.subTest(record=record):
                path.write_text(record)
                with sy.chief_session_lock():
                    self.assertEqual(path.read_text(), f'{os.getpid()}\n')
                self.assertEqual(path.read_text(), '')

    def test_direct_owner_blocks_tmux_and_tmux_child_launches(self):
        with sy.chief_session_lock():
            for argv in (['-x', 'codex'], ['_in-tmux', '-x', 'claude']):
                with self.subTest(argv=argv), \
                     patch.object(sy, 'tmux_enabled', return_value=True), \
                     patch.object(sy, 'launch_tmux') as launch, \
                     patch.object(sy, 'require_clis'), \
                     patch.object(sy.runtime, 'supervise') as supervise:
                    self.assertEqual(sy.main(argv), 1)
                    launch.assert_not_called()
                    supervise.assert_not_called()

    def test_killed_owner_does_not_leave_a_stale_lock(self):
        path = self.workspace.parent / 'chief-of-staff.lock'
        path.parent.mkdir(parents=True)
        code = ("import fcntl,os,sys,time; "
                "f=open(sys.argv[1], 'a+'); fcntl.flock(f, fcntl.LOCK_EX); "
                "f.seek(0); f.truncate(); f.write(str(os.getpid())+'\\n'); f.flush(); "
                "print('locked', flush=True); time.sleep(60)")
        proc = subprocess.Popen([sys.executable, '-c', code, str(path)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), 'locked')
            with self.assertRaisesRegex(sy.SyndicateError, str(proc.pid)):
                with sy.chief_session_lock():
                    self.fail('lock acquired while owner was alive')
            proc.kill()
            proc.wait(timeout=5)
            self.assertEqual(path.read_text(), f'{proc.pid}\n')
            with sy.chief_session_lock():
                self.assertEqual(path.read_text(), f'{os.getpid()}\n')
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            proc.stdout.close()

    def make_snapshot(self, provider='claude', role='chief-of-staff'):
        snapshot = self.project / 'snapshot'
        snapshot.mkdir(exist_ok=True)
        (snapshot / 'checkpoint.md').write_text('Pending user decision abc.')
        (snapshot / 'handoff.md').write_text('Continue portfolio coordination.')
        (snapshot / 'status.json').write_text(json.dumps(dict(provider=provider, current=dict(role=role))))
        return snapshot

    def test_handoff_keeps_provider_unless_explicitly_changed(self):
        snapshot = self.make_snapshot()
        with patch.dict(sy.os.environ, {'SYNDICATE_RUNTIME': 'codex'}):
            result, output = self.command('--dry-run', '--handoff', str(snapshot))
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output)['provider'], 'claude')
        self.assertIn(str(snapshot), json.loads(output)['argv'][-1])
        result, output = self.command('-x', 'codex', '--dry-run', '--handoff', str(snapshot))
        self.assertEqual(json.loads(output)['provider'], 'codex')

    def test_invalid_handoff_and_reserve_do_not_launch(self):
        with patch.object(sy.runtime, 'supervise') as supervise:
            for reserve in ('nan', 'inf', '0', '100', '-1'):
                self.assertEqual(self.command('--reserve', reserve)[0], 1)
            snapshot = self.make_snapshot(role='orchestrator')
            self.assertEqual(self.command('--handoff', str(snapshot))[0], 1)
            (snapshot / 'status.json').write_text('{"current":[]}')
            self.assertEqual(self.command('--handoff', str(snapshot))[0], 1)
        supervise.assert_not_called()
        self.assertFalse(self.workspace.exists())

    def test_quota_stop_saves_chief_handoff_without_provider_switch(self):
        def stopped(argv, workspace, providers, threshold, task, provider, **kwargs):
            snapshot = sy.runtime.create_handoff(workspace, provider, task, 'quota reserve', 'test')
            return 75, snapshot
        with patch.object(sy, 'require_clis'), patch.object(sy.runtime, 'start_monitor'), \
             patch.object(sy.runtime, 'supervise', side_effect=stopped), \
             patch.object(sy.runtime, 'choose_target') as choose:
            result, output = self.command('-x', 'claude', '-p', 'Coordinate projects')
        self.assertEqual(result, 75)
        self.assertIn('Handoff saved', output)
        choose.assert_not_called()
        status = sy.runtime.read_json(self.workspace / '.syndicate' / 'status.json')
        self.assertEqual(status['state'], 'waiting_on_user')
        self.assertEqual(status['current']['role'], 'chief-of-staff')
        snapshot = sy.runtime.read_json(self.workspace / '.syndicate' / 'handoff.json')['path']
        result, _ = self.command('--dry-run', '--handoff', snapshot)
        self.assertEqual(result, 0)

    def test_checkpoint_survives_and_failure_is_recorded(self):
        checkpoint = self.workspace / '.syndicate' / 'checkpoint.md'
        checkpoint.parent.mkdir(parents=True)
        checkpoint.write_text('Unresolved request 123.')
        with patch.object(sy, 'require_clis'), patch.object(sy.runtime, 'start_monitor'), \
             patch.object(sy.runtime, 'supervise', side_effect=RuntimeError('launch failed')):
            with self.assertRaisesRegex(RuntimeError, 'launch failed'):
                self.command('-x', 'codex', '-p')
        self.assertEqual(checkpoint.read_text(), 'Unresolved request 123.')
        status = sy.runtime.read_json(checkpoint.parent / 'status.json')
        self.assertEqual(status['state'], 'failed')
        with patch.object(sy, 'require_clis'), patch.object(sy.runtime, 'start_monitor'), \
             patch.object(sy.runtime, 'supervise', return_value=(0, None)):
            self.assertEqual(self.command('-x', 'codex', '-p')[0], 0)

class ChiefVisibility(ProjectCase):
    def setUp(self):
        super().setUp()
        self.workspace = sy.REGISTRY_PATH.parent / 'portfolio' / 'workspace'

    def save_status(self, state='running', pid=None):
        local = self.workspace / '.syndicate'
        local.mkdir(parents=True, exist_ok=True)
        sy.runtime.atomic_json(local / 'status.json', dict(
            pid=os.getpid() if pid is None else pid, provider='codex', state=state,
            session_id='chief-session', current=dict(role='chief-of-staff', summary='Saved progress')))
        return local

    def snapshot(self):
        return sy.portfolio.chief_status(self.workspace.parent)

    def test_missing_records_are_unknown_and_reads_create_nothing(self):
        self.assertEqual(self.snapshot()['state'], 'unknown')
        self.assertFalse(self.workspace.parent.exists())

    def test_lock_overrides_stale_running_and_waiting_reports(self):
        local = self.save_status()
        (local / 'checkpoint.md').write_text('Review the release <script>.')
        for state, expected in [('running', 'running'), ('waiting_on_user', 'waiting')]:
            self.save_status(state)
            with sy.chief_session_lock():
                result = self.snapshot()
                self.assertEqual(result['state'], expected)
                self.assertTrue(result['owner_active'])
            result = self.snapshot()
            self.assertEqual(result['state'], 'stopped')
            self.assertEqual(result['report_state'], state)
            self.assertEqual(result['checkpoint']['summary'], 'Review the release <script>.')
            self.assertTrue(result['checkpoint']['updated_at'])

    def test_held_lock_requires_matching_valid_chief_status(self):
        local = self.save_status(pid=os.getpid() + 1)
        with sy.chief_session_lock():
            self.assertEqual(self.snapshot()['state'], 'unknown')
            (local / 'status.json').write_text('{broken')
            result = self.snapshot()
            self.assertEqual(result['state'], 'unknown')
            self.assertTrue(result['owner_active'])
            self.assertEqual(result['checkpoint']['state'], 'unavailable')
            self.save_status('done')
            self.assertEqual(self.snapshot()['state'], 'unknown')

    def test_unreadable_ownership_and_checkpoint_excerpt(self):
        local = self.save_status()
        (self.workspace.parent / 'chief-of-staff.lock').mkdir()
        (local / 'checkpoint.md').write_text('x' * 2500)
        result = self.snapshot()
        self.assertEqual(result['state'], 'unavailable')
        self.assertEqual(len(result['checkpoint']['summary']), 2000)
        self.assertTrue(result['checkpoint']['truncated'])

    def test_non_ascii_lock_contents_preserve_kernel_ownership(self):
        self.save_status()
        path = self.workspace.parent / 'chief-of-staff.lock'
        with path.open('wb') as lock:
            lock.write(b'\xff')
            lock.flush()
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.snapshot()
            self.assertEqual(result['state'], 'unknown')
            self.assertTrue(result['owner_active'])
            self.assertIn('contents are invalid', result['reason'])
        result = self.snapshot()
        self.assertEqual(result['state'], 'stopped')
        self.assertFalse(result['owner_active'])

    def test_briefing_and_console_keep_chief_out_of_project_ownership(self):
        self.save_status('waiting_on_user')
        with sy.chief_session_lock():
            with patch.object(sy, 'gather_statuses', return_value=[]):
                data = sy.project_briefing()
        self.assertEqual(data['chief']['state'], 'waiting')
        self.assertEqual(data['projects'], [])
        self.assertEqual(data['sessions'], [])
        output = sy.render_dashboard([], chief=data['chief'], width=50)
        self.assertIn('CHIEF OF STAFF / waiting', output)
        self.assertIn('Checkpoint unavailable', output)
        self.assertIn('no registered projects', output)
