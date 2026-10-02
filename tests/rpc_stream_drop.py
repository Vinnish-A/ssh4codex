"""Opt-in real interruption during a large status JSON response."""
from concurrent.futures import ThreadPoolExecutor
import argparse
import hashlib
import json
import os
from pathlib import Path
from live_support import PRIVATE_ROOT, REPORTS, write_report
import subprocess
import time
import uuid

from stress import NetworkProxy
from ssh4codex.client import Client,load_server


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', required=True)
    args=parser.parse_args()
    base=load_server(args.server)
    token=uuid.uuid4().hex[:12]
    local=PRIVATE_ROOT/'stress'/('stream-'+token);local.mkdir(parents=True)
    direct=Client(args.server)
    script="python3 - <<'INNER'\nfrom pathlib import Path\nimport os\nprint(os.urandom(512*1024).hex())\nINNER"
    work='/tmp/ssh4codex-stream-'+token
    direct.submit('mkdir -p '+work,cwd='/tmp',wait_seconds=3)
    result=direct.submit(script,cwd=work,wait_seconds=3)
    assert result['state']=='succeeded'
    expected=direct.status(result['task_id'],limit=1024*1024)['stdout']['text']
    host=base['target'].split('@')[-1]
    proxy=NetworkProxy(host,base.get('port',22))
    cfg=local/'config.json';ssh=local/'ssh_config'
    ssh.write_text('Host *\n    HostKeyAlias '+host+'\n')
    cfg.write_text(json.dumps({'servers':{'stream':{**base,'target':base['target'].split('@')[0]+'@127.0.0.1',
        'port':proxy.port,'ssh_config':str(ssh.resolve()),
        'control_path':str(Path.home()/'.ssh/ssh4codex'/('stream-'+token))}}}));cfg.chmod(0o600)
    previous=os.environ.get('SSH4CODEX_CONFIG')
    os.environ['SSH4CODEX_CONFIG']=str(cfg.resolve())
    client=Client('stream')
    try:
        client.rpc('doctor')
        proxy.delay=.015;proxy.jitter=.005
        start=time.monotonic()
        initial_bytes=proxy.forwarded_downstream
        calls=[]
        original_call=client.call_ssh
        def track(*a,**k):
            calls.append(k)
            return original_call(*a,**k)
        client.call_ssh=track
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(client.status,result['task_id'],0,0,1024*1024)
            while True:
                if proxy.forwarded_downstream-initial_bytes>65536:
                    partial=proxy.forwarded_downstream-initial_bytes
                    break
                if future.done():raise AssertionError('Download completed before injection')
                assert time.monotonic()-start<90,'No body bytes arrived'
                time.sleep(.01)
            proxy.drop();proxy.delay=proxy.jitter=0
            recovered=future.result()
        assert recovered['stdout']['text']==expected
        assert len(calls)==2 and calls[1]['fresh']
        report={'passed':True,'log_bytes':len(expected),'dropped_after_proxy_bytes':partial,
                'rpc_attempts':len(calls),'recovered_full_response':True,
                'elapsed_s':round(time.monotonic()-start,3)}
        write_report("rpc_stream_drop.json",report);print(json.dumps(report),flush=True)
    finally:
        subprocess.run(['ssh',*client.options,'-O','exit',client.server['target']],capture_output=True,timeout=10)
        proxy.close()
        if previous is None:os.environ.pop('SSH4CODEX_CONFIG',None)
        else:os.environ['SSH4CODEX_CONFIG']=previous


if __name__=='__main__':main()
