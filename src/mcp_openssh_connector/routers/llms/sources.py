"""Источники `llms.txt`: встроенный список и пользовательский файл.

Встроенные (`DEFAULT`) — то, что проверено и в чём уверены; удалить их нельзя.
Пользовательские лежат в JSON-файле `llms_sources_file` и переживают
перезапуск сервера. Здесь только хранение; проверка, что источники живы, — в
`services`.
"""

from ...core import store
from ...core.config import Settings
from ...core.errors import UserError
from .schemas import KnownSource

# Имена файлов, которые домены выкладывают рядом с индексом. Набор ими не
# ограничен, но эти встречаются: полный текст, сокращённый, «контекстные»
# варианты формата llms-ctx.
VARIANTS = (
    "llms.txt",
    "llms-full.txt",
    "llms-small.txt",
    "llms-medium.txt",
    "llms-ctx.txt",
    "llms-ctx-full.txt",
)

DEFAULT: tuple[KnownSource, ...] = (
    KnownSource(
        domain="code.claude.com",
        index="https://code.claude.com/llms.txt",
        covers=(
            "Claude Code: CLI и точки настройки — хуки, слэш-команды, скиллы, "
            "сабагенты, MCP, плагины, права; плюс Agent SDK"
        ),
        default=True,
    ),
    KnownSource(
        domain="docs.claude.com",
        index="https://docs.claude.com/llms.txt",
        covers=(
            "Anthropic API: Messages API, модели, цены, tool use, кэширование "
            "промптов, лимиты, коды ошибок, release notes"
        ),
        full="https://docs.claude.com/llms-full.txt",
        full_size="40 МБ",
        default=True,
    ),
    KnownSource(
        domain="docs.astral.sh/uv",
        index="https://docs.astral.sh/uv/llms.txt",
        covers=(
            "uv: зависимости, проекты и воркспейсы, запуск скриптов, установка самого Python, pip-совместимый интерфейс"
        ),
        default=True,
    ),
    KnownSource(
        domain="docs.astral.sh/ruff",
        index="https://docs.astral.sh/ruff/llms.txt",
        covers=("ruff: правила линтера и их коды, форматтер, конфигурация, интеграция с редакторами"),
        default=True,
    ),
    KnownSource(
        domain="docs.astral.sh/ty",
        index="https://docs.astral.sh/ty/llms.txt",
        covers="ty: проверка типов, конфигурация, language server, переезд с mypy и pyright",
        default=True,
    ),
    KnownSource(
        domain="pydantic.dev",
        index="https://pydantic.dev/llms.txt",
        covers=("корень семейства Pydantic — Validation, AI, Logfire; отсюда ссылки на индекс каждого продукта"),
        default=True,
    ),
    KnownSource(
        domain="docs.pydantic.dev/latest",
        index="https://docs.pydantic.dev/latest/llms.txt",
        covers=("pydantic: модели, валидаторы, сериализация, справочник API, разбор текстов ошибок"),
        full="https://docs.pydantic.dev/latest/llms-full.txt",
        full_size="2 МБ",
        default=True,
    ),
    KnownSource(
        domain="gofastmcp.com",
        index="https://gofastmcp.com/llms.txt",
        covers=(
            "FastMCP: серверы и клиенты MCP на Python — инструменты, ресурсы, "
            "промпты, монтирование, транспорты, аутентификация, тесты"
        ),
        full="https://gofastmcp.com/llms-full.txt",
        full_size="3 МБ",
        default=True,
    ),
)


def custom(s: Settings) -> list[KnownSource]:
    """Пользовательские источники из файла; битый или отсутствующий файл — пусто."""
    data = store.load(s.llms_sources_file) or {}
    try:
        return [KnownSource.model_validate(item) for item in data.get("sources", [])]
    except (ValueError, TypeError):
        return []


def all_sources(s: Settings) -> list[KnownSource]:
    """Встроенные, затем пользовательские; домены не повторяются."""
    return [*DEFAULT, *custom(s)]


def find(domain: str, s: Settings) -> KnownSource | None:
    """Источник по домену или адресу индекса."""
    for known in all_sources(s):
        if domain in (known.domain, known.index):
            return known
    return None


def add(source: KnownSource, s: Settings) -> None:
    """Добавить пользовательский источник и сохранить файл.

    Raises:
        UserError: домен уже есть среди встроенных или пользовательских.
        OSError: файл не записался.
    """
    if find(source.domain, s) is not None:
        raise UserError(f"источник {source.domain!r} уже есть")
    items = [*custom(s), source.model_copy(update={"default": False})]
    store.save(s.llms_sources_file, {"sources": [i.model_dump() for i in items]})


def remove(domain: str, s: Settings) -> KnownSource:
    """Удалить пользовательский источник и сохранить файл.

    Raises:
        UserError: источник встроенный или его нет.
        OSError: файл не записался.
    """
    found = find(domain, s)
    if found is None:
        raise UserError(f"источника {domain!r} нет")
    if found.default:
        raise UserError(f"источник {domain!r} встроенный, удалить нельзя")
    items = [i for i in custom(s) if i.domain != found.domain]
    store.save(s.llms_sources_file, {"sources": [i.model_dump() for i in items]})
    return found
