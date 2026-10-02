"""Opt-in coordinator/worker simulation against the installed stdio MCP release.

Run: .venv/bin/python tests/codex_multitask.py --run
No remote Codex process is used: local SDK calls coordinate isolated tmux tasks.
Synthetic inputs, artifacts, and complete tool responses stay under .local/.
"""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import csv
import json
import os
from pathlib import Path
import shlex
import subprocess
import time
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {'succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted'}


def workers():
    jobs = []
    for index in range(6):
        name = f'calculate-{index}'
        script = f"""import json, time
from pathlib import Path
time.sleep(3)
values = [i * {index + 1} for i in range(1, 10001)]
result = {{'worker': {index}, 'n': len(values), 'sum': sum(values),
          'sum_squares': sum(x*x for x in values), 'mean': sum(values)/len(values)}}
Path('{name}.json').write_text(json.dumps(result))
print('calculation complete')
"""
        jobs.append({'name': name, 'script': script, 'artifacts': [name + '.json']})
    for index in range(3):
        name = f'table-{index}'
        script = f"""import csv, time
time.sleep(3)
with open('{name}.csv', 'w', newline='') as handle:
    writer = csv.writer(handle)
    writer.writerow(['sample', 'group', 'signal'])
    writer.writerows((i, {index}, (i * ({index} + 3)) % 101) for i in range(1000))
print('table complete')
"""
        jobs.append({'name': name, 'script': script, 'artifacts': [name + '.csv']})
    for index in range(2):
        name = f'progress-{index}'
        script = f"""import json, time
from pathlib import Path
for i in range(125):
    print('worker={index} tick=' + str(i) + ' ' + ('x' * 100), flush=True)
    time.sleep(.2)
Path('{name}.json').write_text(json.dumps({{'worker': {index}, 'ticks': 125}}))
print('progress complete', flush=True)
"""
        jobs.append({'name': name, 'script': script, 'artifacts': [name + '.json']})
    jobs.extend([
        {'name': 'explicit-failure', 'script': "import sys\nprint('intentional exit 7', file=sys.stderr, flush=True)\nsys.exit(7)"},
        {'name': 'timeout', 'script': "import time\nprint('timeout worker started', flush=True)\ntime.sleep(90)", 'timeout': .8},
        {'name': 'cancel-selected', 'script': "import time\nfor i in range(180):\n    print('cancel candidate tick=' + str(i), flush=True)\n    time.sleep(.5)"},
    ])
    return jobs


