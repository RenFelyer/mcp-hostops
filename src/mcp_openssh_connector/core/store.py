"""Файлы состояния и приватные каталоги.

Чтение терпимо к мусору: битый файл равен отсутствующему. Запись атомарна —
через уникальный временный файл в том же каталоге и `replace`, так что два
сервера, пишущие одновременно, не портят файл друг другу, а читатель никогда не
видит файл, записанный наполовину.
"""

import json
import os
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from time import time

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from .config.constants import PRIVATE_DIR_MODE
from .schemas import Json

_OBJECT = TypeAdapter(dict[str, Json])


class _Stamped(BaseModel):
    """Запись с отметкой времени; остальные поля — содержимое."""

    model_config = ConfigDict(extra="allow")

    checked_at: float


def load(path: Path) -> dict[str, Json] | None:
    """Прочитать JSON-объект; None — файла нет, он битый или это не объект."""
    try:
        return _OBJECT.validate_json(path.read_bytes())
    except (OSError, ValidationError):
        return None


def write_bytes(path: Path, data: bytes) -> None:
    """Записать байты атомарно; ошибки — наружу.

    Raises:
        OSError: каталог недоступен или диск не принял запись.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def save(path: Path, data: Mapping[str, Json]) -> None:
    """Записать JSON-объект атомарно; ошибки — наружу.

    Raises:
        OSError: каталог недоступен или диск не принял запись.
    """
    write_bytes(path, json.dumps(data, ensure_ascii=False).encode())


def load_stamped(path: Path) -> tuple[float, dict[str, Json]]:
    """Прочитать запись с отметкой времени.

    Returns:
        Возраст записи в секундах и её содержимое без отметки. Возраст `inf` и
        пустое содержимое — файла нет, он битый или отметка не число.
    """
    try:
        stamped = _Stamped.model_validate(load(path))
    except ValidationError:
        return float("inf"), {}
    return time() - stamped.checked_at, stamped.model_extra or {}


def save_stamped(path: Path, data: Mapping[str, Json]) -> None:
    """Записать содержимое с текущей отметкой времени; ошибки — наружу."""
    save(path, {"checked_at": time(), **data})


def private_dir(path: Path) -> Path:
    """Создать каталог 0700 и убедиться, что он наш и закрыт от других.

    Нужен там, где чужой каталог опасен: сокет ControlMaster в подставленном
    каталоге отдал бы соединение и пароль sudo чужому процессу. Проверяется сам
    путь, без перехода по символической ссылке: подставленная ссылка увела бы
    сокеты в каталог, выбранный не нами.

    Raises:
        PermissionError: путь — не каталог (в том числе ссылка), принадлежит не
            нам или открыт группе/остальным.
        OSError: каталог не создаётся.
    """
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError(f"каталог {path} должен быть нашим, не ссылкой и с правами 0700")
    return path
