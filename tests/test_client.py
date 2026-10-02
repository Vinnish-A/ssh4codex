import json
from pathlib import Path
import subprocess

import pytest
from ssh4codex.client import Client, SSHError, load_server


def test_unconfigured_server_fails_before_transport(tmp_path, monkeypatch):
    monkeypatch.setenv('SSH4CODEX_CONFIG', str(tmp_path / 'missing.json'))
    monkeypatch.setenv('SSH4CODEX_STATE', str(tmp_path / 'state'))
    def unexpected_transport(*args, **kwargs):
        pytest.fail('An unconfigured server must never start SSH')
    monkeypatch.setattr(subprocess, 'run', unexpected_transport)
    with pytest.raises(SSHError) as error:
        Client('unconfigured-server')
    assert error.value.kind == 'configuration'
    assert not (tmp_path / 'state').exists()


@pytest.mark.parametrize('missing', ['target', 'session'])
def test_connection_requires_explicit_target_and_session(tmp_path, monkeypatch, missing):
    profile = {'target': 'user@example.invalid', 'session': 'unit-session'}
    profile.pop(missing)
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'servers': {'example': profile}}))
    monkeypatch.setenv('SSH4CODEX_CONFIG', str(config))
    with pytest.raises(SSHError) as error:
        load_server('example')
    assert error.value.kind == 'configuration' and missing in str(error.value)


def test_external_profile_is_read_without_modification(tmp_path, monkeypatch):
    profile = {'target': 'user@example.invalid', 'session': 'custom-session', 'port': 2201}
    config = tmp_path / 'config.json'
    original = json.dumps({'servers': {'example': profile}})
    config.write_text(original)
    monkeypatch.setenv('SSH4CODEX_CONFIG', str(config))
    assert load_server('example') == profile
    assert config.read_text() == original
    with pytest.raises(SSHError) as error:
        load_server('other-server')
    assert error.value.kind == 'configuration'


@pytest.fixture
def client(tmp_path, monkeypatch):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'servers': {'example': {'target': 'user@example', 'session': 'unit-session'}}}))
    monkeypatch.setenv('SSH4CODEX_CONFIG', str(config))
    monkeypatch.setenv('SSH4CODEX_STATE', str(tmp_path / 'state'))
    return Client('example')


def test_uncertain_submit_returns_recoverable_id(client, monkeypatch):
    def timeout(*args, **kwargs): raise subprocess.TimeoutExpired(args[0], 20)
    monkeypatch.setattr(subprocess, 'run', timeout)
    with pytest.raises(SSHError) as result:
        client.call_ssh('rpc', b'{}', uncertain_task='known-id')
    assert result.value.kind == 'submission_unknown' and result.value.task_id == 'known-id'


def test_auth_failure_not_retried_as_command(client, monkeypatch):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 255, b'', b'Permission denied'))
    with pytest.raises(SSHError) as result: client.call_ssh('rpc', uncertain_task='known-id')
    assert result.value.kind == 'authentication'


