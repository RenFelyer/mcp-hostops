"""Сервис роутера llms.txt: реестр источников, скачивание с кэшем, разбор, поиск.

Реестр — встроенный список `LLMS_DEFAULT_SOURCES` (проверенное, удалить нельзя)
плюс пользовательский JSON-файл `llms_sources_file`, который переживает
перезапуск сервера. Флаг `default` в файл не пишется и из него не читается,
чтобы правка руками не сделала источник неудаляемым; домены, совпадающие со
встроенными, из файла пропускаются.

Сеть асинхронная (`httpx2.AsyncClient`). Один вызов инструмента — одна
`Session`: HTTP-клиент с общим пулом соединений, чтобы пробы вариантов и поиск
по многим источникам не открывали TLS-соединение на каждый запрос. Всё
скачанное ложится в кэш на диске с TTL: индекс и страницы перечитываются редко,
а `llms-full.txt` бывает десятки мегабайт и целиком в ответ не идёт никогда —
только совпадения по разделам. Диск и разбор больших файлов уходят в поток,
чтобы не останавливать цикл событий.

Сервер ходит только по https на публичные имена, разрешающиеся в публичные
адреса: адрес выбирает клиент, и без этого ограничения инструмент стал бы окном
во внутреннюю сеть. Правило применяется хуком к каждому запросу, в том числе к
каждому ходу переадресации, до его отправки. Адрес не закрепляется: имя,
которое сменит ответ DNS между проверкой и соединением, проверка не поймает.

Часть сайтов на любой путь отдаёт 200 с HTML-оболочкой, при этом настоящие
файлы у них приходят как text/plain. Поэтому заглушкой считается только ресурс,
который сам выглядит HTML, когда домен ещё и отвечает 200 на мусорный путь
рядом с ним; текстовый ответ — документ, что бы домен ни отдавал на мусор.
"""

import contextlib
import hashlib
import ipaddress
import logging
import re
import socket
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from itertools import pairwise
from pathlib import Path
from time import time
from types import TracebackType
from typing import Self
from urllib.parse import urljoin, urlsplit

import anyio
import anyio.to_thread
import httpx2
from pydantic import BaseModel, TypeAdapter, ValidationError

from ...core import store
from ...core.config.constants import (
    LLMS_DEFAULT_SOURCES,
    LLMS_FULL_NAME,
    LLMS_INDEX_NAME,
    LLMS_JUNK_NAME,
    LLMS_REDIRECTS,
    LLMS_VARIANTS,
)
from ...core.config.environment import Settings, get_settings
from ...core.errors import UserError
from ...core.schemas import KnownSource
from ...core.utils.parallel import gather
from .schemas import (
    IndexEntry,
    LlmsIndex,
    Page,
    SearchHit,
    SearchResult,
    SearchScope,
    SourcesResult,
    SourceState,
    SourceStatus,
    SourceVerdict,
    Variant,
)

log = logging.getLogger(__name__)

_LINK = re.compile(r"\[(?P<title>[^\]]*)\]\((?P<url>[^)\s]+)\)\s*:?\s*(?P<desc>.*)")
_HEADING = re.compile(r"#{1,3} ")
_HOSTNAME = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))*$")
_VERDICTS = TypeAdapter(dict[str, SourceVerdict])  # содержимое файла итогов по доменам
_SOURCES = TypeAdapter(list[KnownSource])  # содержимое файла пользовательских источников


class Fetched(BaseModel):
    """Скачанный ресурс: код, тип и размер содержимого, тело (у HEAD — пустое)."""

    status: int
    content_type: str
    content_length: int | None
    body: bytes

    @property
    def ok(self) -> bool:
        """Код ответа не ошибка (меньше 400)."""
        return self.status < HTTPStatus.BAD_REQUEST

    @property
    def cacheable(self) -> bool:
        """Ответ стоит помнить: успех и устойчивые отказы, но не сбои сервера и не 429."""
        return self.status < HTTPStatus.INTERNAL_SERVER_ERROR and self.status != HTTPStatus.TOO_MANY_REQUESTS

    @property
    def text(self) -> str:
        """Тело текстом; байты вне UTF-8 заменяются, а не роняют вызов."""
        return self.body.decode("utf-8", "replace")

    @property
    def is_html(self) -> bool:
        """Похоже на HTML по типу содержимого или по началу тела."""
        head = self.body.lstrip()[:15].lower()
        return self.content_type.lower().startswith("text/html") or head.startswith((b"<!doctype", b"<html"))


