"""Background jobs router handlers: start, read, kill, list.

Jobs are cancelled in the router's own lifespan — the server doesn't know about
it, so removing the router takes its lifecycle with it.
"""

import contextlib
from collections.abc import AsyncGenerator
from typing import Annotated

from fastmcp import FastMCP
from mcp_types import ToolAnnotations
from pydantic import Field

from ...core.schemas import READS_LOCAL, RUNS_REMOTE, NonEmptyStr, SudoMode
from .schemas import JobRef, JobSnapshot
from .services import manager


@contextlib.asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncGenerator[None]:
    """Cancel all background jobs on shutdown."""
    try:
        yield
    finally:
        await manager.shutdown()


router: FastMCP = FastMCP(name="jobs", lifespan=_lifespan, on_duplicate="error")


@router.tool(title="Command in background", tags={"jobs"}, annotations=RUNS_REMOTE)
async def start(
    host: NonEmptyStr,
    command: NonEmptyStr,
    cwd: NonEmptyStr = "~",
    sudo: SudoMode = "auto",
) -> JobRef:
    """Start a command on a host in the background and return its job id right away.

    For long-running commands: output is collected with the job call. The job
    lives as long as the server does (within the session) and doesn't outlive it.

    Args:
        host: Alias from ~/.ssh/config.
        command: Command for the host's shell.
        cwd: Same as run.
        sudo: Same as run.
    """
    return await manager.start(host, command, cwd, sudo)


@router.tool(
    title="Job status",
    tags={"jobs"},
    # Reading drains the incremental output from the buffer: server state changes,
    # a repeat call gives something different; no network involved.
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
    ),
)
async def get_job(job_id: NonEmptyStr, wait: Annotated[float, Field(ge=0)] = 0.0) -> JobSnapshot:
    """Status of a background job and the output accumulated since the last read.

    Output is returned as a delta and cleared from the buffer.

    Args:
        job_id: Identifier from start.
        wait: Seconds to wait for completion, to avoid polling in a loop; 0 —
            don't wait. Above the server's cap — waits as long as it allows.
    """
    return await manager.snapshot(job_id, wait)


@router.tool(
    title="Kill job",
    tags={"jobs"},
    # Killing kills the local ssh, which tears down the remote command with it;
    # repeating it changes nothing.
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
    ),
)
async def kill(job_id: NonEmptyStr) -> bool:
    """Kill a background job.

    True — the job was running and got cancelled; False — it doesn't exist or
    has already finished.

    Args:
        job_id: Identifier from start.
    """
    return manager.kill(job_id)


@router.tool(title="List jobs", tags={"jobs"}, annotations=READS_LOCAL)
async def list_jobs() -> list[JobRef]:
    """All background jobs of the session: id, host, command, status (no output)."""
    return manager.listing()
