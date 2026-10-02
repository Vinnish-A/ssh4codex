"""Standalone remote helper: Python standard library, tmux, no daemon."""
import argparse
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import signal
import shutil
import subprocess
import sys
import tarfile
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
    tmp.chmod(0o600)
    tmp.replace(path)


@contextmanager
def locked(folder):
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / 'lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


class RemoteError(ValueError):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def read_state(folder):
    try:
        return json.loads((folder / 'state.json').read_text())
    except FileNotFoundError as exc:
        raise RemoteError('task_not_found', 'Task has no recorded state: ' + folder.name) from exc


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
    for key in ('inputs', 'requires'):
        if request.get(key): spec[key] = request[key]
    fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    with locked(folder):
        if (folder / 'state.json').exists():
            state = read_state(folder)
            if state['request_sha256'] != fingerprint:
                raise RemoteError('request_conflict', 'task_id already belongs to a different request')
            return {**state, 'reused': True}
        cwd = Path(spec['cwd']).expanduser().resolve()
        if not cwd.is_dir():
            raise RemoteError('preflight_failed', 'Remote cwd does not exist: ' + str(cwd))
        if not isinstance(spec['script'], str) or not spec['script']:
            raise ValueError('A nonempty script is required')
        if spec['timeout'] is not None and spec['timeout'] <= 0:
            raise ValueError('timeout must be positive')
        if not isinstance(spec['interpreter'], list) or not spec['interpreter']:
            raise ValueError('interpreter must be a nonempty argv list')
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in spec['env'].items()):
            raise ValueError('Environment keys and values must be strings')
        check_environment(spec, cwd)
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


def status(request, alive=None):
    folder = job_dir(request['task_id'])
    state = read_state(folder)
    # The wrapper publishes an exit status, rather than relying on terminal prompt guesses.
    if state['state'] in {'queued', 'running'} and state.get('pane'):
        if alive is None:
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


class DownloadError(ValueError):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def download(task_id):
    """One stream: completion manifest, then numbered regular-file payloads."""
    state = status({'task_id': task_id, 'logs': False})
    if state['state'] != 'succeeded':
        raise DownloadError('artifact_not_ready', 'Fetch requires a succeeded task; current state: ' + state['state'])
    records = state.get('artifacts', [])
    if any(not item['exists'] for item in records):
        raise DownloadError('artifact_missing', 'Expected artifact missing')
    names = [Path(item['path']).name for item in records]
    if len(set(names)) != len(names):
        raise DownloadError('artifact_collision', 'Artifacts have duplicate basenames')
    manifest = json.dumps({'task_id': task_id, 'files': records}).encode()
    with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as stream:
        header = tarfile.TarInfo('manifest.json')
        header.size = len(manifest)
        stream.addfile(header, io.BytesIO(manifest))
        for i, item in enumerate(records):
            try:
                with open(item['path'], 'rb') as handle:
                    if os.fstat(handle.fileno()).st_size != item['size']:
                        raise DownloadError('artifact_changed', 'Artifact size changed since completion')
                    header = tarfile.TarInfo(str(i))
                    header.size = item['size']
                    stream.addfile(header, handle)
            except FileNotFoundError as exc:
                raise DownloadError('artifact_changed', 'Artifact removed since completion') from exc


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
    if action == 'status_many':
        ids = request['task_ids']
        if not isinstance(ids, list) or not 1 <= len(ids) <= 64:
            raise ValueError('status_many requires 1..64 task IDs')
        panes = subprocess.run(['tmux', 'list-panes', '-a', '-F', '#{pane_id} #{pane_dead}'],
                               text=True, capture_output=True)
        alive = {line.split()[0] for line in panes.stdout.splitlines() if line.endswith(' 0')}
        result = []
        for task_id in ids:
            try:
                result.append(status({**request, 'task_id': task_id, 'logs': request.get('logs', False), 'tail': True}, alive))
            except (ValueError, FileNotFoundError) as exc:
                result.append({'task_id': task_id, 'error': getattr(exc, 'kind', 'remote'), 'message': str(exc)})
        return {'tasks': result}
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
    if action == 'upload_prepare': return upload_prepare(request)
    if action == 'upload_status': return upload_status(request['transfer_id'])
    if action == 'doctor':
        tm = subprocess.run(['tmux', '-V'], capture_output=True, text=True)
        required = request.get('requires', [])
        resolved = resolve_executables(required, request.get('cwd', '~'), request.get('env', {}))
        return {'python': sys.version.split()[0], 'tmux': tm.stdout.strip(), 'tmux_exit_code': tm.returncode, 'root': str(ROOT), 'executables': resolved, 'ready': tm.returncode == 0 and all(resolved.values())}
    raise ValueError('Unknown action: ' + action)



