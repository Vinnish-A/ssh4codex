"""Regression tests for interrupted transfers, durable requests and independent readers."""
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess

import pytest
from ssh4codex import remote
from ssh4codex.client import Client, SSHError


@pytest.fixture
def client(tmp_path, monkeypatch):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'servers': {'example': {'target': 'user@example.invalid', 'session': 'unit-session'}}}))
    monkeypatch.setenv('SSH4CODEX_CONFIG', str(config))
    monkeypatch.setenv('SSH4CODEX_STATE', str(tmp_path / 'state'))
    monkeypatch.setattr(remote, 'ROOT', tmp_path / 'remote')
    return Client('example')


def local_transport(client, monkeypatch, interrupt=None):
    calls = []
    def rpc(action, **request):
        try:
            return remote.dispatch({'action': action, **request})
        except remote.RemoteError as exc:
            raise SSHError(exc.kind, str(exc)) from exc
    def stream(argv, stdin, **kwargs):
        assert 'ControlPath=none' in argv
        _, _, _, transfer_id, offset = shlex.split(argv[-1])
        payload = stdin.read()
        calls.append(int(offset))
        if interrupt == 'partial' and len(calls) == 1:
            with pytest.raises(remote.RemoteError):
                remote.upload_stream(transfer_id, int(offset), io.BytesIO(payload[:3]))
            return subprocess.CompletedProcess(argv, 255, b'', b'')
        value = remote.upload_stream(transfer_id, int(offset), io.BytesIO(payload))
        if interrupt == 'ack' and len(calls) == 1:
            return subprocess.CompletedProcess(argv, 0, b'{', b'')
        return subprocess.CompletedProcess(argv, 0, json.dumps(value).encode(), b'')
    monkeypatch.setattr(client, 'rpc', rpc)
    monkeypatch.setattr(subprocess, 'run', stream)
    return calls


@pytest.mark.parametrize('interrupt,offsets', [('partial', [0, 3]), ('ack', [0]), (None, [0])])
def test_verified_resume_and_lost_ack(client, tmp_path, monkeypatch, interrupt, offsets):
    source = tmp_path / 'source'; source.write_bytes(b'abcdefghijk')
    target = tmp_path / 'target'; target.write_bytes(b'old')
    calls = local_transport(client, monkeypatch, interrupt)
    value = client.put(source, str(target), 0o640)
    assert value['phase'] == 'complete' and calls == offsets
    assert target.read_bytes() == source.read_bytes()
    assert target.stat().st_mode & 0o777 == 0o640
    assert Client('example').endpoint() == client.endpoint()
    assert client.put(source, str(target), 0o640)['phase'] == 'complete'
    assert calls == offsets  # no second stream on repeat


def transfer(tmp_path, content=b'new contents'):
    return {'transfer_id': 'a' * 32, 'destination': str(tmp_path / 'target'), 'size': len(content),
            'sha256': hashlib.sha256(content).hexdigest(), 'mode': 0o600}


def test_corruption_preserves_existing_destination(client, tmp_path):
    spec = transfer(tmp_path)
    target = Path(spec['destination']); target.write_bytes(b'original')
    remote.upload_prepare(spec)
    with pytest.raises(remote.RemoteError) as exc:
        remote.upload_stream(spec['transfer_id'], 0, io.BytesIO(b'x' * spec['size']))
    assert exc.value.kind == 'transfer_integrity' and target.read_bytes() == b'original'


def test_concurrent_destination_edit_is_preserved(client, tmp_path):
    spec = transfer(tmp_path); target = Path(spec['destination'])
    remote.upload_prepare(spec)
    target.write_text('someone else changed it')
    with pytest.raises(remote.RemoteError) as exc:
        remote.upload_stream(spec['transfer_id'], 0, io.BytesIO(b'new contents'))
    assert exc.value.kind == 'transfer_conflict' and target.read_text() == 'someone else changed it'


