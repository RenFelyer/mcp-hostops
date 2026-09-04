"""Сервис роутера llms.txt: скачивание с кэшем, проверка домена, разбор, поиск.

Сеть здесь асинхронная (`httpx2.AsyncClient`), поток не нужен. Всё скачанное
ложится в кэш на диске с TTL: индекс и страницы перечитываются редко, а
`llms-full.txt` бывает десятки мегабайт и целиком в ответ не идёт никогда —
только совпадения по разделам.

Домен проверяется мусорным URL рядом с индексом: часть сайтов на любой путь
отдаёт 200 с HTML-оболочкой, и тогда «индекс» — тоже мусор, сколько бы ни весил.
"""

import hashlib
import json
import re
import time
from itertools import pairwise
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx2
from pydantic import BaseModel

from ...core.config import Settings, get_settings
from ...core.errors import UserError
from .schemas import IndexEntry, LlmsIndex, Page, SearchHit, SearchResult, SearchScope

_HTTP_ERROR = 400  # с этого кода ответ — ошибка
_INDEX_NAME = "llms.txt"
_FULL_NAME = "llms-full.txt"
_LINK = re.compile(r"\[(?P<title>[^\]]*)\]\((?P<url>[^)\s]+)\)\s*:?\s*(?P<desc>.*)")
_HEADING = re.compile(r"^#{1,3} ", re.MULTILINE)


class Fetched(BaseModel):
    """Скачанный ресурс: тело и тип содержимого."""

    status: int
    content_type: str
    body: bytes

    @property
    def text(self) -> str:
        """Тело текстом; байты вне UTF-8 заменяются, а не роняют вызов."""
        return self.body.decode("utf-8", "replace")


def make_client(s: Settings) -> httpx2.AsyncClient:
    """HTTP-клиент на один вызов инструмента; тесты подменяют транспорт."""
    return httpx2.AsyncClient(
        timeout=s.llms_timeout,
        follow_redirects=True,
        headers={"User-Agent": "mcp-openssh-connector (llms.txt reader)"},
    )


def index_url(source: str) -> str:
    """Адрес индекса из того, что назвал клиент: домен, домен с путём или URL."""
    url = source if "://" in source else f"https://{source}"
    if not url.endswith(".txt"):
        url = url.rstrip("/") + "/" + _INDEX_NAME
    return url


def _cache_paths(url: str, s: Settings) -> tuple[Path, Path]:
    host = urlsplit(url).hostname or "unknown"
    key = hashlib.sha256(url.encode()).hexdigest()[:24]
    base = s.llms_cache_dir / host / key
    return base.with_suffix(".body"), base.with_suffix(".meta")


def _cached(url: str, s: Settings) -> Fetched | None:
    body_path, meta_path = _cache_paths(url, s)
    try:
        if time.time() - meta_path.stat().st_mtime > s.llms_cache_ttl:
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return Fetched(**meta, body=body_path.read_bytes())
    except (OSError, ValueError, TypeError):
        return None


def _store(url: str, fetched: Fetched, s: Settings) -> None:
    body_path, meta_path = _cache_paths(url, s)
    try:
        body_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_bytes(fetched.body)
        meta_path.write_text(json.dumps(fetched.model_dump(exclude={"body"})), encoding="utf-8")
    except OSError:
        pass


async def fetch(url: str, s: Settings) -> Fetched:
    """Скачать ресурс с кэшем и потолком размера.

    Код ответа возвращается, а не превращается в ошибку: проверке домена нужен
    и 404. Ошибка сети или превышение потолка — `UserError`.

    Raises:
        UserError: сеть недоступна, таймаут или тело больше `llms_max_bytes`.
    """
    if (hit := _cached(url, s)) is not None:
        return hit
    body = bytearray()
    try:
        async with make_client(s) as client, client.stream("GET", url) as response:
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > s.llms_max_bytes:
                    raise UserError(f"{url}: больше потолка {s.llms_max_bytes} байт")
            fetched = Fetched(
                status=response.status_code,
                content_type=response.headers.get("content-type", ""),
                body=bytes(body),
            )
    except httpx2.HTTPError as err:
        raise UserError(f"не удалось скачать {url}: {err}") from err
    _store(url, fetched, s)
    return fetched


async def require_honest(url: str, s: Settings) -> None:
    """Убедиться, что домен не отдаёт 200 на любой путь.

    Мусорный адрес берётся в каталоге самого `url`; имя стабильно, чтобы ответ
    кэшировался вместе с остальным.

    Raises:
        UserError: SPA-заглушка — на мусорный путь пришёл успешный ответ.
    """
    junk = urljoin(url, "zzz-nope-12345.txt")
    probe = await fetch(junk, s)
    if probe.status < _HTTP_ERROR:
        raise UserError(
            f"{urlsplit(url).hostname}: отдаёт {probe.status} на любой путь — это "
            "SPA-заглушка, llms.txt на нём верить нельзя"
        )


