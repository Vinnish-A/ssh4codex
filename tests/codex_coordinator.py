"""Opt-in installed-CLI coordinator joining independent workflow artifacts.

Run after all four workflow reports exist:
    python3 tests/codex_coordinator.py --run
Raw responses, uploaded manifest and fetched summary remain under .local/.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import shlex
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ('analysis', 'handoff', 'network', 'multitask')

REMOTE_JOIN = r'''import csv
from collections import Counter
import hashlib
import json
from pathlib import Path

manifest = json.loads(Path('manifest.json').read_text())
for record in manifest['artifacts']:
    path = Path(record['path'])
    content = path.read_bytes()
    assert len(content) == record['bytes'], str(path) + ': size changed'
    assert hashlib.sha256(content).hexdigest() == record['sha256'], str(path) + ': checksum changed'


def artifact(source, name):
    matches = [a for a in manifest['artifacts'] if a['source'] == source and Path(a['path']).name == name]
    assert len(matches) == 1, (source, name, matches)
    return Path(matches[0]['path'])


with artifact('analysis', 'differential.tsv').open() as handle:
    rows = list(csv.DictReader(handle, delimiter='\t'))
calls = dict(Counter(row['call'] for row in rows))
assert calls == {'up': 20, 'down': 20, 'unchanged': 260}, calls
analysis = {'rows': len(rows), 'calls': calls}

aggregate = json.loads(artifact('handoff', 'aggregate.json').read_text())
counter = json.loads(artifact('handoff', 'counter.json').read_text())
assert aggregate == {'count': 4, 'sum': 20}, aggregate
assert counter == {'executions': 1}, counter
handoff = {'aggregate': aggregate, 'executions': counter['executions']}

calculation = json.loads(artifact('multitask', 'calculate-0.json').read_text())
assert calculation['worker'] == 0 and calculation['n'] == 10000, calculation
assert calculation['sum'] == 50005000, calculation
with artifact('multitask', 'table-0.csv').open() as handle:
    table = list(csv.DictReader(handle))
assert len(table) == 1000, len(table)
for index, row in enumerate(table):
    assert (int(row['sample']), int(row['group']), int(row['signal'])) == (index, 0, index * 3 % 101), row
multitask = {'calculate0_n': calculation['n'], 'calculate0_sum': calculation['sum'],
             'table0_rows': len(table), 'table0_signal_sum': sum(int(row['signal']) for row in table)}

with artifact('network', 'values.tsv').open() as handle:
    values = [int(row['value']) for row in csv.DictReader(handle, delimiter='\t')]
network_summary = json.loads(artifact('network', 'summary.json').read_text())
assert network_summary == {'n': len(values), 'sum': sum(values), 'min': min(values), 'max': max(values)}, network_summary
executions = artifact('network', 'executions.txt').read_text().splitlines()
assert executions == ['one'], executions
network = {'summary': network_summary, 'executions': len(executions)}

summary = {'all_passed': True, 'source_task_count': len(manifest['tasks']),
           'verified_artifacts': len(manifest['artifacts']),
           'report_checksums_verified': sum(a['checksum_source'] == 'report' for a in manifest['artifacts']),
           'completion_checksums_verified': sum(a['checksum_source'] == 'completion_manifest' for a in manifest['artifacts']),
           'analysis': analysis, 'handoff': handoff, 'multitask': multitask, 'network': network}
Path('summary.json').write_text(json.dumps(summary, sort_keys=True))
print('Four workflow artifact groups joined and verified.')
'''


def source_tasks(reports):
    tasks = []
    for source, report in reports.items():
        if source == 'multitask':
            records = [{'task_id': t['task_id'], 'state': t['expected_state'],
                        'exit_code': t['exit_code']} for t in report['tasks']]
        elif source == 'analysis':
            records = [{'task_id': t['task_id'], 'state': t['state'],
                        'exit_code': t['exit_code']} for t in report['tasks']]
        else:
            records = [{'task_id': task_id, 'state': 'succeeded', 'exit_code': 0}
                       for task_id in report['primary_task_ids']]
        tasks.extend(dict(source=source, **record) for record in records)
    assert len({t['task_id'] for t in tasks}) == len(tasks), 'duplicate source task identity'
    assert 1 <= len(tasks) <= 64, 'status-many task limit exceeded'
    return tasks


def source_artifacts(reports, statuses):
    completion = {a['path']: a for status in statuses for a in status.get('artifacts', [])}
    records = []
    for source, report in reports.items():
        if source == 'analysis':
            artifacts = [{'path': a['remote_path'], 'bytes': a['bytes'], 'sha256': a['sha256']}
                         for a in report['artifacts']]
        elif source == 'multitask':
            artifacts = [{'path': a['path'], 'bytes': a['size'], 'sha256': a['sha256']}
                         for task in report['tasks'] for a in task['artifacts']]
        elif source == 'handoff':
            consumer = report['handoff']['consumer']
            checksums = {'numbers.json': consumer['source_artifact'],
                         'aggregate.json': consumer['aggregate_artifact'],
                         'counter.json': report['concurrent_submission']['counter_artifact']}
            artifacts = [dict(path=path, **checksums[Path(path).name])
                         for path in report['remote_artifact_paths']]
        else:
            artifacts = [{'path': path} for path in report['remote_artifact_paths']]
        for artifact in artifacts:
            path = artifact['path']
            assert Path(path).is_absolute(), path
            completed = completion[path]
            assert completed['exists'], completed
            recorded = 'sha256' in artifact
            records.append({'source': source, 'path': path,
                            'bytes': artifact['bytes'] if recorded else completed['size'],
                            'sha256': artifact['sha256'] if recorded else completed['sha256'],
                            'checksum_source': 'report' if recorded else 'completion_manifest'})
    assert len({r['path'] for r in records}) == len(records), 'duplicate artifact path'
    return records


def expected_summary(manifest):
    generator = random.Random(42)
    network_values = [generator.randint(0, 1000) for _ in range(50000)]
    checksum_sources = Counter(a['checksum_source'] for a in manifest['artifacts'])
    return {'all_passed': True, 'source_task_count': len(manifest['tasks']),
            'verified_artifacts': len(manifest['artifacts']),
            'report_checksums_verified': checksum_sources['report'],
            'completion_checksums_verified': checksum_sources['completion_manifest'],
            'analysis': {'rows': 300, 'calls': {'up': 20, 'down': 20, 'unchanged': 260}},
            'handoff': {'aggregate': {'count': 4, 'sum': 20}, 'executions': 1},
            'multitask': {'calculate0_n': 10000, 'calculate0_sum': sum(range(1, 10001)),
                          'table0_rows': 1000, 'table0_signal_sum': sum(i * 3 % 101 for i in range(1000))},
            'network': {'summary': {'n': len(network_values), 'sum': sum(network_values),
                                    'min': min(network_values), 'max': max(network_values)},
                        'executions': 1}}


def exercise(args):
    # Read every prerequisite before creating a run directory or contacting SSH.
    reports = {source: json.loads((Path(args.reports) / f'codex_{source}.json').read_text())
               for source in SOURCES}
    assert all(reports[s].get('all_passed', reports[s].get('passed', False)) for s in SOURCES)
    tasks = source_tasks(reports)
    started = time.perf_counter()
    run_id = uuid.uuid4().hex[:12]
    private = ROOT / '.local/workflows/coordinator' / run_id
    private.mkdir(parents=True, mode=0o700)
    private.chmod(0o700)
    env = dict(os.environ, SSH4CODEX_STATE=str(private / 'state'))
    cli = str(Path(args.cli).expanduser())
    requests = []

    def request(*arguments):
        request_started = time.perf_counter()
        result = subprocess.run([cli, *arguments], env=env, capture_output=True, timeout=90)
        number = len(requests) + 1
        (private / f'{number:02d}.stdout').write_bytes(result.stdout)
        (private / f'{number:02d}.stderr').write_bytes(result.stderr)
        requests.append({'operation': arguments[0], 'returncode': result.returncode,
                         'seconds': round(time.perf_counter() - request_started, 3),
                         'stdout_bytes': len(result.stdout), 'stderr_bytes': len(result.stderr)})
        if arguments[0] == '--version':
            assert result.returncode == 0, result.stderr.decode()
            return result.stdout.decode().strip()
        value = json.loads(result.stdout)
        assert result.returncode == 0, value
        return value

    version = request('--version')
    statuses = request('status-many', args.server, *[t['task_id'] for t in tasks])['tasks']
    assert len(statuses) == len(tasks), statuses
    by_id = {s['task_id']: s for s in statuses}
    for task in tasks:
        status = by_id[task['task_id']]
        assert status['state'] == task['state'], status
        assert status['exit_code'] == task['exit_code'], status
        assert status['session'] == 'data', status
        assert 'stdout' not in status and 'stderr' not in status, status
    manifest = {'tasks': tasks, 'artifacts': source_artifacts(reports, statuses)}
    manifest_path = private / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest, separators=(',', ':')))
    remote_work = args.remote_root.rstrip('/') + '/' + run_id
    prepare_id = 'coordinator-' + run_id + '-prepare'
    prepare = request('run', args.server, '--command', 'mkdir -p ' + shlex.quote(remote_work),
                      '--cwd', '/tmp', '--task-id', prepare_id, '--wait', '3')
    if prepare['state'] in {'queued', 'running'}:
        prepare = request('wait', args.server, prepare_id, '--seconds', '15')
    assert prepare['state'] == 'succeeded' and prepare['session'] == 'data', prepare
    upload = request('put', args.server, str(manifest_path), remote_work + '/manifest.json')
    assert upload['sha256'] == hashlib.sha256(manifest_path.read_bytes()).hexdigest(), upload
    script = private / 'join.py'
    script.write_text(REMOTE_JOIN)
    task_id = 'coordinator-' + run_id + '-join'
    joined = request('run', args.server, '--script', str(script), '--interpreter', 'python3',
                     '--cwd', remote_work, '--task-id', task_id,
                     '--artifact', 'summary.json', '--wait', '3')
    if joined['state'] in {'queued', 'running'}:
        joined = request('wait', args.server, task_id, '--seconds', '15')
    assert joined['state'] == 'succeeded' and joined['exit_code'] == 0, joined
    assert joined['session'] == 'data', joined
    fetched = request('fetch', args.server, task_id, '--to', str(private / 'downloads'))
    assert len(fetched['files']) == 1, fetched
    artifact = fetched['files'][0]
    content = Path(artifact['path']).read_bytes()
    assert len(content) == artifact['size'], artifact
    assert hashlib.sha256(content).hexdigest() == artifact['sha256'], artifact
    summary = json.loads(content)
    assert summary == expected_summary(manifest), summary
    report = {'schema': 1, 'recorded_utc': datetime.now(timezone.utc).isoformat(),
              'scenario': 'independent coordinator verifies and joins four remote workflow artifact groups',
              'cli_version': version, 'server_alias': args.server, 'tmux_session': 'data',
              'run_id': run_id, 'all_passed': True,
              'elapsed_seconds': round(time.perf_counter() - started, 3),
              'subprocess_requests': len(requests),
              'returned_output_bytes': sum(r['stdout_bytes'] + r['stderr_bytes'] for r in requests),
              'requests': requests, 'verified_source_tasks': tasks,
              'source_outcomes': dict(Counter(t['state'] for t in tasks)),
              'manifest_artifact_count': len(manifest['artifacts']),
              'uploaded_manifest_bytes': manifest_path.stat().st_size,
              'uploaded_manifest_sha256': upload['sha256'],
              'primary_task_ids': [task_id],
              'remote_artifact_paths': [remote_work + '/summary.json'],
              'summary_artifact': {'bytes': len(content), 'sha256': artifact['sha256']},
              'checks': {'batch_source_states_and_shared_session': True,
                         'remote_source_checksums': True, 'remote_join_contents': True,
                         'verified_summary_fetch': True, 'independent_local_summary': True},
              'joined_summary': summary}
    Path(args.output).write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'all_passed': True, 'source_tasks': len(tasks),
                      'artifacts': len(manifest['artifacts']), 'request_count': len(requests),
                      'elapsed_seconds': report['elapsed_seconds'], 'task_id': task_id}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Run real remote synthetic coordination')
    parser.add_argument('--cli', default='~/.local/bin/ssh4codex')
    parser.add_argument('--server', default='solvinglab')
    parser.add_argument('--reports', default=str(ROOT / 'benchmarks'))
    parser.add_argument('--remote-root', default='/tmp/ssh4codex-codex-b7ffc0b0cfe1/coordinator')
    parser.add_argument('--output', default=str(ROOT / 'benchmarks/codex_coordinator.json'))
    args = parser.parse_args()
    if not args.run:
        parser.error('Opt-in required: pass --run after all source reports exist')
    exercise(args)


if __name__ == '__main__':
    main()
