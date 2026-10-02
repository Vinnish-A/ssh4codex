"""Opt-in real SSH stress + isolated TCP faults; never changes host networking."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from live_support import PRIVATE_ROOT, REPORTS, write_report
import random
import statistics
import subprocess
import threading
import time
import uuid

from ssh4codex.client import Client, SSHError, load_server


class NetworkProxy:
    """A loopback TCP bridge for this test's SSH connection only.

    Delay is per forwarded chunk, not an emulation of packet loss or exact RTT.
    All forwarded traffic is still SSH ciphertext.
    """
    def __init__(self, host, port):
        self.host, self.remote_port = host, port
        self.delay = 0
        self.jitter = 0
        self.block = False
        self.connections = set()
        self.forwarded_downstream = 0
        self.ready = threading.Event()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        assert self.ready.wait(10)

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        self.server = self.loop.run_until_complete(asyncio.start_server(self._accept, '127.0.0.1', 0))
        self.port = self.server.sockets[0].getsockname()[1]
        self.ready.set()
        self.loop.run_forever()
        pending = asyncio.all_tasks(self.loop)
        for task in pending: task.cancel()
        self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()

    async def _accept(self, reader, writer):
        upstream = None
        try:
            remote_reader, upstream = await asyncio.open_connection(self.host, self.remote_port)
            self.connections.update((writer, upstream))
            async def forward(source, destination, downstream=False):
                while data := await source.read(65536):
                    while self.block: await asyncio.sleep(.02)
                    if self.delay or self.jitter:
                        await asyncio.sleep(max(0, self.delay + random.uniform(-self.jitter, self.jitter)))
                    destination.write(data)
                    await destination.drain()
                    if downstream:self.forwarded_downstream += len(data)
            tasks = [asyncio.create_task(forward(reader, upstream)),
                     asyncio.create_task(forward(remote_reader, writer, True))]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            for stream in [writer, upstream]:
                if stream:
                    self.connections.discard(stream)
                    stream.close()

    def drop(self):
        def close():
            for stream in list(self.connections): stream.close()
        self.loop.call_soon_threadsafe(close)

    def close(self):
        self.drop()
        self.loop.call_soon_threadsafe(self.server.close)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


def metrics(samples):
    samples = sorted(samples)
    return {'n': len(samples), 'p50_s': round(statistics.median(samples), 4),
            'p95_s': round(samples[min(len(samples)-1, int(len(samples)*.95))], 4),
            'max_s': round(max(samples), 4)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--server', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=48)
    parser.add_argument('--concurrency', type=int, default=8)
    parser.add_argument('--require-pass', action='store_true')
    args = parser.parse_args()
    base = load_server(args.server)
    token = uuid.uuid4().hex[:12]
    folder = PRIVATE_ROOT/'stress' / token
    folder.mkdir(parents=True)
    config = folder/'config.json'
    state = folder/'state'
    ssh_config = folder/'ssh_config'
    host = base['target'].split('@')[-1]
    proxy = NetworkProxy(host, base.get('port', 22))
    ssh_config.write_text('Host *\n    HostKeyAlias '+host+'\n')
    entries = {
        'direct': {**base, 'control_path': str(Path.home()/'.ssh/ssh4codex'/('test-'+token))},
        'fault': {**base, 'target': base['target'].split('@')[0]+'@127.0.0.1',
                  'port': proxy.port, 'ssh_config': str(ssh_config.resolve()),
                  'control_path': str(Path.home()/'.ssh/ssh4codex'/('fault-'+token)),
                  'rpc_timeout': 3, 'connect_timeout': 8},
    }
    config.write_text(json.dumps({'servers': entries})); config.chmod(0o600)
    previous = {name: os.environ.get(name) for name in ['SSH4CODEX_CONFIG','SSH4CODEX_STATE']}
    os.environ.update(SSH4CODEX_CONFIG=str(config.resolve()),SSH4CODEX_STATE=str(state.resolve()))
    report = {'run_id': token, 'jobs': args.jobs, 'concurrency': args.concurrency,
              'fault_scope': 'isolated loopback TCP bridge; no tc/iptables or shared SSH disruption', 'phases': {}}
    def phase(name, action):
        before = time.monotonic()
        try:
            result = action()
            report['phases'][name] = {'passed': True, 'elapsed_s': round(time.monotonic()-before, 3), **(result or {})}
        except Exception as exc:
            report['phases'][name] = {'passed': False, 'elapsed_s': round(time.monotonic()-before, 3),
                                     'error': type(exc).__name__, 'message': str(exc)[:300]}
        print(name, json.dumps(report['phases'][name]), flush=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2))
        args.output.chmod(0o600)

    try:
        direct = Client('direct')
        fault = Client('fault')
        work = '/tmp/ssh4codex-stress-'+token
        direct.submit('mkdir -p '+work, cwd='/tmp', wait_seconds=3)
        direct.rpc('doctor'); fault.rpc('doctor')
        known = direct.submit('printf known', cwd=work, wait_seconds=3)['task_id']
        durations = {}
        def latency():
            for name, delay, jitter in [('clean',0,0),('delay',.1,0),('jitter',.1,.075)]:
                proxy.delay, proxy.jitter = delay, jitter
                measurements = []
                for _ in range(8):
                    start = time.monotonic(); value = fault.status(known)
                    assert value['state']=='succeeded'
                    measurements.append(time.monotonic()-start)
                durations[name] = metrics(measurements)
            proxy.delay = proxy.jitter = 0
            return durations
        phase('latency_profiles',latency)

        ids=[]
        def parallel():
            samples=[]; errors=[]
            def task(i):
                start=time.monotonic()
                try:
                    s=Client('direct').submit('printf "job-'+str(i)+'\\n"; sleep .05',cwd=work,wait_seconds=3)
                    ids.append(s['task_id'])
                    assert s['state']=='succeeded',s
                    samples.append(time.monotonic()-start)
                except Exception as exc: errors.append(type(exc).__name__+': '+str(exc)[:150])
            start=time.monotonic()
            with ThreadPoolExecutor(args.concurrency) as pool: list(pool.map(task,range(args.jobs)))
            result={'latency':metrics(samples) if samples else {}, 'errors':errors,
                    'throughput_jobs_s':round(args.jobs/(time.monotonic()-start),3)}
            assert not errors,result
            return result
        phase('concurrent_unique_tasks',parallel)

        def duplicates():
            tid='duplicate-'+token
            script='printf "once\\n" >> once.txt; sleep .1'
            with ThreadPoolExecutor(args.concurrency) as pool:
                results=list(pool.map(lambda _:Client('direct').submit(script,cwd=work,task_id=tid,wait_seconds=3),range(24)))
            assert sum(not s['reused'] for s in results)==1
            check=direct.submit('wc -l < once.txt',cwd=work,wait_seconds=3)
            assert check['stdout']['text'].strip()=='1'
            return {'requests':24,'execution_count':1}
        phase('concurrent_same_task_id',duplicates)

        def batch_status():
            if not hasattr(direct, 'status_many'):
                return {'available': False}
            selected=ids[:16]
            proxy.delay=.1
            start=time.monotonic()
            sequential=[fault.status(tid) for tid in selected]
            sequential_time=time.monotonic()-start
            start=time.monotonic()
            batched=fault.status_many(selected)['tasks']
            batch_time=time.monotonic()-start
            proxy.delay=0
            assert [s['task_id'] for s in batched]==[s['task_id'] for s in sequential]
            assert all(s['state']=='succeeded' for s in batched)
            return {'tasks':len(selected),'sequential_s':round(sequential_time,3),'batch_s':round(batch_time,3)}
        phase('status_batch_vs_sequential',batch_status)

        def drops():
            for _ in range(5):
                proxy.drop(); time.sleep(.15)
                assert fault.status(known)['state']=='succeeded'
            return {'disconnect_recoveries':5}
        phase('idle_connection_drop_recovery',drops)

        def uncertain():
            tid='uncertain-'+token
            script='printf "once\\n" >> uncertain.txt; sleep 2'
            with ThreadPoolExecutor(1) as pool:
                future=pool.submit(fault.submit,script,work,None,None,None,None,tid,3)
                end=time.monotonic()+5
                while True:
                    try:
                        if direct.status(tid)['state'] in {'running','succeeded'}:break
                    except SSHError: pass
                    assert time.monotonic()<end,'submit did not reach server'
                    time.sleep(.05)
                proxy.drop()
                try: future.result()
                except SSHError as exc: assert exc.kind=='submission_unknown' and exc.task_id==tid
            observed=fault.wait(tid,3)
            assert observed['state']=='succeeded'
            recovered=fault.submit(script,cwd=work,task_id=tid,wait_seconds=3)
            assert recovered['state']=='succeeded' and recovered['reused']
            check=direct.submit('wc -l < uncertain.txt',cwd=work,wait_seconds=3)
            assert check['stdout']['text'].strip()=='1'
            return {'execution_count':1,'same_id_recovered':True}
        phase('drop_after_remote_submission',uncertain)

        def interrupted_read():
            fault.status(known)
            proxy.block=True
            with ThreadPoolExecutor(1) as pool:
                future=pool.submit(fault.status,known)
                time.sleep(.4); proxy.drop(); proxy.block=False
                value=future.result()
            assert value['state']=='succeeded'
            return {'read_recovered':True}
        phase('drop_during_read',interrupted_read)
        proxy.block=False

        def blackout():
            fault.status(known)
            proxy.block=True
            start=time.monotonic()
            try: fault.status(known)
            except SSHError as exc: assert exc.kind in {'transport_timeout','transport'}
            else: raise AssertionError('blackout unexpectedly succeeded')
            finally: proxy.block=False; proxy.drop()
            elapsed=time.monotonic()-start
            assert fault.status(known)['state']=='succeeded'
            return {'bounded_failure_s':round(elapsed,3),'recovered_after_blackout':True}
        phase('blackout_then_restore',blackout)

        def many_files():
            script="python3 - <<'INNER'\nfrom pathlib import Path\nfor i in range(24):Path(f'file-{i}.txt').write_text('test data '+str(i))\nINNER"
            s=direct.submit(script,cwd=work,artifacts=['file-'+str(i)+'.txt' for i in range(24)],wait_seconds=3)
            proxy.delay=.1
            start=time.monotonic(); files=fault.fetch(s['task_id'],folder/'files')['files']
            elapsed=time.monotonic()-start
            proxy.delay=0
            assert len(files)==24
            return {'files':24,'elapsed_s':round(elapsed,3)}
        phase('many_small_artifacts_delayed',many_files)

        def interrupted_fetch():
            s=direct.submit('printf "intact\\n" > interrupted-fetch.txt',cwd=work,
                            artifacts=['interrupted-fetch.txt'],wait_seconds=3)
            fault.status(s['task_id'])
            proxy.block=True
            with ThreadPoolExecutor(1) as pool:
                future=pool.submit(fault.fetch,s['task_id'],folder/'interrupted-fetch')
                time.sleep(.4);proxy.drop();proxy.block=False
                result=future.result()
            assert Path(result['files'][0]['path']).read_text()=='intact\n'
            assert not list((folder/'interrupted-fetch').glob('.ssh4codex-part-*'))
            return {'download_recovered':True,'no_partial_files':True}
        phase('download_connection_drop',interrupted_fetch)

        def binary():
            source=folder/'binary.bin';source.write_bytes(bytes(range(256))*32768)
            proxy.delay=.015;proxy.jitter=.01
            start=time.monotonic();direct.put(source,work+'/binary.bin')
            s=direct.submit('true',cwd=work,artifacts=['binary.bin'],wait_seconds=3)
            downloaded=fault.fetch(s['task_id'],folder/'binary')['files'][0]
            assert Path(downloaded['path']).read_bytes()==source.read_bytes()
            proxy.delay=proxy.jitter=0
            return {'bytes':source.stat().st_size,'elapsed_s':round(time.monotonic()-start,3),
                    'fixture':'repeating bytes 0..255; highly compressible, not representative of PNG/qs/random data',
                    'ssh_compression_requested':'Compression=yes' in direct.options}
        phase('8MiB_roundtrip_jitter',binary)
        report['all_passed']=all(s['passed'] for s in report['phases'].values())
        args.output.write_text(json.dumps(report,indent=2))
        args.output.chmod(0o600)
    finally:
        for name in entries:
            c=Client(name)
            subprocess.run(['ssh',*c.options,'-O','exit',c.server['target']],capture_output=True,timeout=10)
        proxy.close()
        for name,value in previous.items():
            if value is None:os.environ.pop(name,None)
            else:os.environ[name]=value
    if args.require_pass:assert report['all_passed'],report


if __name__=='__main__':main()
