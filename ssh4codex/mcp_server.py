"""Optional official MCP SDK adapter; CLI and MCP share one task engine."""
import asyncio
import io
import os
import sys
from typing import Any
from mcp.server.fastmcp import FastMCP
from .client import Client, SSHError

mcp = FastMCP('ssh4codex', log_level='WARNING')


async def invoke(server, method, *args, **kwargs):
    try:
        transport = {key: kwargs.pop(key) for key in ('fresh_connection', 'rpc_timeout', 'transfer_timeout') if key in kwargs}
        return await asyncio.to_thread(getattr(Client(server, **transport), method), *args, **kwargs)
    except (SSHError, ValueError, OSError, KeyError) as exc:
        return {'error': getattr(exc, 'kind', type(exc).__name__), 'message': str(exc), **getattr(exc, 'details', {}),
                **({'task_id': exc.task_id} if getattr(exc, 'task_id', None) else {})}


@mcp.tool()
async def remote_run(server: str, script: str, cwd: str | None = None,
                     interpreter: list[str] | None = None, env: dict[str, str] | None = None,
                     artifacts: list[str] | None = None, timeout: float | None = None,
                     task_id: str | None = None, wait_seconds: float = 2,
                     inputs: dict[str, str] | None = None, requires: list[str] | None = None,
                     profile: str | None = None, fresh_connection: bool = False) -> dict[str, Any]:
    """Run a script in remote tmux. Reuse task_id after uncertainty; never invent a retry id. Logs bounded to 2KB per stream."""
    if not 0 <= wait_seconds <= 60:
        return {'error': 'validation', 'message': 'wait_seconds must be 0..60'}
    return await invoke(server, 'submit', script, cwd, env, interpreter, artifacts, timeout, task_id, wait_seconds, inputs=inputs, requires=requires, profile=profile, fresh_connection=fresh_connection)


@mcp.tool()
async def remote_status(server: str, task_id: str, stdout_cursor: int = 0,
                        stderr_cursor: int = 0, limit: int = 2048, tail: bool = False,
                        fresh_connection: bool = False, rpc_timeout: float | None = None) -> dict[str, Any]:
    """Read state and incremental stdout/stderr. Save returned byte cursors; remaining reports unread data."""
    return await invoke(server, 'status', task_id, stdout_cursor, stderr_cursor, limit, tail, fresh_connection=fresh_connection, rpc_timeout=rpc_timeout)


@mcp.tool()
async def remote_status_many(server: str, task_ids: list[str], logs: bool = False,
                             limit: int = 2048) -> dict[str, Any]:
    """Read 1..64 tasks in one SSH request. No logs by default; optional bounded tails."""
    return await invoke(server, 'status_many', task_ids, logs, limit)


@mcp.tool()
async def remote_wait(server: str, task_id: str, seconds: float = 10) -> dict[str, Any]:
    """Wait at most 60 seconds. A running result is not a failure; the task stays in tmux."""
    return await invoke(server, 'wait', task_id, seconds)


@mcp.tool()
async def remote_cancel(server: str, task_id: str) -> dict[str, Any]:
    """Request cancellation of this task and its process group; never kill the shared tmux session."""
    return await invoke(server, 'cancel', task_id)


@mcp.tool()
async def remote_fetch(server: str, task_id: str, destination: str, transfer_timeout: float | None = None) -> dict[str, Any]:
    """Fetch declared artifacts only after success, verifying completion-time SHA256 before atomic replace."""
    return await invoke(server, 'fetch', task_id, destination, transfer_timeout=transfer_timeout)


@mcp.tool()
async def remote_tasks(server: str) -> dict[str, Any]:
    """Discover the last 20 durable remote tasks after client restart."""
    return await invoke(server, 'rpc', 'list')


@mcp.tool()
async def remote_put(server: str, source: str, destination: str, retries: int = 2, transfer_timeout: float | None = None) -> dict[str, Any]:
    """Upload a local file by path, with SHA256 verification and atomic replace; file contents stay out of tool output."""
    return await invoke(server, 'put', source, destination, retries=retries, transfer_timeout=transfer_timeout)


@mcp.tool()
async def remote_recover(server: str, task_id: str, retry: bool = False) -> dict[str, Any]:
    """Fresh status first; explicit retry replays the saved original request only if no task exists."""
    return await invoke(server, 'recover', task_id, retry)


@mcp.tool()
async def remote_poll(server: str, task_id: str, consumer: str, limit: int = 2048, reset: bool = False) -> dict[str, Any]:
    """Incremental logs with persistent consumer-specific cursors; use a different consumer per agent."""
    return await invoke(server, 'poll', task_id, consumer, limit, reset)


@mcp.tool()
async def remote_transfer_status(server: str, transfer_id: str) -> dict[str, Any]:
    """Inspect received upload bytes. Complete means the destination was atomically committed."""
    return await invoke(server, 'transfer_status', transfer_id)


@mcp.tool()
async def remote_doctor(server: str, requires: list[str] | None = None, profile: str | None = None) -> dict[str, Any]:
    """Check remote tmux and opt-in executable requirements using an external execution profile."""
    return await invoke(server, 'doctor', requires=requires, profile=profile)


def main():
    # SDK 1.26 rewraps stdio buffers and can close them at shutdown. Keep the
    # process streams intact so the frozen runtime can flush them on exit.
    original = sys.stdin, sys.stdout
    sys.stdin = io.TextIOWrapper(os.fdopen(os.dup(0), 'rb'), encoding='utf-8')
    sys.stdout = io.TextIOWrapper(os.fdopen(os.dup(1), 'wb'), encoding='utf-8')
    try:
        mcp.run(transport='stdio')
    finally:
        sys.stdin, sys.stdout = original
