"""OpenSSH transport and task API. Private keys remain under OpenSSH control."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import shutil
import tarfile
import tempfile
import threading
import uuid
import time

from . import remote


class SSHError(RuntimeError):
    def __init__(self, kind, message, task_id=None, **details):
        super().__init__(message)
        self.kind, self.task_id = kind, task_id
        self.details = details


def config_path():
    return Path(os.environ.get('SSH4CODEX_CONFIG', '~/.config/ssh4codex/config.json')).expanduser()


def load_server(name):
    path = config_path()
    config = json.loads(path.read_text()) if path.exists() else {'servers': {}}
    if name in config.get('servers', {}):
        server = config['servers'][name]
    else:
        raise SSHError('configuration', 'Server is not defined in local configuration: ' + name)
    for field in ('target', 'session'):
        if not isinstance(server.get(field), str) or not server[field]:
            raise SSHError('configuration', 'Local server configuration requires ' + field)
    target = server['target']
    if target.startswith('-') or any(c.isspace() for c in target):
        raise SSHError('configuration', 'Invalid SSH target')
    for option in ['connect_timeout', 'rpc_timeout', 'transfer_timeout']:
        if option in server and (not isinstance(server[option], (int, float)) or server[option] <= 0):
            raise SSHError('configuration', option + ' must be positive')
    if 'compression' in server and not isinstance(server['compression'], bool):
        raise SSHError('configuration', 'compression must be a boolean')
    return server


def ssh_program():
    override = os.environ.get('SSH4CODEX_SSH')
    if override:
        return override
    if getattr(sys, 'frozen', False):
        bundled = Path(sys.executable).resolve().parent / 'tools/ssh'
        if bundled.is_file():
            return str(bundled)
    program = shutil.which('ssh')
    if not program:
        raise SSHError('configuration', 'OpenSSH executable not found')
    return program


def transport_env():
    env = dict(os.environ)
    # PyInstaller alters loader paths for its own runtime; do not leak them to external SSH.
    if getattr(sys, 'frozen', False):
        original = env.get('LD_LIBRARY_PATH_ORIG')
        if original is None:
            env.pop('LD_LIBRARY_PATH', None)
        else:
            env['LD_LIBRARY_PATH'] = original
    return env


def compact(state):
    fields = ['task_id', 'state', 'exit_code', 'session', 'pane', 'reused', 'error', 'message', 'artifacts', 'stdout', 'stderr', 'cancel_requested']
    return {k: state[k] for k in fields if k in state}


class Client:
    def __init__(self, server_name, *, fresh_connection=False, rpc_timeout=None, transfer_timeout=None):
        self.name = server_name
        self.server = dict(load_server(server_name))
        transport_hash = hashlib.sha256(json.dumps(self.server, sort_keys=True).encode()).hexdigest()[:16]
        self.fresh_connection = fresh_connection
        for key, value in (("rpc_timeout", rpc_timeout), ("transfer_timeout", transfer_timeout)):
            if value is not None:
                if value <= 0: raise ValueError(key + " must be positive")
                self.server[key] = value
        root = Path(os.environ.get('SSH4CODEX_STATE', '~/.local/state/ssh4codex')).expanduser()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        self.local = root
        self.tmux_target = root / (transport_hash + '.tmux')
        # Short, private socket directory avoids Unix socket path-length failures.
        sockets = Path.home() / '.ssh/ssh4codex'
        sockets.mkdir(parents=True, exist_ok=True, mode=0o700)
        sockets.chmod(0o700)
        self.socket = self.server.get('control_path', str(sockets / transport_hash))
        self.options = ['-o', 'ControlMaster=auto',
                        '-o', 'ControlPersist=4h', '-o', 'ControlPath=' + self.socket,
                        '-o', 'ConnectTimeout=' + str(self.server.get('connect_timeout', 8)), '-o', 'ServerAliveInterval=5',
                        '-o', 'ServerAliveCountMax=2', '-o', 'BatchMode=yes']
        self.options += ['-o', 'Compression=' + ('yes' if self.server.get('compression', True) else 'no')]
        if self.server.get('port') is not None:
            self.options += ['-p', str(self.server['port'])]
        if self.server.get('identity_file'):
            self.options += ['-i', str(Path(self.server['identity_file']).expanduser()), '-o', 'IdentitiesOnly=yes']
        if self.server.get('ssh_config'):
            self.options += ['-F', str(Path(self.server['ssh_config']).expanduser())]
        source = Path(__file__).with_name('remote.py').read_bytes()
        digest = hashlib.sha256(source).hexdigest()
        self.agent_relative = '.local/share/ssh4codex/runtime/' + digest + '/remote.py'
        self.agent_marker = root / (transport_hash + '-' + digest + '.installed')
        self.source = source

    def ssh_argv(self, command, fresh=False):
        bypass = ['-o', 'ControlPath=none', '-o', 'ControlMaster=no'] if fresh or self.fresh_connection else []
        return [ssh_program(), *bypass, *self.options, self.server['target'], command]

    def connect(self, session=None):
        """Interactive SSH + tmux; remember the last selected target before I/O."""
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise SSHError('configuration', 'connect requires an interactive terminal; use run/status for agent tasks')
        if session is None:
            session = self.tmux_target.read_text().strip() if self.tmux_target.exists() else self.server['session']
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', session):
            raise ValueError('Invalid tmux session name')
        # Keep the selected target even if the SSH connection later drops.
        fd, temporary = tempfile.mkstemp(dir=self.local)
        try:
            with os.fdopen(fd, 'w') as handle:
                handle.write(session)
            os.replace(temporary, self.tmux_target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        command = 'exec ' + shlex.join(['tmux', 'new-session', '-A', '-s', session,
                                      '-c', self.server.get('cwd', '.')])
        argv = self.ssh_argv(command)
        argv.insert(1, '-tt')
        print(f'Connecting to {self.name} / tmux {session}', file=sys.stderr, flush=True)
        # Here stdin deliberately carries terminal input, unlike MCP downloads.
        return subprocess.run(argv, env=transport_env()).returncode

    def call_ssh(self, command, payload=None, timeout=20, uncertain_task=None, fresh=False):
        try:
            result = subprocess.run(self.ssh_argv(command, fresh), input=payload, capture_output=True, timeout=timeout, env=transport_env())
        except subprocess.TimeoutExpired as exc:
            raise SSHError('submission_unknown' if uncertain_task else 'transport_timeout',
                           'SSH response timed out; remote task may still be running. Query status; do not resubmit with a new id.', uncertain_task) from exc
        if result.returncode == 255:
            message = result.stderr.decode(errors='replace').strip() or 'SSH exited with status 255 without diagnostics'
            if 'Permission denied' in message or 'authentication' in message:
                kind = 'authentication'
            elif 'Host key verification failed' in message or 'REMOTE HOST IDENTIFICATION' in message:
                kind = 'host_key'
            else:
                kind = 'submission_unknown' if uncertain_task else 'transport'
            raise SSHError(kind, message, uncertain_task)
        return result

    def install_agent(self):
        # The helper is immutable by hash, so new clients cannot replace a running task's code.
        code = "import sys,json,pathlib,tempfile,os; p=json.load(sys.stdin); f=pathlib.Path.home()/p['path']; f.parent.mkdir(parents=True,exist_ok=True,mode=0o700); fd,t=tempfile.mkstemp(dir=f.parent); h=os.fdopen(fd,'w'); h.write(p['source']); h.close(); os.chmod(t,0o700); os.replace(t,f)"
        command = 'python3 -c ' + shlex.quote(code)
        payload = json.dumps({'path': self.agent_relative, 'source': self.source.decode()}).encode()
        timeout = self.server.get('rpc_timeout', 10) + self.server.get('connect_timeout', 8)
        try:
            result = self.call_ssh(command, payload, timeout=timeout)
        except SSHError as exc:
            if exc.kind not in {'transport', 'transport_timeout'}:
                raise
            # Reinstalling the same immutable content is safe, unlike a task.
            result = self.call_ssh(command, payload, timeout=timeout, fresh=True)
        if result.returncode:
            raise SSHError('setup', result.stderr.decode(errors='replace'))
        self.agent_marker.touch(mode=0o600)

    def rpc(self, action, **args):
        if not self.agent_marker.exists():
            self.install_agent()
        # Relative path resolved under the SSH login home, independent of the project cwd.
        command = 'python3 ' + shlex.quote(self.agent_relative) + ' rpc'
        payload = json.dumps({'action': action, **args}).encode()
        timeout = self.server.get('rpc_timeout', 10) + args.get('wait_seconds', 0)
        uncertain = args.get('task_id') if action == 'submit' else None
        observation = action in {'status', 'status_many', 'wait', 'list', 'doctor', 'upload_status', 'upload_prepare'}
        for attempt in range(2 if observation else 1):
            budget = timeout + (self.server.get('connect_timeout', 8) if attempt else 0)
            try:
                result = self.call_ssh(command, payload, timeout=budget,
                                       uncertain_task=uncertain, fresh=bool(attempt))
            except SSHError as exc:
                # Observations can be replayed on a fresh connection. Never
                # kill a shared master or automatically resubmit side effects.
                if observation and not attempt and exc.kind in {'transport', 'transport_timeout'}:
                    continue
                raise
            # Missing helper is a verified pre-execution failure.
            if result.returncode == 2 and b"can't open file" in result.stderr:
                self.install_agent()
                result = self.call_ssh(command, payload, timeout=budget,
                                       uncertain_task=uncertain, fresh=bool(attempt))
            try:
                value = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                # A disconnected mux client may return 0 with partial JSON.
                if observation and not attempt and result.returncode == 0:
                    continue
                raise SSHError('submission_unknown' if uncertain else 'protocol',
                               'Remote helper did not return complete JSON: ' + result.stderr.decode(errors='replace')[:1000], uncertain) from exc
            if result.returncode:
                raise SSHError(value.get('error', 'remote'), value.get('message', str(value)), args.get('task_id'))
            return value

    def profile(self, name=None):
        if name is None:
            return {}
        profiles = self.server.get('profiles', {})
        if name not in profiles:
            raise SSHError('configuration', 'Unknown execution profile: ' + name)
        return profiles[name]

    def endpoint(self):
        # Timeouts/cwd/profile edits must not invalidate recovery. Host changes must.
        return {key: self.server.get(key) for key in ('target', 'port', 'identity_file', 'ssh_config')}

    def submit(self, script, cwd=None, env=None, interpreter=None, artifacts=None, timeout=None,
               task_id=None, wait_seconds=0, limit=2048, inputs=None, requires=None, profile=None):
        if not 0 <= wait_seconds <= 60: raise ValueError('wait_seconds must be 0..60')
        task_id = task_id or uuid.uuid4().hex
        remote.job_dir(task_id)
        defaults = self.profile(profile)
        spec = dict(task_id=task_id, script=script, cwd=cwd or defaults.get('cwd') or self.server.get('cwd', '.'),
                    env={**defaults.get('env', {}), **(env or {})},
                    interpreter=interpreter or defaults.get('interpreter') or ['bash'], artifacts=artifacts or [],
                    timeout=timeout, session=self.server['session'])
        required = list(dict.fromkeys([*defaults.get('requires', []), *(requires or [])]))
        if required: spec['requires'] = required
        path = self.local / ('task-' + task_id + '.json')
        with (self.local / ('task-' + task_id + '.lock')).open('a') as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            previous = json.loads(path.read_text()) if path.exists() else {}
            if previous and (previous['server'] != self.name or previous.get('endpoint', self.endpoint()) != self.endpoint()):
                raise SSHError('configuration', 'task_id already registered to another server endpoint', task_id)
            recorded = previous.get('request')
            if recorded and {k: v for k, v in recorded.items() if k != 'inputs'} != spec:
                raise SSHError('request_conflict', 'task_id already records a different request', task_id)
            fingerprints = []
            for source, destination in sorted((inputs or {}).items()):
                if not destination.startswith(('/', '~/')):
                    raise ValueError('Input destinations must be absolute or start with ~/')
                local_source = Path(source).expanduser()
                fingerprints.append({'destination': destination, 'size': local_source.stat().st_size,
                                     'sha256': remote.file_digest(local_source)})
            if recorded and previous.get('input_fingerprints', []) != fingerprints:
                raise SSHError('request_conflict', 'task_id already records different input content', task_id)
            # Keep the per-task lock across staging: a conflicting retry cannot overwrite inputs.
            # A failed upload never produces a runnable saved request.
            if inputs:
                staged = []
                for (source, destination), expected in zip(sorted(inputs.items()), fingerprints):
                    item = self.put(source, destination)
                    if item['size'] != expected['size'] or item['sha256'] != expected['sha256']:
                        raise SSHError('transfer_integrity', 'Input changed while being staged', task_id)
                    staged.append({k: item[k] for k in ('path', 'size', 'sha256')})
                spec['inputs'] = staged
            record = {'server': self.name, 'task_id': task_id, 'endpoint': self.endpoint(), 'request': spec}
            if fingerprints: record['input_fingerprints'] = fingerprints
            if recorded and previous != record:
                raise SSHError('request_conflict', 'task_id already records a different request', task_id)
            self.save_private(path, record)
        return compact(self.rpc('submit', **spec, wait_seconds=wait_seconds, limit=limit))

    def save_private(self, path, value):
        fd, temporary = tempfile.mkstemp(dir=self.local)
        try:
            with os.fdopen(fd, 'w') as handle:
                json.dump(value, handle)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def recover(self, task_id, retry=False, limit=2048):
        remote.job_dir(task_id)
        path = self.local / ('task-' + task_id + '.json')
        record = json.loads(path.read_text()) if path.exists() else None
        if record and (record['server'] != self.name or
                       record.get('endpoint', self.endpoint()) != self.endpoint()):
            raise SSHError('configuration', 'Task is registered to a different server endpoint', task_id)
        # A new instance avoids changing a concurrently used client's connection mode.
        client = Client(self.name, fresh_connection=True, rpc_timeout=self.server.get('rpc_timeout'),
                        transfer_timeout=self.server.get('transfer_timeout'))
        try:
            return client.status(task_id, limit=limit, tail=True)
        except SSHError as exc:
            if exc.kind != 'task_not_found': raise
        if not retry:
            return {'task_id': task_id, 'state': 'not_found', 'retry_available': bool(record and record.get('request')),
                    'next_action': 'recover with retry=True to replay the saved request under the same task_id'}
        if not record or not record.get('request'):
            raise SSHError('request_unavailable', 'No saved request; cannot reconstruct or replay this task', task_id)
        return compact(client.rpc('submit', **record['request'], wait_seconds=0, limit=limit))

    def poll(self, task_id, consumer, limit=2048, reset=False):
        remote.job_dir(task_id)
        if not isinstance(consumer, str) or not consumer.strip():
            raise ValueError('A nonempty consumer name is required')
        key = hashlib.sha256(json.dumps([self.endpoint(), task_id, consumer], sort_keys=True).encode()).hexdigest()
        path = self.local / ('cursor-' + key + '.json')
        with (self.local / ('cursor-' + key + '.lock')).open('a') as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            cursor = json.loads(path.read_text()) if path.exists() and not reset else {}
            value = self.status(task_id, cursor.get('stdout', 0), cursor.get('stderr', 0), limit)
            self.save_private(path, {name: value[name]['cursor'] for name in ('stdout', 'stderr')})
            return value

    def doctor(self, requires=None, cwd=None, env=None, profile=None):
        defaults = self.profile(profile)
        return self.rpc('doctor', cwd=cwd or defaults.get('cwd') or self.server.get('cwd', '.'),
                        env={**defaults.get('env', {}), **(env or {})},
                        requires=list(dict.fromkeys([*defaults.get('requires', []), *(requires or []),
                                                     *(defaults.get('interpreter', [])[:1])])))

    def status(self, task_id, stdout_cursor=0, stderr_cursor=0, limit=2048, tail=False, logs=True):
        return compact(self.rpc('status', task_id=task_id, stdout_cursor=stdout_cursor,
                                stderr_cursor=stderr_cursor, limit=limit, tail=tail, logs=logs))

    def wait(self, task_id, seconds=10, limit=2048):
        if not 0 <= seconds <= 60:
            raise ValueError('wait must be between 0 and 60 seconds')
        return compact(self.rpc('wait', task_id=task_id, wait_seconds=seconds, limit=limit))

    def status_many(self, task_ids, logs=False, limit=2048):
        if not isinstance(task_ids, list) or not 1 <= len(task_ids) <= 64:
            raise ValueError('status_many requires 1..64 task IDs')
        for task_id in task_ids:
            remote.job_dir(task_id)
        value = self.rpc('status_many', task_ids=task_ids, logs=logs, limit=limit)
        return {'tasks': [compact(item) for item in value['tasks']]}

    def cancel(self, task_id):
        return compact(self.rpc('cancel', task_id=task_id))

    def transfer_status(self, transfer_id):
        return self.rpc('upload_status', transfer_id=transfer_id)

    def put(self, source, destination, mode=0o600, retries=2, progress=None):
        """Resume an identical transfer, verify its prefix and commit only a complete hash."""
        source = Path(source).expanduser()
        if not isinstance(retries, int) or not 0 <= retries <= 5:
            raise ValueError('retries must be 0..5')
        if not isinstance(mode, int) or not 0 <= mode <= 0o777:
            raise ValueError('mode must be an octal permission value up to 0777')
        size, expected = source.stat().st_size, remote.file_digest(source)
        identity = [self.endpoint(), destination, size, expected, mode]
        transfer_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]
        spec = dict(transfer_id=transfer_id, destination=destination, size=size, sha256=expected, mode=mode)
        if progress: progress({'transfer_id': transfer_id, 'phase': 'preparing', 'size': size})
        last_message = 'Transfer did not complete'
        resumed_from = 0
        for attempt in range(retries + 1):
            try:
                ready = self.rpc('upload_prepare', **spec)
                if progress: progress({k: ready[k] for k in ('transfer_id', 'phase', 'bytes_received', 'size')})
                if ready['phase'] == 'complete':
                    return {**ready, 'attempts': attempt, 'resumed_from': resumed_from}
                offset = ready['offset']
                resumed_from = max(resumed_from, offset)
                if source.stat().st_size != size or remote.file_digest(source, offset) != ready['prefix_sha256']:
                    raise SSHError('transfer_integrity', 'Source changed or remote partial upload has a different prefix')
                command = 'python3 ' + shlex.quote(self.agent_relative) + ' upload ' + transfer_id + ' ' + str(offset)
                # Dedicated connection: a large stream must not monopolize the shared control master.
                with source.open('rb') as handle:
                    handle.seek(offset)
                    proc = subprocess.run(self.ssh_argv(command, fresh=True), stdin=handle, capture_output=True,
                                          timeout=self.server.get('transfer_timeout', 300), env=transport_env())
                if proc.returncode == 255:
                    message = proc.stderr.decode(errors='replace').strip() or 'SSH upload exited 255 without diagnostics'
                    if 'Permission denied' in message: raise SSHError('authentication', message)
                    if 'Host key verification failed' in message or 'REMOTE HOST IDENTIFICATION' in message:
                        raise SSHError('host_key', message)
                    raise SSHError('transfer_unknown', message)
                try:
                    value = json.loads(proc.stdout)
                except json.JSONDecodeError as exc:
                    raise SSHError('transfer_unknown', 'Upload acknowledgement missing or incomplete; saved transfer can be resumed') from exc
                if proc.returncode:
                    raise SSHError(value.get('error', 'transfer'), value.get('message', 'Upload failed'))
                return {**value, 'attempts': attempt + 1, 'resumed_from': resumed_from}
            except subprocess.TimeoutExpired:
                last_message = 'Upload response timed out; remote partial data is retained'
            except SSHError as exc:
                if exc.kind not in {'transfer_unknown', 'transfer_busy', 'transfer_incomplete', 'transport', 'transport_timeout'}:
                    exc.details.update(transfer_id=transfer_id, resumed_from=resumed_from)
                    raise
                last_message = str(exc)
            if attempt < retries:
                time.sleep(min(0.5 * (attempt + 1), 1))
        # Lost final acknowledgement may still mean a successful atomic commit.
        try:
            value = self.rpc('upload_prepare', **spec)
            if value['phase'] == 'complete':
                return {**value, 'attempts': retries + 1, 'resumed_from': resumed_from}
        except SSHError:
            pass
        raise SSHError('transfer_unknown', last_message + '; rerun the same put to resume',
                       transfer_id=transfer_id, resumed_from=resumed_from, attempts=retries + 1)

    def fetch(self, task_id, destination):
        remote.job_dir(task_id)
        if not self.agent_marker.exists():
            self.install_agent()
        try:
            return self._fetch_once(task_id, destination)
        except SSHError as exc:
            if exc.kind == 'helper_missing':
                self.install_agent()
                return self._fetch_once(task_id, destination)
            if exc.kind in {'transport', 'transport_timeout'}:
                return self._fetch_once(task_id, destination, fresh=True)
            raise

    def _fetch_once(self, task_id, destination, fresh=False):
        target = Path(destination).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        command = 'python3 ' + shlex.quote(self.agent_relative) + ' download ' + shlex.quote(task_id)
        temporary = []
        fetched = []
        timeout = self.server.get('transfer_timeout', 300)
        expired = threading.Event()
        with tempfile.TemporaryFile() as errors:
            # Downloads have no input. In MCP mode fd 0 carries JSON-RPC;
            # inheriting it lets SSH consume another tool request as remote stdin.
            proc = subprocess.Popen(self.ssh_argv(command, fresh), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=errors, env=transport_env())
            def stop():
                expired.set()
                proc.kill()
            timer = threading.Timer(timeout, stop)
            timer.daemon = True
            timer.start()
            failure = None
            try:
                try:
                    with tarfile.open(fileobj=proc.stdout, mode='r|') as stream:
                        header = stream.next()
                        if header is None or header.name != 'manifest.json' or not header.isfile() or header.size > 16 * 1024 * 1024:
                            raise SSHError('protocol', 'Missing or invalid artifact manifest')
                        manifest = json.load(stream.extractfile(header))
                        if manifest['task_id'] != task_id:
                            raise SSHError('protocol', 'Artifact manifest belongs to another task')
                        records = manifest['files']
                        names = [Path(item['path']).name for item in records]
                        if len(set(names)) != len(names):
                            raise SSHError('artifact_collision', 'Artifacts have duplicate basenames')
                        for i, item in enumerate(records):
                            header = stream.next()
                            if header is None:
                                raise SSHError('transport', 'Artifact stream ended before declared files arrived')
                            if header.name != str(i) or not header.isfile() or header.size != item['size']:
                                raise SSHError('protocol', 'Incomplete or invalid artifact stream')
                            fd, name = tempfile.mkstemp(prefix='.ssh4codex-part-', dir=target)
                            tmp = Path(name)
                            temporary.append((tmp, target / names[i]))
                            digest = hashlib.sha256()
                            with os.fdopen(fd, 'wb') as out:
                                content = stream.extractfile(header)
                                for chunk in iter(lambda: content.read(1024 * 1024), b''):
                                    digest.update(chunk)
                                    out.write(chunk)
                            if digest.hexdigest() != item['sha256'] or tmp.stat().st_size != item['size']:
                                raise SSHError('artifact_changed', 'Artifact changed since task completion: ' + item['path'])
                            fetched.append({'path': str((target / names[i]).resolve()), 'size': item['size'], 'sha256': item['sha256']})
                        if stream.next() is not None:
                            raise SSHError('protocol', 'Unexpected artifact stream member')
                except (tarfile.TarError, ValueError, KeyError, SSHError) as exc:
                    failure = exc
                if failure:
                    # Drain framing/trailers so a remote error can be reported.
                    while proc.stdout.read(65536):
                        pass
                proc.wait()
                errors.seek(0)
                stderr = errors.read(4096).decode(errors='replace')
                if expired.is_set():
                    raise SSHError('transport_timeout', 'Artifact download timed out')
                if proc.returncode == 255:
                    if 'Permission denied' in stderr:
                        raise SSHError('authentication', stderr)
                    if 'Host key verification failed' in stderr or 'REMOTE HOST IDENTIFICATION' in stderr:
                        raise SSHError('host_key', stderr)
                    raise SSHError('transport', stderr)
                if proc.returncode:
                    if proc.returncode == 2 and "can't open file" in stderr:
                        raise SSHError('helper_missing', stderr)
                    try:
                        error = json.loads(stderr)
                    except ValueError:
                        raise SSHError('transfer', stderr)
                    raise SSHError(error['error'], error['message'])
                if failure:
                    if isinstance(failure, SSHError):
                        raise failure
                    # A mux client can exit 0 when the master loses its TCP
                    # stream. Declared framing detects truncation independently.
                    if isinstance(failure, tarfile.ReadError) and str(failure) in {'unexpected end of data', 'empty file', 'truncated header'}:
                        raise SSHError('transport', 'Artifact stream was truncated despite SSH exit status 0') from failure
                    raise SSHError('protocol', 'Invalid artifact stream: ' + str(failure)) from failure
                # Publish only after the complete stream and exit code verify.
                for tmp, dest in temporary:
                    tmp.replace(dest)
                return {'task_id': task_id, 'files': fetched}
            finally:
                timer.cancel()
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
                proc.stdout.close()
                for tmp, _ in temporary:
                    tmp.unlink(missing_ok=True)
