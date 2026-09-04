"""Обработчики роутера llms.txt: индекс, поиск, страница.

Порядок работы тот же, что руками: индекс целиком, адрес страницы из него как
есть, `llms-full.txt` — только поиском по разделам, никогда целиком.
"""

from typing import Annotated

from fastmcp import FastMCP
from mcp_types import ToolAnnotations
from pydantic import Field

from ...core.schemas import NonEmptyStr
from .schemas import LlmsIndex, Page, SearchResult, SearchScope
from .services import fetch_page, load_index, search

router: FastMCP = FastMCP(name="llms", on_duplicate="error")

_READING = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)


@router.tool(title="Индекс llms.txt", tags={"llms"}, annotations=_READING)
async def llms_index(source: NonEmptyStr) -> LlmsIndex:
    """Оглавление документации инструмента с его домена (`llms.txt`).

    Сначала домен проверяется мусорным адресом: сайт, отдающий 200 на любой
    путь, — SPA-заглушка, и его индекс — тоже мусор; такой вызов завершается
    ошибкой. Нет темы в индексе — источник её не покрывает, адреса не угадывать.

    Args:
        source: Домен (`docs.astral.sh/uv`) или адрес индекса целиком.
    """
    return await load_index(source)


@router.tool(title="Поиск по llms.txt", tags={"llms"}, annotations=_READING)
async def llms_search(source: NonEmptyStr, query: NonEmptyStr, scope: SearchScope = "index") -> SearchResult:
    """Найти слова запроса в оглавлении или во всей документации домена.

    Все слова должны встретиться, регистр не важен. Поиск по full — замена
    grep по `llms-full.txt`: файл кэшируется, в ответ идут только совпавшие
    разделы, обрезанные по потолку.

    Args:
        source: Домен или адрес индекса, как у llms_index.
        query: Слова через пробел.
        scope: index — по названиям и описаниям ссылок; full — по разделам
            `llms-full.txt`, когда темы в оглавлении не видно.
    """
    return await search(source, query, scope)


@router.tool(title="Страница из llms.txt", tags={"llms"}, annotations=_READING)
async def llms_fetch(url: NonEmptyStr, offset: Annotated[int, Field(ge=0)] = 0) -> Page:
    """Страница документации целиком, кусками по потолку из настроек.

    Адрес брать из индекса как есть: сегмент языка, версия и `.md` на конце
    угадываются плохо. Следующий кусок — по next_offset из ответа.

    Args:
        url: Абсолютный адрес страницы из llms_index или llms_search.
        offset: Позиция в символах, с которой отдавать.
    """
    return await fetch_page(url, offset)