def make_client(s: Settings, guard: Callable[[httpx2.Request], Awaitable[None]]) -> httpx2.AsyncClient:
    """HTTP-клиент на один вызов инструмента; `guard` зовётся перед каждым запросом.

    Тесты подменяют функцию, чтобы подставить транспорт, но хук обязаны
    сохранить: без него проверка адреса не работает.
    """
    return httpx2.AsyncClient(
        timeout=s.llms_timeout,
        follow_redirects=True,
        max_redirects=LLMS_REDIRECTS,
        headers={"User-Agent": "mcp-openssh-connector (llms.txt reader)"},
        event_hooks={"request": [guard]},
    )


def check_url(url: str) -> str:
    """Адрес, по которому сервер готов ходить: https и публичное имя хоста.

    Returns:
        Имя хоста в нижнем регистре — для проверки, куда оно разрешается.

    Raises:
        UserError: не https, имя хоста не доменное, localhost или непубличный IP.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https":
        raise UserError(f"{url}: сервер ходит только по https")
    with contextlib.suppress(ValueError):
        if not ipaddress.ip_address(host).is_global:
            raise UserError(f"{url}: непубличный адрес")
    if not _HOSTNAME.match(host) or host == "localhost" or host.endswith(".localhost"):
        raise UserError(f"{url}: адрес должен вести на публичный домен")
    return host


async def resolve_public(host: str) -> None:
    """Имя должно разрешаться, и только в публичные адреса.

    Имя вида `10-0-0-1.nip.io` или внутреннее имя из search-домена проходит
    проверку по виду, но ведёт внутрь сети; ловится здесь. IP-литерал уже
    проверен в `check_url`.

    Raises:
        UserError: имя не разрешается или хотя бы один его адрес непубличный.
    """
    with contextlib.suppress(ValueError):
        ipaddress.ip_address(host)
        return
    try:
        found = await anyio.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as err:
        raise UserError(f"{host}: имя не разрешается ({err})") from err
    for *_, sockaddr in found:
        try:
            public = ipaddress.ip_address(sockaddr[0]).is_global
        except ValueError:
            public = False
        if not public:
            raise UserError(f"{host}: имя ведёт на непубличный адрес")


def custom_sources(s: Settings) -> list[KnownSource]:
    """Пользовательские источники из файла; битый или отсутствующий файл — пусто."""
    data = store.load(s.llms_sources_file) or {}
    builtin = {known.domain for known in LLMS_DEFAULT_SOURCES}
    try:
        items = _SOURCES.validate_python(data.get("sources", []))
    except ValidationError:
        return []
    return [item.model_copy(update={"default": False}) for item in items if item.domain not in builtin]


def _save_sources(items: list[KnownSource], s: Settings) -> None:
    store.save(s.llms_sources_file, {"sources": [i.model_dump(exclude={"default"}) for i in items]})


def all_sources(s: Settings) -> list[KnownSource]:
    """Встроенные, затем пользовательские; домены не повторяются."""
    return [*LLMS_DEFAULT_SOURCES, *custom_sources(s)]


def find_source(domain: str, s: Settings) -> KnownSource | None:
    """Источник по домену или адресу индекса."""
    return next((known for known in all_sources(s) if domain in (known.domain, known.index)), None)


def _forget_status(s: Settings) -> None:
    """Сбросить итоги проверки: состав реестра изменился."""
    with contextlib.suppress(OSError):
        s.llms_status_file.unlink()


def remove_source(domain: str, s: Settings) -> KnownSource:
    """Удалить пользовательский источник; встроенный удалить нельзя.

    Raises:
        UserError: источник встроенный или его нет.
        OSError: файл источников не записался.
    """
    found = find_source(domain, s)
    if found is None:
        raise UserError(f"источника {domain!r} нет")
    if found.default:
        raise UserError(f"источник {domain!r} встроенный, удалить нельзя")
    _save_sources([i for i in custom_sources(s) if i.domain != found.domain], s)
    _forget_status(s)
    return found


def index_url(source: str, s: Settings) -> str:
    """Адрес индекса: из реестра по домену, иначе из того, что назвал клиент.

    Клиент может назвать домен (`docs.astral.sh/uv`), домен с путём или адрес
    файла целиком.
    """
    if (known := find_source(source, s)) is not None:
        return known.index
    url = source if "://" in source else f"https://{source}"
    if not url.endswith(".txt"):
        url = url.rstrip("/") + "/" + LLMS_INDEX_NAME
    return url


def domain_of(url: str, s: Settings) -> str:
    """Имя источника для ответов: домен из реестра или хост адреса."""
    if (known := find_source(url, s)) is not None:
        return known.domain
    return urlsplit(url).hostname or url


def _cache_paths(method: str, url: str, s: Settings) -> tuple[Path, Path]:
    host = urlsplit(url).hostname or "unknown"
    key = hashlib.sha256(f"{method} {url}".encode()).hexdigest()[:24]
    base = s.llms_cache_dir / host / key
    return base.with_suffix(".body"), base.with_suffix(".meta")


def _cached(method: str, url: str, s: Settings) -> Fetched | None:
    """Прочитать из кэша, если запись есть и не старше TTL; иначе None."""
    body_path, meta_path = _cache_paths(method, url, s)
    try:
        if time() - meta_path.stat().st_mtime > s.llms_cache_ttl:
            return None
        return Fetched.model_validate({**(store.load(meta_path) or {}), "body": body_path.read_bytes()})
    except (OSError, ValidationError):
        return None


def _store(method: str, url: str, fetched: Fetched, s: Settings) -> None:
    """Положить в кэш; тело раньше меты, чтобы свежая мета не указала на старое тело."""
    body_path, meta_path = _cache_paths(method, url, s)
    with contextlib.suppress(OSError):
        store.write_bytes(body_path, fetched.body)
        store.save(meta_path, fetched.model_dump(exclude={"body"}))


def parse_index(text: str, base_url: str) -> LlmsIndex:
    """Разобрать `llms.txt`: заголовок, аннотация, ссылки по разделам.

    Формат: `# Заголовок`, `> аннотация`, `## Раздел`, строки `- [имя](url):
    описание`. Относительные адреса раскрываются от `base_url`. Варианты
    файлов здесь не проверяются — это сеть, см. `Session.variants`.
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
        elif line.startswith(("-", "*")) and (m := _LINK.search(line)):
            url = urljoin(base_url, m["url"])
            if url.endswith(LLMS_FULL_NAME):
                full_url = url
            entries.append(
                IndexEntry(title=m["title"].strip(), url=url, description=m["desc"].strip(), section=section)
            )
    return LlmsIndex(url=base_url, title=title, summary=summary, entries=entries, full_url=full_url)


