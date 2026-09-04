"""Обработчики роутера хостов: статус и параметры хостов из ~/.ssh/config.

Сервисы синхронные (socket, subprocess, файлы), обработчики уводят их в поток.
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


@router.tool(title="Список хостов", tags={"hosts"}, annotations=READS_REMOTE)
async def list_hosts(refresh: bool = False) -> ListHostsResult:
    """Хосты из ~/.ssh/config с их последней известной доступностью.

    Кэш старше порога сервер обновляет сам; хост, которого в кэше нет, получает
    статус unknown.

    Args:
        refresh: Перемерить заново вместо чтения кэша.
    """
    return await anyio.to_thread.run_sync(list_statuses, refresh)


@router.tool(title="Проверка хостов", tags={"hosts"}, annotations=READS_REMOTE)
async def check_hosts(
    aliases: Annotated[list[NonEmptyStr], Field(min_length=1)], deep: bool = False
) -> list[CheckResult]:
    """Проверить доступность конкретных хостов прямо сейчас, мимо кэша.

    Args:
        aliases: Алиасы из ~/.ssh/config; чужой алиас даёт статус unknown.
        deep: False — TCP-проба порта («хост поднят»); True — реальный вход
            `ssh ... true` («ключ принят, внутрь пускают»), причина отказа в
            detail.
    """
    return await anyio.to_thread.run_sync(check_statuses, aliases, deep)


@router.tool(title="Параметры хоста", tags={"hosts"}, annotations=READS_LOCAL)
async def host_info(alias: NonEmptyStr) -> Host:
    """Параметры одного хоста глазами ssh: hostname, user, port, jump-хост.

    Args:
        alias: Алиас из ~/.ssh/config.
    """
    return await require_host(alias)
