"""Кэш статусов доступности: JSON в runtime-каталоге.

Формат: `{"checked_at": <epoch>, "hosts": {<alias>: <status>}}`. Запись атомарна
через временный файл и `replace`, чтобы читатель не увидел половину. Кэш — это
оптимизация: при любой ошибке ввода-вывода сервер просто мерит заново.
"""

import json
from collections.abc import Mapping
from math import inf
from time import time

from .config import Settings


def read(s: Settings) -> tuple[float, dict[str, str]]:
    """Прочитать кэш.

    Returns:
        Возраст записи в секундах и статусы по алиасам. Возраст `inf` — кэша
        нет или он битый.
    """
    try:
        data = json.loads(s.cache_file.read_text(encoding="utf-8"))
        return time() - float(data["checked_at"]), dict(data["hosts"])
    except (OSError, ValueError, KeyError, TypeError):
        return inf, {}


def write(statuses: Mapping[str, str], s: Settings) -> None:
    """Записать статусы атомарно; ошибка ввода-вывода молча глотается."""
    payload = {"checked_at": time(), "hosts": dict(statuses)}
    try:
        s.cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = s.cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(s.cache_file)
    except OSError:
        pass
