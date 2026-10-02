import json
from pathlib import Path
import subprocess

import pytest
from ssh4codex.client import Client, SSHError


@pytest.fixture
def client(tmp_path, monkeypatch):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'servers': {'example': {'target': 'user@example', 'session': 'data'}}}))
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


def test_fetch_running_blocked(client, monkeypatch, tmp_path):
    monkeypatch.setattr(client, 'status', lambda *a, **k: {'state': 'running'})
    with pytest.raises(SSHError) as result: client.fetch('x', tmp_path / 'out')
    assert result.value.kind == 'artifact_not_ready'


def test_config_not_shell_expanded(client):
    argv = client.ssh_argv('echo hello')
    assert argv[-2:] == ['user@example', 'echo hello']
    assert 'BatchMode=yes' in argv


def test_task_registered_before_transport_failure(client, monkeypatch):
    def fail(*a, **k): raise SSHError('submission_unknown', 'test', 'stable-id')
    monkeypatch.setattr(client, 'rpc', fail)
    with pytest.raises(SSHError): client.submit('echo test', task_id='stable-id')
    assert json.loads((client.local / 'task-stable-id.json').read_text())['task_id'] == 'stable-id'
