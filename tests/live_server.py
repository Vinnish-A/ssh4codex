"""Explicit opt-in real-server acceptance test, never collected by pytest."""
from concurrent.futures import ThreadPoolExecutor
import argparse
import hashlib
import json
from pathlib import Path
from live_support import PRIVATE_ROOT, REPORTS, write_report
import shlex
import subprocess
import time
import uuid

from ssh4codex.client import Client, SSHError

ROOT = Path(__file__).resolve().parents[1]
(PRIVATE_ROOT/'acceptance').mkdir(parents=True,exist_ok=True)
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--server', required=True)
parser.add_argument('--rscript', required=True)
args=parser.parse_args()
run_id = uuid.uuid4().hex[:12]
work = '/tmp/ssh4codex-tests/' + run_id
client = Client(args.server)
checks = []


def check(name, action):
    started = time.perf_counter()
    action()
    checks.append({'check': name, 'passed': True, 'seconds': round(time.perf_counter()-started, 3)})
    print(name, 'PASS', flush=True)


def run(script, **kwargs):
    return client.submit(script, cwd=work, wait_seconds=3, **kwargs)


client.submit('mkdir -p ' + shlex.quote(work), wait_seconds=3)
assert client.rpc('doctor')['tmux_exit_code'] == 0
checks.append({'check': 'doctor_python_tmux', 'passed': True})


def short():
    s=run('printf "ok\\n"; printf "err\\n" >&2')
    assert s['state']=='succeeded' and s['stdout']['text']=='ok\n' and s['stderr']['text']=='err\n'
check('short_job_separate_streams', short)


def failure():
    s=run('printf "real failure\\n" >&2; exit 7')
    assert s['state']=='failed' and s['exit_code']==7
check('exit_7_is_failed', failure)


def quotes():
    script="python3 - <<'INNER'\nfrom pathlib import Path\np=Path(\"中文 ' $() space.txt\")\np.write_text('A中文\\n')\nprint(p.name)\nINNER"
    s=run(script,artifacts=["中文 ' $() space.txt"])
    assert s['state']=='succeeded' and s['artifacts'][0]['exists']
    result=client.fetch(s['task_id'],PRIVATE_ROOT/'acceptance'/'downloads')
    assert Path(result['files'][0]['path']).read_text()=='A中文\n'
check('literal_quotes_unicode_binary_fetch', quotes)


def idempotence():
    tid=uuid.uuid4().hex
    script='printf "once\\n" >> count.txt; sleep .2'
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda _: Client(args.server).submit(script,cwd=work,task_id=tid,wait_seconds=2),range(2)))
    assert sorted(s['reused'] for s in responses)==[False, True]
    s=run("wc -l < count.txt")
    assert s['stdout']['text'].strip()=='1'
    try: client.submit('echo conflict',cwd=work,task_id=tid)
    except SSHError as exc: assert exc.kind=='remote'
    else: raise AssertionError('conflict not detected')
check('concurrent_retry_runs_once_and_conflict_rejected', idempotence)


def cursors():
    s=run("python3 - <<'INNER'\nprint('中文-' * 3000)\nINNER")
    tid=s['task_id']; cursor=0; chunks=[]
    while True:
        status=client.status(tid,stdout_cursor=cursor,limit=997)
        out=status['stdout']; assert len(out['text'].encode())<=997
        chunks.append(out['text']); cursor=out['cursor']
        if not out['remaining']:break
    assert ''.join(chunks)=='中文-'*3000+'\n'
    empty=client.status(tid,stdout_cursor=cursor)
    assert empty['stdout']['text']==''
check('incremental_utf8_logs_no_duplicates_or_loss', cursors)


def ready():
    s=client.submit('sleep 2; printf finished > later.tsv',cwd=work,artifacts=['later.tsv'])
    try: client.fetch(s['task_id'],PRIVATE_ROOT/'acceptance'/'downloads')
    except SSHError as exc: assert exc.kind=='artifact_not_ready'
    else: raise AssertionError('early fetch accepted')
    assert client.wait(s['task_id'],5)['state']=='succeeded'
    assert client.fetch(s['task_id'],PRIVATE_ROOT/'acceptance'/'downloads')['files']
check('early_fetch_blocked_then_verified', ready)


def changed():
    s=run('printf first > mutable.tsv',artifacts=['mutable.tsv'])
    run('printf second > mutable.tsv')
    try: client.fetch(s['task_id'],PRIVATE_ROOT/'acceptance'/'downloads')
    except SSHError as exc: assert exc.kind=='artifact_changed'
    else: raise AssertionError('changed artifact accepted')
check('changed_artifact_checksum_rejected', changed)


def missing():
    s=run('true',artifacts=['does-not-exist.tsv'])
    try: client.fetch(s['task_id'],PRIVATE_ROOT/'acceptance'/'downloads')
    except SSHError as exc: assert exc.kind=='artifact_missing'
    else: raise AssertionError('missing artifact accepted')
check('missing_artifact_reported', missing)


def timeout():
    s=run('sleep 30',timeout=.2)
    assert s['state']=='timed_out'
check('timeout_terminates_group', timeout)


def cancel():
    s=client.submit('sleep 30',cwd=work)
    client.cancel(s['task_id'])
    assert client.wait(s['task_id'],5)['state']=='cancelled'
check('cancel_only_selected_task', cancel)


def reconnect():
    s=client.submit('sleep 3; printf recovered > recovered.tsv',cwd=work,artifacts=['recovered.tsv'])
    subprocess.run(['ssh',*client.options,'-O','exit',client.server['target']],check=True,capture_output=True)
    recovered=Client(args.server).wait(s['task_id'],8)
    assert recovered['state']=='succeeded'
    proc=subprocess.run(['python3','-m','ssh4codex','status',args.server,s['task_id']],cwd=ROOT,text=True,capture_output=True,check=True)
    assert json.loads(proc.stdout)['state']=='succeeded'
check('transport_disconnect_and_fresh_client_recovery', reconnect)


def upload():
    p=PRIVATE_ROOT/'acceptance'/'upload.bin';p.write_bytes(bytes(range(256))*2048)
    result=client.put(p,work+"/uploaded ' $() space.bin")
    assert result['sha256']==hashlib.sha256(p.read_bytes()).hexdigest()
    s=run('true',artifacts=["uploaded ' $() space.bin"])
    fetched=client.fetch(s['task_id'],PRIVATE_ROOT/'acceptance'/'downloads')
    assert Path(fetched['files'][0]['path']).read_bytes()==p.read_bytes()
check('512KiB_binary_upload_and_roundtrip', upload)


def r_environment():
    script="""library(ggplot2)
library(qs)
d <- data.frame(x=1:10,y=(1:10)^2)
qsave(d,'tidy_data.qs')
ggsave('tidy_plot.png',ggplot(d,aes(x,y))+geom_point()+theme_classic(),width=4.5,height=4.5,dpi=150)
cat('R tidy ggplot2 qs OK\\n')
"""
    s=run(script,interpreter=[args.rscript],artifacts=['tidy_data.qs','tidy_plot.png'])
    if s['state']=='running':s=client.wait(s['task_id'],15)
    assert s['state']=='succeeded',s
    assert len(client.fetch(s['task_id'],PRIVATE_ROOT/'acceptance'/'downloads')['files'])==2
check('real_tidy_R_ggplot2_qs_and_PNG_fetch', r_environment)

report={'server':args.server,'tmux_session':client.server['session'],'remote_test_directory':work,'checks':checks,'all_passed':all(c['passed'] for c in checks)}
write_report("live_acceptance.json",report)
print('ALL',len(checks),'PASSED',flush=True)
