"""Неизменяемые параметры системы.

Всё, что не настраивается окружением и не меняется в работе: имена каталогов и
файлов, пороги проб, вокабуляры разбора команд, встроенный реестр `llms.txt`.
Настраиваемое — в `environment`. Значения неизменяемы и по типу: кортежи,
`frozenset`, `MappingProxyType`, замороженные модели.
"""

import errno
from pathlib import Path
from types import MappingProxyType
from typing import Final

from ..schemas import KnownSource

# ── общее ────────────────────────────────────────────────────────────────────
APP_NAME: Final = "mcp-openssh-connector"
PRIVATE_DIR_MODE: Final = 0o700  # каталог, закрытый от группы и остальных
MAX_WORKERS: Final = 16  # потоков разом: больше не ускоряет, а плодит процессы ssh

# ── ssh ──────────────────────────────────────────────────────────────────────
SSH_DIR: Final = Path.home() / ".ssh"
SSH_CONFIG: Final = SSH_DIR / "config"  # ssh всё равно читает именно его
TERM_GRACE: Final = 2.0  # секунд между SIGTERM и SIGKILL при таймауте run

# ── управление ~/.ssh/config ───────────────────────────────────────────────────
SECRET_FILE_MODE: Final = 0o600  # права файла-секрета и managed-конфига
# Шапка managed-файла: сервер владеет им целиком и вправе перезаписать.
MANAGED_HEADER: Final = "# Управляется mcp-openssh-connector: add_host/remove_host.\n"
# Порядок ключей в каноническом Host-блоке; прочие опции идут после в порядке ввода.
MANAGED_KEY_ORDER: Final = ("HostName", "User", "Port", "IdentityFile", "ProxyJump")
SSH_DEFAULT_PORT: Final = 22  # порт ssh по умолчанию: для него known_hosts не пишет `[host]:port`

# ── пробы доступности ────────────────────────────────────────────────────────
# ZeroTier поднимает путь к пиру лениво: пока идёт рандеву через корневые
# серверы, ядро мгновенно отдаёт EHOSTUNREACH. Одна проба принимает это за
# «узел мёртв», хотя вторая уже проходит. Больший таймаут не лечит — ошибка
# приходит сразу, а не по нему.
PROBE_RETRY_ERRNOS: Final = frozenset({errno.EHOSTUNREACH, errno.ENETUNREACH})
PROBE_PATH_BUDGET: Final = 2.5  # с суммарно на повторы, пока оверлей поднимает путь
PROBE_PATH_PAUSE: Final = 0.25  # с между попытками

# ── sudo ─────────────────────────────────────────────────────────────────────
SUDO_MASK: Final = "***"
# Скрипт прайминга: первую строку stdin читает оболочка и отдаёт по трубе одному
# вызову `sudo -v`. Так sudo получает ровно одну строку и при неверном пароле
# не съедает следующие строки stdin как новые попытки, а пароль не попадает в
# argv. `-k` сбрасывает тикет: с живым тикетом `sudo -v` пароль не читал бы.
SUDO_PRIME: Final = "IFS= read -r __sudo_pw && printf '%s\\n' \"$__sudo_pw\" | sudo -k -S -p '' -v && unset __sudo_pw"
# Обёртки, за которыми стоит настоящая команда: имя → опции, принимающие
# отдельное значение, и число позиционных аргументов до команды (`timeout 5 …`).
# Сам sudo/doas сюда не входят — их и надо распознать.
SUDO_WRAPPERS: Final = MappingProxyType(
    {
        "env": (frozenset({"-u", "-C", "-S", "--unset", "--chdir", "--split-string"}), 0),
        "nohup": (frozenset[str](), 0),
        "time": (frozenset({"-f", "-o", "--format", "--output"}), 0),
        "command": (frozenset[str](), 0),
        "builtin": (frozenset[str](), 0),
        "exec": (frozenset({"-a"}), 0),
        "nice": (frozenset({"-n", "--adjustment"}), 0),
        "ionice": (frozenset({"-c", "-n", "--class", "--classdata"}), 0),
        "stdbuf": (frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}), 0),
        "setsid": (frozenset[str](), 0),
        "timeout": (frozenset({"-s", "-k", "--signal", "--kill-after"}), 1),
    }
)
# Оболочки, у которых `-c '<код>'` запускает вложенную команду: в неё надо
# заглянуть, иначе sudo внутри `sh -c '…'` останется незамеченным.
SHELLS: Final = frozenset({"sh", "bash", "dash", "zsh", "ash", "ksh", "mksh", "fish"})

# ── llms.txt ─────────────────────────────────────────────────────────────────
LLMS_REDIRECTS: Final = 5  # переадресаций подряд; каждая проверяется как новый адрес
LLMS_JUNK_NAME: Final = "zzz-nope-12345.txt"  # стабильное имя, чтобы проба кэшировалась
LLMS_INDEX_NAME: Final = "llms.txt"
LLMS_FULL_NAME: Final = "llms-full.txt"
# Имена файлов, которые домены выкладывают рядом с индексом. Набор ими не
# ограничен, но эти встречаются: полный текст, сокращённый, «контекстные»
# варианты формата llms-ctx.
LLMS_VARIANTS: Final = (
    "llms.txt",
    "llms-full.txt",
    "llms-small.txt",
    "llms-medium.txt",
    "llms-ctx.txt",
    "llms-ctx-full.txt",
)
# Встроенный реестр: проверенное, в чём уверены; удалить нельзя.
LLMS_DEFAULT_SOURCES: Final[tuple[KnownSource, ...]] = (
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
        full_size=40_000_000,
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
        covers="ruff: правила линтера и их коды, форматтер, конфигурация, интеграция с редакторами",
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
        covers="корень семейства Pydantic — Validation, AI, Logfire; отсюда ссылки на индекс каждого продукта",
        default=True,
    ),
    KnownSource(
        domain="docs.pydantic.dev/latest",
        index="https://docs.pydantic.dev/latest/llms.txt",
        covers="pydantic: модели, валидаторы, сериализация, справочник API, разбор текстов ошибок",
        full_size=2_000_000,
        default=True,
    ),
    KnownSource(
        domain="gofastmcp.com",
        index="https://gofastmcp.com/llms.txt",
        covers=(
            "FastMCP: серверы и клиенты MCP на Python — инструменты, ресурсы, "
            "промпты, монтирование, транспорты, аутентификация, тесты"
        ),
        full_size=3_000_000,
        default=True,
    ),
)
