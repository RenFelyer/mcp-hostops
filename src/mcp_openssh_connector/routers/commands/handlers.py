"""Обработчик роутера команд: выполнение команды на хосте по ssh."""

from typing import Annotated

from fastmcp import FastMCP
from mcp_types import ToolAnnotations
from pydantic import Field

from ...core.schemas import NonEmptyStr, SudoMode
from .schemas import RunResult
from .services import run_command

router: FastMCP = FastMCP(name="commands", on_duplicate="error")


@router.tool(
    title="Команда на хосте",
    tags={"commands"},
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
async def run(
    host: NonEmptyStr,
    command: NonEmptyStr,
    cwd: NonEmptyStr = "~",
    timeout: Annotated[float | None, Field(gt=0)] = None,
    sudo: SudoMode = "auto",
    stdin: str | None = None,
) -> RunResult:
    """Выполнить команду на хосте и дождаться результата.

    По таймауту процесс убивается — для долгого используйте start. Пароль sudo
    берётся из ~/.ssh/<host>.secret и в выводе маскируется.

    Args:
        host: Алиас из ~/.ssh/config.
        command: Команда для оболочки хоста.
        cwd: Каталог выполнения; по умолчанию домашний. `~` и `~/…`
            раскрываются, остальное берётся буквально.
        timeout: Секунды; None — значение по умолчанию из настроек.
        sudo: auto — решить по команде; true — праймить пароль принудительно;
            false — не праймить (нужно и там, где sudo настроен NOPASSWD).
        stdin: Текст на stdin команды.
    """
    return await run_command(host, command, cwd, timeout, sudo, stdin)
