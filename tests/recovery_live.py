"""Opt-in real-host recovery tests. Configuration and raw reports stay external."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import time
import uuid

from live_support import PRIVATE_ROOT, write_report
from stress import NetworkProxy
from ssh4codex.client import Client, SSHError, load_server, ssh_program, transport_env


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--server', required=True)
    args = parser.parse_args()
    base = load_server(args.server)
    token = uuid.uuid4().hex[:12]
    folder = PRIVATE_ROOT / 'recovery' / token
    folder.mkdir(parents=True, mode=0o700)
    host = base['target'].split('@')[-1]
    proxy = NetworkProxy(host, base.get('port', 22))
    ssh_config = folder / 'ssh_config'
    ssh_config.write_text('Host *\n    HostKeyAlias ' + host + '\n')
    common = {**base, 'session': 's4c-recovery-' + token, 'rpc_timeout': 15, 'transfer_timeout': 90}
    config = folder / 'config.json'
    entries = {
        'direct': {**common, 'control_path': str(Path.home()/'.ssh/ssh4codex'/('rec-'+token))},
        'fault': {**common, 'target': base['target'].split('@')[0]+'@127.0.0.1', 'port': proxy.port,
                  'ssh_config': str(ssh_config), 'control_path': str(Path.home()/'.ssh/ssh4codex'/('rec-fault-'+token))}}
    config.write_text(json.dumps({'servers': entries})); config.chmod(0o600)
    os.environ.update(SSH4CODEX_CONFIG=str(config), SSH4CODEX_STATE=str(folder/'state'))
    report = {'run_id': token, 'scope': 'isolated tmux session, master sockets and TCP bridge', 'phases': {}}
    def phase(name, action):
        started = time.monotonic()
        try:
            detail = action() or {}
            report['phases'][name] = {'passed': True, 'seconds': round(time.monotonic()-started, 3), **detail}
        except Exception as exc:
            report['phases'][name] = {'passed': False, 'seconds': round(time.monotonic()-started, 3),
                                     'error': type(exc).__name__, 'message': str(exc)[:400]}
        print(name, json.dumps(report['phases'][name]), flush=True)
        write_report('recovery-'+token+'.json', report)
    direct, fault = Client('direct'), Client('fault')
    work = '/tmp/ssh4codex-recovery-' + token
    pool = ThreadPoolExecutor(max_workers=8)
    try:
        direct.submit('mkdir -p ' + work, cwd='/tmp', wait_seconds=3)
        direct.doctor(); fault.doctor()
        known = direct.submit('printf known', cwd=work, wait_seconds=3)['task_id']
        payload = folder/'payload.bin'
        payload.write_bytes(os.urandom(50*1024*1024))
        def clean():
            start = time.monotonic()
            future = pool.submit(direct.put, payload, work+'/large.bin')
            latencies = []
            while not future.done():
                before = time.monotonic(); assert direct.status(known)['state'] == 'succeeded'
                latencies.append(time.monotonic()-before)
                time.sleep(.1)
            value = future.result(); elapsed = time.monotonic()-start
            assert value['sha256'] == hashlib.sha256(payload.read_bytes()).hexdigest()
            assert value['phase'] == 'complete'
            return {'bytes': value['size'], 'MiB_per_second': round(50/elapsed, 2), 'status_calls': len(latencies),
                    'status_p50_s': round(statistics.median(latencies), 3), 'status_max_s': round(max(latencies), 3)}
        phase('50MiB_incompressible_with_concurrent_status', clean)
        small = folder/'resume.bin'; small.write_bytes(os.urandom(13*1024*1024))
        def resume():
            proxy.delay = .025
            observed = {}
            original = fault.rpc
            def capture(action, **kwargs):
                result = original(action, **kwargs)
                if action == 'upload_prepare': observed.update(result)
                return result
            fault.rpc = capture
            future = pool.submit(fault.put, small, work+'/resume.bin')
            deadline = time.monotonic()+40
            before_drop = 0
            while time.monotonic() < deadline and not future.done():
                if observed:
                    status = direct.transfer_status(observed['transfer_id'])
                    before_drop = status['bytes_received']
                    if before_drop >= 1024*1024:
                        proxy.drop(); proxy.delay = 0; break
                time.sleep(.2)
            try:
                value = future.result(timeout=100)
            finally:
                fault.rpc = original; proxy.delay = 0
            assert before_drop >= 1024*1024 and value['resumed_from'] > 0
            assert value['sha256'] == hashlib.sha256(small.read_bytes()).hexdigest() and value['phase'] == 'complete'
            # Recreate the client, proving resume identity does not depend on process memory.
            again = Client('fault').put(small, work+'/resume.bin')
            assert again['phase'] == 'complete' and again['attempts'] == 0
            return {'bytes_at_disconnect': before_drop, 'resumed_from': value['resumed_from'], 'attempts': value['attempts'], 'new_client_repeat_streams': again['attempts']}
        phase('13MiB_disconnect_resume_and_new_client', resume)
        def lost_ack():
            source = folder/'ack.txt'; source.write_text('acknowledgement test')
            original = subprocess.run
            dropped = []
            def run(argv, **kwargs):
                value = original(argv, **kwargs)
                if isinstance(argv, list) and ' upload ' in argv[-1] and not dropped:
                    dropped.append(True)
                    return subprocess.CompletedProcess(argv, 0, b'{', b'')
                return value
            subprocess.run = run
            try: value = direct.put(source, work+'/ack.txt')
            finally: subprocess.run = original
            assert dropped and value['phase'] == 'complete'
            return {'atomic_commit_verified_after_lost_ack': True}
        phase('lost_upload_acknowledgement', lost_ack)
        def recover():
            task_id = 'recovery-' + token
            original = direct.call_ssh
            def lose(command, *args, **kwargs):
                value = original(command, *args, **kwargs)
                if kwargs.get('uncertain_task'):
                    raise SSHError('submission_unknown', 'injected lost acknowledgement', task_id)
                return value
            direct.call_ssh = lose
            try:
                try: direct.submit('echo once >> executions.txt', cwd=work, task_id=task_id)
                except SSHError as exc: assert exc.kind == 'submission_unknown'
            finally: direct.call_ssh = original
            value = Client('direct').recover(task_id, retry=True)
            if value['state'] != 'succeeded': value = direct.wait(task_id, 3)
            assert value['state'] == 'succeeded'
            check = direct.submit('wc -l < executions.txt', cwd=work, wait_seconds=3)
            assert check['stdout']['text'].strip() == '1'
            # A request recorded before a connection failure can be explicitly replayed.
            missing = 'unsent-' + token
            def offline(*a, **k): raise SSHError('submission_unknown', 'before delivery', missing)
            direct.call_ssh = offline
            try:
                try: direct.submit('printf recovered', cwd=work, task_id=missing)
                except SSHError: pass
            finally: direct.call_ssh = original
            assert direct.recover(missing)['state'] == 'not_found'
            value = direct.recover(missing, retry=True)
            value = direct.wait(missing, 3)
            assert value['state'] == 'succeeded' and value['stdout']['text'] == 'recovered'
            return {'executions_after_lost_ack': 1, 'saved_unsent_request_recovered': True}
        phase('task_recovery_and_no_duplicate_execution', recover)
        def staging():
            source = folder/'numbers.txt'; source.write_text('1\n2\n3\n')
            task_id = 'stage-' + token
            value = direct.submit('awk "{s+=\\$1} END {print s}" input.txt > result.txt', cwd=work,
                                  inputs={str(source): work+'/input.txt'}, requires=['awk'], artifacts=['result.txt'], task_id=task_id, wait_seconds=3)
            assert value['state'] == 'succeeded', value
            direct.fetch(task_id, folder/'artifacts')
            assert (folder/'artifacts/result.txt').read_text().strip() == '6'
            try: direct.submit('touch should-not-exist', cwd=work, requires=['missing-unit-tool'], task_id='bad-'+token)
            except SSHError as exc: assert exc.kind == 'preflight_failed'
            else: raise AssertionError('Missing tool should block launch')
            assert direct.recover('bad-'+token)['state'] == 'not_found'
            assert direct.doctor(requires=['missing-unit-tool'])['ready'] is False
            return {'staged_input_sum': 6, 'missing_tool_blocked_before_tmux': True}
        phase('verified_inputs_preflight_and_artifact_fetch', staging)
        def concurrent():
            def submit(i):
                c = Client('direct')
                value = c.submit('printf task-'+str(i), cwd=work, task_id='parallel-'+token+'-'+str(i), wait_seconds=3)
                if value['state'] != 'succeeded': value = c.wait(value['task_id'], 5)
                assert value['state'] == 'succeeded'
                return value['task_id']
            ids = list(pool.map(submit, range(24)))
            assert len(set(ids)) == 24
            # Two agents see the same first log chunk and then advance independently.
            task = direct.submit('printf abcdefghij', cwd=work, wait_seconds=3)['task_id']
            assert direct.poll(task, 'agent-a', 4)['stdout']['text'] == 'abcd'
            assert Client('direct').poll(task, 'agent-a', 4)['stdout']['text'] == 'efgh'
            assert Client('direct').poll(task, 'agent-b', 4)['stdout']['text'] == 'abcd'
            return {'parallel_tasks': 24, 'independent_consumers': 2}
        phase('multi_agent_tasks_and_persistent_cursors', concurrent)
    finally:
        pool.shutdown(wait=True)
        # Only this test's session and master sockets; never touch a configured user's session.
        direct.call_ssh('tmux kill-session -t ='+common['session'], timeout=10, fresh=True)
        for client in (direct, fault):
            subprocess.run([ssh_program(), *client.options, '-O', 'exit', client.server['target']], capture_output=True, env=transport_env())
        proxy.close()
    assert all(item['passed'] for item in report['phases'].values()), 'One or more phases failed; see the private report'


if __name__ == '__main__': main()