def test_read_transport_retry_bypasses_master(client, monkeypatch):
    client.agent_marker.touch()
    calls = []
    def call(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1: raise SSHError('transport_timeout', 'blackhole')
        return subprocess.CompletedProcess(args, 0, b'{"state":"succeeded"}', b'')
    monkeypatch.setattr(client, 'call_ssh', call)
    assert client.rpc('status', task_id='x')['state'] == 'succeeded'
    assert len(calls) == 2 and calls[1]['fresh']
    argv = client.ssh_argv('command', fresh=True)
    assert argv.index('ControlPath=none') < argv.index('ControlPath=' + client.socket)


def test_submission_is_not_automatically_replayed(client, monkeypatch):
    client.agent_marker.touch()
    calls = []
    def fail(*a, **k):
        calls.append(k)
        raise SSHError('submission_unknown', 'lost response', 'stable')
    monkeypatch.setattr(client, 'call_ssh', fail)
    with pytest.raises(SSHError): client.submit('printf once', task_id='stable')
    assert len(calls) == 1


def test_truncated_submission_keeps_recovery_id(client, monkeypatch):
    client.agent_marker.touch()
    monkeypatch.setattr(client, 'call_ssh', lambda *a, **k: subprocess.CompletedProcess(a, 0, b'{"task_id":', b''))
    with pytest.raises(SSHError) as exc: client.submit('printf once', task_id='stable')
    assert exc.value.kind == 'submission_unknown' and exc.value.task_id == 'stable'


def test_read_retry_is_bounded(client, monkeypatch):
    client.agent_marker.touch()
    calls=[]
    def fail(*a, **k):
        calls.append(k)
        raise SSHError('transport_timeout', 'still unavailable')
    monkeypatch.setattr(client,'call_ssh',fail)
    with pytest.raises(SSHError): client.rpc('status',task_id='x')
    assert len(calls)==2 and calls[1]['fresh']


def test_partial_read_reply_with_zero_exit_retries(client, monkeypatch):
    client.agent_marker.touch()
    calls=[]
    def read(*a,**k):
        calls.append(k)
        return subprocess.CompletedProcess(a,0,b'{"state":' if len(calls)==1 else b'{"state":"succeeded"}',b'')
    monkeypatch.setattr(client,'call_ssh',read)
    assert client.rpc('status',task_id='x')['state']=='succeeded'
    assert len(calls)==2 and calls[1]['fresh']


def test_upload_lost_response_is_not_replayed(client, monkeypatch, tmp_path):
    source=tmp_path/'upload';source.write_bytes(b'data')
    calls=[]
    def interrupted(*a, **k):
        calls.append(k)
        return subprocess.CompletedProcess(a,255,b'',b'Connection reset by peer')
    monkeypatch.setattr(subprocess,'run',interrupted)
    with pytest.raises(SSHError) as exc:client.put(source,'/remote/upload')
    assert exc.value.kind=='transfer_unknown' and len(calls)==1


def test_config_not_shell_expanded(client):
    argv = client.ssh_argv('echo hello')
    assert argv[-2:] == ['user@example', 'echo hello']
    assert 'BatchMode=yes' in argv


def test_task_registered_before_transport_failure(client, monkeypatch):
    def fail(*a, **k): raise SSHError('submission_unknown', 'test', 'stable-id')
    monkeypatch.setattr(client, 'rpc', fail)
    with pytest.raises(SSHError): client.submit('echo test', task_id='stable-id')
    assert json.loads((client.local / 'task-stable-id.json').read_text())['task_id'] == 'stable-id'


def test_interactive_connect_remembers_target_after_disconnect(client, monkeypatch):
    from ssh4codex import client as module
    monkeypatch.setattr(module.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(module.sys.stdout, 'isatty', lambda: True)
    calls = []
    def launch(argv, **kwargs):
        calls.append(argv)
        assert 'stdin' not in kwargs and 'capture_output' not in kwargs
        return subprocess.CompletedProcess(argv, 255)
    monkeypatch.setattr(subprocess, 'run', launch)
    assert client.connect() == 255
    assert calls[-1][1] == '-tt' and calls[-1][-1] == 'exec tmux new-session -A -s unit-session -c .'
    assert client.connect('analysis') == 255
    assert Client('example').connect() == 255
    assert calls[-1][-1] == 'exec tmux new-session -A -s analysis -c .'
    assert client.tmux_target.stat().st_mode & 0o777 == 0o600


def test_connect_invalid_target_does_not_overwrite_selection(client, monkeypatch):
    from ssh4codex import client as module
    monkeypatch.setattr(module.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(module.sys.stdout, 'isatty', lambda: True)
    client.tmux_target.write_text('unit-session')
    with pytest.raises(ValueError):
        client.connect('unit-session; touch /tmp/injected')
    assert client.tmux_target.read_text() == 'unit-session'


def test_connect_requires_terminal_before_persisting_target(client, monkeypatch):
    from ssh4codex import client as module
    monkeypatch.setattr(module.sys.stdin, 'isatty', lambda: False)
    with pytest.raises(SSHError) as error:
        client.connect('unit-session')
    assert error.value.kind == 'configuration'
    assert not client.tmux_target.exists()


def test_manual_session_selection_does_not_redirect_automated_tasks(client, monkeypatch):
    client.tmux_target.write_text('analysis')
    monkeypatch.setattr(client, 'rpc', lambda action, **spec: spec)
    assert client.submit('printf test', task_id='configured-session')['session'] == 'unit-session'
