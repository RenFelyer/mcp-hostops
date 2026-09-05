"""Обработчики роутера управления ~/.ssh/config: add_host, remove_host, forget_host, copy_id.

Сервисы синхронные (файлы, ssh -G, ssh-keygen, ssh-copy-id) — обработчики уводят
их в поток. Подсказки клиенту у каждого инструмента свои: все правят локальные
файлы (кроме copy_id, который ещё и ходит на хост), поэтому общих пресетов тут нет.
"""

from typing import Annotated

import anyio.to_thread
from fastmcp import FastMCP
from mcp_types import ToolAnnotations
from pydantic import Field

from ...core.schemas import NonEmptyStr
from . import services
from .schemas import AddHostResult, CopyIdResult, ForgetHostResult, ManagedHost, RemoveHostResult

router: FastMCP = FastMCP(name="sshconfig", on_duplicate="error")


@router.tool(
    title="Добавить хост",
    tags={"sshconfig"},
    # Пишет Host-блок в managed-файл (и один раз Include в конфиг); в сеть не
    # ходит (ssh -G локален). Повтор с теми же полями даёт то же состояние.
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
    ),
)
async def add_host(
    alias: NonEmptyStr,
    hostname: NonEmptyStr,
    user: str = "",
    port: Annotated[int, Field(gt=0)] = 22,
    identity_file: str = "",
    proxy_jump: str = "",
    extra: dict[str, str] | None = None,
) -> AddHostResult:
    """Добавить хост в ~/.ssh/config через managed-файл сервера.

    Блок пишется в каноническом виде в отдельный файл, подключённый к основному
    конфигу через Include; ручной конфиг не переписывается. Существующий
    managed-блок того же алиаса заменяется; алиас, описанный вручную, занят.

    Args:
        alias: Имя хоста для ssh (`ssh <alias>`); без пробелов и без * ? # !.
        hostname: Адрес или доменное имя хоста (HostName).
        user: Пользователь входа; пусто — не писать User.
        port: Порт ssh.
        identity_file: Путь к приватному ключу (IdentityFile); пусто — не писать.
        proxy_jump: Алиас jump-хоста (ProxyJump); пусто — прямой вход.
        extra: Прочие опции ssh как «Ключ: Значение», пишутся в блок как есть.
    """
    spec = ManagedHost(
        alias=alias,
        hostname=hostname,
        user=user,
        port=port,
        identity_file=identity_file,
        proxy_jump=proxy_jump,
        extra=extra or {},
    )
    return await anyio.to_thread.run_sync(services.add_host, spec)


@router.tool(
    title="Удалить хост",
    tags={"sshconfig"},
    # Убирает managed-блок и, по флагам, записи known_hosts и секрет; повторный
    # вызов уже не найдёт хост и завершится ошибкой — потому не idempotent.
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
    ),
)
async def remove_host(alias: NonEmptyStr, forget_known: bool = True, drop_secret: bool = False) -> RemoveHostResult:
    """Удалить хост из managed-файла и подчистить его след.

    Трогает только записи, добавленные сервером: хост из ручного конфига —
    ошибка. По умолчанию заодно чистит known_hosts.

    Args:
        alias: Алиас, добавленный ранее add_host.
        forget_known: Удалить и записи этого хоста из known_hosts.
        drop_secret: Удалить и файл ~/.ssh/<alias>.secret с паролем sudo.
    """
    return await anyio.to_thread.run_sync(services.remove_host, alias, forget_known, drop_secret)


@router.tool(
    title="Забыть ключ хоста",
    tags={"sshconfig"},
    # Чистит только known_hosts; конфиг не трогает. Повтор ничего не меняет.
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
    ),
)
async def forget_host(target: NonEmptyStr) -> ForgetHostResult:
    """Удалить записи known_hosts для хоста, не трогая конфиг.

    Для случая «Remote host identification has changed»: следующий вход примет
    новый ключ. Конфиг и секреты остаются на месте.

    Args:
        target: Алиас из конфига (чистится его hostname) или сам hostname/IP.
    """
    return await anyio.to_thread.run_sync(services.forget_host, target)


@router.tool(
    title="Раздать ключ хосту",
    tags={"sshconfig"},
    # Ходит на хост и правит его authorized_keys; ssh-copy-id повторно уже
    # установленный ключ пропускает, так что повтор безопасен.
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
    ),
)
async def copy_id(alias: NonEmptyStr, identity: str = "") -> CopyIdResult:
    """Установить публичный ключ на хост (ssh-copy-id), пароль — из секрета.

    Пароль берётся из ~/.ssh/<alias>.secret и отдаётся хосту через sshpass, не
    попадая в argv или лог; нужны установленные ssh-copy-id и sshpass. После
    этого вход идёт по ключу, а sudo — с тем же секретом.

    Args:
        alias: Алиас из ~/.ssh/config.
        identity: Путь к публичному ключу (-i); пусто — ключ по умолчанию.
    """
    return await anyio.to_thread.run_sync(services.copy_id, alias, identity)
