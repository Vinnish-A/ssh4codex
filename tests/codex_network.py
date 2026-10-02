"""Opt-in standalone Codex-like analysis, lost acknowledgement and recovery."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from stress import NetworkProxy
from ssh4codex.client import load_server

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', action='store_true', help='Run synthetic tasks on the configured remote server')
    parser.add_argument('--remote-root', default='/tmp')
    parser.add_argument('--output', type=Path, default=Path('benchmarks/codex_network.json'))
    parser.add_argument('--binary', type=Path, default=Path.home()/'.local/bin/ssh4codex')
    args = parser.parse_args()
    if not args.run:
        parser.error('Opt-in required: pass --run to contact the remote server')
    start = time.monotonic()
    token = uuid.uuid4().hex[:12]
    local = ROOT/'.local/workflows/network'/token
    local.mkdir(parents=True)
    base = load_server('solvinglab')
    host = base['target'].split('@')[-1]
    proxy = NetworkProxy(host,base.get('port',22))
    ssh_config = local/'ssh_config'
    ssh_config.write_text('Host *\n    HostKeyAlias '+host+'\n')
    config = local/'config.json'
    config.write_text(json.dumps({'servers':{
        'direct':{**base,'control_path':str(Path.home()/'.ssh/ssh4codex'/('codex-direct-'+token))},
        'network':{**base,'target':base['target'].split('@')[0]+'@127.0.0.1',
                   'port':proxy.port,'ssh_config':str(ssh_config.resolve()),
                   'control_path':str(Path.home()/'.ssh/ssh4codex'/('codex-fault-'+token))}
    }})); config.chmod(0o600)
    env = dict(os.environ,SSH4CODEX_CONFIG=str(config.resolve()),SSH4CODEX_STATE=str((local/'agent-A').resolve()))
    # Separate process after simulated restart; server/identity/master unchanged.
    restarted = dict(env,SSH4CODEX_STATE=str((local/'agent-B').resolve()))
    requests = []
    def cli(*words,environment=None):
        before = time.monotonic()
        result = subprocess.run([str(args.binary),*words],env=environment or env,
                                capture_output=True,text=True,timeout=45)
        value = json.loads(result.stdout)
        requests.append({'command':words[0],'exit_code':result.returncode,
                         'elapsed_s':round(time.monotonic()-before,3),
                         'output_bytes':len(result.stdout.encode())+len(result.stderr.encode())})
        return result.returncode,value
    version=subprocess.run([str(args.binary),'--version'],capture_output=True,text=True,check=True,timeout=10).stdout.strip().split()[-1]
    report = {'scenario':'standalone analysis under lost acknowledgement, reconnect and agent restart',
              'runtime':version,'run_id':token,'shared_session':'data','checks':{}}
    work = args.remote_root.rstrip('/')+'/network-'+token
    try:
        for server in ['direct','network']:
            code,value=cli('doctor',server);assert code==0,value
        code,state=cli('run','direct','--cwd','/tmp','--command','mkdir -p '+work,'--wait','3')
        assert state['state']=='succeeded',state
        script = local/'analysis.py'
        script.write_text('''from pathlib import Path
import json,random,time
with Path('executions.txt').open('a') as f:f.write('one\\n')
random.seed(42)
values=[random.randint(0,1000) for _ in range(50000)]
for i in range(14):
 print(f'analysis step {i+1}/14',flush=True)
 time.sleep(.4)
summary={'n':len(values),'sum':sum(values),'min':min(values),'max':max(values)}
Path('summary.json').write_text(json.dumps(summary))
Path('values.tsv').write_text('value\\n'+'\\n'.join(map(str,values))+'\\n')
print('analysis complete',flush=True)
''')
        tid='codex-network-'+token
        run_args=('run','network','--script',str(script),'--interpreter','python3','--cwd',work,
                  '--artifact','summary.json','--artifact','values.tsv','--artifact','executions.txt',
                  '--task-id',tid,'--wait','10')
        proxy.delay=.025;proxy.jitter=.015
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(cli,*run_args)
            end=time.monotonic()+15
            while True:
                code,value=cli('status','direct',tid)
                if code==0 and value.get('state')=='running':break
                assert time.monotonic()<end,'No running acknowledgement before injection'
                time.sleep(.1)
            proxy.drop()
            code,lost=future.result()
        assert code==2 and lost['error']=='submission_unknown' and lost['task_id']==tid,lost
        report['checks']['lost_ack_preserves_id']=True
        # New agent process has no local task record; remote state is authoritative.
        code,recovered=cli('wait','network',tid,'--seconds','10',environment=restarted)
        assert code==0 and recovered['state']=='succeeded',recovered
        report['checks']['new_agent_recovers_remote_task']=True
        code,reused=cli(*run_args,environment=restarted)
        assert code==0 and reused['state']=='succeeded' and reused['reused'],reused
        code,fetched=cli('fetch','network',tid,'--to',str(local/'downloads'),environment=restarted)
        assert code==0,fetched
        files={Path(f['path']).name:Path(f['path']) for f in fetched['files']}
        for record in fetched['files']:
            assert hashlib.sha256(Path(record['path']).read_bytes()).hexdigest()==record['sha256']
        lines=files['values.tsv'].read_text().splitlines()
        values=list(map(int,lines[1:]))
        summary=json.loads(files['summary.json'].read_text())
        assert summary=={'n':50000,'sum':sum(values),'min':min(values),'max':max(values)}
        assert files['executions.txt'].read_text()=='one\n'
        report['checks']['exactly_one_execution']=True
        report['checks']['independent_analysis_verification']=True
        report['checks']['all_artifacts_sha256']=True
        code,logs=cli('status','network',tid,environment=restarted)
        code,empty=cli('status','network',tid,'--stdout-cursor',str(logs['stdout']['cursor']),
                       '--stderr-cursor',str(logs['stderr']['cursor']),environment=restarted)
        assert code==0 and empty['stdout']['text']=='' and empty['stderr']['text']==''
        assert 'analysis complete' in logs['stdout']['text']
        report['checks']['saved_log_cursor_no_duplicate']=True
        report.update(passed=all(report['checks'].values()),primary_task_ids=[tid],
                      remote_artifact_paths=[a['path'] for a in recovered['artifacts']],
                      elapsed_s=round(time.monotonic()-start,3),requests=len(requests),
                      returned_output_bytes=sum(r['output_bytes'] for r in requests),
                      operation_metrics=requests,files=len(files),analysis_rows=len(values))
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,indent=2))
        print(json.dumps({k:v for k,v in report.items() if k not in {'operation_metrics','remote_artifact_paths'}}))
    finally:
        # Release only these dedicated test masters, not the agents' shared connection.
        from ssh4codex.client import Client
        previous=os.environ.get('SSH4CODEX_CONFIG')
        os.environ['SSH4CODEX_CONFIG']=str(config.resolve())
        for server in ['direct','network']:
            c=Client(server)
            subprocess.run(['ssh',*c.options,'-O','exit',c.server['target']],capture_output=True,timeout=10)
        if previous is None:os.environ.pop('SSH4CODEX_CONFIG',None)
        else:os.environ['SSH4CODEX_CONFIG']=previous
        proxy.close()


if __name__=='__main__':main()