async def fetch_ok(url: str, s: Settings) -> Fetched:
    """Скачать с проверкой домена; неуспешный код ответа — ошибка вызова.

    Raises:
        UserError: SPA-заглушка, ошибка сети или код ответа не 2xx/3xx.
    """
    await require_honest(url, s)
    fetched = await fetch(url, s)
    if fetched.status >= _HTTP_ERROR:
        raise UserError(f"{url}: HTTP {fetched.status}")
    return fetched


def parse_index(text: str, base_url: str) -> LlmsIndex:
    """Разобрать `llms.txt`: заголовок, аннотация, ссылки по разделам.

    Формат: `# Заголовок`, `> аннотация`, `## Раздел`, строки `- [имя](url):
    описание`. Относительные адреса раскрываются от `base_url`.
    """
    title = summary = section = full_url = ""
    entries: list[IndexEntry] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("# ") and not title:
            title = line[2:].strip()
        elif line.startswith("> ") and not summary:
            summary = line[2:].strip()
        elif line.startswith("## "):
            section = line[3:].strip()
        elif (m := _LINK.search(line)) and line.startswith(("-", "*")):
            url = urljoin(base_url, m["url"])
            if url.endswith(_FULL_NAME):
                full_url = url
            entries.append(
                IndexEntry(
                    title=m["title"].strip(),
                    url=url,
                    description=m["desc"].strip(),
                    section=section,
                )
            )
    return LlmsIndex(url=base_url, title=title, summary=summary, entries=entries, full_url=full_url)


async def load_index(source: str) -> LlmsIndex:
    """Индекс домена: проверка домена, скачивание, разбор."""
    s = get_settings()
    url = index_url(source)
    return parse_index((await fetch_ok(url, s)).text, url)


def _matches(text: str, words: list[str]) -> bool:
    lowered = text.lower()
    return all(word in lowered for word in words)


def sections(text: str) -> list[tuple[str, str]]:
    """Разрезать markdown по заголовкам до третьего уровня.

    Returns:
        Пары (заголовок, текст раздела с заголовком); текст до первого
        заголовка идёт разделом с пустым заголовком.
    """
    bounds = [0, *(m.start() for m in _HEADING.finditer(text)), len(text)]
    result = []
    for begin, end in pairwise(bounds):
        chunk = text[begin:end].strip()
        if not chunk:
            continue
        first = chunk.split("\n", 1)[0]
        heading = first.lstrip("#").strip() if first.startswith("#") else ""
        result.append((heading, chunk))
    return result


async def search(source: str, query: str, scope: SearchScope) -> SearchResult:
    """Найти слова запроса (все, без учёта регистра) в индексе или в full-файле.

    Raises:
        UserError: пустой запрос, домен нечестный или файла нет.
    """
    s = get_settings()
    words = query.lower().split()
    if not words:
        raise UserError("пустой запрос")
    index = await load_index(source)
    if scope == "index":
        hits = [
            SearchHit(
                source=scope,
                title=entry.title,
                url=entry.url,
                text=entry.description,
                truncated=False,
            )
            for entry in index.entries
            if _matches(f"{entry.title} {entry.description} {entry.url}", words)
        ]
    else:
        full_url = index.full_url or urljoin(index.url, _FULL_NAME)
        text = (await fetch_ok(full_url, s)).text
        hits = [
            SearchHit(
                source=scope,
                title=heading,
                url=full_url,
                text=chunk[: s.llms_hit_chars],
                truncated=len(chunk) > s.llms_hit_chars,
            )
            for heading, chunk in sections(text)
            if _matches(chunk, words)
        ]
    return SearchResult(query=query, scope=scope, total=len(hits), hits=hits[: s.llms_max_hits])


async def fetch_page(url: str, offset: int) -> Page:
    """Страница по адресу из индекса, кусок с `offset` длиной `llms_page_chars`.

    Raises:
        UserError: домен нечестный, страницы нет или сеть недоступна.
    """
    s = get_settings()
    fetched = await fetch_ok(url, s)
    text = fetched.text
    end = offset + s.llms_page_chars
    return Page(
        url=url,
        content_type=fetched.content_type,
        length=len(text),
        offset=offset,
        text=text[offset:end],
        next_offset=end if end < len(text) else None,
    )
