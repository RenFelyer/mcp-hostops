"""Сервис роутера команд: синхронное выполнение команды на хосте с таймаутом."""

import time

import anyio

from ...core.config import get_settings
from ...core.schemas import SudoMode
from ...core.utils.hosts import require_host
from ...core.utils.ssh import Capture, prepare, pump
from .schemas import RunResult

_TERM_GRACE = 2.0  # секунд между SIGTERM и SIGKILL при таймауте


async def run_command(
    host: str,
    command: str,
    cwd: str,
    timeout: float | None,
    sudo_mode: SudoMode,
    user_stdin: str | None,
) -> RunResult:
    """Выполнить команду на хосте синхронно, с таймаутом; пароль sudo маскируется.

    По таймауту убивается локальный ssh; удалённая команда без pty (хост не в
    `pty_hosts`) SIGHUP не получит и может доработать сама.

    Args:
        host: Алиас из конфига.
        command: Команда для оболочки хоста.
        cwd: Каталог выполнения.
        timeout: Секунды; None — дефолт из настроек. Сверху ограничен
            `max_command_timeout`.
        sudo_mode: Режим прайминга пароля sudo.
        user_stdin: Текст на stdin команды после строки пароля.

    Raises:
        UserError: алиаса нет в конфиге или пароль sudo недоступен.
    """
    s = get_settings()
    timeout = s.run_timeout if timeout is None else min(timeout, s.max_command_timeout)
    call = prepare(await require_host(host), command, cwd, sudo_mode, user_stdin, s)
    capture = Capture(s.output_limit, call.password)
    exit_code: int | None = None
    started = time.monotonic()

    async with await anyio.open_process(call.argv) as proc:
        try:
            with anyio.move_on_after(timeout) as scope:
                await pump(proc, call.stdin, capture)
                exit_code = await proc.wait()
            if scope.cancelled_caught:
                proc.terminate()
                with anyio.move_on_after(_TERM_GRACE):
                    await proc.wait()
        finally:
            # Не ушёл по SIGTERM или вызов отменили снаружи — добиваем, иначе
            # закрытие процесса будет ждать его завершения.
            if proc.returncode is None:
                proc.kill()

    return RunResult(
        **capture.drained().model_dump(),
        exit_code=exit_code,
        duration=time.monotonic() - started,
        timed_out=scope.cancelled_caught,
        sudo_used=call.password is not None,
    )
