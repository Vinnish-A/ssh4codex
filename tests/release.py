"""Exercise the downloaded runtime without Python or SSH on its PATH."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def check(package):
    with tempfile.TemporaryDirectory(prefix='ssh4codex-smoke-') as directory:
        env = dict(os.environ, PATH='/nonexistent', HOME=directory,
                   SSH4CODEX_CONFIG=directory + '/config.json',
                   SSH4CODEX_STATE=directory + '/state')
        executable = str(package / 'ssh4codex')
        def run(*args):
            return subprocess.run([executable, *args], env=env, capture_output=True,
                                  text=True, check=True, timeout=30).stdout
        assert run('--version').strip().endswith((package / 'VERSION').read_text().strip())
        unknown = subprocess.run([executable, 'doctor', 'unconfigured-smoke-server'], env=env,
                                 capture_output=True, text=True, timeout=10)
        assert unknown.returncode == 2
        assert json.loads(unknown.stdout)['error'] == 'configuration'
        assert not list(package.rglob('catalog.json'))
        assert 'run' in run('--help')
        assert '--session' in run('connect', '--help')
        ssh = subprocess.run([str(package / 'tools/ssh'), '-V'], env=env,
                             capture_output=True, text=True, check=True, timeout=10)
        assert 'OpenSSH' in ssh.stderr
        eof = subprocess.run([executable, 'mcp'], env=env, input='',
                             capture_output=True, text=True, check=True, timeout=30)
        assert 'Traceback' not in eof.stderr, eof.stderr
        params = StdioServerParameters(command=str(package / 'ssh4codex-mcp'), env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert len(tools.tools) == 8
                response = await session.call_tool('remote_tasks', {'server': 'missing-smoke-server'})
                assert response.structuredContent['error'] == 'configuration'
        print(json.dumps({'standalone': True, 'cli': True, 'bundled_ssh': True,
                          'mcp_tools': len(tools.tools), 'external_path': '/nonexistent'}))


asyncio.run(check(Path(sys.argv[1]).resolve()))
