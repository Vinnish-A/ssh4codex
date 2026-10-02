"""Standalone remote helper: Python standard library, tmux, no daemon."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time
from contextlib import contextmanager

ROOT = Path.home() / '.local/share/ssh4codex'
TERMINAL = {'succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted'}


def job_dir(task_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', task_id):
        raise ValueError('task_id must contain 1–64 letters, digits, underscores or hyphens')
    return ROOT / 'tasks' / task_id


def atomic_json(path, value):
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False))
    tmp.replace(path)


@contextmanager
def locked(folder):
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / 'lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def read_state(folder):
    return json.loads((folder / 'state.json').read_text())


def write_state(folder, **updates):
    state = read_state(folder)
    state.update(updates)
    atomic_json(folder / 'state.json', state)
    return state


def tmux(*args):
    return subprocess.run(['tmux', *args], capture_output=True, text=True, check=True)


def submit(request):
    folder = job_dir(request['task_id'])
    spec = {k: request.get(k) for k in ['script', 'cwd', 'env', 'interpreter', 'artifacts', 'timeout', 'session']}
    fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    with locked(folder):
        if (folder / 'state.json').exists():
            state = read_state(folder)
            if state['request_sha256'] != fingerprint:
                raise ValueError('task_id already belongs to a different request')
            return {**state, 'reused': True}
        cwd = Path(spec['cwd']).expanduser().resolve()
        if not cwd.is_dir():
            raise ValueError('Remote cwd does not exist: ' + str(cwd))
        if not isinstance(spec['script'], str) or not spec['script']:
            raise ValueError('A nonempty script is required')
        if spec['timeout'] is not None and spec['timeout'] <= 0:
            raise ValueError('timeout must be positive')
        if not isinstance(spec['interpreter'], list) or not spec['interpreter']:
            raise ValueError('interpreter must be a nonempty argv list')
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in spec['env'].items()):
            raise ValueError('Environment keys and values must be strings')
        # Check the requested session, without changing its user's windows.
        session = spec['session']
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', session):
            raise ValueError('Invalid tmux session name')
        if subprocess.run(['tmux', 'has-session', '-t', '=' + session], capture_output=True).returncode:
            tmux('new-session', '-d', '-s', session)
        (folder / 'script').write_text(spec['script'])
        atomic_json(folder / 'request.json', spec)
        state = {'task_id': request['task_id'], 'state': 'queued', 'exit_code': None,
                 'request_sha256': fingerprint, 'submitted_at': time.time(),
                 'cwd': str(cwd), 'session': session}
        atomic_json(folder / 'state.json', state)
        command = shlex.join([sys.executable, str(Path(__file__).resolve()), 'worker', request['task_id']])
        try:
            pane = tmux('new-window', '-d', '-P', '-F', '#{pane_id}', '-t', '=' + session + ':',
                        '-n', 's4c-' + request['task_id'][:16], '-c', str(cwd), command).stdout.strip()
        except subprocess.CalledProcessError as exc:
            write_state(folder, state='failed', error=exc.stderr.strip(), finished_at=time.time())
            raise
        return {**write_state(folder, pane=pane), 'reused': False}


def artifact_manifest(spec, cwd):
    result = []
    for name in spec['artifacts']:
        p = Path(name).expanduser()
        if not p.is_absolute():
            p = cwd / p
        p = p.resolve()
        record = {'path': str(p), 'exists': p.is_file()}
        if record['exists']:
            digest = hashlib.sha256()
            with p.open('rb') as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(chunk)
            record.update(size=p.stat().st_size, sha256=digest.hexdigest())
        result.append(record)
    return result


def worker(task_id):
    folder = job_dir(task_id)
    with locked(folder):
        state = read_state(folder)
        if state['state'] != 'queued':
            return
        if (folder / 'cancel').exists():
            write_state(folder, state='cancelled', finished_at=time.time())
            return
        spec = json.loads((folder / 'request.json').read_text())
        write_state(folder, state='running', started_at=time.time(), supervisor_pid=os.getpid())
    stopped = []
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: stopped.append(True))
    child = None
    try:
        cwd = Path(state['cwd'])
        env = dict(os.environ, **spec['env'])
        with (folder / 'stdout.log').open('wb') as stdout, (folder / 'stderr.log').open('wb') as stderr:
            child = subprocess.Popen([*spec['interpreter'], str(folder / 'script')], cwd=cwd,
                                     env=env, stdout=stdout, stderr=stderr, start_new_session=True)
            with locked(folder):
                write_state(folder, child_pid=child.pid)
            started = time.monotonic()
            final = None
            while child.poll() is None:
                if stopped or (folder / 'cancel').exists():
                    final = 'cancelled'
                elif spec['timeout'] and time.monotonic() - started >= spec['timeout']:
                    final = 'timed_out'
                if final:
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    # Wait on the process group, not just its leader: descendants may ignore TERM.
                    end = time.monotonic() + 1
                    while time.monotonic() < end:
                        try:
                            os.killpg(child.pid, 0)
                        except ProcessLookupError:
                            break
                        time.sleep(.05)
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    break
                time.sleep(.05)
            code = child.wait()
        final = final or ('succeeded' if code == 0 else 'failed')
        artifacts = artifact_manifest(spec, cwd)
        with locked(folder):
            write_state(folder, state=final, exit_code=code, finished_at=time.time(), artifacts=artifacts)
    except Exception as exc:
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            except ProcessLookupError:
                pass
        with locked(folder):
            write_state(folder, state='failed', error=str(exc), finished_at=time.time())


def bounded_log(path, cursor, limit, tail):
    if cursor < 0 or limit < 4 or limit > 1024 * 1024:
        raise ValueError('cursor must be nonnegative; log limit must be 4..1048576 bytes')
    if not path.exists():
        return {'text': '', 'cursor': cursor, 'remaining': 0, 'skipped': 0}
    with path.open('rb') as handle:
        size = os.fstat(handle.fileno()).st_size
        if cursor > size:
            raise ValueError('cursor exceeds log size')
        start = max(cursor, size - limit) if tail else cursor
        handle.seek(start)
        content = handle.read(limit)
        # Do not split a UTF-8 character across incremental reads.
        if start + len(content) < size:
            for trim in range(4):
                candidate = content if not trim else content[:-trim]
                try:
                    candidate.decode('utf-8')
                    content = candidate
                    break
                except UnicodeDecodeError as exc:
                    if exc.reason != 'unexpected end of data':
                        break
        return {'text': content.decode('utf-8', errors='replace'), 'cursor': start + len(content),
                'remaining': max(0, size - start - len(content)), 'skipped': start - cursor}


def status(request):
    folder = job_dir(request['task_id'])
    state = read_state(folder)
    # The wrapper publishes an exit status, rather than relying on terminal prompt guesses.
    if state['state'] in {'queued', 'running'} and state.get('pane'):
        panes = subprocess.run(['tmux', 'list-panes', '-a', '-F', '#{pane_id} #{pane_dead}'],
                               text=True, capture_output=True)
        alive = {line.split()[0] for line in panes.stdout.splitlines() if line.endswith(' 0')}
        if state['pane'] not in alive:
            with locked(folder):
                state = read_state(folder)
                if state['state'] in {'queued', 'running'}:
                    state = write_state(folder, state='interrupted', finished_at=time.time(),
                                        error='Task pane disappeared without a recorded completion; not replayed')
    if request.get('logs', True):
        limit = request.get('limit', 2048)
        state['stdout'] = bounded_log(folder / 'stdout.log', request.get('stdout_cursor', 0), limit, request.get('tail', False))
        state['stderr'] = bounded_log(folder / 'stderr.log', request.get('stderr_cursor', 0), limit, request.get('tail', False))
    return state


def wait_status(request):
    seconds = request.get('wait_seconds', 0)
    if not 0 <= seconds <= 60:
        raise ValueError('wait_seconds must be 0..60')
    end = time.monotonic() + seconds
    folder = job_dir(request['task_id'])
    while read_state(folder)['state'] not in TERMINAL and time.monotonic() < end:
        time.sleep(min(.05, max(0, end - time.monotonic())))
    return status({**request, 'tail': True})


def dispatch(request):
    action = request['action']
    if action == 'submit':
        state = submit(request)
        if request.get('wait_seconds', 0):
            state = {**wait_status(request), 'reused': state['reused']}
        return state
    if action == 'wait':
        return wait_status(request)
    if action == 'status':
        return status(request)
    if action == 'cancel':
        folder = job_dir(request['task_id'])
        with locked(folder):
            state = read_state(folder)
            if state['state'] not in TERMINAL:
                (folder / 'cancel').touch()
        return {'task_id': request['task_id'], 'state': state['state'], 'cancel_requested': state['state'] not in TERMINAL}
    if action == 'list':
        records = []
        for p in sorted((ROOT / 'tasks').glob('*/state.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:request.get('limit', 20)]:
            s = json.loads(p.read_text())
            records.append({k: s.get(k) for k in ['task_id', 'state', 'exit_code', 'submitted_at', 'session']})
        return {'tasks': records}
    if action == 'doctor':
        tm = subprocess.run(['tmux', '-V'], capture_output=True, text=True)
        return {'python': sys.version.split()[0], 'tmux': tm.stdout.strip(), 'tmux_exit_code': tm.returncode, 'root': str(ROOT)}
    raise ValueError('Unknown action: ' + action)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['rpc', 'worker'])
    parser.add_argument('task_id', nargs='?')
    args = parser.parse_args()
    if args.action == 'worker':
        worker(args.task_id)
    else:
        try:
            print(json.dumps(dispatch(json.load(sys.stdin)), ensure_ascii=False))
        except Exception as exc:
            print(json.dumps({'error': type(exc).__name__, 'message': str(exc)}, ensure_ascii=False))
            sys.exit(1)