def sections(text: str) -> list[tuple[str, str]]:
    """Разрезать markdown по заголовкам до третьего уровня.

    Строки `# …` внутри огороженного кода (``` или ~~~) заголовками не считаются:
    иначе комментарий в примере на bash резал бы раздел пополам.

    Returns:
        Пары (заголовок, текст раздела с заголовком); текст до первого
        заголовка идёт разделом с пустым заголовком.
    """
    starts: list[int] = []
    fenced = False
    pos = 0
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced and _HEADING.match(line):
            starts.append(pos)
        pos += len(line)
    result = []
    for begin, end in pairwise([0, *starts, len(text)]):
        chunk = text[begin:end].strip()
        if not chunk:
            continue
        first = chunk.split("\n", 1)[0]
        heading = first.lstrip("#").strip() if first.startswith("#") else ""
        result.append((heading, chunk))
    return result


def _matches(text: str, words: list[str]) -> bool:
    lowered = text.lower()
    return all(word in lowered for word in words)


def matching_sections(text: str, words: list[str], limit: int) -> list[tuple[str, str, bool]]:
    """Разделы, где есть все слова: заголовок, фрагмент до `limit` символов, обрезан ли.

    Чистая функция для потока: на full-файле в десятки мегабайт разрез и поиск
    заняли бы цикл событий на секунды.
    """
    return [(heading, chunk[:limit], len(chunk) > limit) for heading, chunk in sections(text) if _matches(chunk, words)]


