"""Схемы ответов роутера llms.txt."""

from typing import Literal

from pydantic import BaseModel, Field

# Где искать: в оглавлении или во всей документации одним файлом.
SearchScope = Literal["index", "full"]


class IndexEntry(BaseModel):
    """Ссылка из оглавления `llms.txt`."""

    title: str
    url: str = Field(description="абсолютный адрес; передавать в llms_fetch как есть")
    description: str
    section: str = Field(description="заголовок раздела индекса; пусто вне разделов")


class LlmsIndex(BaseModel):
    """Ответ `llms_index`: разобранное оглавление."""

    url: str = Field(description="адрес самого индекса")
    title: str
    summary: str
    entries: list[IndexEntry]
    full_url: str = Field(description="адрес `llms-full.txt`, если индекс его называет")


class SearchHit(BaseModel):
    """Одно совпадение `llms_search`."""

    source: SearchScope
    title: str = Field(description="запись индекса или заголовок раздела full-файла")
    url: str
    text: str = Field(description="описание записи или фрагмент раздела")
    truncated: bool = Field(description="фрагмент обрезан по потолку")


class SearchResult(BaseModel):
    """Ответ `llms_search`."""

    query: str
    scope: SearchScope
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
