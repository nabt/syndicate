"""Durable cross-project decisions, separate from agent-owned project status."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
from stat import S_ISREG
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
    closed = [task for task in row.get('completed_tasks', []) + row.get('ended_tasks', [])
              if task.get('closeout_session') and task['closeout_session'] == task.get('session_id')]
    for session in reversed(sessions):
        if session.get('live'):
            match = next((task for task in tasks + closed if task_identity(task, row.get('path')) ==
                          (session['source_path'], session['task_id'])), None)
            if match:
                return match
    reported = [task for task in tasks + closed if task.get('is_current')]
    reported_current = row.get('current') or {}
    current_id = reported_current.get('task_id') if isinstance(reported_current, dict) else None
    identified = [task for task in tasks if task.get('id') == current_id] if current_id else []
    running = [task for task in tasks if task.get('state') == 'running']
    candidates = reported or identified or running or tasks
    if candidates:
        return max(candidates, key=lambda task: task.get('updated_at') or '')
    return row.get('last_completed') or (row.get('current') if 'tasks' not in row else {}) or {}


def chief_status(directory):
    """Read portfolio-only Chief records; the inherited kernel lock proves ownership.

    Do not probe PIDs: dashboards may run in a different PID namespace and the
    CLI can retain the lock after its supervisor exits. Never create state here.
    """
    directory = Path(directory)
    local = directory / 'workspace' / '.syndicate'
    result = dict(state='unknown', owner_active=None, provider='unknown',
                  report_state='unknown', summary=None, session_id=None,
                  status_updated_at=None, reason=None,
                  checkpoint=dict(state='unavailable', summary=None, updated_at=None, truncated=False))
    try:
        fd, _ = open_lock(directory / 'chief-of-staff.lock')
        with os.fdopen(fd, 'rb') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                result['owner_active'] = True
            else:
                result['owner_active'] = False
                fcntl.flock(lock, fcntl.LOCK_UN)
            try:
                owner_pid = lock.read(64).decode('ascii').strip()
            except UnicodeError:
                owner_pid = ''
                result['reason'] = 'Chief ownership contents are invalid; activity cannot be matched to its status.'
    except FileNotFoundError:
        owner_pid = ''
        result['reason'] = 'No Chief ownership record is available.'
    except OSError:
        owner_pid = ''
        result.update(state='unavailable', reason='Chief ownership record cannot be read.')
    try:
        with (local / 'status.json').open() as stream:
            status = json.load(stream)
            stamp = datetime.fromtimestamp(os.fstat(stream.fileno()).st_mtime, timezone.utc).isoformat()
        current = status.get('current') if isinstance(status, dict) else None
        if not isinstance(current, dict) or current.get('role') != 'chief-of-staff':
            raise ValueError('Invalid Chief status')
        result.update(report_state=status.get('state') if isinstance(status.get('state'), str) else 'unknown',
                      provider=status.get('provider') if status.get('provider') in ('claude', 'codex') else 'unknown',
                      session_id=status.get('session_id') if isinstance(status.get('session_id'), str) else None,
                      summary=current.get('summary') if isinstance(current.get('summary'), str) else None,
                      status_updated_at=stamp)
        matches = (type(status.get('pid')) is int and owner_pid == str(status['pid']))
        if result['owner_active'] is True:
            if not matches:
                result['reason'] = result['reason'] or 'Chief owns the lock, but its status record does not match the owner.'
            elif result['report_state'] == 'running':
                result['state'] = 'running'
            elif result['report_state'] in ('waiting_on_user', 'waiting', 'blocked'):
                result['state'] = 'waiting'
            else:
                result['reason'] = 'Chief owns the lock; current activity is unknown.'
    except (OSError, ValueError, TypeError):
        if result['owner_active'] is True:
            result['reason'] = 'Chief owns the lock; its status record is unavailable or invalid.'
        elif not result['reason']:
            result['reason'] = 'Chief status record is unavailable or invalid.'
    if result['owner_active'] is False:
        result.update(state='stopped', reason='No active Chief lock owner; status and checkpoint are saved records.')
    try:
        with (local / 'checkpoint.md').open() as stream:
            text = stream.read(2001)
            stamp = datetime.fromtimestamp(os.fstat(stream.fileno()).st_mtime, timezone.utc).isoformat()
        result['checkpoint'] = dict(state='available' if text.strip() else 'empty', summary=text[:2000].strip() or None,
                                    updated_at=stamp, truncated=len(text) > 2000)
    except (OSError, UnicodeError):
        pass
    return result


def briefing(rows, decisions, coordination=None, chief=None):
    coordination = coordination or dict(briefs={}, sessions=[], requests=[])
    inbox = pending(decisions)
    projects = []
    assignments = {task_identity(task, row['path']): row['path'] for row in rows
                   for task in row.get('tasks', []) + row.get('completed_tasks', []) + row.get('ended_tasks', [])
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
                             report_state=current.get('state', 'unknown'),
                             liveness=current.get('liveness')))
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
    return dict(generated_at=now(), chief=chief, projects=projects, decisions=inbox, next_action=next_action,
                briefs=coordination['briefs'], sessions=coordination['sessions'], requests=requests)


def process_namespace():
    try:
        return str(Path('/proc/self/ns/pid').readlink())
    except OSError:
        return None


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


def boot_id():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip() or None
    except OSError:
        return None


def machine_id():
    """Host identity, so a boot_id change can be told apart from another machine."""
    for path in ('/etc/machine-id', '/var/lib/dbus/machine-id'):
        try:
            value = Path(path).read_text().strip()
        except OSError:
            continue
        if value:
            return value
    return None


def owner_record(pid):
    """Launcher-recorded owner facts: PID, reuse-proof start identity, and the
    PID namespace and machine it was read on."""
    valid = isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
    return dict(pid=pid, process_identity=process_identity(pid) if valid else None,
                process_namespace=process_namespace(), machine_id=machine_id())


def process_gone(pid):
    """True only when this /proc view definitively shows no running process.

    ENOENT under /proc is confirmed with kill(pid, 0), which is answered in
    the caller's PID namespace and is not affected by hidepid; a permission
    error means the process exists.
    """
    try:
        Path('/proc/self/stat').read_text()
    except OSError:
        return False  # /proc unavailable: absence proves nothing
    try:
        pid = int(pid)
        stat = Path(f'/proc/{pid}/stat').read_text()
    except (FileNotFoundError, ProcessLookupError):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False  # PermissionError: exists but hidden or foreign-owned
        return False
    except (OSError, ValueError, TypeError):
        return False
    try:
        return stat.rsplit(')', 1)[1].split()[0] in ('Z', 'X')
    except IndexError:
        return False


def owner_liveness(pid, identity, namespace, machine=None):
    """Tri-state owner check: 'alive', 'dead', or 'unknown'.

    'dead' needs host-visible proof: a different boot of the same machine,
    or a same-namespace /proc view in which the recorded start identity no
    longer matches. A foreign PID namespace, unreadable /proc, missing
    identity, or a ps view (which a sandbox can filter) is never 'dead' --
    sandbox-hidden processes are never treated as exited.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return 'unknown'
    if not isinstance(identity, str) or not identity:
        return 'unknown'
    current_namespace = process_namespace()
    same_namespace = bool(namespace) and namespace == current_namespace
    if identity.startswith('ps:'):
        # No /proc (e.g. macOS): ps start time can confirm a live owner, but a
        # missing or different entry is not proof of exit.
        if not namespace and not current_namespace and process_identity(pid) == identity:
            return 'alive'
        return 'unknown'
    boot = boot_id()
    if boot and ':' in identity and not identity.startswith(boot + ':'):
        here = machine_id()
        if isinstance(machine, str) and machine and here:
            return 'dead' if machine == here else 'unknown'  # reboot vs another host
        return 'dead' if same_namespace else 'unknown'
    if not same_namespace:
        return 'unknown'
    current = process_identity(pid)
    if current is not None:
        return 'alive' if current == identity else 'dead'  # mismatch: reused PID
    return 'dead' if process_gone(pid) else 'unknown'


