"""Сервис роутера llms.txt: скачивание с кэшем, проверка, разбор, поиск, реестр.

Сеть здесь асинхронная (`httpx2.AsyncClient`), поток не нужен. Всё скачанное
ложится в кэш на диске с TTL: индекс и страницы перечитываются редко, а
`llms-full.txt` бывает десятки мегабайт и целиком в ответ не идёт никогда —
только совпадения по разделам.

Часть сайтов на любой путь отдаёт 200 с HTML-оболочкой, при этом настоящие
файлы у них приходят как text/plain. Поэтому заглушкой считается только ресурс,
который сам выглядит HTML, когда домен ещё и отвечает 200 на мусорный путь
рядом с ним; текстовый ответ — документ, что бы домен ни отдавал на мусор.
"""

import contextlib
import hashlib
import json
import re
import time
from itertools import pairwise
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import anyio
import httpx2
from pydantic import BaseModel

from ...core import store
from ...core.config import Settings, get_settings
from ...core.errors import UserError
from . import sources
from .schemas import (
    IndexEntry,
    KnownSource,
    LlmsIndex,
    Page,
    SearchHit,
    SearchResult,
    SearchScope,
    SourcesResult,
    SourceState,
    SourceStatus,
)
from .sources import VARIANTS

_HTTP_ERROR = 400  # с этого кода ответ — ошибка
_JUNK_NAME = "zzz-nope-12345.txt"  # стабильное имя, чтобы проба кэшировалась
_INDEX_NAME = "llms.txt"
_FULL_NAME = "llms-full.txt"
_LINK = re.compile(r"\[(?P<title>[^\]]*)\]\((?P<url>[^)\s]+)\)\s*:?\s*(?P<desc>.*)")
_HEADING = re.compile(r"^#{1,3} ", re.MULTILINE)


class Fetched(BaseModel):
    """Скачанный ресурс: код, тип содержимого и тело (у HEAD — пустое)."""

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


def index_url(source: str, s: Settings) -> str:
    """Адрес индекса: из реестра по домену, иначе из того, что назвал клиент.

    Клиент может назвать домен (`docs.astral.sh/uv`), домен с путём или адрес
    файла целиком.
    """
    if (known := sources.find(source, s)) is not None:
        return known.index
    url = source if "://" in source else f"https://{source}"
    if not url.endswith(".txt"):
        url = url.rstrip("/") + "/" + _INDEX_NAME
    return url


def domain_of(url: str, s: Settings) -> str:
    """Имя источника для ответов: домен из реестра или хост адреса."""
    if (known := sources.find(url, s)) is not None:
        return known.domain
    return urlsplit(url).hostname or url


def _cache_paths(method: str, url: str, s: Settings) -> tuple[Path, Path]:
    host = urlsplit(url).hostname or "unknown"
    key = hashlib.sha256(f"{method} {url}".encode()).hexdigest()[:24]
    base = s.llms_cache_dir / host / key
    return base.with_suffix(".body"), base.with_suffix(".meta")


def _cached(method: str, url: str, s: Settings) -> Fetched | None:
    body_path, meta_path = _cache_paths(method, url, s)
    try:
        if time.time() - meta_path.stat().st_mtime > s.llms_cache_ttl:
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return Fetched(**meta, body=body_path.read_bytes())
    except (OSError, ValueError, TypeError):
        return None


def _store(method: str, url: str, fetched: Fetched, s: Settings) -> None:
    body_path, meta_path = _cache_paths(method, url, s)
    with contextlib.suppress(OSError):
        body_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_bytes(fetched.body)
        meta_path.write_text(json.dumps(fetched.model_dump(exclude={"body"})), encoding="utf-8")


async def fetch(url: str, s: Settings, method: str = "GET", *, cache: bool = True) -> Fetched:
    """Скачать ресурс с кэшем и потолком размера.

    Код ответа возвращается, а не превращается в ошибку: проверке домена нужен
    и 404. HEAD тела не тянет — им проверяется наличие файлов. `cache=False`
    минует кэш на диске: так проверяют, что источник жив сейчас.

    Raises:
        UserError: сеть недоступна, таймаут или тело больше `llms_max_bytes`.
    """
    if cache and (hit := _cached(method, url, s)) is not None:
        return hit
    body = bytearray()
    try:
        async with make_client(s) as client, client.stream(method, url) as response:
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
    if cache:
        _store(method, url, fetched, s)
    return fetched


