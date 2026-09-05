"""Роутер llms.txt: реестр, разбор индекса, поиск, кэш и проверка домена — без сети."""

import json
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import anyio
import httpx2
import pytest

from mcp_openssh_connector.core.config import constants
from mcp_openssh_connector.core.config.environment import Settings, get_settings
from mcp_openssh_connector.core.errors import UserError
from mcp_openssh_connector.core.schemas import KnownSource
from mcp_openssh_connector.routers.llms import services
from mcp_openssh_connector.routers.llms.schemas import LlmsIndex, SearchResult, SearchScope, SourceStatus
from mcp_openssh_connector.routers.llms.services import Session

INDEX = """# Ruff

> Линтер и форматтер.

Вводная строка без ссылки.

## Правила

- [E501](https://docs.astral.sh/ruff/rules/line-too-long.md): длинные строки
- [Настройка](/ruff/configuration.md): pyproject и ruff.toml

## Всё сразу

- [Полностью](llms-full.txt)
"""

FULL = """# Ruff

Вступление.

## Линтер

Правила и их коды.

```bash
# это комментарий в коде, а не заголовок
ruff check .
```

### E501

Строка длиннее лимита.

## Форматтер

Стиль как у black.
"""

SockAddr = tuple[str, int] | tuple[str, int, int, int]