def record_liveness(record):
    """owner_liveness for a dict carrying owner_record fields."""
    if not isinstance(record, dict):
        return 'unknown'
    return owner_liveness(record.get('pid'), record.get('process_identity'),
                          record.get('process_namespace'), record.get('machine_id'))


class UnsafeLockError(OSError):
    pass


_O = lambda name: getattr(os, name, 0)  # noqa: E731 - flags absent on some platforms


def open_lock(path, create=False, writable=False):
    """Open an flock file without following links or blocking. Returns (fd, stat).

    The parent directory is opened O_DIRECTORY|O_NOFOLLOW and the file through
    dir_fd, non-blocking (a FIFO opens immediately), so a planted
    symlink or FIFO can neither redirect nor hang the caller. The file must be
    a regular file owned by the current user with a single link; anything else
    raises UnsafeLockError. flock works on the default read-only descriptor,
    close-on-exec by default. Chief uses writable=True for its PID record and
    explicitly passes the validated descriptor to its CLI child. No open
    truncates the file; callers must acquire ownership before changing it.
    """
    path = Path(path)
    directory = os.open(path.parent, os.O_RDONLY | _O('O_DIRECTORY') | _O('O_NOFOLLOW') | _O('O_CLOEXEC'))
    try:
        flags = ((os.O_RDWR if writable else os.O_RDONLY) | _O('O_NOFOLLOW') |
                 _O('O_CLOEXEC') | _O('O_NONBLOCK') | (os.O_CREAT if create else 0))
        fd = os.open(path.name, flags, 0o600, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        info = os.fstat(fd)
        if not S_ISREG(info.st_mode):
            raise UnsafeLockError(f'{path} is not a regular file')
        if info.st_uid != os.getuid():
            raise UnsafeLockError(f'{path} is not owned by the current user')
        if info.st_nlink != 1:
            raise UnsafeLockError(f'{path} has {info.st_nlink} hard links')
    except BaseException:
        os.close(fd)
        raise
    return fd, info


def probe_lock_detail(path):
    """('held' | 'free' | 'unknown', (st_dev, st_ino) or None).

    A shared, non-blocking probe never creates the file and holds it only
    momentarily; launchers retry briefly so a probe cannot fail them. Any
    unsafe or unreadable lock file is 'unknown'.
    """
    try:
        fd, info = open_lock(path)
    except OSError:
        return 'unknown', None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return 'held', (info.st_dev, info.st_ino)
        except OSError:
            return 'unknown', None
        fcntl.flock(fd, fcntl.LOCK_UN)
        return 'free', (info.st_dev, info.st_ino)
    finally:
        os.close(fd)


def probe_lock(path, expect=None):
    """'held', 'free', or 'unknown'. With expect=(st_dev, st_ino), 'free' is
    reported only for that inode: a replaced lock file proves nothing."""
    state, inode = probe_lock_detail(path)
    if state == 'free' and expect is not None:
        try:
            expected = tuple(int(v) for v in expect)
        except (TypeError, ValueError):
            return 'unknown'
        if len(expected) != 2 or inode != expected:
            return 'unknown'
    return state


CHIEF_ROLE = 'chief-of-staff'


def _within(path, root):
    """True if path is root or below it, lexically normalized and resolved.

    `..` cannot escape (paths are normalized before comparison) and symlinks
    are resolved; any error fails closed (treated as within)."""
    try:
        raw = os.path.abspath(os.path.expanduser(os.fspath(path)))
        root = os.fspath(root)
        candidates = {os.path.normpath(raw), os.path.realpath(raw)}
        roots = {os.path.normpath(os.path.abspath(root)), os.path.realpath(root)}
        return any(left == right or left.startswith(right.rstrip(os.sep) + os.sep)
                   for left in candidates for right in roots)
    except (OSError, ValueError, TypeError, RuntimeError):
        return True  # cannot rule out the Chief workspace: treat as protected


def is_chief_record(directory, *, path=None, record=None):
    """The single Chief of Staff guard for stale detection and cleanup.

    Decided only from launcher-controlled facts, never from agent-writable
    status content (a 'chief' flag, 'role', or current.role is ignored):
    - the given path, or a record's launcher-written source_path, resolved
      under <directory>/workspace (the real Chief workspace is always
      excluded, whatever its status file says).
    Path errors fail closed. Such records are never judged stale, ended, or
    modified by sweeps. Chief live endpoints need no check here: sweeps never
    read or touch live endpoints at all.
    """
    workspace = Path(directory) / 'workspace'
    paths = [path]
    if isinstance(record, dict):
        paths.append(record.get('source_path'))
    return any(isinstance(p, (str, Path)) and str(p) and _within(p, workspace) for p in paths)


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
    def edit(self, *, read_only=False):
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / 'coordination.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self.read()
            yield data
            if not read_only:
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
        namespace = session.get('process_namespace')
        if namespace and namespace != process_namespace():
            # A reboot of this machine is an actual dead owner, not a sandbox
            # visibility error; from another namespace that is the only proof.
            if record_liveness(session) == 'dead':
                return False
            raise PortfolioError('Cannot validate session ownership from this PID namespace; '
                                 'rerun this command outside the sandbox with approval')
        identity = process_identity(session['pid'])
        return (session['state'] == 'active' and identity is not None
                and identity == session.get('process_identity'))

    def start_session(self, source_path, task_id, provider, pid):
        with self.edit() as data:
            for previous in data['sessions']:
                if previous['source_path'] == source_path and previous['state'] == 'active':
                    if self.session_live(previous):
                        raise PortfolioError('An active session already owns this source')
                    self._end(data, previous, 'superseded')
            session = dict(id=uuid.uuid4().hex, source_path=source_path, task_id=task_id,
                           provider=provider, **owner_record(pid),
                           state='active', started_at=now())
            data['sessions'].append(session)
            return dict(session)

    @staticmethod
    def _end(data, session, reason):
        session.update(state='ended', ended_at=now(), reason=reason)
        for request in data['requests']:
            if request['session_id'] == session['id'] and request['state'] == 'queued':
                request.update(state='failed', reason='Target session ended before acknowledgment', updated_at=now())

    def end_session(self, session_id, reason='session exited'):
        with self.edit() as data:
            session = next((s for s in data['sessions'] if s['id'] == session_id), None)
            if not session:
                raise PortfolioError('Session not found')
            self._end(data, session, reason)

    @staticmethod
    def session_liveness(session):
        """Never raises: 'dead' for ended records, otherwise owner_liveness."""
        if session.get('state') != 'active':
            return 'dead'
        return record_liveness(session)

    def record_cli_owner(self, session_id, owner):
        """Record the supervised CLI child as an additional owner of an active
        session, so a supervisor crash is not mistaken for the session's end."""
        with self.edit() as data:
            session = next((s for s in data['sessions'] if s['id'] == session_id), None)
            if session and session['state'] == 'active':
                session['cli_owner'] = dict(owner)

    def reconcile(self, verdict, reason='owner process exited'):
        """End active sessions that verdict(session) proves dead, like end_session.

        verdict is re-evaluated under the store lock and must return True only
        on definitive evidence. Chief records are excluded by the caller's
        verdict and again here. Returns ended session IDs; writes nothing when
        no session qualifies, so repeated sweeps are idempotent.
        """
        candidates = [s['id'] for s in self.read()['sessions']
                      if s['state'] == 'active' and verdict(s)]
        if not candidates:
            return []
        ended = []
        with self.edit() as data:
            for session in data['sessions']:
                if (session['id'] in candidates and session['state'] == 'active'
                        and not is_chief_record(self.directory, record=session) and verdict(session)):
                    self._end(data, session, reason)
                    ended.append(session['id'])
        return ended

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

    def snapshot(self, strict=True):
        """strict keeps the explicit foreign-namespace error for ownership commands;
        read-only views pass strict=False and get liveness 'unknown' instead."""
        data = self.read()
        for session in data['sessions']:
            session['liveness'] = self.session_liveness(session)
            session['live'] = self.session_live(session) if strict else session['liveness'] == 'alive'
        sessions = {s['id']: s for s in data['sessions']}
        for request in data['requests']:
            if request['state'] != 'queued':
                request['delivery_state'] = request['state']
                continue
            target = sessions.get(request['session_id'])
            if target and not target['live'] and target['liveness'] == 'unknown':
                request.update(delivery_state='unverified',
                               reason='Target session ownership cannot be verified from this environment')
            elif request['session_id'] and (not target or not target['live']):
                request.update(delivery_state='failed', reason='Target session is no longer active')
            elif target:
                request['delivery_state'] = 'awaiting_acknowledgment'
            else:
                available = any(s['live'] and s['source_path'] == request['source_path']
                                and s['task_id'] == request['task_id'] for s in sessions.values())
                request['delivery_state'] = 'awaiting_acknowledgment' if available else 'waiting_for_session'
        return data
