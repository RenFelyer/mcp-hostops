"""Сервис роутера команд: выполнение команды на хосте с таймаутом."""

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
    """Выполнить команду на хосте и дождаться её; пароль sudo маскируется.

    По таймауту убивается локальный ssh; удалённая команда без pty (хост не в
    `pty_hosts`) SIGHUP не получит и может доработать сама.

    Args:
        host: Алиас из конфига.
        command: Команда для оболочки хоста.
        cwd: Каталог выполнения.
        timeout: Секунды; None — дефолт из настроек.
        sudo_mode: Режим прайминга пароля sudo.
        user_stdin: Текст на stdin команды после строки пароля.

    Raises:
        UserError: алиаса нет в конфиге, пароль sudo недоступен или таймаут
            больше `max_command_timeout`.
    """
    s = get_settings()
    if timeout is None:
        timeout = s.run_timeout
    elif timeout > s.max_command_timeout:
        raise UserError(f"таймаут {timeout} больше потолка {s.max_command_timeout} с; для долгого — start")
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
