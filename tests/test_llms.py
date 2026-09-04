"""Роутер llms.txt: разбор индекса, поиск, кэш и проверка домена — без сети."""

import json
from collections.abc import Iterator
from pathlib import Path

import anyio
import httpx2
import pytest

from mcp_openssh_connector.core.config import Settings, get_settings
from mcp_openssh_connector.core.errors import UserError
from mcp_openssh_connector.routers.llms import services

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


def test_index_url_forms() -> None:
    assert services.index_url("docs.astral.sh/uv") == "https://docs.astral.sh/uv/llms.txt"
    assert services.index_url("https://x.dev/") == "https://x.dev/llms.txt"
    assert services.index_url("https://x.dev/v2/llms.txt") == "https://x.dev/v2/llms.txt"


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
            return httpx2.Response(200, text=pages[path], headers={"content-type": "text/markdown"})
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
        "/ruff/configuration.md": "x" * 30,
    }
    _use(_serve(pages), monkeypatch)

    async def scenario() -> None:
        index = await services.load_index("docs.astral.sh/ruff")
        assert len(index.entries) == 3

        by_index = await services.search("docs.astral.sh/ruff", "PYPROJECT", "index")
        assert [h.title for h in by_index.hits] == ["Настройка"]

        by_full = await services.search("docs.astral.sh/ruff", "коды", "full")
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
    # Всё скачанное лежит в кэше: индекс, full, страница и мусорная проба.
    bodies = list((cache / "mcp-openssh-connector" / "llms").rglob("*.body"))
    assert len(bodies) == 4


@pytest.mark.usefixtures("cache")
def test_spa_domain_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(_serve({"/llms.txt": INDEX}, spa=True), monkeypatch)
    with pytest.raises(UserError, match="SPA"):
        anyio.run(services.load_index, "spa.example")


@pytest.mark.usefixtures("cache")
def test_missing_index_is_user_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(_serve({}), monkeypatch)
    with pytest.raises(UserError, match="HTTP 404"):
        anyio.run(services.load_index, "honest.example")


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
