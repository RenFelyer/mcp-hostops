"""Commands router service: running a command on a host with a timeout."""

import time

import anyio

from ...core.config.constants import TERM_GRACE
from ...core.config.environment import get_settings
from ...core.errors import UserError
from ...core.schemas import SudoMode
from ...core.utils.hosts import require_host
from ...core.utils.ssh import Capture, execute, prepare, spawn
from .schemas import RunResult


async def run_command(
    host: str,
    command: str,
    cwd: str,
    timeout: float | None,
    sudo_mode: SudoMode,
    user_stdin: str | None,
) -> RunResult:
    """Run a command on a host and wait for it; the sudo password is masked.

    On timeout the local ssh is killed; a remote command without a pty (host
    not in `pty_hosts`) won't get SIGHUP and may keep running on its own.

    Args:
        host: Alias from the config.
        command: Command for the host's shell.
        cwd: Working directory.
        timeout: Seconds; None — the default from settings.
        sudo_mode: Sudo password priming mode.
        user_stdin: Text for the command's stdin, after the password line.

    Raises:
        UserError: the alias is not in the config, the sudo password is
            unavailable, or the timeout exceeds `max_command_timeout`.
    """
    s = get_settings()
    if timeout is None:
        timeout = s.run_timeout
    elif timeout > s.max_command_timeout:
        raise UserError(f"timeout {timeout} exceeds the cap of {s.max_command_timeout}s; use start for long-running")
    call = prepare(await require_host(host), command, cwd, sudo_mode, user_stdin, s)
    capture = Capture(s.output_limit, call.password)
    exit_code: int | None = None
    started = time.monotonic()

    async with spawn(call) as proc:
        with anyio.move_on_after(timeout) as scope:
            exit_code = await execute(proc, call, capture)
        if scope.cancelled_caught:
            proc.terminate()
            with anyio.move_on_after(TERM_GRACE):
                await proc.wait()

    return RunResult(
        **capture.drained().model_dump(),
        exit_code=exit_code,
        duration=time.monotonic() - started,
        timed_out=scope.cancelled_caught,
        sudo_used=call.password is not None,
    )