RUFF = KnownSource(
    domain="docs.astral.sh/ruff",
    index="https://docs.astral.sh/ruff/llms.txt",
    covers="ruff",
    default=True,
)
DEAD = KnownSource(domain="dead.example", index="https://dead.example/llms.txt", covers="-", default=True)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Кэш, данные и runtime-каталог — во временном месте; настройки заново."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _serve(pages: dict[str, str], *, spa: bool = False) -> httpx2.MockTransport:
    """Транспорт: известные пути — 200, остальное — 404 (или 200 при SPA)."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path in pages:
            body = "" if request.method == "HEAD" else pages[path]
            headers = {"content-type": "text/markdown", "content-length": str(len(pages[path].encode()))}
            return httpx2.Response(200, text=body, headers=headers)
        if spa:
            return httpx2.Response(200, text="<html>app</html>", headers={"content-type": "text/html"})
        return httpx2.Response(404, text="nope")

    return httpx2.MockTransport(handler)


def _use(transport: httpx2.MockTransport, monkeypatch: pytest.MonkeyPatch) -> None:
    """Подставить транспорт, сохранив хук проверки адреса; DNS не трогать."""

    def make_client(_s: Settings, guard: Callable[[httpx2.Request], Awaitable[None]]) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=transport, follow_redirects=True, event_hooks={"request": [guard]})

    async def resolve_public(_host: str) -> None:
        pass

    monkeypatch.setattr(services, "make_client", make_client)
    monkeypatch.setattr(services, "resolve_public", resolve_public)


def test_index_url_forms_and_registry() -> None:
    s = Settings()
    assert services.index_url("docs.astral.sh/uv", s) == "https://docs.astral.sh/uv/llms.txt"
    assert services.index_url("https://x.dev/", s) == "https://x.dev/llms.txt"
    assert services.index_url("https://x.dev/v2/llms.txt", s) == "https://x.dev/v2/llms.txt"
    assert services.domain_of("https://docs.astral.sh/uv/llms.txt", s) == "docs.astral.sh/uv"
    assert services.domain_of("https://x.dev/llms.txt", s) == "x.dev"


@pytest.mark.parametrize(
    "url",
    [
        "http://docs.example/llms.txt",  # только https
        "https://localhost/llms.txt",
        "https://127.0.0.1/llms.txt",
        "https://10.0.0.1/llms.txt",
        "https://[::1]/llms.txt",
        "https://../llms.txt",  # хост, который выводит кэш за пределы каталога
        "file:///etc/passwd",
    ],
)
def test_check_url_rejects_non_public(url: str) -> None:
    with pytest.raises(UserError):
        services.check_url(url)


def test_check_url_accepts_public() -> None:
    services.check_url("https://docs.astral.sh/uv/llms.txt")
    services.check_url("https://93.184.216.34/llms.txt")


def test_default_registry_consistent() -> None:
    known = constants.LLMS_DEFAULT_SOURCES
    assert len({k.domain for k in known}) == len(known)
    for k in known:
        assert k.default is True
        assert k.index.startswith("https://")
        assert k.index.endswith("llms.txt")
        assert k.covers
        assert k.full_size is None or k.full_size > 0  # размер, если задан, положителен
    assert "llms.txt" in constants.LLMS_VARIANTS
    assert "llms-full.txt" in constants.LLMS_VARIANTS


def test_parse_index_sections_and_relative_urls() -> None:
    index = services.parse_index(INDEX, "https://docs.astral.sh/ruff/llms.txt")
    assert index.title == "Ruff"
    assert index.summary == "Линтер и форматтер."
    assert [e.title for e in index.entries] == ["E501", "Настройка", "Полностью"]
    assert index.entries[1].url == "https://docs.astral.sh/ruff/configuration.md"
    assert index.entries[1].section == "Правила"
    assert index.entries[0].description == "длинные строки"
    assert index.full_url == "https://docs.astral.sh/ruff/llms-full.txt"


def test_sections_split_by_headings_outside_fences() -> None:
    got = services.sections(FULL)
    assert [h for h, _ in got] == ["Ruff", "Линтер", "E501", "Форматтер"]
    assert got[2][1].startswith("### E501")
    assert "комментарий в коде" in got[1][1]  # `# …` внутри ``` остался в разделе


def test_index_search_fetch_end_to_end(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        "/ruff/llms.txt": INDEX,
        "/ruff/llms-full.txt": FULL,
        "/ruff/llms-small.txt": "small",
        "/ruff/configuration.md": "x" * 30,
    }
    _use(_serve(pages), monkeypatch)

    async def scenario() -> None:
        async with Session() as session:
            index = await session.load_index("docs.astral.sh/ruff")
            assert len(index.entries) == 3
            assert [(v.name, v.size) for v in index.variants] == [
                ("llms.txt", len(INDEX.encode())),
                ("llms-full.txt", len(FULL.encode())),
                ("llms-small.txt", 5),
            ]

            by_index = await session.search("PYPROJECT", "docs.astral.sh/ruff", "index")
            assert [h.title for h in by_index.hits] == ["Настройка"]
            assert by_index.hits[0].domain == "docs.astral.sh/ruff"
            assert by_index.searched == ["docs.astral.sh/ruff"]

            by_full = await session.search("коды", "docs.astral.sh/ruff", "full")
            assert [h.title for h in by_full.hits] == ["Линтер"]
            assert by_full.hits[0].url.endswith("/ruff/llms-full.txt")

            with pytest.raises(UserError, match="scope=full"):
                await session.fetch_page("https://docs.astral.sh/ruff/llms-full.txt", 0)

            monkeypatch.setattr(get_settings(), "llms_page_chars", 20)
            page = await session.fetch_page("https://docs.astral.sh/ruff/configuration.md", 0)
            assert page.length == 30
            assert len(page.text) == 20
            assert page.next_offset == 20
            tail = await session.fetch_page("https://docs.astral.sh/ruff/configuration.md", 20)
            assert tail.text == "x" * 10
            assert tail.next_offset is None

    anyio.run(scenario)
    # В кэше индекс, full, страница и HEAD по каждому варианту; мусорной пробы
    # нет — текстовые ответы в ней не нуждаются.
    bodies = list((home / "cache" / "mcp-openssh-connector" / "llms").rglob("*.body"))
    assert len(bodies) == 3 + len(constants.LLMS_VARIANTS)


@pytest.mark.usefixtures("home")
def test_verify_sources_cached_until_reboot_or_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "LLMS_DEFAULT_SOURCES", (RUFF, DEAD))
    heads = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal heads
        heads += request.method == "HEAD"
        if request.url.path == "/ruff/llms.txt":
            return httpx2.Response(200, headers={"content-type": "text/plain"})
        return httpx2.Response(404)

    _use(httpx2.MockTransport(handler), monkeypatch)
    s = get_settings()

    async def verify(*, refresh: bool = False) -> list[tuple[str, str, str]]:
        async with Session(s) as session:
            result = await session.verify_sources(refresh=refresh)
        return [(st.domain, st.state, st.detail) for st in result.sources]

    assert anyio.run(verify) == [
        ("docs.astral.sh/ruff", "ok", ""),
        ("dead.example", "unavailable", "HTTP 404"),
    ]
    assert heads == 2
    assert s.llms_status_file.is_file()  # runtime-каталог: до перезагрузки

    anyio.run(verify)
    assert heads == 2  # из сохранённых итогов, без сети

    anyio.run(lambda: verify(refresh=True))
    assert heads == 4

    monkeypatch.setattr(s, "llms_status_ttl", 0.0)
    anyio.run(verify)
    assert heads == 6  # протухло

    # Битые сохранённые итоги — перепроверка, а не падение.
    s.llms_status_file.write_text(json.dumps({"checked_at": 9e12, "sources": {"docs.astral.sh/ruff": "мусор"}}))
    monkeypatch.setattr(s, "llms_status_ttl", 1e6)
    anyio.run(verify)
    assert heads == 8


@pytest.mark.usefixtures("home")
def test_search_all_uses_only_live_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "LLMS_DEFAULT_SOURCES", (RUFF, DEAD))
    _use(_serve({"/ruff/llms.txt": INDEX}), monkeypatch)

    async def search(query: str, scope: SearchScope) -> SearchResult:
        async with Session() as session:
            return await session.search(query, None, scope)

    result = anyio.run(search, "длинные", "index")
    assert result.searched == ["docs.astral.sh/ruff"]
    assert result.skipped == ["dead.example: HTTP 404"]
    assert [h.title for h in result.hits] == ["E501"]

    with pytest.raises(UserError, match="full"):
        anyio.run(search, "x", "full")


@pytest.mark.usefixtures("home")
def test_add_and_remove_source_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "LLMS_DEFAULT_SOURCES", (RUFF,))
    pages = {"/llms.txt": INDEX, "/llms-full.txt": FULL, "/empty/llms.txt": "# Пусто\n"}
    _use(_serve(pages), monkeypatch)
    s = get_settings()

    async def add(domain: str, covers: str, index: str | None) -> SourceStatus:
        async with Session(s) as session:
            return await session.add_source(domain, covers, index)

    added = anyio.run(add, "new.example", "новое", None)
    assert added.default is False
    assert added.index == "https://new.example/llms.txt"
    assert added.full_size == len(FULL.encode())  # размер llms-full.txt узнан HEAD-ом
    assert s.llms_sources_file.is_file()

    # Файл читается заново — так источник переживает перезапуск; флаг default
    # в нём не хранится, и подделать его правкой файла нельзя.
    assert [k.domain for k in services.all_sources(s)] == ["docs.astral.sh/ruff", "new.example"]
    assert "default" not in json.loads(s.llms_sources_file.read_text())["sources"][0]
    assert services.index_url("new.example", s) == "https://new.example/llms.txt"

    with pytest.raises(UserError, match="уже есть"):
        anyio.run(add, "new.example", "x", None)
    with pytest.raises(UserError, match="нет ни одной ссылки"):
        anyio.run(add, "empty.example", "x", "https://empty.example/empty/llms.txt")
    with pytest.raises(UserError, match="встроенный"):
        services.remove_source("docs.astral.sh/ruff", s)
    with pytest.raises(UserError, match="нет"):
        services.remove_source("nobody.example", s)

    removed = services.remove_source("new.example", s)
    assert removed.domain == "new.example"
    assert [k.domain for k in services.all_sources(s)] == ["docs.astral.sh/ruff"]
    assert json.loads(s.llms_sources_file.read_text()) == {"sources": []}


@pytest.mark.usefixtures("home")
def test_custom_cannot_shadow_default_or_claim_default_flag() -> None:
    s = get_settings()
    s.llms_sources_file.parent.mkdir(parents=True)
    s.llms_sources_file.write_text(
        json.dumps(
            {
                "sources": [
                    {"domain": "docs.astral.sh/uv", "index": "https://evil/llms.txt", "covers": "x"},
                    {
                        "domain": "mine.example",
                        "index": "https://mine.example/llms.txt",
                        "covers": "x",
                        "default": True,
                    },
                ]
            }
        )
    )
    custom = services.custom_sources(s)
    assert [k.domain for k in custom] == ["mine.example"]
    assert custom[0].default is False
    assert services.remove_source("mine.example", s).domain == "mine.example"


@pytest.mark.usefixtures("home")
def test_spa_domain_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # Индекса нет: вместо него HTML-оболочка, и на мусор тоже 200.
    _use(_serve({}, spa=True), monkeypatch)

    async def load() -> None:
        async with Session() as session:
            await session.load_index("spa.example")

    with pytest.raises(UserError, match="SPA"):
        anyio.run(load)


@pytest.mark.usefixtures("home")
def test_text_index_trusted_on_spa_like_host(monkeypatch: pytest.MonkeyPatch) -> None:
    # Как docs.claude.com: настоящие файлы — text, на всё остальное — HTML 200.
    _use(_serve({"/llms.txt": INDEX, "/llms-full.txt": FULL}, spa=True), monkeypatch)

    async def load() -> LlmsIndex:
        async with Session() as session:
            return await session.load_index("spa.example")

    index = anyio.run(load)
    assert len(index.entries) == 3
    assert [v.name for v in index.variants] == ["llms.txt", "llms-full.txt"]


@pytest.mark.usefixtures("home")
def test_missing_index_is_user_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(_serve({}), monkeypatch)

    async def load() -> None:
        async with Session() as session:
            await session.load_index("honest.example")

    with pytest.raises(UserError, match="HTTP 404"):
        anyio.run(load)


def test_cache_respects_ttl_and_skips_server_errors(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if request.url.path == "/down.txt":
            return httpx2.Response(503, text="later")
        return httpx2.Response(200, text="body")

    _use(httpx2.MockTransport(handler), monkeypatch)
    s = get_settings()

    async def fetch(url: str, times: int) -> None:
        async with Session(s) as session:
            for _ in range(times):
                await session.fetch(url)

    anyio.run(fetch, "https://h.example/a.txt", 2)
    assert calls == 1  # второй раз — из кэша
    meta = next((home / "cache" / "mcp-openssh-connector").rglob("*.meta"))
    assert json.loads(meta.read_text())["status"] == 200
    monkeypatch.setattr(s, "llms_cache_ttl", 0.0)
    anyio.run(fetch, "https://h.example/a.txt", 1)
    assert calls == 2  # протухло — перекачали

    monkeypatch.setattr(s, "llms_cache_ttl", 1e6)
    anyio.run(fetch, "https://h.example/down.txt", 2)
    assert calls == 4  # 503 не кэшируется: сбой сервера временный


@pytest.mark.usefixtures("home")
def test_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(_serve({"/big.txt": "x" * 100}), monkeypatch)

    async def fetch() -> None:
        async with Session(Settings(llms_max_bytes=10)) as session:
            await session.fetch("https://h.example/big.txt")

    with pytest.raises(UserError, match="потолка"):
        anyio.run(fetch)


@pytest.mark.usefixtures("home")
def test_redirect_checked_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    # Переадресация внутрь проверяется до отправки: запрос туда не уходит вовсе.
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.host)
        if request.url.host == "h.example":
            return httpx2.Response(302, headers={"location": "http://internal.example/llms.txt"})
        return httpx2.Response(200, text="secret")

    _use(httpx2.MockTransport(handler), monkeypatch)

    async def fetch() -> None:
        async with Session() as session:
            await session.fetch("https://h.example/llms.txt")

    with pytest.raises(UserError, match="https"):
        anyio.run(fetch)
    assert seen == ["h.example"]


def test_resolve_public_rejects_private_and_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def getaddrinfo(host: str, _port: int | None, **_options: int) -> list[tuple[int, int, int, str, SockAddr]]:
        if host == "inner.example":
            return [(2, 1, 6, "", ("10.0.0.1", 0))]
        if host == "outer.example":
            return [
                (2, 1, 6, "", ("93.184.216.34", 0)),
                (10, 1, 6, "", ("2606:2800:220:1:248:1893:25c8:1946", 0, 0, 0)),
            ]
        raise OSError("нет такого имени")

    monkeypatch.setattr(anyio, "getaddrinfo", getaddrinfo)
    anyio.run(services.resolve_public, "outer.example")
    anyio.run(services.resolve_public, "93.184.216.34")  # литерал: без DNS
    with pytest.raises(UserError, match="непубличный"):
        anyio.run(services.resolve_public, "inner.example")
    with pytest.raises(UserError, match="не разрешается"):
        anyio.run(services.resolve_public, "nowhere.example")


@pytest.mark.usefixtures("home")
def test_guard_runs_once_per_host(monkeypatch: pytest.MonkeyPatch) -> None:
    # Пробы вариантов бьют в один хост: DNS-проверка на сессию — одна.
    _use(_serve({"/llms.txt": INDEX}), monkeypatch)
    resolved: list[str] = []

    async def resolve_public(host: str) -> None:
        resolved.append(host)

    monkeypatch.setattr(services, "resolve_public", resolve_public)

    async def load() -> None:
        async with Session() as session:
            await session.load_index("one.example")

    anyio.run(load)
    assert resolved == ["one.example"]
