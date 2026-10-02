import argparse
import json
from pathlib import Path
import sys

from .client import Client, SSHError, config_path, ssh_program, transport_env
from . import __version__


def parser():
    p = argparse.ArgumentParser(description='Durable tmux tasks over SSH; compact JSON output')
    p.add_argument('--version', action='version', version='ssh4codex '+__version__)
    sub = p.add_subparsers(dest='action', required=True)
    add = sub.add_parser('add', help='Save a named server; credentials stay in SSH config')
    add.add_argument('server'); add.add_argument('--target', required=True)
    add.add_argument('--port', type=int)
    add.add_argument('--cwd', default='.')
    add.add_argument('--session', required=True)
    add.add_argument('--identity-file'); add.add_argument('--control-path'); add.add_argument('--ssh-config')
    add.add_argument('--connect-timeout', type=int); add.add_argument('--rpc-timeout', type=float)
    add.add_argument('--transfer-timeout', type=float)
    add.add_argument('--compression', action=argparse.BooleanOptionalAction, default=None)
    for action in ['doctor', 'list', 'disconnect']:
        a = sub.add_parser(action); a.add_argument('server')
    connect = sub.add_parser('connect', help='Connect directly to the last selected tmux session (interactive terminal)')
    connect.add_argument('server')
    connect.add_argument('--session', help='Select and remember a tmux session; first use defaults to server configuration')
    batch = sub.add_parser('status-many', help='Read 1..64 tasks in one SSH request; omit logs by default')
    batch.add_argument('server'); batch.add_argument('task_ids', nargs='+')
    batch.add_argument('--logs', action='store_true'); batch.add_argument('--limit', type=int, default=2048)
    run = sub.add_parser('run', help='Submit and briefly wait; running tasks continue remotely')
    run.add_argument('server')
    script = run.add_mutually_exclusive_group(required=True)
    script.add_argument('--script', type=Path); script.add_argument('--command')
    run.add_argument('--cwd'); run.add_argument('--task-id')
    run.add_argument('--env', action='append', default=[], metavar='KEY=VALUE')
    run.add_argument('--interpreter', help='Executable or absolute interpreter path')
    run.add_argument('--artifact', action='append', default=[])
    run.add_argument('--timeout', type=float)
    run.add_argument('--wait', type=float, default=2)
    run.add_argument('--limit', type=int, default=2048)
    for action in ['status', 'wait', 'cancel', 'fetch']:
        a = sub.add_parser(action); a.add_argument('server'); a.add_argument('task_id')
        if action == 'status':
            a.add_argument('--stdout-cursor', type=int, default=0); a.add_argument('--stderr-cursor', type=int, default=0)
            a.add_argument('--limit', type=int, default=2048); a.add_argument('--tail', action='store_true')
        if action == 'wait':
            a.add_argument('--seconds', type=float, default=10); a.add_argument('--limit', type=int, default=2048)
        if action == 'fetch': a.add_argument('--to', required=True)
    put = sub.add_parser('put', help='Upload a local file atomically with SHA256 verification')
    put.add_argument('server'); put.add_argument('source'); put.add_argument('destination')
    put.add_argument('--mode', type=lambda v: int(v, 8), default=0o600)
    put.add_argument('--retries', type=int, default=2)
    put.add_argument('--progress', action='store_true', help='Emit transfer identity and resume checkpoints as JSON on stderr')
    transfer = sub.add_parser('transfer-status', help='Inspect bytes received for a resumable upload')
    transfer.add_argument('server'); transfer.add_argument('transfer_id')
    recover = sub.add_parser('recover', help='Query a task using a fresh connection; optionally replay its saved request')
    recover.add_argument('server'); recover.add_argument('task_id')
    recover.add_argument('--retry', action='store_true')
    recover.add_argument('--limit', type=int, default=2048)
    poll = sub.add_parser('poll', help='Read new logs using a private cursor for this consumer')
    poll.add_argument('server'); poll.add_argument('task_id'); poll.add_argument('--consumer', required=True)
    poll.add_argument('--reset', action='store_true'); poll.add_argument('--limit', type=int, default=2048)
    run.add_argument('--input', action='append', default=[], metavar='LOCAL=REMOTE')
    for name in ('run', 'doctor'):
        command = sub.choices[name]
        command.add_argument('--profile')
        command.add_argument('--require', action='append', default=[])
    for name in ('run', 'wait', 'recover'):
        sub.choices[name].add_argument('--fetch-to', help='Fetch declared artifacts if this response reports success')
    for name, command in sub.choices.items():
        if name != 'add':
            command.add_argument('--fresh-connection', action='store_true', help='Bypass the shared SSH master for this call')
            command.add_argument('--rpc-timeout', type=float)
            command.add_argument('--transfer-timeout', type=float)
    sub.add_parser('mcp', help='Serve the same API using the optional official MCP SDK')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.action == 'mcp':
            from .mcp_server import main as serve
            serve(); return
        if args.action == 'add':
            path = config_path(); path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            config = json.loads(path.read_text()) if path.exists() else {'servers': {}}
            config['servers'][args.server] = {k: v for k, v in vars(args).items() if k not in {'action', 'server'} and v is not None}
            path.write_text(json.dumps(config, indent=2)); path.chmod(0o600)
            value = {'server': args.server, 'config': str(path)}
        else:
            c = Client(args.server, fresh_connection=args.fresh_connection, rpc_timeout=args.rpc_timeout, transfer_timeout=args.transfer_timeout)
            if args.action == 'connect':
                sys.exit(c.connect(args.session))
            if args.action == 'run':
                if not 0 <= args.wait <= 60: raise ValueError('wait must be between 0 and 60 seconds')
                env = dict(item.split('=', 1) for item in args.env)
                script = args.script.read_text() if args.script else args.command
                value = c.submit(script, args.cwd, env, [args.interpreter] if args.interpreter else None, args.artifact, args.timeout, args.task_id, args.wait, args.limit, inputs=dict(item.split('=', 1) for item in args.input), requires=args.require, profile=args.profile)
            elif args.action == 'status':
                value = c.status(args.task_id, args.stdout_cursor, args.stderr_cursor, args.limit, args.tail)
            elif args.action == 'status-many':
                value = c.status_many(args.task_ids, args.logs, args.limit)
            elif args.action == 'wait': value = c.wait(args.task_id, args.seconds, args.limit)
            elif args.action == 'cancel': value = c.cancel(args.task_id)
            elif args.action == 'fetch': value = c.fetch(args.task_id, args.to)
            elif args.action == 'put':
                progress = (lambda item: print(json.dumps(item), file=sys.stderr, flush=True)) if args.progress else None
                value = c.put(args.source, args.destination, args.mode, args.retries, progress)
            elif args.action == 'transfer-status': value = c.transfer_status(args.transfer_id)
            elif args.action == 'recover': value = c.recover(args.task_id, args.retry, args.limit)
            elif args.action == 'poll': value = c.poll(args.task_id, args.consumer, args.limit, args.reset)
            elif args.action == 'doctor': value = c.doctor(requires=args.require, profile=args.profile)
            elif args.action == 'list': value = c.rpc('list')
            elif args.action == 'disconnect':
                import subprocess
                proc = subprocess.run([ssh_program(), *c.options, '-O', 'exit', c.server['target']], capture_output=True, env=transport_env())
                value = {'disconnected': proc.returncode == 0, 'remote_tasks_terminated': False}
        if getattr(args, 'fetch_to', None) and value.get('state') == 'succeeded':
            try:
                value['delivery'] = c.fetch(value['task_id'], args.fetch_to)
            except (SSHError, ValueError, OSError) as exc:
                value['delivery'] = {'error': getattr(exc, 'kind', type(exc).__name__), 'message': str(exc)}
        print(json.dumps(value, ensure_ascii=False, separators=(',', ':')))
        if value.get('delivery', {}).get('error'):
            sys.exit(2)
        if value.get('state') in {'failed', 'cancelled', 'timed_out', 'interrupted'}:
            sys.exit(1)
    except (SSHError, ValueError, OSError, KeyError) as exc:
        value = {'error': getattr(exc, 'kind', type(exc).__name__), 'message': str(exc), **getattr(exc, 'details', {})}
        if getattr(exc, 'task_id', None): value['task_id'] = exc.task_id
        print(json.dumps(value, ensure_ascii=False, separators=(',', ':')))
        sys.exit(2)
