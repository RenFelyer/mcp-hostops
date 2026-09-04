"""Схемы ответов роутера llms.txt."""

from typing import Literal

from pydantic import BaseModel, Field

# Где искать: в оглавлении или во всей документации одним файлом.
SearchScope = Literal["index", "full"]

# Итог проверки источника: индекс отвечает текстом; не отвечает; HTML-заглушка.
SourceState = Literal["ok", "unavailable", "stub"]


class KnownSource(BaseModel):
    """Источник из реестра: где индекс и что он покрывает."""

    domain: str = Field(description="как называть источник в llms_index/llms_search")
    index: str = Field(description="адрес `llms.txt`")
    covers: str
    full: str = Field(default="", description="адрес `llms-full.txt`, если известен")
    full_size: str = Field(default="", description="размер full-файла, чтобы не читать целиком")
    default: bool = Field(default=False, description="встроенный; удалить нельзя")


class SourceStatus(KnownSource):
    """Источник с итогом последней проверки."""

    state: SourceState
    detail: str = Field(description="код ответа или причина; пусто при ok")


class SourcesResult(BaseModel):
    """Ответ `llms_sources`: проверенный реестр и известные имена вариантов."""

    checked_ago: float = Field(description="возраст проверки, секунды")
    sources: list[SourceStatus]
    variants: list[str] = Field(description="имена файлов, которые llms_index ищет на домене")


class IndexEntry(BaseModel):
    """Ссылка из оглавления `llms.txt`."""

    title: str
    url: str = Field(description="абсолютный адрес; передавать в llms_fetch как есть")
    description: str
    section: str = Field(description="заголовок раздела индекса; пусто вне разделов")


class LlmsIndex(BaseModel):
    """Ответ `llms_index`: разобранное оглавление и что ещё лежит на домене."""

    url: str = Field(description="адрес самого индекса")
    title: str
    summary: str
    entries: list[IndexEntry]
    full_url: str = Field(description="адрес `llms-full.txt`, если индекс его называет")
    variants: list[str] = Field(description="варианты файлов, найденные рядом с индексом")


class SearchHit(BaseModel):
    """Одно совпадение `llms_search`."""

    domain: str
    scope: SearchScope
    title: str = Field(description="запись индекса или заголовок раздела full-файла")
    url: str
    text: str = Field(description="описание записи или фрагмент раздела")
    truncated: bool = Field(description="фрагмент обрезан по потолку")


class SearchResult(BaseModel):
    """Ответ `llms_search`."""

    query: str
    scope: SearchScope
    searched: list[str] = Field(description="домены, по которым искали")
    skipped: list[str] = Field(description="домены, пропущенные из-за ошибки, с причиной")
    total: int = Field(description="совпадений всего; в hits — не больше потолка")
    hits: list[SearchHit]


class Page(BaseModel):
    """Ответ `llms_fetch`: кусок страницы с позиции offset."""

    url: str
    content_type: str
    length: int = Field(description="символов на странице всего")
    offset: int
    text: str
    next_offset: int | None = Field(description="откуда читать дальше; null — конец")
