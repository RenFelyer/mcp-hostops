"""Отладочный лог в файл; в обычной работе выключен.

Включается переменной `OPENSSH_MCP_DEBUG_LOG=<путь>`: тогда всё от уровня DEBUG,
включая fastmcp, пишется в этот файл. Файл держится с правами 0600 — в записях
бывают команды и имена хостов. Без переменной обработчики не добавляются, и
сервер по stdio ничего лишнего не выводит. Пароль sudo в лог не попадает
никогда: логируются argv и коды, но не stdin.
"""

import logging
from pathlib import Path

from .environment import get_settings

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_FILE_MODE = 0o600


def setup() -> None:
    """Подключить файл отладки, если он задан в настройках."""
    path: Path | None = get_settings().debug_log
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=_FILE_MODE)
    path.chmod(_FILE_MODE)  # `touch` не трогает права существующего файла
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(_FORMAT))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