async def exercise(args):
    run_id = uuid.uuid4().hex[:12]
    local = ROOT / '.local' / 'workflows' / 'multitask' / run_id
    local.mkdir(parents=True, mode=0o700)
    remote = args.remote_root.rstrip('/') + '/' + run_id
    started = time.perf_counter()
    calls = Counter()
    request_bytes = 0
    response_bytes = 0
    log_bytes = 0
    journal = []
    checks = []
    jobs = workers()
    for job in jobs:
        job['task_id'] = f'multitask-{run_id}-{job["name"]}'
    by_id = {job['task_id']: job for job in jobs}
    env = dict(os.environ, PATH='/nonexistent', SSH4CODEX_STATE=str(local / 'state'))
    parameters = StdioServerParameters(command=str(Path(args.binary).expanduser()), env=env)
    version_binary = Path(args.binary).expanduser().with_name('ssh4codex')
    release = subprocess.run([str(version_binary), '--version'], env=env, text=True,
                             capture_output=True, check=True).stdout.strip().split()[-1]
    with (local / 'mcp-stderr.log').open('w') as errlog:
        async with stdio_client(parameters, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                initialization = await session.initialize()
                checks.append({'check': 'official_sdk_initialize_with_empty_system_path', 'passed': True})

                async def call(name, arguments, *, client=None):
                    nonlocal request_bytes, response_bytes, log_bytes
                    arguments = dict(server=args.server, **arguments)
                    calls[name] += 1
                    request_bytes += len(json.dumps({'name': name, 'arguments': arguments}, ensure_ascii=False).encode())
                    call_start = time.perf_counter()
                    result = await (client or session).call_tool(name, arguments)
                    raw = result.model_dump(mode='json', exclude_none=True)
                    response_bytes += len(json.dumps(raw, ensure_ascii=False).encode())
                    value = result.structuredContent
                    if value is None:
                        value = json.loads(next(item.text for item in result.content if item.type == 'text'))
                    for stream in ('stdout', 'stderr'):
                        log_bytes += len(value.get(stream, {}).get('text', '').encode())
                    journal.append({'tool': name, 'arguments': arguments, 'response': raw,
                                    'seconds': round(time.perf_counter() - call_start, 6)})
                    (local / 'responses.json').write_text(json.dumps(journal, indent=2))
                    assert not result.isError and 'error' not in value, (name, value)
                    return value

                setup = await call('remote_run', {
                    'script': 'mkdir -p ' + shlex.quote(remote), 'cwd': '/tmp',
                    'task_id': f'multitask-{run_id}-setup', 'wait_seconds': 2,
                })
                if setup['state'] not in TERMINAL:
                    setup = await call('remote_wait', {'task_id': setup['task_id'], 'seconds': 10})
                assert setup['state'] == 'succeeded', setup
                assert setup['session'] == 'data', setup
                checks.append({'check': 'all_remote_compute_in_shared_data_tmux_session', 'passed': True})

                async def submit(job):
                    return await call('remote_run', {
                        'script': job['script'], 'cwd': remote, 'interpreter': ['python3', '-u'],
                        'artifacts': job.get('artifacts', []), 'timeout': job.get('timeout'),
                        'task_id': job['task_id'], 'wait_seconds': 0,
                    })

                workload_start = time.perf_counter()
                submitted = await asyncio.gather(*(submit(job) for job in jobs))
                submission_seconds = time.perf_counter() - workload_start
                assert all(item['session'] == 'data' for item in submitted)
                task_ids = list(by_id)
                before = await call('remote_status_many', {'task_ids': task_ids})
                assert all('stdout' not in item and 'stderr' not in item for item in before['tasks'])
                running_before_cancel = [by_id[item['task_id']]['name'] for item in before['tasks']
                                         if item['state'] == 'running' and by_id[item['task_id']]['name'] != 'cancel-selected']
                assert running_before_cancel, 'No unrelated worker running when cancellation was requested'
                cancel_job = next(job for job in jobs if job['name'] == 'cancel-selected')
                await call('remote_cancel', {'task_id': cancel_job['task_id']})
                snapshots = [before]
                deadline = time.perf_counter() + 120
                final = None
                while time.perf_counter() < deadline:
                    snapshot = await call('remote_status_many', {'task_ids': task_ids})
                    snapshots.append(snapshot)
                    assert all('stdout' not in item and 'stderr' not in item for item in snapshot['tasks'])
                    if all(item['state'] in TERMINAL for item in snapshot['tasks']):
                        final = {by_id[item['task_id']]['name']: item for item in snapshot['tasks']}
                        break
                    await asyncio.sleep(.8)
                assert final is not None, 'Workers did not reach terminal states within 120 seconds'
                workload_seconds = time.perf_counter() - workload_start
                assert final['explicit-failure']['state'] == 'failed' and final['explicit-failure']['exit_code'] == 7
                assert final['timeout']['state'] == 'timed_out'
                assert final['cancel-selected']['state'] == 'cancelled'
                success_names = [job['name'] for job in jobs if job.get('artifacts')]
                assert all(final[name]['state'] == 'succeeded' and final[name]['exit_code'] == 0 for name in success_names)
                assert sum(item['state'] == 'cancelled' for item in final.values()) == 1
                checks.extend({'check': name, 'passed': True} for name in [
                    '14_mixed_parallel_workers', 'batch_polling_without_log_payloads',
                    'exit_7_is_failure', 'timeout_is_terminal_failure',
                    'only_selected_worker_cancelled', 'unrelated_workers_complete_after_cancel',
                ])

                # Fetch logs only for progress and diagnostic workers, retaining byte cursors.
                cursor_calls = 0
                for name in ['progress-0', 'progress-1', 'explicit-failure', 'timeout', 'cancel-selected']:
                    cursors = {'stdout': 0, 'stderr': 0}
                    chunks = {'stdout': [], 'stderr': []}
                    while True:
                        status = await call('remote_status', {
                            'task_id': final[name]['task_id'], 'stdout_cursor': cursors['stdout'],
                            'stderr_cursor': cursors['stderr'], 'limit': 4096,
                        })
                        cursor_calls += 1
                        for stream in cursors:
                            output = status[stream]
                            assert len(output['text'].encode()) <= 4096
                            assert output['cursor'] >= cursors[stream]
                            cursors[stream] = output['cursor']
                            chunks[stream].append(output['text'])
                        if not any(status[stream]['remaining'] for stream in cursors):
                            break
                    empty = await call('remote_status', {
                        'task_id': final[name]['task_id'], 'stdout_cursor': cursors['stdout'],
                        'stderr_cursor': cursors['stderr'], 'limit': 4096,
                    })
                    cursor_calls += 1
                    assert empty['stdout']['text'] == empty['stderr']['text'] == ''
                    for stream in chunks:
                        (local / f'{name}-{stream}.log').write_text(''.join(chunks[stream]))
                    if name.startswith('progress-'):
                        lines = ''.join(chunks['stdout']).splitlines()
                        assert len(lines) == 126 and lines[-1] == 'progress complete'
                        for tick, line in enumerate(lines[:-1]):
                            assert line == f'worker={name[-1]} tick={tick} ' + 'x' * 100
                    if name == 'explicit-failure':
                        assert ''.join(chunks['stderr']) == 'intentional exit 7\n'
                checks.append({'check': 'relevant_logs_reassembled_without_gaps_duplicates_or_repeat_payload', 'passed': True})

                fetch_start = time.perf_counter()
                fetch_results = await asyncio.gather(*(call('remote_fetch', {
                    'task_id': final[name]['task_id'], 'destination': str(local / 'artifacts' / name),
                }) for name in success_names))
                fetch_seconds = time.perf_counter() - fetch_start
                artifact_bytes = sum(item['size'] for response in fetch_results for item in response['files'])
                artifact_count = sum(len(response['files']) for response in fetch_results)
                # A new coordinator has an empty tool schema cache. The SDK may send
                # tools/list while SSH downloads are active; SSH must not read MCP stdin.
                cold_start = time.perf_counter()
                original_calls = dict(calls)
                original_request_bytes = request_bytes
                original_response_bytes = response_bytes
                with (local / 'cold-mcp-stderr.log').open('w') as cold_errlog:
                    async with stdio_client(parameters, errlog=cold_errlog) as (cold_read, cold_write):
                        async with ClientSession(cold_read, cold_write) as cold_session:
                            await cold_session.initialize()
                            cold_results = await asyncio.wait_for(asyncio.gather(*(call('remote_fetch', {
                                'task_id': final[name]['task_id'],
                                'destination': str(local / 'cold-artifacts' / name),
                            }, client=cold_session) for name in success_names)), 30)
                cold_seconds = time.perf_counter() - cold_start
                assert sum(len(item['files']) for item in cold_results) == 11
                for first, second in zip(fetch_results, cold_results):
                    assert [(item['size'], item['sha256']) for item in first['files']] == [
                        (item['size'], item['sha256']) for item in second['files']]
                checks.append({'check': 'cold_sdk_tool_cache_11_concurrent_fetches_preserve_mcp_stdio', 'passed': True})
                for index in range(6):
                    name = f'calculate-{index}'
                    result = json.loads((local / 'artifacts' / name / (name + '.json')).read_text())
                    scale = index + 1
                    assert result == {'worker': index, 'n': 10000, 'sum': 50005000 * scale,
                                      'sum_squares': 333383335000 * scale * scale, 'mean': 5000.5 * scale}
                for index in range(3):
                    name = f'table-{index}'
                    with (local / 'artifacts' / name / (name + '.csv')).open() as handle:
                        rows = list(csv.DictReader(handle))
                    assert len(rows) == 1000
                    assert all(row == {'sample': str(i), 'group': str(index), 'signal': str((i * (index + 3)) % 101)}
                               for i, row in enumerate(rows))
                for index in range(2):
                    name = f'progress-{index}'
                    assert json.loads((local / 'artifacts' / name / (name + '.json')).read_text()) == {'worker': index, 'ticks': 125}
                checks.append({'check': '11_success_artifacts_fetched_and_contents_validated', 'passed': True})
                report = {
                    'schema': 1, 'recorded_utc': datetime.now(timezone.utc).isoformat(),
                    'installed_release': release, 'transport': 'official MCP Python SDK / installed stdio binary',
                    'mcp_server_info': initialization.serverInfo.model_dump(mode='json'),
                    'server_process_environment_PATH': '/nonexistent', 'tmux_session': 'data',
                    'synthetic_inputs_only': True, 'remote_native_codex_used': False,
                    'remote_work_directory': remote,
                    'workers': len(jobs), 'outcomes': dict(Counter(item['state'] for item in final.values())),
                    'tasks': [{'task_id': job['task_id'], 'role': job['name'],
                               'expected_state': ('failed' if job['name'] == 'explicit-failure' else
                                                  'timed_out' if job['name'] == 'timeout' else
                                                  'cancelled' if job['name'] == 'cancel-selected' else 'succeeded'),
                               'actual_state': final[job['name']]['state'],
                               'exit_code': final[job['name']].get('exit_code'),
                               'artifacts': final[job['name']].get('artifacts', [])}
                              for job in jobs],
                    'measurements': {
                        'elapsed_seconds': round(time.perf_counter() - started, 3),
                        'submission_seconds': round(submission_seconds, 3),
                        'workload_seconds': round(workload_seconds, 3),
                        'completed_workers_per_second': round(len(jobs) / workload_seconds, 4),
                        'successful_workers_per_second': round(len(success_names) / workload_seconds, 4),
                        'artifact_fetch_seconds': round(fetch_seconds, 3),
                        'artifact_count': artifact_count, 'artifact_bytes': artifact_bytes,
                        'cold_cache_fetch_seconds': round(cold_seconds, 3),
                        'cold_cache_artifact_count': 11,
                        'cold_cache_artifact_bytes': sum(file['size'] for item in cold_results for file in item['files']),
                        'original_workflow_tool_calls': original_calls,
                        'original_workflow_sdk_arguments_json_bytes': original_request_bytes,
                        'original_workflow_sdk_results_json_bytes': original_response_bytes,
                        'cold_cache_tool_calls': {'remote_fetch': 11},
                        'cold_cache_sdk_arguments_json_bytes': request_bytes - original_request_bytes,
                        'cold_cache_sdk_results_json_bytes': response_bytes - original_response_bytes,
                        'tool_calls': dict(calls), 'total_tool_calls': sum(calls.values()),
                        'status_many_tasks_per_call': len(jobs), 'status_many_calls': len(snapshots),
                        'cursor_status_calls': cursor_calls,
                        'sdk_tool_arguments_json_bytes': request_bytes,
                        'sdk_tool_result_json_bytes': response_bytes,
                        'returned_stdout_stderr_text_bytes': log_bytes,
                        'unrelated_running_workers_at_cancel': len(running_before_cancel),
                    },
                    'measurement_notes': [
                        'JSON byte counts use SDK objects and exclude JSON-RPC framing, initialize, and SSH wire bytes.',
                        'Workload elapsed includes concurrent submission, cancellation, and terminal batch polling.',
                        'Artifact bytes are verified downloaded file sizes; tool result bytes include both MCP representations.',
                    ],
                    'checks': checks, 'all_passed': all(check['passed'] for check in checks),
                }
                (local / 'initialization.json').write_text(initialization.model_dump_json(indent=2))
                (local / 'responses.json').write_text(json.dumps(journal, indent=2))
                (local / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
                (ROOT / 'benchmarks' / 'codex_multitask.json').write_text(json.dumps(report, indent=2) + '\n')
                print(json.dumps(report, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Explicitly authorize real remote synthetic tasks')
    parser.add_argument('--server', default='solvinglab')
    parser.add_argument('--binary', default='~/.local/bin/ssh4codex-mcp')
    parser.add_argument('--remote-root', default='/tmp/ssh4codex-codex-b7ffc0b0cfe1/multitask')
    args = parser.parse_args()
    if not args.run:
        parser.error('Opt-in required: pass --run to contact the remote server')
    asyncio.run(exercise(args))


if __name__ == '__main__':
    main()
