"""Роутер llms.txt: реестр, разбор индекса, поиск, кэш и проверка домена — без сети."""

import json
from collections.abc import Iterator
from pathlib import Path

import anyio
import httpx2
import pytest

from mcp_openssh_connector.core.config import Settings, get_settings
from mcp_openssh_connector.core.errors import UserError
from mcp_openssh_connector.routers.llms import services, sources
from mcp_openssh_connector.routers.llms.schemas import KnownSource

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

### E501

Строка длиннее лимита.

## Форматтер

Стиль как у black.
"""

RUFF = KnownSource(domain="docs.astral.sh/ruff", index="https://docs.astral.sh/ruff/llms.txt", covers="ruff")
DEAD = KnownSource(domain="dead.example", index="https://dead.example/llms.txt", covers="-")


def test_index_url_forms_and_registry() -> None:
    assert services.index_url("docs.astral.sh/uv") == "https://docs.astral.sh/uv/llms.txt"
    assert services.index_url("https://x.dev/") == "https://x.dev/llms.txt"
    assert services.index_url("https://x.dev/v2/llms.txt") == "https://x.dev/v2/llms.txt"
    assert services.domain_of("https://docs.astral.sh/uv/llms.txt") == "docs.astral.sh/uv"
    assert services.domain_of("https://x.dev/llms.txt") == "x.dev"


def test_registry_consistent() -> None:
    known = sources.KNOWN
    assert len({k.domain for k in known}) == len(known)
    for k in known:
        assert k.index.startswith("https://")
        assert k.index.endswith("llms.txt")
        assert k.covers
        assert (k.full == "") == (k.full_size == "")  # размер только вместе с адресом
    assert "llms.txt" in sources.VARIANTS
    assert "llms-full.txt" in sources.VARIANTS


def test_parse_index_sections_and_relative_urls() -> None:
    index = services.parse_index(INDEX, "https://docs.astral.sh/ruff/llms.txt")
    assert index.title == "Ruff"
    assert index.summary == "Линтер и форматтер."
    assert [e.title for e in index.entries] == ["E501", "Настройка", "Полностью"]
    assert index.entries[1].url == "https://docs.astral.sh/ruff/configuration.md"
    assert index.entries[1].section == "Правила"
    assert index.entries[0].description == "длинные строки"
    assert index.full_url == "https://docs.astral.sh/ruff/llms-full.txt"


def test_sections_split_by_headings() -> None:
    got = services.sections(FULL)
    assert [h for h, _ in got] == ["Ruff", "Линтер", "E501", "Форматтер"]
    assert got[2][1].startswith("### E501")


def _serve(pages: dict[str, str], *, spa: bool = False) -> httpx2.MockTransport:
    """Транспорт: известные пути — 200, остальное — 404 (или 200 при SPA)."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path in pages:
            body = "" if request.method == "HEAD" else pages[path]
            return httpx2.Response(200, text=body, headers={"content-type": "text/markdown"})
        if spa:
            return httpx2.Response(200, text="<html>app</html>", headers={"content-type": "text/html"})
        return httpx2.Response(404, text="nope")

    return httpx2.MockTransport(handler)


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _use(transport: httpx2.MockTransport, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        services,
        "make_client",
        lambda _s: httpx2.AsyncClient(transport=transport, follow_redirects=True),
    )


def test_index_search_fetch_end_to_end(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        "/ruff/llms.txt": INDEX,
        "/ruff/llms-full.txt": FULL,
        "/ruff/llms-small.txt": "small",
        "/ruff/configuration.md": "x" * 30,
    }
    _use(_serve(pages), monkeypatch)

    async def scenario() -> None:
        index = await services.load_index("docs.astral.sh/ruff", get_settings())
        assert len(index.entries) == 3
        assert index.variants == ["llms.txt", "llms-full.txt", "llms-small.txt"]

        by_index = await services.search("PYPROJECT", "docs.astral.sh/ruff", "index")
        assert [h.title for h in by_index.hits] == ["Настройка"]
        assert by_index.hits[0].domain == "docs.astral.sh/ruff"
        assert by_index.searched == ["docs.astral.sh/ruff"]

        by_full = await services.search("коды", "docs.astral.sh/ruff", "full")
        assert [h.title for h in by_full.hits] == ["Линтер"]
        assert by_full.hits[0].url.endswith("/ruff/llms-full.txt")

        monkeypatch.setattr(get_settings(), "llms_page_chars", 20)
        page = await services.fetch_page("https://docs.astral.sh/ruff/configuration.md", 0)
        assert page.length == 30
        assert len(page.text) == 20
        assert page.next_offset == 20
        tail = await services.fetch_page("https://docs.astral.sh/ruff/configuration.md", 20)
        assert tail.text == "x" * 10
        assert tail.next_offset is None

    anyio.run(scenario)
    # В кэше индекс, full, страница, мусорная проба и HEAD по каждому варианту.
    bodies = list((cache / "mcp-openssh-connector" / "llms").rglob("*.body"))
    assert len(bodies) == 4 + len(sources.VARIANTS)


@pytest.mark.usefixtures("cache")
def test_search_all_known_skips_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "KNOWN", (RUFF, DEAD))
    transport = _serve({"/ruff/llms.txt": INDEX})
    _use(transport, monkeypatch)

    result = anyio.run(services.search, "длинные", None, "index")
    assert result.searched == ["docs.astral.sh/ruff"]
    assert result.skipped == ["dead.example: https://dead.example/llms.txt: HTTP 404"]
    assert [h.title for h in result.hits] == ["E501"]

    with pytest.raises(UserError, match="full"):
        anyio.run(services.search, "x", None, "full")


@pytest.mark.usefixtures("cache")
def test_spa_domain_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(_serve({"/llms.txt": INDEX}, spa=True), monkeypatch)
    with pytest.raises(UserError, match="SPA"):
        anyio.run(services.load_index, "spa.example", get_settings())


@pytest.mark.usefixtures("cache")
def test_missing_index_is_user_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(_serve({}), monkeypatch)
    with pytest.raises(UserError, match="HTTP 404"):
        anyio.run(services.load_index, "honest.example", get_settings())


def test_cache_respects_ttl(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handler(_request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(200, text="body")

    _use(httpx2.MockTransport(handler), monkeypatch)
    s = get_settings()

    async def twice() -> None:
        await services.fetch("https://h.example/a.txt", s)
        await services.fetch("https://h.example/a.txt", s)

    anyio.run(twice)
    assert calls == 1  # второй раз — из кэша
    meta = next((cache / "mcp-openssh-connector").rglob("*.meta"))
    assert json.loads(meta.read_text())["status"] == 200
    monkeypatch.setattr(s, "llms_cache_ttl", 0.0)
    anyio.run(services.fetch, "https://h.example/a.txt", s)
    assert calls == 2  # протухло — перекачали


@pytest.mark.usefixtures("cache")
def test_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(_serve({"/big.txt": "x" * 100}), monkeypatch)
    s = Settings(llms_max_bytes=10)
    with pytest.raises(UserError, match="потолка"):
        anyio.run(services.fetch, "https://h.example/big.txt", s)
