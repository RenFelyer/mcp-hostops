"""Кэш статусов доступности хостов: JSON с отметкой времени в runtime-каталоге.

Кэш — это оптимизация: при любой ошибке ввода-вывода или мусоре в файле сервер
просто мерит заново.
"""

import contextlib
from collections.abc import Mapping

from . import store
from .config.environment import Settings
from .schemas import Availability, as_availability


def read(s: Settings) -> tuple[float, dict[str, Availability]]:
    """Прочитать кэш.

    Returns:
        Возраст записи в секундах и статусы по алиасам; чужое значение в файле
        читается как «unknown». Возраст `inf` — кэша нет или он битый.
    """
    age, data = store.load_stamped(s.cache_file)
    hosts = data.get("hosts")
    if not isinstance(hosts, Mapping):
        return age, {}
    return age, {alias: as_availability(status) for alias, status in hosts.items()}


def write(statuses: Mapping[str, Availability], s: Settings) -> None:
    """Записать статусы; ошибка ввода-вывода молча глотается."""
    with contextlib.suppress(OSError):
        store.save_stamped(s.cache_file, {"hosts": dict(statuses)})
