import contextlib
import io
import json
import os
from unittest.mock import patch

from test_syndicate import ProjectCase, sy


class Closeout(ProjectCase):
    def setUp(self):
        super().setUp()
        sy.write_status_stub(self.project, os.getpid(), 'Closeout test')
        status = sy.read_status(str(self.project))
        self.task_id = status['current']['task_id']
        self.owner = sy.coordination_store().start_session(str(self.project), self.task_id, 'codex', os.getpid())
        self.session_id = self.owner['id']
        status['current']['session_id'] = self.session_id
        status['tasks'][-1]['session_id'] = self.session_id
        status['tasks'].append(dict(id='unrelated', state='queued', progress='Keep this'))
        sy.runtime.atomic_json(sy.status_path(self.project), status)
        self.original = status

    def close(self, state='done', **overrides):
        args = sy.build_parsers()[1].parse_args(['tasks', 'close', self.task_id,
            '-C', str(self.project), '--session', self.session_id, '--state', state,
            '--progress', 'Verified tests; no remaining work', '--message', 'Completed: evidence'])
        for key, value in overrides.items():
            setattr(args, key, value)
        with contextlib.redirect_stdout(io.StringIO()):
            return sy.cmd_tasks_close(args)

    def messages(self):
        return sy.live.MessageStore(sy.portfolio_dir()).read()

    def test_atomic_closeout_preserves_ownership_and_other_tasks_and_deduplicates(self):
        self.close()
        first = sy.read_status(str(self.project))
        self.close()
        second = sy.read_status(str(self.project))
        self.assertEqual(first, second)
        self.assertEqual(len(self.messages()), 1)
        task = second['tasks'][0]
        self.assertEqual(task['state'], 'done')
        self.assertTrue(task['completed_at'])
        self.assertEqual(second['state'], 'done')
        self.assertEqual(second['tasks'][1], self.original['tasks'][1])
        for key in ('pid', 'process_identity', 'process_namespace', 'machine_id', 'lock_owned', 'lock_inode', 'cli_owner'):
            self.assertEqual(second.get(key), self.original.get(key))
            self.assertEqual(task.get(key), self.original['tasks'][0].get(key))

    def test_waiting_and_blocked_are_incomplete(self):
        self.close('waiting_on_user')
        self.assertIsNone(sy.read_status(str(self.project))['tasks'][0]['completed_at'])
        self.assertEqual(self.messages()[0]['closeout']['state'], 'waiting_on_user')

    def test_foreign_and_hidden_owners_cannot_close(self):
        before = sy.status_path(self.project).read_bytes()
        with self.assertRaises(sy.SyndicateError):
            self.close(session='foreign')
        with patch.object(sy.portfolio, 'process_namespace', return_value='hidden'):
            with self.assertRaisesRegex(sy.portfolio.PortfolioError, 'outside the sandbox'):
                self.close()
        self.assertEqual(sy.status_path(self.project).read_bytes(), before)
        self.assertEqual(self.messages(), [])

    def test_message_failure_does_not_change_status(self):
        before = sy.status_path(self.project).read_bytes()
        with patch.object(sy.live.MessageStore, 'update', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(sy.SyndicateError, 'before commit.*disk full'):
                self.close()
        self.assertEqual(sy.status_path(self.project).read_bytes(), before)
        self.assertEqual(self.messages(), [])

    def test_closeout_does_not_rewrite_agent_status_and_survives_exit(self):
        before = sy.status_path(self.project).read_bytes()
        self.close()
        self.assertEqual(sy.status_path(self.project).read_bytes(), before)
        self.assertEqual(sy.read_status(str(self.project))['state'], 'done')
        sy.coordination_store().end_session(self.session_id)
        self.assertEqual(sy.read_status(str(self.project))['state'], 'done')
        self.assertEqual(len(self.messages()), 1)

    def rewrite_running_report(self, updated_at):
        status = json.loads(sy.status_path(self.project).read_text())
        status['current']['summary'] = 'Closing out...'
        next(task for task in status['tasks'] if task['id'] == self.task_id)['updated_at'] = updated_at
        sy.runtime.atomic_json(sy.status_path(self.project), status)

    def test_live_closeout_wins_over_newer_same_session_report(self):
        sy.register_project(self.project)
        self.close()
        self.rewrite_running_report('2099-01-01T00:00:00Z')
        args = sy.build_parsers()[1].parse_args(['tasks', 'list', '--json'])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            sy.cmd_tasks(args)
        task = next(t for t in json.loads(output.getvalue()) if t['id'] == self.task_id)
        self.assertEqual(task['state'], 'done')
        self.assertEqual(task['progress'], self.messages()[0]['closeout']['progress'])
        row, = sy.gather_statuses(sweep=False)
        self.assertEqual(row['state'], 'done')
        self.assertEqual(row['completed_tasks'][0]['id'], self.task_id)
        briefing, = sy.project_briefing()['projects']
        self.assertEqual((briefing['task_id'], briefing['report_state']), (self.task_id, 'done'))

    def test_briefing_selects_closed_active_task_ahead_of_queued_work(self):
        sy.register_project(self.project)
        self.close()
        briefing, = sy.project_briefing()['projects']
        self.assertEqual((briefing['task_id'], briefing['report_state']), (self.task_id, 'done'))

    def test_waiting_closeout_survives_stale_session_sweep(self):
        sy.register_project(self.project)
        self.close('waiting_on_user')
        sy.coordination_store().end_session(self.session_id)
        row, = sy.gather_statuses()
        task = next(t for t in row['tasks'] if t['id'] == self.task_id)
        self.assertEqual(task['state'], 'waiting_on_user')
        self.assertIsNone(task['completed_at'])
        self.assertNotIn(self.task_id, [task['id'] for task in row['ended_tasks']])

    def test_later_launch_can_supersede_a_waiting_closeout(self):
        sy.register_project(self.project)
        self.close('waiting_on_user')
        sy.coordination_store().end_session(self.session_id)
        sy.write_status_stub(self.project, 2**30, 'Another task')
        row, = sy.gather_statuses()
        task = next(t for t in row['ended_tasks'] if t['id'] == self.task_id)
        self.assertEqual(task['last_state'], 'waiting_on_user')
        self.assertEqual(self.messages()[0]['closeout']['state'], 'waiting_on_user')

    def supervise(self, action):
        sy.coordination_store().end_session(self.session_id)
        args = sy.build_parsers()[0].parse_args(['--project', str(self.project), 'Closeout regression'])

        def run(_args):
            with sy.project_session(self.project, 'claude') as session:
                self.task_id = session['task_id']
                self.session_id = session['id']
                action()
            return 0

        with patch.object(sy, 'run_session', side_effect=run):
            self.assertEqual(sy.cmd_run(args), 0)

    def send_message(self, key):
        return sy.live.MessageStore(sy.portfolio_dir()).update(
            source_path=str(self.project), task_id=self.task_id, session_id=self.session_id,
            key=key, message='Verified outcome')

    def test_supervised_exit_preserves_existing_closeout(self):
        for state in ('done', 'ended', 'waiting_on_user', 'blocked'):
            with self.subTest(state=state):
                def close_and_report():
                    self.close(state)
                    self.rewrite_running_report('2099-01-01T00:00:00Z')

                self.supervise(close_and_report)
                status = sy.read_status(str(self.project))
                self.assertEqual(status['state'], state)
                records = [r for r in self.messages() if r['session_id'] == self.session_id]
                self.assertEqual([r['closeout']['state'] for r in records], [state])

    def test_later_different_key_message_does_not_break_supervised_exit(self):
        def close_and_send():
            self.close()
            self.send_message('pr33-merge-verified')
            self.rewrite_running_report('2099-01-01T00:00:00Z')

        self.supervise(close_and_send)
        self.assertEqual(sy.read_status(str(self.project))['state'], 'done')
        self.assertEqual([r['key'] for r in self.messages()],
                         ['session-closeout', 'pr33-merge-verified'])

    def test_supervised_exit_reuses_an_existing_same_key_message(self):
        existing = []
        self.supervise(lambda: existing.append(self.send_message('session-closeout')))
        record, = self.messages()
        self.assertEqual(record, existing[0])
        self.assertEqual(sy.read_status(str(self.project))['state'], 'running')
        row = next(row for row in sy.gather_statuses() if row['path'] == str(self.project))
        self.assertIn(self.task_id, [task['id'] for task in row['ended_tasks']])

    def test_exit_notification_failure_does_not_record_launch_failure(self):
        with patch.object(sy.live.MessageStore, 'update', side_effect=OSError('disk full')), \
                contextlib.redirect_stderr(io.StringIO()) as output:
            self.supervise(lambda: None)
        self.assertIn('disk full', output.getvalue())
        self.assertEqual(sy.read_status(str(self.project))['state'], 'running')
        self.assertEqual(self.messages(), [])

    def test_supervisor_exception_after_closeout_leaves_raw_report_alone(self):
        reports = []

        def close_then_fail():
            self.close()
            reports.append(sy.status_path(self.project).read_bytes())
            raise OSError('supervisor cleanup failed')

        with self.assertRaisesRegex(OSError, 'supervisor cleanup failed'):
            self.supervise(close_then_fail)
        self.assertEqual(sy.status_path(self.project).read_bytes(), reports[0])
        self.assertEqual(sy.read_status(str(self.project))['state'], 'done')

    def test_concurrent_unrelated_task_addition_survives_commit(self):
        original_update = sy.live.MessageStore.update
        def add_task(store, **fields):
            status = json.loads(sy.status_path(self.project).read_text())
            status['tasks'].append(dict(id='concurrent', state='queued'))
            sy.runtime.atomic_json(sy.status_path(self.project), status)
            return original_update(store, **fields)
        with patch.object(sy.live.MessageStore, 'update', add_task):
            self.close()
        status = sy.read_status(str(self.project))
        self.assertEqual(status['tasks'][-1]['id'], 'concurrent')
        self.assertEqual(status['tasks'][0]['state'], 'done')

    def test_mismatched_task_session_rejected_before_commit(self):
        status = json.loads(sy.status_path(self.project).read_text())
        status['tasks'][0]['session_id'] = 'different'
        sy.runtime.atomic_json(sy.status_path(self.project), status)
        before = sy.status_path(self.project).read_bytes()
        with self.assertRaisesRegex(sy.SyndicateError, 'owning task/session'):
            self.close()
        self.assertEqual(self.messages(), [])
        self.assertEqual(sy.status_path(self.project).read_bytes(), before)

    def test_changed_retry_rejected(self):
        self.close()
        with self.assertRaisesRegex(sy.portfolio.PortfolioError, 'another message'):
            self.close(message='Different outcome')

    def test_new_session_does_not_inherit_old_closeout(self):
        self.close()
        status = sy.read_status(str(self.project))
        status['current']['session_id'] = 'new-session'
        status['tasks'][0].update(session_id='new-session', state='running')
        status['state'] = 'running'
        sy.runtime.atomic_json(sy.status_path(self.project), status)
        self.assertEqual(sy.read_status(str(self.project))['state'], 'running')

    def test_exit_safety_net_sends_attention_once(self):
        self.assertTrue(sy.close_owned_task(self.project, self.task_id))
        self.assertFalse(sy.close_owned_task(self.project, self.task_id))
        task = sy.read_status(str(self.project))['tasks'][0]
        self.assertEqual(task['state'], 'ended')
        self.assertTrue(task['needs_attention'])
        self.assertEqual(task['last_state'], 'running')
        self.assertEqual(len(self.messages()), 1)

    def test_explicit_task_state_preserved(self):
        for state in ('done', 'blocked', 'waiting_on_user', 'failed', 'ended'):
            status = dict(self.original, state=state)
            sy.runtime.atomic_json(sy.status_path(self.project), status)
            self.assertFalse(sy.close_owned_task(self.project, self.task_id))
        self.assertEqual(self.messages(), [])

    def test_explicit_task_state_preserved_when_top_level_still_running(self):
        for state in ('done', 'blocked', 'waiting_on_user', 'failed', 'ended'):
            status = json.loads(json.dumps(self.original))
            status['tasks'][0]['state'] = state
            sy.runtime.atomic_json(sy.status_path(self.project), status)
            self.assertFalse(sy.close_owned_task(self.project, self.task_id))
            self.assertEqual(sy.read_status(str(self.project))['tasks'][0]['state'], state)
        self.assertEqual(self.messages(), [])

    def test_deliberate_handoff_preserved(self):
        sy.coordination_store().end_session(self.session_id)
        with sy.project_session(self.project, 'codex', {'handoff': True}):
            pass
        self.assertEqual(sy.read_status(str(self.project))['state'], 'running')
        self.assertEqual(self.messages(), [])

    def test_resume_clears_prior_closeout_before_claiming_task(self):
        self.close('ended')
        sy.coordination_store().end_session(self.session_id)
        handoff = self.project / '.syndicate' / 'handoffs' / 'run'
        handoff.mkdir(parents=True)
        (handoff / 'status.json').write_text(json.dumps(self.original))
        args = sy.build_parsers()[0].parse_args(
            ['--project', str(self.project), '--handoff', str(handoff)])

        def run(_args):
            with sy.project_session(self.project, 'codex', {'handoff': True}):
                status = sy.read_status(str(self.project))
                task = next(t for t in status['tasks'] if t['id'] == self.task_id)
                self.assertEqual((status['state'], task['state']), ('running', 'running'))
                self.assertNotEqual(task['session_id'], self.session_id)
                self.assertNotIn('closeout_session', task)
                self.assertNotIn('completed_at', task)
            return 0

        with patch.object(sy, 'run_session', side_effect=run):
            self.assertEqual(sy.cmd_run(args), 0)

    def test_both_providers_load_closeout_even_without_handoff(self):
        self.config('handoff = off\n')
        for host in ('claude', 'codex'):
            team = sy.resolve_team(self.project, host)
            body = sy.orchestrator_body(team)
            self.assertIn('# Close a Syndicate session', body)
            self.assertIn('syndicate tasks close', body)
            argv = (sy.build_claude if host == 'claude' else sy.build_codex)(team, False, [])
            self.assertIn('# Close a Syndicate session', ' '.join(argv))

    def test_invalid_saved_patch_cannot_overwrite_ownership(self):
        self.close()
        store = sy.live.MessageStore(sy.portfolio_dir())
        for value in ({'pid': 99}, {'state': 'bogus'}, {'updated_at': 'invalid'}):
            records = self.messages()
            records[0]['closeout'].update(value)
            before = sy.status_path(self.project).read_bytes()
            valid = dict(self.messages()[0]['closeout'])
            store.path.write_text(json.dumps(records))
            with self.assertRaisesRegex(sy.portfolio.PortfolioError, 'Cannot read messages'):
                sy.read_status(str(self.project))
            self.assertEqual(sy.status_path(self.project).read_bytes(), before)
            records[0]['closeout'] = valid
            store.path.write_text(json.dumps(records))

    def test_closeout_only_replaces_atomic_message_record(self):
        before = sy.coordination_store().path.read_bytes()
        atomic = sy.runtime.atomic_json
        writes = []
        def track(path, data):
            writes.append(path)
            return atomic(path, data)
        with patch.object(sy.runtime, 'atomic_json', side_effect=track):
            self.close()
        self.assertEqual(writes, [sy.live.MessageStore(sy.portfolio_dir()).path])
        self.assertEqual(sy.coordination_store().path.read_bytes(), before)

    def test_effective_task_timestamp_is_the_closeout_commit_time(self):
        self.close()
        status = sy.read_status(str(self.project))
        task = sy.project_tasks(status)[0]
        self.assertEqual(task['updated_at'], self.messages()[0]['closeout']['updated_at'])
