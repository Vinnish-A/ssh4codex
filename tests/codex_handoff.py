"""Opt-in live CLI handoff and cross-process durable submission simulation.

Run: python3 tests/codex_handoff.py --run
Measurements, raw responses and artifacts remain in external private storage.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from live_support import PRIVATE_ROOT, REPORTS, write_report
from ssh4codex.client import load_server
import shlex
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


class Requests:
    def __init__(self, context, role):
        self.context = context
        self.role = role
        self.directory = Path(context['private']) / role
        self.directory.mkdir(parents=True, exist_ok=True)
        self.state = self.directory / 'state'
        self.env = dict(os.environ, SSH4CODEX_CONFIG=context['config'],
                        SSH4CODEX_STATE=str(self.state))
        self.records = []

    def call(self, *arguments, expected_returncode=0):
        started = time.perf_counter()
        result = subprocess.run([self.context['cli'], *arguments], env=self.env,
                                capture_output=True, timeout=90)
        number = len(self.records) + 1
        (self.directory / f'{number:02d}.stdout').write_bytes(result.stdout)
        (self.directory / f'{number:02d}.stderr').write_bytes(result.stderr)
        self.records.append({'operation': arguments[0],
                             'seconds': round(time.perf_counter() - started, 3),
                             'returncode': result.returncode,
                             'stdout_bytes': len(result.stdout),
                             'stderr_bytes': len(result.stderr)})
        value = json.loads(result.stdout)
        assert result.returncode == expected_returncode, value
        return value

    def summary(self):
        return {'role': self.role, 'request_count': len(self.records),
                'output_bytes': sum(r['stdout_bytes'] + r['stderr_bytes']
                                    for r in self.records),
                'requests': self.records}


def submitted(requests, context, name, script, artifacts, wait=0):
    arguments = ['run', context['server'], '--script', str(script),
                 '--cwd', context['remote_work'], '--task-id', name,
                 '--wait', str(wait)]
    for artifact in artifacts:
        arguments += ['--artifact', artifact]
    return requests.call(*arguments)


def verified_fetch(requests, context, task_id, directory, expected):
    result = requests.call('fetch', context['server'], task_id, '--to', str(directory))
    assert len(result['files']) == 1, result
    record = result['files'][0]
    path = Path(record['path'])
    content = path.read_bytes()
    assert hashlib.sha256(content).hexdigest() == record['sha256'], record
    assert json.loads(content) == expected, content
    return {'bytes': len(content), 'sha256': record['sha256']}


def worker(context_path, role):
    context = json.loads(Path(context_path).read_text())
    requests = Requests(context, role)
    prefix = context['prefix']
    server = context['server']
    if role == 'producer':
        response = submitted(requests, context, prefix + '-producer',
                             Path(context['producer_script']), ['numbers.json'])
        assert response['state'] in {'queued', 'running'}, response
        assert response['reused'] is False and response['session'] == context['session'], response
        answer = {'task_id': response['task_id'], 'initial_state': response['state'],
                  'session': response['session'], 'exits_while_remote_active': True}
    elif role == 'consumer':
        assert not requests.state.exists(), 'consumer inherited local task records'
        listing = requests.call('list', server)
        matching = [t for t in listing['tasks'] if t['task_id'] == prefix + '-producer']
        assert len(matching) == 1, listing
        task_id = matching[0]['task_id']
        status = requests.call('status', server, task_id)
        assert status['task_id'] == task_id and status['session'] == context['session'], status
        completed = requests.call('wait', server, task_id, '--seconds', '15')
        assert completed['state'] == 'succeeded' and completed['exit_code'] == 0, completed
        source = verified_fetch(requests, context, task_id, requests.directory / 'downloads',
                                {'values': [2, 4, 6, 8]})
        aggregate = submitted(requests, context, prefix + '-aggregate',
                               Path(context['aggregate_script']), ['aggregate.json'], wait=3)
        if aggregate['state'] in {'queued', 'running'}:
            aggregate = requests.call('wait', server, aggregate['task_id'], '--seconds', '15')
        assert aggregate['state'] == 'succeeded' and aggregate['exit_code'] == 0, aggregate
        output = verified_fetch(requests, context, aggregate['task_id'],
                                requests.directory / 'aggregate', {'count': 4, 'sum': 20})
        answer = {'discovered_task_id': task_id, 'independent_empty_state': True,
                  'aggregate_task_id': aggregate['task_id'], 'session': status['session'],
                  'source_artifact': source, 'aggregate_artifact': output}
    else:
        (requests.directory / 'ready').touch()
        deadline = time.monotonic() + 30
        while not Path(context['barrier']).exists():
            assert time.monotonic() < deadline, 'submission barrier did not open'
            time.sleep(0.01)
        response = submitted(requests, context, prefix + '-durable',
                             Path(context['race_script']), ['counter.json'])
        assert response['state'] in {'queued', 'running', 'succeeded'}, response
        answer = {'task_id': response['task_id'], 'reused': response['reused'],
                  'session': response['session']}
    answer['process_id'] = os.getpid()
    answer['telemetry'] = requests.summary()
    print(json.dumps(answer))


def launch(context_path, role):
    return subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                             '--worker', role, '--context', str(context_path),
                             '--server', json.loads(Path(context_path).read_text())['server']],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def finish(process, private, role):
    stdout, stderr = process.communicate(timeout=90)
    (private / f'{role}.stdout').write_bytes(stdout)
    (private / f'{role}.stderr').write_bytes(stderr)
    assert process.returncode == 0, f'{role} failed: {stderr.decode()}'
    return json.loads(stdout)


def main(args):
    started = time.perf_counter()
    run_id = uuid.uuid4().hex[:12]
    private = PRIVATE_ROOT / 'workflows/handoff' / run_id
    private.mkdir(parents=True, mode=0o700)
    private.chmod(0o700)
    config_path = Path(os.environ.get('SSH4CODEX_CONFIG', '~/.config/ssh4codex/config.json')).expanduser()
    config = private / 'config.json'
    config.write_bytes(config_path.read_bytes())
    config.chmod(0o600)
    context = {'cli': str(Path(args.cli).expanduser()), 'server': args.server,
               'config': str(config), 'private': str(private), 'prefix': 'handoff-' + run_id,
               'session': load_server(args.server)['session'],
               'remote_work': args.remote_root.rstrip('/') + '/' + run_id,
               'barrier': str(private / 'submit-now')}
    scripts = {
        'producer': """python3 - <<'INNER'