def test_active_writer_is_busy_and_status_remains_readable(client, tmp_path):
    spec = transfer(tmp_path); remote.upload_prepare(spec)
    with remote.transfer_lock(remote.transfer_dir(spec['transfer_id'])):
        assert remote.upload_status(spec['transfer_id'])['bytes_received'] == 0
        with pytest.raises(remote.RemoteError) as exc: remote.upload_prepare(spec)
        assert exc.value.kind == 'transfer_busy'


def test_damaged_prefix_is_not_appended(client, tmp_path, monkeypatch):
    source = tmp_path / 'source'; source.write_text('contents')
    def prepare(*args, **kwargs):
        return {'phase': 'partial', 'offset': 3, 'prefix_sha256': '0' * 64}
    monkeypatch.setattr(client, 'rpc', prepare)
    with pytest.raises(SSHError) as exc: client.put(source, str(tmp_path / 'target'))
    assert exc.value.kind == 'transfer_integrity' and exc.value.details['transfer_id']


def test_exhausted_upload_has_reusable_identity_and_message(client, tmp_path, monkeypatch):
    source = tmp_path / 'source'; source.write_text('contents')
    monkeypatch.setattr(client, 'rpc', lambda *a, **k: {'phase': 'ready', 'offset': 0, 'prefix_sha256': hashlib.sha256(b'').hexdigest()})
    calls = []
    def fail(*a, **k):
        calls.append(1)
        return subprocess.CompletedProcess(a, 255, b'', b'')
    monkeypatch.setattr(subprocess, 'run', fail)
    with pytest.raises(SSHError) as exc: client.put(source, str(tmp_path / 'target'), retries=1)
    assert len(calls) == 2 and exc.value.kind == 'transfer_unknown'
    assert 'without diagnostics' in str(exc.value) and len(exc.value.details['transfer_id']) == 32


def test_recovery_observes_then_explicitly_replays_original_request(client, monkeypatch):
    calls = []
    def rpc(self, action, **kwargs):
        calls.append((action, kwargs, self.fresh_connection))
        if action == 'submit' and len(calls) == 1:
            raise SSHError('submission_unknown', 'lost', kwargs['task_id'])
        if action == 'status': raise SSHError('task_not_found', 'absent')
        return {'task_id': kwargs['task_id'], 'state': 'queued'}
    monkeypatch.setattr(Client, 'rpc', rpc)
    with pytest.raises(SSHError): client.submit('printf original', task_id='stable', env={'X': '1'})
    saved = client.local / 'task-stable.json'
    assert saved.stat().st_mode & 0o777 == 0o600
    assert client.recover('stable')['state'] == 'not_found'
    assert len([c for c in calls if c[0] == 'submit']) == 1
    assert client.recover('stable', retry=True)['state'] == 'queued'
    assert calls[-1][1] == calls[0][1] and calls[-1][2]


def test_recover_existing_task_never_replays(client, monkeypatch):
    calls = []
    def rpc(self, action, **kwargs):
        calls.append(action)
        return {'task_id': 'stable', 'state': 'succeeded'}
    monkeypatch.setattr(Client, 'rpc', rpc)
    assert client.recover('stable', retry=True)['state'] == 'succeeded'
    assert calls == ['status']


def test_conflicting_saved_request_fails_without_network(client, monkeypatch):
    monkeypatch.setattr(client, 'rpc', lambda *a, **k: {'state': 'queued'})
    client.submit('original', task_id='stable')
    with pytest.raises(SSHError) as exc: client.submit('different', task_id='stable')
    assert exc.value.kind == 'request_conflict'


def test_failed_input_never_submits_or_saves_request(client, monkeypatch, tmp_path):
    source = tmp_path / "source"; source.write_text("input")
    def fail(*a, **k): raise SSHError('transfer_unknown', 'interrupted')
    monkeypatch.setattr(client, 'put', fail)
    with pytest.raises(SSHError): client.submit('bash input', task_id='staged', inputs={str(source): '/tmp/input'})
    assert not (client.local / 'task-staged.json').exists()


