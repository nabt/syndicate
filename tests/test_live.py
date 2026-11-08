import contextlib
import concurrent.futures
import io
import json
import os
import pty
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from test_syndicate import sy

live = sy.live


class LiveInbox(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'repo'
        self.directory = self.root / 'portfolio'
        self.store = sy.portfolio.CoordinationStore(self.directory)
        self.owner = self.store.start_session(str(self.project), 'task', 'codex', os.getpid())
        sy.runtime.atomic_json(self.project / '.syndicate/status.json',
            dict(current=dict(task_id='task', session_id=self.owner['id'])))
        self.decisions = sy.portfolio.DecisionStore(self.directory)
        self.thread = str(uuid.uuid4())

    def request(self, **kwargs):
        fields = dict(source_path=str(self.project), project_path=str(self.project),
                      task_id='task', key='request', message='Review this plan')
        fields.update(kwargs)
        return self.store.send_request(**fields)

    def decision(self):
        return self.decisions.request(source_path=str(self.project), project_path=str(self.project),
                                      task_id='task', key='choice', question='Alpha or Beta?')

    def delivery(self, provider='codex', chief=False, run_id='run', **kwargs):
        project = self.directory / 'workspace' if chief else self.project
        if chief:
            sy.runtime.atomic_json(project / '.syndicate/status.json',
                                   dict(session_id='chief', current=dict(role='chief-of-staff')))
        delivery = live.Delivery(self.directory, project, run_id, provider, chief=chief, **kwargs)
        sy.runtime.atomic_json(project / '.syndicate/run.json',
                               dict(id=run_id, live_endpoint=str(delivery.path)))
        if provider == 'codex':
            live.attach(project, self.thread)
        return delivery

    def test_request_wakes_only_exact_owner_and_only_once(self):
        delivery = self.delivery()
        request = self.request()
        self.request(task_id='another-task', key='other')
        with patch.object(live.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            delivery.tick(1)
            delivery.tick(2)
        run.assert_called_once()
        args = run.call_args.args[0]
        self.assertEqual(args[:4], ['codex', 'queue', '--thread', self.thread])
        self.assertNotIn('--resume', args)
        self.assertIn(request['id'], args[-1])
        self.assertNotIn('another-task', args[-1])
        self.assertEqual(self.store.read()['requests'][0]['state'], 'queued')

    def test_answer_revision_wakes_team_and_resolution_wakes_chief(self):
        team = self.delivery()
        chief = self.delivery(chief=True, run_id='chief')
        question = self.decision()
        with patch.object(live.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            team.tick(1)
            run.assert_not_called()
            chief.tick(1)
            self.assertEqual(run.call_count, 1)
            self.decisions.transition(question['id'], 'answer', answer='Alpha')
            team.tick(2)
            self.assertEqual(run.call_count, 2)
            self.decisions.transition(question['id'], 'answer', answer='Beta')
            team.tick(3)
            self.assertEqual(run.call_count, 3)
            self.decisions.transition(question['id'], 'acknowledge', source_path=str(self.project),
                                      task_id='task', answer_revision=2)
            chief.tick(4)
            self.assertEqual(run.call_count, 4)

    def test_messages_and_acknowledgments_notify_chief_without_status_poll_prompts(self):
        chief = self.delivery(chief=True)
        message = live.MessageStore(self.directory).update(source_path=str(self.project), task_id='task',
                   session_id=self.owner['id'], key='help', message='Need the requester to inspect a mockup')
        request = self.request()
        self.store.transition_request(request['id'], 'acknowledge', source_path=str(self.project),
                  task_id='task', session_id=self.owner['id'], note='Added to plan')
        with patch.object(live.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            chief.tick(1)
            prompt = run.call_args.args[0][-1]
            self.assertIn(message['id'], prompt)
            self.assertIn(request['id'], prompt)
            self.assertNotIn('briefing --json', prompt)
            sy.runtime.atomic_json(self.project / '.syndicate/status.json', dict(state='waiting_on_user'))
            chief.tick(2)
            run.assert_called_once()
        messages = live.MessageStore(self.directory)
        self.assertEqual(messages.read()[0]['state'], 'pending')
        messages.update(message_id=message['id'])
        self.assertEqual(messages.read()[0]['state'], 'acknowledged')

    def test_retry_preserves_unacknowledged_request_and_records_error(self):
        delivery = self.delivery()
        self.request()
        with patch.object(live.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'offline')) as run:
            delivery.tick(1)
            delivery.tick(2)
            run.assert_called_once()
        record = sy.runtime.read_json(delivery.path)
        self.assertEqual(record['delivered'], [])
        self.assertEqual(record['last_error'], 'offline')
        with patch.object(live.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            delivery.tick(12)
            run.assert_called_once()
        self.assertTrue(sy.runtime.read_json(delivery.path)['delivered'])

    def test_no_wake_after_owner_exits_or_for_an_old_session_binding(self):
        delivery = self.delivery()
        self.request()
        self.store.end_session(self.owner['id'])
        with patch.object(live.subprocess, 'run') as run:
            delivery.tick(1)
            run.assert_not_called()
        second = self.store.start_session(str(self.project), 'task', 'codex', os.getpid())
        events = live.events_for(self.directory, str(self.project), 'task', second['id'], False, '')
        self.assertEqual(events, [])

    def test_binding_cannot_be_replaced_or_attached_after_close(self):
        delivery = self.delivery()
        with self.assertRaises(sy.portfolio.PortfolioError):
            live.attach(self.project, str(uuid.uuid4()))
        delivery.close()
        with self.assertRaises(sy.portfolio.PortfolioError):
            live.attach(self.project, self.thread)

    def test_message_keys_are_idempotent_and_corrupt_records_are_preserved(self):
        messages = live.MessageStore(self.directory)
        fields = dict(source_path=str(self.project), task_id='task', session_id=self.owner['id'],
                      key='help', message='Need attention')
        self.assertEqual(messages.update(**fields)['id'], messages.update(**fields)['id'])
        with self.assertRaises(sy.portfolio.PortfolioError):
            messages.update(**dict(fields, message='Changed'))
        messages.path.write_text('{broken')
        with self.assertRaises(sy.portfolio.PortfolioError):
            messages.update(**fields)
        self.assertEqual(messages.path.read_text(), '{broken')

    def test_claude_channel_emits_notification_without_auto_acknowledgment(self):
        delivery = self.delivery(provider='claude', transport='channel')
        self.request()
        argv = delivery.prepare(['claude', '--agent', 'orchestrator'])
        self.assertIn('--dangerously-load-development-channels', argv)
        self.assertNotIn('--dangerously-skip-permissions', argv)
        delivery.tick(1)
        self.assertEqual(sy.runtime.read_json(delivery.path)['delivered'], [])
        proc = subprocess.Popen([sys.executable, str(Path(live.__file__)), 'channel', str(delivery.path)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        def send(message):
            proc.stdin.write(json.dumps(message).encode() + b'\n')
            proc.stdin.flush()
        def read():
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(5), 'channel did not respond')
            return json.loads(proc.stdout.readline())
        send(dict(jsonrpc='2.0', id=1, method='initialize', params=dict(protocolVersion='2024-11-05')))
        response = read()
        self.assertIn('claude/channel', response['result']['capabilities']['experimental'])
        send(dict(jsonrpc='2.0', method='notifications/initialized'))
        notification = read()
        self.assertEqual(notification['method'], 'notifications/claude/channel')
        self.assertIn('portfolio notification', notification['params']['content'])
        proc.stdin.close()
        proc.wait(timeout=5)
        self.assertEqual(proc.returncode, 0, proc.stderr.read().decode())
        proc.stdout.close()
        proc.stderr.close()
        self.assertTrue(sy.runtime.read_json(delivery.path)['delivered'])
        self.assertEqual(self.store.read()['requests'][0]['state'], 'queued')

    def test_message_cli_rejects_non_owner_and_lists_pending_records(self):
        with patch.object(sy, 'REGISTRY_PATH', self.root / 'projects.json'), \
             patch.object(sy, 'decision_owner', return_value=(self.project, 'task')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(sy.main(['messages', 'send', 'Please review', '--key', 'review',
                                     '--session', self.owner['id']]), 0)
            message_id = json.loads(output.getvalue())['id']
            self.assertEqual(sy.main(['messages', 'send', 'Forged', '--key', 'bad', '--session', 'other']), 1)
            self.assertEqual(sy.main(['messages', 'acknowledge', message_id]), 0)
            self.assertEqual(sy.main(['messages', 'list', '--json']), 0)

    def test_claude_default_polls_without_preview_and_retries_unseen_events(self):
        delivery = self.delivery(provider='claude')
        self.assertEqual(delivery.prepare(['claude', 'test']), ['claude', 'test'])
        request = self.request()
        with patch.object(live.subprocess, 'run') as run:
            delivery.tick(1)
            run.assert_not_called()
        first = live.poll(self.project)
        envelope = first['events'][0]
        self.assertEqual(envelope['schema_version'], 1)
        self.assertEqual(envelope['payload']['record_id'], request['id'])
        self.assertEqual(envelope['recipient']['session_id'], self.owner['id'])
        self.assertEqual(live.poll(self.project)['events'], first['events'])
        live.poll(self.project, seen=[envelope['event_id']])
        self.assertEqual(live.poll(self.project)['events'], [])
        self.assertEqual(self.store.read()['requests'][0]['state'], 'queued')
        live.poll(self.project, scheduled='abcd1234')
        self.assertEqual(sy.runtime.read_json(delivery.path)['scheduler_job_id'], 'abcd1234')

    def test_scheduled_poll_cli_records_readiness_and_seen_markers(self):
        self.delivery(provider='claude')
        self.request()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(sy.main(['live', 'poll', '-C', str(self.project)]), 0)
            envelope = json.loads(output.getvalue())['events'][0]
            self.assertEqual(sy.main(['live', 'seen', envelope['event_id'], '-C', str(self.project)]), 0)
            self.assertEqual(sy.main(['live', 'scheduled', 'abcd1234', '-C', str(self.project)]), 0)
        with self.assertRaises(sy.portfolio.PortfolioError):
            live.poll(self.project, seen=['unknown-event'])

    def test_configured_poll_cadence_does_not_queue_between_checks(self):
        delivery = self.delivery(transport='poll', interval=5)
        with patch.object(live.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            delivery.tick(1)
            self.request()
            delivery.tick(300)
            run.assert_not_called()
            delivery.tick(301)
            run.assert_called_once()
            delivery.tick(302)
            run.assert_called_once()

    def test_event_queue_orders_all_record_types_and_deduplicates_concurrent_publishers(self):
        request = self.request()
        decision = self.decision()
        question = live.event('decision', decision, 'open')
        request_event = live.event('request', request, 'queued')
        queue = live.EventQueue(self.directory)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: queue.register([question, request_event]), range(8)))
        self.assertTrue(all(result == results[0] for result in results))
        stored = queue.read()
        self.assertEqual([e['sequence'] for e in stored['events']], [1, 2])
        self.assertEqual(stored['next_sequence'], 3)
        self.assertEqual(stored['events'][0]['kind'], 'request')
        for envelope in stored['events']:
            self.assertTrue(all(k in envelope for k in ('schema_version', 'event_id', 'sequence', 'correlation_id',
                                                       'sender', 'recipient', 'occurred_at', 'enqueued_at', 'payload')))
        # A host-detected delivery failure is a different event from queuing,
        # even though the request store still has state='queued'.
        failed = queue.register([live.event('request', request, 'failed')])[0]
        self.assertNotEqual(failed['event_id'], stored['events'][0]['event_id'])
        self.assertEqual(failed['correlation_id'], stored['events'][0]['correlation_id'])
        self.assertEqual(failed['sender']['role'], 'syndicate')
        queue.path.write_text('{broken')
        with self.assertRaises(sy.portfolio.PortfolioError):
            queue.register([question])
        self.assertEqual(queue.path.read_text(), '{broken')

    def test_polling_batches_do_not_lose_unprocessed_events(self):
        self.delivery(provider='claude', chief=True)
        messages = live.MessageStore(self.directory)
        for index in range(25):
            messages.update(source_path=str(self.project), task_id='task', session_id=self.owner['id'],
                            key=str(index), message=f'Prompt {index}')
        workspace = self.directory / 'workspace'
        batch = live.poll(workspace)
        self.assertEqual(len(batch['events']), 20)
        self.assertTrue(batch['has_more'])
        live.poll(workspace, seen=[e['event_id'] for e in batch['events'][1:]])
        second = live.poll(workspace)
        self.assertEqual(len(second['events']), 6)
        self.assertEqual(second['events'][0]['event_id'], batch['events'][0]['event_id'])
        self.assertEqual([e['sequence'] for e in second['events']], [1, 21, 22, 23, 24, 25])

    def test_poll_does_not_process_a_closed_endpoint(self):
        delivery = self.delivery(provider='claude')
        self.request()
        delivery.close()
        with self.assertRaises(sy.portfolio.PortfolioError):
            live.poll(self.project)

    def test_interval_validation_never_silently_rounds_the_schedule(self):
        self.assertEqual(live.cron_expression(5), '*/5 * * * *')
        self.assertEqual(live.cron_expression(60), '0 * * * *')
        for value in (0, -5, 7, 90, True, 2.5, '5.0'):
            with self.assertRaises(sy.portfolio.PortfolioError):
                live.interval_minutes(value)

    def test_interactive_supervisor_delivers_through_native_queue(self):
        # Real PTY, supervisor, binding, and queue subprocess. No model, account,
        # network, or existing user session participates in this test.
        fake_bin = self.root / 'fake-bin'
        fake_bin.mkdir()
        queued = self.root / 'queued.json'
        fake_codex = fake_bin / 'codex'
        fake_codex.write_text('#!' + sys.executable + '\nimport json,sys\n'
                              f'open({str(queued)!r}, "w").write(json.dumps(sys.argv[1:]))\n')
        fake_codex.chmod(0o700)
        child = self.root / 'child.py'
        child.write_text(f'''
import sys,time
from pathlib import Path
sys.path.insert(0, {str(sy.SCRIPT.parent)!r})
import syndicate_live as live
root = Path({str(self.root)!r})
live.attach(root / 'repo', {self.thread!r})
deadline = time.monotonic() + 6
while not (root / 'queued.json').exists() and time.monotonic() < deadline:
    time.sleep(.05)
sys.exit(0 if (root / 'queued.json').exists() else 1)
''')
        runner = self.root / 'runner.py'
        runner.write_text(f'''
import os,sys
from pathlib import Path
sys.path.insert(0, {str(sy.SCRIPT.parent)!r})
import syndicate_runtime as rt
import syndicate_portfolio as portfolio
root = Path({str(self.root)!r})
project = root / 'repo'
directory = root / 'portfolio'
store = portfolio.CoordinationStore(directory)
# The test process's first owner is still live; use a separate source identity.
with store.edit() as data:
    data['sessions'] = []
owner = store.start_session(str(project), 'task', 'codex', os.getpid())
rt.atomic_json(project / '.syndicate/status.json', dict(current=dict(task_id='task', session_id=owner['id'])))
store.send_request(source_path=str(project), project_path=str(project), task_id='task', key='live', message='Read inbox')
rt.STATE = root / 'limits'
rt.save_observation('codex', {{'rateLimits': {{'primary': {{'usedPercent': 10}}}}}})
code, handoff = rt.supervise([sys.executable, str(root / 'child.py')], project, {{'codex'}}, 10,
                            'test', 'codex', live=dict(directory=directory))
rt.atomic_json(root / 'result.json', dict(code=code, handoff=str(handoff) if handoff else None))
''')
        master, slave = pty.openpty()
        try:
            env = dict(os.environ, PATH=str(fake_bin) + os.pathsep + os.environ.get('PATH', ''))
            proc = subprocess.Popen([sys.executable, str(runner)], stdin=slave, stdout=slave,
                                    stderr=slave, env=env, start_new_session=True)
            try:
                proc.wait(timeout=12)
                if proc.returncode:
                    os.set_blocking(master, False)
                    self.fail(os.read(master, 65536).decode(errors='replace'))
            finally:
                sy.runtime.stop_group(proc)
        finally:
            os.close(slave)
            os.close(master)
        self.assertEqual(sy.runtime.read_json(self.root / 'result.json'), dict(code=0, handoff=None))
        args = json.loads(queued.read_text())
        self.assertEqual(args[:3], ['queue', '--thread', self.thread])
        run = sy.runtime.read_json(self.project / '.syndicate/run.json')
        self.assertFalse(sy.runtime.read_json(run['live_endpoint'])['active'])
