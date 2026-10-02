"""Opt-in real interruption after artifact bytes have reached local staging."""
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
    script="python3 - <<'INNER'\nfrom pathlib import Path\nimport os\nPath('random.bin').write_bytes(os.urandom(2*1024*1024))\nINNER"
    work='/tmp/ssh4codex-stream-'+token
    direct.submit('mkdir -p '+work,cwd='/tmp',wait_seconds=3)
    result=direct.submit(script,cwd=work,artifacts=['random.bin'],wait_seconds=3)
    assert result['state']=='succeeded'
    original=result['artifacts'][0]
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
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(client.fetch,result['task_id'],local/'downloads')
            while True:
                parts=list((local/'downloads').glob('.ssh4codex-part-*'))
                if parts and parts[0].stat().st_size>0:
                    partial=parts[0].stat().st_size
                    break
                if future.done():raise AssertionError('Download completed before injection')
                assert time.monotonic()-start<90,'No body bytes arrived'
                time.sleep(.01)
            proxy.drop();proxy.delay=proxy.jitter=0
            files=future.result()['files']
        downloaded=Path(files[0]['path'])
        assert downloaded.stat().st_size==original['size']
        assert hashlib.sha256(downloaded.read_bytes()).hexdigest()==original['sha256']
        assert not list((local/'downloads').glob('.ssh4codex-part-*'))
        report={'passed':True,'bytes':original['size'],'entropy':'os.urandom; incompressible',
                'dropped_after_local_bytes':partial,'recovered_sha256':True,
                'elapsed_s':round(time.monotonic()-start,3),'no_partial_files':True}
        write_report("stream_drop.json",report);print(json.dumps(report),flush=True)
    finally:
        subprocess.run(['ssh',*client.options,'-O','exit',client.server['target']],capture_output=True,timeout=10)
        proxy.close()
        if previous is None:os.environ.pop('SSH4CODEX_CONFIG',None)
        else:os.environ['SSH4CODEX_CONFIG']=previous


if __name__=='__main__':main()
