"""JSON-файлы состояния: чтение с терпимостью к мусору и атомарная запись."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, Any] | None:
    """Прочитать JSON-объект; None — файла нет, он битый или это не объект."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save(path: Path, data: Mapping[str, Any]) -> None:
    """Записать атомарно через временный файл и `replace`; ошибки — наружу.

    Raises:
        OSError: каталог недоступен или диск не принял запись.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