def _looks_html(fetched: Fetched) -> bool:
    head = fetched.body.lstrip()[:15].lower()
    return fetched.content_type.lower().startswith("text/html") or head.startswith((b"<!doctype", b"<html"))


async def fetch_ok(url: str, s: Settings) -> Fetched:
    """Скачать документ; неуспешный код или SPA-заглушка — ошибка вызова.

    HTML в ответе сам по себе не приговор: честный домен может отдать HTML
    страницу. Приговор — HTML плюс успешный ответ на мусорный путь рядом.

    Raises:
        UserError: ошибка сети, код ответа не 2xx/3xx или SPA-заглушка.
    """
    fetched = await fetch(url, s)
    if fetched.status >= _HTTP_ERROR:
        raise UserError(f"{url}: HTTP {fetched.status}")
    if _looks_html(fetched):
        probe = await fetch(urljoin(url, _JUNK_NAME), s)
        if probe.status < _HTTP_ERROR:
            raise UserError(
                f"{url}: пришла HTML-оболочка, а домен отвечает {probe.status} на "
                "любой путь — это SPA-заглушка, не документ"
            )
    return fetched


async def present_variants(index: str, s: Settings) -> list[str]:
    """Какие из `VARIANTS` лежат рядом с индексом (по HEAD, параллельно).

    Домен, отвечающий 200 на всё, отдаёт на отсутствующий файл HTML-оболочку,
    поэтому «есть» — это успешный код и не HTML в типе содержимого.
    """
    found: dict[str, bool] = {}

    async def probe(name: str) -> None:
        try:
            head = await fetch(urljoin(index, name), s, "HEAD")
            found[name] = head.status < _HTTP_ERROR and not _looks_html(head)
        except UserError:
            found[name] = False

    async with anyio.create_task_group() as tg:
        for name in VARIANTS:
            tg.start_soon(probe, name)
    return [name for name in VARIANTS if found.get(name)]


