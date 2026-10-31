"""Durable cross-project decisions, separate from agent-owned project status."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import uuid

import syndicate_runtime as runtime


class PortfolioError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


class DecisionStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / 'decisions.json'

    def read(self):
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError) as error:
            raise PortfolioError(f'Cannot read decision inbox: {error}') from error
        def valid(record):
            text_fields = ('id', 'state', 'source_path', 'project_path', 'task_id',
                           'key', 'question', 'created_at', 'updated_at')
            return (isinstance(record, dict)
                    and all(isinstance(record.get(k), str) and record[k].strip() for k in text_fields)
                    and record['state'] in ('open', 'answered', 'resolved', 'withdrawn')
                    and isinstance(record.get('options'), list)
                    and all(isinstance(option, str) for option in record['options'])
                    and all(isinstance(record.get(k, ''), str) for k in ('context', 'recommendation', 'answer'))
                    and isinstance(record.get('blocking'), bool)
                    and (record.get('deadline') is None or isinstance(record['deadline'], str))
                    and isinstance(record.get('history'), list)
                    and isinstance(record.get('answer_revision'), int)
                    and not isinstance(record['answer_revision'], bool) and record['answer_revision'] >= 0
                    and (record['state'] not in ('answered', 'resolved')
                         or (isinstance(record.get('answer'), str) and record['answer'].strip()
                             and isinstance(record.get('answered_at'), str) and record['answer_revision'] > 0)))

        if isinstance(data, dict) and isinstance(data.get('decisions'), list):
            for record in data['decisions']:
                if isinstance(record, dict) and 'answer_revision' not in record:
                    history = record.get('history')
                    answers = sum(isinstance(event, dict) and event.get('action') == 'answer'
                                  for event in history) if isinstance(history, list) else 0
                    record['answer_revision'] = max(answers, 1 if record.get('answer') else 0)

        if (not isinstance(data, dict) or data.get('version') != 1
                or not isinstance(data.get('decisions'), list)
                or any(not valid(d) for d in data['decisions'])):
            raise PortfolioError('Invalid decision inbox; restore or repair decisions.json before continuing')
        return data['decisions']

    @contextmanager
    def edit(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / 'decisions.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            records = self.read()
            yield records
            runtime.atomic_json(self.path, {'version': 1, 'decisions': records})

    def request(self, *, source_path, project_path, task_id, key, question,
                context='', options=None, recommendation='', blocking=False, deadline=None):
        if not all(isinstance(v, str) and v.strip() for v in (source_path, project_path, task_id, key, question)):
            raise PortfolioError('Source, project, task, key and question must be nonempty')
        with self.edit() as records:
            match = next((d for d in records if (d['source_path'], d['task_id'], d['key']) ==
                          (source_path, task_id, key)), None)
            if match:
                if match['question'] != question:
                    raise PortfolioError('This key already identifies a different question; use a new key')
                if match['state'] in ('resolved', 'withdrawn'):
                    return dict(match)
            else:
                match = dict(id=uuid.uuid4().hex, source_path=source_path, task_id=task_id,
                             key=key, question=question, state='open', answer_revision=0, created_at=now(), history=[])
                records.append(match)
            match.update(project_path=project_path, context=context, options=options or [],
                         recommendation=recommendation, blocking=blocking, deadline=deadline,
                         updated_at=now())
            return dict(match)

    def transition(self, decision_id, action, *, answer=None, source_path=None, task_id=None, answer_revision=None):
        with self.edit() as records:
            decision = next((d for d in records if d['id'] == decision_id), None)
            if decision is None:
                raise PortfolioError('Decision not found')
            state = decision['state']
            if action == 'answer':
                if state not in ('open', 'answered') or not answer or not answer.strip():
                    raise PortfolioError('Only open or answered decisions accept a nonempty answer')
                decision.update(state='answered', answer=answer, answered_at=now(),
                                answer_revision=decision['answer_revision'] + 1)
            elif action == 'acknowledge':
                if state != 'answered':
                    raise PortfolioError('Only answered decisions can be acknowledged')
                if (source_path, task_id) != (decision['source_path'], decision['task_id']):
                    raise PortfolioError('Acknowledgment must reference the owning source and task')
                if (not isinstance(answer_revision, int) or isinstance(answer_revision, bool)
                        or answer_revision != decision['answer_revision']):
                    raise PortfolioError('Answer revision changed or missing; reread and incorporate the current answer')
                decision.update(state='resolved', resolved_at=now())
            elif action == 'withdraw':
                if state not in ('open', 'answered'):
                    raise PortfolioError('Only pending decisions can be withdrawn')
                decision.update(state='withdrawn', withdrawn_at=now())
            else:
                raise PortfolioError('Unknown decision action')
            decision['updated_at'] = now()
            decision.setdefault('history', []).append(dict(action=action, at=decision['updated_at'],
                                                          **({'answer': answer, 'answer_revision': decision['answer_revision']} if action == 'answer' else
                                                             {'answer_revision': answer_revision} if action == 'acknowledge' else {})))
            return dict(decision)


def pending(records):
    return sorted((d for d in records if d['state'] in ('open', 'answered')),
                  key=lambda d: (d['state'] != 'open', not d.get('blocking'), d.get('created_at', '')))


def task_identity(task, project_path):
    return task.get('source_path', project_path), task.get('id')


def current_task(row, sessions=()):
    """Current ownership wins over attention ordering; old blockers remain in tasks."""
    tasks = row.get('tasks', [])
    for session in reversed(sessions):
        if session.get('live'):
            match = next((task for task in tasks if task_identity(task, row.get('path')) ==
                          (session['source_path'], session['task_id'])), None)
            if match:
                return match
    reported = [task for task in tasks if task.get('is_current')]
    reported_current = row.get('current') or {}
    current_id = reported_current.get('task_id') if isinstance(reported_current, dict) else None
    identified = [task for task in tasks if task.get('id') == current_id] if current_id else []
    running = [task for task in tasks if task.get('state') == 'running']
    candidates = reported or identified or running or tasks
    if candidates:
        return max(candidates, key=lambda task: task.get('updated_at') or '')
    return row.get('last_completed') or (row.get('current') if 'tasks' not in row else {}) or {}


def briefing(rows, decisions, coordination=None):
    coordination = coordination or dict(briefs={}, sessions=[], requests=[])
    inbox = pending(decisions)
    projects = []
    assignments = {task_identity(task, row['path']): row['path'] for row in rows
                   for task in row.get('tasks', []) + row.get('completed_tasks', [])
                   + ([row['last_completed']] if row.get('last_completed') else [])}
    for row in rows:
        current = current_task(row, coordination['sessions'])
        owners = [session for session in coordination['sessions']
                  if assignments.get((session['source_path'], session['task_id']), session['source_path']) == row['path']]
        current_owners = [session for session in owners if (session['source_path'], session['task_id'])
                          == task_identity(current, row['path'])]
        projects.append(dict(brief=coordination['briefs'].get(row['path']), sessions=owners,
                             name=row['name'], path=row['path'], status=row['project_status'],
                             task=current.get('summary'), progress=current.get('progress'),
                             task_id=current.get('id'), source_path=current.get('source_path', row['path']), provider=(current_owners[-1]['provider'] if current_owners else current.get('provider')) or 'unknown',
                             pid=current.get('pid'), updated_at=current.get('updated_at') or row.get('updated_at'),
                             report_state=current.get('state', 'unknown')))
    first = next((d for d in inbox if d['state'] == 'open'), None)
    if first:
        next_action = f"Answer decision {first['id']}: {first['question']}"
    elif inbox:
        next_action = 'Answered decisions are waiting for their owning teams to acknowledge them.'
    elif any(p['status'] == 'Needs attention' for p in projects):
        next_action = 'Inspect projects needing attention; no structured decision questions have been reported.'
    else:
        next_action = 'No pending decisions. Review project progress.'
    requests = [request for request in coordination['requests']
                if request['state'] in ('queued', 'failed')]
    if not first and any(r.get('delivery_state') == 'failed' for r in requests):
        next_action = 'Review requests whose target team could not acknowledge them.'
    elif not first and not inbox and requests:
        next_action = 'Team requests are waiting for their owning tasks to acknowledge them.'
    return dict(generated_at=now(), projects=projects, decisions=inbox, next_action=next_action,
                briefs=coordination['briefs'], sessions=coordination['sessions'], requests=requests)


def process_identity(pid):
    """Linux process start time distinguishes a current owner from a reused PID."""
    try:
        stat = Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()
        if stat[0] in ('Z', 'X'):
            return None
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return boot + ':' + stat[19]
    except (OSError, ValueError, IndexError, TypeError):
        if Path("/proc").is_dir():
            return None
        # POSIX platforms without /proc can still identify a process by start time.
        import subprocess
        try:
            result = subprocess.check_output(['ps', '-p', str(int(pid)), '-o', 'lstart=', '-o', 'stat='],
                                             text=True, stderr=subprocess.DEVNULL, timeout=2).split()
            return 'ps:' + ' '.join(result[:-1]) if len(result) >= 6 and not result[-1].startswith('Z') else None
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            return None


BRIEF_LIST_FIELDS = ('constraints', 'decisions', 'dependencies', 'next_steps')


class CoordinationStore:
    """Project context, session ownership and requests share one transactional file."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / 'coordination.json'

    def read(self):
        if not self.path.exists():
            return dict(version=1, briefs={}, sessions=[], requests=[])
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError) as error:
            raise PortfolioError(f'Cannot read coordination state: {error}') from error
        valid = (isinstance(data, dict) and data.get('version') == 1
                 and isinstance(data.get('briefs'), dict)
                 and isinstance(data.get('sessions'), list)
                 and isinstance(data.get('requests'), list))
        if valid:
            for path, brief in data['briefs'].items():
                valid = valid and (isinstance(brief, dict) and brief.get('project_path') == path
                    and isinstance(brief.get('revision'), int) and brief['revision'] > 0
                    and isinstance(brief.get('goal'), str) and isinstance(brief.get('updated_at'), str)
                    and brief.get('priority') in (None, 'low', 'normal', 'high', 'critical')
                    and all(isinstance(brief.get(field), list) and
                            all(isinstance(v, str) for v in brief[field]) for field in BRIEF_LIST_FIELDS))
            for session in data['sessions']:
                valid = valid and (isinstance(session, dict)
                    and all(isinstance(session.get(k), str) and session[k] for k in
                            ('id', 'source_path', 'task_id', 'provider', 'started_at'))
                    and isinstance(session.get('pid'), int) and not isinstance(session['pid'], bool) and session['pid'] > 0
                    and session.get('state') in ('active', 'ended'))
            for request in data['requests']:
                valid = valid and (isinstance(request, dict)
                    and all(isinstance(request.get(k), str) and request[k] for k in
                            ('id', 'source_path', 'project_path', 'task_id', 'key', 'message', 'updated_at'))
                    and request.get('state') in ('queued', 'acknowledged', 'failed', 'cancelled')
                    and (request.get('session_id') is None or isinstance(request['session_id'], str))
                    and isinstance(request.get('history'), list))
        if not valid:
            raise PortfolioError('Invalid coordination state; restore or repair coordination.json before continuing')
        return data

    @contextmanager
    def edit(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / 'coordination.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self.read()
            yield data
            runtime.atomic_json(self.path, data)

    def update_brief(self, project_path, changes, revision=None):
        if not changes or any(k not in ('goal', 'priority', *BRIEF_LIST_FIELDS) for k in changes):
            raise PortfolioError('Provide at least one project brief field')
        for key, value in changes.items():
            if key in BRIEF_LIST_FIELDS:
                valid = isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)
            elif key == 'priority':
                valid = value in (None, 'low', 'normal', 'high', 'critical')
            else:
                valid = isinstance(value, str)
            if not valid:
                raise PortfolioError(f'Invalid brief field: {key}')
        with self.edit() as data:
            previous = data['briefs'].get(project_path)
            current_revision = previous['revision'] if previous else 0
            if revision is not None and revision != current_revision:
                raise PortfolioError(f'Brief changed; read revision {current_revision} and retry')
            brief = previous or dict(project_path=project_path, goal='', priority=None,
                                     **{k: [] for k in BRIEF_LIST_FIELDS})
            brief.update(changes, revision=current_revision + 1, updated_at=now())
            data['briefs'][project_path] = brief
            return dict(brief)

    @staticmethod
    def session_live(session):
        if session['state'] != 'active':
            return False
        identity = process_identity(session['pid'])
        return (session['state'] == 'active' and identity is not None
                and identity == session.get('process_identity'))

    def start_session(self, source_path, task_id, provider, pid):
        with self.edit() as data:
            for previous in data['sessions']:
                if previous['source_path'] == source_path and previous['state'] == 'active':
                    if self.session_live(previous):
                        raise PortfolioError('An active session already owns this source')
                    previous.update(state='ended', ended_at=now(), reason='superseded')
            session = dict(id=uuid.uuid4().hex, source_path=source_path, task_id=task_id,
                           provider=provider, pid=pid, process_identity=process_identity(pid),
                           state='active', started_at=now())
            data['sessions'].append(session)
            return dict(session)

    def end_session(self, session_id, reason='session exited'):
        with self.edit() as data:
            session = next((s for s in data['sessions'] if s['id'] == session_id), None)
            if not session:
                raise PortfolioError('Session not found')
            session.update(state='ended', ended_at=now(), reason=reason)
            for request in data['requests']:
                if request['session_id'] == session_id and request['state'] == 'queued':
                    request.update(state='failed', reason='Target session ended before acknowledgment', updated_at=now())

    def send_request(self, *, source_path, project_path, task_id, key, message):
        if not all(isinstance(v, str) and v.strip() for v in (source_path, project_path, task_id, key, message)):
            raise PortfolioError('Request source, project, task, key and message must be nonempty')
        with self.edit() as data:
            previous = next((r for r in data['requests'] if
                             (r['source_path'], r['task_id'], r['key']) == (source_path, task_id, key)), None)
            if previous:
                if previous['message'] != message:
                    raise PortfolioError('This key identifies another request; use a new key')
                return dict(previous)
            session = next((s for s in reversed(data['sessions']) if s['source_path'] == source_path
                            and s['task_id'] == task_id and self.session_live(s)), None)
            request = dict(id=uuid.uuid4().hex, source_path=source_path, project_path=project_path,
                           task_id=task_id, key=key, message=message, session_id=session['id'] if session else None,
                           state='queued', created_at=now(), updated_at=now(), history=[])
            data['requests'].append(request)
            return dict(request)

    def transition_request(self, request_id, action, *, source_path=None, task_id=None,
                           session_id=None, note=''):
        with self.edit() as data:
            request = next((r for r in data['requests'] if r['id'] == request_id), None)
            if not request or request['state'] not in (('queued', 'failed') if action == 'cancel' else ('queued',)):
                raise PortfolioError('Only queued requests can be acknowledged, failed or cancelled')
            if action == 'cancel':
                request['state'] = 'cancelled'
            elif action in ('acknowledge', 'fail'):
                session = next((s for s in data['sessions'] if s['id'] == session_id), None)
                if (not session or not self.session_live(session)
                        or (source_path, task_id) != (request['source_path'], request['task_id'])
                        or (session['source_path'], session['task_id']) != (source_path, task_id)
                        or request['session_id'] not in (None, session_id)):
                    raise PortfolioError('Request must be handled by its owning active task/session')
                if not note.strip():
                    raise PortfolioError('Provide a note describing incorporation or failure')
                request.update(state='acknowledged' if action == 'acknowledge' else 'failed',
                               session_id=session_id, note=note)
            else:
                raise PortfolioError('Unknown request action')
            request['updated_at'] = now()
            request['history'].append(dict(action=action, at=request['updated_at'], note=note))
            return dict(request)

    def retry_request(self, request_id):
        with self.edit() as data:
            request = next((r for r in data['requests'] if r['id'] == request_id), None)
            if not request or request['state'] not in ('queued', 'failed'):
                raise PortfolioError('Only pending or failed requests can be retried')
            old_session = next((s for s in data['sessions'] if s['id'] == request['session_id']), None)
            if request['state'] == 'queued' and old_session and self.session_live(old_session):
                raise PortfolioError('Request is still awaiting acknowledgment from an active session')
            session = next((s for s in reversed(data['sessions']) if
                            (s['source_path'], s['task_id']) == (request['source_path'], request['task_id'])
                            and self.session_live(s)), None)
            request.update(state='queued', session_id=session['id'] if session else None, updated_at=now())
            request.pop('reason', None)
            request.pop('note', None)
            request['history'].append(dict(action='retry', at=request['updated_at']))
            return dict(request)

    def snapshot(self):
        data = self.read()
        for session in data['sessions']:
            session['live'] = self.session_live(session)
        sessions = {s['id']: s for s in data['sessions']}
        for request in data['requests']:
            if request['state'] != 'queued':
                request['delivery_state'] = request['state']
                continue
            target = sessions.get(request['session_id'])
            if request['session_id'] and (not target or not target['live']):
                request.update(delivery_state='failed', reason='Target session is no longer active')
            elif target:
                request['delivery_state'] = 'awaiting_acknowledgment'
            else:
                available = any(s['live'] and s['source_path'] == request['source_path']
                                and s['task_id'] == request['task_id'] for s in sessions.values())
                request['delivery_state'] = 'awaiting_acknowledgment' if available else 'waiting_for_session'
        return data
