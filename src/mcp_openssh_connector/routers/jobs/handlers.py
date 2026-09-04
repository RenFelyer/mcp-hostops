"""Обработчики роутера фоновых задач: запуск, чтение, снятие, обзор.

Задачи снимаются в lifespan самого роутера — сервер об этом не знает, удаление
роутера уносит и его жизненный цикл.
"""

import contextlib
from collections.abc import AsyncIterator
from typing import Annotated

from fastmcp import FastMCP
from mcp_types import ToolAnnotations
from pydantic import Field

from ...core.errors import UserError
from ...core.schemas import NonEmptyStr, SudoMode
from .schemas import JobRef, JobSnapshot
from .services import manager


@contextlib.asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncIterator[None]:
    """Снять все фоновые задачи при остановке."""
    try:
        yield
    finally:
        await manager.shutdown()


router: FastMCP = FastMCP(name="jobs", lifespan=_lifespan, on_duplicate="error")


@router.tool(
    title="Команда в фоне",
    tags={"jobs"},
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
async def start(
    host: NonEmptyStr,
    command: NonEmptyStr,
    cwd: NonEmptyStr = "~",
    sudo: SudoMode = "auto",
) -> JobRef:
    """Запустить команду на хосте в фоне и сразу вернуть id задачи.

    Для долгих команд: вывод забирается вызовом job. Задача живёт, пока жив
    сервер (в пределах сессии), и не переживает её.

    Args:
        host: Алиас из ~/.ssh/config.
        command: Команда для оболочки хоста.
        cwd: Как у run.
        sudo: Как у run.
    """
    return await manager.start(host, command, cwd, sudo)


@router.tool(
    title="Состояние задачи",
    tags={"jobs"},
    # Чтение забирает прирост вывода из буфера — повторный вызов даст другое.
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    ),
)
async def job(job_id: NonEmptyStr, wait: Annotated[float, Field(ge=0)] = 0.0) -> JobSnapshot:
    """Состояние фоновой задачи и накопленный с прошлого чтения вывод.

    Вывод отдаётся приростом и из буфера вычищается.

    Args:
        job_id: Идентификатор из start.
        wait: Секунды ожидания завершения, чтобы не опрашивать в цикле; 0 — не
            ждать.
    """
    snap = await manager.snapshot(job_id, wait)
    if snap is None:
        raise UserError(f"задача {job_id!r} не найдена")
    return snap


@router.tool(
    title="Снять задачу",
    tags={"jobs"},
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
async def kill(job_id: NonEmptyStr) -> bool:
    """Снять фоновую задачу.

    True — задача была запущена и получила отмену; False — её нет или она уже
    завершилась.

    Args:
        job_id: Идентификатор из start.
    """
    return manager.kill(job_id)


@router.tool(
    title="Список задач",
    tags={"jobs"},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False),
)
async def jobs() -> list[JobRef]:
    """Все фоновые задачи сессии: id, хост, команда, состояние (без вывода)."""
    return manager.listing()
