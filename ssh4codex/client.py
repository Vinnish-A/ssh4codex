"""OpenSSH transport and task API. Private keys remain under OpenSSH control."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import shutil
import uuid

from . import remote


class SSHError(RuntimeError):
    def __init__(self, kind, message, task_id=None):
        super().__init__(message)
        self.kind, self.task_id = kind, task_id


def config_path():
    return Path(os.environ.get('SSH4CODEX_CONFIG', '~/.config/ssh4codex/config.json')).expanduser()


def server_catalog():
    return json.loads(Path(__file__).with_name('catalog.json').read_text())


def load_server(name):
    path = config_path()
    config = json.loads(path.read_text()) if path.exists() else {'servers': {}}
    if name in config.get('servers', {}):
        server = config['servers'][name]
    elif name in server_catalog():
        entry = server_catalog()[name]
        server = {k: entry[k] for k in ['target', 'port', 'cwd', 'session']}
        for candidate in entry.get('identity_candidates', []):
            if Path(candidate).expanduser().is_file():
                server['identity_file'] = candidate
                break
    else:
        raise SSHError('configuration', 'Unknown server: ' + name)
    target = server['target']
    if target.startswith('-') or any(c.isspace() for c in target):
        raise SSHError('configuration', 'Invalid SSH target')
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
    fields = ['task_id', 'state', 'exit_code', 'session', 'pane', 'reused', 'error', 'artifacts', 'stdout', 'stderr', 'cancel_requested']
    return {k: state[k] for k in fields if k in state}


class Client:
    def __init__(self, server_name):
        self.name = server_name
        self.server = load_server(server_name)
        root = Path(os.environ.get('SSH4CODEX_STATE', '~/.local/state/ssh4codex')).expanduser()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        self.local = root
        transport_hash = hashlib.sha256(json.dumps(self.server, sort_keys=True).encode()).hexdigest()[:16]
        # Short, private socket directory avoids Unix socket path-length failures.
        sockets = Path.home() / '.ssh/ssh4codex'
        sockets.mkdir(parents=True, exist_ok=True, mode=0o700)
        sockets.chmod(0o700)
        self.socket = self.server.get('control_path', str(sockets / transport_hash))
        self.options = ['-o', 'ControlMaster=auto',
                        '-o', 'ControlPersist=4h', '-o', 'ControlPath=' + self.socket,
                        '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=15',
                        '-o', 'ServerAliveCountMax=2', '-o', 'BatchMode=yes']
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

    def ssh_argv(self, command):
        return [ssh_program(), *self.options, self.server['target'], command]

    def call_ssh(self, command, payload=None, timeout=20, uncertain_task=None):
        try:
            result = subprocess.run(self.ssh_argv(command), input=payload, capture_output=True, timeout=timeout, env=transport_env())
        except subprocess.TimeoutExpired as exc:
            raise SSHError('submission_unknown' if uncertain_task else 'transport_timeout',
                           'SSH response timed out; remote task may still be running. Query status; do not resubmit with a new id.', uncertain_task) from exc
        if result.returncode == 255:
            message = result.stderr.decode(errors='replace').strip()
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
        result = self.call_ssh('python3 -c ' + shlex.quote(code),
                               json.dumps({'path': self.agent_relative, 'source': self.source.decode()}).encode())
        if result.returncode:
            raise SSHError('setup', result.stderr.decode(errors='replace'))
        self.agent_marker.touch(mode=0o600)

    def rpc(self, action, **args):
        if not self.agent_marker.exists():
            self.install_agent()
        # Relative path resolved under the SSH login home, independent of the project cwd.
        command = 'python3 ' + shlex.quote(self.agent_relative) + ' rpc'
        payload = json.dumps({'action': action, **args}).encode()
        result = self.call_ssh(command, payload, timeout=max(20, args.get('wait_seconds', 0) + 10), uncertain_task=args.get('task_id') if action == 'submit' else None)
        # A missing helper is a pre-execution failure; only this case is safe to retry.
        if result.returncode == 2 and b"can't open file" in result.stderr:
            self.install_agent()
            result = self.call_ssh(command, payload, timeout=max(20, args.get('wait_seconds', 0) + 10), uncertain_task=args.get('task_id') if action == 'submit' else None)
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise SSHError('protocol', 'Remote helper did not return JSON: ' + result.stderr.decode(errors='replace')[:1000]) from exc
        if result.returncode:
            raise SSHError('remote', value.get('message', str(value)), args.get('task_id'))
        return value

    def submit(self, script, cwd=None, env=None, interpreter=None, artifacts=None, timeout=None, task_id=None, wait_seconds=0, limit=2048):
        task_id = task_id or uuid.uuid4().hex
        remote.job_dir(task_id)  # Validate without creating any local remote-state paths.
        spec = dict(task_id=task_id, script=script, cwd=cwd or self.server.get('cwd', '.'),
                    env=env or {}, interpreter=interpreter or ['bash'], artifacts=artifacts or [],
                    timeout=timeout, session=self.server.get('session', 'ssh4codex'))
        # Persist identity before network mutation: recover even if the response is lost.
        path = self.local / ('task-' + task_id + '.json')
        record = {'server': self.name, 'task_id': task_id}
        if path.exists() and json.loads(path.read_text()) != record:
            raise SSHError('configuration', 'task_id already registered to another server')
        path.write_text(json.dumps(record))
        path.chmod(0o600)
        if not 0 <= wait_seconds <= 60: raise ValueError('wait_seconds must be 0..60')
        return compact(self.rpc('submit', **spec, wait_seconds=wait_seconds, limit=limit))

    def status(self, task_id, stdout_cursor=0, stderr_cursor=0, limit=2048, tail=False, logs=True):
        return compact(self.rpc('status', task_id=task_id, stdout_cursor=stdout_cursor,
                                stderr_cursor=stderr_cursor, limit=limit, tail=tail, logs=logs))

    def wait(self, task_id, seconds=10, limit=2048):
        if not 0 <= seconds <= 60:
            raise ValueError('wait must be between 0 and 60 seconds')
        return compact(self.rpc('wait', task_id=task_id, wait_seconds=seconds, limit=limit))

    def cancel(self, task_id):
        return compact(self.rpc('cancel', task_id=task_id))

    def put(self, source, destination, mode=0o600):
        source = Path(source).expanduser()
        digest = hashlib.sha256()
        with source.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
        expected = digest.hexdigest()
        encoded = base64.b64encode(destination.encode()).decode()
        # Python 3.10 on the remote: stream hashing rather than file_digest (3.11+).
        code = "\n".join([
            "import base64,pathlib,sys,tempfile,os,hashlib,json",
            "p=pathlib.Path(base64.b64decode('" + encoded + "').decode()).expanduser()",
            "p.parent.mkdir(parents=True,exist_ok=True)",
            "fd,t=tempfile.mkstemp(prefix='.ssh4codex-',dir=p.parent)",
            "try:",
            " h=hashlib.sha256()",
            " with os.fdopen(fd,'wb') as f:",
            "  for chunk in iter(lambda: sys.stdin.buffer.read(1048576),b''):",
            "   h.update(chunk); f.write(chunk)",
            " if h.hexdigest() != '" + expected + "': raise ValueError('Upload checksum mismatch')",
            " os.chmod(t," + str(mode) + "); os.replace(t,p)",
            " print(json.dumps({'path':str(p.resolve()),'size':p.stat().st_size,'sha256':h.hexdigest()}))",
            "finally:",
            " if os.path.exists(t): os.unlink(t)",
        ])
        with source.open('rb') as handle:
            try:
                proc = subprocess.run(self.ssh_argv('python3 -c ' + shlex.quote(code)),
                                      stdin=handle, capture_output=True, timeout=300, env=transport_env())
            except subprocess.TimeoutExpired as exc:
                raise SSHError('transfer_unknown', 'Upload response timed out; inspect destination before retrying') from exc
        if proc.returncode:
            raise SSHError('transfer', proc.stderr.decode(errors='replace'))
        return json.loads(proc.stdout)

    def fetch(self, task_id, destination):
        state = self.status(task_id, logs=False)
        if state['state'] != 'succeeded':
            raise SSHError('artifact_not_ready', 'Fetch requires a succeeded task; current state: ' + state['state'])
        target = Path(destination).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        records = state.get('artifacts', [])
        missing = [r['path'] for r in records if not r['exists']]
        if missing:
            raise SSHError('artifact_missing', 'Expected artifact missing: ' + ', '.join(missing))
        basenames = [Path(r['path']).name for r in records]
        if len(set(basenames)) != len(basenames):
            raise SSHError('artifact_collision', 'Artifacts have duplicate basenames; declare unique output names')
        fetched = []
        for item in records:
            path_encoded = base64.b64encode(item['path'].encode()).decode()
            code = "import base64,sys,shutil; f=open(base64.b64decode('" + path_encoded + "').decode(),'rb'); shutil.copyfileobj(f,sys.stdout.buffer)"
            dest = target / Path(item['path']).name
            tmp = dest.with_name(dest.name + '.ssh4codex-part-' + uuid.uuid4().hex)
            try:
                with tmp.open('wb') as out:
                    proc = subprocess.Popen(self.ssh_argv('python3 -c ' + shlex.quote(code)), stdout=out, stderr=subprocess.PIPE, env=transport_env())
                    try:
                        _, stderr = proc.communicate(timeout=300)
                    except subprocess.TimeoutExpired:
                        proc.kill(); proc.communicate()
                        raise SSHError('transport_timeout', 'Artifact download timed out')
                    if proc.returncode:
                        raise SSHError('transfer', stderr.decode(errors='replace'))
                digest = hashlib.sha256()
                with tmp.open('rb') as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                        digest.update(chunk)
                if digest.hexdigest() != item['sha256'] or tmp.stat().st_size != item['size']:
                    raise SSHError('artifact_changed', 'Artifact changed since task completion: ' + item['path'])
                tmp.replace(dest)
                fetched.append({'path': str(dest.resolve()), 'size': item['size'], 'sha256': item['sha256']})
            finally:
                tmp.unlink(missing_ok=True)
        return {'task_id': task_id, 'files': fetched}
