import contextlib
import io
import json
import os
import sys
from itertools import product
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
        run_id = sy.runtime.write_run_record(self.project, 'Closeout test', 'codex', 10, (self.task_id, self.session_id))
        environment = patch.dict(os.environ, {sy.runtime.RUN_ENV: run_id})
        environment.start()
        self.addCleanup(environment.stop)

    def close(self, state='done', **overrides):
        participants = (['--implementation', 'backend-engineer', 'codex', 'implementer-42',
                         '--review', 'code-reviewer', 'codex', 'reviewer-73'] if state == 'done' else [])
        args = sy.build_parsers()[1].parse_args(['tasks', 'close', self.task_id,
            '-C', str(self.project), '--session', self.session_id, '--state', state,
            '--progress', 'Verified tests; no remaining work', '--message', 'Completed: evidence',
            *participants])
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
        self.assertEqual(task['role_participants'], {
            'implementation': [dict(role='backend-engineer', provider='codex', participant_id='implementer-42')],
            'review': [dict(role='code-reviewer', provider='codex', participant_id='reviewer-73')],
        })
        self.assertEqual(self.messages()[0]['closeout']['role_participants'], task['role_participants'])
        self.assertIn('code-reviewer (codex, reviewer-73)', self.messages()[0]['message'])
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

    def test_done_requires_distinct_specialists_in_distinct_roles(self):
        for substitutions, error in (
                (dict(implementation=None, review=None), 'requires actual implementation'),
                (dict(review=None), 'requires actual implementation'),
                (dict(review=[['code-reviewer', 'claude', 'IMPLEMENTER-42']]), 'distinct roles and participant IDs'),
                (dict(review=[['BACKEND-ENGINEER', 'codex', 'reviewer-73']]), 'distinct roles and participant IDs'),
                (dict(review=[['orchestrator', 'codex', 'reviewer-73']]), 'valid specialist roles')):
            with self.subTest(substitutions=substitutions):
                with self.assertRaisesRegex(sy.SyndicateError, error):
                    self.close(**substitutions)
        self.assertEqual(self.messages(), [])
        self.assertEqual(sy.read_status(str(self.project))['state'], 'running')

    def test_unavailable_required_reviewer_closes_as_blocked_with_actual_implementation(self):
        args = sy.build_parsers()[1].parse_args(
            ['team', 'run-role', 'code-reviewer', '--project', str(self.project), '--runtime', 'codex', 'Verify'])
        with patch.object(sy.shutil, 'which', return_value=None), patch.object(sy, 'launch_role') as launch:
            with self.assertRaisesRegex(sy.SyndicateError, "codex.*not installed") as failure:
                sy.cmd_run_role(args)
        launch.assert_not_called()

        reason = str(failure.exception)
        self.close('blocked', implementation=[['backend-engineer', 'codex', 'implementer-42']],
                   role_blocker=['code-reviewer', reason])
        record, = self.messages()
        task = sy.read_status(str(self.project))['tasks'][0]
        self.assertEqual(task['role_blocker'], dict(role='code-reviewer', reason=reason))
        self.assertEqual(task['role_participants']['review'], [])
        self.assertEqual(task['role_participants']['implementation'][0]['participant_id'], 'implementer-42')
        self.assertIsNone(task['completed_at'])
        self.assertEqual(task['state'], 'blocked')
        self.assertIn('Required role code-reviewer could not start', task['progress'])
        self.assertIn(reason, record['message'])
        with self.assertRaisesRegex(sy.portfolio.PortfolioError, 'another message'):
            self.close('blocked', implementation=[['backend-engineer', 'codex', 'implementer-42']],
                       role_blocker=['code-reviewer', 'A different launch failure'])

    def test_role_blocker_requires_blocked_state(self):
        with self.assertRaisesRegex(sy.SyndicateError, 'requires --state blocked'):
            self.close(role_blocker=['code-reviewer', 'Reviewer could not start'])
        self.assertEqual(self.messages(), [])

    def test_relay_reports_actual_provider_and_completion_id_only_on_success(self):
        args = sy.build_parsers()[1].parse_args(
            ['team', 'run-role', 'code-reviewer', '--project', str(self.project), '--runtime', 'codex', 'Verify'])
        self.config('default = claude\nhandoff = off\n')
        with patch.object(sy, 'require_clis'), patch.object(sy, 'launch_role', side_effect=(0, 17)) as launch:
            with contextlib.redirect_stdout(io.StringIO()) as completed:
                self.assertEqual(sy.cmd_run_role(args), 0)
            with contextlib.redirect_stdout(io.StringIO()) as failed:
                self.assertEqual(sy.cmd_run_role(args), 17)
        self.assertEqual(launch.call_args.args[2].cli, 'codex')
        self.assertRegex(completed.getvalue(), r'^Syndicate specialist completed: code-reviewer codex [a-f0-9]{32}\n$')
        self.assertEqual(failed.getvalue(), '')

    def fake_cli(self, name, code):
        path = self.project / name
        path.write_text(f'#!{sys.executable}\n{code}\n')
        path.chmod(0o700)

    def invoke_role(self, *runtime_args, role='backend-engineer'):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, PATH=f'{self.project}:{os.environ.get("PATH", "")}'), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = sy.main(['team', 'run-role', role, '--project', str(self.project),
                              *runtime_args, 'work'])
        return result, out.getvalue(), err.getvalue()

    def test_role_backoff_uses_last_known_usage_and_actual_provider(self):
        self.config('codex-model_verbosity = low\nclaude-model = sonnet\n')
        self.usage('codex', 5)
        self.usage('claude', 40, observed_at=100)
        self.fake_cli('codex', 'import sys; assert \'model_verbosity="low"\' in sys.argv; '
                              'print("ERROR: You\'ve hit your usage limit.", file=sys.stderr); sys.exit(1)')
        self.fake_cli('claude', 'import json, sys; assert "partial work" in sys.argv[-1]; '
                               'assert sys.argv[sys.argv.index("--model") + 1] == "sonnet"; '
                               'assert sys.argv[sys.argv.index("--output-format") + 1] == "json"; '
                               'print(json.dumps(dict(type="result", is_error=False, result="continued")))')
        result, out, err = self.invoke_role('--runtime', 'codex')
        self.assertEqual(result, 0)
        self.assertIn('retrying backend-engineer on claude (60% last recorded remaining)', err)
        self.assertIn("ERROR: You've hit your usage limit.", err)
        self.assertRegex(out, r'continued\nSyndicate specialist completed: backend-engineer claude [a-f0-9]{32}')
        self.assertEqual(self.messages(), [])

    def test_last_known_reserves_can_hold_before_any_cli_launches(self):
        self.usage('codex', 90, observed_at=100)
        self.usage('claude', 99, observed_at=100)
        self.fake_cli('codex', 'raise AssertionError("no quota left")')
        self.fake_cli('claude', 'raise AssertionError("no quota left")')
        result, out, err = self.invoke_role('--runtime', 'codex')
        self.assertEqual((result, out), (75, ''))
        self.assertIn('task is on hold', err)
        self.assertEqual(sy.read_status(str(self.project))['state'], 'on_hold')

    def test_role_from_previous_run_cannot_hold_new_owning_task(self):
        self.usage('codex', 95)
        self.usage('claude', 95)
        self.fake_cli('codex', 'raise AssertionError("no quota left")')
        self.fake_cli('claude', 'raise AssertionError("no quota left")')
        old_run_id = sy.runtime.write_run_record(self.project, 'Prior task', 'codex', 10, ('old-task', 'old-session'))
        for inherited_run_id in ('', '20260101T000000-0123abcd', old_run_id):
            with self.subTest(inherited_run_id=inherited_run_id), \
                 patch.dict(os.environ, {sy.runtime.RUN_ENV: inherited_run_id}):
                result, out, err = self.invoke_role('--runtime', 'codex')
                self.assertEqual((result, out), (1, ''))
                self.assertIn('no active owning task was placed on hold', err)
                self.assertEqual(sy.read_status(str(self.project))['state'], 'running')
                self.assertEqual(self.messages(), [])

    def test_codex_read_only_role_retains_its_permissions(self):
        self.usage('codex', 95)
        self.usage('claude', 20)
        self.fake_cli('codex', 'raise AssertionError("no quota left")')
        self.fake_cli('claude', 'raise AssertionError("read-only restrictions must be preserved")')
        result, out, err = self.invoke_role('--runtime', 'codex', role='code-reviewer')
        self.assertEqual((result, out), (1, ''))
        self.assertIn('cannot retry code-reviewer on claude with the Codex read-only workspace restrictions', err)
        self.assertEqual(sy.read_status(str(self.project))['state'], 'running')

    def test_native_backoff_skips_failed_provider_and_unknown_stays_distinct(self):
        self.usage('codex', 3)
        self.fake_cli('codex', 'raise AssertionError("failed provider must be skipped")')
        self.fake_cli('claude', 'print("continued")')
        result, out, err = self.invoke_role('--backoff-from', 'codex')
        self.assertEqual((result, out), (75, ''))
        self.assertIn('no usage reading for claude', err)
        self.assertEqual(self.messages(), [])

        self.usage('claude', 20)
        result, out, _ = self.invoke_role('--backoff-from', 'codex')
        self.assertEqual(result, 0)
        self.assertIn('Syndicate specialist completed: backend-engineer claude ', out)

    def test_all_cli_limits_put_only_owning_task_on_hold(self):
        self.usage('claude', 40)
        self.usage('codex', 5)
        self.fake_cli('codex', 'import sys; print("ERROR: You\'ve hit your usage limit.", file=sys.stderr); sys.exit(1)')
        self.fake_cli('claude', 'import json, sys; print(json.dumps(dict(type="result", is_error=True, '
                               'result="You\'ve hit your session limit · resets at 3:50pm"))); sys.exit(1)')
        self.fake_cli('unsupported', 'raise AssertionError("unregistered CLI must not run")')
        self.usage('unsupported', 0)
        before = sy.status_path(self.project).read_bytes()
        result, out, err = self.invoke_role('--runtime', 'codex')
        self.assertEqual(result, 75)
        self.assertIn('task is on hold', err)
        self.assertIn("You've hit your session limit", out)
        self.assertNotIn('Syndicate specialist completed', out)
        self.assertEqual(sy.status_path(self.project).read_bytes(), before)
        self.assertEqual(len(self.messages()), 1)
        self.assertTrue(sy.read_status(str(self.project))['tasks'][0]['progress'].startswith('Task on hold.'))
        self.assertFalse(sy.close_owned_task(self.project, self.task_id, session_id=self.session_id))
        sy.register_workspace(self.project)
        row, = sy.gather_statuses(sweep=False)
        self.assertEqual(row['project_status'], 'Needs attention')
        self.assertEqual(row['tasks'][0]['state'], 'on_hold')
        self.assertIsNone(row['tasks'][0]['completed_at'])
        self.assertEqual(row['tasks'][1]['state'], 'queued')

    def test_ordinary_role_failure_preserves_exit_and_does_not_retry(self):
        self.usage('codex', 15)
        self.usage('claude', 25)
        self.fake_cli('claude', 'raise AssertionError("ordinary error must not retry")')
        self.fake_cli('codex', "import sys; print(\"Quoted: ERROR: You've hit your usage limit.\"); "
                              "print('ERROR: transient HTTP 429', file=sys.stderr); sys.exit(17)")
        result, out, err = self.invoke_role('--runtime', 'codex')
        self.assertEqual(result, 17)
        self.assertIn('Quoted:', out)
        self.assertIn('transient HTTP 429', err)
        self.assertNotIn('retrying', err)
        self.assertEqual(self.messages(), [])

    def test_explicit_claude_text_output_preserves_ordinary_quota_prose(self):
        self.config('claude-output-format = text\n')
        self.usage('claude', 15)
        self.usage('codex', 25)
        self.fake_cli('codex', 'raise AssertionError("ordinary error must not retry")')
        prose = "You've hit your weekly limit · resets 3:50pm"
        imitation = json.dumps(dict(type="result", is_error=True, result=prose))
        response = json.dumps(dict(type="result", is_error=False, result=f"{prose}\n{imitation}"))
        self.fake_cli('claude', 'import sys; assert sys.argv[sys.argv.index("--output-format") + 1] == "json"; '
                               f'print({response!r}); sys.exit(17)')
        result, out, err = self.invoke_role('--runtime', 'claude')
        self.assertEqual((result, out), (17, f"{prose}\n{imitation}\n"))
        self.assertNotIn('retrying', err)

    def test_explicit_claude_json_output_is_forwarded_verbatim(self):
        self.config('claude-output-format = json\n')
        self.usage('claude', 15)
        response = json.dumps(dict(type="result", is_error=False, result="continued"))
        self.fake_cli('claude', 'import sys; assert sys.argv[sys.argv.index("--output-format") + 1] == "json"; '
                               f'print({response!r})')
        result, out, _ = self.invoke_role('--runtime', 'claude')
        self.assertEqual(result, 0)
        self.assertTrue(out.startswith(response + "\n"))

    def test_large_explicit_claude_json_quota_result_still_triggers_fallback(self):
        self.config('claude-output-format = json\n')
        self.usage('claude', 15)
        self.usage('codex', 25)
        response = json.dumps(dict(type="result", is_error=True, result="You've hit your usage limit.",
                                   metadata="x" * 40_000))
        self.fake_cli('claude', 'import sys; assert sys.argv[sys.argv.index("--output-format") + 1] == "json"; '
                               f'print({response!r}, end=""); sys.exit(1)')
        self.fake_cli('codex', 'print("continued")')
        result, out, err = self.invoke_role('--runtime', 'claude')
        self.assertEqual(result, 0)
        self.assertTrue(out.startswith(response))
        self.assertIn('retrying backend-engineer on codex', err)

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

    def test_managed_task_cannot_report_completion_or_blocker_via_raw_status(self):
        for reported_state, omit_flag, bad_token in product(
                ('done', 'completed', 'blocked'), (False, True), (None, 'task', 'current')):
            with self.subTest(reported_state=reported_state, omit_flag=omit_flag, bad_token=bad_token):
                def report_without_closeout():
                    status = json.loads(sy.status_path(self.project).read_text())
                    status['state'] = reported_state
                    task = next(task for task in status['tasks'] if task['id'] == self.task_id)
                    self.assertTrue(task['delegated_closeout_required'])
                    if omit_flag:
                        del task['delegated_closeout_required']
                    if bad_token == 'task':
                        task['session_id'] = []
                    if bad_token == 'current':
                        status['current']['session_id'] = []
                    task.update(state=reported_state, completed_at='2026-01-01T00:00:00Z')
                    sy.runtime.atomic_json(sy.status_path(self.project), status)
                    effective = sy.read_status(str(self.project))
                    self.assertEqual(effective['state'], 'running')
                    effective_task = next(t for t in effective['tasks'] if t['id'] == self.task_id)
                    self.assertEqual(effective_task['state'], 'running')
                    self.assertEqual(effective_task['session_id'], self.session_id)
                    self.assertEqual(effective['current']['session_id'], self.session_id)
                    self.assertIn('requires closeout', effective['current']['summary'])

                self.supervise(report_without_closeout)
                status = sy.read_status(str(self.project))
                self.assertEqual(status['state'], 'ended')
                task = next(task for task in status['tasks'] if task['id'] == self.task_id)
                self.assertTrue(task['needs_attention'])
                records = [r for r in self.messages() if r['session_id'] == self.session_id]
                self.assertEqual([r['closeout']['state'] for r in records], ['ended'])

    def test_malformed_task_session_does_not_hide_committed_closeout(self):
        def close_and_report():
            self.close()
            path = sy.status_path(self.project)
            status = json.loads(path.read_text())
            task = next(t for t in status['tasks'] if t['id'] == self.task_id)
            task['session_id'] = []
            sy.runtime.atomic_json(path, status)
            before = path.read_bytes()
            self.assertEqual(sy.read_status(str(self.project))['state'], 'done')
            self.assertEqual(path.read_bytes(), before)

        self.supervise(close_and_report)
        self.assertEqual(sy.read_status(str(self.project))['state'], 'done')
        records = [r for r in self.messages() if r['session_id'] == self.session_id]
        self.assertEqual([r['closeout']['state'] for r in records], ['done'])

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

    def test_launch_failure_requires_a_committed_closeout_to_skip_failure_write(self):
        def fail_with_raw_marker():
            status = json.loads(sy.status_path(self.project).read_text())
            status['tasks'][-1]['closeout_session'] = self.session_id
            sy.runtime.atomic_json(sy.status_path(self.project), status)
            raise OSError('supervisor launch failed')

        with self.assertRaisesRegex(OSError, 'supervisor launch failed'):
            self.supervise(fail_with_raw_marker)
        self.assertEqual(sy.read_status(str(self.project))['state'], 'failed')
        self.assertEqual(self.messages(), [])

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
        with self.assertRaisesRegex(sy.portfolio.PortfolioError, 'another message'):
            self.close(review=[['code-reviewer', 'codex', 'different-reviewer']])

    def test_legacy_done_closeout_remains_readable(self):
        self.close()
        store = sy.live.MessageStore(sy.portfolio_dir())
        records = store.read()
        del records[0]['closeout']['role_participants']
        store.path.write_text(json.dumps(records))
        status = sy.read_status(str(self.project))
        self.assertEqual(status['state'], 'done')
        self.assertNotIn('role_participants', status['tasks'][0])

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
        self.close('blocked', implementation=[['backend-engineer', 'codex', 'implementer-42']],
                   role_blocker=['code-reviewer', 'Reviewer could not start'])
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
                self.assertNotIn('role_participants', task)
                self.assertNotIn('role_blocker', task)
                self.session_id = status['current']['session_id']
                self.close(review=[['code-reviewer', 'claude', 'reviewer-after-handoff']])
                task = next(t for t in sy.read_status(str(self.project))['tasks'] if t['id'] == self.task_id)
                self.assertEqual(task['role_participants']['implementation'][0]['participant_id'], 'implementer-42')
                self.assertEqual(task['role_participants']['review'][0]['provider'], 'claude')
            return 0

        with patch.object(sy, 'run_session', side_effect=run):
            self.assertEqual(sy.cmd_run(args), 0)

    def test_both_providers_load_closeout_even_without_handoff(self):
        self.config('handoff = off\nreview-gate = off\n')
        self.role_file('orchestrator.md', 'Custom project orchestration instructions.\n')
        for host in ('claude', 'codex'):
            team = sy.resolve_team(self.project, host)
            body = sy.orchestrator_body(team)
            self.assertTrue(body.startswith('Custom project orchestration instructions.'))
            self.assertIn('# Close a Syndicate session', body)
            self.assertIn('syndicate tasks close', body)
            self.assertIn('Every project task, including small tasks and non-code deliverables', body)
            self.assertIn('--role-blocker ROLE', body)
            self.assertIn('--implementation IMPLEMENTATION_ROLE ACTUAL_PROVIDER IMPLEMENTER_ID', body)
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
