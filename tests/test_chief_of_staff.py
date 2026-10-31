import contextlib
import fcntl
import io
import json
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
                    self.assertEqual(agents['chief-of-staff']['tools'], ['Read', 'Bash'])
                    self.assertEqual(argv[argv.index('--tools') + 1], 'Read,Bash')
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
