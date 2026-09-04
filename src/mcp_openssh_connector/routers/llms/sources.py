"""Реестр известных источников `llms.txt` и имена вариантов файлов.

Зеркало списка из скилла `llms-txt` (`sources.md`): найденное и проверенные
пустышки записываются в оба места. Реестр — данные, а не поведение: домен из
него читается так же, как любой другой, просто без угадывания адреса индекса.
"""

from .schemas import AbsentSource, KnownSource

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

KNOWN: tuple[KnownSource, ...] = (
    KnownSource(
        domain="code.claude.com",
        index="https://code.claude.com/llms.txt",
        covers=(
            "Claude Code: CLI и точки настройки — хуки, слэш-команды, скиллы, "
            "сабагенты, MCP, плагины, права; плюс Agent SDK"
        ),
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
    ),
    KnownSource(
        domain="docs.astral.sh/uv",
        index="https://docs.astral.sh/uv/llms.txt",
        covers=(
            "uv: зависимости, проекты и воркспейсы, запуск скриптов, установка самого Python, pip-совместимый интерфейс"
        ),
    ),
    KnownSource(
        domain="docs.astral.sh/ruff",
        index="https://docs.astral.sh/ruff/llms.txt",
        covers=("ruff: правила линтера и их коды, форматтер, конфигурация, интеграция с редакторами"),
    ),
    KnownSource(
        domain="docs.astral.sh/ty",
        index="https://docs.astral.sh/ty/llms.txt",
        covers="ty: проверка типов, конфигурация, language server, переезд с mypy и pyright",
    ),
    KnownSource(
        domain="pydantic.dev",
        index="https://pydantic.dev/llms.txt",
        covers=("корень семейства Pydantic — Validation, AI, Logfire; отсюда ссылки на индекс каждого продукта"),
    ),
    KnownSource(
        domain="docs.pydantic.dev/latest",
        index="https://docs.pydantic.dev/latest/llms.txt",
        covers=("pydantic: модели, валидаторы, сериализация, справочник API, разбор текстов ошибок"),
        full="https://docs.pydantic.dev/latest/llms-full.txt",
        full_size="2 МБ",
    ),
)

# Домены, где искали и не нашли: чтобы не ходить второй раз. Указывать, как
# именно не нашли — 404 на имена или SPA-заглушка с 200 на любой путь.
ABSENT: tuple[AbsentSource, ...] = ()
