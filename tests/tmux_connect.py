"""Opt-in PTY test of direct tmux connection, resumption and shared clients.

Run: .venv/bin/python tests/tmux_connect.py --run
Uses only dedicated synthetic sessions; terminal transcripts stay in .local/.
"""
import argparse
import json
import os
from pathlib import Path
import pty
import select
import shlex
import subprocess
import time
import uuid

from ssh4codex.client import Client, load_server, ssh_program, transport_env

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--binary', default=str(ROOT / '.venv/bin/ssh4codex'))
    parser.add_argument('--server', default='solvinglab')
    args = parser.parse_args()
    if not args.run:
        parser.error('Pass --run to test real remote tmux sessions')
    started = time.monotonic()
    run_id = uuid.uuid4().hex[:10]
    sessions = ['s4c-' + run_id + suffix for suffix in ('-a', '-b')]
    private = ROOT / '.local/tmux-connect' / run_id
    private.mkdir(parents=True, mode=0o700)
    config = private / 'config.json'
    config.write_text(json.dumps({'servers': {args.server: {
        **load_server(args.server), 'session': sessions[0], 'cwd': '/tmp',
        'control_path': str(Path.home() / '.ssh/ssh4codex' / ('connect-' + run_id)),
    }}}))
    config.chmod(0o600)
    env = dict(os.environ, SSH4CODEX_CONFIG=str(config), SSH4CODEX_STATE=str(private / 'state'),
               TERM='xterm-256color')
    os.environ.update({k: env[k] for k in ('SSH4CODEX_CONFIG', 'SSH4CODEX_STATE')})
    client = Client(args.server)
    peers = []
    version = subprocess.check_output([args.binary, '--version'], env=env, text=True).strip()

    def tmux(*words):
        result = client.call_ssh(shlex.join(['tmux', *words]), payload=b'', timeout=15)
        assert result.returncode == 0, result.stderr.decode()
        return result.stdout.decode().strip().splitlines()

    def terminals(session):
        # There may be no tmux server before the first connection creates it.
        result = client.call_ssh(shlex.join(['tmux', 'list-clients', '-t', '=' + session,
                                            '-F', '#{client_tty}']), payload=b'', timeout=15)
        return set(result.stdout.decode().strip().splitlines()) if result.returncode == 0 else set()

    def drain(peer, expected=None):
        until = time.monotonic() + (8 if expected else .1)
        while time.monotonic() < until:
            if select.select([peer['fd']], [], [], .1)[0]:
                try:
                    block = os.read(peer['fd'], 65536)
                except OSError:
                    break
                if not block:
                    break
                peer['output'].extend(block)
                if expected and expected.encode() in peer['output']:
                    return
        if expected:
            raise AssertionError('Expected terminal result not received: ' + expected)

    def connect(session, explicit=False):
        before = terminals(session)
        master, slave = pty.openpty()
        words = [args.binary, 'connect', args.server]
        if explicit:
            words += ['--session', session]
        process = subprocess.Popen(words, stdin=slave, stdout=slave, stderr=slave, env=env)
        os.close(slave)
        peer = {'process': process, 'fd': master, 'output': bytearray(), 'session': session}
        peers.append(peer)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            drain(peer)
            attached = terminals(session) - before
            if attached:
                assert len(attached) == 1
                peer['tty'] = attached.pop()
                return peer
            assert process.poll() is None, peer['output'].decode(errors='replace')
        raise AssertionError('No remote tmux client attached')

    def detach(peer):
        tmux('detach-client', '-t', peer['tty'])
        assert peer['process'].wait(timeout=10) == 0
        drain(peer)

    try:
        first = connect(sessions[0])
        second = connect(sessions[0])
        assert terminals(sessions[0]) == {first['tty'], second['tty']}
        detach(second)
        assert terminals(sessions[0]) == {first['tty']}
        marker = 'persist-' + run_id
        os.write(first['fd'], ('export SSH4CODEX_CONNECT_MARKER=' + marker + '\r').encode())
        os.write(first['fd'], b"printf 'SET=%s\\n' \"$SSH4CODEX_CONNECT_MARKER\"\r")
        drain(first, 'SET=' + marker)
        detach(first)
        resumed = connect(sessions[0])
        os.write(resumed['fd'], b"printf 'RESTORED=%s\\n' \"$SSH4CODEX_CONNECT_MARKER\"\r")
        drain(resumed, 'RESTORED=' + marker)
        detach(resumed)
        selected = connect(sessions[1], explicit=True)
        detach(selected)
        remembered = connect(sessions[1])
        detach(remembered)
        assert client.tmux_target.read_text() == sessions[1]
        report = {'runtime': version, 'passed': True, 'run_id': run_id,
                  'elapsed_seconds': round(time.monotonic() - started, 3),
                  'interactive_connections': len(peers),
                  'checks': {'configured_default_created_and_attached': True,
                             'existing_client_not_detached': True,
                             'detach_preserves_shell_environment': True,
                             'explicit_session_remembered_by_new_process': True},
                  'synthetic_sessions_only': True}
        (ROOT / 'benchmarks/tmux_connect.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report))
    finally:
        for session in sessions:
            client.call_ssh(shlex.join(['tmux', 'kill-session', '-t', '=' + session]), payload=b'', timeout=15)
        for index, peer in enumerate(peers):
            peer['process'].wait(timeout=10)
            drain(peer)
            (private / f'{index}.terminal').write_bytes(peer['output'])
            os.close(peer['fd'])
        subprocess.run([ssh_program(), *client.options, '-O', 'exit', client.server['target']],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=15, env=transport_env())


if __name__ == '__main__':
    main()