class Session:
    """Один вызов инструмента: настройки и HTTP-клиент с общим пулом соединений."""

    def __init__(self, s: Settings | None = None) -> None:
        self.s = s or get_settings()
        self._client = make_client(self.s, self._guard)
        self._public: set[str] = set()

    async def __aenter__(self) -> Self:
        """Открыть пул соединений."""
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        """Закрыть пул соединений."""
        await self._client.aclose()

    async def _guard(self, request: httpx2.Request) -> None:
        """Проверить адрес перед отправкой каждого запроса, включая переадресацию."""
        host = check_url(str(request.url))
        if host not in self._public:
            await resolve_public(host)
            self._public.add(host)

    async def fetch(self, url: str, method: str = "GET", *, cache: bool = True) -> Fetched:
        """Скачать ресурс с кэшем и потолком размера.

        Код ответа возвращается, а не превращается в ошибку: проверке домена
        нужен и 404. HEAD тела не тянет — им проверяется наличие файлов.
        `cache=False` минует кэш на диске: так проверяют, что источник жив
        сейчас. Адрес и каждый ход переадресации проверяет `_guard` до отправки.

        Raises:
            UserError: адрес не https или непубличный, сеть недоступна, таймаут,
                слишком длинная цепочка переадресаций или тело больше
                `llms_max_bytes`.
        """
        s = self.s
        check_url(url)  # в кэш такой адрес не попал бы, но проверка дешевле спора об этом
        if cache and (hit := await anyio.to_thread.run_sync(_cached, method, url, s)) is not None:
            return hit
        body = bytearray()
        try:
            async with self._client.stream(method, url) as response:
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > s.llms_max_bytes:
                        raise UserError(f"{url}: больше потолка {s.llms_max_bytes} байт")
                length = response.headers.get("content-length")
                fetched = Fetched(
                    status=response.status_code,
                    content_type=response.headers.get("content-type", ""),
                    content_length=int(length) if length and length.isdigit() else None,
                    body=bytes(body),
                )
        except httpx2.HTTPError as err:
            raise UserError(f"не удалось скачать {url}: {err}") from err
        log.debug("%s %s -> %s, %d байт", method, url, fetched.status, len(fetched.body))
        if cache and fetched.cacheable:
            await anyio.to_thread.run_sync(_store, method, url, fetched, s)
        return fetched

    async def is_stub(self, url: str, *, cache: bool) -> bool:
        """Отвечает ли домен успехом на мусорный путь рядом с `url` (HEAD)."""
        junk = await self.fetch(urljoin(url, LLMS_JUNK_NAME), "HEAD", cache=cache)
        return junk.ok

    async def fetch_ok(self, url: str) -> Fetched:
        """Скачать документ; неуспешный код или SPA-заглушка — ошибка вызова.

        HTML в ответе сам по себе не приговор: честный домен может отдать HTML
        страницу. Приговор — HTML плюс успешный ответ на мусорный путь рядом.

        Raises:
            UserError: ошибка сети, код ответа не 2xx/3xx или SPA-заглушка.
        """
        fetched = await self.fetch(url)
        if not fetched.ok:
            raise UserError(f"{url}: HTTP {fetched.status}")
        if fetched.is_html and await self.is_stub(url, cache=True):
            raise UserError(f"{url}: пришла HTML-оболочка, а домен отвечает успехом на любой путь — это SPA-заглушка")
        return fetched

    async def variants(self, index: str) -> list[Variant]:
        """Какие из `LLMS_VARIANTS` лежат рядом с индексом (по HEAD, параллельно).

        Домен, отвечающий 200 на всё, отдаёт на отсутствующий файл HTML-оболочку,
        поэтому «есть» — это успешный код и не HTML в типе содержимого.
        """

        async def probe(name: str) -> Variant | None:
            try:
                head = await self.fetch(urljoin(index, name), "HEAD")
            except UserError:
                return None
            if not head.ok or head.is_html:
                return None
            return Variant(name=name, size=head.content_length)

        return [found for found in await gather(probe, LLMS_VARIANTS) if found is not None]

    async def index(self, source: str) -> LlmsIndex:
        """Индекс источника без вариантов: скачать и разобрать."""
        url = index_url(source, self.s)
        return parse_index((await self.fetch_ok(url)).text, url)

    async def load_index(self, source: str) -> LlmsIndex:
        """Индекс источника вместе с вариантами файлов рядом."""
        index = await self.index(source)
        index.variants = await self.variants(index.url)
        return index

    async def check(self, known: KnownSource) -> SourceStatus:
        """Жив ли источник сейчас: HEAD индекса мимо кэша, при HTML — мусорная проба."""
        state: SourceState = "ok"
        detail = ""
        try:
            head = await self.fetch(known.index, "HEAD", cache=False)
            if not head.ok:
                state, detail = "unavailable", f"HTTP {head.status}"
            elif head.is_html:
                state = "stub" if await self.is_stub(known.index, cache=False) else "unavailable"
                detail = "вместо индекса HTML-оболочка"
        except UserError as err:
            state, detail = "unavailable", str(err)
        return SourceStatus(**known.model_dump(), state=state, detail=detail)

    async def verify_sources(self, *, refresh: bool = False) -> SourcesResult:
        """Все источники с итогом проверки; итоги лежат в runtime-каталоге до TTL.

        Runtime-каталог живёт до перезагрузки машины, но переживает перезапуск
        сервера: между сессиями источники заново не опрашиваются. Опрашиваются
        все разом, мимо кэша скачивания.
        """
        s = self.s
        known = all_sources(s)
        age, saved = store.load_stamped(s.llms_status_file)
        if not refresh and age < s.llms_status_ttl:
            # Сохранённое могло быть записано другим составом реестра или
            # испорчено: тогда проверяем заново, а не падаем.
            with contextlib.suppress(KeyError, ValidationError):
                rows = _VERDICTS.validate_python(saved.get("sources"))
                statuses = [SourceStatus(**item.model_dump(), **rows[item.domain].model_dump()) for item in known]
                return SourcesResult(checked_ago=age, sources=statuses, variants=list(LLMS_VARIANTS))
        statuses = await gather(self.check, known)
        with contextlib.suppress(OSError):
            store.save_stamped(
                s.llms_status_file,
                {"sources": {st.domain: st.model_dump(include={"state", "detail"}) for st in statuses}},
            )
        return SourcesResult(checked_ago=0.0, sources=statuses, variants=list(LLMS_VARIANTS))

    async def _full_size(self, index_url: str) -> int | None:
        """Размер `llms-full.txt` рядом с индексом (один HEAD); None — файла нет."""
        try:
            head = await self.fetch(urljoin(index_url, LLMS_FULL_NAME), "HEAD")
        except UserError:
            return None
        return head.content_length if head.ok and not head.is_html else None

    async def add_source(self, domain: str, covers: str, index: str | None) -> SourceStatus:
        """Проверить источник по сети, добавить в пользовательский файл и вернуть.

        Индекс скачивается целиком: он должен быть текстом с хотя бы одной
        ссылкой. Размер `llms-full.txt` рядом узнаётся одним HEAD; остальные
        варианты (small, ctx…) не пробуются — их показывает llms_index.

        Raises:
            UserError: домен уже есть, индекса нет, это заглушка или в нём нет
                ссылок.
            OSError: файл источников не записался.
        """
        s = self.s
        if find_source(domain, s) is not None:
            raise UserError(f"источник {domain!r} уже есть")
        parsed = await self.index(index or domain)
        if not parsed.entries:
            raise UserError(f"{parsed.url}: в индексе нет ни одной ссылки — это не llms.txt")
        known = KnownSource(domain=domain, index=parsed.url, covers=covers, full_size=await self._full_size(parsed.url))
        _save_sources([*custom_sources(s), known], s)
        _forget_status(s)
        return SourceStatus(**known.model_dump(), state="ok", detail="")

    def _index_hits(self, index: LlmsIndex, words: list[str]) -> list[SearchHit]:
        domain = domain_of(index.url, self.s)
        return [
            SearchHit(domain=domain, title=entry.title, url=entry.url, text=entry.description, truncated=False)
            for entry in index.entries
            if _matches(f"{entry.title} {entry.description} {entry.url}", words)
        ]

    async def _full_hits(self, index: LlmsIndex, words: list[str]) -> list[SearchHit]:
        full_url = index.full_url or urljoin(index.url, LLMS_FULL_NAME)
        fetched = await self.fetch_ok(full_url)
        domain = domain_of(index.url, self.s)
        found = await anyio.to_thread.run_sync(matching_sections, fetched.text, words, self.s.llms_hit_chars)
        return [
            SearchHit(domain=domain, title=heading, url=full_url, text=text, truncated=truncated)
            for heading, text, truncated in found
        ]

    async def search(self, query: str, source: str | None, scope: SearchScope) -> SearchResult:
        """Найти слова запроса (все, без учёта регистра).

        Без `source` — по оглавлениям всех источников реестра, которые последняя
        проверка признала живыми, параллельно; остальные и упавшие по пути
        называются в `skipped`.

        Raises:
            UserError: пустой запрос, `full` без источника, либо названный
                источник нечестный или без файла.
        """
        words = query.lower().split()
        if not words:
            raise UserError("пустой запрос")
        if source is None and scope == "full":
            raise UserError("поиск по full — только с указанным источником")

        async def one(target: str) -> list[SearchHit]:
            index = await self.index(target)
            return self._index_hits(index, words) if scope == "index" else await self._full_hits(index, words)

        async def guarded(target: str) -> list[SearchHit] | str:
            try:
                return await one(target)
            except UserError as err:
                return f"{target}: {err}"

        skipped: list[str] = []
        outcomes: list[list[SearchHit] | str]
        if source is not None:
            targets = [source]
            outcomes = [await one(source)]
        else:
            verified = (await self.verify_sources()).sources
            targets = [st.domain for st in verified if st.state == "ok"]
            skipped += [f"{st.domain}: {st.detail}" for st in verified if st.state != "ok"]
            outcomes = await gather(guarded, targets)

        hits: list[SearchHit] = []
        searched: list[str] = []
        for target, outcome in zip(targets, outcomes, strict=True):
            if isinstance(outcome, str):
                skipped.append(outcome)
            else:
                searched.append(target)
                hits += outcome
        return SearchResult(
            query=query,
            scope=scope,
            searched=searched,
            skipped=skipped,
            total=len(hits),
            hits=hits[: self.s.llms_max_hits],
        )

    async def fetch_page(self, url: str, offset: int) -> Page:
        """Страница по адресу из индекса, кусок с `offset` длиной `llms_page_chars`.

        Raises:
            UserError: это `llms-full.txt` (он читается только поиском), домен
                нечестный, страницы нет или сеть недоступна.
        """
        if urlsplit(url).path.rsplit("/", 1)[-1] == LLMS_FULL_NAME:
            raise UserError(f"{url}: {LLMS_FULL_NAME} целиком не отдаётся — llms_search со scope=full")
        fetched = await self.fetch_ok(url)
        text = fetched.text
        end = offset + self.s.llms_page_chars
        return Page(
            url=url,
            content_type=fetched.content_type,
            length=len(text),
            offset=offset,
            text=text[offset:end],
            next_offset=end if end < len(text) else None,
        )