import json, time
from pathlib import Path
time.sleep(6)
Path('numbers.json').write_text(json.dumps({'values': [2, 4, 6, 8]}))
print('producer finished')
INNER
""",
        'aggregate': """python3 - <<'INNER'
import json
from pathlib import Path
values = json.loads(Path('numbers.json').read_text())['values']
Path('aggregate.json').write_text(json.dumps({'count': len(values), 'sum': sum(values)}))
print('aggregation finished')
INNER
""",
        'race': """python3 - <<'INNER'
import fcntl, json, time
from pathlib import Path
with open('execution.lock', 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    with open('executions.txt', 'a') as counter:
        counter.write('executed\\n')
    count = len(Path('executions.txt').read_text().splitlines())
    Path('counter.json').write_text(json.dumps({'executions': count}))
time.sleep(2)
print('durable task finished')
INNER
"""}
    for name, script in scripts.items():
        path = private / (name + '.sh')
        path.write_text(script)
        context[name + '_script'] = str(path)
    context_path = private / 'context.json'
    context_path.write_text(json.dumps(context))
    coordinator = Requests(context, 'coordinator')
    version = subprocess.run([context['cli'], '--version'], check=True,
                             capture_output=True, text=True).stdout.strip()
    setup = coordinator.call('run', context['server'], '--command',
                             'mkdir -p ' + shlex.quote(context['remote_work']), '--cwd', '/tmp',
                             '--task-id', context['prefix'] + '-prepare', '--wait', '3')
    assert setup['state'] == 'succeeded' and setup['session'] == context['session'], setup
    producer = finish(launch(context_path, 'producer'), private, 'producer')
    after_exit = coordinator.call('status', context['server'], producer['task_id'])
    assert after_exit['state'] in {'queued', 'running'}, after_exit
    producer['state_after_process_exit'] = after_exit['state']
    print('Producer process exited while remote job remained active.', flush=True)
    consumer = finish(launch(context_path, 'consumer'), private, 'consumer')
    print('Fresh consumer discovered, waited, fetched and aggregated the job.', flush=True)
    roles = ['race-1', 'race-2', 'race-3']
    processes = [launch(context_path, role) for role in roles]
    deadline = time.monotonic() + 30
    while not all((private / role / 'ready').exists() for role in roles):
        assert time.monotonic() < deadline, 'workers did not reach submission barrier'
        time.sleep(0.01)
    race_started = time.perf_counter()
    Path(context['barrier']).touch()
    results = [finish(process, private, role) for process, role in zip(processes, roles)]
    assert len({r['process_id'] for r in results}) == 3, results
    assert sorted(r['reused'] for r in results) == [False, True, True], results
    durable_id = context['prefix'] + '-durable'
    completed = coordinator.call('wait', context['server'], durable_id, '--seconds', '15')
    assert completed['state'] == 'succeeded' and completed['exit_code'] == 0, completed
    counter = verified_fetch(coordinator, context, durable_id, private / 'counter', {'executions': 1})
    # Recount the actual append-only side effect, rather than trusting its receipt alone.
    audit = coordinator.call('run', context['server'], '--command',
                             "python3 -c \"from pathlib import Path; print(len(Path('executions.txt').read_text().splitlines()))\"",
                             '--cwd', context['remote_work'], '--task-id', context['prefix'] + '-audit', '--wait', '3')
    if audit['state'] in {'queued', 'running'}:
        audit = coordinator.call('wait', context['server'], audit['task_id'], '--seconds', '15')
    assert audit['state'] == 'succeeded' and audit['stdout']['text'].strip() == '1', audit
    conflict = coordinator.call('run', context['server'], '--command', 'printf conflict',
                                '--cwd', context['remote_work'], '--task-id', durable_id,
                                '--wait', '0', '--artifact', 'counter.json', expected_returncode=2)
    assert conflict['error'] == 'remote' and 'different request' in conflict['message'], conflict
    print('Three independent submitters: exactly one original, two reused; side effect count 1; conflict rejected.', flush=True)
    report = {'schema_version': 1, 'recorded_at': datetime.now(timezone.utc).isoformat(),
              'cli_version': version, 'server_alias': context['server'], 'session': context['session'],
              'run_id': run_id, 'all_passed': True,
              'independent_local_state_directories': 6,
              'shared_server_configuration': True,
              'primary_task_ids': [producer['task_id'], consumer['aggregate_task_id'], durable_id],
              'remote_work_directory': context['remote_work'],
              'remote_artifact_paths': [context['remote_work'] + '/' + name
                                        for name in ['numbers.json', 'aggregate.json', 'counter.json']],
              'elapsed_seconds': round(time.perf_counter() - started, 3),
              'checks': ['producer_exits_with_active_remote_task',
                         'fresh_process_discovers_remote_task_without_local_ledger',
                         'fresh_process_waits_and_verifies_source_artifact',
                         'dependent_aggregation_verifies_output',
                         'three_process_same_request_exactly_one_original',
                         'append_only_side_effect_count_one', 'different_request_same_id_rejected'],
              'handoff': {'producer': producer, 'consumer': consumer},
              'concurrent_submission': {'workers': results, 'task_id': durable_id,
                                        'reused_flags': [r['reused'] for r in results],
                                        'side_effect_count': 1, 'counter_artifact': counter,
                                        'conflict_returncode': 2, 'conflict_error': conflict['error'],
                                        'seconds': round(time.perf_counter() - race_started, 3)},
              'coordinator': coordinator.summary()}
    telemetry = [producer['telemetry'], consumer['telemetry'], coordinator.summary(),
                 *[r['telemetry'] for r in results]]
    report['cli_request_count'] = sum(t['request_count'] for t in telemetry)
    report['cli_output_bytes'] = sum(t['output_bytes'] for t in telemetry)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.write_text(json.dumps(report, indent=2) + '\n')
    output.chmod(0o600)
    print(json.dumps({'all_passed': True, 'request_count': report['cli_request_count'],
                      'elapsed_seconds': report['elapsed_seconds'], 'report': str(output)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--cli', default='~/.local/bin/ssh4codex')
    parser.add_argument('--server', required=True)
    parser.add_argument('--remote-root', default='/tmp/ssh4codex-workflows/handoff')
    parser.add_argument('--output', default=str(REPORTS / "codex_handoff.json"))
    parser.add_argument('--worker', choices=['producer', 'consumer', 'race-1', 'race-2', 'race-3'])
    parser.add_argument('--context')
    args = parser.parse_args()
    if args.worker:
        worker(args.context, args.worker)
    else:
        if not args.run:
            parser.error('Pass --run to create real remote synthetic tasks')
        main(args)
