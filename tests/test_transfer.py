"""Artifact framing, integrity, safe staging and local task registration races."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
from types import SimpleNamespace

import pytest
from ssh4codex.client import Client, SSHError
from ssh4codex import remote


@pytest.fixture
def client(tmp_path, monkeypatch):
    config=tmp_path/'config.json'
    config.write_text(json.dumps({'servers':{'one':{'target':'user@example.invalid','session':'unit-session'},'two':{'target':'user@example.invalid','session':'unit-session'}}}))
    monkeypatch.setenv('SSH4CODEX_CONFIG',str(config))
    monkeypatch.setenv('SSH4CODEX_STATE',str(tmp_path/'state'))
    c=Client('one');c.agent_marker.touch()
    return c


def wire(items):
    out=io.BytesIO()
    with tarfile.open(fileobj=out,mode='w') as stream:
        for name,payload in items:
            header=tarfile.TarInfo(name);header.size=len(payload)
            stream.addfile(header,io.BytesIO(payload))
    return out.getvalue()


def fake_process(monkeypatch,data,returncode=0):
    class Process:
        stdout=io.BytesIO(data)
        def wait(self):return self.returncode
        def poll(self):return self.returncode
        def kill(self):pass
    Process.returncode=returncode
    monkeypatch.setattr(subprocess,'Popen',lambda *a,**k:Process())


def test_download_roundtrip_uses_completion_manifest(client,monkeypatch,tmp_path):
    monkeypatch.setattr(remote,'ROOT',tmp_path/'remote')
    folder=remote.job_dir('result');folder.mkdir(parents=True)
    source=tmp_path/'file.txt';source.write_text('中文 data')
    remote.atomic_json(folder/'state.json',{'task_id':'result','state':'succeeded','artifacts':[
        {'path':str(source),'exists':True,'size':source.stat().st_size,'sha256':hashlib.sha256(source.read_bytes()).hexdigest()}]})
    buffer=io.BytesIO()
    monkeypatch.setattr(remote.sys,'stdout',SimpleNamespace(buffer=buffer))
    remote.download('result')
    fake_process(monkeypatch,buffer.getvalue())
    fetched=client.fetch('result',tmp_path/'downloads')
    assert Path(fetched['files'][0]['path']).read_bytes()==source.read_bytes()


def test_changed_payload_does_not_replace_existing_file(client,monkeypatch,tmp_path):
    target=tmp_path/'downloads';target.mkdir();(target/'a.txt').write_bytes(b'original')
    manifest={'task_id':'result','files':[{'path':'/remote/a.txt','size':3,'sha256':hashlib.sha256(b'abc').hexdigest()}]}
    fake_process(monkeypatch,wire([('manifest.json',json.dumps(manifest).encode()),('0',b'xyz')]))
    with pytest.raises(SSHError) as exc:client.fetch('result',target)
    assert exc.value.kind=='artifact_changed'
    assert (target/'a.txt').read_bytes()==b'original'
    assert sorted(p.name for p in target.iterdir())==['a.txt']


def test_download_does_not_inherit_mcp_request_stream(client,monkeypatch,tmp_path):
    manifest={'task_id':'result','files':[]}
    class Process:
        returncode=0
        stdout=io.BytesIO(wire([('manifest.json',json.dumps(manifest).encode())]))
        def wait(self):return 0
        def poll(self):return 0
        def kill(self):pass
    def launch(*args,**kwargs):
        assert kwargs['stdin']==subprocess.DEVNULL
        return Process()
    monkeypatch.setattr(subprocess,'Popen',launch)
    assert client.fetch('result',tmp_path/'downloads')['files']==[]


def test_invalid_archive_names_are_not_extracted(client,monkeypatch,tmp_path):
    manifest={'task_id':'result','files':[{'path':'/remote/a.txt','size':3,'sha256':hashlib.sha256(b'abc').hexdigest()}]}
    fake_process(monkeypatch,wire([('manifest.json',json.dumps(manifest).encode()),('../escape',b'abc')]))
    with pytest.raises(SSHError) as exc:client.fetch('result',tmp_path/'downloads')
    assert exc.value.kind=='protocol'
    assert not (tmp_path/'escape').exists()


def test_truncated_stream_with_ssh_exit_zero_retries(client,monkeypatch,tmp_path):
    manifest={'task_id':'result','files':[{'path':'/remote/a.txt','size':3,'sha256':hashlib.sha256(b'abc').hexdigest()}]}
    complete=wire([('manifest.json',json.dumps(manifest).encode()),('0',b'abc')])
    calls=[]
    class Process:
        returncode=0
        def __init__(self,data):self.stdout=io.BytesIO(data)
        def wait(self):return 0
        def poll(self):return 0
        def kill(self):pass
    def connect(argv,**kwargs):
        calls.append(argv)
        return Process(complete[:1538] if len(calls)==1 else complete)
    monkeypatch.setattr(subprocess,'Popen',connect)
    fetched=client.fetch('result',tmp_path/'downloads')
    assert Path(fetched['files'][0]['path']).read_bytes()==b'abc'
    assert len(calls)==2 and 'ControlPath=none' in calls[1]
    assert not list((tmp_path/'downloads').glob('.ssh4codex-part-*'))


def test_record_binding_is_atomic_across_concurrent_servers(client,monkeypatch):
    monkeypatch.setattr(Client,'rpc',lambda *a,**k:{'task_id':'shared','state':'running'})
    def submit(name):
        try:
            Client(name).submit('printf once',task_id='shared')
            return name
        except SSHError as exc:
            assert exc.kind=='configuration'
            return None
    with ThreadPoolExecutor(8) as pool:results=list(pool.map(submit,['one','two']*16))
    winners=set(x for x in results if x)
    assert len(winners)==1
    record=json.loads((client.local/'task-shared.json').read_text())
    assert record['server']==winners.pop()
