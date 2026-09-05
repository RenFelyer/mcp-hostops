"""Hosts router handlers: host status and parameters from ~/.ssh/config.

Services are synchronous (socket, subprocess, files); handlers move them to a thread.
"""

from typing import Annotated

import anyio.to_thread
from fastmcp import FastMCP
from pydantic import Field

from ...core.schemas import READS_LOCAL, READS_REMOTE, Host, NonEmptyStr
from ...core.utils.hosts import require_host
from .schemas import CheckResult, ListHostsResult
from .services import check_statuses, list_statuses

router: FastMCP = FastMCP(name="hosts", on_duplicate="error")


@router.tool(title="List hosts", tags={"hosts"}, annotations=READS_REMOTE)
async def list_hosts(refresh: bool = False) -> ListHostsResult:
    """Hosts from ~/.ssh/config with their last known availability.

    The server refreshes a cache older than the threshold itself; a host missing
    from the cache gets an unknown status.

    Args:
        refresh: Re-measure now instead of reading the cache.
    """
    return await anyio.to_thread.run_sync(list_statuses, refresh)


@router.tool(title="Check hosts", tags={"hosts"}, annotations=READS_REMOTE)
async def check_hosts(
    aliases: Annotated[list[NonEmptyStr], Field(min_length=1)], deep: bool = False
) -> list[CheckResult]:
    """Check the availability of specific hosts right now, bypassing the cache.

    Args:
        aliases: Aliases from ~/.ssh/config; an unknown alias gets an unknown status.
        deep: False — TCP probe of the port ("host is up"); True — an actual
            login (`ssh ... true`, "key accepted, access granted"), failure
            reason in detail.
    """
    return await anyio.to_thread.run_sync(check_statuses, aliases, deep)


@router.tool(title="Host parameters", tags={"hosts"}, annotations=READS_LOCAL)
async def get_host(alias: NonEmptyStr) -> Host:
    """Parameters of a single host as ssh sees them: hostname, user, port, jump host.

    Args:
        alias: Alias from ~/.ssh/config.
    """
    return await require_host(alias)