def file_digest(path, limit=None):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        remaining = limit
        while remaining is None or remaining > 0:
            chunk = handle.read(1024 * 1024 if remaining is None else min(1024 * 1024, remaining))
            if not chunk: break
            digest.update(chunk)
            if remaining is not None: remaining -= len(chunk)
    return digest.hexdigest()


def resolve_executables(names, cwd, env):
    directory = Path(cwd).expanduser().resolve()
    search = dict(os.environ, **env).get('PATH', os.defpath)
    result = {}
    for name in names:
        if not isinstance(name, str) or not name:
            raise RemoteError('preflight_failed', 'Required executable must be a nonempty string')
        if '/' in name:
            path = Path(name).expanduser()
            if not path.is_absolute(): path = directory / path
            result[name] = str(path) if path.is_file() and os.access(path, os.X_OK) else None
        else:
            # Relative PATH entries belong to the requested task directory.
            absolute_search = os.pathsep.join(str(directory / part) if not Path(part).is_absolute() else part
                                             for part in search.split(os.pathsep))
            result[name] = shutil.which(name, path=absolute_search)
    return result


def check_environment(spec, cwd):
    executables = resolve_executables([spec['interpreter'][0], *spec.get('requires', [])], cwd, spec['env'])
    missing = [name for name, path in executables.items() if path is None]
    if missing:
        raise RemoteError('preflight_failed', 'Required executable unavailable: ' + ', '.join(missing))
    for item in spec.get('inputs', []):
        path = Path(item['path']).expanduser()
        if not path.is_absolute(): path = cwd / path
        if not path.is_file() or path.stat().st_size != item['size'] or file_digest(path) != item['sha256']:
            raise RemoteError('preflight_failed', 'Input missing or changed: ' + str(path))


def transfer_dir(transfer_id):
    if not re.fullmatch(r'[a-f0-9]{32}', transfer_id):
        raise ValueError('Invalid transfer_id')
    return ROOT / 'transfers' / transfer_id


@contextmanager
def transfer_lock(folder):
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / 'lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RemoteError('transfer_busy', 'This transfer already has an active writer; query transfer-status') from exc
        yield


def target_fingerprint(path):
    if not path.exists(): return None
    if not path.is_file(): raise RemoteError('transfer_conflict', 'Destination is not a regular file')
    return {'size': path.stat().st_size, 'sha256': file_digest(path)}


def upload_status(transfer_id):
    folder = transfer_dir(transfer_id)
    try:
        info = json.loads((folder / 'transfer.json').read_text())
    except FileNotFoundError as exc:
        raise RemoteError('transfer_not_found', 'No transfer record: ' + transfer_id) from exc
    stage = Path(info['stage_path'])
    received = stage.stat().st_size if stage.exists() else (info['size'] if info['phase'] == 'complete' else 0)
    return {k: info[k] for k in ('transfer_id', 'path', 'size', 'sha256', 'phase')} | {'bytes_received': received}


