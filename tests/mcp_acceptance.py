"""Test the actual stdio MCP protocol using the official client SDK."""
import argparse
import asyncio
import json
from pathlib import Path
from live_support import write_report
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', required=True)
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    params=StdioServerParameters(command=str(root/'.venv/bin/ssh4codex-mcp'),args=[])
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()
            tools=await session.list_tools()
            assert len(tools.tools)==8
            response=await session.call_tool('remote_run',{'server':args.server,'script':"printf 'MCP roundtrip OK\\n'",'wait_seconds':3})
            data=response.structuredContent
            # FastMCP may wrap primitive returns; dict tool returns should remain structured.
            assert data['state']=='succeeded',data
            assert data['stdout']['text']=='MCP roundtrip OK\n'
            listed=await session.call_tool('remote_tasks',{'server':args.server})
            assert listed.structuredContent['tasks']
            batch=await session.call_tool('remote_status_many',{'server':args.server,'task_ids':[data['task_id']]})
            assert batch.structuredContent['tasks'][0]['state']=='succeeded'
            assert 'stdout' not in batch.structuredContent['tasks'][0]
            report={'passed':True,'transport':'stdio','tools':[t.name for t in tools.tools],
                    'remote_run_state':data['state'],'remote_task_discovery':True,'batch_status':True}
            write_report("mcp_acceptance.json",report)
            print(json.dumps(report))

asyncio.run(main())
