import argparse
import json
from pathlib import Path
import sys

from .client import Client, SSHError, config_path, server_catalog, ssh_program, transport_env
from . import __version__


def parser():
    p = argparse.ArgumentParser(description='Durable tmux tasks over SSH; compact JSON output')
    p.add_argument('--version', action='version', version='ssh4codex '+__version__)
    sub = p.add_subparsers(dest='action', required=True)
    add = sub.add_parser('add', help='Save a named server; credentials stay in SSH config')
    add.add_argument('server'); add.add_argument('--target', required=True)
    add.add_argument('--port', type=int)
    add.add_argument('--cwd', default='.')
    add.add_argument('--session', default='ssh4codex')
    add.add_argument('--identity-file'); add.add_argument('--control-path'); add.add_argument('--ssh-config')
    for action in ['doctor', 'list', 'disconnect']:
        a = sub.add_parser(action); a.add_argument('server')
    run = sub.add_parser('run', help='Submit and briefly wait; running tasks continue remotely')
    run.add_argument('server')
    script = run.add_mutually_exclusive_group(required=True)
    script.add_argument('--script', type=Path); script.add_argument('--command')
    run.add_argument('--cwd'); run.add_argument('--task-id')
    run.add_argument('--env', action='append', default=[], metavar='KEY=VALUE')
    run.add_argument('--interpreter', default='bash', help='Executable, e.g. absolute tidy Rscript path')
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
    sub.add_parser('catalog', help='List recorded server aliases and verified key deployment metadata')
    sub.add_parser('mcp', help='Serve the same API using the optional official MCP SDK')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.action == 'mcp':
            from .mcp_server import main as serve
            serve(); return
        if args.action == 'catalog':
            value = {'servers': server_catalog()}
        elif args.action == 'add':
            path = config_path(); path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            config = json.loads(path.read_text()) if path.exists() else {'servers': {}}
            config['servers'][args.server] = {k: v for k, v in vars(args).items() if k not in {'action', 'server'} and v is not None}
            path.write_text(json.dumps(config, indent=2)); path.chmod(0o600)
            value = {'server': args.server, 'config': str(path)}
        else:
            c = Client(args.server)
            if args.action == 'run':
                if not 0 <= args.wait <= 60: raise ValueError('wait must be between 0 and 60 seconds')
                env = dict(item.split('=', 1) for item in args.env)
                script = args.script.read_text() if args.script else args.command
                value = c.submit(script, args.cwd, env, [args.interpreter], args.artifact, args.timeout, args.task_id, args.wait, args.limit)
            elif args.action == 'status':
                value = c.status(args.task_id, args.stdout_cursor, args.stderr_cursor, args.limit, args.tail)
            elif args.action == 'wait': value = c.wait(args.task_id, args.seconds, args.limit)
            elif args.action == 'cancel': value = c.cancel(args.task_id)
            elif args.action == 'fetch': value = c.fetch(args.task_id, args.to)
            elif args.action == 'put': value = c.put(args.source, args.destination, args.mode)
            elif args.action == 'doctor': value = c.rpc('doctor')
            elif args.action == 'list': value = c.rpc('list')
            elif args.action == 'disconnect':
                import subprocess
                proc = subprocess.run([ssh_program(), *c.options, '-O', 'exit', c.server['target']], capture_output=True, env=transport_env())
                value = {'disconnected': proc.returncode == 0, 'remote_tasks_terminated': False}
        print(json.dumps(value, ensure_ascii=False, separators=(',', ':')))
        if value.get('state') in {'failed', 'cancelled', 'timed_out', 'interrupted'}:
            sys.exit(1)
    except (SSHError, ValueError, OSError, KeyError) as exc:
        value = {'error': getattr(exc, 'kind', type(exc).__name__), 'message': str(exc)}
        if getattr(exc, 'task_id', None): value['task_id'] = exc.task_id
        print(json.dumps(value, ensure_ascii=False, separators=(',', ':')))
        sys.exit(2)