def upload_prepare(request):
    transfer_id = request['transfer_id']; folder = transfer_dir(transfer_id)
    destination = Path(request['destination']).expanduser().resolve()
    spec = {'path': str(destination), 'size': request['size'], 'sha256': request['sha256'], 'mode': request['mode']}
    if not isinstance(spec['size'], int) or spec['size'] < 0 or not re.fullmatch(r'[a-f0-9]{64}', spec['sha256']):
        raise ValueError('Invalid transfer size or digest')
    if not isinstance(spec['mode'], int) or not 0 <= spec['mode'] <= 0o777:
        raise ValueError('Upload mode must be 000..777')
    with transfer_lock(folder):
        metadata = folder / 'transfer.json'
        if metadata.exists():
            info = json.loads(metadata.read_text())
            if any(info[k] != value for k, value in spec.items()):
                raise RemoteError('transfer_conflict', 'Transfer ID belongs to a different file request')
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            info = {**spec, 'transfer_id': transfer_id, 'baseline': target_fingerprint(destination),
                    'phase': 'ready', 'stage_path': str(destination.parent / ('.ssh4codex-' + transfer_id + '.part'))}
            atomic_json(metadata, info); metadata.chmod(0o600)
        current = target_fingerprint(destination)
        expected = {'size': info['size'], 'sha256': info['sha256']}
        if current == expected:
            destination.chmod(info['mode'])
            info['phase'] = 'complete'; atomic_json(metadata, info)
            Path(info['stage_path']).unlink(missing_ok=True)
            return {**upload_status(transfer_id), 'offset': info['size'], 'prefix_sha256': info['sha256']}
        if info['phase'] == 'complete' or current != info['baseline']:
            raise RemoteError('transfer_conflict', 'Destination changed after this transfer began; it was not overwritten')
        stage = Path(info['stage_path'])
        offset = stage.stat().st_size if stage.exists() else 0
        if offset > info['size']:
            raise RemoteError('transfer_integrity', 'Partial file exceeds the declared size')
        return {**upload_status(transfer_id), 'offset': offset,
                'prefix_sha256': file_digest(stage) if stage.exists() else hashlib.sha256(b'').hexdigest()}


def upload_stream(transfer_id, offset, source):
    folder = transfer_dir(transfer_id)
    with transfer_lock(folder):
        info = json.loads((folder / 'transfer.json').read_text())
        stage = Path(info['stage_path']); destination = Path(info['path'])
        if info['phase'] == 'complete':
            raise RemoteError('transfer_busy', 'Transfer already completed; verify with put before retrying')
        if offset != (stage.stat().st_size if stage.exists() else 0):
            raise RemoteError('transfer_busy', 'Partial offset changed; query and verify before resuming')
        info['phase'] = 'sending'; atomic_json(folder / 'transfer.json', info)
        with stage.open('ab', buffering=0) as handle:
            stage.chmod(0o600)
            while chunk := source.read(512 * 1024):
                if handle.tell() + len(chunk) > info['size']:
                    raise RemoteError('transfer_integrity', 'Upload exceeds declared size')
                handle.write(chunk)
            os.fsync(handle.fileno())
        info['phase'] = 'verifying'; atomic_json(folder / 'transfer.json', info)
        if stage.stat().st_size != info['size']:
            info['phase'] = 'partial'; atomic_json(folder / 'transfer.json', info)
            raise RemoteError('transfer_incomplete', 'Upload interrupted; verified resume is available')
        if file_digest(stage) != info['sha256']:
            raise RemoteError('transfer_integrity', 'Upload checksum mismatch; destination was not replaced')
        # Serialize competing transfers to the same destination during compare-and-replace.
        destination_lock = ROOT / 'transfer-destinations' / hashlib.sha256(str(destination).encode()).hexdigest()
        with locked(destination_lock):
            current = target_fingerprint(destination)
            expected = {'size': info['size'], 'sha256': info['sha256']}
            if current != info['baseline'] and current != expected:
                raise RemoteError('transfer_conflict', 'Destination changed during upload; it was not overwritten')
            os.chmod(stage, info['mode']); os.replace(stage, destination)
            info['phase'] = 'complete'; atomic_json(folder / 'transfer.json', info)
        return upload_status(transfer_id)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['rpc', 'worker', 'download', 'upload'])
    parser.add_argument('task_id', nargs='?')
    parser.add_argument('offset', nargs='?', type=int)
    args = parser.parse_args()
    if args.action == 'worker':
        worker(args.task_id)
    elif args.action == 'upload':
        try:
            print(json.dumps(upload_stream(args.task_id, args.offset, sys.stdin.buffer)))
        except Exception as exc:
            print(json.dumps({'error': getattr(exc, 'kind', 'transfer'), 'message': str(exc)}))
            sys.exit(1)
    elif args.action == 'download':
        try:
            download(args.task_id)
        except Exception as exc:
            print(json.dumps({'error': getattr(exc, 'kind', 'transfer'), 'message': str(exc)}), file=sys.stderr)
            sys.exit(1)
    else:
        try:
            print(json.dumps(dispatch(json.load(sys.stdin)), ensure_ascii=False))
        except Exception as exc:
            print(json.dumps({'error': getattr(exc, 'kind', type(exc).__name__), 'message': str(exc)}, ensure_ascii=False))
            sys.exit(1)
