"""Commands router handler: running a command on a host over ssh."""

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from ...core.schemas import RUNS_REMOTE, NonEmptyStr, SudoMode
from .schemas import RunResult
from .services import run_command

router: FastMCP = FastMCP(name="commands", on_duplicate="error")


@router.tool(title="Command on host", tags={"commands"}, annotations=RUNS_REMOTE)
async def run(
    host: NonEmptyStr,
    command: NonEmptyStr,
    cwd: NonEmptyStr = "~",
    timeout: Annotated[float | None, Field(gt=0)] = None,
    sudo: SudoMode = "auto",
    stdin: str | None = None,
) -> RunResult:
    """Run a command on a host and wait for the result.

    On timeout the local ssh is killed (exit_code null, timed_out true) — use
    start for long-running commands. The sudo password is taken from
    ~/.ssh/<host>.secret and masked in the output.

    Args:
        host: Alias from ~/.ssh/config.
        command: Command for the host's shell.
        cwd: Working directory; defaults to home. `~` and `~/…` are expanded,
            everything else is taken literally.
        timeout: Seconds; null — the default from settings. Above the server's
            cap — a call error.
        sudo: auto — decide from the command; true — prime the password
            unconditionally; false — don't prime it (needed where sudo is
            configured NOPASSWD).
        stdin: Text for the command's stdin.
    """
    return await run_command(host, command, cwd, timeout, sudo, stdin)