def parse_index(text: str, base_url: str) -> LlmsIndex:
    """Разобрать `llms.txt`: заголовок, аннотация, ссылки по разделам.

    Формат: `# Заголовок`, `> аннотация`, `## Раздел`, строки `- [имя](url):
    описание`. Относительные адреса раскрываются от `base_url`. Варианты
    файлов здесь не проверяются — это сеть, см. `present_variants`.
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
    return LlmsIndex(
        url=base_url,
        title=title,
        summary=summary,
        entries=entries,
        full_url=full_url,
        variants=[],
    )


async def load_index(source: str, s: Settings) -> LlmsIndex:
    """Индекс источника: проверка домена, скачивание, разбор, варианты рядом."""
    url = index_url(source, s)
    index = parse_index((await fetch_ok(url, s)).text, url)
    index.variants = await present_variants(url, s)
    return index


async def _check(known: KnownSource, s: Settings) -> SourceStatus:
    """Жив ли источник сейчас: HEAD индекса мимо кэша, при HTML — мусорная проба."""
    state: SourceState = "ok"
    detail = ""
    try:
        head = await fetch(known.index, s, "HEAD", cache=False)
        if head.status >= _HTTP_ERROR:
            state, detail = "unavailable", f"HTTP {head.status}"
        elif _looks_html(head):
            junk = await fetch(urljoin(known.index, _JUNK_NAME), s, "HEAD", cache=False)
            state = "stub" if junk.status < _HTTP_ERROR else "unavailable"
            detail = "вместо индекса HTML-оболочка"
    except UserError as err:
        state, detail = "unavailable", str(err)
    return SourceStatus(**known.model_dump(), state=state, detail=detail)


def _forget_status(s: Settings) -> None:
    """Сбросить итоги проверки: состав реестра изменился."""
    with contextlib.suppress(OSError):
        s.llms_status_file.unlink()


async def verify_sources(s: Settings, *, refresh: bool = False) -> SourcesResult:
    """Все источники с итогом проверки; итоги лежат в runtime-каталоге до TTL.

    Runtime-каталог живёт до перезагрузки машины, но переживает перезапуск
    сервера: между сессиями источники заново не опрашиваются. Опрашиваются все
    разом, мимо кэша скачивания.
    """
    saved = store.load(s.llms_status_file) or {}
    checked_at = float(saved.get("checked_at", 0))
    known = sources.all_sources(s)
    rows = saved.get("sources", {})
    fresh = not refresh and time.time() - checked_at < s.llms_status_ttl and all(item.domain in rows for item in known)
    if fresh:
        statuses = [SourceStatus(**item.model_dump(), **rows[item.domain]) for item in known]
        return SourcesResult(
            checked_ago=time.time() - checked_at,
            sources=statuses,
            variants=list(VARIANTS),
        )

    results: dict[str, SourceStatus] = {}

    async def check(item: KnownSource) -> None:
        results[item.domain] = await _check(item, s)

    async with anyio.create_task_group() as tg:
        for item in known:
            tg.start_soon(check, item)
    statuses = [results[item.domain] for item in known]
    with contextlib.suppress(OSError):
        store.save(
            s.llms_status_file,
            {
                "checked_at": time.time(),
                "sources": {st.domain: {"state": st.state, "detail": st.detail} for st in statuses},
            },
        )
    return SourcesResult(checked_ago=0.0, sources=statuses, variants=list(VARIANTS))


async def add_source(domain: str, covers: str, index: str | None, s: Settings) -> SourceStatus:
    """Проверить источник по сети, добавить в пользовательский файл и вернуть.

    Индекс скачивается целиком: он должен быть текстом с хотя бы одной ссылкой.
    `llms-full.txt` рядом определяется сам.

    Raises:
        UserError: домен уже есть, индекса нет, это заглушка или в нём нет ссылок.
        OSError: файл источников не записался.
    """
    if sources.find(domain, s) is not None:
        raise UserError(f"источник {domain!r} уже есть")
    url = index_url(index or domain, s)
    parsed = parse_index((await fetch_ok(url, s)).text, url)
    if not parsed.entries:
        raise UserError(f"{url}: в индексе нет ни одной ссылки — это не llms.txt")
    variants = await present_variants(url, s)
    full = urljoin(url, _FULL_NAME) if _FULL_NAME in variants else ""
    known = KnownSource(domain=domain, index=url, covers=covers, full=full)
    sources.add(known, s)
    _forget_status(s)
    return SourceStatus(**known.model_dump(), state="ok", detail="")


def remove_source(domain: str, s: Settings) -> KnownSource:
    """Удалить пользовательский источник; встроенный удалить нельзя.

    Raises:
        UserError: источник встроенный или его нет.
        OSError: файл источников не записался.
    """
    removed = sources.remove(domain, s)
    _forget_status(s)
    return removed


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


def _index_hits(index: LlmsIndex, words: list[str], s: Settings) -> list[SearchHit]:
    domain = domain_of(index.url, s)
    return [
        SearchHit(
            domain=domain,
            scope="index",
            title=entry.title,
            url=entry.url,
            text=entry.description,
            truncated=False,
        )
        for entry in index.entries
        if _matches(f"{entry.title} {entry.description} {entry.url}", words)
    ]


async def _full_hits(index: LlmsIndex, words: list[str], s: Settings) -> list[SearchHit]:
    full_url = index.full_url or urljoin(index.url, _FULL_NAME)
    text = (await fetch_ok(full_url, s)).text
    return [
        SearchHit(
            domain=domain_of(index.url, s),
            scope="full",
            title=heading,
            url=full_url,
            text=chunk[: s.llms_hit_chars],
            truncated=len(chunk) > s.llms_hit_chars,
        )
        for heading, chunk in sections(text)
        if _matches(chunk, words)
    ]


async def search(query: str, source: str | None, scope: SearchScope) -> SearchResult:
    """Найти слова запроса (все, без учёта регистра).

    Без `source` — по оглавлениям всех источников реестра, которые последняя
    проверка признала живыми; остальные и упавшие по пути называются в
    `skipped`.

    Raises:
        UserError: пустой запрос, `full` без источника, либо названный источник
            нечестный или без файла.
    """
    s = get_settings()
    words = query.lower().split()
    if not words:
        raise UserError("пустой запрос")
    if source is None and scope == "full":
        raise UserError("поиск по full — только с указанным источником")

    hits: list[SearchHit] = []
    searched: list[str] = []
    skipped: list[str] = []
    if source is not None:
        targets = [source]
    else:
        verified = (await verify_sources(s)).sources
        targets = [st.domain for st in verified if st.state == "ok"]
        skipped += [f"{st.domain}: {st.detail}" for st in verified if st.state != "ok"]
    for target in targets:
        try:
            index = await load_index(target, s)
            found = _index_hits(index, words, s) if scope == "index" else await _full_hits(index, words, s)
        except UserError as err:
            if source is not None:
                raise
            skipped.append(f"{target}: {err}")
            continue
        searched.append(target)
        hits += found
    return SearchResult(
        query=query,
        scope=scope,
        searched=searched,
        skipped=skipped,
        total=len(hits),
        hits=hits[: s.llms_max_hits],
    )


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