def test_remote_preflight_blocks_missing_executable_and_changed_input(client, tmp_path, monkeypatch):
    spec = {'task_id': 'preflight', 'script': 'echo executed', 'cwd': str(tmp_path), 'env': {},
            'interpreter': ['bash'], 'artifacts': [], 'timeout': None, 'session': 'unit-session'}
    def forbidden(*a, **k): pytest.fail('Preflight failure must not launch tmux')
    monkeypatch.setattr(subprocess, 'run', forbidden)
    for extra in ({'requires': ['missing-unit-executable']}, {'inputs': [{'path': str(tmp_path/'missing'), 'size': 1, 'sha256': '0'*64}]}):
        with pytest.raises(remote.RemoteError) as exc: remote.submit({**spec, **extra})
        assert exc.value.kind == 'preflight_failed'
    assert not (remote.job_dir('preflight') / 'state.json').exists()


def test_profiles_and_independent_cursor_consumers(client, tmp_path, monkeypatch):
    client.server['profiles'] = {'project': {'cwd': '/project', 'env': {'A': '1'}, 'interpreter': ['python3'], 'requires': ['rg']}}
    requests = []
    def rpc(action, **kwargs):
        requests.append(kwargs)
        return {'state': 'queued'}
    monkeypatch.setattr(client, 'rpc', rpc)
    client.submit('print(1)', profile='project', env={'B': '2'})
    assert requests[-1]['env'] == {'A': '1', 'B': '2'} and requests[-1]['requires'] == ['rg']
    logfile = tmp_path / 'log'; logfile.write_text('0123456789')
    monkeypatch.setattr(client, 'status', lambda task, out, err, limit: {
        'stdout': remote.bounded_log(logfile, out, limit, False),
        'stderr': {'text': '', 'cursor': err}})
    assert client.poll('task', 'agent-a', 4)['stdout']['text'] == '0123'
    assert client.poll('task', 'agent-a', 4)['stdout']['text'] == '4567'
    assert client.poll('task', 'agent-b', 4)['stdout']['text'] == '0123'
    assert client.poll('task', 'agent-a', 4, reset=True)['stdout']['text'] == '0123'


def test_overrides_do_not_rotate_shared_master_or_tmux_selection(client):
    alternate = Client('example', rpc_timeout=99, transfer_timeout=123, fresh_connection=True)
    assert alternate.socket == client.socket and alternate.tmux_target == client.tmux_target
    assert alternate.server['rpc_timeout'] == 99 and 'rpc_timeout' not in client.server
    assert 'ControlPath=none' in alternate.ssh_argv('true')


def test_conflicting_request_does_not_stage_inputs_first(client, monkeypatch):
    monkeypatch.setattr(client, 'rpc', lambda *a, **k: {'state': 'queued'})
    client.submit('original', task_id='stable')
    def forbidden(*a, **k): pytest.fail('A conflicting request must not mutate remote inputs')
    monkeypatch.setattr(client, 'put', forbidden)
    with pytest.raises(SSHError): client.submit('changed', task_id='stable', inputs={'local': '/tmp/destination'})


def test_changed_input_on_same_id_cannot_overwrite_original(client, tmp_path, monkeypatch):
    source = tmp_path/'source'; source.write_bytes(b'original')
    target = tmp_path/'target'
    monkeypatch.setattr(client, 'rpc', lambda *a, **k: {'state': 'queued'})
    def put(source, destination):
        target.write_bytes(Path(source).read_bytes())
        return {'path': destination, 'size': target.stat().st_size, 'sha256': remote.file_digest(target)}
    monkeypatch.setattr(client, 'put', put)
    client.submit('cat input', task_id='stable-input', inputs={str(source): str(target)})
    source.write_bytes(b'changed')
    with pytest.raises(SSHError) as exc:
        client.submit('cat input', task_id='stable-input', inputs={str(source): str(target)})
    assert exc.value.kind == 'request_conflict' and target.read_bytes() == b'original'
