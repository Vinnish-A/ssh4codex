import json
import pytest
from ssh4codex import cli
from ssh4codex.client import SSHError


def test_delivery_failure_retains_successful_task_and_exits_two(monkeypatch, capsys):
    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def wait(self, *args): return {'task_id': 'task', 'state': 'succeeded'}
        def fetch(self, *args): raise SSHError('transport', 'delivery unavailable')
    monkeypatch.setattr(cli, 'Client', FakeClient)
    with pytest.raises(SystemExit) as exc:
        cli.main(['wait', 'example', 'task', '--fetch-to', '/unused'])
    assert exc.value.code == 2
    value = json.loads(capsys.readouterr().out)
    assert value['state'] == 'succeeded' and value['delivery']['error'] == 'transport'


def test_running_task_does_not_fetch_early(monkeypatch, capsys):
    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def wait(self, *args): return {'task_id': 'task', 'state': 'running'}
        def fetch(self, *args): pytest.fail('A running task must not be fetched')
    monkeypatch.setattr(cli, 'Client', FakeClient)
    cli.main(['wait', 'example', 'task', '--fetch-to', '/unused'])
    assert json.loads(capsys.readouterr().out)['state'] == 'running'


def test_upload_failure_exposes_recoverable_transfer_identity(monkeypatch, capsys):
    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def put(self, *args): raise SSHError('transfer_unknown', 'interrupted', transfer_id='a'*32, attempts=3)
    monkeypatch.setattr(cli, 'Client', FakeClient)
    with pytest.raises(SystemExit) as exc: cli.main(['put', 'example', 'source', '/destination'])
    result = json.loads(capsys.readouterr().out)
    assert exc.value.code == 2 and result['transfer_id'] == 'a'*32 and result['attempts'] == 3
