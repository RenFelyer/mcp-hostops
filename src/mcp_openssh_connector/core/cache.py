"""Кэш статусов доступности хостов: JSON с отметкой времени в runtime-каталоге.

Кэш — это оптимизация: при любой ошибке ввода-вывода или мусоре в файле сервер
просто мерит заново.
"""

import contextlib
from collections.abc import Mapping

from pydantic import TypeAdapter, ValidationError

from . import store
from .config.environment import Settings
from .schemas import Availability

_HOSTS = TypeAdapter(dict[str, Availability])


def read(s: Settings) -> tuple[float, dict[str, Availability]]:
    """Прочитать кэш.

    Returns:
        Возраст записи в секундах и статусы по алиасам. Возраст `inf` и пустые
        статусы — кэша нет, он битый или в нём чужое значение статуса.
    """
    age, data = store.load_stamped(s.cache_file)
    try:
        return age, _HOSTS.validate_python(data.get("hosts"))
    except ValidationError:
        return float("inf"), {}


def write(statuses: Mapping[str, Availability], s: Settings) -> None:
    """Записать статусы; ошибка ввода-вывода молча глотается."""
    with contextlib.suppress(OSError):
        store.save_stamped(s.cache_file, {"hosts": dict(statuses)})
