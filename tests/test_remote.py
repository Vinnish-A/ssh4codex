import json
from pathlib import Path
import subprocess
import time
import uuid

import pytest
from ssh4codex import remote


@pytest.fixture
def spool(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, 'ROOT', tmp_path / 'spool')
    return tmp_path


def request(tmp_path, **kwargs):
    return {'task_id': uuid.uuid4().hex, 'script': 'printf ok', 'cwd': str(tmp_path),
            'env': {}, 'interpreter': ['bash'], 'artifacts': [], 'timeout': None,
            'session': 's4c-test-' + uuid.uuid4().hex[:8], **kwargs}


def test_invalid_task_path():
    for bad in ['../escape', '', '/root', 'x' * 65, 'a\nb']:
        with pytest.raises(ValueError): remote.job_dir(bad)


def test_incremental_logs_and_tail(tmp_path):
    p = tmp_path / 'out'; p.write_bytes(b'0123456789abcdefghij')
    first = remote.bounded_log(p, 0, 7, False)
    assert first == {'text': '0123456', 'cursor': 7, 'remaining': 13, 'skipped': 0}
    second = remote.bounded_log(p, first['cursor'], 7, False)
    assert second['text'] == '789abcd'
    tail = remote.bounded_log(p, 0, 7, True)
    assert tail['text'] == 'defghij' and tail['skipped'] == 13
    with pytest.raises(ValueError): remote.bounded_log(p, 21, 7, False)


def test_utf8_cursor_not_split(tmp_path):
    p = tmp_path / 'out'; p.write_text('A中文BC')
    cursor = 0; text = ''
    while cursor < p.stat().st_size:
        out = remote.bounded_log(p, cursor, 4, False)
        assert out['cursor'] > cursor
        cursor = out['cursor']; text += out['text']
    assert text == 'A中文BC'


def test_retry_same_id_once_and_conflicting_request(spool, monkeypatch):
    calls = []
    def fake_tmux(*args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, '%900\n', '')
    monkeypatch.setattr(remote, 'tmux', fake_tmux)
    spec = request(spool)
    first = remote.submit(spec)
    second = remote.submit(spec)
    assert not first['reused'] and second['reused']
    assert len([c for c in calls if c[0] == 'new-window']) == 1
    with pytest.raises(ValueError): remote.submit({**spec, 'script': 'printf changed'})


def queued(spool, **kwargs):
    spec = request(spool, **kwargs)
    folder = remote.job_dir(spec['task_id']); folder.mkdir(parents=True)
    (folder / 'script').write_text(spec['script'])
    remote.atomic_json(folder / 'request.json', spec)
    remote.atomic_json(folder / 'state.json', {'task_id': spec['task_id'], 'state': 'queued', 'cwd': spec['cwd']})
    return spec, folder


def test_worker_failed_exit_and_separate_logs(spool):
    spec, folder = queued(spool, script='printf out; printf err >&2; exit 7')
    remote.worker(spec['task_id'])
    s = remote.read_state(folder)
    assert s['state'] == 'failed' and s['exit_code'] == 7
    assert (folder / 'stdout.log').read_text() == 'out'
    assert (folder / 'stderr.log').read_text() == 'err'


def test_worker_timeout(spool):
    spec, folder = queued(spool, script='sleep 30', timeout=.1)
    before = time.monotonic(); remote.worker(spec['task_id'])
    assert remote.read_state(folder)['state'] == 'timed_out'
    assert time.monotonic() - before < 3


def test_worker_pre_cancel(spool):
    spec, folder = queued(spool, script='sleep 30')
    (folder / 'cancel').touch()
    remote.worker(spec['task_id'])
    assert remote.read_state(folder)['state'] == 'cancelled'


def test_missing_artifact_distinct_from_script_exit(spool):
    spec, folder = queued(spool, artifacts=['missing.tsv'])
    remote.worker(spec['task_id'])
    s = remote.read_state(folder)
    assert s['state'] == 'succeeded' and s['exit_code'] == 0
    assert s['artifacts'][0]['exists'] is False


def test_artifact_hash(spool):
    spec, folder = queued(spool, script="printf 'result\\n' > 'file with space.tsv'", artifacts=['file with space.tsv'])
    remote.worker(spec['task_id'])
    item = remote.read_state(folder)['artifacts'][0]
    assert item['exists'] and item['size'] == 7 and len(item['sha256']) == 64
