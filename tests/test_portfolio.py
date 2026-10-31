import concurrent.futures
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_syndicate import sy


class Decisions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = sy.portfolio.DecisionStore(self.root / 'portfolio')
        self.request = dict(source_path=str(self.root), project_path=str(self.root),
                            task_id='task', key='database', question='Which database?',
                            context='Need persistence', options=['SQLite', 'Postgres'],
                            recommendation='SQLite', blocking=True)

    def test_lifecycle_dedup_and_owner_acknowledgment(self):
        first = self.store.request(**self.request)
        again = self.store.request(**{**self.request, 'context': 'Updated context'})
        self.assertEqual(first['id'], again['id'])
        self.assertEqual(len(self.store.read()), 1)
        with self.assertRaises(sy.portfolio.PortfolioError):
            self.store.transition(first['id'], 'acknowledge', source_path=str(self.root), task_id='task')
        self.store.transition(first['id'], 'answer', answer='SQLite')
        self.store.request(**self.request)
        self.assertEqual(self.store.read()[0]['state'], 'answered')
        with self.assertRaises(sy.portfolio.PortfolioError):
            self.store.transition(first['id'], 'acknowledge', source_path='/wrong', task_id='task')
        self.assertEqual(self.store.read()[0]['state'], 'answered')
        resolved = self.store.transition(first['id'], 'acknowledge', source_path=str(self.root), task_id='task', answer_revision=1)
        self.assertEqual(resolved['state'], 'resolved')
        self.assertFalse(sy.portfolio.pending(self.store.read()))
        self.assertEqual(self.store.request(**self.request)['state'], 'resolved')

    def test_revised_answer_rejects_stale_and_unversioned_acknowledgment(self):
        record = self.store.request(**self.request)
        first = self.store.transition(record['id'], 'answer', answer='SQLite')
        latest = self.store.transition(record['id'], 'answer', answer='Postgres')
        self.assertGreater(latest['answer_revision'], first['answer_revision'])
        before = self.store.path.read_bytes()
        for revision in (None, first['answer_revision'], True):
            with self.assertRaisesRegex(sy.portfolio.PortfolioError, 'revision'):
                self.store.transition(record['id'], 'acknowledge', source_path=str(self.root),
                                      task_id='task', answer_revision=revision)
            self.assertEqual(self.store.path.read_bytes(), before)
        resolved = self.store.transition(record['id'], 'acknowledge', source_path=str(self.root),
                                        task_id='task', answer_revision=latest['answer_revision'])
        self.assertEqual(resolved['state'], 'resolved')
        self.assertEqual(resolved['answer'], 'Postgres')
        self.assertEqual(resolved['history'][-1]['answer_revision'], latest['answer_revision'])

    def test_legacy_answers_receive_deterministic_revision_and_increment(self):
        record = self.store.request(**self.request)
        self.store.transition(record['id'], 'answer', answer='SQLite')
        data = json.loads(self.store.path.read_text())
        del data['decisions'][0]['answer_revision']
        self.store.path.write_text(json.dumps(data))
        self.assertEqual(self.store.read()[0]['answer_revision'], 1)
        changed = self.store.transition(record['id'], 'answer', answer='Postgres')
        self.assertEqual(changed['answer_revision'], 2)
        with self.assertRaisesRegex(sy.portfolio.PortfolioError, 'revision'):
            self.store.transition(record['id'], 'acknowledge', source_path=str(self.root),
                                  task_id='task', answer_revision=1)

    def test_invalid_transitions_and_changed_question(self):
        record = self.store.request(**self.request)
        for answer in ('', '   '):
            with self.assertRaises(sy.portfolio.PortfolioError):
                self.store.transition(record['id'], 'answer', answer=answer)
        with self.assertRaises(sy.portfolio.PortfolioError):
            self.store.request(**{**self.request, 'question': 'A different question?'})
        self.store.transition(record['id'], 'withdraw')
        with self.assertRaises(sy.portfolio.PortfolioError):
            self.store.transition(record['id'], 'answer', answer='Too late')
        self.assertEqual(self.store.read()[0]['state'], 'withdrawn')

    def test_concurrent_writers_preserve_decisions(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda index: self.store.request(**{**self.request, 'key': str(index)}), range(24)))
        self.assertEqual(len(self.store.read()), 24)
        self.assertEqual(len({d['id'] for d in self.store.read()}), 24)

    def test_corrupt_storage_is_not_overwritten(self):
        self.store.directory.mkdir()
        for text in ('broken json', '{}', '{"version":1,"decisions":[{}]}'):
            self.store.path.write_text(text)
            with self.assertRaises(sy.portfolio.PortfolioError):
                self.store.request(**self.request)
            self.assertEqual(self.store.path.read_text(), text)

    def test_cli_roundtrip_and_source_preservation(self):
        source = self.root / 'project'
        source.mkdir()
        sy.write_status_stub(source, 123, 'Build inbox')
        original = sy.status_path(source).read_bytes()
        task_id = json.loads(original)['current']['task_id']
        with patch.object(sy, 'REGISTRY_PATH', self.root / 'projects.json'):
            def run(args):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(sy.main(args), 0)
                return out.getvalue()
            record = json.loads(run(['decisions', 'request', 'Which database?', '-C', str(source),
                                     '--key', 'database', '--blocking', '--option', 'SQLite']))
            listed = json.loads(run(['decisions', 'list', '--json']))
            self.assertEqual(listed[0]['task_id'], task_id)
            run(['decisions', 'answer', record['id'], 'SQLite'])
            self.assertEqual(json.loads(run(['decisions', 'list', '--json']))[0]['state'], 'answered')
            run(['decisions', 'acknowledge', record['id'], '-C', str(source), '--answer-revision', '1'])
            self.assertEqual(json.loads(run(['decisions', 'list', '--json'])), [])
            self.assertEqual(json.loads(run(['decisions', 'list', '--all', '--json']))[0]['state'], 'resolved')
            self.assertEqual(json.loads(run(['briefing', '--json']))['decisions'], [])
        self.assertEqual(original, sy.status_path(source).read_bytes())

    def test_assignment_filter_and_unknown_provider(self):
        source = self.root / 'source'
        source.mkdir()
        sy.write_status_stub(source, 123, 'Assigned task')
        status = json.loads(sy.status_path(source).read_text())
        task_id = status['current']['task_id']
        row = dict(path='/assigned/repo', name='repo', tasks=[dict(
            id=task_id, source_path=str(source), summary='Assigned task', state='stale')],
            completed_tasks=[], project_status='Needs attention')
        with patch.object(sy, 'REGISTRY_PATH', self.root / 'projects.json'), \
             patch.object(sy, 'gather_statuses', return_value=[row]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(sy.main(['decisions', 'request', 'Proceed?', '-C', str(source), '--key', 'proceed']), 0)
            record = json.loads(output.getvalue())
            self.assertEqual(record['source_path'], str(source))
            self.assertEqual(record['project_path'], '/assigned/repo')
            output.seek(0)
            output.truncate()
            with patch.object(sy, 'resolve_project', return_value=Path('/assigned/repo')):
                self.assertEqual(sy.main(['decisions', 'list', '--project', 'repo', '--json']), 0)
            self.assertEqual(json.loads(output.getvalue())[0]['id'], record['id'])
        self.assertEqual(sy.portfolio.briefing([row], [])['projects'][0]['provider'], 'unknown')

    def test_same_key_in_different_tasks_and_answer_history(self):
        first = self.store.request(**self.request)
        second = self.store.request(**{**self.request, 'task_id': 'other-task'})
        self.assertNotEqual(first['id'], second['id'])
        self.store.transition(first['id'], 'answer', answer='SQLite')
        updated = self.store.transition(first['id'], 'answer', answer='Postgres')
        self.assertEqual(updated['answer'], 'Postgres')
        self.assertEqual([h['answer'] for h in updated['history']], ['SQLite', 'Postgres'])

    def test_briefing_and_console_pending_answers(self):
        record = self.store.request(**self.request)
        rows = [dict(name='project', path=str(self.root), project_status='Needs attention',
                     tasks=[dict(summary='Work', progress='Blocked', state='stale', provider='codex')])]
        brief = sy.portfolio.briefing(rows, self.store.read())
        self.assertIn(record['id'], brief['next_action'])
        self.assertEqual(brief['projects'][0]['provider'], 'codex')
        self.assertEqual(brief['projects'][0]['report_state'], 'stale')
        self.store.transition(record['id'], 'answer', answer='SQLite')
        text = sy.render_dashboard(rows, decisions=self.store.read(), details=True)
        self.assertIn('awaiting team acknowledgment', text)
        self.assertIn('Answer: SQLite', text)
        self.assertLess(text.index('DECISION INBOX'), text.index('PROJECT '))


class Coordination(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = sy.portfolio.CoordinationStore(self.root / 'portfolio')
        identity = patch.object(sy.portfolio, 'process_identity', return_value='birth')
        identity.start()
        self.addCleanup(identity.stop)
        registry = patch.object(sy, 'REGISTRY_PATH', self.root / 'projects.json')
        registry.start()
        self.addCleanup(registry.stop)

    def start(self, source='/source', task='task', provider='claude'):
        return self.store.start_session(source, task, provider, 123)

    def send(self, source='/source', task='task', key='next'):
        return self.store.send_request(source_path=source, project_path='/project',
                                       task_id=task, key=key, message='Review the release plan')

    def test_brief_revisions_merge_fields_and_detect_conflicts(self):
        first = self.store.update_brief('/project', dict(goal='Ship the release', constraints=['Stay offline']), 0)
        self.assertEqual(first['revision'], 1)
        with self.assertRaises(sy.portfolio.PortfolioError):
            self.store.update_brief('/project', dict(goal='Overwrite'), 0)
        second = self.store.update_brief('/project', dict(priority='high', next_steps=['Review']), 1)
        self.assertEqual(second['goal'], 'Ship the release')
        self.assertEqual(second['constraints'], ['Stay offline'])
        self.assertEqual(second['priority'], 'high')
        self.assertEqual(second['revision'], 2)
        self.store.update_brief('/project', dict(constraints=[]), 2)
        self.assertEqual(self.store.read()['briefs']['/project']['constraints'], [])

    def test_brief_invalid_fields_do_not_corrupt_file(self):
        self.store.update_brief('/project', dict(goal='Goal'))
        before = self.store.path.read_bytes()
        for changes in ({'unknown': 'value'}, {'priority': 'super'}, {'constraints': ['']}, {}):
            with self.assertRaises(sy.portfolio.PortfolioError):
                self.store.update_brief('/project', changes)
        self.assertEqual(before, self.store.path.read_bytes())

    def test_simultaneous_brief_writers_preserve_projects(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda i: self.store.update_brief('/project/' + str(i), {'goal': 'Goal'}), range(20)))
        self.assertEqual(len(self.store.read()['briefs']), 20)

    def test_concurrent_project_sessions_and_exclusive_source(self):
        first = self.start('/theforge', provider='claude')
        second = self.start('/graphcue', provider='codex')
        with self.assertRaises(sy.portfolio.PortfolioError):
            self.start('/theforge', task='other')
        live = self.store.snapshot()['sessions']
        self.assertEqual({s['provider'] for s in live if s['live']}, {'claude', 'codex'})
        self.store.end_session(first['id'])
        self.assertFalse(self.store.snapshot()['sessions'][0]['live'])
        self.assertTrue(self.store.snapshot()['sessions'][1]['live'])
        self.assertNotEqual(first['id'], second['id'])

    def test_request_receipt_requires_owning_session_and_note(self):
        owner = self.start()
        other = self.start('/other')
        request = self.send()
        self.assertEqual(request['session_id'], owner['id'])
        self.assertEqual(self.send()['id'], request['id'])
        for source, task, session, note in [('/other', 'task', other['id'], 'Read'),
                                           ('/source', 'wrong', owner['id'], 'Read'),
                                           ('/source', 'task', owner['id'], '')]:
            with self.assertRaises(sy.portfolio.PortfolioError):
                self.store.transition_request(request['id'], 'acknowledge', source_path=source,
                                              task_id=task, session_id=session, note=note)
        result = self.store.transition_request(request['id'], 'acknowledge', source_path='/source',
                                  task_id='task', session_id=owner['id'], note='Added release review to plan')
        self.assertEqual(result['state'], 'acknowledged')
        self.store.end_session(owner['id'])
        self.assertEqual(self.store.read()['requests'][0]['state'], 'acknowledged')

    def test_offline_request_waits_for_its_task(self):
        request = self.send()
        self.start(task='unrelated')
        self.assertEqual(self.store.snapshot()['requests'][0]['delivery_state'], 'waiting_for_session')
        self.store.end_session(self.store.read()['sessions'][0]['id'])
        owner = self.start()
        self.assertEqual(self.store.snapshot()['requests'][0]['delivery_state'], 'awaiting_acknowledgment')
        self.store.transition_request(request['id'], 'acknowledge', source_path='/source', task_id='task',
                                      session_id=owner['id'], note='Incorporated')

    def test_session_exit_failure_retry_and_cancellation(self):
        first = self.start()
        request = self.send()
        self.store.end_session(first['id'])
        self.assertEqual(self.store.read()['requests'][0]['state'], 'failed')
        second = self.start()
        retried = self.store.retry_request(request['id'])
        self.assertEqual(retried['session_id'], second['id'])
        with self.assertRaises(sy.portfolio.PortfolioError):
            self.store.retry_request(request['id'])
        with self.assertRaises(sy.portfolio.PortfolioError):
            self.store.transition_request(request['id'], 'acknowledge', source_path='/source', task_id='task',
                                          session_id=first['id'], note='Old session')
        failed = self.store.transition_request(request['id'], 'fail', source_path='/source', task_id='task',
                          session_id=second['id'], note='Requires requester approval')
        self.assertEqual(failed['state'], 'failed')
        self.assertEqual(self.store.transition_request(request['id'], 'cancel')['state'], 'cancelled')

    def test_pid_reuse_and_abrupt_exit_are_visible(self):
        owner = self.start()
        request = self.send()
        with patch.object(sy.portfolio, 'process_identity', return_value='different-birth'):
            snapshot = self.store.snapshot()
            self.assertFalse(snapshot['sessions'][0]['live'])
            self.assertEqual(snapshot['requests'][0]['delivery_state'], 'failed')
            with self.assertRaises(sy.portfolio.PortfolioError):
                self.store.transition_request(request['id'], 'acknowledge', source_path='/source', task_id='task',
                                               session_id=owner['id'], note='Wrong process')

    def test_concurrent_requests_deduplicate_without_lost_messages(self):
        self.start()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            records = list(pool.map(lambda i: self.send(key='same' if i < 8 else str(i)), range(20)))
        self.assertEqual(len({r['id'] for r in records[:8]}), 1)
        self.assertEqual(len(self.store.read()['requests']), 13)

    def test_live_current_task_wins_over_stale_attention_and_retains_owners(self):
        source = self.root / 'source'
        source.mkdir()
        sy.register_project(source)
        sy.write_status_stub(source, 2**30, 'Old interrupted work')
        old_id = json.loads(sy.status_path(source).read_text())['current']['task_id']
        sy.write_status_stub(source, sy.os.getpid(), 'Current Codex work')
        current_id = json.loads(sy.status_path(source).read_text())['current']['task_id']
        store = sy.coordination_store()
        owner = store.start_session(str(source), current_id, 'codex', sy.os.getpid())
        rows = sy.gather_statuses()
        self.assertEqual(rows[0]['tasks'][0]['id'], old_id)
        self.assertTrue(next(t for t in rows[0]['tasks'] if t['id'] == current_id)['is_current'])
        brief = sy.portfolio.briefing(rows, [], store.snapshot())['projects'][0]
        self.assertEqual(brief['task_id'], current_id)
        self.assertEqual(brief['source_path'], str(source))
        self.assertEqual(brief['task'], 'Current Codex work')
        self.assertEqual(brief['provider'], 'codex')
        self.assertEqual(brief['sessions'][0]['id'], owner['id'])
        self.assertEqual(brief['status'], 'Needs attention')
        store.end_session(owner['id'])
        stopped = sy.portfolio.briefing(rows, [], store.snapshot())['projects'][0]
        self.assertEqual(stopped['task_id'], current_id)
        self.assertFalse(stopped['sessions'][0]['live'])

    def test_ownership_is_independent_of_current_task_and_follows_assignment(self):
        owner = self.start('/source', task='old')
        rows = [dict(name='source', path='/source', project_status='No tasks', tasks=[]),
                dict(name='assigned', path='/assigned', project_status='Working', tasks=[
                    dict(id='old', source_path='/source', summary='Owned work', state='running'),
                    dict(id='current', source_path='/other', summary='Other work', state='running', is_current=True)])]
        brief = sy.portfolio.briefing(rows, [], self.store.snapshot())
        self.assertEqual(brief['projects'][0]['sessions'], [])
        self.assertEqual(brief['projects'][1]['sessions'][0]['id'], owner['id'])
        self.assertEqual(brief['projects'][1]['task_id'], 'old')
        # With no live owner, the explicit current task wins, but historical ownership remains visible.
        self.store.end_session(owner['id'])
        stopped = sy.portfolio.briefing(rows, [], self.store.snapshot())['projects'][1]
        self.assertEqual(stopped['task_id'], 'current')
        self.assertEqual(stopped['sessions'][0]['id'], owner['id'])
        self.assertEqual(stopped['provider'], 'unknown')

    def test_corrupt_coordination_is_not_overwritten(self):
        self.store.directory.mkdir()
        for data in ('bad json', '{}', '{"version":1,"briefs":{},"sessions":[{}],"requests":[]}'):
            self.store.path.write_text(data)
            with self.assertRaises(sy.portfolio.PortfolioError):
                self.store.update_brief('/project', {'goal': 'Goal'})
            self.assertEqual(self.store.path.read_text(), data)

    def test_project_session_publishes_token_and_closes_on_error(self):
        source = self.root / 'source'
        source.mkdir()
        sy.write_status_stub(source, 123, 'Task')
        task_id = json.loads(sy.status_path(source).read_text())['current']['task_id']
        with self.assertRaisesRegex(RuntimeError, 'launch error'):
            with sy.project_session(source, 'codex') as session:
                current = json.loads(sy.status_path(source).read_text())['current']
                self.assertEqual(current['session_id'], session['id'])
                self.assertEqual(session['task_id'], task_id)
                self.assertTrue(sy.coordination_store().snapshot()['sessions'][0]['live'])
                raise RuntimeError('launch error')
        self.assertEqual(sy.coordination_store().read()['sessions'][0]['state'], 'ended')

    def test_active_ownership_blocks_launch_before_status_overwrite(self):
        source = self.root / 'source'
        source.mkdir()
        sy.write_status_stub(source, 123, 'Existing task')
        before = sy.status_path(source).read_bytes()
        task_id = json.loads(before)['current']['task_id']
        sy.coordination_store().start_session(str(source), task_id, 'claude', 123)
        args = sy.build_parsers()[0].parse_args(['-C', str(source), 'New task'])
        with self.assertRaisesRegex(sy.SyndicateError, 'already owns'):
            sy.cmd_run(args)
        self.assertEqual(before, sy.status_path(source).read_bytes())

    def test_console_context_is_opt_in_and_failures_are_visible(self):
        brief = self.store.update_brief('/project', {'goal': 'Release goal', 'constraints': ['Offline']})
        owner = self.start('/project')
        request = self.send('/project')
        self.store.transition_request(request['id'], 'fail', source_path='/project', task_id='task',
                                      session_id=owner['id'], note='Cannot accept before approval')
        rows = [dict(name='project', path='/project', project_status='Working', tasks=[])]
        plain = sy.render_dashboard(rows, coordination=self.store.snapshot())
        detailed = sy.render_dashboard(rows, coordination=self.store.snapshot(), details=True)
        self.assertIn('Cannot accept before approval', plain)
        self.assertNotIn('Release goal', plain)
        self.assertIn('Release goal', detailed)
        self.assertIn('Offline', detailed)
        self.assertIn(owner['id'], detailed)

    def test_cli_briefs_sessions_requests_and_briefing(self):
        source = self.root / 'source'
        source.mkdir()
        sy.write_status_stub(source, 123, 'Task')
        before = sy.status_path(source).read_bytes()
        task_id = json.loads(before)['current']['task_id']
        owner = sy.coordination_store().start_session(str(source), task_id, 'codex', 123)

        def run(args):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(sy.main(args), 0)
            return output.getvalue()

        with patch.object(sy, 'resolve_project', return_value=source):
            brief = json.loads(run(['briefs', 'update', '--project', 'source', '--goal', 'Ship',
                                   '--constraint', 'Offline', '--priority', 'high', '--revision', '0']))
            self.assertEqual(brief['revision'], 1)
            self.assertEqual(json.loads(run(['briefs', 'show', '--project', 'source', '--json']))['goal'], 'Ship')
            request = json.loads(run(['requests', 'send', 'Review plan', '-C', str(source), '--key', 'review']))
            listed = json.loads(run(['requests', 'list', '-C', str(source), '--task', task_id, '--json']))
            self.assertEqual(listed[0]['delivery_state'], 'awaiting_acknowledgment')
            run(['requests', 'acknowledge', request['id'], '-C', str(source), '--session', owner['id'], '--note', 'Added to plan'])
            self.assertEqual(json.loads(run(['requests', 'list', '--json'])), [])
            self.assertEqual(json.loads(run(['requests', 'list', '--all', '--json']))[0]['state'], 'acknowledged')
            self.assertEqual(json.loads(run(['sessions', '--json']))[0]['id'], owner['id'])
            self.assertIn(str(source), json.loads(run(['briefing', '--json']))['briefs'])
        self.assertEqual(before, sy.status_path(source).read_bytes())
