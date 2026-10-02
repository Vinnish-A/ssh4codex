"""Test the actual stdio MCP protocol using the official client SDK."""
import argparse
import asyncio
import json
import os
from pathlib import Path
from live_support import PRIVATE_ROOT, write_report
import uuid
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', required=True)
    parser.add_argument('--binary', help='Standalone MCP executable; otherwise use the source entry point')
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    params=StdioServerParameters(command=args.binary or str(root/'.venv/bin/ssh4codex-mcp'),args=[],env=dict(os.environ))
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()
            tools=await session.list_tools()
            assert len(tools.tools)==12
            response=await session.call_tool('remote_run',{'server':args.server,'script':"printf 'MCP roundtrip OK\\n'",'wait_seconds':3})
            data=response.structuredContent
            # FastMCP may wrap primitive returns; dict tool returns should remain structured.
            assert data.get('state')=='succeeded',data
            assert data['stdout']['text']=='MCP roundtrip OK\n'
            listed=await session.call_tool('remote_tasks',{'server':args.server})
            assert listed.structuredContent['tasks']
            batch=await session.call_tool('remote_status_many',{'server':args.server,'task_ids':[data['task_id']]})
            assert batch.structuredContent['tasks'][0]['state']=='succeeded'
            assert 'stdout' not in batch.structuredContent['tasks'][0]
            token=uuid.uuid4().hex
            directory=PRIVATE_ROOT/'mcp-recovery'/token
            directory.mkdir(parents=True,mode=0o700)
            source=directory/'input.txt'; source.write_text('verified input')
            remote_file='/tmp/ssh4codex-mcp-'+token+'.txt'
            staged=await session.call_tool('remote_put',{'server':args.server,'source':str(source),'destination':remote_file})
            assert staged.structuredContent['phase']=='complete',staged
            inspected=await session.call_tool('remote_transfer_status',{'server':args.server,'transfer_id':staged.structuredContent['transfer_id']})
            assert inspected.structuredContent['bytes_received']==len('verified input')
            ready=await session.call_tool('remote_doctor',{'server':args.server,'requires':['bash']})
            assert ready.structuredContent['ready']
            checked=await session.call_tool('remote_run',{'server':args.server,'script':'cat '+remote_file,
                'inputs':{str(source):remote_file},'requires':['cat'],'wait_seconds':3})
            checked=checked.structuredContent
            assert checked['state']=='succeeded'
            recovered=await session.call_tool('remote_recover',{'server':args.server,'task_id':checked['task_id'],'retry':True})
            assert recovered.structuredContent['state']=='succeeded'
            for expected in ('verified input',''):
                polled=await session.call_tool('remote_poll',{'server':args.server,'task_id':checked['task_id'],'consumer':token})
                assert polled.structuredContent['stdout']['text']==expected
            report={'passed':True,'transport':'stdio','tools':[t.name for t in tools.tools],
                    'remote_run_state':data['state'],'remote_task_discovery':True,'batch_status':True,'verified_inputs':True,'transfer_inspection':True,'recovery':True,'persistent_poll':True,'preflight':True}
            write_report("mcp_acceptance.json",report)
            print(json.dumps(report))

asyncio.run(main())
