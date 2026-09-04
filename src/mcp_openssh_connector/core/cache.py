"""Кэш статусов доступности хостов: JSON в runtime-каталоге.

Формат: `{"checked_at": <epoch>, "hosts": {<alias>: <status>}}`. Кэш — это
оптимизация: при любой ошибке ввода-вывода сервер просто мерит заново.
"""

import contextlib
from collections.abc import Mapping
from math import inf
from time import time

from . import store
from .config import Settings


def read(s: Settings) -> tuple[float, dict[str, str]]:
    """Прочитать кэш.

    Returns:
        Возраст записи в секундах и статусы по алиасам. Возраст `inf` — кэша
        нет или он битый.
    """
    data = store.load(s.cache_file) or {}
    try:
        return time() - float(data["checked_at"]), dict(data["hosts"])
    except (ValueError, KeyError, TypeError):
        return inf, {}


def write(statuses: Mapping[str, str], s: Settings) -> None:
    """Записать статусы; ошибка ввода-вывода молча глотается."""
    with contextlib.suppress(OSError):
        store.save(s.cache_file, {"checked_at": time(), "hosts": dict(statuses)})
