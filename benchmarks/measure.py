"""Measured comparisons on one server. No extrapolation to total Codex tokens."""
import argparse
import json
import sys
from pathlib import Path
import shlex
import statistics
import subprocess
import time
import uuid

import tiktoken
from ssh4codex.client import Client

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'tests'))
from live_support import write_report
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--server',required=True)
args=parser.parse_args()
c=Client(args.server)
encoding=tiktoken.get_encoding('o200k_base')

def timed(action):
 start=time.perf_counter();out=action();return round(time.perf_counter()-start,4),out

def ssh(command,cold=False):
 argv=c.ssh_argv(command,fresh=cold)
 return subprocess.run(argv,capture_output=True,text=True,check=True).stdout

latency={}
for label,action in [
 ('bare_ssh_warm',lambda:ssh("printf 'ok\\n'")),
 ('bare_ssh_cold',lambda:ssh("printf 'ok\\n'",True)),
 ('ssh4codex_durable_task',lambda:c.submit("printf 'ok\\n'",wait_seconds=2)),
]:
 measurements=[timed(action)[0] for _ in range(5)]
 latency[label]={'seconds':measurements,'median_seconds':statistics.median(measurements)}

# Repeated observation of the same completed job: terminal history versus log cursors.
script="python3 -c "+shlex.quote("for i in range(300): print(f'analysis progress {i:03d}: processed batch successfully')")
s=c.submit(script,wait_seconds=3)
tid=s['task_id']
response1=c.status(tid,limit=2048,tail=True)
response2=c.status(tid,stdout_cursor=response1['stdout']['cursor'],stderr_cursor=response1['stderr']['cursor'],limit=2048)
response3=c.status(tid,stdout_cursor=response2['stdout']['cursor'],stderr_cursor=response2['stderr']['cursor'],limit=2048)
json_outputs=[json.dumps(x,separators=(',',':')) for x in [response1,response2,response3]]
pane=ssh(shlex.join(["tmux","new-window","-d","-P","-F","#{pane_id}","-t","="+c.server["session"]+":","-n","s4c-benchmark","bash"])).strip()
try:
 cmd=script+"; printf '__BENCH_DONE__\\n'"
 ssh('tmux send-keys -t '+shlex.quote(pane)+' -l '+shlex.quote(cmd)+'; tmux send-keys -t '+shlex.quote(pane)+' Enter')
 captures=[]
 for _ in range(3):
  captures.append(ssh('tmux capture-pane -pt '+shlex.quote(pane)+' -S -100'))
 assert '__BENCH_DONE__' in captures[-1]
finally:
 ssh('tmux kill-pane -t '+shlex.quote(pane))

baseline_tokens=sum(len(encoding.encode(x)) for x in captures)
agent_tokens=sum(len(encoding.encode(x)) for x in json_outputs)
report={
 'server':args.server,'transport':'OpenSSH with RSA authentication','latency':latency,
 'output_comparison':{'scenario':'Three observations after a 300-line generated log; last 100 tmux history lines vs 2048-byte tail then two cursor reads',
 'tokenizer':'tiktoken o200k_base (proxy, not verified Codex tokenizer)',
 'tmux_capture_tokens':baseline_tokens,'ssh4codex_json_tokens':agent_tokens,
 'output_token_reduction_fraction':round(1-agent_tokens/baseline_tokens,4),
 'initial_tail_skipped_bytes':response1['stdout']['skipped'],
 'full_log_preserved_remotely':True},
 'limitations':['Bare warm SSH is a lighter contract and can be faster than durable task execution.',
 'Token comparison measures these returned outputs only; excludes tool schemas, prompts and full conversation.',
 'Bounded tail omits older log bytes by default and reports skipped; use cursor=0 to read all.',
 'Same LAN/WAN/server conditions at one time; no claim about arbitrary hosts or R compute time.']}
write_report("measurement.json",report)
write_report("output_samples.json",{'tmux':captures,'ssh4codex':json_outputs})
print(json.dumps(report,indent=2))
